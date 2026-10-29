import sqlite3
from pathlib import Path

from autohunt.operator_state import FUNNEL_COLUMNS, funnel_rows, summary
from autohunt.storage import migrate


def test_funnel_has_only_operator_columns_and_does_not_infer_missing_urls(tmp_path: Path):
    db = tmp_path / "state.sqlite"
    migrate(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO candidates VALUES('s','Sergey',1,'now')")
        conn.execute("INSERT INTO vacancies VALUES('1','https://hh.ru/vacancy/1','Role','Company','now')")
        conn.execute(
            "INSERT INTO candidate_application_processes VALUES('s','1','b','d','resume','hash','APPLICATION_CONFIRMED','HH says applied','2026-08-10T12:00:00+00:00')"
        )
    rows = funnel_rows(db)
    assert len(rows) == 1
    assert tuple(rows[0]) == FUNNEL_COLUMNS
    assert rows[0]["Компания"] == "Company"
    assert rows[0]["Полный URL вакансии"] == "https://hh.ru/vacancy/1"
    assert rows[0]["Полный URL чата"] is None
    assert rows[0]["Дата отклика"] == "2026-08-10"
    assert summary(db) == {"funnel_rows": 1, "open_ui_incidents": 0, "unlinked_chats": 0}
