"""Regenerates chaos/patches/*.patch from readable edit definitions.

A deploy patch must apply cleanly to target-repo's `baseline`. When services/
changes, run this (after `make target-init` on a fresh target-repo) to rebuild
every patch: each edit is applied to a temporary clone, and `git diff` is saved
with a Subject/Author header (see chaos/deploy_sim.py). tests/test_patches.py
fails if a patch no longer applies.

    rm -rf target-repo && make target-init && python3 scripts/make_patches.py
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "chaos" / "patches"

# name -> commit subject, (fictional) author, and edits: (file, old text, new text); old=None creates the file.
PATCHES = {
    "s04_order_promotions.patch": dict(
        subject="order: apply loyalty promotions to new orders",
        author="Aigerim Sadykova <aigerim.sadykova@shop.example>",
        edits=[("order/main.go",
                'var errTooManyOrders = errors.New("too many orders")\n',
                'var errTooManyOrders = errors.New("too many orders")\n\n'
                '// promotion is a loyalty discount for a group of customers (PROMO-142).\n'
                'type promotion struct {\n\tDiscount float64\n}\n\n'
                '// promotions by customer group (customer_id % 5).\n'
                'var promotions = map[int]*promotion{\n\t0: {Discount: 0.10},\n}\n'),
               ("order/main.go",
                '\to := order{CustomerID: req.CustomerID, ProductID: req.ProductID, Quantity: req.Quantity, '
                'Amount: req.Amount, Status: "confirmed"}\n',
                '\to := order{CustomerID: req.CustomerID, ProductID: req.ProductID, Quantity: req.Quantity, '
                'Amount: req.Amount, Status: "confirmed"}\n'
                "\t// Apply the customer's loyalty promotion.\n"
                '\tpromo := promotions[req.CustomerID%5]\n'
                '\to.Amount = o.Amount * (1 - promo.Discount)\n')]),
    "s06_order_pool_size.patch": dict(
        subject="order: lower DB pool size to reduce connections on shared postgres",
        author="Daniyar Bekov <daniyar.bekov@shop.example>",
        edits=[("order/config.yaml", "  max_conns: 10\n", "  max_conns: 2\n")]),
    "s08_gateway_payment_url.patch": dict(
        subject="gateway: point payment client at the new payment endpoint",
        author="Maria Ivanova <maria.ivanova@shop.example>",
        edits=[("gateway/config.yaml", 'payment_url: "http://toxiproxy:18082"', 'payment_url: "http://toxiproxy:18092"')]),
    "s09_order_drop_index.patch": dict(
        subject="order: drop unused orders(customer_id, created_at) index to speed up inserts",
        author="Nurlan Abenov <nurlan.abenov@shop.example>",
        edits=[("order/migrations/002_drop_unused_orders_index.sql", None,
                "-- The (customer_id, created_at) index is big and slows down inserts; reports do not use it.\n"
                "DROP INDEX IF EXISTS orders_customer_created_idx;\n")]),
    "noise_notification_template.patch": dict(
        subject="notification: friendlier order confirmation text",
        author="Dana Omarova <dana.omarova@shop.example>",
        edits=[("notification/config.yaml",
                'template: "Thank you! Your order #{order_id} ({amount} USD) is confirmed."',
                'template: "Great choice! Order #{order_id} is confirmed - total {amount} USD. Enjoy your meal!"')]),
    "noise_payment_refactor.patch": dict(
        subject="payment: extract charge request validation",
        author="Timur Akhmetov <timur.akhmetov@shop.example>",
        edits=[("payment/main.go",
                "\tif err := obs.ReadJSON(r, &req); err != nil || req.Amount <= 0 {\n",
                "\tif err := obs.ReadJSON(r, &req); err != nil || !req.valid() {\n"),
               ("payment/main.go",
                "func randomHex(n int) string {\n",
                "// valid reports whether the charge request can be sent to the provider.\n"
                "func (c chargeRequest) valid() bool {\n\treturn c.Amount > 0 && c.CustomerID > 0\n}\n\n"
                "func randomHex(n int) string {\n")]),
    "noise_inventory_cache_ttl.patch": dict(
        subject="inventory: cache the product list for 15s instead of 10s",
        author="Arman Seitkali <arman.seitkali@shop.example>",
        edits=[("inventory/config.yaml", "  ttl_s: 10 ", "  ttl_s: 15 ")]),
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    repo = tmp / "repo"
    subprocess.run(["git", "clone", "-q", str(ROOT / "target-repo"), str(repo)], check=True)
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        for name, spec in PATCHES.items():
            git(repo, "reset", "-q", "--hard", "baseline")
            git(repo, "clean", "-q", "-fd")
            for rel, old, new in spec["edits"]:
                path = repo / rel
                if old is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(new)
                    continue
                text = path.read_text()
                assert text.count(old) == 1, f"{name}: expected text found {text.count(old)}x in {rel}"
                path.write_text(text.replace(old, new))
            if any(rel.endswith(".go") for rel, _, _ in spec["edits"]):
                subprocess.run(["gofmt", "-w", str(repo)], check=True)
            git(repo, "add", "-A")
            diff = git(repo, "diff", "--cached")
            (OUT / name).write_text(f"Subject: {spec['subject']}\nAuthor: {spec['author']}\n\n{diff}")
            print(f"wrote {name} ({len(diff.splitlines())} diff lines)")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
