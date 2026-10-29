"""Durable Codex vacancy decisions. JSONL transports; SQLite is source of truth."""

from __future__ import annotations

from contextlib import closing

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import bootstrap
from .storage import migrate


class VacancyDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vacancy_id: str = Field(pattern=r"^\d+$")
    decision: Literal["apply", "skip", "needs_review"]
    reason: str = Field(min_length=1)
    score: int | None = Field(default=None, ge=0, le=100)
    cover_letter: str | None = None
    question_answers: dict[str, str] = Field(default_factory=dict)
    vacancy_evidence: list[str] = Field(default_factory=list)
    candidate_fact_keys: list[str] = Field(default_factory=list)
    question_evidence: dict[str, list[str]] = Field(default_factory=dict)
    unknown_user_facts: list[str] = Field(default_factory=list)
    revision: str = Field(min_length=1)
    source_run: str = Field(min_length=1)

    @model_validator(mode="after")
    def apply_requires_final_text(self) -> VacancyDecisionInput:
        if self.decision == "apply" and (not self.cover_letter or not self.cover_letter.strip()):
            raise ValueError("apply decision requires a final cover_letter")
        if self.cover_letter is not None and not self.cover_letter.strip():
            raise ValueError("cover_letter must be nonblank when present")
        if self.decision == "apply" and not [item for item in self.vacancy_evidence if item.strip()]:
            raise ValueError("apply decision requires exact vacancy_evidence")
        if self.decision == "apply" and not [item for item in self.candidate_fact_keys if item.strip()]:
            raise ValueError("apply decision requires candidate_fact_keys")
        if self.question_answers and set(self.question_answers) != set(self.question_evidence):
            raise ValueError("every question answer requires question_evidence")
        return self


def _now() -> str:
    return datetime.now(UTC).isoformat()


def persist_semantic_decisions(database: Path, rows: list[VacancyDecisionInput]) -> int:
    """Official semantic-operator contract; SQLite, not a plan file, is authority.

    The caller may be Codex, but it cannot select a resume, response route, or
    manufacture an executable request.  It only resolves a vacancy already
    persisted by the collector.  Queue construction stays downstream.
    """
    if len({(row.vacancy_id, row.revision) for row in rows}) != len(rows):
        raise ValueError("duplicate vacancy/revision in semantic decision batch")
    migrate(database)
    context = bootstrap(database)
    required = ("candidate_id", "candidate_profile_version", "job_policy_version")
    if any(not context.get(key) for key in required):
        raise RuntimeError("active candidate profile and job policy are required before decision import")
    candidate_id = str(context["candidate_id"])
    profile_version = str(context["candidate_profile_version"])
    policy_version = str(context["job_policy_version"])
    with closing(sqlite3.connect(database)) as db, db:
        for row in rows:
            decision_id = f"decision:{row.vacancy_id}:{row.revision}"
            gate = db.execute(
                "SELECT eligibility,rule_id FROM candidate_vacancy_state WHERE candidate_id=? AND vacancy_id=?",
                (candidate_id, row.vacancy_id),
            ).fetchone()
            if row.decision == "apply" and gate and gate[0] == "hard_reject":
                raise ValueError(f"hard reject cannot be overridden: {gate[1]}")
            if row.decision == "apply" and gate and gate[0] == "unknown_requires_semantic":
                db.execute("UPDATE candidate_vacancy_state SET eligibility='eligible',detail='semantic resolution: apply',updated_at=? WHERE candidate_id=? AND vacancy_id=?", (_now(), candidate_id, row.vacancy_id))
            if row.decision == "apply" and gate is None:
                raise ValueError(f"vacancy has no deterministic evaluation: {row.vacancy_id}")
            if row.decision == "apply" and row.unknown_user_facts:
                raise ValueError("apply cannot retain unresolved user facts")
            revision = db.execute(
                "SELECT full_description FROM vacancy_fact_revisions WHERE vacancy_id=? AND revision=?",
                (row.vacancy_id, row.revision),
            ).fetchone()
            full_description = str(revision[0] or "") if revision else ""
            if row.decision == "apply" and (not full_description or any(fragment.casefold() not in full_description.casefold() for fragment in row.vacancy_evidence)):
                raise ValueError("vacancy_evidence must be exact fragments of the current full vacancy text")
            known_fact_keys = {
                str(value[0]) for value in db.execute(
                    "SELECT fact_key FROM candidate_facts_v2 WHERE candidate_id=? UNION SELECT fact_key FROM candidate_profile_facts_v2 WHERE candidate_id=?",
                    (candidate_id, candidate_id),
                )
            }
            if row.decision == "apply" and not set(row.candidate_fact_keys) <= known_fact_keys:
                raise ValueError("candidate_fact_keys must be verified active-candidate facts")
            db.execute(
                "INSERT INTO candidate_vacancy_decisions(candidate_id,decision_id,vacancy_id,candidate_profile_version,job_policy_version,vacancy_revision,source_run,decision,score,reason,cover_letter,question_answers_json,unknown_user_facts_json,decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id,vacancy_id,vacancy_revision) DO UPDATE SET source_run=excluded.source_run,"
                "decision=excluded.decision,score=excluded.score,reason=excluded.reason,"
                "cover_letter=excluded.cover_letter,question_answers_json=excluded.question_answers_json,"
                "unknown_user_facts_json=excluded.unknown_user_facts_json,decided_at=excluded.decided_at",
                (
                    candidate_id, decision_id, row.vacancy_id, profile_version, policy_version, row.revision, row.source_run, row.decision, row.score,
                    row.reason, row.cover_letter, json.dumps(row.question_answers, ensure_ascii=False),
                    json.dumps(row.unknown_user_facts, ensure_ascii=False), _now(),
                ),
            )
            db.execute(
                "INSERT INTO candidate_decision_evidence(candidate_id,decision_id,vacancy_evidence_json,candidate_fact_keys_json,question_evidence_json,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(candidate_id,decision_id) DO UPDATE SET vacancy_evidence_json=excluded.vacancy_evidence_json,candidate_fact_keys_json=excluded.candidate_fact_keys_json,question_evidence_json=excluded.question_evidence_json,created_at=excluded.created_at",
                (candidate_id, decision_id, json.dumps(row.vacancy_evidence, ensure_ascii=False), json.dumps(row.candidate_fact_keys, ensure_ascii=False), json.dumps(row.question_evidence, ensure_ascii=False), _now()),
            )
            db.execute(
                "UPDATE candidate_work_items SET state='resolved',updated_at=? "
                "WHERE candidate_id=? AND vacancy_id=? AND kind='semantic_decision' AND state='pending'",
                (_now(), candidate_id, row.vacancy_id),
            )
    return len(rows)


def create_semantic_work_item(database: Path, *, vacancy_id: str, source_run: str) -> str | None:
    """Create one idempotent pending semantic task for eligible/unknown vacancy."""
    migrate(database)
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    if not candidate_id:
        raise RuntimeError("active candidate required")
    with closing(sqlite3.connect(database)) as db, db:
        state = db.execute(
            "SELECT eligibility,detail FROM candidate_vacancy_state WHERE candidate_id=? AND vacancy_id=?",
            (candidate_id, vacancy_id),
        ).fetchone()
        if not state or state[0] == "hard_reject":
            return None
        latest = db.execute(
            "SELECT revision FROM vacancy_fact_revisions WHERE vacancy_id=? ORDER BY captured_at DESC LIMIT 1",
            (vacancy_id,),
        ).fetchone()
        if latest is None:
            raise RuntimeError(f"full vacancy revision required before semantic work: {vacancy_id}")
        decision = db.execute(
            "SELECT 1 FROM candidate_vacancy_decisions WHERE candidate_id=? AND vacancy_id=? AND vacancy_revision=?",
            (candidate_id, vacancy_id, latest[0]),
        ).fetchone()
        if decision:
            return None
        item_id = f"semantic:{vacancy_id}"
        payload = json.dumps({"vacancy_id": vacancy_id, "eligibility": state[0], "detail": state[1], "source_run": source_run}, ensure_ascii=False)
        db.execute(
            "INSERT INTO candidate_work_items(candidate_id,item_id,run_id,kind,vacancy_id,state,payload_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'pending',?,?,?) ON CONFLICT(candidate_id,item_id) DO UPDATE SET run_id=excluded.run_id,state='pending',payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (candidate_id, item_id, source_run, "semantic_decision", vacancy_id, payload, _now(), _now()),
        )
    return item_id


def import_decisions(database: Path, path: Path) -> int:
    """Compatibility transport for tests/offline tooling; production never needs it."""
    rows = [VacancyDecisionInput.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return persist_semantic_decisions(database, rows)


def status(database: Path) -> dict[str, int]:
    migrate(database)
    context = bootstrap(database)
    candidate_id = context.get("candidate_id")
    with closing(sqlite3.connect(database)) as db, db:
        rows = db.execute("SELECT decision, COUNT(*) FROM candidate_vacancy_decisions WHERE candidate_id=? GROUP BY decision", (candidate_id,)).fetchall() if candidate_id else []
        intents = db.execute("SELECT status, COUNT(*) FROM action_intents GROUP BY status").fetchall()
    return {
        **{f"decisions_{decision}": count for decision, count in rows},
        **{f"intents_{state}": count for state, count in intents},
    }
