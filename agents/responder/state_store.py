"""Explicit task state in SQLite (data/incidents.db), SPEC 6.6.

Every graph node saves its output here as JSON, so an investigation can be
inspected (or audited for the report) long after the process ended:

    incidents   one row per run: alert, status, start/end time
    plans       the InvestigationPlan
    evidence    one row per EvidenceBundle
    hypotheses  one row per Diagnostician run (loop = verification round)
    verdicts    one row per Verifier run
    reports     the final IncidentReport and where it was written

The LangGraph checkpointer (data/checkpoints.db) is separate: it stores the
graph state after every step for resume/debugging, in LangGraph's own format.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY, run_id TEXT, alert_json TEXT, status TEXT,
    started_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS plans (
    incident_id TEXT, plan_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS evidence (
    incident_id TEXT, agent TEXT, task_id TEXT, status TEXT, bundle_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS hypotheses (
    incident_id TEXT, loop INTEGER, set_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS verdicts (
    incident_id TEXT, loop INTEGER, hypothesis_id TEXT, status TEXT, verdict_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS reports (
    incident_id TEXT, status TEXT, report_json TEXT, md_path TEXT, created_at TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(model: BaseModel) -> str:
    return model.model_dump_json()


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The specialists run in parallel threads; one connection guarded by a lock is enough.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def _insert(self, sql: str, *params: object) -> None:
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def start_incident(self, incident_id: str, run_id: str, alert: BaseModel) -> None:
        self._insert("INSERT INTO incidents VALUES (?, ?, ?, 'running', ?, NULL)",
                     incident_id, run_id, _json(alert), _now())

    def save_plan(self, incident_id: str, plan: BaseModel) -> None:
        self._insert("INSERT INTO plans VALUES (?, ?, ?)", incident_id, _json(plan), _now())

    def save_evidence(self, incident_id: str, bundle) -> None:
        self._insert("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?)", incident_id, bundle.agent,
                     bundle.task_id, bundle.status, _json(bundle), _now())

    def save_hypotheses(self, incident_id: str, loop: int, hypotheses: BaseModel) -> None:
        self._insert("INSERT INTO hypotheses VALUES (?, ?, ?, ?)", incident_id, loop, _json(hypotheses), _now())

    def save_verdict(self, incident_id: str, loop: int, verdict) -> None:
        self._insert("INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, ?)", incident_id, loop,
                     verdict.hypothesis_id, verdict.status, _json(verdict), _now())

    def save_report(self, incident_id: str, report, md_path: str | None) -> None:
        self._insert("INSERT INTO reports VALUES (?, ?, ?, ?, ?)", incident_id, report.status,
                     _json(report), md_path, _now())
        with self._lock:
            self._db.execute("UPDATE incidents SET status = ?, finished_at = ? WHERE incident_id = ?",
                             (report.status, _now(), incident_id))
            self._db.commit()

    def load(self, incident_id: str) -> dict:
        """Everything stored for one incident (used by tests and for debugging)."""
        out: dict = {}
        with self._lock:
            for table, column in [("incidents", "alert_json"), ("plans", "plan_json"), ("evidence", "bundle_json"),
                                  ("hypotheses", "set_json"), ("verdicts", "verdict_json"), ("reports", "report_json")]:
                rows = self._db.execute(f"SELECT {column} FROM {table} WHERE incident_id = ?", (incident_id,)).fetchall()
                out[table] = [json.loads(r[0]) for r in rows]
        return out

    def close(self) -> None:
        self._db.close()
