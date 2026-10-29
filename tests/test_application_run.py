import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from autohunt.application_run import (
    MATCH,
    MISMATCH,
    UNKNOWN,
    ApplicationPlanItem,
    load_application_plan,
    observed_state,
    resume_state,
)


def test_missing_live_observation_is_unknown_not_mismatch():
    assert observed_state("expected", None) == UNKNOWN
    assert observed_state("HH identity requires live verification", "observed-account") == UNKNOWN
    assert resume_state("resume://expected", []) == UNKNOWN
    assert observed_state("expected", "different") == MISMATCH
    assert observed_state("expected", "expected") == MATCH


def valid_item(**changes):
    item = {
        "vacancy_id": "135885630",
        "resume_id": "Бизнес ассистент/Операционный ассистент",
        "expected_cover_letter": "Точный текст",
        "decision_id": "decision-135885630",
        "source_revision": "live-20260807T043614Z",
        "response_url": "https://hh.ru/applicant/vacancy_response?vacancyId=135885630&employerId=10256320",
    }
    item.update(changes)
    return item


@pytest.mark.parametrize(
    "changes",
    [
        {"response_url": "https://example.test/applicant/vacancy_response?vacancyId=135885630"},
        {"response_url": "https://hh.ru/vacancy/135885630"},
        {"response_url": "https://hh.ru/applicant/vacancy_response?vacancyId=9"},
    ],
)
def test_plan_rejects_wrong_or_untrusted_response_route(changes):
    with pytest.raises(ValidationError):
        ApplicationPlanItem.model_validate(valid_item(**changes))


def test_plan_requires_at_most_ten_preselected_items():
    path = Path("var/test_application_plan_limit.jsonl")
    rows = [
        json.dumps(
            valid_item(
                vacancy_id=str(index),
                response_url=f"https://hh.ru/applicant/vacancy_response?vacancyId={index}",
            )
        )
        for index in range(1, 12)
    ]
    path.write_text("\n".join(rows), encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="1..10"):
            load_application_plan(path)
    finally:
        path.unlink(missing_ok=True)
