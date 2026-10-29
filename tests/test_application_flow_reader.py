import json
from pathlib import Path

from autohunt.models import PageFamily
from autohunt.readers import ApplicationFlowReader

FIXTURES = Path(__file__).parents[1] / "research_reference" / "fixtures"


def test_post_application_cover_dialog_is_optional_and_available():
    raw = json.loads((FIXTURES / "required_cover_letter.json").read_text(encoding="utf-8"))
    result = ApplicationFlowReader().read_cover_letter_dialog(raw)
    assert result.observation.family is PageFamily.APPLICATION_FLOW
    assert result.cover_letter_required is False
    assert result.cover_letter_max_length == 10000
    assert result.submit_enabled is True


def test_stored_answers_keep_form_state_partial():
    raw = json.loads(
        (FIXTURES / "corrupted_input_employer_answers.json").read_text(encoding="utf-8")
    )
    result = ApplicationFlowReader().read_employer_answers(raw)
    assert result.vacancy_id == "900002"
    assert result.employer_question_count == 3
    assert result.observation.confidence == "partial"
