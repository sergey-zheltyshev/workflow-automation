"""Full-live HH vacancy reader using the project persistent browser context.

Search cards provide only vacancy IDs and URLs.  Every discovered card is
opened as a vacancy page and its full rendered text becomes a SQLite revision
before any semantic decision is created.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError

from .context import evaluate_persisted_vacancy
from .decisions import create_semantic_work_item
from .direct_chat_sync import sync_direct_chats
from .models import Capabilities, PageFamily, PageObservation, VacancyDTO
from .production_browser import async_production_browser_context
from .sync import RegistrySync
from .ui_memory import open_incident


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalized(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _facts_from_body(body: str) -> dict[str, object]:
    """Only record explicit format/schedule facts; semantics belong to review."""
    normalized = _normalized(body).lower()
    observed: list[str] = []
    facts: dict[str, object] = {}
    if "формат работы: удалённо" in normalized:
        facts["work_format"] = "remote"
        observed.append("work_format")
    elif "формат работы: на месте работодателя" in normalized:
        facts["work_format"] = "onsite"
        observed.append("work_format")
    elif "формат работы: гибрид" in normalized:
        facts["work_format"] = "hybrid"
        observed.append("work_format")
    schedule = re.search(r"график:\s*(6/1|5/2|2/2|4/4)", normalized)
    if schedule:
        facts["schedule"] = schedule.group(1)
        observed.append("schedule")
    facts["observed_fields"] = observed
    return facts


async def _open(context: BrowserContext, url: str):
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    return page


async def _search_cards(context: BrowserContext, query: str, max_pages: int) -> tuple[list[dict[str, str]], bool]:
    cards: dict[str, dict[str, str]] = {}
    next_url = "https://hh.ru/search/vacancy?" + urlencode({"text": query})
    pagination_complete = True
    for number in range(max_pages):
        page = await _open(context, next_url)
        try:
            page_size = page.locator('[data-qa="items_on_page-menu"]')
            if await page_size.count() != 1 or "50" not in _normalized(await page_size.inner_text()):
                raise RuntimeError("HH search page size is not live-confirmed as 50")
            for _ in range(40):
                values = await page.locator('a[href*="/vacancy/"]').evaluate_all(
                    """els => els.map(e => ({href:e.href, title:(e.innerText||'').trim(),
                    card:(e.closest('[data-qa*=\"vacancy\"]')?.innerText||'').trim()}))"""
                )
                if values:
                    break
                await page.wait_for_timeout(250)
            for value in values:
                match = re.search(r"/vacancy/(\d+)", str(value["href"]))
                if not match:
                    continue
                vacancy_id = match.group(1)
                cards.setdefault(vacancy_id, {
                    "vacancy_id": vacancy_id,
                    "url": f"https://hh.ru/vacancy/{vacancy_id}",
                    "title": str(value["title"]).strip(),
                })
            next_link = page.locator('[data-qa="pager-next"]')
            if await next_link.count() == 0:
                break
            href = await next_link.first.get_attribute("href")
            if not href:
                break
            if number + 1 >= max_pages:
                pagination_complete = False
                break
            next_url = href if href.startswith("http") else f"https://hh.ru{href}"
        finally:
            await page.close()
    return list(cards.values()), pagination_complete


async def _read_vacancy(context: BrowserContext, card: dict[str, str]) -> tuple[VacancyDTO | None, dict[str, str] | None]:
    """Read one full vacancy page. Persistence remains serialized by caller."""
    vacancy_id = card["vacancy_id"]
    page = None
    try:
        page = await _open(context, card["url"])
        body = await page.locator("body").inner_text()
        title = (await page.locator("h1").first.inner_text()).strip()
        if vacancy_id not in page.url or len(_normalized(body)) < 120:
            raise RuntimeError("vacancy live body incomplete")
        company = ""
        company_locator = page.locator('[data-qa="vacancy-company-name"]')
        if await company_locator.count() == 1:
            company = (await company_locator.inner_text()).strip()
        return VacancyDTO(
            vacancy_id=vacancy_id, url=page.url.split("?")[0], title=title or card["title"], company=company or "unknown",
            full_description=body, structured_facts=_facts_from_body(body),
            observation=PageObservation(
                url=page.url, family=PageFamily.VACANCY,
                capabilities=Capabilities(can_open_vacancy=True, can_start_application="Откликнуться" in body, already_applied="Вы\xa0откликнулись" in body),
                confidence="confirmed", evidence=["full rendered vacancy body", _now()],
            ),
        ), None
    except (PlaywrightError, RuntimeError, TimeoutError) as exc:
        return None, {"vacancy_id": vacancy_id, "url": page.url if page else card["url"], "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if page:
            await page.close()


async def collect_direct_full_live(
    root: Path, *, queries: list[str], max_pages: int = 50, max_cards_per_query: int = 2500,
) -> dict[str, object]:
    """Read all discovered cards fully; no card-level business filtering."""
    if not queries:
        raise ValueError("at least one search query is required")
    if not 1 <= max_pages <= 50 or not 1 <= max_cards_per_query <= 2500:
        raise ValueError("search limits out of range")
    database = root / "var" / "autohunt.sqlite"
    sync = RegistrySync(database)
    run_id = f"direct-search:{uuid.uuid4()}"
    seen: set[str] = set()
    result: dict[str, object] = {"run_id": run_id, "discovered": 0, "full_read": 0, "semantic_items": 0, "incidents": []}
    async with async_production_browser_context(root) as context:
        result["chat_start"] = await sync_direct_chats(database, context, run_id=run_id)
        for query in queries:
            cards, pagination_complete = await _search_cards(context, query, max_pages)
            if not pagination_complete:
                open_incident(
                    database,
                    surface="SEARCH_PAGINATION",
                    detail="Configured page limit reached while HH still exposed a next page",
                    evidence={"query": query, "max_pages": max_pages},
                    safe_resume={"action": "continue_full_live_search", "query": query, "from_next_page": True},
                )
                incidents = result["incidents"]
                assert isinstance(incidents, list)
                incidents.append({"query": query, "error": "explicit_page_limit_reached_before_pagination_end"})
            query_dtos: list[VacancyDTO] = []
            query_seen = 0
            unseen_cards = [card for card in cards if card["vacancy_id"] not in seen]
            room = max_cards_per_query - query_seen
            pending_cards = unseen_cards[:room]
            for card in pending_cards:
                seen.add(card["vacancy_id"])
            query_seen += len(pending_cards)
            if len(pending_cards) < len(unseen_cards):
                open_incident(
                    database, surface="SEARCH_CARD_LIMIT",
                    detail="Configured card limit reached before all full-live search cards were read",
                    evidence={"query": query, "max_cards_per_query": max_cards_per_query, "cards_discovered": len(cards)},
                    safe_resume={"action": "continue_full_live_search", "query": query},
                )
                incidents = result["incidents"]
                assert isinstance(incidents, list)
                incidents.append({"query": query, "error": "explicit_card_limit_reached"})
            result["discovered"] = int(result["discovered"]) + len(pending_cards)
            for offset in range(0, len(pending_cards), 5):
                outcomes = await asyncio.gather(*(_read_vacancy(context, card) for card in pending_cards[offset:offset + 5]))
                for dto, failure in outcomes:
                    if dto:
                        query_dtos.append(dto)
                        result["full_read"] = int(result["full_read"]) + 1
                        continue
                    assert failure is not None
                    open_incident(
                        database, surface="SEARCH_VACANCY_READ", vacancy_id=failure["vacancy_id"],
                        detail=f"direct full read: {failure['error']}", evidence={"url": failure["url"], "query": query},
                        safe_resume={"action": "live_reread_vacancy", "vacancy_id": failure["vacancy_id"]},
                    )
                    incidents = result["incidents"]
                    assert isinstance(incidents, list)
                    incidents.append(failure)
            if query_dtos:
                revision = f"{run_id}:{uuid.uuid4()}"
                sync.sync_vacancies(revision, query, "https://hh.ru/search/vacancy", query_dtos)
                for dto in query_dtos:
                    evaluate_persisted_vacancy(database, dto.vacancy_id, revision)
                    if create_semantic_work_item(database, vacancy_id=dto.vacancy_id, source_run=run_id):
                        result["semantic_items"] = int(result["semantic_items"]) + 1
        result["chat_end"] = await sync_direct_chats(database, context, run_id=run_id)
    return result
