"""Structured messages exchanged between agents (SPEC section 7).

Every agent reads and writes one of these Pydantic models. They are serialized
to JSON for LLM prompts, the call log and the state store, so the hand-off
between agents is always a validated, typed object - never free text.

Field names are FIXED by the spec. The only additions here are lenient
validators (clamp scores to 0..1, treat naive datetimes as UTC, keep at most 3
hypotheses) so that small LLM formatting slips do not fail a whole run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


FaultType = Literal[
    "latency", "dependency_down", "bad_deploy", "memory_leak", "goroutine_leak",
    "resource_exhaustion", "misconfiguration", "slow_query", "no_fault",
]


class Alert(BaseModel):
    """What the system receives: the SYMPTOM, not the cause."""
    alert_name: str
    service: str
    severity: Literal["warning", "critical"]
    started_at: UTCDateTime
    summary: str
    labels: dict[str, str] = Field(default_factory=dict)


class Task(BaseModel):
    """One unit of work for one specialist agent."""
    task_id: str
    agent: str
    objective: str
    window_start: UTCDateTime
    window_end: UTCDateTime
    focus_services: list[str]


class InvestigationPlan(BaseModel):
    incident_id: str
    alert: Alert
    window_start: UTCDateTime
    window_end: UTCDateTime
    focus_services: list[str]
    tasks: list[Task]


class Evidence(BaseModel):
    evidence_id: str
    source: Literal["logs", "metrics", "changes", "verification"]
    service: str
    signal: str                        # e.g. "p95 latency of POST /charge 2.3s vs 0.06s baseline"
    observed_at: UTCDateTime | None = None
    strength: float                    # 0..1
    raw_ref: str | None = None         # the query / commit sha that produced it

    @field_validator("strength")
    @classmethod
    def _clamp_strength(cls, v: float) -> float:
        return _clamp01(v)


class EvidenceBundle(BaseModel):
    """Output of a specialist (Log / Metrics / Change)."""
    agent: str
    task_id: str
    status: Literal["ok", "partial", "failed"]
    evidence: list[Evidence]
    notes: str = ""


class Hypothesis(BaseModel):
    hypothesis_id: str
    root_cause_service: str
    fault_type: FaultType
    explanation: str
    supporting_evidence: list[str]     # evidence_ids
    suspect_commit: str | None = None
    confidence: float

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        return _clamp01(v)


class HypothesisSet(BaseModel):
    hypotheses: list[Hypothesis]       # ranked, max 3

    @field_validator("hypotheses")
    @classmethod
    def _max_three(cls, v: list[Hypothesis]) -> list[Hypothesis]:
        return v[:3]


class Verdict(BaseModel):
    hypothesis_id: str
    status: Literal["confirmed", "rejected", "inconclusive"]
    checks: list[Evidence]
    reasoning: str


class IncidentReport(BaseModel):
    incident_id: str
    status: Literal["complete", "partial", "failed"]
    root_cause_service: str
    fault_type: FaultType
    confidence: float
    suspect_commit: str | None
    summary: str
    timeline: list[str]
    evidence_refs: list[str]
    recommended_actions: list[str]
    limitations: list[str]


class AgentFailure(BaseModel):
    """Returned instead of the normal output when an agent cannot finish."""
    agent: str
    error: str
    attempts: int
