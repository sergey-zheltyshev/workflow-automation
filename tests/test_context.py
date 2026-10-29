import sqlite3
from pathlib import Path

import pytest

from autohunt.candidate_facts import all_facts, record_profile_version
from autohunt.context import (
    bootstrap,
    eligibility,
    evaluate_and_record_eligibility,
    evaluate_persisted_vacancy,
    list_candidates,
    provision_candidate,
    select_candidate,
)
from autohunt.decisions import import_decisions
from autohunt.storage import migrate


def test_candidate_switch_isolation(tmp_path: Path):
    db = tmp_path / "state.sqlite"
    migrate(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO candidates VALUES('a','Candidate A',1,'now')")
        conn.execute("INSERT INTO candidates VALUES('b','Candidate B',0,'now')")
        conn.execute("INSERT INTO job_policy_versions VALUES('pa','a','{\"salary_floor\":100}','test','now')")
        conn.execute("INSERT INTO job_policy_versions VALUES('pb','b','{\"salary_floor\":10}','test','now')")
    select_candidate(db, "b")
    assert bootstrap(db)["candidate_id"] == "b"
    assert [item["candidate_id"] for item in list_candidates(db)] == ["a", "b"]


def test_policy_sends_low_salary_to_semantic_review():
    policy = {"salary_floor": 100, "currency": "RUB", "allowed_countries": ["Россия"], "allowed_work_formats": ["remote"], "forbidden_schedules": ["6/1"]}
    assert eligibility(policy, {"salary_from": 50, "currency": "RUB", "country": "Россия", "work_format": "remote", "schedule": "5/2"}) == ("unknown_requires_semantic", "salary_floor")
    assert eligibility(policy, {"salary_from": 200, "currency": "RUB", "country": "Россия", "work_format": "remote", "schedule": "6/1"}) == ("unknown_requires_semantic", "schedule")
    assert eligibility(policy, {"salary_from": 200, "currency": "RUB", "country": "Россия", "work_format": "remote"}) == ("eligible", None)


def test_remote_unknowns_and_salary_range_do_not_become_hidden_rejects():
    policy = {"salary_floor": 70, "currency": "RUB", "allowed_countries": ["Россия"], "allowed_work_formats": ["remote", "hybrid"], "forbidden_schedules": []}
    assert eligibility(policy, {"salary_from": 60, "salary_to": 90, "currency": "RUB", "work_format": "remote"}) == ("eligible", None)
    assert eligibility(policy, {"currency": "RUB", "work_format": "remote"}) == ("eligible", None)
    assert eligibility(policy, {"salary_from": 100, "currency": "RUB", "work_format": "remote"}) == ("eligible", None)


def test_same_global_vacancy_has_independent_candidate_lifecycle(tmp_path: Path):
    db = tmp_path / "state.sqlite"
    provision_candidate(db, candidate_id="a", display_name="A", job_policy={"salary_floor": 100, "currency": "RUB", "allowed_work_formats": ["remote"]}, automation_policy={}, account_binding={"hh_identity":"a","resume_id":"ra","resume_title":"A"}, job_policy_version="ja", automation_policy_version="aa", binding_id="ba")
    provision_candidate(db, candidate_id="b", display_name="B", job_policy={"salary_floor": 10, "currency": "RUB", "allowed_work_formats": ["remote"]}, automation_policy={}, account_binding={"hh_identity":"b","resume_id":"rb","resume_title":"B"}, job_policy_version="jb", automation_policy_version="ab", binding_id="bb")
    select_candidate(db, "a")
    assert evaluate_and_record_eligibility(db, {"vacancy_id":"77","salary_from":50,"currency":"RUB","work_format":"remote"})[0] == "unknown_requires_semantic"
    select_candidate(db, "b")
    assert evaluate_and_record_eligibility(db, {"vacancy_id":"77","salary_from":50,"currency":"RUB","work_format":"remote"}) == ("eligible", None)


def test_candidate_b_isolation_and_evidence_gate_blocks_apply(tmp_path: Path):
    db = tmp_path / "state.sqlite"
    provision_candidate(db, candidate_id="a", display_name="Candidate A", job_policy={"salary_floor": 100, "currency": "RUB", "allowed_countries": ["Россия"], "allowed_work_formats": ["remote"]}, automation_policy={"hh_applications": "disabled"}, account_binding={"hh_identity": "a", "resume_id": "resume-a", "resume_title": "A"}, job_policy_version="job-a", automation_policy_version="auto-a", binding_id="bind-a")
    provision_candidate(db, candidate_id="b", display_name="Candidate B", job_policy={"salary_floor": 10, "currency": "RUB", "allowed_countries": ["Казахстан"], "allowed_work_formats": ["hybrid"]}, automation_policy={"hh_applications": "disabled"}, account_binding={"hh_identity": "b", "resume_id": "resume-b", "resume_title": "B"}, job_policy_version="job-b", automation_policy_version="auto-b", binding_id="bind-b")
    select_candidate(db, "a")
    record_profile_version(db, "profile-a", {"email": "a@example.test"}, {"email": "test"})
    assert evaluate_and_record_eligibility(db, {"vacancy_id": "1", "salary_from": 50, "currency": "RUB", "country": "Россия", "work_format": "remote"}) == ("unknown_requires_semantic", "salary_floor")
    decision = tmp_path / "decision.jsonl"
    decision.write_text('{"vacancy_id":"1","decision":"apply","reason":"semantic cannot override","cover_letter":"text","revision":"r1","source_run":"s"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="exact vacancy_evidence"):
        import_decisions(db, decision)
    select_candidate(db, "b")
    record_profile_version(db, "profile-b", {"email": "b@example.test"}, {"email": "test"})
    assert all_facts(db) == {"email": "b@example.test"}
    assert bootstrap(db)["account_binding"]["resume_id"] == "resume-b"
    assert evaluate_and_record_eligibility(db, {"vacancy_id": "1", "salary_from": 50, "currency": "RUB", "country": "Казахстан", "work_format": "hybrid", "schedule": "5/2"}) == ("eligible", None)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO vacancy_fact_revisions VALUES(?,?,?,?,?,?,?)", ("1", "rev1", '{"salary_from":50,"currency":"RUB","country":"Казахстан","work_format":"hybrid","schedule":"5/2"}', "h", "run", "now", ""))
    assert evaluate_persisted_vacancy(db, "1", "rev1") == ("eligible", None)
