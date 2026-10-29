import json
from pathlib import Path

from autohunt.models import PageFamily
from autohunt.readers import NegotiationsReader


def test_negotiations_reader_keeps_card_statuses_unknown():
    path = (
        Path(__file__).parents[1] / "research_reference" / "fixtures" / "negotiations_initial.json"
    )
    result = NegotiationsReader().read(json.loads(path.read_text(encoding="utf-8")))
    assert result.observation.family is PageFamily.NEGOTIATIONS
    assert result.tab_counts == {"all": 51, "interview": 8, "awaiting": 15, "discard": 28}
    assert result.visible_item_count > 0
    assert set(result.visible_statuses) == {"unknown"}
