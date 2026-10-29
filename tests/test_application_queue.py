import json
import sqlite3
from pathlib import Path

from autohunt.application_run import build_authoritative_application_queue
from autohunt.storage import migrate


def _seed(db: Path, *, decision_revision: str = "r1", latest_revision: str = "r1", process: bool = False, evidence: bool = True):
    migrate(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO candidates VALUES('s','Sergey',1,'now')")
        conn.execute("INSERT INTO account_bindings VALUES('b','s','id','resume','Resume','[]',1,'now')")
        conn.execute("INSERT INTO candidate_profiles VALUES('s','profile', '{}','{}','now')")
        conn.execute("INSERT INTO job_policy_versions VALUES('policy','s','{}','test','now')")
        conn.execute("INSERT INTO automation_policy_versions VALUES('auto','s','{}','test','now')")
        conn.execute("INSERT INTO vacancies VALUES('1','https://hh.ru/vacancy/1','Role','Co','now')")
        conn.execute("INSERT INTO candidate_vacancy_state VALUES('s','1','eligible',NULL,'ok','h','now')")
        conn.execute("INSERT INTO vacancy_fact_revisions(vacancy_id,revision,facts_json,content_hash,source_run,captured_at,full_description) VALUES('1',?,?,?,'run','2026-08-10T00:00:00+00:00','full live text')", (latest_revision, '{}', latest_revision))
        conn.execute("INSERT INTO candidate_vacancy_decisions VALUES('s','d','1','profile','policy',?,'run','apply',80,'fit','cover',?, '[]','now')", (decision_revision, json.dumps({})))
        if evidence:
            conn.execute("INSERT INTO candidate_decision_evidence VALUES('s','d','[\"full live text\"]','[\"name\"]','{}','now')")
        if process:
            conn.execute("INSERT INTO candidate_application_processes VALUES('s','1','b','d','resume','h','APPLICATION_CONFIRMED','live proof','now')")


def test_queue_requires_current_revision_and_no_existing_process(tmp_path: Path):
    stale = tmp_path / 'stale.sqlite'
    _seed(stale, decision_revision='old', latest_revision='new')
    assert build_authoritative_application_queue(stale) == []
    applied = tmp_path / 'applied.sqlite'
    _seed(applied, process=True)
    assert build_authoritative_application_queue(applied) == []
    missing_evidence = tmp_path / 'missing_evidence.sqlite'
    _seed(missing_evidence, evidence=False)
    assert build_authoritative_application_queue(missing_evidence) == []
    ready = tmp_path / 'ready.sqlite'
    _seed(ready)
    assert [item.vacancy_id for item in build_authoritative_application_queue(ready)] == ['1']
