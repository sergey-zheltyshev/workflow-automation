"""Durable HH UI-map memory shared by fresh agent sessions.

Browser Use or direct Playwright may discover a screen.  This module records
what was observed; it never performs browser input, navigation or submission.
"""
from contextlib import closing
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .storage import migrate


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _active_candidate(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
    if row is None:
        raise RuntimeError("active candidate is required")
    return str(row[0])


def record_observation(
    database: Path, *, surface: str, url: str, fingerprint: str, evidence: dict[str, Any], candidate_id: str | None = None,
) -> str:
    """Persist read-only evidence for a screen. Evidence must contain no secrets."""
    if not all(value.strip() for value in (surface, url, fingerprint)):
        raise ValueError("surface, url and fingerprint are required")
    migrate(database)
    observation_id = f"uiobs:{uuid.uuid4()}"
    with closing(sqlite3.connect(database)) as db, db:
        candidate_id = candidate_id or _active_candidate(db)
        db.execute(
            "INSERT INTO hh_ui_observations VALUES(?,?,?,?,?,?,?)",
            (observation_id, candidate_id, surface, url, fingerprint, json.dumps(evidence, ensure_ascii=False), _now()),
        )
    return observation_id


def open_incident(
    database: Path, *, surface: str, detail: str, evidence: dict[str, Any], safe_resume: dict[str, Any], vacancy_id: str | None = None,
    candidate_id: str | None = None,
) -> str:
    """Create NEEDS_MAP state. Caller must do this before retrying an unknown UI."""
    if not surface.strip() or not detail.strip():
        raise ValueError("surface and detail are required")
    migrate(database)
    incident_id = f"uiinc:{uuid.uuid4()}"
    with closing(sqlite3.connect(database)) as db, db:
        candidate_id = candidate_id or _active_candidate(db)
        db.execute(
            "INSERT INTO hh_ui_incidents VALUES(?,?,?,?,?,?,?,?,?,?)",
            (incident_id, candidate_id, vacancy_id, surface, "needs_map", detail, json.dumps(evidence, ensure_ascii=False), json.dumps(safe_resume, ensure_ascii=False), _now(), None),
        )
    return incident_id


def activate_contract(database: Path, *, surface: str, version: str, contract: dict[str, Any], source: str) -> None:
    """Activate one reviewed UI contract and retire prior active versions for surface."""
    if not all(value.strip() for value in (surface, version, source)):
        raise ValueError("surface, version and source are required")
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE hh_ui_contracts SET active=0 WHERE surface=?", (surface,))
        db.execute(
            "INSERT INTO hh_ui_contracts(surface,version,contract_json,source,observed_at,active) VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(surface,version) DO UPDATE SET contract_json=excluded.contract_json,source=excluded.source,observed_at=excluded.observed_at,active=1",
            (surface, version, json.dumps(contract, ensure_ascii=False), source, _now()),
        )


def seed_static_contracts(database: Path) -> int:
    """Load reviewed historical contracts once; live observations can replace them."""
    from .hh_ui_contract import CONTRACT

    migrate(database)
    inserted = 0
    with closing(sqlite3.connect(database)) as db, db:
        for state, item in CONTRACT.items():
            surface = str(state)
            exists = db.execute("SELECT 1 FROM hh_ui_contracts WHERE surface=?", (surface,)).fetchone()
            if exists:
                continue
            db.execute(
                "INSERT INTO hh_ui_contracts(surface,version,contract_json,source,observed_at,active) VALUES(?,?,?,?,?,1)",
                (surface, "historical-v1", json.dumps({"selectors": item.selectors, "allowed_actions": item.allowed_actions, "transitions": [str(value) for value in item.transitions], "reconciliation": item.reconciliation, "evidence": item.evidence}, ensure_ascii=False), "compiled historical HH research", _now()),
            )
            inserted += 1
    return inserted


def map_status(database: Path) -> list[dict[str, object]]:
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT c.surface,c.version,c.source,c.observed_at,"
            "(SELECT COUNT(*) FROM hh_ui_incidents i WHERE i.surface=c.surface AND i.status!='resolved') AS open_incidents "
            "FROM hh_ui_contracts c WHERE c.active=1 ORDER BY c.surface"
        ).fetchall()
    return [dict(row) for row in rows]
