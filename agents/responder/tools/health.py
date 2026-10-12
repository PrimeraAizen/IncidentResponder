"""health_check tool (Verifier): GET /healthz of a service, right now.

/healthz also reports each service's own dependency checks (e.g. order:
{"postgres": "ok", "nats": "nats RECONNECTING"}). Infrastructure components
have no HTTP endpoint, so for them the tool returns the health checks of the
services that depend on them, which is how an on-call engineer would check.
"""

from __future__ import annotations

import time

import httpx
from pydantic import BaseModel, Field

from responder.tools.base import Tool, ToolContext, ToolError
from responder.tools.topology import load_topology

SERVICE_PORTS = {"gateway": 8080, "order": 8081, "payment": 8082, "inventory": 8083, "notification": 8084}


class HealthCheckArgs(BaseModel):
    service: str = Field(description="Service or component, e.g. payment or postgres")


def health_check(args: HealthCheckArgs, ctx: ToolContext) -> dict:
    topology = load_topology()
    if args.service in SERVICE_PORTS:
        return _check(args.service, ctx)
    if args.service in topology:
        dependents = [name for name, c in topology.items() if args.service in c["depends_on"] and name in SERVICE_PORTS]
        return {"component": args.service,
                "note": "no HTTP health endpoint; these are the health checks of the services that use it",
                "dependents": [_check(name, ctx) for name in dependents]}
    raise ToolError(f"unknown service {args.service!r}; known: {', '.join(topology)}")


def _check(service: str, ctx: ToolContext) -> dict:
    url = f"http://{ctx.settings.services_host}:{SERVICE_PORTS[service]}/healthz"
    start = time.monotonic()
    try:
        resp = ctx.http.get(url, timeout=3)
    except httpx.HTTPError as exc:  # unreachable is a result, not a tool failure
        return {"service": service, "status_code": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
    latency_ms = int((time.monotonic() - start) * 1000)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return {"service": service, "status_code": resp.status_code, "latency_ms": latency_ms,
            "status": body.get("status"), "checks": body.get("checks") or {}}


HEALTH_CHECK = Tool("health_check",
                    "Current /healthz of a service (status code, latency, dependency checks). For postgres, "
                    "redis or nats: the health checks of the services that depend on them.",
                    HealthCheckArgs, health_check)
