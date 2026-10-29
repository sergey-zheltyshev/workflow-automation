"""One real HH read-only vertical slice. It never fills, clicks, sends or submits."""

from __future__ import annotations

from contextlib import closing

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .context import evaluate_persisted_vacancy
from .decisions import create_semantic_work_item
from .live_adapter import ReadOnlyHHSession, automation_browser_profile, rendered_body
from .models import (
    Capabilities,
    ChatMessageDTO,
    ChatThreadDTO,
    PageFamily,
    PageObservation,
    VacancyDTO,
)
from .sync import RegistrySync


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _vacancy_id(url: str) -> str | None:
    match = re.search(r"/vacancy/(\d+)", urlparse(url).path)
    return match.group(1) if match else None


def _normalize(value: str | None) -> str:
    return " ".join((value or "").split())


def _structured_facts(description: str, salary: str, title: str) -> dict[str, object]:
    """Keep extraction value separate from evidence strength for policy decisions."""
    text = _normalize(description)
    lower = text.lower()
    currency = "RUB" if ("₽" in salary or "руб" in salary.lower()) else "KZT" if ("₸" in salary or "тенге" in salary.lower()) else None
    amounts = [int(x.replace(" ", "")) for x in re.findall(r"\d[\d ]{2,}\d|\d{4,}", salary.replace("\u00a0", " "))]
    fmt = "remote" if any(x in lower for x in ("удалён", "удален", "remote")) else "hybrid" if "гибрид" in lower else "onsite" if any(x in lower for x in ("в офисе", "офисный формат")) else None
    schedule = next((m for m in re.findall(r"\b\d\s*/\s*\d\b", text) ), None)
    country = "Казахстан" if any(x in lower for x in ("алматы", "казахстан", "₸", "тенге")) else "Россия" if any(x in lower for x in ("москва", "росси", "руб", "₽")) else None
    provenance = {
        "salary_from": "explicit_hh_metadata" if amounts and salary else "unknown",
        "salary_to": "explicit_hh_metadata" if amounts and salary else "unknown",
        "currency": "explicit_hh_metadata" if currency and salary else "unknown",
        "work_format": "explicit_vacancy_text" if fmt else "unknown",
        "schedule": "explicit_vacancy_text" if schedule else "unknown",
        # City/currency does not establish employer or legal work country.
        "country": "inferred" if country else "unknown",
    }
    return {"title": title, "salary_from": min(amounts) if amounts else None, "salary_to": max(amounts) if amounts else None, "currency": currency, "country": country, "work_format": fmt, "schedule": schedule, "salary_raw": salary or None, "provenance": provenance}


@dataclass
class RunMetrics:
    run_id: str
    query: str
    started_at: str
    search_cards: int = 0
    new_vacancy_ids: int = 0
    full_vacancies: int = 0
    parse_failures: int = 0
    chat_urls: int = 0
    chats_changed: int = 0
    messages_seen: int = 0
    chat_thread_failures: int = 0
    browser_navigations: int = 0
    retries: int = 0
    captcha: bool = False
    vacancies_opened: int = 0
    ready_confirmed: int = 0
    incidents: int = 0
    vacancy_ready_total_ms: int = 0
    full_vacancy_content: int = 0


class CaptchaDetected(RuntimeError):
    pass


class AuthenticationLost(RuntimeError):
    pass


class VacancyNeedsResearch(RuntimeError):
    def __init__(self, detail: str, incident_path: str) -> None:
        super().__init__(detail)
        self.incident_path = incident_path


class LiveReadOnlyRun:
    """Deterministic Playwright reader; all writes are local evidence/SQLite/work JSONL."""

    def __init__(
        self, root: Path, *, cdp_endpoint: str, query: str, max_pages: int, max_full: int, max_workers: int,
        chat_only: bool = False, skip_chat_sync: bool = False, queries: list[str] | None = None,
    ) -> None:
        self.root = root
        self.cdp_endpoint = cdp_endpoint
        self.query = query
        self.max_pages = max_pages
        self.max_full = max_full
        self.max_workers = max_workers
        self.chat_only = chat_only
        self.skip_chat_sync = skip_chat_sync
        self.queries = [value.strip() for value in (queries or [query]) if value.strip()]
        if not self.queries:
            raise ValueError("at least one search query is required")
        self.run_id = f"live-{_stamp()}"
        self.evidence = root / "var" / "evidence" / "live_runs" / self.run_id
        self.work_dir = root / "work_items"
        self.database = root / "var" / "autohunt.sqlite"
        self.sync = RegistrySync(self.database)
        self.metrics = RunMetrics(self.run_id, query, datetime.now(UTC).isoformat())
        self.chat_work_items: list[dict[str, object]] = []
        self._ready_order = 0
        self._ready_order_lock = asyncio.Lock()

    def _journal(self, kind: str, payload: dict[str, object]) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
            candidate_id = row[0] if row else None
            if candidate_id:
                binding = db.execute("SELECT binding_id FROM account_bindings WHERE candidate_id=? AND active=1", (candidate_id,)).fetchone()
                if binding:
                    db.execute("INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (f"run:{self.run_id}:{kind}", candidate_id, binding[0], self.run_id, kind, json.dumps(payload, ensure_ascii=False), datetime.now(UTC).isoformat()))
                    db.execute("INSERT OR REPLACE INTO candidate_runs(run_id,candidate_id,binding_id,mode,status,started_at,finished_at) VALUES(?,?,?,?,?,?,?)", (self.run_id, candidate_id, binding[0], "live_read_only", "running" if kind == "run_started" else "checkpointed", self.metrics.started_at, datetime.now(UTC).isoformat() if kind != "run_started" else None))

    async def _read_body(self, page: Page) -> str:
        text = await rendered_body(page)
        normalized = _normalize(text).lower()
        if "подтвердите, что вы не робот" in normalized or "captcha" in normalized:
            self.metrics.captcha = True
            raise CaptchaDetected(f"CAPTCHA rendered at {page.url}")
        if "/account/login" in urlparse(page.url).path:
            raise AuthenticationLost(f"HH session redirected to login: {page.url}")
        return text

    async def _open(self, session: ReadOnlyHHSession, url: str) -> Page:
        # Read-only navigation can safely retry transient HH HTTP/2 failures;
        # mutation paths never use this helper.
        last: Exception | None = None
        for attempt in range(3):
            self.metrics.browser_navigations += 1
            try:
                return await session.open_read_only(url)
            except (PlaywrightTimeoutError, RuntimeError) as exc:
                last = exc
            except Exception as exc:  # Playwright network errors are provider-specific classes.
                if "ERR_HTTP2_PROTOCOL_ERROR" not in str(exc):
                    raise
                last = exc
            self.metrics.retries += 1
            await asyncio.sleep(0.75 * (attempt + 1))
        assert last is not None
        raise last

    async def _capture(self, page: Page, label: str) -> tuple[str, str]:
        html = await page.content()
        path = self.evidence / f"{label}.html"
        path.write_text(html, encoding="utf-8", newline="\n")
        return path.name, hashlib.sha256(html.encode("utf-8")).hexdigest()

    async def _first_text(self, page: Page, selector: str, timeout: int = 5_000) -> str:
        locator = page.locator(selector)
        if await locator.count() == 0:
            return ""
        try:
            return _normalize(await locator.first.inner_text(timeout=timeout))
        except PlaywrightTimeoutError:
            return ""

    def _known_vacancy_ids(self) -> set[str]:
        with closing(sqlite3.connect(self.database)) as db, db:
            candidate_id = self.sync.candidate_id
            if not candidate_id:
                raise RuntimeError("candidate-scoped vacancy processing requires active candidate")
            return {str(row[0]) for row in db.execute("SELECT vacancy_id FROM candidate_vacancy_state WHERE candidate_id=?", (candidate_id,))}

    def _known_chat_ids(self) -> set[str]:
        with closing(sqlite3.connect(self.database)) as db, db:
            candidate_id = self.sync.candidate_id
            if not candidate_id:
                raise RuntimeError("candidate-scoped chat sync requires active candidate")
            return {str(row[0]) for row in db.execute("SELECT chat_id FROM candidate_chats WHERE candidate_id=?", (candidate_id,))}

    async def _collect_chat_urls(self, page: Page) -> list[dict[str, str]]:
        known: dict[str, str] = {}
        stable = 0
        for _ in range(45):
            rows = await page.locator('a[href*="/chat/"]').evaluate_all(
                """links => links.map(a => ({url:a.href, title:(a.innerText||'').trim()}))
                  .filter(x => /\\/chat\\/\\d+/.test(x.url))"""
            )
            before = len(known)
            for row in rows:
                known[row["url"]] = row["title"]
            viewport = await page.evaluate(
                """() => {
                  const el = [...document.querySelectorAll('*')].filter(x => {
                    const s = getComputedStyle(x);
                    return (s.overflowY === 'auto' || s.overflowY === 'scroll') &&
                      x.scrollHeight > x.clientHeight + 120 && x.clientHeight > 150;
                  }).sort((a,b) => (b.scrollHeight-b.clientHeight)-(a.scrollHeight-a.clientHeight))[0];
                  if (!el) return {atEnd:true};
                  el.scrollTop = Math.min(el.scrollTop + Math.max(300, Math.floor(el.clientHeight*.85)), el.scrollHeight);
                  return {atEnd:el.scrollTop + el.clientHeight >= el.scrollHeight - 2};
                }"""
            )
            stable = stable + 1 if len(known) == before and viewport["atEnd"] else 0
            if stable >= 3:
                break
            await page.wait_for_timeout(450)
        return [{"url": url, "title": title} for url, title in sorted(known.items())]

    async def _sync_chats(self, session: ReadOnlyHHSession) -> None:
        page = await self._open(session, "https://chatik.hh.ru/")
        try:
            await self._read_body(page)
            rows = await self._collect_chat_urls(page)
            name, digest = await self._capture(page, "chat-list")
        finally:
            await page.close()
        self.metrics.chat_urls = len(rows)
        known = self._known_chat_ids()
        old_messages: dict[str, dict[str, set[str]]] = {}
        with closing(sqlite3.connect(self.database)) as db, db:
            candidate_id = self.sync.candidate_id
            if not candidate_id:
                raise RuntimeError("candidate-scoped message sync requires active candidate")
            for chat_id, direction, text in db.execute("SELECT chat_id,direction,text FROM candidate_messages WHERE candidate_id=?", (candidate_id,)):
                old_messages.setdefault(str(chat_id), {}).setdefault(str(text), set()).add(str(direction))

        semaphore = asyncio.Semaphore(self.max_workers)

        async def read_thread(row: dict[str, str]) -> tuple[ChatThreadDTO | None, bool]:
            match = re.search(r"/chat/(\d+)", row["url"])
            if not match:
                return None, False
            chat_id = match.group(1)
            page: Page | None = None
            try:
                async with semaphore:
                    page = await self._open(session, row["url"])
                    await self._read_body(page)
                    messages_raw = await page.locator('[data-qa^="chatik-chat-message-"]').evaluate_all(
                        """els => els.filter(e => /^chatik-chat-message-\\d+$/.test(e.getAttribute('data-qa')||''))
                          .map(e => ({id:e.getAttribute('data-qa'), text:(e.querySelector('[data-qa$="-text"]')?.innerText||e.innerText||'').trim(), author:(e.querySelector('[data-qa="chat-bubble-author-name"]')?.innerText||'').trim(), cls:e.className||'', qa:e.getAttribute('data-qa')||'', datetime:(e.querySelector('time')?.getAttribute('datetime')||''), html:e.outerHTML}))"""
                    )
                    def direction(raw: dict[str, object]) -> str:
                        signal = f"{raw.get('cls', '')} {raw.get('qa', '')} {raw.get('author', '')} {raw.get('html', '')}".lower()
                        if any(marker in signal for marker in ("system", "bot", "автоответ", "hh.ru")):
                            return "system"
                        if any(marker in signal for marker in ("outgoing", "own", "mine", "message--out", "chat-bubble--out")):
                            return "outgoing"
                        if any(marker in signal for marker in ("incoming", "employer", "recruiter", "message--in", "chat-bubble--in")):
                            return "incoming"
                        if str(raw.get("author") or "").strip():
                            return "incoming"
                        return "unknown"
                    messages = [
                        ChatMessageDTO(
                            direction=direction(x),
                            text=_normalize(str(x["text"])),
                            received_at=str(x.get("datetime") or "") if "T" in str(x.get("datetime") or "") else None,
                        )
                        for x in messages_raw if _normalize(str(x["text"]))
                    ]
                    payload = {"url": page.url, "chat_id": chat_id, "messages": [m.model_dump() for m in messages]}
                    evidence_file = self.evidence / f"chat-{chat_id}.json"
                    evidence_file.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
                    changed = chat_id not in known or any(
                        m.text not in old_messages.get(chat_id, {}) or m.direction not in old_messages.get(chat_id, {}).get(m.text, set())
                        for m in messages
                    )
                    return ChatThreadDTO(chat_id=chat_id, url=row["url"], title=_normalize(row["title"]) or None, messages=messages, observation=PageObservation(url=row["url"], family=PageFamily.CHAT_THREAD, capabilities=Capabilities(), confidence="confirmed", evidence=[evidence_file.name])), changed
            except CaptchaDetected:
                raise
            except (PlaywrightTimeoutError, RuntimeError) as exc:
                self.metrics.chat_thread_failures += 1
                (self.evidence / f"chat-{chat_id}-error.txt").write_text(str(exc) + "\n", encoding="utf-8")
                return None, False
            finally:
                if page:
                    await page.close()

        results = await asyncio.gather(*(read_thread(row) for row in rows))
        threads = [thread for thread, _ in results if thread is not None]
        changed_threads = [thread for thread, changed in results if thread is not None and changed]
        self.metrics.chats_changed = len(changed_threads)
        self.metrics.messages_seen = sum(len(thread.messages) for thread in threads)
        self.chat_work_items = [
            {"id": f"chat-{thread.chat_id}", "kind": "chat_review", "chat_id": thread.chat_id, "url": thread.url, "title": thread.title, "reason": "new_or_changed_message", "evidence_refs": thread.observation.evidence}
            for thread in changed_threads
        ]
        self.sync.sync_threads(threads)
        self.sync.record_snapshots([(digest, str(self.evidence / name), "chat_list")])

    async def _extract_cards(self, page: Page) -> list[dict[str, str | bool | None]]:
        return await page.locator('a[href*="/vacancy/"]').evaluate_all(
            """links => {
              const out = new Map();
              for (const a of links) {
                const m = a.href.match(/\\/vacancy\\/(\\d+)/); if (!m || out.has(m[1])) continue;
                const card = a.closest('[data-qa*="vacancy"]') || a.parentElement?.parentElement || a.parentElement;
                const text = (card?.innerText || '').trim();
                const pick = qa => (card?.querySelector(`[data-qa="${qa}"]`)?.innerText || '').trim();
                out.set(m[1], {vacancy_id:m[1], url:a.href.split('?')[0], title:(a.innerText||'').trim(),
                  company:pick('vacancy-serp__vacancy-employer'), salary:pick('vacancy-serp__vacancy-compensation'),
                  raw_text:text, already_applied:/вы\\s+откликнулись/i.test(text)});
              } return [...out.values()];
            }"""
        )

    def _search_url(self, page_number: int, query: str | None = None) -> str:
        return "https://hh.ru/search/vacancy?" + urlencode({"text": query or self.query, "page": page_number})

    async def _collect_search(self, session: ReadOnlyHHSession) -> list[dict[str, str | bool | None]]:
        cards: dict[str, dict[str, str | bool | None]] = {}
        for query_index, query in enumerate(self.queries, start=1):
            for number in range(self.max_pages):
                page = await self._open(session, self._search_url(number, query))
                try:
                    await self._read_body(page)
                    page_cards = await self._wait_for_search_cards(page)
                    for card in page_cards:
                        vacancy_id = str(card["vacancy_id"])
                        existing = cards.get(vacancy_id)
                        if existing is None:
                            cards[vacancy_id] = {**card, "source_queries": [query]}
                        else:
                            source_queries = list(existing.get("source_queries", []))
                            if query not in source_queries:
                                source_queries.append(query)
                            existing["source_queries"] = source_queries
                    name, digest = await self._capture(page, f"search-{query_index:02d}-{number + 1:02d}")
                    self.sync.record_snapshots([(digest, str(self.evidence / name), "search_results")])
                finally:
                    await page.close()
        self.metrics.search_cards = len(cards)
        return list(cards.values())

    async def _wait_for_search_cards(self, page: Page) -> list[dict[str, str | bool | None]]:
        """Search shell is not a result. Wait for cards or a rendered explicit no-results state."""
        if urlparse(page.url).path != "/search/vacancy":
            raise RuntimeError(f"unexpected search route: {page.url}")
        for _ in range(90):
            cards = await self._extract_cards(page)
            if cards:
                return cards
            body = _normalize(await rendered_body(page)).lower()
            if "по вашему запросу ничего не найдено" in body or "ничего не найдено" in body:
                return []
            await page.wait_for_timeout(500)
        raise RuntimeError(f"search cards did not render: {page.url}")

    @staticmethod
    def _meaningful_description(description: str) -> bool:
        normalized = _normalize(description).lower()
        blocked = ("мы используем файлы cookie", "загрузка", "loading", "подождите")
        return len(normalized) >= 120 and not any(token in normalized[:180] for token in blocked)

    async def _visible_dialogs(self, page: Page) -> list[dict[str, str]]:
        return await page.locator('[role="dialog"], [aria-modal="true"], dialog, [data-qa*="modal"], [data-qa*="popup"]').evaluate_all(
            """els => els.filter(el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
              return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 2 && r.height > 2; })
              .map(el => ({tag:el.tagName, qa:el.getAttribute('data-qa')||'', text:(el.innerText||'').trim().slice(0,1200)}))"""
        )

    @staticmethod
    def _classify_dialog(dialog: dict[str, str]) -> str:
        text = _normalize(dialog["text"]).lower()
        if "cookie" in text or "файлы cookie" in text:
            return "cookie"
        if "вы откликаетесь на вакансию в другой стране" in text:
            return "country_application"
        return "unknown"

    async def _wait_for_vacancy_ready(self, page: Page, expected_id: str) -> tuple[str, str, list[str]]:
        """Proof is URL + real title/description + two equal React-rendered snapshots, never a sleep."""
        deadline = time.monotonic() + 45
        previous: str | None = None
        while time.monotonic() < deadline:
            dialogs = await self._visible_dialogs(page)
            classifications = [self._classify_dialog(dialog) for dialog in dialogs]
            if "unknown" in classifications:
                raise RuntimeError(f"unknown visible dialog: {dialogs}")
            if _vacancy_id(page.url) != expected_id:
                raise RuntimeError(f"unexpected vacancy route: {page.url}")
            title = await self._first_text(page, "h1", timeout=1_500)
            description = await self._first_text(page, '[data-qa="vacancy-description"]', timeout=1_500)
            if title and self._meaningful_description(description):
                fingerprint = hashlib.sha256(f"{title}\x00{description}".encode()).hexdigest()
                if fingerprint == previous:
                    return title, description, classifications
                previous = fingerprint
            else:
                previous = None
            await page.wait_for_timeout(250)
        raise RuntimeError(f"vacancy READY proof timed out: {page.url}")

    async def _capture_incident(
        self, page: Page, *, expected_id: str, index: int, detail: str, console: list[dict[str, str]], network: list[dict[str, str]],
    ) -> str:
        incident = self.evidence / "incidents" / f"vacancy-{expected_id}-{_stamp()}"
        incident.mkdir(parents=True, exist_ok=False)
        failures: list[str] = []
        async def attempt(name: str, action: object) -> None:
            try:
                await action  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 - diagnostics must not hide original issue
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        await attempt("viewport", page.screenshot(path=str(incident / "viewport.png")))
        await attempt("full_page", page.screenshot(path=str(incident / "full-page.png"), full_page=True))
        try:
            html = await page.content()
            (incident / "dom.html").write_text(html, encoding="utf-8", newline="\n")
        except Exception as exc:  # noqa: BLE001 - preserve primary read failure
            failures.append(f"dom: {type(exc).__name__}: {exc}")
        try:
            visible = await page.locator("body").inner_text(timeout=3_000)
            (incident / "visible.txt").write_text(visible, encoding="utf-8", newline="\n")
        except Exception as exc:  # noqa: BLE001 - preserve primary read failure
            failures.append(f"visible_text: {type(exc).__name__}: {exc}")
        try:
            dialogs = await self._visible_dialogs(page)
        except Exception as exc:  # noqa: BLE001 - preserve primary read failure
            dialogs = []
            failures.append(f"dialogs: {type(exc).__name__}: {exc}")
        manifest = {
            "expected": {"vacancy_id": expected_id, "ready": "matching URL, h1, meaningful vacancy-description, stable React snapshot, classified dialogs"},
            "actual": {"url": page.url, "page_identity": f"vacancy-{index:02d}", "error": detail,
                       "dialogs": [{**dialog, "classification": self._classify_dialog(dialog)} for dialog in dialogs]},
            "console_errors": console, "network_errors": network, "capture_failures": failures,
            "playwright_trace": str(self.evidence / "playwright-trace.zip"),
        }
        (incident / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.metrics.incidents += 1
        return str(incident)

    async def _read_vacancy(self, session: ReadOnlyHHSession, card: dict[str, str | bool | None], index: int) -> dict[str, object]:
        expected = str(card["vacancy_id"])
        page = await session.new_hh_page()
        console: list[dict[str, str]] = []
        network: list[dict[str, str]] = []
        page.on("console", lambda msg: console.append({"type": msg.type, "text": msg.text}))
        page.on("pageerror", lambda error: console.append({"type": "pageerror", "text": str(error)}))
        page.on("requestfailed", lambda request: network.append({"url": request.url, "failure": str(request.failure)}))
        opened = time.monotonic()
        self.metrics.browser_navigations += 1
        self.metrics.vacancies_opened += 1
        try:
            await session.navigate_hh_page(page, str(card["url"]))
            await self._read_body(page)
            title, description, dialogs = await self._wait_for_vacancy_ready(page, expected)
            ready_ms = round((time.monotonic() - opened) * 1000)
            self.metrics.ready_confirmed += 1
            self.metrics.vacancy_ready_total_ms += ready_ms
            self.metrics.full_vacancy_content += 1
            async with self._ready_order_lock:
                self._ready_order += 1
                screenshot_due = self._ready_order % 10 == 0
            company = await self._first_text(page, '[data-qa="vacancy-company-name"]')
            salary = await self._first_text(page, '[data-qa="vacancy-salary"]')
            name, digest = await self._capture(page, f"vacancy-{index:02d}")
            content_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()
            proof = {"vacancy_id": expected, "final_url": page.url, "title": title, "description_length": len(description),
                     "content_hash": content_hash, "ready_at": datetime.now(UTC).isoformat(), "open_to_ready_ms": ready_ms,
                     "page_identity": f"vacancy-{index:02d}", "dialogs": dialogs}
            proof_path = self.evidence / f"vacancy-{index:02d}-ready.json"
            proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            screenshot = ""
            if screenshot_due:
                screenshot_path = self.evidence / f"vacancy-{index:02d}-sanity.png"
                await page.screenshot(path=str(screenshot_path))
                screenshot = screenshot_path.name
            return {
                "vacancy_id": expected, "title": title, "company": company or card["company"] or "", "salary": salary or card["salary"] or "",
                "structured_facts": _structured_facts(description, salary or str(card["salary"] or ""), title),
                "description": description, "url": page.url, "source_query": str(card.get("source_queries", [self.query])[0]),
                "source_queries": list(card.get("source_queries", [self.query])),
                "already_applied": bool(card["already_applied"]), "evidence": {"file": name, "sha256": digest, "ready_proof": proof_path.name, "sanity_screenshot": screenshot},
            }
        except CaptchaDetected:
            raise
        except Exception as exc:
            incident = await self._capture_incident(page, expected_id=expected, index=index, detail=f"{type(exc).__name__}: {exc}", console=console, network=network)
            raise VacancyNeedsResearch(str(exc), incident) from exc
        finally:
            await page.close()

    async def run(self) -> dict[str, object]:
        self.evidence.mkdir(parents=True, exist_ok=False)
        session = ReadOnlyHHSession(
            automation_browser_profile(self.root),
            self.root / "var" / "controller.lock",
        )
        started = time.monotonic()
        self._journal("run_started", {"queries": self.queries, "mode": "read_only", "chat_only": self.chat_only})
        try:
            await session.start()
            if not session.context:
                raise RuntimeError("launched browser has no context")
            await session.context.tracing.start(screenshots=True, snapshots=True, sources=True)
            identity_page = await self._open(session, "https://hh.ru/")
            try:
                await self._read_body(identity_page)
                identity = {"url": identity_page.url, "title": await identity_page.title()}
                name, digest = await self._capture(identity_page, "identity")
                self.sync.record_snapshots([(digest, str(self.evidence / name), "identity")])
            finally:
                await identity_page.close()
            self.work_dir.mkdir(exist_ok=True)
            chat_work_file = self.work_dir / f"chats_{self.run_id}.jsonl"
            if not self.skip_chat_sync:
                await self._sync_chats(session)
            chat_work_file.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.chat_work_items),
                encoding="utf-8",
            )
            if self.chat_only:
                report = {
                    "identity": identity,
                    "metrics": asdict(self.metrics),
                    "chat_work_items": str(chat_work_file),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
                (self.evidence / "manifest.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                self._journal("run_completed", report["metrics"])
                return report
            cards = await self._collect_search(session)
            known = self._known_vacancy_ids()
            new_cards = [card for card in cards if str(card["vacancy_id"]) not in known][: self.max_full]
            self.metrics.new_vacancy_ids = len(new_cards)
            semaphore = asyncio.Semaphore(self.max_workers)

            async def read_one(index: int, card: dict[str, str | bool | None]) -> dict[str, object] | None:
                try:
                    async with semaphore:
                        return await self._read_vacancy(session, card, index)
                except CaptchaDetected:
                    raise
                except AuthenticationLost:
                    raise
                except VacancyNeedsResearch:
                    self.metrics.parse_failures += 1
                    self.sync.record_external_item(
                        f"needs-research:{self.run_id}:{card['vacancy_id']}", str(card["vacancy_id"]), str(card["url"]), "vacancy", "needs_research"
                    )
                    return None
                except (PlaywrightTimeoutError, RuntimeError) as exc:
                    self.metrics.parse_failures += 1
                    (self.evidence / f"vacancy-{index:02d}-error.txt").write_text(str(exc) + "\n", encoding="utf-8")
                    return None

            results = await asyncio.gather(
                *(read_one(index, card) for index, card in enumerate(new_cards, start=1))
            )
            full = [row for row in results if row is not None]
            self.metrics.full_vacancies = len(full)
            self.sync.sync_vacancies(
                self.run_id, self.query, self._search_url(0, self.query),
                [VacancyDTO(vacancy_id=row["vacancy_id"], url=row["url"], title=row["title"], company=row["company"], compensation=row["salary"], full_description=row["description"], structured_facts=row["structured_facts"], description_summary=row["description"][:500], observation=PageObservation(url=row["url"], family=PageFamily.VACANCY, capabilities=Capabilities(already_applied=bool(row["already_applied"])), confidence="confirmed", evidence=[row["evidence"]["file"]])) for row in full],
            )
            # Collector hands every non-hard-rejected confirmed vacancy directly
            # to the official semantic batch protocol. No ad-hoc SQLite bridge.
            with closing(sqlite3.connect(self.database)) as db, db:
                revisions = {
                    str(vacancy_id): str(revision)
                    for vacancy_id, revision in db.execute(
                        "SELECT vacancy_id,revision FROM vacancy_fact_revisions WHERE captured_at IN "
                        "(SELECT MAX(f2.captured_at) FROM vacancy_fact_revisions f2 GROUP BY f2.vacancy_id)"
                    )
                }
            for row in full:
                vacancy_id = str(row["vacancy_id"])
                revision = revisions.get(vacancy_id)
                if revision:
                    evaluate_persisted_vacancy(self.database, vacancy_id, revision)
                    create_semantic_work_item(self.database, vacancy_id=vacancy_id, source_run=self.run_id)
            work_file = self.work_dir / f"vacancies_{self.run_id}.jsonl"
            work_file.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in full), encoding="utf-8")
            report = {
                "identity": identity, "queries": self.queries, "browser_attach_count": 1, "metrics": asdict(self.metrics), "work_items": str(work_file), "chat_work_items": str(chat_work_file),
                "ready_proofs": sorted(path.name for path in self.evidence.glob("vacancy-*-ready.json")),
                "sanity_screenshots": sorted(str(path) for path in self.evidence.glob("vacancy-*-sanity.png")),
                "incident_bundles": sorted(str(path) for path in (self.evidence / "incidents").glob("*") if path.is_dir()) if (self.evidence / "incidents").exists() else [],
                "average_open_to_ready_seconds": round(self.metrics.vacancy_ready_total_ms / self.metrics.ready_confirmed / 1000, 3) if self.metrics.ready_confirmed else 0,
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
            (self.evidence / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._journal("run_completed", report["metrics"])
            return report
        except CaptchaDetected as exc:
            report = {"metrics": asdict(self.metrics), "stopped": "captcha", "detail": str(exc), "elapsed_seconds": round(time.monotonic() - started, 2)}
            (self.evidence / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._journal("run_stopped_captcha", {"detail": str(exc)})
            return report
        except AuthenticationLost as exc:
            report = {"metrics": asdict(self.metrics), "stopped": "authentication_lost", "detail": str(exc), "elapsed_seconds": round(time.monotonic() - started, 2)}
            (self.evidence / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._journal("run_stopped_authentication", {"detail": str(exc)})
            return report
        except Exception as exc:
            self._journal("run_failed", {"error_type": type(exc).__name__, "detail": str(exc)})
            raise
        finally:
            if session.context:
                try:
                    await session.context.tracing.stop(path=str(self.evidence / "playwright-trace.zip"))
                except Exception as exc:  # noqa: BLE001 - trace loss must be recorded, never mask outcome
                    (self.evidence / "trace-stop-error.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            await session.stop()


def write_benchmark(root: Path, report: dict[str, object]) -> Path:
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    elapsed = float(report["elapsed_seconds"])
    full = int(metrics["full_vacancies"])
    path = root / "reports" / "SEARCH_THROUGHPUT_BENCHMARK.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        "# Live HH search throughput benchmark\n\n"
        f"- run: `{metrics['run_id']}`\n- query: `{metrics['query']}`\n- search cards: {metrics['search_cards']}\n"
        f"- new vacancy IDs: {metrics['new_vacancy_ids']}\n- full vacancies read: {full}\n"
        f"- parse failures: {metrics['parse_failures']}\n- chat URLs discovered: {metrics['chat_urls']}\n"
        f"- changed/new chats: {metrics['chats_changed']}\n- total runtime: {elapsed:.2f} s\n"
        f"- avg full vacancy read: {(elapsed / full) if full else 0:.2f} s\n"
        f"- browser navigations: {metrics['browser_navigations']}\n- timeout/retry: {metrics['retries']}\n"
        f"- CAPTCHA: {metrics['captcha']}\n- Codex work items: {full}\n"
        "- data collected without Codex: all search cards, full vacancy DOM, chat list and SQLite sync.\n",
        encoding="utf-8",
    )
    return path
