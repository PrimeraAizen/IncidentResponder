"""Full evaluation (SPEC 9): every scenario × N repeats, scored, summarized into docs/results.md.

    uv run --project agents python eval/run_all.py --repeats 3          # = make eval
    uv run --project agents python eval/run_all.py --only s01 s11       # a subset
    uv run --project agents python eval/run_all.py --summarize eval/results/<ts>   # rebuild the summary only
    uv run --project agents python eval/run_all.py --resume eval/results/<ts>      # continue an interrupted eval
    uv run --project agents python eval/run_all.py --resume eval/results/<ts> --max-runs 5   # only 5 runs now

Each run is saved as it finishes (eval/results/<ts>/<scenario>_<n>.json), so a
crash in run 30 of 36 loses nothing: --resume continues in the same directory
(skipping saved runs) and --summarize rebuilds the tables from the files. If the
LLM provider fails (daily quota used up, or unreachable), the evaluation stops instead of saving failed runs.
The summary also picks a typical correct run, writes its per-agent load report,
and copies its call log and report to logs/sample/ and reports/sample/.
"""

from __future__ import annotations

import json
import shutil
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # run as a script: make the `chaos` and `eval` packages importable

import typer  # noqa: E402

from chaos import runner  # noqa: E402
from chaos.cli import say  # noqa: E402
from chaos.scenario import scenario_files  # noqa: E402
from eval import load_report  # noqa: E402
from eval.run_scenario import LLMUnavailable, console, print_summary, run_once  # noqa: E402
from responder.config import Settings  # noqa: E402

RESULTS_MD = ROOT / "docs" / "results.md"


def summarize(records: list[dict]) -> list[dict]:
    """One row per scenario: correct runs, majority verdict, commit accuracy, means."""
    rows = []
    for scenario in sorted({r["scenario"] for r in records}):
        runs = [r for r in records if r["scenario"] == scenario]
        correct = sum(r["score"]["correct"] for r in runs)
        commit_runs = [r for r in runs if r["score"]["commit_expected"]]
        rows.append({
            "scenario": scenario,
            "expected": f"{runs[0]['expected']['root_cause_service']} / {runs[0]['expected']['fault_type']}",
            "reported": ", ".join(f"{(r['report'] or {}).get('root_cause_service')}/{(r['report'] or {}).get('fault_type')}"
                                  for r in runs),
            "runs": len(runs), "correct": correct, "majority": correct * 2 > len(runs),
            "commit": f"{sum(bool(r['score']['commit_ok']) for r in commit_runs)}/{len(commit_runs)}" if commit_runs else "-",
            "symptoms_ok": all(r["symptoms_ok"] for r in runs),
            "mean_s": statistics.mean(r["investigation_s"] for r in runs),
            "mean_llm": statistics.mean(r["llm_calls"] for r in runs),
            "mean_tools": statistics.mean(r["tool_calls"] for r in runs),
            "mean_tokens": statistics.mean(r["tokens_in"] + r["tokens_out"] for r in runs),
        })
    return rows


def results_markdown(rows: list[dict], records: list[dict], model: str) -> str:
    runs_correct = sum(r["correct"] for r in rows)
    runs_total = sum(r["runs"] for r in rows)
    lines = [
        f"Run on {run_dates(records)} with `{model}`, {max(r['runs'] for r in rows)} repeat(s) per scenario.",
        "",
        f"**Scenarios correct (majority of repeats): {sum(r['majority'] for r in rows)}/{len(rows)}** · "
        f"runs correct: {runs_correct}/{runs_total} ({runs_correct / runs_total:.0%}) · "
        f"mean investigation time: {statistics.mean(r['investigation_s'] for r in records):.0f} s · "
        f"mean tokens per run: {statistics.mean(r['tokens_in'] + r['tokens_out'] for r in records):,.0f}",
        "",
        "| scenario | expected | reported (per run) | correct | majority | commit | time s | LLM | tools | tokens |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [f"| {r['scenario']} | {r['expected']} | {r['reported']} | {r['correct']}/{r['runs']} | "
              f"{'✅' if r['majority'] else '❌'} | {r['commit']} | {r['mean_s']:.0f} | {r['mean_llm']:.1f} | "
              f"{r['mean_tools']:.1f} | {r['mean_tokens']:,.0f} |" for r in rows]
    if not all(r["symptoms_ok"] for r in rows):
        lines += ["", "⚠ Some runs' symptom checks failed (the fault did not fully develop): "
                  + ", ".join(r["scenario"] for r in rows if not r["symptoms_ok"])]
    return "\n".join(lines)


def run_dates(records: list[dict]) -> str:
    """Date span of the runs, from their run ids ("20261017T131529Z-…"); resumed evaluations span several days."""
    days = sorted({f"{r['run_id'][:4]}-{r['run_id'][4:6]}-{r['run_id'][6:8]}" for r in records})
    return days[0] if len(days) == 1 else f"{days[0]} … {days[-1]}"


def typical_run(records: list[dict]) -> dict:
    """The correct run with the median number of calls (falls back to any run)."""
    pool = sorted([r for r in records if r["score"]["correct"]] or records,
                  key=lambda r: r["llm_calls"] + r["tool_calls"])
    return pool[len(pool) // 2]


def write_results(records: list[dict], model: str) -> None:
    text = RESULTS_MD.read_text(encoding="utf-8")
    text = load_report.replace_section(text, "eval", results_markdown(summarize(records), records, model))
    typical = typical_run(records)
    sample_log = ROOT / "logs" / "sample" / "typical_run.jsonl"
    sample_log.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(typical["call_log"], sample_log)
    if typical.get("report_md"):
        sample_report = ROOT / "reports" / "sample" / f"{typical['scenario']}_report.md"
        sample_report.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(typical["report_md"], sample_report)
    shares = load_report.agent_shares(load_report.read_log(sample_log))
    text = load_report.replace_section(text, "load-report", load_report.to_markdown(
        shares, f"logs/sample/typical_run.jsonl ({typical['scenario']}, run {typical['repeat']})"))
    RESULTS_MD.write_text(text, encoding="utf-8")
    console.print(f"[green]✓[/] docs/results.md updated; typical run: {typical['scenario']} → logs/sample/typical_run.jsonl")


def pending_runs(scenarios: list[str], repeats: int, out_dir: Path) -> list[tuple[str, int]]:
    """(scenario, repeat) pairs without a saved result, in round-robin order: all of repeat 1, then repeat 2, …"""
    return [(sc, r) for r in range(1, repeats + 1) for sc in scenarios
            if not list(out_dir.glob(f"{sc}_*_{r}.json"))]


def main(repeats: int = typer.Option(3, help="Runs per scenario."),
         only: list[str] = typer.Option(None, help="Run only these scenario ids (repeatable)."),
         resume: Path = typer.Option(None, help="Continue an interrupted evaluation in this results dir."),
         max_runs: int = typer.Option(0, help="Do at most this many runs now (0 = all remaining); resume later."),
         summarize_dir: Path = typer.Option(None, "--summarize", help="Only rebuild the summary from this results dir.")) -> None:
    settings = Settings.from_env()
    if summarize_dir is None:
        scenarios = only or [p.stem.split("_")[0] for p in scenario_files()]
        out_dir = resume or ROOT / "eval" / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        todo = pending_runs(scenarios, repeats, out_dir)
        if max_runs:
            todo = todo[:max_runs]
        console.print(f"{len(todo)} run(s) to do now: {', '.join(f'{sc}_{r}' for sc, r in todo)}")
        try:
            for scenario_id, repeat in todo:
                run_once(scenario_id, repeat, out_dir, settings)
        except LLMUnavailable as exc:
            console.print(f"[red]✗ {exc}[/]\n  Resume later with: python eval/run_all.py --repeats {repeats} --resume {out_dir}")
        finally:
            runner.reset_all(say)
            console.print(f"Results: {out_dir}")
        summarize_dir = out_dir
    records = [json.loads(p.read_text()) for p in sorted(summarize_dir.glob("s*_*.json"))]
    if records:
        print_summary(records)
        write_results(records, settings.llm_model)


if __name__ == "__main__":
    typer.run(main)
