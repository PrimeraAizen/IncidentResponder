"""Fault primitives used by scenario steps, and the reset back to a healthy baseline.

- Toxiproxy: add "toxics" (latency, timeout, ...) to the proxies between each
  service and its dependencies. The always-on baseline toxics (DB network
  latency, see infra/toxiproxy/baseline/) are re-applied after every reset.
- Docker Compose: stop containers, start them again, rebuild services.
- /admin/chaos: in-process faults (memory/goroutine leak, CPU burn). The admin
  port is internal, so we reach it with `docker compose exec`.
- Extra load: a second load generator for scenario-specific traffic.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TARGET_REPO = ROOT / "target-repo"
DEPLOY_EVENTS = ROOT / "data" / "deploy_events.jsonl"
BASELINE_TOXICS = ROOT / "infra" / "toxiproxy" / "baseline"
SHOP_SERVICES = ["gateway", "order", "payment", "inventory", "notification"]  # built from target-repo


def toxiproxy_url() -> str:
    return os.getenv("TOXIPROXY_URL", "http://localhost:8474").rstrip("/")


# ------------------------------------------------------------------ Toxiproxy
def add_toxic(proxy: str, toxic_type: str, stream: str = "downstream", toxicity: float = 1.0,
              **attributes: int) -> None:
    """Add a toxic to a proxy. YAML keys ending in _ms map to Toxiproxy attributes (latency_ms -> latency)."""
    attrs = {k.removesuffix("_ms"): v for k, v in attributes.items()}
    body = {"name": f"{proxy}_{toxic_type}", "type": toxic_type, "stream": stream,
            "toxicity": toxicity, "attributes": attrs}
    httpx.post(f"{toxiproxy_url()}/proxies/{proxy}/toxics", json=body, timeout=5).raise_for_status()


def reset_toxiproxy() -> None:
    """Remove all toxics, re-enable all proxies, then restore the baseline toxics."""
    httpx.post(f"{toxiproxy_url()}/reset", timeout=5).raise_for_status()
    for file in sorted(BASELINE_TOXICS.glob("*.json")):
        resp = httpx.post(f"{toxiproxy_url()}/proxies/{file.stem}/toxics",
                          content=file.read_bytes(), timeout=5)
        if resp.status_code not in (200, 409):
            resp.raise_for_status()


# ------------------------------------------------------------- Docker Compose
def target_commit() -> str:
    out = subprocess.run(["git", "-C", str(TARGET_REPO), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run `docker compose ...` from the repo root with TARGET_COMMIT set (image build arg)."""
    env = {**os.environ, "TARGET_COMMIT": target_commit()}
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          check=check)


def stop_service(service: str) -> None:
    compose("stop", service)


def start_stopped() -> None:
    """Start every container that is stopped (without recreating the running ones)."""
    compose("up", "-d", "--no-recreate")


def rebuild(services: list[str], wait_healthy: bool = True) -> None:
    """Rebuild and restart services from target-repo.

    wait_healthy=False returns as soon as the new containers run: a bad release
    may never become healthy (s06), and a deploy pipeline does not wait for that.
    """
    wait = ["--wait", "--wait-timeout", "120"] if wait_healthy else []
    compose("up", "-d", "--build", "--no-deps", *wait, *services)


# ------------------------------------------------------- /admin/chaos (internal)
def set_chaos_flags(service: str, **flags: bool) -> dict:
    """POST /admin/chaos inside the service's container (the admin port is not published)."""
    out = compose("exec", "-T", service, "wget", "-qO-", "--header", "Content-Type: application/json",
                  "--post-data", json.dumps(flags), "http://localhost:9000/admin/chaos")
    return json.loads(out.stdout)


def clear_chaos_flags() -> None:
    """Turn every in-process fault off in every running shop service."""
    for service in SHOP_SERVICES:
        compose("exec", "-T", service, "wget", "-qO-", "--header", "Content-Type: application/json",
                "--post-data", json.dumps({"leak_memory": False, "leak_goroutines": False, "cpu_burn": False}),
                "http://localhost:9000/admin/chaos", check=False)


# ------------------------------------------------------------------ extra load
def start_extra_load(rps: float, duration_s: int, mix: str | None = None) -> None:
    """A second load generator on top of the always-on one (removed when it finishes)."""
    args = ["--target=http://gateway:8080", f"--rps={rps}", f"--duration={duration_s}s"]
    if mix:
        args.append(f"--mix={mix}")
    compose("run", "-d", "--rm", "--no-deps", "loadgen", *args)


def stop_extra_load() -> None:
    ids = subprocess.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=incident-responder",
                          "--filter", "label=com.docker.compose.oneoff=True",
                          "--filter", "label=com.docker.compose.service=loadgen"],
                         capture_output=True, text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "stop", *ids], capture_output=True)


# --------------------------------------------------------------- target-repo
def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(TARGET_REPO), *args], capture_output=True, text=True,
                          check=True).stdout


def changed_services() -> list[str]:
    """Shop services whose code/config differ from the `baseline` tag (committed or not)."""
    paths = set(_git("diff", "--name-only", "baseline").split())
    paths |= {line[3:] for line in _git("status", "--porcelain").splitlines()}
    top = {p.split("/")[0] for p in paths}
    if top & {"pkg", "go.mod", "go.sum", "Dockerfile"}:
        return list(SHOP_SERVICES)  # shared code changed: everything is affected
    return [s for s in SHOP_SERVICES if s in top]


def reset_target_repo() -> list[str]:
    """git reset --hard baseline. Returns the services that must be rebuilt."""
    changed = changed_services()
    if changed or _git("rev-parse", "HEAD") != _git("rev-parse", "baseline^{commit}"):
        _git("reset", "-q", "--hard", "baseline")
        _git("clean", "-q", "-fd")
    return changed


def clear_deploy_events() -> None:
    DEPLOY_EVENTS.parent.mkdir(parents=True, exist_ok=True)
    DEPLOY_EVENTS.write_text("", encoding="utf-8")


def wait_healthy(url: str = "http://localhost:8080/healthz", timeout_s: int = 120) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False
