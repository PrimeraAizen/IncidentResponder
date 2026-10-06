"""Message schemas: JSON round-trip and the lenient validators."""

from datetime import datetime, timezone

from responder.schemas import (AgentFailure, Alert, Evidence, EvidenceBundle, Hypothesis, HypothesisSet,
                               IncidentReport, InvestigationPlan, Task, Verdict)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _evidence(**kw) -> Evidence:
    return Evidence(**{"evidence_id": "metrics-1", "source": "metrics", "service": "gateway",
                       "signal": "p95 gateway→payment 2.05s vs 0.06s", "observed_at": T0, "strength": 0.9,
                       "raw_ref": "histogram_quantile(...)", **kw})


def _hypothesis(**kw) -> Hypothesis:
    return Hypothesis(**{"hypothesis_id": "h1", "root_cause_service": "payment", "fault_type": "latency",
                         "explanation": "slow payment", "supporting_evidence": ["metrics-1"], "confidence": 0.8, **kw})


def test_round_trip_all_messages():
    alert = Alert(alert_name="HighLatency", service="gateway", severity="critical", started_at=T0, summary="slow")
    task = Task(task_id="t1", agent="log_analyst", objective="find errors", window_start=T0, window_end=T0,
                focus_services=["gateway"])
    messages = [
        alert, task,
        InvestigationPlan(incident_id="inc-1", alert=alert, window_start=T0, window_end=T0,
                          focus_services=["gateway"], tasks=[task]),
        EvidenceBundle(agent="metrics_analyst", task_id="t1", status="ok", evidence=[_evidence()]),
        HypothesisSet(hypotheses=[_hypothesis()]),
        Verdict(hypothesis_id="h1", status="confirmed", checks=[_evidence(source="verification")], reasoning="ok"),
        IncidentReport(incident_id="inc-1", status="complete", root_cause_service="payment", fault_type="latency",
                       confidence=0.8, suspect_commit=None, summary="s", timeline=["12:00 alert"],
                       evidence_refs=["metrics-1"], recommended_actions=["check provider"], limitations=[]),
        AgentFailure(agent="metrics_analyst", error="timeout", attempts=3),
    ]
    for msg in messages:
        assert type(msg).model_validate_json(msg.model_dump_json()) == msg


def test_scores_are_clamped_to_0_1():
    assert _evidence(strength=1.7).strength == 1.0
    assert _hypothesis(confidence=-0.2).confidence == 0.0


def test_naive_datetimes_are_treated_as_utc():
    alert = Alert.model_validate({"alert_name": "x", "service": "gateway", "severity": "warning",
                                  "started_at": "2026-10-06T12:00:00", "summary": "s"})
    assert alert.started_at == T0


def test_at_most_three_hypotheses_are_kept():
    hs = HypothesisSet(hypotheses=[_hypothesis(hypothesis_id=f"h{i}") for i in range(5)])
    assert [h.hypothesis_id for h in hs.hypotheses] == ["h0", "h1", "h2"]
