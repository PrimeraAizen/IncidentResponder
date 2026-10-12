"""docker_inspect tool (Change Investigator): container state of the system's components.

Read-only (Docker SDK): state, health, restart count, OOM kill, start time,
image. Only the components of the system under investigation are visible;
the harness containers (toxiproxy, loadgen) are not (anti-leakage rule).
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import docker
from pydantic import BaseModel, Field

from responder.tools.base import Tool, ToolContext, ToolError

SYSTEM_COMPONENTS = ("gateway", "order", "payment", "inventory", "notification", "postgres", "redis", "nats")


def docker_client(timeout_s: int) -> docker.DockerClient:
    """DOCKER_HOST if set, else the usual socket locations (Linux, then Docker Desktop on macOS)."""
    if os.getenv("DOCKER_HOST"):
        return docker.from_env(timeout=timeout_s)
    for sock in (Path("/var/run/docker.sock"), Path.home() / ".docker" / "run" / "docker.sock"):
        if sock.exists():
            return docker.DockerClient(base_url=f"unix://{sock}", timeout=timeout_s)
    raise ToolError("Docker is not reachable (no DOCKER_HOST and no docker.sock found)")


def _parse_docker_time(value: str) -> datetime | None:
    """Docker uses nanosecond timestamps and 0001-01-01 for 'never'."""
    if not value or value.startswith("0001-"):
        return None
    value = re.sub(r"(\.\d{6})\d*", r"\1", value).replace("Z", "+00:00")
    return datetime.fromisoformat(value)


class DockerInspectArgs(BaseModel):
    service: str | None = Field(None, description="Component name, e.g. order or postgres; omit for all components")


def docker_inspect(args: DockerInspectArgs, ctx: ToolContext) -> dict:
    if args.service and args.service not in SYSTEM_COMPONENTS:
        raise ToolError(f"unknown component {args.service!r}; known: {', '.join(SYSTEM_COMPONENTS)}")
    wanted = [args.service] if args.service else list(SYSTEM_COMPONENTS)
    if ctx.docker is None:
        ctx.docker = docker_client(ctx.settings.tool_timeout_s)

    found: dict[str, dict] = {}
    for c in ctx.docker.containers.list(
            all=True, filters={"label": f"com.docker.compose.project={ctx.settings.compose_project}"}):
        service = c.labels.get("com.docker.compose.service")
        if service in wanted and c.labels.get("com.docker.compose.oneoff") != "True":
            found[service] = _summarize(service, c.attrs, ctx)
    return {"containers": [found.get(s, {"service": s, "state": "missing"}) for s in wanted]}


def _summarize(service: str, attrs: dict, ctx: ToolContext) -> dict:
    state = attrs["State"]
    info = {"service": service, "state": state["Status"],
            "health": (state.get("Health") or {}).get("Status"),
            "restart_count": attrs.get("RestartCount", 0), "oom_killed": state.get("OOMKilled", False),
            "exit_code": state.get("ExitCode"), "image": attrs["Config"]["Image"]}
    started = _parse_docker_time(state.get("StartedAt", ""))
    if started:
        info["started_at"] = started.isoformat(timespec="seconds")
        info["started_vs_alert_s"] = ctx.vs_alert(started)
    finished = _parse_docker_time(state.get("FinishedAt", ""))
    if finished and state["Status"] != "running":
        info["finished_at"] = finished.isoformat(timespec="seconds")
        info["finished_vs_alert_s"] = ctx.vs_alert(finished)
    return info


DOCKER_INSPECT = Tool("docker_inspect",
                      "Container state of system components: running/exited, health, restart count, "
                      "OOM-killed, start time (vs_alert_s = seconds relative to the alert). Omit service for all.",
                      DockerInspectArgs, docker_inspect)
