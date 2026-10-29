"""Direct-persistent HH application transport.

This replaces only the CDP session boundary. The existing Gateway remains the
only mutation owner; this module supplies one authenticated Playwright context
for a whole batch and records a UI incident instead of retrying unknown screens.
"""
from __future__ import annotations

from contextlib import closing

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic

from playwright.async_api import BrowserContext, Page

from .application_run import (
    MATCH,
    ApplicationBatchReport,
    ApplicationPlanItem,
    build_authoritative_application_queue,
    observed_state,
    resume_state,
)
from .context import bootstrap
from .direct_chat_sync import sync_direct_chats
from .gateway import ApplicationCircuitOpen, ApplicationGateway, ApplicationRequest
from .hh_application_adapter import HHApplicationAdapter
from .lifecycle import record_lifecycle
from .production_browser import async_production_browser_context
from .ui_memory import open_incident, record_observation


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _checkpoint(database: Path, *, stage: str, batch_id: str, payload: dict[str, object]) -> None:
    state = bootstrap(database)
    candidate_id = str(state.get("candidate_id") or "")
    binding = state.get("account_binding") or {}
    if not candidate_id or not binding:
        return
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "INSERT INTO candidate_checkpoints(candidate_id,binding_id,name,value_json,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(candidate_id,binding_id,name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (candidate_id, binding["binding_id"], "direct_batch", json.dumps({"batch_id": batch_id, "stage": stage, **payload}, ensure_ascii=False), _now()),
        )


async def _open(context: BrowserContext, url: str) -> Page:
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    return page


async def _capture_unknown(database: Path, page: Page, *, vacancy_id: str, detail: str) -> str:
    body = (await page.locator("body").inner_text())[:12_000]
    fingerprint = sha256(f"{page.url}\n{body[:2_000]}".encode()).hexdigest()
    observation_id = record_observation(
        database, surface="UNKNOWN_UI", url=page.url, fingerprint=fingerprint,
        evidence={"body": body, "title": await page.title()},
    )
    return open_incident(
        database, surface="UNKNOWN_UI", vacancy_id=vacancy_id, detail=detail,
        evidence={"observation_id": observation_id, "url": page.url},
        safe_resume={"action": "ui_map_then_live_reread", "vacancy_id": vacancy_id},
    )


def _normalized(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


async def _attach_vacancy_page_cover(
    database: Path, context: BrowserContext, item: ApplicationPlanItem,
) -> str:
    """Finish HH's explicit vacancy-page cover branch in the same batch context.

    This is deliberately narrow: no action means ``cover_pending`` rather than
    a speculative chat send. Chatik is a separate mapped surface.
    """
    page = await _open(context, item.vacancy_url)
    try:
        body = _normalized(await page.locator("body").inner_text())
        if "Вы откликнулись" not in body or "Резюме доставлено" not in body:
            return "needs_reconciliation"
        action = page.get_by_text("Приложить сопроводительное письмо", exact=True)
        if await action.count() == 0:
            return "cover_pending"
        if await action.count() != 1:
            return "needs_map"
        await action.click()
        await page.wait_for_timeout(650)
        field = page.locator("textarea:visible")
        if await field.count() != 1:
            return "needs_map"
        await field.fill(item.expected_cover_letter)
        await field.press("End")
        await field.press(" ")
        await field.press("Backspace")
        actual = await field.input_value()
        expected_hash = sha256(item.expected_cover_letter.encode()).hexdigest()
        if actual != item.expected_cover_letter or sha256(actual.encode()).hexdigest() != expected_hash:
            return "needs_reconciliation"
        submit = page.get_by_role("button", name="Отправить", exact=True)
        if await submit.count() != 1 or not await submit.is_enabled():
            return "needs_map"
        await submit.click()
        await page.wait_for_timeout(1_100)
        final = _normalized(await page.locator("body").inner_text())
        proof = (
            "Сопроводительное письмо отправлено" in final
            and await page.get_by_text("Приложить сопроводительное письмо", exact=True).count() == 0
        )
        if not proof:
            return "needs_reconciliation"
        candidate_id = str(bootstrap(database)["candidate_id"])
        with closing(sqlite3.connect(database)) as db, db:
            db.execute(
                "UPDATE candidate_application_processes SET state=?,detail=?,updated_at=? WHERE candidate_id=? AND vacancy_id=?",
                ("APPLICATION_CONFIRMED", "HH vacancy page: exact cover readback and rendered cover confirmation", _now(), candidate_id, item.vacancy_id),
            )
        record_lifecycle(
            database, candidate_id=candidate_id, vacancy_id=item.vacancy_id,
            state="COVER_CONFIRMED", decision_id=item.decision_id,
            live_revision=item.source_revision,
            note="HH vacancy page rendered cover confirmation after exact readback",
        )
        return "cover_confirmed"
    finally:
        await page.close()


async def _attach_chatik_cover(
    database: Path, context: BrowserContext, item: ApplicationPlanItem,
) -> str:
    """Use HH's explicit Chatik cover action when no vacancy-page action exists."""
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute(
            "SELECT c.url FROM candidate_chat_vacancy_links l "
            "JOIN candidate_chats c ON c.candidate_id=l.candidate_id AND c.chat_id=l.chat_id "
            "WHERE l.candidate_id=? AND l.vacancy_id=? ORDER BY c.updated_at DESC LIMIT 1",
            (str(bootstrap(database)["candidate_id"]), item.vacancy_id),
        ).fetchone()
        title = db.execute("SELECT title FROM vacancies WHERE vacancy_id=?", (item.vacancy_id,)).fetchone()
    chat_url = str(row[0]) if row else ""
    if not chat_url:
        listing = await _open(context, "https://hh.ru/chat")
        try:
            expected_title = str(title[0]) if title else ""
            links = await listing.locator('a[href*="/chat/"]').evaluate_all(
                "els => els.map(e => ({href:e.href, text:(e.innerText||'').trim()}))"
            )
            matches = [str(value["href"]) for value in links if expected_title and expected_title in str(value["text"])]
            chat_url = matches[0] if len(set(matches)) == 1 else ""
        finally:
            await listing.close()
    if not chat_url:
        return "cover_pending"
    page = await _open(context, chat_url)
    try:
        body = await page.locator("body").inner_text()
        if "Без сопроводительного письма" not in body:
            return "cover_pending"
        action = page.get_by_text("Добавить сопроводительное", exact=True)
        if await action.count() != 1:
            return "needs_map"
        await action.click()
        await page.wait_for_timeout(650)
        field = page.locator("textarea:visible")
        if await field.count() != 1:
            return "needs_map"
        await field.fill(item.expected_cover_letter)
        await field.press("End")
        await field.press(" ")
        await field.press("Backspace")
        actual = await field.input_value()
        if actual != item.expected_cover_letter:
            return "needs_reconciliation"
        submit = page.locator('button[data-qa="chatik-do-send-message"]')
        if await submit.count() != 1 or not await submit.is_enabled():
            return "needs_map"
        await submit.click()
        await page.wait_for_timeout(1_100)
        final = await page.locator("body").inner_text()
        proof = item.expected_cover_letter in final and await page.get_by_text("Добавить сопроводительное", exact=True).count() == 0
        if not proof:
            return "needs_reconciliation"
        candidate_id = str(bootstrap(database)["candidate_id"])
        with closing(sqlite3.connect(database)) as db, db:
            db.execute(
                "UPDATE candidate_application_processes SET state=?,detail=?,updated_at=? WHERE candidate_id=? AND vacancy_id=?",
                ("APPLICATION_CONFIRMED", "HH Chatik: exact cover rendered and one-time action consumed", _now(), candidate_id, item.vacancy_id),
            )
        record_lifecycle(
            database, candidate_id=candidate_id, vacancy_id=item.vacancy_id,
            state="COVER_CONFIRMED", decision_id=item.decision_id,
            live_revision=item.source_revision,
            note="HH Chatik rendered exact cover and consumed one-time action",
        )
        return "cover_confirmed"
    finally:
        await page.close()


async def preflight_direct_context(root: Path, context: BrowserContext, plan: list[ApplicationPlanItem]) -> list[dict[str, object]]:
    """Live read/reconcile using supplied persistent context; never submits."""
    database = root / "var" / "autohunt.sqlite"
    state = bootstrap(database)
    binding = state.get("account_binding") or {}
    candidate_id = str(state.get("candidate_id") or "")
    if not candidate_id or not binding:
        raise RuntimeError("active candidate context is incomplete")
    # HH vacancy pages do not consistently render account identity or selected
    # resume.  Read both from the authenticated applicant surface first, in the
    # same persistent context; vacancy pages then prove only target/action.
    profile_page = await _open(context, "https://hh.ru/applicant/resumes")
    try:
        profile_adapter = HHApplicationAdapter(profile_page, vacancy_id="profile", resume_id=str(binding["resume_id"]))
        profile_snapshot = await profile_adapter.live_identity_snapshot()
        profile_resume = resume_state(str(binding["resume_id"]), profile_snapshot["resume_options"])
        selected_profile_resume = next(
            (item for item in profile_snapshot["resume_options"] if item.get("selected")), None
        )
    finally:
        await profile_page.close()
    rows: list[dict[str, object]] = []
    for item in plan:
        page = await _open(context, item.vacancy_url)
        try:
            adapter = HHApplicationAdapter(page, vacancy_id=item.vacancy_id, resume_id=item.resume_id)
            reconciliation = await adapter.reconcile(item.vacancy_id, item.resume_id)
            capabilities = await adapter.inspect(item.vacancy_id, item.resume_id)
            # Applicant profile exposes resume cards but may omit cryptedUserId;
            # vacancy page exposes the account signal.  Both reads are in this
            # one authenticated context and each owns the signal it renders.
            vacancy_identity = await adapter.live_identity_snapshot()
            identity = observed_state(str(binding["hh_identity"]), vacancy_identity.get("account_identity"))
            resume = profile_resume
            required = [str(value) for value in capabilities.get("question_keys", [])]
            answers_known = not required or all(str(item.question_answers.get(question) or "").strip() for question in required)
            clear = reconciliation in {"vacancy_ready", "clear"} and capabilities.get("vacancy_id") == item.vacancy_id and identity == MATCH and resume == MATCH and answers_known and not capabilities.get("unknown_modal")
            preflight_id = f"preflight:{uuid.uuid4()}"
            evidence = {"reconciliation": reconciliation, "capabilities": capabilities, "identity": vacancy_identity, "resume_source": profile_snapshot, "identity_source_url": page.url, "resume_source_url": "https://hh.ru/applicant/resumes"}
            observation_id = record_observation(
                database,
                surface=str(reconciliation).upper(),
                url=page.url,
                fingerprint=sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                evidence=evidence,
            )
            evidence["ui_observation_id"] = observation_id
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("INSERT INTO account_binding_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"identity:{uuid.uuid4()}", candidate_id, binding["binding_id"], vacancy_identity.get("account_identity"), selected_profile_resume.get("id") if selected_profile_resume else str(binding["resume_id"]), selected_profile_resume.get("url") if selected_profile_resume else str(binding["resume_id"]), selected_profile_resume.get("title") if selected_profile_resume else None, identity, resume, json.dumps(evidence, ensure_ascii=False), _now()))
                db.execute("INSERT INTO application_preflights VALUES(?,?,?,?,?,?,?,?,?,?)", (preflight_id, candidate_id, binding["binding_id"], item.vacancy_id, item.decision_id, item.resume_id, "clear" if clear else "blocked", "direct persistent preflight" if clear else "direct persistent preflight blocked", json.dumps(evidence, ensure_ascii=False), _now()))
            record_lifecycle(
                database, candidate_id=candidate_id, vacancy_id=item.vacancy_id,
                state="READY_TO_APPLY" if clear else "NEEDS_RECONCILIATION",
                decision_id=item.decision_id, live_revision=item.source_revision,
                note="direct persistent preflight clear" if clear else "direct persistent preflight blocked",
            )
            rows.append({"vacancy_id": item.vacancy_id, "preflight_id": preflight_id, "preflight": "clear" if clear else "blocked"})
        except Exception as exc:  # noqa: BLE001 - any read failure must become a durable safe-resume incident.
            incident_id = await _capture_unknown(database, page, vacancy_id=item.vacancy_id, detail=f"direct preflight: {type(exc).__name__}: {exc}")
            rows.append({"vacancy_id": item.vacancy_id, "preflight": "needs_map", "incident_id": incident_id})
        finally:
            await page.close()
    return rows


async def execute_direct_authoritative_batch(root: Path, *, limit: int = 10) -> ApplicationBatchReport:
    """Only direct-persistent application entrypoint. Queue comes solely from SQLite."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be 1..10")
    database = root / "var" / "autohunt.sqlite"
    started = monotonic()
    planned = build_authoritative_application_queue(database)[:limit]
    batch_id = f"direct-applications:{uuid.uuid4()}"
    if not planned:
        return ApplicationBatchReport(batch_id=batch_id, planned=0, applied=[], stopped=None, elapsed_seconds=0.0)
    _checkpoint(database, stage="planned", batch_id=batch_id, payload={"vacancy_ids": [item.vacancy_id for item in planned]})
    async with async_production_browser_context(root) as context:
        chat_start = await sync_direct_chats(database, context, run_id=batch_id)
        _checkpoint(database, stage="chat_start_complete", batch_id=batch_id, payload={"chat": chat_start})
        preflights = await preflight_direct_context(root, context, planned)
        _checkpoint(database, stage="preflight_complete", batch_id=batch_id, payload={"preflights": preflights})
        clear = {str(row["vacancy_id"]): row for row in preflights if row["preflight"] == "clear"}
        selected = [item.model_copy(update={"preflight_id": str(clear[item.vacancy_id]["preflight_id"])}) for item in planned if item.vacancy_id in clear]
        if not selected:
            chat_end = await sync_direct_chats(database, context, run_id=batch_id)
            _checkpoint(database, stage="chat_end_complete", batch_id=batch_id, payload={"chat": chat_end})
            return ApplicationBatchReport(batch_id=batch_id, planned=len(planned), applied=[], stopped="no clear direct preflight", elapsed_seconds=round(monotonic() - started, 2))
        pages: list[Page] = []

        async def browser_for(request: ApplicationRequest) -> HHApplicationAdapter:
            item = next(value for value in selected if value.vacancy_id == request.vacancy_id)
            page = await _open(context, item.vacancy_url)
            pages.append(page)
            binding = (bootstrap(database).get("account_binding") or {})
            return HHApplicationAdapter(
                page, vacancy_id=request.vacancy_id, resume_id=request.resume_id,
                resume_title=str(binding.get("resume_title") or ""),
            )

        try:
            gateway = ApplicationGateway(database, _NoSyncBrowser(), production=True)
            outcomes: list[str] = []
            applied: list[str] = []
            stopped: str | None = None
            # A confirmed primary response is not an ambiguous failure.  Route
            # its cover immediately, then continue with the next prepared
            # vacancy in this same persistent Chrome context.  Only a genuine
            # unknown/blocked primary mutation opens the circuit.
            for item in selected:
                try:
                    adapter = await browser_for(item)
                    outcome = await gateway.apply_to_vacancy_async(item, adapter)
                except (ApplicationCircuitOpen, RuntimeError) as exc:
                    stopped = f"{type(exc).__name__}: {exc}"
                    outcomes.append(f"blocked:{stopped}")
                    break
                if outcome == "application_confirmed_cover_pending":
                    cover_outcome = await _attach_vacancy_page_cover(database, context, item)
                    if cover_outcome == "cover_pending":
                        cover_outcome = await _attach_chatik_cover(database, context, item)
                    outcome = f"{outcome}:{cover_outcome}"
                outcomes.append(outcome)
                if outcome.startswith(("applied", "application_confirmed_cover_pending")):
                    applied.append(item.vacancy_id)
                record_lifecycle(
                    database, candidate_id=str(bootstrap(database)["candidate_id"]),
                    vacancy_id=item.vacancy_id,
                    state=("COVER_CONFIRMED" if outcome.endswith(":cover_confirmed") else "APPLICATION_CONFIRMED") if outcome.startswith(("applied", "application_confirmed_cover_pending")) else "NEEDS_RECONCILIATION",
                    decision_id=item.decision_id, live_revision=item.source_revision,
                    note="HH application and vacancy-page cover confirmed" if outcome.endswith(":cover_confirmed") else ("HH application reconciliation confirmed; cover pending" if outcome.startswith("application_confirmed_cover_pending") else ("HH application reconciliation confirmed" if outcome == "applied" else f"application outcome: {outcome}")),
                )
            _checkpoint(database, stage="applications_reconciled", batch_id=batch_id, payload={"applied": applied, "outcomes": outcomes})
            return ApplicationBatchReport(batch_id=batch_id, planned=len(planned), applied=applied, stopped=stopped, elapsed_seconds=round(monotonic() - started, 2))
        except (ApplicationCircuitOpen, RuntimeError) as exc:
            stopped = f"{type(exc).__name__}: {exc}"
            _checkpoint(database, stage="stopped", batch_id=batch_id, payload={"detail": stopped})
            return ApplicationBatchReport(batch_id=batch_id, planned=len(planned), applied=[], stopped=stopped, elapsed_seconds=round(monotonic() - started, 2))
        finally:
            for page in pages:
                await page.close()
            chat_end = await sync_direct_chats(database, context, run_id=batch_id)
            _checkpoint(database, stage="chat_end_complete", batch_id=batch_id, payload={"chat": chat_end})


class _NoSyncBrowser:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"sync browser API forbidden in direct batch: {name}")
