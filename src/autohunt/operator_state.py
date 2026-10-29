"""Small, canonical read model for an AutoHunt operator.

This module deliberately does no browser work and no mutation.  It is the
stable hand-off surface a fresh agent reads before it decides what to do.
"""
from contextlib import closing
import sqlite3
from pathlib import Path

from .storage import migrate

FUNNEL_COLUMNS = (
    "Компания", "Вакансия", "Дата отклика", "Дедлайн", "Полный URL вакансии",
    "Полный URL чата", "Полный URL анкеты", "Полный URL тестового", "Примечание",
)


def funnel_rows(database: Path, *, candidate_id: str | None = None) -> list[dict[str, str | None]]:
    """Return exactly the operator table columns, with no inferred proof."""
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        db.row_factory = sqlite3.Row
        if candidate_id is None:
            active = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
            if active is None:
                return []
            candidate_id = str(active[0])
        rows = db.execute(
            "SELECT company,vacancy,application_date,deadline,vacancy_url,chat_url,form_url,test_url,note "
            "FROM candidate_operator_funnel WHERE candidate_id=? "
            "ORDER BY COALESCE(application_date,'' ) DESC, last_live_check_at DESC, company, vacancy",
            (candidate_id,),
        ).fetchall()
    return [
        {
            "Компания": row["company"], "Вакансия": row["vacancy"],
            "Дата отклика": row["application_date"], "Дедлайн": row["deadline"],
            "Полный URL вакансии": row["vacancy_url"], "Полный URL чата": row["chat_url"],
            "Полный URL анкеты": row["form_url"], "Полный URL тестового": row["test_url"],
            "Примечание": row["note"],
        }
        for row in rows
    ]


def summary(database: Path) -> dict[str, int]:
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        active = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
        if active is None:
            return {"funnel_rows": 0, "open_ui_incidents": 0, "unlinked_chats": 0}
        candidate_id = str(active[0])
        return {
            "funnel_rows": db.execute("SELECT COUNT(*) FROM candidate_operator_funnel WHERE candidate_id=?", (candidate_id,)).fetchone()[0],
            "open_ui_incidents": db.execute("SELECT COUNT(*) FROM hh_ui_incidents WHERE candidate_id=? AND status!='resolved'", (candidate_id,)).fetchone()[0],
            "unlinked_chats": db.execute("SELECT COUNT(*) FROM candidate_chats c WHERE c.candidate_id=? AND NOT EXISTS (SELECT 1 FROM candidate_chat_vacancy_links l WHERE l.candidate_id=c.candidate_id AND l.chat_id=c.chat_id AND l.vacancy_id IS NOT NULL)", (candidate_id,)).fetchone()[0],
        }
