import json
from pathlib import Path

from autohunt.models import PageFamily
from autohunt.readers import SearchReader

FIXTURES = Path(__file__).parents[1] / "research_reference" / "fixtures"


def test_search_reader_keeps_zero_card_map_state():
    raw = json.loads((FIXTURES / "vacancy_map_initial.json").read_text(encoding="utf-8"))
    result = SearchReader().read(raw)
    assert result.observation.family is PageFamily.SEARCH_RESULTS
    assert result.query == "business-analyst"
    assert result.card_count == 0
    assert not result.observation.capabilities.can_open_vacancy


def test_search_reader_identifies_filter_drawer_as_partial_state():
    raw = json.loads((FIXTURES / "advanced_search_drawer.json").read_text(encoding="utf-8"))
    result = SearchReader().read(raw)
    assert result.filter_drawer_open
    assert result.query == "business-analyst"
    assert result.control_count > 10
    assert result.observation.confidence == "partial"
