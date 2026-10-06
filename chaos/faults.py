"""Fault primitives used by scenario steps, and the reset back to a healthy baseline.

- Toxiproxy: add "toxics" (latency, timeout, ...) to the proxies that sit
  between each service and its dependencies.
- Docker Compose: stop/start containers and rebuild services (Phase 2+).
- target-repo: reset to the `baseline` tag (Phase 2 adds deploy-sim patches).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TARGET_REPO = ROOT / "target-repo"
SHOP_SERVICES = ["gateway", "order", "payment"]  # built from target-repo (Phase 2: + inventory, notification)


def toxiproxy_url() -> str:
    return os.getenv("TOXIPROXY_URL", "http://localhost:8474").rstrip("/")


# ------------------------------------------------------------------ Toxiproxy
def add_toxic(proxy: str, toxic_type: str, stream: str = "downstream", toxicity: float = 1.0,
              **attributes: int) -> None:
    """Add a toxic to a proxy. YAML keys ending in _ms map to Toxiproxy attributes (latency_ms -> latency)."""
    attrs = {k.removesuffix("_ms"): v for k, v in attributes.items()}
    body = {"name": f"{proxy}_{toxic_type}", "type": toxic_type, "stream": stream,
            "toxicity": toxicity, "attributes": attrs}
    resp = httpx.post(f"{toxiproxy_url()}/proxies/{proxy}/toxics", json=body, timeout=5)
    resp.raise_for_status()


def reset_toxiproxy() -> None:
    """Remove all toxics and re-enable all proxies."""
    httpx.post(f"{toxiproxy_url()}/reset", timeout=5).raise_for_status()


# ----------------------------------------------------------------- Compose/git
def target_commit() -> str:
    out = subprocess.run(["git", "-C", str(TARGET_REPO), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def compose(*args: str) -> subprocess.CompletedProcess:
    """Run `docker compose ...` from the repo root with TARGET_COMMIT set (image build arg)."""
    env = {**os.environ, "TARGET_COMMIT": target_commit()}
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          check=True)


def reset_target_repo() -> bool:
    """git reset --hard baseline. Returns True if anything changed (services must be rebuilt)."""
    git = ["git", "-C", str(TARGET_REPO)]
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
    baseline = subprocess.run([*git, "rev-parse", "baseline^{commit}"], capture_output=True, text=True,
                              check=True).stdout
    dirty = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
    if head == baseline and not dirty:
        return False
    subprocess.run([*git, "reset", "-q", "--hard", "baseline"], check=True)
    subprocess.run([*git, "clean", "-q", "-fd"], check=True)
    return True


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
