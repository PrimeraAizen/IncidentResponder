"""Deploy simulator: a real release of one service in target-repo.

    python -m chaos deploy order s04_order_promotions.patch

1. `git apply` the patch (the code or config change);
2. bump <service>/VERSION (patch level, like a release);
3. commit with the patch's realistic author and message;
4. rebuild and restart the service (its build_info metric now shows the new commit);
5. append {ts, service, version, commit, author, message} to data/deploy_events.jsonl.

These are exactly the traces the Change Investigator reads later: git history,
the deploy log and container restarts. Nothing here tells it whether a change
was harmless ("noise") or the cause of an incident.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from chaos import faults

PATCHES_DIR = Path(__file__).resolve().parent / "patches"

# Harmless changes deployed shortly before every scenario, so that "the latest
# deploy is guilty" is not a winning strategy. Order = preference.
NOISE_PATCHES = {
    "notification": "noise_notification_template.patch",
    "payment": "noise_payment_refactor.patch",
    "inventory": "noise_inventory_cache_ttl.patch",
}


@dataclass
class Patch:
    path: Path
    subject: str
    author: str  # "Name <email>"

    @property
    def author_name(self) -> str:
        return self.author.split(" <")[0]

    @property
    def author_email(self) -> str:
        return self.author.split(" <")[1].rstrip(">")


def read_patch(name: str) -> Patch:
    """A patch file starts with `Subject:` and `Author:` lines; `git apply` ignores them."""
    path = PATCHES_DIR / name
    header: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            break
        key, _, value = line.partition(": ")
        header[key] = value
    return Patch(path=path, subject=header["Subject"], author=header["Author"])


def bump_version(service: str) -> str:
    """1.4.2 -> 1.4.3 in target-repo/<service>/VERSION."""
    file = faults.TARGET_REPO / service / "VERSION"
    major, minor, patch = file.read_text().strip().split(".")
    version = f"{major}.{minor}.{int(patch) + 1}"
    file.write_text(version + "\n")
    return version


def deploy(service: str, patch_name: str) -> dict:
    patch = read_patch(patch_name)
    git = ["git", "-C", str(faults.TARGET_REPO)]
    subprocess.run([*git, "apply", str(patch.path)], check=True, capture_output=True, text=True)
    version = bump_version(service)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "-c", f"user.name={patch.author_name}", "-c", f"user.email={patch.author_email}",
                    "-c", "commit.gpgsign=false", "commit", "-q", "-m", patch.subject], check=True)
    commit = faults.target_commit()

    faults.rebuild([service], wait_healthy=False)  # a broken release may never become healthy
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "service": service,
             "version": version, "commit": commit, "author": patch.author, "message": patch.subject}
    faults.DEPLOY_EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with faults.DEPLOY_EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    return event


def noise_services(avoid: set[str], count: int = 2) -> list[str]:
    """Services for harmless deploys: never one that the scenario's ground truth involves."""
    return [s for s in NOISE_PATCHES if s not in avoid][:count]


def deploy_noise(services: list[str]) -> list[dict]:
    return [deploy(service, NOISE_PATCHES[service]) for service in services]
