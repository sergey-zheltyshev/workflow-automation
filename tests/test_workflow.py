import json
import sqlite3
from pathlib import Path

from autohunt.candidate_facts import record_profile_version
from autohunt.context import evaluate_and_record_eligibility, provision_candidate, select_candidate
from autohunt.decisions import create_semantic_work_item
from autohunt.sync import RegistrySync
from autohunt.workflow import MvpWorkflow, WorkBatchControl


def _candidate(database: Path) -> None:
    provision_candidate(database, candidate_id="a", display_name="A", job_policy={"allowed_work_formats": ["remote"]}, automation_policy={}, account_binding={"hh_identity":"a", "resume_id":"r", "resume_title":"R"}, job_policy_version="j", automation_policy_version="p", binding_id="b")
    select_candidate(database, "a")


def test_mvp_run_is_restart_safe_and_chats_first():
    database = Path("var/test_workflow.sqlite")
    database.unlink(missing_ok=True)
    _candidate(database)
    sync = RegistrySync(database)
    sync.link_chat_to_vacancy("linked", "v1", "test")
    workflow = MvpWorkflow(database, Path("var/test_mvp_output"))
    first = workflow.run("run-1")
    second = workflow.run("run-1")
    assert [item.kind for item in first][:1] == ["chat_review"]
    assert [item.id for item in first] == [item.id for item in second]


def test_mvp_stops_on_explicit_captcha_signal():
    database = Path("var/test_workflow_captcha.sqlite")
    database.unlink(missing_ok=True)
    _candidate(database)
    assert MvpWorkflow(database, Path("var/test_mvp_output")).run("captcha", captcha_detected=True) == []


def test_semantic_batch_start_next_commit_resume_finish(tmp_path, monkeypatch):
    database = tmp_path / "batch.sqlite"
    _candidate(database)
    record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
    with sqlite3.connect(database) as db:
        for vacancy_id in ("1", "2"):
            db.execute("INSERT INTO vacancies(vacancy_id,url,title,company) VALUES(?,?,?,?)", (vacancy_id, f"https://hh.ru/vacancy/{vacancy_id}", f"Role {vacancy_id}", "Co"))
            db.execute("INSERT INTO vacancy_fact_revisions(vacancy_id,revision,facts_json,content_hash,source_run,captured_at,full_description) VALUES(?,?,?,?,?,?,?)", (vacancy_id, f"r{vacancy_id}", json.dumps({"description": f"Full {vacancy_id}"}), "h", "collector", f"2026-01-0{vacancy_id}T00:00:00Z", f"Full {vacancy_id}"))
    for vacancy_id in ("1", "2"):
        evaluate_and_record_eligibility(database, {"vacancy_id": vacancy_id, "work_format": "remote"})
        create_semantic_work_item(database, vacancy_id=vacancy_id, source_run="collector")
    control = WorkBatchControl(database)
    monkeypatch.setattr("autohunt.workflow.export_funnel_xlsx", lambda *_: tmp_path / "f.xlsx")
    started = control.start()
    batch = control.next(str(started["run_id"]), limit=2)
    assert len(batch["vacancies"]) == 2
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps([{"vacancy_id": "1", "decision": "skip", "score": 10, "reason": "Not target", "cover_letter": None, "question_answers": {}, "unknown_user_facts": []}]), encoding="utf-8")
    assert control.commit(str(started["run_id"]), path)["accepted"] == 1
    resumed = control.start()
    assert resumed == {"run_id": started["run_id"], "resumed": True}
    remaining = control.next(str(started["run_id"]), limit=2)
    assert [item["vacancy_id"] for item in remaining["vacancies"]] == ["2"]
    assert control.finish(str(started["run_id"]), tmp_path)["status"] == "checkpointed"


def test_semantic_batch_can_limit_next_to_one_collector_run(tmp_path):
    database = tmp_path / "scoped.sqlite"
    _candidate(database)
    record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
    with sqlite3.connect(database) as db:
        for vacancy_id, source_run in (("1", "collector-a"), ("2", "collector-b")):
            db.execute("INSERT INTO vacancies(vacancy_id,url,title,company) VALUES(?,?,?,?)", (vacancy_id, f"https://hh.ru/vacancy/{vacancy_id}", f"Role {vacancy_id}", "Co"))
            db.execute("INSERT INTO vacancy_fact_revisions(vacancy_id,revision,facts_json,content_hash,source_run,captured_at,full_description) VALUES(?,?,?,?,?,?,?)", (vacancy_id, f"r{vacancy_id}", json.dumps({"description": f"Full {vacancy_id}"}), "h", source_run, f"2026-01-0{vacancy_id}T00:00:00Z", f"Full {vacancy_id}"))
    for vacancy_id, source_run in (("1", "collector-a"), ("2", "collector-b")):
        evaluate_and_record_eligibility(database, {"vacancy_id": vacancy_id, "work_format": "remote"})
        create_semantic_work_item(database, vacancy_id=vacancy_id, source_run=source_run)
    control = WorkBatchControl(database)
    started = control.start()
    batch = control.next(str(started["run_id"]), limit=30, source_run="collector-b")
    assert [item["vacancy_id"] for item in batch["vacancies"]] == ["2"]


def test_semantic_batch_reads_full_text_from_sqlite_without_work_items_file(tmp_path, monkeypatch):
    database = tmp_path / "sqlite-only.sqlite"
    _candidate(database)
    record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO vacancies(vacancy_id,url,title,company) VALUES('1','https://hh.ru/vacancy/1','Role','Co')")
        db.execute("INSERT INTO vacancy_fact_revisions(vacancy_id,revision,facts_json,content_hash,source_run,captured_at,full_description) VALUES('1','collector-a','{}','h','collector-a','2026-01-01T00:00:00Z','SQLite-only full text')")
    evaluate_and_record_eligibility(database, {"vacancy_id": "1", "work_format": "remote"})
    create_semantic_work_item(database, vacancy_id="1", source_run="collector-a")
    monkeypatch.chdir(tmp_path)
    control = WorkBatchControl(database)
    run_id = str(control.start()["run_id"])
    batch = control.next(run_id, source_run="collector-a")
    assert batch["vacancies"][0]["full_description"] == "SQLite-only full text"
