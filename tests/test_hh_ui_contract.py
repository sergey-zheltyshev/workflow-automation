from autohunt.hh_ui_contract import (
    CONTRACT,
    HHState,
    canonical_response_control_index,
    canonical_response_href,
)


def test_rt006_duplicate_known_apply_ctas_resolve_without_click():
    """Top/sticky duplicate CTA is PREPARED discovery, never a mutation."""
    rows = [
        {"index": 0, "visible": True, "href": "/applicant/vacancy_response?vacancyId=135996193", "top": 800, "viewport": 700},
        {"index": 1, "visible": True, "href": "https://hh.ru/applicant/vacancy_response?vacancyId=135996193", "top": 120, "viewport": 700},
        {"index": 2, "visible": False, "href": "/applicant/vacancy_response?vacancyId=other", "top": 0, "viewport": 700},
    ]
    assert canonical_response_href("135996193", "https://hh.ru/vacancy/135996193", rows) == "https://hh.ru/applicant/vacancy_response?vacancyId=135996193"
    assert "start_application" in CONTRACT[HHState.APPLY_AVAILABLE].allowed_actions
    assert CONTRACT[HHState.APPLY_AVAILABLE].reconciliation.startswith("after action")
    assert canonical_response_control_index("135996193", "https://hh.ru/vacancy/135996193", rows) == 1


def test_unknown_or_conflicting_cta_never_resolves_to_action():
    rows = [
        {"visible": True, "href": "/applicant/vacancy_response?vacancyId=135996193&a=1"},
        {"visible": True, "href": "/applicant/vacancy_response?vacancyId=135996193&a=2"},
    ]
    assert canonical_response_href("135996193", "https://hh.ru/vacancy/135996193", rows) is None
