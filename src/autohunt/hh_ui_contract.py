"""Narrow production HH UI contract compiled from existing HH research.

This is deliberately an adapter contract, not another workflow layer.  Sources:
``research/catalog/HH_ROUTE_CATALOG*``, ``HH_STATE_CATALOG*``,
``HH_TRANSITION_MAP*``, ``HH_SELECTOR_EVIDENCE*`` and RT-006.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urljoin, urlparse


class HHState(StrEnum):
    SEARCH_RESULTS = "SEARCH_RESULTS"
    VACANCY_READY = "VACANCY_READY"
    APPLY_AVAILABLE = "APPLY_AVAILABLE"
    COUNTRY_CONFIRMATION = "COUNTRY_CONFIRMATION"
    RESPONSE_FORM = "RESPONSE_FORM"
    RESPONSE_FORM_WITH_COVER = "RESPONSE_FORM_WITH_COVER"
    RESPONSE_FORM_WITH_QUESTIONS = "RESPONSE_FORM_WITH_QUESTIONS"
    APPLICATION_CONFIRMED = "APPLICATION_CONFIRMED"
    APPLICATION_CONFIRMED_COVER_PENDING = "APPLICATION_CONFIRMED_COVER_PENDING"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    NEGOTIATIONS = "NEGOTIATIONS"
    CHAT_LIST = "CHAT_LIST"
    CHAT_THREAD = "CHAT_THREAD"
    CAPTCHA = "CAPTCHA"
    ERROR = "ERROR"
    UNKNOWN_UI = "UNKNOWN_UI"


@dataclass(frozen=True)
class UIStateContract:
    selectors: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    transitions: tuple[HHState, ...]
    reconciliation: str
    evidence: str


_EVIDENCE = "HH_SELECTOR_EVIDENCE.md; HH_STATE_CATALOG.md; HH_TRANSITION_MAP.md"
CONTRACT: dict[HHState, UIStateContract] = {
    HHState.SEARCH_RESULTS: UIStateContract(("[data-qa='serp-item']",), ("open_vacancy",), (HHState.VACANCY_READY,), "no mutation", _EVIDENCE),
    HHState.VACANCY_READY: UIStateContract(("h1", "[data-qa='vacancy-description']"), ("inspect_application_state", "start_application"), (HHState.APPLY_AVAILABLE, HHState.ALREADY_APPLIED), "reread vacancy id and response state", _EVIDENCE),
    # RT-006: top/sticky copies can coexist.  Canonical href, not DOM position,
    # represents this one business action.
    HHState.APPLY_AVAILABLE: UIStateContract(("[data-qa='vacancy-response-link-top']", "[data-qa='vacancy-serp__vacancy_response']"), ("start_application",), (HHState.RESPONSE_FORM, HHState.COUNTRY_CONFIRMATION, HHState.ALREADY_APPLIED), "after action reread form/already-applied", "HH_SELECTOR_EVIDENCE.md#RT-006"),
    HHState.COUNTRY_CONFIRMATION: UIStateContract(("[role='dialog']",), ("confirm_country_application",), (HHState.RESPONSE_FORM, HHState.ALREADY_APPLIED), "always reconcile; click may create primary response", _EVIDENCE),
    HHState.RESPONSE_FORM: UIStateContract(("[data-qa='vacancy-response-form']", "textarea", "form"), ("inspect_response_form", "fill_response", "submit_response"), (HHState.RESPONSE_FORM_WITH_COVER, HHState.RESPONSE_FORM_WITH_QUESTIONS, HHState.APPLICATION_CONFIRMED), "reread target/resume/questions", _EVIDENCE),
    HHState.RESPONSE_FORM_WITH_COVER: UIStateContract(("textarea",), ("fill_response", "submit_response"), (HHState.APPLICATION_CONFIRMED,), "exact fill/readback/hash/blur/readback", _EVIDENCE),
    HHState.RESPONSE_FORM_WITH_QUESTIONS: UIStateContract(("input[required]", "textarea[required]", "select[required]"), ("fill_response", "submit_response"), (HHState.APPLICATION_CONFIRMED,), "all required answers reread before submit", _EVIDENCE),
    HHState.APPLICATION_CONFIRMED: UIStateContract(("[data-qa='vacancy-response-status']",), (), (), "target + selected resume + HH post-state", _EVIDENCE),
    HHState.APPLICATION_CONFIRMED_COVER_PENDING: UIStateContract(("textarea",), ("fill_response", "submit_response"), (HHState.APPLICATION_CONFIRMED,), "never repeat primary application", _EVIDENCE),
    HHState.ALREADY_APPLIED: UIStateContract(("[data-qa='vacancy-response-link-top']",), (), (), "terminal for candidate + vacancy", _EVIDENCE),
    HHState.NEGOTIATIONS: UIStateContract(("[data-qa='negotiations-item']",), ("open_chat",), (HHState.CHAT_THREAD,), "reread application history", _EVIDENCE),
    HHState.CHAT_LIST: UIStateContract(("[data-qa='chat-cell-title']",), ("open_chat",), (HHState.CHAT_THREAD,), "no mutation", _EVIDENCE),
    HHState.CHAT_THREAD: UIStateContract(("[data-qa='chat-message']",), ("prepare_reply",), (), "reread sent message", _EVIDENCE),
    HHState.CAPTCHA: UIStateContract(("iframe[src*='captcha']",), (), (), "global stop", _EVIDENCE),
    HHState.ERROR: UIStateContract(("[data-qa='error']",), (), (), "capture evidence; item block", _EVIDENCE),
    HHState.UNKNOWN_UI: UIStateContract((), (), (), "capture incident evidence; item block", _EVIDENCE),
}
SELECTORS = {state: contract.selectors for state, contract in CONTRACT.items()}


def canonical_response_href(vacancy_id: str, page_url: str, rows: Iterable[dict[str, object]]) -> str | None:
    """Return one verified HH response route across duplicate visible CTA copies.

    Discovery has no side effect.  A missing/ambiguous route is PREPARED, never
    IN_FLIGHT.  RT-006 permits multiple visual anchors only when they resolve to
    this exact same canonical route.
    """
    candidates: set[str] = set()
    for row in rows:
        if not row.get("visible"):
            continue
        raw = str(row.get("href") or "")
        if not raw:
            continue
        url = urljoin(page_url, raw)
        parsed = urlparse(url)
        if parsed.hostname not in {"hh.ru", "www.hh.ru"}:
            continue
        if "vacancy_response" in parsed.path and vacancy_id in url:
            candidates.add(url)
    return next(iter(candidates)) if len(candidates) == 1 else None


def canonical_response_control_index(vacancy_id: str, page_url: str, rows: list[dict[str, object]]) -> int | None:
    """Pick a canonical visible CTA for a trusted Playwright pointer click."""
    href = canonical_response_href(vacancy_id, page_url, rows)
    if not href:
        return None
    matches = [
        row for row in rows
        if row.get("visible") and urljoin(page_url, str(row.get("href") or "")) == href
    ]
    if not matches:
        return None
    matches.sort(key=lambda row: (0 if 0 <= float(row.get("top", -1)) <= float(row.get("viewport", 0)) else 1, abs(float(row.get("top", 0)))))
    return int(matches[0]["index"])
