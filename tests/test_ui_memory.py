import sqlite3
from pathlib import Path

from autohunt.ui_memory import (
    activate_contract,
    map_status,
    open_incident,
    record_observation,
    seed_static_contracts,
)


def test_ui_memory_keeps_recovery_evidence_and_one_active_contract(tmp_path: Path):
    db = tmp_path / "state.sqlite"
    with sqlite3.connect(db) as conn:
        # Migration owns actual schema; this creates only the candidate required by the API.
        pass
    from autohunt.storage import migrate

    migrate(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO candidates VALUES('s','Sergey',1,'now')")
    observation = record_observation(db, surface="chat_input", url="https://chatik.hh.ru/chat/1", fingerprint="abc", evidence={"screenshot": "x.png"})
    incident = open_incident(db, surface="chat_input", detail="send control changed", evidence={"observation_id": observation}, safe_resume={"action": "map_then_reread"}, vacancy_id="1")
    activate_contract(db, surface="chat_input", version="v1", contract={"ready": ["textarea"]}, source="browser-use-reviewed")
    activate_contract(db, surface="chat_input", version="v2", contract={"ready": ["textarea", "send"]}, source="live-playwright-reviewed")
    rows = map_status(db)
    assert incident.startswith("uiinc:")
    assert rows == [{"surface": "chat_input", "version": "v2", "source": "live-playwright-reviewed", "observed_at": rows[0]["observed_at"], "open_incidents": 1}]
    assert seed_static_contracts(db) > 0
