import json
import sqlite3
from pathlib import Path

from autohunt.models import (
    Capabilities,
    ChatMessageDTO,
    ChatThreadDTO,
    PageFamily,
    PageObservation,
    VacancyDTO,
)
from autohunt.storage import backfill_vacancy_descriptions, migrate
from autohunt.sync import RegistrySync


def test_migrate_is_idempotent_and_installs_sync_tables(tmp_path: Path):
    database = tmp_path / "test_storage.sqlite"
    migrate(database)
    migrate(database)
    with sqlite3.connect(database) as db:
        versions = db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        views = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,), (20,)]
    assert {"search_runs", "search_results", "negotiations", "messages", "chat_vacancy_links", "snapshots", "external_items", "user_questions", "candidate_decision_evidence"} <= tables
    assert {"candidate_vacancy_review", "application_review", "chat_review", "candidate_funnel", "candidate_action_queue", "candidate_operator_funnel"} <= views


def test_sync_is_idempotent_and_unlinked_chat_is_error():
    database = Path("var/test_registry.sqlite")
    database.unlink(missing_ok=True)
    sync = RegistrySync(database)
    observation = PageObservation(
        url="https://hh.ru/vacancy/1", family=PageFamily.VACANCY, capabilities=Capabilities(), confidence="confirmed"
    )
    vacancy = VacancyDTO(vacancy_id="1", url="https://hh.ru/vacancy/1", title="UX", company="Acme", full_description="Complete vacancy text", structured_facts={"salary_from": 100000, "currency": "RUB", "country": "Россия"}, observation=observation)
    thread = ChatThreadDTO(chat_id="c1", url="https://chatik.hh.ru/chat/c1", title="Acme", messages=[ChatMessageDTO(direction="incoming", text="Hi")], observation=observation)
    sync.sync_vacancies("s1", "UX", "https://hh.ru/search/vacancy?text=UX", [vacancy])
    sync.sync_vacancies("s1", "UX", "https://hh.ru/search/vacancy?text=UX", [vacancy])
    sync.sync_threads([thread])
    sync.sync_threads([thread])
    assert sync.sync_negotiations([("1", "awaiting", "https://hh.ru/applicant/negotiations")]) == 1
    sync.project("phase", {"status": "ready"})
    sync.record_external_item("x1", "1", "https://example.test/form", "form", "pending")
    sync.link_chat_to_vacancy("c1", "1", "explicit chat evidence")
    sync.sync_threads([thread])
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert db.execute("SELECT link_status FROM chat_vacancy_links WHERE chat_id='c1'").fetchone()[0] == "linked"
        assert db.execute("SELECT status FROM negotiations WHERE vacancy_id='1'").fetchone()[0] == "awaiting"
        assert db.execute("SELECT value_json FROM user_projection WHERE key='phase'").fetchone()[0] == '{"status": "ready"}'
        assert db.execute("SELECT status FROM external_items WHERE id='x1'").fetchone()[0] == "pending"
        assert db.execute("SELECT json_extract(facts_json,'$.salary_from') FROM vacancy_fact_revisions WHERE vacancy_id='1'").fetchone()[0] == 100000
        assert db.execute("SELECT full_description FROM vacancy_fact_revisions WHERE vacancy_id='1'").fetchone()[0] == "Complete vacancy text"


def test_chat_projection_is_candidate_scoped():
    database = Path("var/test_registry_scoped.sqlite")
    database.unlink(missing_ok=True)
    migrate(database)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO candidates VALUES('a','A',1,'now')")
        db.execute("INSERT INTO candidates VALUES('b','B',0,'now')")
    thread = ChatThreadDTO(chat_id="c1", url="https://chatik.hh.ru/chat/c1", title="Acme", messages=[ChatMessageDTO(direction="incoming", text="A only")], observation=PageObservation(url="https://chatik.hh.ru/chat/c1", family=PageFamily.CHAT_THREAD, capabilities=Capabilities(), confidence="confirmed"))
    RegistrySync(database).sync_threads([thread])
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT text FROM candidate_messages WHERE candidate_id='a'").fetchone()[0] == "A only"
        assert db.execute("SELECT COUNT(*) FROM candidate_messages WHERE candidate_id='b'").fetchone()[0] == 0


def test_live_run_journal_is_durable_and_candidate_scoped():
    from autohunt.live_run import LiveReadOnlyRun
    database = Path("var/test_live_journal.sqlite")
    database.unlink(missing_ok=True)
    migrate(database)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO candidates VALUES('a','A',1,'now')")
        db.execute("INSERT INTO account_bindings VALUES('bind','a','hh-a','resume-a','A','[]',1,'now')")
    run = LiveReadOnlyRun(Path("."), cdp_endpoint="http://127.0.0.1:1", query="q", max_pages=1, max_full=1, max_workers=1, skip_chat_sync=True)
    run.database = database
    run._journal("run_started", {"query":"q"})
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT candidate_id,binding_id,run_id,kind FROM runtime_events").fetchone() == ("a", "bind", run.run_id, "run_started")
        assert db.execute("SELECT candidate_id FROM candidate_runs WHERE run_id=?", (run.run_id,)).fetchone()[0] == "a"


def test_backfill_adds_identity_and_full_description_from_jsonl(tmp_path):
    database = tmp_path / "backfill.sqlite"
    work_items = tmp_path / "work_items"
    work_items.mkdir()
    (work_items / "vacancies_collector-a.jsonl").write_text(
        json.dumps({"vacancy_id": "123", "url": "https://hh.ru/vacancy/123", "title": "Role", "company": "Co", "description": "Durable full text", "structured_facts": {"work_format": "remote"}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result = backfill_vacancy_descriptions(database, work_items)
    assert result["identities_inserted"] == 1
    assert result["revisions_inserted"] == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT full_description FROM vacancy_fact_revisions WHERE vacancy_id='123' AND revision='collector-a'").fetchone()[0] == "Durable full text"
