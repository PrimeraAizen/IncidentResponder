"""Change, health and report tools against fakes: a temp git repo, a deploy log file,
a fake Docker client and the mocked HTTP API (no live system needed)."""

import json
import subprocess
from datetime import timedelta

import pytest

from responder.agents.reporter import final_decision
from responder.schemas import AgentFailure, Evidence, Hypothesis, HypothesisSet, IncidentReport, Verdict
from responder.tools.base import ToolContext, ToolError
from responder.tools.deploys import DeployEventsArgs, deploy_events
from responder.tools.docker import DockerInspectArgs, docker_inspect
from responder.tools.git import GitDiffArgs, GitLogArgs, git_diff, git_log
from responder.tools.health import HealthCheckArgs, health_check
from responder.tools.report import WriteReportArgs, write_report

from conftest import NOW, FakeContainer, FakeDocker


@pytest.fixture
def ctx(settings, http, fake_docker) -> ToolContext:
    return ToolContext(settings=settings, http=http, window_start=NOW - timedelta(minutes=30), window_end=NOW,
                       incident_start=NOW - timedelta(minutes=2), docker=fake_docker)


def test_deploy_events_are_filtered_and_relative_to_the_alert(ctx, settings):
    settings.data_dir.mkdir(parents=True)
    events = [{"ts": (NOW - timedelta(hours=2)).isoformat(), "service": "order", "commit": "old"},
              {"ts": (NOW - timedelta(minutes=3)).isoformat(), "service": "order", "commit": "c311c5d"}]
    settings.deploy_events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    result = deploy_events(DeployEventsArgs(), ctx)
    assert [e["commit"] for e in result["events"]] == ["c311c5d"]
    assert result["events"][0]["vs_alert_s"] == -60


def test_git_log_and_diff(ctx, settings):
    repo = settings.target_repo
    repo.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    (repo / "order").mkdir()
    (repo / "order" / "config.yaml").write_text("max_conns: 10\n")
    run("add", "-A")
    run("-c", "user.name=Dev", "-c", "user.email=dev@shop.example", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "order: lower pool", "--date", (NOW - timedelta(minutes=3)).isoformat())
    ctx.window_end = NOW + timedelta(days=3650)  # commit date = now (committer date); widen the window
    commits = git_log(GitLogArgs(), ctx)["commits"]
    assert commits[0]["message"] == "order: lower pool" and commits[0]["files"] == ["order/config.yaml"]
    assert "max_conns: 10" in git_diff(GitDiffArgs(commit=commits[0]["commit"]), ctx)["diff"]
    with pytest.raises(ToolError):
        git_diff(GitDiffArgs(commit="HEAD; rm -rf /"), ctx)
    with pytest.raises(ToolError):
        git_log(GitLogArgs(path="../etc"), ctx)


def test_docker_inspect_reports_stopped_and_oom_killed_containers(ctx):
    ctx.docker = FakeDocker([FakeContainer("postgres", status="exited"),
                             FakeContainer("notification", restarts=1, oom=True), FakeContainer("toxiproxy")])
    result = {c["service"]: c for c in docker_inspect(DockerInspectArgs(), ctx)["containers"]}
    assert result["postgres"]["state"] == "exited" and "finished_vs_alert_s" in result["postgres"]
    assert result["notification"]["restart_count"] == 1 and result["notification"]["oom_killed"]
    assert result["gateway"]["state"] == "missing"
    assert "toxiproxy" not in result                     # harness containers are never visible
    with pytest.raises(ToolError):
        docker_inspect(DockerInspectArgs(service="toxiproxy"), ctx)


def test_health_check_of_infrastructure_asks_its_dependents(ctx):
    assert health_check(HealthCheckArgs(service="payment"), ctx)["status_code"] == 200
    result = health_check(HealthCheckArgs(service="postgres"), ctx)
    assert {d["service"] for d in result["dependents"]} == {"order", "inventory"}


def test_write_report_renders_markdown_and_json(ctx, settings):
    report = IncidentReport(incident_id="inc-1", status="complete", root_cause_service="payment",
                            fault_type="latency", confidence=0.8, suspect_commit=None, summary="Payment was slow.",
                            timeline=["12:00:00 - alert"], evidence_refs=["metrics-1"],
                            recommended_actions=["Check the provider"], limitations=[])
    ev = Evidence(evidence_id="metrics-1", source="metrics", service="gateway", signal="p95 2s", strength=0.9)
    paths = write_report(WriteReportArgs(report=report, evidence=[ev]), ctx)
    md = open(paths["md_path"]).read()
    assert md.startswith("# Incident inc-1: payment / latency") and "| metrics-1 |" in md
    assert json.loads(open(paths["json_path"]).read())["report"]["confidence"] == 0.8


# ---------------------------------------------------------------- final decision
H = Hypothesis(hypothesis_id="h1", root_cause_service="payment", fault_type="latency", explanation="x",
               supporting_evidence=["metrics-1"], confidence=0.7)


def _verdict(status: str) -> Verdict:
    return Verdict(hypothesis_id="h1", status=status, checks=[], reasoning="r")


@pytest.mark.parametrize("status, confidence, report_status", [
    ("confirmed", 0.8, "complete"), ("inconclusive", 0.56, "complete"), ("rejected", 0.35, "partial")])
def test_final_decision_follows_the_verdict(status, confidence, report_status):
    d = final_decision(HypothesisSet(hypotheses=[H]), [_verdict(status)], [H], [])
    assert (d.confidence, d.status) == (confidence, report_status)


def test_final_decision_without_verification_or_with_failures():
    unverified = final_decision(HypothesisSet(hypotheses=[H]), [], [], [])
    assert (unverified.status, unverified.confidence) == ("partial", 0.56)
    degraded = final_decision(HypothesisSet(hypotheses=[H]), [_verdict("confirmed")], [H],
                              [AgentFailure(agent="log_analyst", error="down", attempts=3)])
    assert degraded.status == "partial" and degraded.confidence == 0.64
    assert final_decision(None, [], [], []).status == "failed"


H2 = Hypothesis(hypothesis_id="h1", root_cause_service="nats", fault_type="latency", explanation="y",
                supporting_evidence=["logs-1"], confidence=0.6)


def test_rejected_top_falls_back_to_an_unrejected_hypothesis():
    # H was judged inconclusive, then the Diagnostician switched to H2, which was rejected.
    d = final_decision(HypothesisSet(hypotheses=[H2]), [_verdict("inconclusive"), _verdict("rejected")], [H, H2], [])
    assert d.hypothesis is H and d.verdict.status == "inconclusive"


def test_when_everything_was_rejected_the_most_confident_hypothesis_wins():
    d = final_decision(HypothesisSet(hypotheses=[H2]), [_verdict("rejected"), _verdict("rejected")], [H, H2], [])
    assert d.hypothesis is H and d.status == "partial" and d.confidence == 0.35
    assert any("rejected every hypothesis" in item for item in d.limitations)
