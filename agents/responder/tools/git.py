"""git_log and git_diff tools (Change Investigator): read-only access to the target repo.

Only `git log` and `git show` are ever run, with validated arguments
(a commit must look like a sha, a path must stay inside the repo).
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime

from pydantic import BaseModel, Field

from responder.schemas import UTCDateTime
from responder.tools.base import Tool, ToolContext, ToolError

MAX_COMMITS = 20
MAX_DIFF_CHARS = 3500
_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")
_FIELD_SEP = "\x1f"  # unit separator: never appears in commit messages


def _git(ctx: ToolContext, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(ctx.settings.target_repo), *args],
                            capture_output=True, text=True, timeout=ctx.settings.tool_timeout_s)
    if result.returncode != 0:
        raise ToolError(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
    return result.stdout


class GitLogArgs(BaseModel):
    since: UTCDateTime | None = Field(None, description="Default: start of the investigation window")
    until: UTCDateTime | None = Field(None, description="Default: end of the investigation window")
    path: str | None = Field(None, description="Only commits touching this path, e.g. order/ or gateway/config.yaml")


def git_log(args: GitLogArgs, ctx: ToolContext) -> dict:
    since, until = ctx.window(args.since, args.until)
    cmd = ["log", f"--since={since.isoformat()}", f"--until={until.isoformat()}", f"-n{MAX_COMMITS}",
           f"--format=%x1e%h{_FIELD_SEP}%an <%ae>{_FIELD_SEP}%aI{_FIELD_SEP}%s", "--name-only"]
    if args.path:
        if args.path.startswith("/") or ".." in args.path:
            raise ToolError("path must be relative to the repository root")
        cmd += ["--", args.path]
    commits = []
    for block in _git(ctx, *cmd).split("\x1e"):
        if not block.strip():
            continue
        header, *files = [line for line in block.splitlines() if line.strip()]
        sha, author, date, subject = header.split(_FIELD_SEP)
        commits.append({"commit": sha, "author": author, "date": date, "message": subject,
                        "vs_alert_s": ctx.vs_alert(datetime.fromisoformat(date)), "files": files})
    return {"window": [since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds")],
            "commits": commits}


class GitDiffArgs(BaseModel):
    commit: str = Field(description="Commit sha (short or full)")


def git_diff(args: GitDiffArgs, ctx: ToolContext) -> dict:
    commit = args.commit.strip().lower()
    if not _SHA_RE.match(commit):
        raise ToolError(f"{args.commit!r} is not a commit sha")
    text = _git(ctx, "show", "--format=%h %an: %s%n", "--stat", "--patch", commit)
    truncated = len(text) > MAX_DIFF_CHARS
    return {"commit": commit, "diff": text[:MAX_DIFF_CHARS] + ("\n…[diff truncated]" if truncated else "")}


GIT_LOG = Tool("git_log", "Commits in the target repo in a time window: sha, author, date, message, changed files.",
               GitLogArgs, git_log)
GIT_DIFF = Tool("git_diff", "The change made by one commit (stat + patch, truncated).", GitDiffArgs, git_diff)
