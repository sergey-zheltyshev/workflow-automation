import json
from pathlib import Path

from autohunt.models import PageFamily
from autohunt.readers import ChatListReader


def test_chat_queue_preserves_hints_but_not_status_claims():
    path = (
        Path(__file__).parents[1] / "research_reference" / "fixtures" / "chatik_priority_queue.json"
    )
    result = ChatListReader().read(json.loads(path.read_text(encoding="utf-8")))
    assert result.observation.family is PageFamily.CHAT_LIST
    assert result.observation.confidence == "partial"
    assert len(result.items) > 10
    assert all(item.status == "unknown" for item in result.items)
    assert any("external_form" in item.category_hints for item in result.items)
