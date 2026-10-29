"""Canonical vacancy lifecycle writes.

Historical process rows remain evidence.  New code writes this small state
machine so the operator can distinguish review, application, cover and chat
work without guessing from a UI colour or an old score.
"""
from __future__ import annotations

from contextlib import closing

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .storage import migrate

STATES = frozenset({
    "DISCOVERED", "LIVE_READ", "SEMANTIC_REVIEWED", "READY_TO_APPLY",
    "FORM_REQUIRES_REVIEW", "APPLICATION_IN_FLIGHT", "APPLICATION_CONFIRMED",
    "COVER_PENDING", "COVER_CONFIRMED", "CHAT_REPLY_PENDING",
    "CHAT_REPLY_CONFIRMED", "REJECTED", "CLOSED", "NEEDS_RECONCILIATION",
})


def record_lifecycle(
    database: Path, *, candidate_id: str, vacancy_id: str, state: str,
    decision_id: str | None = None, live_revision: str | None = None,
    deadline_at: str | None = None, note: str = "",
) -> None:
    """Persist one explicit state; invalid states never silently enter queue."""
    if state not in STATES:
        raise ValueError(f"unknown lifecycle state: {state}")
    if not vacancy_id.isdigit():
        raise ValueError("vacancy_id is required")
    migrate(database)
    stamp = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "INSERT INTO candidate_vacancy_lifecycle("
            "candidate_id,vacancy_id,state,decision_id,live_revision,deadline_at,note,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(candidate_id,vacancy_id) DO UPDATE SET "
            "state=excluded.state,decision_id=excluded.decision_id,"
            "live_revision=excluded.live_revision,deadline_at=excluded.deadline_at,"
            "note=excluded.note,updated_at=excluded.updated_at",
            (candidate_id, vacancy_id, state, decision_id, live_revision, deadline_at, note, stamp),
        )
