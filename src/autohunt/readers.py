import re
from html import unescape
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from .models import (
    ApplicationFlowDTO,
    Capabilities,
    ChatListDTO,
    ChatListItemDTO,
    ChatMessageDTO,
    ChatThreadDTO,
    NegotiationsDTO,
    PageFamily,
    PageObservation,
    SearchDTO,
    VacancyDTO,
)


def _text(html: str) -> str:
    without_nonvisible = re.sub(
        r"<(script|style|template)[^>]*>.*?</\1\s*>", " ", html, flags=re.IGNORECASE | re.DOTALL
    )
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", without_nonvisible))).strip()


def detect_page(url: str, html: str, *, visible_text: str | None = None) -> PageObservation:
    """Classify from rendered text when available; source scripts are not page evidence."""
    text = (visible_text if visible_text is not None else _text(html)).lower()
    caps = Capabilities(
        can_open_vacancy="vacancy" in url or "ваканси" in text,
        can_start_application="откликнуться" in text,
        has_cover_letter_field="сопроводитель" in text,
        has_submit_control="отправить" in text,
        has_post_application_cover_action="приложить сопроводительное" in text,
        has_chat_input="сообщение" in text,
        has_external_form_link=bool(re.search(r"https?://", text)),
        already_applied="вы откликнулись" in text,
    )
    if "captcha" in text or "подтвердите, что вы не робот" in text:
        family = PageFamily.CAPTCHA
    elif "chatik.hh.ru/chat/" in url or "chatik-new-message-text" in html:
        family = PageFamily.CHAT_THREAD
    elif "/vacancy/" in url or "вы откликнулись" in text:
        family = PageFamily.VACANCY
    elif "negotiations" in url or "отклики и приглашения" in text:
        family = PageFamily.NEGOTIATIONS
    elif "/vacancies" in url or "/search/vacancy" in url:
        family = PageFamily.SEARCH_RESULTS
    elif "profile" in url:
        family = PageFamily.PROFILE
    else:
        family = PageFamily.UNKNOWN
    confidence = "confirmed" if family is not PageFamily.UNKNOWN else "unknown"
    return PageObservation(
        url=url, family=family, capabilities=caps, confidence=confidence, evidence=[family]
    )


class VacancyReader:
    def read(self, url: str, html: str) -> VacancyDTO:
        obs = detect_page(url, html)
        title = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
        company = re.search(
            r'data-qa=["\']vacancy-company-name["\'][^>]*>(.*?)</', html, re.IGNORECASE | re.DOTALL
        )
        return VacancyDTO(
            url=url,
            vacancy_id=(re.search(r"/vacancy/(\d+)", url) or [None, None])[1],
            title=_text(title.group(1)) if title else None,
            company=_text(company.group(1)) if company else None,
            description_summary=_text(html)[:500] or None,
            observation=obs,
        )


class SearchReader:
    """Read captured search/map and filter-drawer JSON without a live browser."""

    def read(self, raw: dict) -> SearchDTO:
        url = raw.get("url")
        controls = raw.get("controls", [])
        text = str(raw.get("text", ""))
        drawer_open = bool(text) and "Фильтры" in text
        query = None
        if url:
            query = parse_qs(urlparse(url).query).get("text", [None])[0]
        if not query:
            query = next((c.get("value") for c in controls if c.get("name") == "text"), None)
        capabilities = Capabilities(can_open_vacancy=bool(raw.get("cards", 0)))
        observation = PageObservation(
            url=url or "fixture://advanced-search-drawer",
            family=PageFamily.SEARCH_RESULTS,
            capabilities=capabilities,
            confidence="confirmed" if url else "partial",
            evidence=[
                "captured search fixture",
                "filter drawer" if drawer_open else "map/list state",
            ],
        )
        return SearchDTO(
            url=url,
            query=query,
            card_count=raw.get("cards"),
            filter_drawer_open=drawer_open,
            control_count=len(controls),
            observation=observation,
        )


class NegotiationsReader:
    """Read the captured negotiations list. Never infer per-card status from nearby text."""

    _tab_qa_to_status: ClassVar[dict[str, str]] = {
        "tab_filter_all": "all",
        "tab_filter_invitation": "invitation",
        "tab_filter_interview": "interview",
        "tab_filter_hired": "hired",
        "tab_filter_awaiting": "awaiting",
        "tab_filter_discard": "discard",
    }

    def read(self, raw: dict) -> NegotiationsDTO:
        tab_counts: dict[str, int] = {}
        statuses: list[str] = []
        items = 0
        for action in raw.get("actions", []):
            qa = action.get("qa")
            text = str(action.get("text", ""))
            if qa == "negotiations-item":
                items += 1
                statuses.append("unknown")
            if qa in self._tab_qa_to_status:
                match = re.search(r"(\d+)", text)
                if match:
                    tab_counts[self._tab_qa_to_status[qa]] = int(match.group(1))
        observation = PageObservation(
            url=raw.get("url", "fixture://negotiations"),
            family=PageFamily.NEGOTIATIONS,
            capabilities=Capabilities(),
            confidence="confirmed",
            evidence=["captured negotiations list", "per-card status deliberately unknown"],
        )
        return NegotiationsDTO(
            url=observation.url,
            tab_counts=tab_counts,
            visible_item_count=items,
            visible_statuses=statuses,
            observation=observation,
        )


class ApplicationFlowReader:
    """Read form metadata or stored answers; never produce a submission instruction."""

    def read_cover_letter_dialog(self, raw: dict) -> ApplicationFlowDTO:
        textarea = raw.get("textarea", {})
        submit = raw.get("submit", {})
        observation = PageObservation(
            url="fixture://cover-letter-dialog",
            family=PageFamily.APPLICATION_FLOW,
            capabilities=Capabilities(
                has_cover_letter_field=bool(textarea),
                has_submit_control=bool(submit),
            ),
            confidence="confirmed",
            evidence=["captured post-application cover letter dialog"],
        )
        return ApplicationFlowDTO(
            cover_letter_required=textarea.get("required"),
            cover_letter_max_length=10000 if "10000" in raw.get("dialog_text", "") else None,
            submit_enabled=not submit.get("disabled", True),
            observation=observation,
        )

    def read_employer_answers(self, raw: dict) -> ApplicationFlowDTO:
        answers = raw.get("answers", [])
        observation = PageObservation(
            url=f"fixture://employer-answers/{raw.get('vacancy_id', 'unknown')}",
            family=PageFamily.APPLICATION_FLOW,
            capabilities=Capabilities(has_employer_questions=bool(answers)),
            confidence="partial",
            evidence=["stored answers; form state not captured"],
        )
        return ApplicationFlowDTO(
            vacancy_id=raw.get("vacancy_id"),
            employer_question_count=len(answers),
            observation=observation,
        )


class ChatListReader:
    """Read keyword-classified queue; categories are hints, never confirmed statuses."""

    def read(self, raw: dict) -> ChatListDTO:
        items = [
            ChatListItemDTO(
                chat_id=str(row["chat_id"]),
                title=row["title"],
                category_hints=list(row.get("categories", [])),
            )
            for row in raw.get("rows", [])
            if row.get("chat_id") and row.get("title")
        ]
        return ChatListDTO(
            items=items,
            observation=PageObservation(
                url="fixture://chatik-priority-queue",
                family=PageFamily.CHAT_LIST,
                capabilities=Capabilities(),
                confidence="partial",
                evidence=[
                    "read-only keyword classification",
                    "categories are not confirmed statuses",
                ],
            ),
        )


class ChatThreadReader:
    def read(self, url: str, html: str) -> ChatThreadDTO:
        obs = detect_page(url, html)
        chat = re.search(r"/chat/(\d+)", url)
        text = _text(html)
        chunks = [x.strip() for x in re.split(r"\n{2,}|(?<=\.)\s{2,}", text) if len(x.strip()) > 3]
        return ChatThreadDTO(
            chat_id=chat.group(1) if chat else None,
            url=url,
            title=chunks[0] if chunks else None,
            messages=[ChatMessageDTO(direction="unknown", text=x) for x in chunks[:50]],
            observation=obs,
        )
