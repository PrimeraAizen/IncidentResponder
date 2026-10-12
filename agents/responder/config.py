"""Runtime settings, read once from environment variables (and the repo's .env file).

All limits from SPEC 6.5 live here so they can be changed without touching code.
API keys are only ever read from the environment - never hard-coded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# agents/responder/config.py -> repo root is two levels above the package.
REPO_ROOT = Path(os.getenv("REPO_ROOT", Path(__file__).resolve().parents[2]))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_provider: str          # "openai" (any OpenAI-compatible endpoint) or "fake"
    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str
    llm_timeout_s: int
    llm_max_rpm: int           # client-side requests/minute cap (0 = unlimited)
    llm_max_retry_wait_s: int  # longest wait between retries, even if the server asks for more
    # Data sources (as seen from the host)
    prom_url: str
    loki_url: str
    # Limits (SPEC 6.5)
    max_tool_calls_per_agent: int
    max_llm_calls_per_agent: int
    max_graph_steps: int
    run_timeout_s: int
    max_verify_loops: int
    tool_timeout_s: int
    retry_attempts: int
    # Paths
    repo_root: Path
    logs_dir: Path
    data_dir: Path
    reports_dir: Path
    target_repo: Path          # git history the Change Investigator reads
    # Live system access for tools
    compose_project: str       # Docker Compose project whose containers docker_inspect may read
    services_host: str         # where the services' /healthz ports are published

    @property
    def deploy_events_path(self) -> Path:
        return self.data_dir / "deploy_events.jsonl"

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "incidents.db"

    @property
    def checkpoint_db_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(REPO_ROOT / ".env", override=False)
        return cls(
            llm_provider=os.getenv("LLM_PROVIDER", "openai").lower(),
            llm_base_url=os.getenv("LLM_BASE_URL") or None,
            llm_api_key=os.getenv("LLM_API_KEY") or None,
            llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            llm_timeout_s=_int("LLM_TIMEOUT_S", 60),
            llm_max_rpm=_int("LLM_MAX_RPM", 0),
            llm_max_retry_wait_s=_int("LLM_MAX_RETRY_WAIT_S", 60),
            prom_url=os.getenv("PROM_URL", "http://localhost:9090").rstrip("/"),
            loki_url=os.getenv("LOKI_URL", "http://localhost:3100").rstrip("/"),
            max_tool_calls_per_agent=_int("MAX_TOOL_CALLS_PER_AGENT", 4),
            max_llm_calls_per_agent=_int("MAX_LLM_CALLS_PER_AGENT", 3),
            max_graph_steps=_int("MAX_GRAPH_STEPS", 25),
            run_timeout_s=_int("RUN_TIMEOUT_S", 180),
            max_verify_loops=_int("MAX_VERIFY_LOOPS", 2),
            tool_timeout_s=_int("TOOL_TIMEOUT_S", 10),
            retry_attempts=_int("RETRY_ATTEMPTS", 3),
            repo_root=REPO_ROOT,
            logs_dir=REPO_ROOT / "logs",
            data_dir=REPO_ROOT / "data",
            reports_dir=REPO_ROOT / "reports",
            target_repo=Path(os.getenv("TARGET_REPO", REPO_ROOT / "target-repo")),
            compose_project=os.getenv("COMPOSE_PROJECT", "incident-responder"),
            services_host=os.getenv("SERVICES_HOST", "localhost"),
        )
