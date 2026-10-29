"""Candidate-scoped chat triage. Reply text remains a durable prepared action."""

from __future__ import annotations

from contextlib import closing

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .context import bootstrap
from .gateway import (
    ChatReplyGateway,
    ChatReplyRequest,
    CoverAttachmentGateway,
    CoverAttachmentRequest,
)
from .hh_chat_adapter import HHChatAdapter
from .live_adapter import ReadOnlyHHSession
from .production_browser import async_production_browser_context
from .storage import migrate
from .sync import RegistrySync


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _default_external_deadline(received_at: str | None) -> str | None:
    """Two days only from an HH message timestamp, never observation time."""
    if not received_at:
        return None
    try:
        point = datetime.fromisoformat(received_at)
    except ValueError:
        return None
    if point.tzinfo is None:
        return None
    return (point.astimezone(UTC) + timedelta(hours=48)).isoformat()


def _concrete_interview_request(text: str) -> bool:
    """Escalate a proposed/requested slot, not a generic future-stage mention."""
    lower = text.lower()
    if not re.search(r"(собеседован|созвон|интервью|скрининг)", lower):
        return False
    return bool(re.search(
        r"(\b\d{1,2}:\d{2}\b|\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|"
        r"\b(сегодня|завтра|послезавтра|понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\b|"
        r"(предлагаем|можем|удобно|выберите|подтвердите).{0,60}(время|слот|созвон|интервью|скрининг))",
        lower,
    ))


def triage_changed_chats(database: Path, chat_ids: list[str], *, run_id: str) -> dict[str, int]:
    """Classify new business state; never send a reply from a reader phase."""
    migrate(database)
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    binding = context.get("account_binding") or {}
    if not candidate_id or not binding:
        raise RuntimeError("active candidate/account binding required")
    stats = {"refusals": 0, "waiting": 0, "reply_candidates": 0, "interviews": 0, "external_items": 0, "user_questions": 0}
    sync = RegistrySync(database, candidate_id=candidate_id)
    with closing(sqlite3.connect(database)) as db, db:
        for chat_id in chat_ids:
            link = db.execute("SELECT vacancy_id FROM candidate_chat_vacancy_links WHERE candidate_id=? AND chat_id=?", (candidate_id, chat_id)).fetchone()
            vacancy_id = str(link[0]) if link and link[0] else None
            rows = db.execute("SELECT direction,text,received_at FROM candidate_messages WHERE candidate_id=? AND chat_id=? ORDER BY rowid DESC LIMIT 20", (candidate_id, chat_id)).fetchall()
            bot_thread = any("робот-рекрутер" in str(row[1]).lower() for row in rows)
            incoming = next(
                (
                    (str(row[1]), str(row[2]) if row[2] else None) for row in rows
                    if (
                        row[0] in {"incoming", "system"}
                        or (row[0] == "unknown" and bot_thread and ("?" in str(row[1]) or "англий" in str(row[1]).lower()))
                    )
                    and "Отклик на вакансию" not in str(row[1])
                ),
                "",
            )
            if not incoming:
                continue
            incoming, received_at = incoming
            text = incoming.lower()
            if re.search(r"(отказ|не готовы|не подойд|к сожалению)", text):
                stats["refusals"] += 1
                if vacancy_id:
                    db.execute("INSERT INTO candidate_negotiations(candidate_id,vacancy_id,status,source_url,observed_at) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,vacancy_id) DO UPDATE SET status=excluded.status,observed_at=excluded.observed_at", (candidate_id, vacancy_id, "closed_refusal", "chatik", _now()))
                continue
            if re.search(r"(рассмотрим|вернемся|ожидайте)", text):
                stats["waiting"] += 1
                continue
            # Robot completion summaries frequently quote all previous
            # questions and answers.  They are terminal observations, not a
            # new employer question merely because the quoted history has '?'.
            if re.search(r"(спасибо за удел[её]нное время|все ваши ответы.*записал|в разговоре с кандидатом я узнал|вопросов к работодателю.*не было)", text):
                stats["waiting"] += 1
                db.execute(
                    "UPDATE candidate_work_items SET state='resolved',updated_at=? WHERE candidate_id=? AND item_id=? AND kind='chat_reply' AND state IN ('pending','prepared','needs_user')",
                    (_now(), candidate_id, f"chat-reply:{chat_id}"),
                )
                continue
            urls = re.findall(r"https?://[^\s<>]+", incoming)
            for url in urls:
                if "hh.ru" not in url:
                    kind = "test" if re.search(r"test|тест", url, re.IGNORECASE) else "form"
                    deadline = _default_external_deadline(received_at)
                    sync.record_external_item(
                        f"chat:{chat_id}:{hash(url)}", vacancy_id, url, kind, "discovered",
                        received_at=received_at, deadline_at=deadline,
                        deadline_source="default_48h_from_hh_message" if deadline else None,
                    )
                    stats["external_items"] += 1
            if _concrete_interview_request(incoming):
                item_id = f"chat-interview:{chat_id}"
                payload = json.dumps({"chat_id": chat_id, "vacancy_id": vacancy_id, "text": incoming, "action": "escalate_to_candidate"}, ensure_ascii=False)
                db.execute("INSERT INTO candidate_work_items(candidate_id,item_id,run_id,kind,vacancy_id,state,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?) ON CONFLICT(candidate_id,item_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at", (candidate_id, item_id, run_id, "interview", vacancy_id, payload, _now(), _now()))
                stats["interviews"] += 1
                continue
            if "?" in incoming or re.search(r"(подскажите|уточните|вопрос)", text) or (bot_thread and "англий" in text):
                item_id = f"chat-reply:{chat_id}"
                payload = json.dumps({"chat_id": chat_id, "vacancy_id": vacancy_id, "incoming": incoming, "action": "semantic_reply_required"}, ensure_ascii=False)
                db.execute("INSERT INTO candidate_work_items(candidate_id,item_id,run_id,kind,vacancy_id,state,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?) ON CONFLICT(candidate_id,item_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at", (candidate_id, item_id, run_id, "chat_reply", vacancy_id, payload, _now(), _now()))
                stats["reply_candidates"] += 1
    return stats


def prepare_known_chat_replies(database: Path, chat_ids: list[str]) -> int:
    """Prepare only replies supported verbatim by CandidateProfile facts."""
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    profile = dict(context.get("candidate_profile") or {})
    if not candidate_id:
        return 0
    prepared = 0
    with closing(sqlite3.connect(database)) as db, db:
        for chat_id in chat_ids:
            row = db.execute(
                "SELECT item_id,payload_json FROM candidate_work_items WHERE candidate_id=? AND item_id=? AND kind='chat_reply' AND state IN ('pending','needs_user')",
                (candidate_id, f"chat-reply:{chat_id}"),
            ).fetchone()
            if not row:
                continue
            payload = json.loads(str(row[1]))
            question = str(payload.get("incoming") or "").lower()
            text: str | None = None
            if "англий" in question and profile.get("english"):
                text = "В1-В2"
            if text is None:
                continue
            payload["expected_text"] = text
            payload["reply_basis"] = "CandidateProfile confirmed fact"
            db.execute(
                "UPDATE candidate_work_items SET state='prepared',payload_json=?,updated_at=? WHERE candidate_id=? AND item_id=?",
                (json.dumps(payload, ensure_ascii=False), _now(), candidate_id, str(row[0])),
            )
            prepared += 1
    return prepared


def mark_chat_ui_needs_user(database: Path, *, chat_id: str, vacancy_id: str, detail: str) -> None:
    """A real HH bot question without any response control is user-visible work."""
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    if not candidate_id:
        raise RuntimeError("active candidate required")
    now = _now()
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "UPDATE candidate_chat_vacancy_links SET vacancy_id=?,link_status='linked',detail=? WHERE candidate_id=? AND chat_id=?",
            (vacancy_id, "exact vacancy title matched in live chat; " + detail, candidate_id, chat_id),
        )
        db.execute(
            "UPDATE candidate_work_items SET state='needs_user',updated_at=? WHERE candidate_id=? AND item_id=?",
            (now, candidate_id, f"chat-reply:{chat_id}"),
        )
        db.execute(
            "INSERT OR REPLACE INTO candidate_user_questions(candidate_id,id,vacancy_id,question,status,created_at) VALUES(?,?,?,?,?,?)",
            (candidate_id, f"chat-ui:{chat_id}", vacancy_id, "HH Robot-рекрутер спросил уровень английского, но в текущем чате не показал поле или кнопку для ответа. Откройте этот чат в HH и продолжите анкету вручную.", "open", now),
        )


def persist_prepared_reply(database: Path, *, chat_id: str, exact_text: str) -> None:
    """Semantic operator resolves one pending reply; no browser data is accepted."""
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    if not candidate_id or not exact_text.strip():
        raise RuntimeError("active candidate and nonblank exact reply required")
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute("SELECT item_id,payload_json FROM candidate_work_items WHERE candidate_id=? AND item_id=? AND kind='chat_reply' AND state='pending'", (candidate_id, f"chat-reply:{chat_id}")).fetchone()
        if not row:
            raise RuntimeError("pending chat reply not found")
        payload = json.loads(row[1])
        payload["expected_text"] = exact_text
        db.execute("UPDATE candidate_work_items SET state='prepared',payload_json=?,updated_at=? WHERE candidate_id=? AND item_id=?", (json.dumps(payload, ensure_ascii=False), _now(), candidate_id, row[0]))


async def execute_authoritative_chat_replies(root: Path, *, cdp_endpoint: str, limit: int = 1) -> list[str]:
    """System-built chat reply queue. No externally supplied URL/text reaches UI."""
    database = root / "var" / "autohunt.sqlite"
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    if not candidate_id:
        raise RuntimeError("active candidate required")
    with closing(sqlite3.connect(database)) as db, db:
        rows = db.execute("SELECT item_id,vacancy_id,payload_json FROM candidate_work_items WHERE candidate_id=? AND kind='chat_reply' AND state='prepared' ORDER BY updated_at LIMIT ?", (candidate_id, limit)).fetchall()
        chats = {str(row[0]): str(row[1]) for row in db.execute("SELECT chat_id,url FROM candidate_chats WHERE candidate_id=?", (candidate_id,))}
    session = ReadOnlyHHSession(root / "var" / "profile", root / "var" / "controller.lock")
    outcomes: list[str] = []
    try:
        await session.attach_existing_cdp(cdp_endpoint)
        gateway = ChatReplyGateway(database)
        for item_id, _, raw in rows:
            payload = json.loads(raw)
            chat_id = str(payload["chat_id"])
            if chat_id not in chats or "expected_text" not in payload:
                raise RuntimeError("authoritative prepared chat reply is incomplete")
            page = await session.open_hh_page(chats[chat_id])
            try:
                expected_text = str(payload["expected_text"])
                option_label = expected_text if expected_text in {"А1-А2", "В1-В2", "С1-С2"} else None
                outcome = await gateway.send(ChatReplyRequest(chat_id=chat_id, expected_text=expected_text, option_label=option_label), HHChatAdapter(page))
                outcomes.append(outcome)
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("UPDATE candidate_work_items SET state=?,updated_at=? WHERE candidate_id=? AND item_id=?", ("sent" if outcome == "sent" else "needs_reconciliation", _now(), candidate_id, item_id))
                    if outcome == "sent":
                        db.execute("UPDATE candidate_user_questions SET status='resolved' WHERE candidate_id=? AND id=?", (candidate_id, f"chat-ui:{chat_id}"))
            finally:
                await page.close()
    finally:
        await session.stop()
    return outcomes


async def execute_direct_authoritative_chat_replies(root: Path, *, limit: int = 10, chat_id: str | None = None) -> list[str]:
    """Persistent-profile replacement for CDP chat execution.

    Queue and text come only from prepared candidate work items. One context is
    shared by all replies; each rendered outgoing message remains Gateway proof.
    """
    if not 1 <= limit <= 10:
        raise ValueError("limit must be 1..10")
    database = root / "var" / "autohunt.sqlite"
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    if not candidate_id:
        raise RuntimeError("active candidate required")
    with closing(sqlite3.connect(database)) as db, db:
        if chat_id:
            rows = db.execute(
                "SELECT item_id,payload_json FROM candidate_work_items WHERE candidate_id=? AND item_id=? AND kind='chat_reply' AND state='prepared'",
                (candidate_id, f"chat-reply:{chat_id}"),
            ).fetchall()
        else:
            rows = db.execute("SELECT item_id,payload_json FROM candidate_work_items WHERE candidate_id=? AND kind='chat_reply' AND state='prepared' ORDER BY updated_at LIMIT ?", (candidate_id, limit)).fetchall()
        chats = {str(chat_id): str(url) for chat_id, url in db.execute("SELECT chat_id,url FROM candidate_chats WHERE candidate_id=?", (candidate_id,))}
    outcomes: list[str] = []
    async with async_production_browser_context(root) as browser_context:
        gateway = ChatReplyGateway(database)
        for item_id, raw in rows:
            payload = json.loads(raw)
            chat_id = str(payload.get("chat_id") or "")
            expected = str(payload.get("expected_text") or "")
            if not chat_id or not expected or chat_id not in chats:
                raise RuntimeError("authoritative prepared chat reply is incomplete")
            page = await browser_context.new_page()
            try:
                await page.goto(chats[chat_id], wait_until="domcontentloaded")
                await page.wait_for_timeout(800)
                option = expected if expected in {"А1-А2", "В1-В2", "С1-С2"} else None
                outcome = await gateway.send(ChatReplyRequest(chat_id=chat_id, expected_text=expected, option_label=option), HHChatAdapter(page))
                outcomes.append(outcome)
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("UPDATE candidate_work_items SET state=?,updated_at=? WHERE candidate_id=? AND item_id=?", ("sent" if outcome == "sent" else "needs_reconciliation", _now(), candidate_id, item_id))
            finally:
                await page.close()
    return outcomes


async def inspect_authoritative_cover_attachment(root: Path, *, cdp_endpoint: str, vacancy_id: str) -> dict[str, object]:
    """Open only HH's already-offered post-application cover form.

    Cover text and chat route come from durable state.  This intentionally does
    not send anything: caller must see the exact HH form before a one-time
    cover submit is implemented against its real controls.
    """
    database = root / "var" / "autohunt.sqlite"
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    if not candidate_id:
        raise RuntimeError("active candidate required")
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute(
            "SELECT p.state,d.cover_letter,c.url FROM candidate_application_processes p "
            "JOIN candidate_vacancy_decisions d ON d.candidate_id=p.candidate_id AND d.decision_id=p.decision_id "
            "JOIN candidate_chat_vacancy_links l ON l.candidate_id=p.candidate_id AND l.vacancy_id=p.vacancy_id "
            "JOIN candidate_chats c ON c.candidate_id=l.candidate_id AND c.chat_id=l.chat_id "
            "WHERE p.candidate_id=? AND p.vacancy_id=? AND d.decision='apply' "
            "ORDER BY c.updated_at DESC LIMIT 1",
            (candidate_id, vacancy_id),
        ).fetchone()
    if not row:
        raise RuntimeError("authoritative cover/chat state missing")
    state, cover, chat_url = map(str, row)
    if state not in {"application_confirmed_cover_pending", "application_confirmed_payload_unknown", "confirmed"} or not cover:
        raise RuntimeError("primary application is not in a cover-attachable state")
    session = ReadOnlyHHSession(root / "var" / "profile", root / "var" / "controller.lock")
    try:
        await session.attach_existing_cdp(cdp_endpoint)
        page = await session.open_hh_page(chat_url)
        adapter = HHChatAdapter(page)
        await adapter.open_cover_attachment()
        surface = await adapter.cover_surface()
        with closing(sqlite3.connect(database)) as db, db:
            db.execute(
                "INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (f"event:cover-surface:{chat_url}", candidate_id, str(context["account_binding"]["binding_id"]), None, "cover_attachment_form_opened", json.dumps({"vacancy_id": vacancy_id, "chat_url": chat_url, "cover_sha256": hashlib.sha256(cover.encode()).hexdigest(), "surface": surface}, ensure_ascii=False), _now()),
            )
        return {"vacancy_id": vacancy_id, "cover": cover, **surface}
    finally:
        await session.stop()


async def execute_authoritative_cover_attachment(root: Path, *, cdp_endpoint: str, vacancy_id: str) -> str:
    """Attach exact persisted cover through HH's Chatik action exactly once."""
    database = root / "var" / "autohunt.sqlite"
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    binding = context.get("account_binding") or {}
    if not candidate_id or not binding:
        raise RuntimeError("active candidate/account binding required")
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute(
            "SELECT p.state,d.cover_letter,c.chat_id,c.url FROM candidate_application_processes p "
            "JOIN candidate_vacancy_decisions d ON d.candidate_id=p.candidate_id AND d.decision_id=p.decision_id "
            "JOIN candidate_chat_vacancy_links l ON l.candidate_id=p.candidate_id AND l.vacancy_id=p.vacancy_id "
            "JOIN candidate_chats c ON c.candidate_id=l.candidate_id AND c.chat_id=l.chat_id "
            "WHERE p.candidate_id=? AND p.vacancy_id=? AND d.decision='apply' "
            "ORDER BY c.updated_at DESC LIMIT 1",
            (candidate_id, vacancy_id),
        ).fetchone()
    if not row:
        raise RuntimeError("authoritative cover/chat state missing")
    state, cover, chat_id, chat_url = map(str, row)
    if state not in {"application_confirmed_cover_pending", "application_confirmed_payload_unknown", "confirmed"} or not cover:
        raise RuntimeError("primary application is not in a cover-attachable state")
    session = ReadOnlyHHSession(root / "var" / "profile", root / "var" / "controller.lock")
    try:
        await session.attach_existing_cdp(cdp_endpoint)
        page = await session.open_hh_page(chat_url)
        outcome = await CoverAttachmentGateway(database).attach(
            CoverAttachmentRequest(vacancy_id=vacancy_id, chat_id=chat_id, expected_text=cover),
            HHChatAdapter(page),
        )
        return outcome
    finally:
        await session.stop()


async def execute_direct_authoritative_cover_attachment(root: Path, *, vacancy_id: str) -> str:
    """Direct persistent replacement for post-application Chatik cover attach."""
    database = root / "var" / "autohunt.sqlite"
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    binding = context.get("account_binding") or {}
    if not candidate_id or not binding:
        raise RuntimeError("active candidate/account binding required")
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute(
            "SELECT p.state,d.cover_letter,c.chat_id,c.url FROM candidate_application_processes p "
            "JOIN candidate_vacancy_decisions d ON d.candidate_id=p.candidate_id AND d.decision_id=p.decision_id "
            "JOIN candidate_chat_vacancy_links l ON l.candidate_id=p.candidate_id AND l.vacancy_id=p.vacancy_id "
            "JOIN candidate_chats c ON c.candidate_id=l.candidate_id AND c.chat_id=l.chat_id "
            "WHERE p.candidate_id=? AND p.vacancy_id=? AND d.decision='apply' ORDER BY c.updated_at DESC LIMIT 1",
            (candidate_id, vacancy_id),
        ).fetchone()
    if not row:
        raise RuntimeError("authoritative cover/chat state missing")
    state, cover, chat_id, chat_url = map(str, row)
    if state not in {"application_confirmed_cover_pending", "application_confirmed_payload_unknown", "confirmed"} or not cover:
        raise RuntimeError("primary application is not in a cover-attachable state")
    async with async_production_browser_context(root) as browser_context:
        page = await browser_context.new_page()
        try:
            await page.goto(chat_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(800)
            outcome = await CoverAttachmentGateway(database).attach(
                CoverAttachmentRequest(vacancy_id=vacancy_id, chat_id=chat_id, expected_text=cover),
                HHChatAdapter(page),
            )
        finally:
            await page.close()
    return outcome
