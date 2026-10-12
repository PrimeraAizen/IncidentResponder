"""deploy_events tool (Change Investigator): releases recorded in data/deploy_events.jsonl.

The deploy log lives in a plain file, not in Postgres, so it can still be read
when the database is the thing that is down.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, Field

from responder.schemas import UTCDateTime
from responder.tools.base import Tool, ToolContext


class DeployEventsArgs(BaseModel):
    start: UTCDateTime | None = Field(None, description="Default: start of the investigation window")
    end: UTCDateTime | None = Field(None, description="Default: end of the investigation window")


def deploy_events(args: DeployEventsArgs, ctx: ToolContext) -> dict:
    start, end = ctx.window(args.start, args.end)
    path = ctx.settings.deploy_events_path
    if not path.exists():
        return {"events": [], "note": "no deploy log found"}
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        ts = datetime.fromisoformat(event["ts"])
        if start <= ts <= end:
            events.append({**event, "vs_alert_s": ctx.vs_alert(ts)})
    events.sort(key=lambda e: e["ts"])
    return {"window": [start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")],
            "events": events}


DEPLOY_EVENTS = Tool("deploy_events",
                     "Deploys (service, version, commit, author, message, time) in the window; "
                     "vs_alert_s is seconds relative to the alert (negative = before).",
                     DeployEventsArgs, deploy_events)
