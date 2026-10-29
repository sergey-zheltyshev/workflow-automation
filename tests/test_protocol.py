import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from autohunt.protocol import Decision, WorkItem, record_decision


def test_protocol_rejects_unknown_fields_and_records_decision():
    with pytest.raises(ValidationError):
        WorkItem(id="w1", kind="vacancy_review", evidence_refs=["s1"], created_at=datetime.now(UTC), unsafe="send")
    audit_path = Path("var/test_protocol_audit.jsonl")
    audit_path.unlink(missing_ok=True)
    record_decision(audit_path, Decision(work_item_id="w1", decision="hold", reason="salary unknown", evidence_refs=["s1"], decided_at=datetime.now(UTC)))
    assert json.loads(audit_path.read_text(encoding="utf-8"))["event_type"] == "decision_recorded"
