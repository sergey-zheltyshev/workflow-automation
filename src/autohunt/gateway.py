"""Single durable boundary for every HH application mutation."""

from contextlib import closing
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .lifecycle import record_lifecycle
from .storage import migrate


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    kind: str = Field(pattern="^(application|cover_letter|chat_reply)$")
    target: dict[str, str] = Field(min_length=1)
    text: str | None = None


class ApplicationRequest(BaseModel):
    """Business input only. Browser mechanics stay behind ApplicationGateway."""

    model_config = ConfigDict(extra="forbid")

    vacancy_id: str = Field(pattern=r"^\d+$")
    # HH response pages expose selected resume label, not a stable public numeric ID.
    resume_id: str = Field(min_length=1)
    expected_cover_letter: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    # Minted only by authoritative read-only preflight. External JSON cannot
    # manufacture a usable production request without its SQLite row.
    preflight_id: str | None = None
    question_answers: dict[str, str] = Field(default_factory=dict)


class ChatReplyRequest(BaseModel):
    """Prepared exact reply. Chat id/text only; browser route is resolved from SQLite."""

    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1)
    expected_text: str = Field(min_length=1)
    option_label: str | None = None


class CoverAttachmentRequest(BaseModel):
    """Exact cover for an already confirmed HH application and linked chat."""

    model_config = ConfigDict(extra="forbid")
    vacancy_id: str = Field(pattern=r"^\d+$")
    chat_id: str = Field(min_length=1)
    expected_text: str = Field(min_length=1)


class ApplicationBrowser(Protocol):
    """Internal port. Production adapter owns selectors/submit/proof; caller never does."""

    def reconcile(self, vacancy_id: str, resume_id: str) -> str: ...
    def inspect(self, vacancy_id: str, resume_id: str) -> dict[str, object]: ...
    def fill_and_verify(self, expected: str) -> None: ...
    def submit_once(self) -> None: ...
    def prove_applied(self, vacancy_id: str, expected: str) -> bool: ...


class AsyncApplicationBrowser(Protocol):
    async def reconcile(self, vacancy_id: str, resume_id: str) -> str: ...
    async def inspect(self, vacancy_id: str, resume_id: str) -> dict[str, object]: ...
    async def confirm_country_application(self) -> None: ...
    async def open_application_form(self) -> None: ...
    async def fill_and_verify(self, expected: str) -> None: ...
    async def fill_questions_and_verify(self, answers: dict[str, str]) -> None: ...
    async def submit_once(self) -> None: ...
    async def prove_applied(self, vacancy_id: str, expected: str) -> bool: ...


class AsyncChatBrowser(Protocol):
    async def fill_and_verify(self, expected: str) -> None: ...
    async def send_once(self) -> None: ...
    async def choose_once(self, label: str) -> None: ...
    async def prove_sent(self, expected: str) -> bool: ...


class AsyncCoverAttachmentBrowser(Protocol):
    async def open_cover_attachment(self) -> None: ...
    async def fill_and_verify(self, expected: str) -> None: ...
    async def send_once(self) -> None: ...
    async def prove_cover_attached(self, expected: str) -> bool: ...


class ApplicationCircuitOpen(RuntimeError):
    """A batch must stop after any ambiguous or unsafe application transition."""


def _application_intent(request: ApplicationRequest) -> Intent:
    payload_hash = hashlib.sha256(request.expected_cover_letter.encode()).hexdigest()
    return Intent(
        id=f"apply-{request.vacancy_id}-{payload_hash[:12]}",
        idempotency_key=f"apply:{request.vacancy_id}:{request.resume_id}:{payload_hash}",
        kind="application",
        target={
            "vacancy_id": request.vacancy_id,
            "resume_id": request.resume_id,
            "decision_id": request.decision_id,
            "source_revision": request.source_revision,
        },
        text=request.expected_cover_letter,
    )


def _status(database: Path, intent_id: str) -> str:
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute("SELECT status FROM action_intents WHERE id=?", (intent_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"intent missing after prepare: {intent_id}")
    return str(row[0])


def _record(database: Path, intent_id: str, outcome: str, detail: str, status: str) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "INSERT INTO action_attempts(intent_id,outcome,detail,attempted_at) VALUES(?,?,?,?)",
            (intent_id, outcome, detail, _now()),
        )
        db.execute("UPDATE action_intents SET status=? WHERE id=?", (status, intent_id))


def _journal(database: Path, candidate_id: str, binding_id: str, kind: str, payload: dict[str, object], *, run_id: str | None = None) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (f"event:{uuid.uuid4()}", candidate_id, binding_id, run_id, kind, json.dumps(payload, ensure_ascii=False), _now()),
        )


class ApplicationGateway:
    """Reusable mutation boundary; only its browser port may submit an application."""

    def __init__(self, database: Path, browser: ApplicationBrowser, *, production: bool = False) -> None:
        migrate(database)
        self.database, self.browser, self.production = database, browser, production

    def _authorize(self, request: ApplicationRequest) -> tuple[str, str, str, str]:
        """Validate immutable SQLite decision and business key before any production intent."""
        with closing(sqlite3.connect(self.database)) as db, db:
            active = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
            if active is None:
                raise ApplicationCircuitOpen("active candidate missing")
            candidate_id = str(active[0])
            binding = db.execute("SELECT binding_id,resume_id FROM account_bindings WHERE candidate_id=? AND active=1", (candidate_id,)).fetchone()
            if binding is None or binding[1] != request.resume_id:
                raise ApplicationCircuitOpen("account binding or stable resume mismatch")
            profile = db.execute("SELECT version_id FROM candidate_profiles WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (candidate_id,)).fetchone()
            policy = db.execute("SELECT policy_version FROM job_policy_versions WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (candidate_id,)).fetchone()
            automation = db.execute("SELECT policy_json FROM automation_policy_versions WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (candidate_id,)).fetchone()
            if not automation or json.loads(automation[0]).get("hh_applications") != "enabled":
                raise ApplicationCircuitOpen("automation policy does not permit live applications")
            decision = db.execute(
                "SELECT cover_letter FROM candidate_vacancy_decisions WHERE candidate_id=? AND decision_id=? AND vacancy_id=? AND vacancy_revision=? AND decision='apply' AND candidate_profile_version=? AND job_policy_version=?",
                (candidate_id, request.decision_id, request.vacancy_id, request.source_revision, profile[0] if profile else None, policy[0] if policy else None),
            ).fetchone()
            gate = db.execute("SELECT eligibility FROM candidate_vacancy_state WHERE candidate_id=? AND vacancy_id=?", (candidate_id, request.vacancy_id)).fetchone()
            existing = db.execute("SELECT state FROM candidate_application_processes WHERE candidate_id=? AND vacancy_id=?", (candidate_id, request.vacancy_id)).fetchone()
            if not decision or decision[0] != request.expected_cover_letter or not gate or gate[0] != "eligible":
                raise ApplicationCircuitOpen("authoritative decision, cover, or eligibility mismatch")
            # A PREPARED process is executable only after the journal proved no
            # browser action was invoked (for example RT-006 CTA resolution).
            if existing and existing[0] not in {"application_confirmed_cover_pending", "prepared"}:
                raise ApplicationCircuitOpen("business idempotency blocked: candidate already has application process")
            if not request.preflight_id:
                raise ApplicationCircuitOpen("mandatory authoritative preflight missing")
            preflight = db.execute(
                "SELECT status,resume_id FROM application_preflights WHERE preflight_id=? AND candidate_id=? AND binding_id=? AND vacancy_id=? AND decision_id=?",
                (request.preflight_id, candidate_id, binding[0], request.vacancy_id, request.decision_id),
            ).fetchone()
            if not preflight or preflight[0] != "clear" or preflight[1] != request.resume_id:
                raise ApplicationCircuitOpen("mandatory live preflight failed or stale")
            observation = db.execute(
                "SELECT identity_status,resume_status FROM account_binding_observations WHERE candidate_id=? AND binding_id=? ORDER BY observed_at DESC LIMIT 1",
                (candidate_id, binding[0]),
            ).fetchone()
            if not observation or observation[0] != "MATCH" or observation[1] != "MATCH":
                raise ApplicationCircuitOpen("live account or stable resume identity is not verified")
            identity = db.execute("SELECT hh_identity FROM account_bindings WHERE binding_id=?", (binding[0],)).fetchone()[0]
            return candidate_id, str(binding[0]), hashlib.sha256(request.expected_cover_letter.encode()).hexdigest(), str(identity)

    def _process(self, candidate_id: str, binding_id: str, request: ApplicationRequest, cover_hash: str, state: str, detail: str) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO candidate_application_processes(candidate_id,vacancy_id,binding_id,decision_id,resume_id,cover_sha256,state,detail,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(candidate_id,vacancy_id) DO UPDATE SET state=excluded.state,detail=excluded.detail,updated_at=excluded.updated_at",
                (candidate_id, request.vacancy_id, binding_id, request.decision_id, request.resume_id, cover_hash, state, detail, _now()),
            )

    def apply_to_vacancy(self, request: ApplicationRequest) -> str:
        intent_id = DryRunGateway(self.database).prepare(_application_intent(request))
        status = _status(self.database, intent_id)
        if status != "prepared":
            return status
        state = self.browser.reconcile(request.vacancy_id, request.resume_id)
        if state == "already_applied":
            raise RuntimeError("already applied; submit blocked")
        if state != "clear":
            raise RuntimeError(f"reconciliation blocked: {state}")
        capabilities = self.browser.inspect(request.vacancy_id, request.resume_id)
        if capabilities.get("vacancy_id") != request.vacancy_id or capabilities.get("resume_id") != request.resume_id:
            raise RuntimeError("target or resume mismatch")
        if capabilities.get("required_questions"):
            raise RuntimeError("unsupported required employer questions")
        self.browser.fill_and_verify(request.expected_cover_letter)
        self.browser.submit_once()
        if not self.browser.prove_applied(request.vacancy_id, request.expected_cover_letter):
            _record(self.database, intent_id, "reconciliation", "post-submit proof absent", "needs_reconciliation")
            return "needs_reconciliation"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO action_attempts(intent_id,outcome,detail,attempted_at) VALUES(?,?,?,?)", (intent_id, "applied", "proof_rule=true", _now()))
            db.execute("UPDATE action_intents SET status='applied' WHERE id=?", (intent_id,))
            db.execute("INSERT OR REPLACE INTO proofs(intent_id,rule,status,detail,verified_at) VALUES(?,?,?,?,?)", (intent_id, "live HH reread", "applied", "proof_rule=true", _now()))
        return "applied"


    async def apply_to_vacancy_async(self, request: ApplicationRequest, browser: AsyncApplicationBrowser) -> str:
        """Production lifecycle. Every potentially mutating click reconciles, even on error."""
        candidate_id = binding_id = cover_hash = expected_identity = ""
        if self.production:
            candidate_id, binding_id, cover_hash, expected_identity = self._authorize(request)
            self._process(candidate_id, binding_id, request, cover_hash, "prepared", "authoritative SQLite request")
            _journal(self.database, candidate_id, binding_id, "application_prepared", {"vacancy_id": request.vacancy_id, "decision_id": request.decision_id})
        intent_id = DryRunGateway(self.database).prepare(_application_intent(request))
        status = _status(self.database, intent_id)
        if status != "prepared":
            return status
        mutation_started = False
        async def reconcile_after_mutation(detail: str) -> str:
            _record(self.database, intent_id, "reconciliation", detail, "reconciling")
            if self.production:
                _journal(self.database, candidate_id, binding_id, "application_reconciling", {"vacancy_id": request.vacancy_id, "detail": detail})
            state = await browser.reconcile(request.vacancy_id, request.resume_id)
            if state == "already_applied":
                _record(self.database, intent_id, "application_confirmed", detail, "application_confirmed_cover_pending")
                if self.production:
                    self._process(candidate_id, binding_id, request, cover_hash, "application_confirmed_cover_pending", detail)
                    _journal(self.database, candidate_id, binding_id, "application_confirmed_cover_pending", {"vacancy_id": request.vacancy_id, "detail": detail})
                return "application_confirmed_cover_pending"
            _record(self.database, intent_id, "reconciliation", f"{detail}; state={state}", "needs_reconciliation")
            if self.production:
                _journal(self.database, candidate_id, binding_id, "application_reconciliation_required", {"vacancy_id": request.vacancy_id, "state": state, "detail": detail})
            return "needs_reconciliation"
        try:
            reconciliation = await browser.reconcile(request.vacancy_id, request.resume_id)
            if reconciliation == "vacancy_ready":
                caps = await browser.inspect(request.vacancy_id, request.resume_id)
                if not caps.get("apply_button_available"):
                    raise ApplicationCircuitOpen("vacancy apply control unavailable")
                _record(self.database, intent_id, "mutation_in_flight", "open application form before-click", "in_flight")
                if self.production:
                    self._process(candidate_id, binding_id, request, cover_hash, "in_flight", "open application form before-click")
                    _journal(self.database, candidate_id, binding_id, "application_in_flight", {"vacancy_id": request.vacancy_id, "kind": "open_form"})
                mutation_started = True
                await browser.open_application_form()
                reconciliation = await browser.reconcile(request.vacancy_id, request.resume_id)
                if reconciliation == "already_applied":
                    return await reconcile_after_mutation("application opening click created primary application")
            if reconciliation != "clear":
                raise ApplicationCircuitOpen(f"reconciliation blocked: {reconciliation}")
            caps = await browser.inspect(request.vacancy_id, request.resume_id)
            if self.production and caps.get("account_identity") != expected_identity:
                raise ApplicationCircuitOpen("live HH account identity mismatch")
            if caps.get("country_confirmation_required"):
                _record(self.database, intent_id, "mutation_in_flight", "country confirmation before-click", "in_flight")
                if self.production:
                    self._process(candidate_id, binding_id, request, cover_hash, "in_flight", "country confirmation before-click")
                    _journal(self.database, candidate_id, binding_id, "application_in_flight", {"vacancy_id": request.vacancy_id, "kind": "country_confirmation"})
                mutation_started = True
                await browser.confirm_country_application()
                _record(self.database, intent_id, "reconciliation", "country confirmation after-click", "reconciling")
                country_state = await browser.reconcile(request.vacancy_id, request.resume_id)
                if self.production:
                    _journal(self.database, candidate_id, binding_id, "application_reconciling", {"vacancy_id": request.vacancy_id, "detail": "country confirmation after-click"})
                if country_state == "already_applied":
                    return await reconcile_after_mutation("country confirmation created primary application")
                if country_state != "clear":
                    _record(self.database, intent_id, "reconciliation", f"country confirmation after-click; state={country_state}", "needs_reconciliation")
                    if self.production:
                        _journal(self.database, candidate_id, binding_id, "application_reconciliation_required", {"vacancy_id": request.vacancy_id, "state": country_state, "detail": "country confirmation after-click"})
                    raise ApplicationCircuitOpen("country confirmation uncertain; manual reconciliation required")
                _record(self.database, intent_id, "reconciliation", "country confirmation after-click; state=clear", "in_flight")
                if self.production:
                    _journal(self.database, candidate_id, binding_id, "application_reconciled_clear", {"vacancy_id": request.vacancy_id, "kind": "country_confirmation"})
                reconciliation = "clear"
            # no branch may reach text/submit without re-reading target after any prior UI transition
            if mutation_started:
                caps = await browser.inspect(request.vacancy_id, request.resume_id)
            if caps.get("vacancy_id") != request.vacancy_id or caps.get("resume_id") != request.resume_id:
                if self.production:
                    _journal(self.database, candidate_id, binding_id, "application_capability_blocked", {"vacancy_id": request.vacancy_id, "reason": "target_or_resume_mismatch", "capabilities": caps})
                raise ApplicationCircuitOpen("target or resume mismatch")
            if caps.get("unknown_modal"):
                if self.production:
                    _journal(self.database, candidate_id, binding_id, "unknown_pre_submit_modal", {"vacancy_id": request.vacancy_id, "capabilities": caps})
                raise ApplicationCircuitOpen("unknown pre-submit modal")
            required_questions = caps.get("required_questions")
            if caps.get("already_applied") or (required_questions and not request.question_answers):
                raise ApplicationCircuitOpen("application capability blocked")
            if request.question_answers:
                await browser.fill_questions_and_verify(request.question_answers)
            await browser.fill_and_verify(request.expected_cover_letter)
            _record(self.database, intent_id, "primary_cover_verified", "exact primary-form cover readback before submit", "prepared")
            if self.production:
                _journal(self.database, candidate_id, binding_id, "primary_cover_verified", {"vacancy_id": request.vacancy_id, "cover_sha256": cover_hash})
            _record(self.database, intent_id, "mutation_in_flight", "application submit before-click", "in_flight")
            if self.production:
                self._process(candidate_id, binding_id, request, cover_hash, "in_flight", "application submit before-click")
                _journal(self.database, candidate_id, binding_id, "application_in_flight", {"vacancy_id": request.vacancy_id, "kind": "submit"})
            mutation_started = True
            await browser.submit_once()
            if not await browser.prove_applied(request.vacancy_id, request.expected_cover_letter):
                return await reconcile_after_mutation("post-submit proof absent")
        except Exception as exc:
            if mutation_started:
                try:
                    await reconcile_after_mutation(f"exception after possible mutation: {type(exc).__name__}: {exc}")
                except Exception as reconciliation_error:  # noqa: BLE001 - preserve uncertainty on any failed reread
                    _record(self.database, intent_id, "reconciliation_error", f"{type(reconciliation_error).__name__}: {reconciliation_error}", "needs_manual_reconciliation")
                    if self.production:
                        _journal(self.database, candidate_id, binding_id, "application_reconciliation_failed", {"vacancy_id": request.vacancy_id, "error": str(reconciliation_error)})
            else:
                _record(self.database, intent_id, "blocked", f"{type(exc).__name__}: {exc}", "blocked")
            raise
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO action_attempts(intent_id,outcome,detail,attempted_at) VALUES(?,?,?,?)", (intent_id, "applied", "proof_rule=true", _now()))
            db.execute("UPDATE action_intents SET status='applied' WHERE id=?", (intent_id,))
            db.execute("INSERT OR REPLACE INTO proofs(intent_id,rule,status,detail,verified_at) VALUES(?,?,?,?,?)", (intent_id,"live HH reread","applied","proof_rule=true",_now()))
        if self.production:
            self._process(candidate_id, binding_id, request, cover_hash, "confirmed", "HH reconciliation and proof")
            _journal(self.database, candidate_id, binding_id, "application_confirmed", {"vacancy_id": request.vacancy_id, "cover_sha256": cover_hash})
        return "applied"


class ChatReplyGateway:
    """Separate safe mutation boundary for HH chat replies."""

    def __init__(self, database: Path) -> None:
        migrate(database)
        self.database = database

    async def send(self, request: ChatReplyRequest, browser: AsyncChatBrowser) -> str:
        with closing(sqlite3.connect(self.database)) as db, db:
            active = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
            if not active:
                raise ApplicationCircuitOpen("active candidate missing")
            candidate_id = str(active[0])
            binding = db.execute("SELECT binding_id FROM account_bindings WHERE candidate_id=? AND active=1", (candidate_id,)).fetchone()
            policy = db.execute("SELECT policy_json FROM automation_policy_versions WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (candidate_id,)).fetchone()
            chat = db.execute("SELECT 1 FROM candidate_chats WHERE candidate_id=? AND chat_id=?", (candidate_id, request.chat_id)).fetchone()
            if not binding or not policy or json.loads(policy[0]).get("hh_replies") != "enabled" or not chat:
                raise ApplicationCircuitOpen("chat reply is not authorized by current candidate policy")
            binding_id = str(binding[0])
        digest = hashlib.sha256(request.expected_text.encode()).hexdigest()
        intent = Intent(id=f"chat-{request.chat_id}-{digest[:12]}", idempotency_key=f"chat:{candidate_id}:{request.chat_id}:{digest}", kind="chat_reply", target={"chat_id": request.chat_id}, text=request.expected_text)
        intent_id = DryRunGateway(self.database).prepare(intent)
        prior_status = _status(self.database, intent_id)
        recovered_preclick = False
        if prior_status == "blocked":
            # A selector/hydration miss happens before a real chat click.  It
            # is safe to resume this exact durable intent after adapter repair.
            with closing(sqlite3.connect(self.database)) as db, db:
                detail = db.execute("SELECT detail FROM action_attempts WHERE intent_id=? ORDER BY id DESC LIMIT 1", (intent_id,)).fetchone()
                if detail and "cardinality mismatch" in str(detail[0]):
                    db.execute("UPDATE action_intents SET status='prepared' WHERE id=?", (intent_id,))
                    prior_status = "prepared"
                    recovered_preclick = True
        if recovered_preclick:
            _journal(self.database, candidate_id, binding_id, "chat_reply_prepared", {"chat_id": request.chat_id, "reason": "no_mutation_occurred_before_ui_resolution"})
        if prior_status != "prepared":
            return prior_status
        _journal(self.database, candidate_id, binding_id, "chat_reply_prepared", {"chat_id": request.chat_id, "text_sha256": digest})
        mutation_started = False
        try:
            if request.option_label:
                if request.option_label != request.expected_text:
                    raise ApplicationCircuitOpen("chat option must equal authoritative expected text")
            else:
                await browser.fill_and_verify(request.expected_text)
            _record(self.database, intent_id, "mutation_in_flight", "chat send before-click", "in_flight")
            mutation_started = True
            if request.option_label:
                await browser.choose_once(request.option_label)
            else:
                await browser.send_once()
            if not await browser.prove_sent(request.expected_text):
                _record(self.database, intent_id, "reconciliation", "reply proof absent", "needs_reconciliation")
                _journal(self.database, candidate_id, binding_id, "chat_reply_reconciliation_required", {"chat_id": request.chat_id})
                return "needs_reconciliation"
        except Exception as exc:
            _record(self.database, intent_id, "reconciliation" if mutation_started else "blocked", f"{type(exc).__name__}: {exc}", "needs_reconciliation" if mutation_started else "blocked")
            _journal(self.database, candidate_id, binding_id, "chat_reply_reconciliation_required" if mutation_started else "chat_reply_blocked", {"chat_id": request.chat_id, "error": str(exc)})
            raise
        _record(self.database, intent_id, "sent", "HH reread confirms exact outgoing text", "sent")
        _journal(self.database, candidate_id, binding_id, "chat_reply_sent", {"chat_id": request.chat_id})
        return "sent"


class CoverAttachmentGateway:
    """Dedicated mutation boundary for HH's post-application cover action.

    It intentionally does not reuse ChatReplyGateway: a cover is authorized by
    a confirmed application, not the general chat-reply flag, and its proof
    includes the post-action cover surface state.
    """

    def __init__(self, database: Path) -> None:
        migrate(database)
        self.database = database

    def _authorize(self, request: CoverAttachmentRequest) -> tuple[str, str]:
        with closing(sqlite3.connect(self.database)) as db, db:
            active = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
            if not active:
                raise ApplicationCircuitOpen("active candidate missing")
            candidate_id = str(active[0])
            binding = db.execute("SELECT binding_id FROM account_bindings WHERE candidate_id=? AND active=1", (candidate_id,)).fetchone()
            automation = db.execute("SELECT policy_json FROM automation_policy_versions WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (candidate_id,)).fetchone()
            row = db.execute(
                "SELECT p.state,d.cover_letter FROM candidate_application_processes p "
                "JOIN candidate_vacancy_decisions d ON d.candidate_id=p.candidate_id AND d.decision_id=p.decision_id "
                "JOIN candidate_chat_vacancy_links l ON l.candidate_id=p.candidate_id AND l.vacancy_id=p.vacancy_id AND l.chat_id=? "
                "WHERE p.candidate_id=? AND p.vacancy_id=? AND d.decision='apply'",
                (request.chat_id, candidate_id, request.vacancy_id),
            ).fetchone()
            allowed = {"application_confirmed_cover_pending", "application_confirmed_payload_unknown", "confirmed"}
            if not binding or not automation or json.loads(automation[0]).get("hh_applications") != "enabled":
                raise ApplicationCircuitOpen("cover attachment is not authorized by current application policy")
            if not row or row[0] not in allowed or row[1] != request.expected_text:
                raise ApplicationCircuitOpen("authoritative primary application, chat link, or cover mismatch")
            return candidate_id, str(binding[0])

    async def attach(self, request: CoverAttachmentRequest, browser: AsyncCoverAttachmentBrowser) -> str:
        candidate_id, binding_id = self._authorize(request)
        digest = hashlib.sha256(request.expected_text.encode()).hexdigest()
        intent = Intent(
            id=f"cover-{request.vacancy_id}-{digest[:12]}",
            idempotency_key=f"cover:{candidate_id}:{request.vacancy_id}:{request.chat_id}:{digest}",
            kind="cover_letter", target={"vacancy_id": request.vacancy_id, "chat_id": request.chat_id}, text=request.expected_text,
        )
        intent_id = DryRunGateway(self.database).prepare(intent)
        if _status(self.database, intent_id) != "prepared":
            return _status(self.database, intent_id)
        mutation_started = False
        try:
            await browser.open_cover_attachment()
            await browser.fill_and_verify(request.expected_text)
            _record(self.database, intent_id, "mutation_in_flight", "cover attachment send before-click", "in_flight")
            mutation_started = True
            await browser.send_once()
            if not await browser.prove_cover_attached(request.expected_text):
                _record(self.database, intent_id, "reconciliation", "cover attachment proof absent", "needs_reconciliation")
                with closing(sqlite3.connect(self.database)) as db, db:
                    db.execute(
                        "UPDATE candidate_application_processes SET state='cover_needs_reconciliation',detail=?,updated_at=? WHERE candidate_id=? AND vacancy_id=?",
                        ("cover action clicked; rendered proof incomplete; do not retry", _now(), candidate_id, request.vacancy_id),
                    )
                record_lifecycle(
                    self.database, candidate_id=candidate_id, vacancy_id=request.vacancy_id,
                    state="NEEDS_RECONCILIATION", note="cover action clicked; exact attachment proof incomplete",
                )
                _journal(self.database, candidate_id, binding_id, "cover_attachment_reconciliation_required", {"vacancy_id": request.vacancy_id, "chat_id": request.chat_id})
                return "needs_reconciliation"
        except Exception as exc:
            _record(self.database, intent_id, "reconciliation" if mutation_started else "blocked", f"{type(exc).__name__}: {exc}", "needs_reconciliation" if mutation_started else "blocked")
            _journal(self.database, candidate_id, binding_id, "cover_attachment_reconciliation_required" if mutation_started else "cover_attachment_blocked", {"vacancy_id": request.vacancy_id, "chat_id": request.chat_id, "error": str(exc)})
            raise
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE candidate_application_processes SET state='confirmed',detail=?,updated_at=? WHERE candidate_id=? AND vacancy_id=?", ("HH post-application cover action reread confirms exact text", _now(), candidate_id, request.vacancy_id))
            db.execute("UPDATE action_intents SET status='applied' WHERE id=?", (intent_id,))
            db.execute("INSERT INTO action_attempts(intent_id,outcome,detail,attempted_at) VALUES(?,?,?,?)", (intent_id, "attached", "HH reread confirms exact cover action", _now()))
            db.execute("INSERT OR REPLACE INTO proofs(intent_id,rule,status,detail,verified_at) VALUES(?,?,?,?,?)", (intent_id, "HH post-application cover reread", "attached", "exact outgoing text and action consumed", _now()))
        record_lifecycle(
            self.database, candidate_id=candidate_id, vacancy_id=request.vacancy_id,
            state="COVER_CONFIRMED", note="HH post-application cover action reread confirms exact text",
        )
        _journal(self.database, candidate_id, binding_id, "cover_attached", {"vacancy_id": request.vacancy_id, "chat_id": request.chat_id, "cover_sha256": digest})
        return "attached"


async def apply_batch_async(
    gateway: ApplicationGateway,
    requests: Iterable[ApplicationRequest],
    browser_for: Callable[[ApplicationRequest], Awaitable[AsyncApplicationBrowser]],
    *,
    limit: int = 10,
) -> list[str]:
    """Fail closed. A blocked or ambiguous item opens circuit for remaining batch items."""
    items = list(requests)
    if not 1 <= len(items) <= limit or limit > 10:
        raise ValueError("batch size must be 1..10")
    if len({(item.vacancy_id, item.resume_id) for item in items}) != len(items):
        raise ApplicationCircuitOpen("duplicate vacancy/resume pair in batch")
    outcomes: list[str] = []
    for request in items:
        browser = await browser_for(request)
        outcome = await gateway.apply_to_vacancy_async(request, browser)
        outcomes.append(outcome)
        if outcome != "applied":
            raise ApplicationCircuitOpen(f"circuit opened after {request.vacancy_id}: {outcome}")
    return outcomes


class DryRunGateway:
    def __init__(self, database: Path) -> None:
        migrate(database)
        self.database = database

    def prepare(self, intent: Intent) -> str:
        if intent.text is not None and (not intent.text.strip() or "\x00" in intent.text):
            raise ValueError("unsafe text")
        text_hash = hashlib.sha256(intent.text.encode()).hexdigest() if intent.text else None
        with closing(sqlite3.connect(self.database)) as db, db:
            existing = db.execute("SELECT id FROM action_intents WHERE idempotency_key=?", (intent.idempotency_key,)).fetchone()
            if existing:
                return existing[0]
            db.execute(
                "INSERT INTO action_intents(id,idempotency_key,kind,target_json,text,text_sha256,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (intent.id, intent.idempotency_key, intent.kind, json.dumps(intent.target), intent.text, text_hash, "prepared", _now()),
            )
        return intent.id

    def execute_dry_run(self, intent_id: str, *, simulate_timeout: bool = False) -> str:
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute("SELECT status FROM action_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise KeyError(intent_id)
            if row[0] != "prepared":
                return row[0]
            outcome = "timeout" if simulate_timeout else "simulated_success"
            db.execute("INSERT INTO action_attempts(intent_id,outcome,detail,attempted_at) VALUES(?,?,?,?)", (intent_id, outcome, "dry-run only", _now()))
            status = "needs_reconciliation" if simulate_timeout else "verified"
            db.execute("UPDATE action_intents SET status=? WHERE id=?", (status, intent_id))
            db.execute("INSERT OR REPLACE INTO proofs(intent_id,rule,status,detail,verified_at) VALUES(?,?,?,?,?)", (intent_id, "dry-run proof", status, outcome, _now()))
        return status
