"""Durable Desktop-only orchestration for AutoHunt business phases.

This is deliberately not a daemon.  ChatGPT Desktop performs the semantic
phase between two bounded Playwright phases, so SQLite is the only hand-off
mechanism and the only recovery record.
"""

from __future__ import annotations

from contextlib import closing

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .context import bootstrap
from .storage import migrate


def _now() -> str:
    return datetime.now(UTC).isoformat()


class BusinessBatch:
    """State machine: READ → SEMANTIC → APPLY_RECONCILE, with no browser."""

    def __init__(self, database: Path):
        self.database = database
        migrate(database)

    def start_read(self, *, queries: list[str]) -> str:
        context = bootstrap(self.database)
        candidate_id = str(context.get("candidate_id") or "")
        binding = context.get("account_binding") or {}
        if not candidate_id or not binding or not queries:
            raise RuntimeError("active candidate/account binding and queries required")
        run_id = f"business-read:{uuid.uuid4()}"
        now = _now()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "UPDATE candidate_runs SET status='interrupted' ,finished_at=? WHERE candidate_id=? AND binding_id=? AND mode='business_read' AND status='running'",
                (now, candidate_id, binding["binding_id"]),
            )
            db.execute(
                "INSERT INTO candidate_runs(run_id,candidate_id,binding_id,mode,status,started_at,finished_at) VALUES(?,?,?,?,?,?,NULL)",
                (run_id, candidate_id, binding["binding_id"], "business_read", "running", now),
            )
            self._checkpoint(db, candidate_id, str(binding["binding_id"]), run_id, "READ_RUNNING", {"queries": queries})
        return run_id

    def complete_read(self, run_id: str, *, full_read: int, incidents: int) -> None:
        self._transition(run_id, from_status="running", to_status="awaiting_semantic", stage="SEMANTIC_PENDING", payload={"full_read": full_read, "incidents": incidents})

    def fail_read(self, run_id: str, *, detail: str) -> None:
        self._transition(run_id, from_status="running", to_status="needs_reconciliation", stage="READ_FAILED", payload={"detail": detail}, finish=True)

    def begin_apply(self, run_id: str) -> int:
        """Permit browser apply only after durable decisions built an action queue."""
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute("SELECT candidate_id,binding_id,status FROM candidate_runs WHERE run_id=? AND mode='business_read'", (run_id,)).fetchone()
            if not row or row[2] != "awaiting_semantic":
                raise RuntimeError("batch is not awaiting semantic review")
            ready = db.execute("SELECT COUNT(*) FROM candidate_action_queue WHERE candidate_id=?", (row[0],)).fetchone()[0]
            if not ready:
                raise RuntimeError("no evidence-gated applications ready")
            db.execute("UPDATE candidate_runs SET status='apply_running' WHERE run_id=?", (run_id,))
            self._checkpoint(db, str(row[0]), str(row[1]), run_id, "APPLY_RUNNING", {"ready": ready})
        return int(ready)

    def complete_apply(self, run_id: str, *, applied: list[str], stopped: str | None) -> None:
        self._transition(
            run_id, from_status="apply_running", to_status="completed" if stopped is None else "needs_reconciliation",
            stage="COMPLETED" if stopped is None else "NEEDS_RECONCILIATION", payload={"applied": applied, "stopped": stopped}, finish=True,
        )

    def status(self) -> dict[str, object]:
        context = bootstrap(self.database)
        candidate_id = str(context.get("candidate_id") or "")
        with closing(sqlite3.connect(self.database)) as db, db:
            runs = db.execute("SELECT run_id,mode,status,started_at,finished_at FROM candidate_runs WHERE candidate_id=? AND mode='business_read' ORDER BY started_at DESC LIMIT 5", (candidate_id,)).fetchall()
            semantic = db.execute("SELECT COUNT(*) FROM candidate_work_items WHERE candidate_id=? AND kind='semantic_decision' AND state='pending'", (candidate_id,)).fetchone()[0]
            ready = db.execute("SELECT COUNT(*) FROM candidate_action_queue WHERE candidate_id=?", (candidate_id,)).fetchone()[0]
        return {"runs": [dict(zip(("run_id", "mode", "status", "started_at", "finished_at"), row, strict=True)) for row in runs], "semantic_pending": semantic, "ready_to_apply": ready}

    def _transition(self, run_id: str, *, from_status: str, to_status: str, stage: str, payload: dict[str, object], finish: bool = False) -> None:
        now = _now()
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute("SELECT candidate_id,binding_id FROM candidate_runs WHERE run_id=? AND mode='business_read' AND status=?", (run_id, from_status)).fetchone()
            if not row:
                raise RuntimeError(f"invalid batch transition from {from_status}")
            db.execute("UPDATE candidate_runs SET status=?,finished_at=? WHERE run_id=?", (to_status, now if finish else None, run_id))
            self._checkpoint(db, str(row[0]), str(row[1]), run_id, stage, payload)

    @staticmethod
    def _checkpoint(db: sqlite3.Connection, candidate_id: str, binding_id: str, run_id: str, stage: str, payload: dict[str, object]) -> None:
        db.execute(
            "INSERT INTO candidate_checkpoints(candidate_id,binding_id,name,value_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,binding_id,name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (candidate_id, binding_id, "business_batch", json.dumps({"run_id": run_id, "stage": stage, **payload}, ensure_ascii=False), _now()),
        )
