import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from autohunt.application_run import build_authoritative_application_queue
from autohunt.candidate_facts import record_profile_version
from autohunt.context import evaluate_and_record_eligibility, provision_candidate, select_candidate
from autohunt.decisions import (
    VacancyDecisionInput,
    create_semantic_work_item,
    import_decisions,
    persist_semantic_decisions,
    status,
)


def row(**changes):
    result = {
        "vacancy_id": "1",
        "decision": "apply",
        "reason": "Direct operational fit",
        "score": 80,
        "cover_letter": "Здравствуйте! Это точное письмо.",
        "question_answers": {"q1": "verified answer"},
        "vacancy_evidence": ["Remote operations"],
        "candidate_fact_keys": ["name"],
        "question_evidence": {"q1": ["Remote operations"]},
        "unknown_user_facts": [],
        "revision": "live-1-v1",
        "source_run": "live-1",
    }
    result.update(changes)
    return result


def test_apply_requires_cover_letter():
    with pytest.raises(ValidationError, match="requires a final cover_letter"):
        VacancyDecisionInput.model_validate(row(cover_letter=None))


def test_import_is_durable_and_idempotent():
    database = Path("var/test_decisions.sqlite")
    path = Path("var/test_decisions.jsonl")
    database.unlink(missing_ok=True)
    path.write_text(json.dumps(row(), ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        provision_candidate(database, candidate_id="a", display_name="A", job_policy={"allowed_work_formats": ["remote"]}, automation_policy={}, account_binding={"hh_identity": "a", "resume_id": "r", "resume_title": "R"}, job_policy_version="jp-a", automation_policy_version="ap-a", binding_id="b-a")
        select_candidate(database, "a")
        record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
        evaluate_and_record_eligibility(database, {"vacancy_id": "1", "work_format": "remote"})
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO vacancies VALUES('1','https://hh.ru/vacancy/1','Role','Co','now')")
            db.execute("INSERT INTO vacancy_fact_revisions VALUES('1','live-1-v1','{}','h','run','now','Remote operations')")
        assert import_decisions(database, path) == 1
        assert import_decisions(database, path) == 1
        assert status(database)["decisions_apply"] == 1
    finally:
        path.unlink(missing_ok=True)


def test_import_rejects_duplicate_vacancy_revision():
    database = Path("var/test_decisions_duplicates.sqlite")
    path = Path("var/test_decisions_duplicates.jsonl")
    database.unlink(missing_ok=True)
    path.write_text("\n".join([json.dumps(row()), json.dumps(row())]), encoding="utf-8")
    try:
        provision_candidate(database, candidate_id="a", display_name="A", job_policy={"allowed_work_formats": ["remote"]}, automation_policy={}, account_binding={"hh_identity": "a", "resume_id": "r", "resume_title": "R"}, job_policy_version="jp-a", automation_policy_version="ap-a", binding_id="b-a")
        select_candidate(database, "a")
        record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
        with pytest.raises(ValueError, match="duplicate"):
            import_decisions(database, path)
    finally:
        path.unlink(missing_ok=True)


def test_semantic_apply_resolves_unknown_and_builds_system_queue():
    database = Path("var/test_semantic_queue.sqlite")
    database.unlink(missing_ok=True)
    provision_candidate(database, candidate_id="a", display_name="A", job_policy={"allowed_work_formats": ["remote"]}, automation_policy={}, account_binding={"hh_identity": "a", "resume_id": "https://hh.ru/resume/r", "resume_title": "R"}, job_policy_version="jp-a", automation_policy_version="ap-a", binding_id="b-a")
    select_candidate(database, "a")
    record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
    # UNKNOWN is a semantic task, not a permanent no-apply state.
    evaluate_and_record_eligibility(database, {"vacancy_id": "1", "work_format": None})
    import sqlite3
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO vacancies(vacancy_id,url,title,company) VALUES('1','https://hh.ru/vacancy/1','Assistant','Co')")
        db.execute("INSERT INTO vacancy_fact_revisions VALUES('1','r1','{}','h','run','now','Remote operations')")
    assert create_semantic_work_item(database, vacancy_id="1", source_run="run") == "semantic:1"
    persist_semantic_decisions(database, [VacancyDecisionInput.model_validate(row(revision="r1", source_run="run"))])
    queue = build_authoritative_application_queue(database)
    assert [(item.vacancy_id, item.resume_id, item.expected_cover_letter) for item in queue] == [("1", "https://hh.ru/resume/r", "Здравствуйте! Это точное письмо.")]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT state FROM candidate_work_items WHERE candidate_id='a' AND item_id='semantic:1'").fetchone()[0] == "resolved"
