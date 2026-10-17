"""Per-agent share of all LLM + tool calls in one run, from its JSONL call log.

Proves SPEC requirement 2.4: no agent performs more than 40% of the calls.
Attempts that failed and were retried (status "retry") are not counted twice.

    uv run --project agents python eval/load_report.py logs/run_<id>.jsonl          # print
    uv run --project agents python eval/load_report.py logs/run_<id>.jsonl --write  # + docs/results.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # run as a script: make the `eval` package importable

import typer  # noqa: E402

LIMIT = 0.40
RESULTS_MD = ROOT / "docs" / "results.md"


def agent_shares(records: list[dict]) -> list[dict]:
    """[{agent, llm, tool, total, share, ok}] sorted by share, largest first."""
    counts: dict[str, dict[str, int]] = {}
    for rec in records:
        if rec["status"] == "retry":
            continue
        c = counts.setdefault(rec["agent"], {"llm": 0, "tool": 0})
        c[rec["call_type"]] += 1
    total = sum(c["llm"] + c["tool"] for c in counts.values()) or 1
    rows = [{"agent": agent, "llm": c["llm"], "tool": c["tool"], "total": c["llm"] + c["tool"],
             "share": (c["llm"] + c["tool"]) / total} for agent, c in counts.items()]
    for row in rows:
        row["ok"] = row["share"] <= LIMIT
    return sorted(rows, key=lambda r: r["share"], reverse=True)


def to_markdown(rows: list[dict], source: str) -> str:
    total = sum(r["total"] for r in rows)
    lines = [f"Call log: `{source}` ({total} calls). Limit: no agent above {LIMIT:.0%}.", "",
             "| agent | LLM calls | tool calls | total | share | ≤ 40% |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['agent']} | {r['llm']} | {r['tool']} | {r['total']} | {r['share']:.0%} | {'✅' if r['ok'] else '❌'} |"
              for r in rows]
    return "\n".join(lines)


def replace_section(text: str, name: str, body: str) -> str:
    """Replace the content between <!-- name:start --> and <!-- name:end --> (append if missing)."""
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    block = f"{start}\n{body}\n{end}"
    if start in text and end in text:
        return text[:text.index(start)] + block + text[text.index(end) + len(end):]
    return text.rstrip() + "\n\n" + block + "\n"


def read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(call_log: Path = typer.Argument(..., exists=True, help="logs/run_<id>.jsonl"),
         write: bool = typer.Option(False, help="Also write the table into docs/results.md.")) -> None:
    rows = agent_shares(read_log(call_log))
    source = str(call_log.resolve().relative_to(ROOT)) if call_log.resolve().is_relative_to(ROOT) else str(call_log)
    table = to_markdown(rows, source)
    print(table)
    if write:
        RESULTS_MD.write_text(replace_section(RESULTS_MD.read_text(encoding="utf-8"), "load-report", table),
                              encoding="utf-8")
        print(f"\nwritten to {RESULTS_MD.relative_to(ROOT)}")
    if not all(r["ok"] for r in rows):
        raise typer.Exit(1)


if __name__ == "__main__":
    typer.run(main)
