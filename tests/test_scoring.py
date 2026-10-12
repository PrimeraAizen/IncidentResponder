"""Scoring rules (SPEC 8)."""

from eval.scoring import commit_matches, score
from responder.schemas import IncidentReport


def report(root: str, fault: str, commit: str | None = None) -> IncidentReport:
    return IncidentReport(incident_id="i", status="complete", root_cause_service=root, fault_type=fault,
                          confidence=0.8, suspect_commit=commit, summary="", timeline=[], evidence_refs=[],
                          recommended_actions=[], limitations=[])


S01 = {"root_cause_service": "payment", "fault_type": "latency", "accepted_services": [], "must_not_blame": ["gateway"]}
S09 = {"root_cause_service": "postgres", "fault_type": "slow_query", "accepted_services": ["order"], "must_not_blame": []}
S04 = {"root_cause_service": "order", "fault_type": "bad_deploy", "suspect_commit": "from_deploy",
       "accepted_services": [], "must_not_blame": []}


def test_correct_needs_service_and_fault_type():
    assert score(S01, report("payment", "latency"), [])["correct"]
    assert not score(S01, report("payment", "dependency_down"), [])["correct"]
    assert not score(S01, report("order", "latency"), [])["correct"]


def test_accepted_services_count_as_correct():
    assert score(S09, report("order", "slow_query"), [])["correct"]


def test_must_not_blame():
    result = score(S01, report("gateway", "latency"), [])
    assert not result["correct"] and result["blamed_forbidden"]


def test_commit_accuracy_is_reported_separately():
    good = score(S04, report("order", "bad_deploy", "c311c5d"), ["c311c5d"])
    assert good["correct"] and good["commit_ok"]
    no_commit = score(S04, report("order", "bad_deploy", None), ["c311c5d"])
    assert no_commit["correct"] and no_commit["commit_ok"] is False
    assert score(S01, report("payment", "latency"), [])["commit_ok"] is None


def test_commit_prefix_matching():
    assert commit_matches("c311c5d", ["c311c5d4e2f0"]) and commit_matches("C311C5D4E2", ["c311c5d"])
    assert not commit_matches("c311", ["c311c5d"]) and not commit_matches(None, ["c311c5d"])


def test_no_report_is_wrong():
    assert not score(S01, None, [])["correct"]
