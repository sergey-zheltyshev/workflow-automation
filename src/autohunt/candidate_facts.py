"""Verified candidate facts used by Codex for truthful decisions and question answers."""

from contextlib import closing
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .storage import migrate


def _active_candidate(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
    if row is None:
        raise RuntimeError("active candidate is required")
    return str(row[0])


def record_fact(database: Path, fact_key: str, fact_value: str, *, source: str, candidate_id: str | None = None) -> None:
    if not fact_key.strip() or not fact_value.strip() or not source.strip():
        raise ValueError("candidate fact key, value and source are required")
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        candidate_id = candidate_id or _active_candidate(db)
        db.execute(
            "INSERT INTO candidate_facts_v2(candidate_id,fact_key,fact_value,source,verified_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(candidate_id,fact_key) DO UPDATE SET fact_value=excluded.fact_value,"
            "source=excluded.source,verified_at=excluded.verified_at",
            (candidate_id, fact_key, fact_value, source, datetime.now(UTC).isoformat()),
        )


def all_facts(database: Path, *, candidate_id: str | None = None) -> dict[str, str]:
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        candidate_id = candidate_id or _active_candidate(db)
        return {str(key): str(value) for key, value in db.execute("SELECT fact_key,fact_value FROM candidate_facts_v2 WHERE candidate_id=?", (candidate_id,))}


def record_profile_version(database: Path, version_id: str, facts: dict[str, str], sources: dict[str, str], *, candidate_id: str | None = None) -> None:
    """Persist sourced candidate knowledge; decisions must not depend on chat memory."""
    if not version_id or set(facts) != set(sources):
        raise ValueError("profile version requires sourced facts")
    migrate(database)
    created_at = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(database)) as db, db:
        candidate_id = candidate_id or _active_candidate(db)
        db.execute("INSERT OR REPLACE INTO candidate_profiles VALUES(?,?,?,?,?)", (candidate_id, version_id, json.dumps(sources, ensure_ascii=False), json.dumps(facts, ensure_ascii=False), created_at))
        db.executemany("INSERT OR REPLACE INTO candidate_profile_facts_v2 VALUES(?,?,?,?,?)", [(candidate_id, version_id, key, value, sources[key]) for key, value in facts.items()])
        db.executemany("INSERT INTO candidate_facts_v2(candidate_id,fact_key,fact_value,source,verified_at) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,fact_key) DO UPDATE SET fact_value=excluded.fact_value,source=excluded.source,verified_at=excluded.verified_at", [(candidate_id, key, value, sources[key], created_at) for key, value in facts.items()])
