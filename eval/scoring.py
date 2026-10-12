"""Scoring of one investigation against a scenario's ground truth (SPEC 8). Pure functions.

A run is correct iff
  - root_cause_service ∈ {expected} ∪ accepted_services, and
  - fault_type matches, and
  - no must_not_blame service is named as the root cause.
For scenarios caused by a deploy (`suspect_commit: from_deploy`), the reported
suspect commit is scored separately ("commit accuracy").
"""

from __future__ import annotations

from typing import Any


def commit_matches(reported: str | None, expected: list[str]) -> bool:
    """Short and full shas match if one is a prefix of the other (at least 7 characters)."""
    if not reported:
        return False
    reported = reported.strip().lower()
    return any(len(c) >= 7 and len(reported) >= 7 and (reported.startswith(c.lower()) or c.lower().startswith(reported))
               for c in expected)


def score(expected: dict[str, Any], report: Any, scenario_commits: list[str]) -> dict[str, Any]:
    """`report` is an IncidentReport (or None); `scenario_commits` are the commits made by the fault's deploy steps."""
    if report is None:
        return {"correct": False, "root_ok": False, "fault_ok": False, "blamed_forbidden": False,
                "commit_expected": expected.get("suspect_commit") == "from_deploy", "commit_ok": None,
                "reason": "no report"}
    allowed = {expected["root_cause_service"], *expected.get("accepted_services", [])}
    root_ok = report.root_cause_service in allowed
    fault_ok = report.fault_type == expected["fault_type"]
    blamed_forbidden = report.root_cause_service in set(expected.get("must_not_blame", []))
    commit_expected = expected.get("suspect_commit") == "from_deploy"
    return {
        "correct": root_ok and fault_ok and not blamed_forbidden,
        "root_ok": root_ok, "fault_ok": fault_ok, "blamed_forbidden": blamed_forbidden,
        "commit_expected": commit_expected,
        "commit_ok": commit_matches(report.suspect_commit, scenario_commits) if commit_expected else None,
    }
