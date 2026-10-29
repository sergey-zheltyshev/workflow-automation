"""Idempotent, offline registry synchronization for reader DTOs."""

from contextlib import closing
import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .models import ChatThreadDTO, VacancyDTO
from .storage import migrate


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(direction: str, text: str, occurrence: int) -> str:
    value = f"{direction}\x00{text}\x00{occurrence}".encode()
    return hashlib.sha256(value).hexdigest()


class RegistrySync:
    def __init__(self, database: Path, *, candidate_id: str | None = None) -> None:
        migrate(database)
        self.database = database
        with closing(sqlite3.connect(database)) as db, db:
            row = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone() if candidate_id is None else (candidate_id,)
        self.candidate_id = str(row[0]) if row else None

    def sync_vacancies(
        self, search_run_id: str, query: str, source_url: str, vacancies: Iterable[VacancyDTO]
    ) -> dict[str, int]:
        captured_at = _now()
        items = list(vacancies)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO search_runs(id, query, source_url, captured_at) VALUES (?, ?, ?, ?)",
                (search_run_id, query, source_url, captured_at),
            )
            if self.candidate_id:
                db.execute("INSERT OR REPLACE INTO candidate_search_runs(candidate_id,id,query,source_url,captured_at) VALUES(?,?,?,?,?)", (self.candidate_id, search_run_id, query, source_url, captured_at))
            for rank, item in enumerate(items, start=1):
                if not item.vacancy_id:
                    continue
                db.execute(
                    "INSERT INTO vacancies(vacancy_id,url,title,company,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(vacancy_id) DO UPDATE SET url=excluded.url,title=excluded.title,company=excluded.company,updated_at=excluded.updated_at",
                    (item.vacancy_id, item.url, item.title, item.company, captured_at),
                )
                db.execute(
                    "INSERT OR IGNORE INTO search_results(search_run_id,vacancy_id,rank) VALUES(?,?,?)",
                    (search_run_id, item.vacancy_id, rank),
                )
                if self.candidate_id:
                    db.execute("INSERT OR REPLACE INTO candidate_search_results(candidate_id,search_run_id,vacancy_id,rank) VALUES(?,?,?,?)", (self.candidate_id, search_run_id, item.vacancy_id, rank))
                if item.structured_facts or item.full_description:
                    facts_json = json.dumps(item.structured_facts, ensure_ascii=False, sort_keys=True)
                    db.execute(
                        "INSERT INTO vacancy_fact_revisions(vacancy_id,revision,facts_json,content_hash,source_run,captured_at,full_description) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(vacancy_id,revision) DO UPDATE SET facts_json=excluded.facts_json,content_hash=excluded.content_hash,source_run=excluded.source_run,captured_at=excluded.captured_at,full_description=COALESCE(excluded.full_description,vacancy_fact_revisions.full_description)",
                        (item.vacancy_id, search_run_id, facts_json, hashlib.sha256(facts_json.encode()).hexdigest(), search_run_id, captured_at, item.full_description),
                    )
            db.execute(
                "INSERT INTO checkpoints(name,value_json,updated_at) VALUES('vacancy_sync',?,?) "
                "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (json.dumps({"search_run_id": search_run_id, "count": len(items)}), captured_at),
            )
        return {"vacancies_seen": len(items)}

    def sync_threads(self, threads: Iterable[ChatThreadDTO]) -> dict[str, int]:
        items = list(threads)
        message_count = 0
        with closing(sqlite3.connect(self.database)) as db, db:
            for thread in items:
                if not thread.chat_id:
                    continue
                db.execute(
                    "INSERT INTO chats(chat_id,url,title,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET url=excluded.url,title=excluded.title,updated_at=excluded.updated_at",
                    (thread.chat_id, thread.url, thread.title, _now()),
                )
                if self.candidate_id:
                    db.execute("INSERT INTO candidate_chats(candidate_id,chat_id,url,title,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,chat_id) DO UPDATE SET url=excluded.url,title=excluded.title,updated_at=excluded.updated_at", (self.candidate_id, thread.chat_id, thread.url, thread.title, _now()))
                occurrence: defaultdict[tuple[str, str], int] = defaultdict(int)
                for message in thread.messages:
                    key = (message.direction, message.text)
                    fingerprint = _fingerprint(*key, occurrence[key])
                    occurrence[key] += 1
                    db.execute(
                        "INSERT OR IGNORE INTO messages(chat_id,fingerprint,direction,text) VALUES(?,?,?,?)",
                        (thread.chat_id, fingerprint, message.direction, message.text),
                    )
                    if self.candidate_id:
                        db.execute(
                            "INSERT OR IGNORE INTO candidate_messages(candidate_id,chat_id,fingerprint,direction,text,received_at) VALUES(?,?,?,?,?,?)",
                            (self.candidate_id, thread.chat_id, fingerprint, message.direction, message.text, message.received_at),
                        )
                    message_count += 1
                db.execute(
                    "INSERT OR IGNORE INTO chat_vacancy_links(chat_id,vacancy_id,link_status,detail) VALUES(?,?,?,?)",
                    (thread.chat_id, None, "error", "vacancy linkage not present in reader evidence"),
                )
                if self.candidate_id:
                    db.execute("INSERT OR IGNORE INTO candidate_chat_vacancy_links(candidate_id,chat_id,vacancy_id,link_status,detail) VALUES(?,?,?,?,?)", (self.candidate_id, thread.chat_id, None, "error", "vacancy linkage not present in reader evidence"))
            db.execute(
                "INSERT INTO checkpoints(name,value_json,updated_at) VALUES('chat_sync',?,?) "
                "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (json.dumps({"threads": len(items), "messages_seen": message_count}), _now()),
            )
        return {"threads_seen": len(items), "messages_seen": message_count}

    def sync_negotiations(self, rows: Iterable[tuple[str, str, str]]) -> int:
        """Upsert observed statuses; caller supplies vacancy_id, status, source_url."""
        items = list(rows)
        observed_at = _now()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executemany(
                "INSERT INTO negotiations(vacancy_id,status,source_url,observed_at) VALUES(?,?,?,?) "
                "ON CONFLICT(vacancy_id) DO UPDATE SET status=excluded.status,source_url=excluded.source_url,observed_at=excluded.observed_at",
                [(vacancy_id, status, source_url, observed_at) for vacancy_id, status, source_url in items],
            )
            if self.candidate_id:
                db.executemany(
                    "INSERT INTO candidate_negotiations(candidate_id,vacancy_id,status,source_url,observed_at) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,vacancy_id) DO UPDATE SET status=excluded.status,source_url=excluded.source_url,observed_at=excluded.observed_at",
                    [(self.candidate_id, vacancy_id, status, source_url, observed_at) for vacancy_id, status, source_url in items],
                )
            db.execute(
                "INSERT INTO checkpoints(name,value_json,updated_at) VALUES('negotiation_sync',?,?) "
                "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (json.dumps({"count": len(items)}), observed_at),
            )
        return len(items)

    def project(self, key: str, value: object) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO user_projection(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), _now()),
            )
            if self.candidate_id:
                db.execute("INSERT INTO candidate_projections(candidate_id,projection_key,value_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(candidate_id,projection_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (self.candidate_id, key, json.dumps(value, ensure_ascii=False), _now()))

    def rebuild_registry_projection(self) -> list[dict[str, object]]:
        """Derived user registry. One candidate-scoped row per vacancy, never source state."""
        if not self.candidate_id:
            raise RuntimeError("active candidate required for registry projection")
        with closing(sqlite3.connect(self.database)) as db, db:
            rows = db.execute(
                "SELECT s.vacancy_id,v.title,v.company,v.url,s.eligibility,s.detail,"
                "p.state,p.updated_at,d.decided_at,"
                "(SELECT url FROM candidate_chats c JOIN candidate_chat_vacancy_links l ON l.candidate_id=c.candidate_id AND l.chat_id=c.chat_id WHERE l.candidate_id=s.candidate_id AND l.vacancy_id=s.vacancy_id LIMIT 1),"
                "(SELECT url FROM candidate_external_items e WHERE e.candidate_id=s.candidate_id AND e.vacancy_id=s.vacancy_id AND e.kind='form' LIMIT 1),"
                "(SELECT url FROM candidate_external_items e WHERE e.candidate_id=s.candidate_id AND e.vacancy_id=s.vacancy_id AND e.kind='test' LIMIT 1) "
                "FROM candidate_vacancy_state s JOIN vacancies v ON v.vacancy_id=s.vacancy_id "
                "LEFT JOIN candidate_application_processes p ON p.candidate_id=s.candidate_id AND p.vacancy_id=s.vacancy_id "
                "LEFT JOIN candidate_vacancy_decisions d ON d.candidate_id=s.candidate_id AND d.vacancy_id=s.vacancy_id "
                "WHERE s.candidate_id=? ORDER BY COALESCE(p.updated_at,d.decided_at,s.updated_at) DESC",
                (self.candidate_id,),
            ).fetchall()
        result = []
        for row in rows:
            stage = row[6] or ("semantic_apply" if row[8] else row[4])
            result.append({"Company": row[2], "Vacancy": row[1], "Stage": stage, "Application date": row[7], "Deadline": None, "Vacancy URL": row[3], "Chat URL": row[9], "Form URL": row[10], "Test URL": row[11], "Note": row[5]})
        self.project("registry", result)
        return result

    def record_snapshots(self, rows: Iterable[tuple[str, str, str]]) -> int:
        """Persist immutable evidence references by content hash."""
        items = list(rows)
        captured_at = _now()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executemany(
                "INSERT OR IGNORE INTO snapshots(sha256,path,family,captured_at) VALUES(?,?,?,?)",
                [(sha256, path, family, captured_at) for sha256, path, family in items],
            )
        return len(items)

    def record_external_item(
        self, item_id: str, vacancy_id: str | None, url: str, kind: str, status: str,
        *, received_at: str | None = None, deadline_at: str | None = None, deadline_source: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO external_items(id,vacancy_id,url,kind,status,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET vacancy_id=excluded.vacancy_id,url=excluded.url,kind=excluded.kind,status=excluded.status,updated_at=excluded.updated_at",
                (item_id, vacancy_id, url, kind, status, _now()),
            )
            if self.candidate_id:
                db.execute(
                    "INSERT INTO candidate_external_items(candidate_id,id,vacancy_id,url,kind,status,updated_at,received_at,deadline_at,deadline_source) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(candidate_id,id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,received_at=COALESCE(candidate_external_items.received_at,excluded.received_at),deadline_at=COALESCE(candidate_external_items.deadline_at,excluded.deadline_at),deadline_source=COALESCE(candidate_external_items.deadline_source,excluded.deadline_source)",
                    (self.candidate_id, item_id, vacancy_id, url, kind, status, _now(), received_at, deadline_at, deadline_source),
                )

    def link_chat_to_vacancy(self, chat_id: str, vacancy_id: str, detail: str) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO chat_vacancy_links(chat_id,vacancy_id,link_status,detail) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET vacancy_id=excluded.vacancy_id,link_status=excluded.link_status,detail=excluded.detail",
                (chat_id, vacancy_id, "linked", detail),
            )
            if self.candidate_id:
                db.execute("INSERT INTO candidate_chat_vacancy_links(candidate_id,chat_id,vacancy_id,link_status,detail) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,chat_id) DO UPDATE SET vacancy_id=excluded.vacancy_id,link_status=excluded.link_status,detail=excluded.detail", (self.candidate_id, chat_id, vacancy_id, "linked", detail))
