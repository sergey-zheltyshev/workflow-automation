"""Production HH application batch runner. Every submit crosses ApplicationGateway."""

from __future__ import annotations

from contextlib import closing

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import ConfigDict, Field, model_validator

from .context import bootstrap
from .gateway import (
    ApplicationCircuitOpen,
    ApplicationGateway,
    ApplicationRequest,
    AsyncApplicationBrowser,
    apply_batch_async,
)
from .hh_application_adapter import HHApplicationAdapter
from .live_adapter import ReadOnlyHHSession, rendered_body

MATCH = "MATCH"
MISMATCH = "MISMATCH"
UNKNOWN = "UNKNOWN"


def observed_state(expected: str, observed: str | None) -> str:
    """Absence is not evidence of another account/resume."""
    if not observed or expected == "HH identity requires live verification":
        return UNKNOWN
    return MATCH if observed == expected else MISMATCH


def resume_state(expected: str, options: list[dict[str, object]], selected_id: str | None = None) -> str:
    if selected_id:
        return observed_state(expected, selected_id)
    ids = {str(item["id"]) for item in options if item.get("id")}
    if not ids:
        return UNKNOWN
    return MATCH if expected in ids else MISMATCH


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


class ApplicationPlanItem(ApplicationRequest):
    """Auditable, preselected candidate. No generated or browser-discovered mutation input."""

    model_config = ConfigDict(extra="forbid")

    response_url: str = Field(min_length=1)
    vacancy_url: str = ""  # populated only by authoritative SQLite queue builder

    @model_validator(mode="after")
    def response_url_matches_vacancy(self) -> ApplicationPlanItem:
        parsed = urlparse(self.response_url)
        if parsed.scheme != "https" or parsed.hostname not in {"hh.ru", "www.hh.ru"}:
            raise ValueError("response_url must be an https hh.ru route")
        if parsed.path != "/applicant/vacancy_response":
            raise ValueError("response_url must be HH vacancy response route")
        if parse_qs(parsed.query).get("vacancyId") != [self.vacancy_id]:
            raise ValueError("response_url vacancyId mismatch")
        return self


@dataclass
class ApplicationBatchReport:
    batch_id: str
    planned: int
    applied: list[str]
    stopped: str | None
    elapsed_seconds: float


def build_authoritative_application_queue(database: Path) -> list[ApplicationPlanItem]:
    """Only production planner. JSONL is not an input to this function or live execution."""
    context = bootstrap(database)
    binding = context.get("account_binding")
    if not context.get("candidate_id") or not binding or not context.get("candidate_profile_version") or not context.get("job_policy_version"):
        raise RuntimeError("active candidate context is incomplete")
    with closing(sqlite3.connect(database)) as db, db:
        rows = db.execute(
            "SELECT vacancy_id,decision_id,vacancy_revision,cover_letter,question_answers_json,vacancy_url "
            "FROM candidate_action_queue WHERE candidate_id=? "
            "ORDER BY decided_at DESC LIMIT 10",
            (context["candidate_id"],),
        ).fetchall()
    return [
        ApplicationPlanItem(
            vacancy_id=row[0], resume_id=binding["resume_id"], expected_cover_letter=row[3],
            decision_id=row[1], source_revision=row[2], question_answers=json.loads(row[4]),
            # This is a route-shaped placeholder; read-only preflight must discover/verify response route.
            response_url=f"https://hh.ru/applicant/vacancy_response?vacancyId={row[0]}",
            vacancy_url=row[5],
        ) for row in rows
    ]


def recover_preclick_ui_resolution(database: Path, vacancy_id: str) -> bool:
    """Return a documented pre-click RT-006 failure to executable PREPARED.

    This narrow repair is intentionally unavailable for a submit/country click:
    it requires the recorded detail proving failure happened before adapter action.
    """
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    binding = context.get("account_binding") or {}
    if not candidate_id or not binding:
        raise RuntimeError("active candidate context is incomplete")
    with closing(sqlite3.connect(database)) as db, db:
        process = db.execute(
            "SELECT state,detail FROM candidate_application_processes WHERE candidate_id=? AND vacancy_id=?",
            (candidate_id, vacancy_id),
        ).fetchone()
        if process is None:
            return False
        state, detail = map(str, process)
        if (state, detail) not in {
            ("in_flight", "open application form before-click"),
            ("in_flight", "country confirmation before-click"),
            ("prepared", "authoritative SQLite request"),
        }:
            raise RuntimeError("pre-click recovery requires documented UI-resolution state")
        intents = db.execute("SELECT id,target_json FROM action_intents WHERE kind='application'").fetchall()
        intent_ids = [str(intent_id) for intent_id, target in intents if json.loads(str(target)).get("vacancy_id") == vacancy_id]
        attempts = db.execute(
            f"SELECT outcome,detail FROM action_attempts WHERE intent_id IN ({','.join('?' for _ in intent_ids)})",
            intent_ids,
        ).fetchall() if intent_ids else []
        details = "\n".join(str(row[1]) for row in attempts)
        # Recorded exception is produced before `open_application_form` invokes
        # navigation/click.  Any submit/country evidence forbids this repair.
        # The pre-RT-006 adapter used a route GET (not a DOM click); HH bounced
        # it to forceQuickResponse and reread vacancy_ready.  That historical
        # signature is also proven non-mutating and may be repaired once.
        no_click = (
            "unknown HH apply control cardinality" in details
            or "vacancy apply control unavailable" in details
            or "reconciliation blocked: vacancy_ready" in details
        )
        clear_form_open = "target or resume mismatch; state=clear" in details
        clear_country_confirm = "country confirmation after-click; state=clear" in details
        if not (no_click or clear_form_open or clear_country_confirm) or "application submit" in details.lower():
            raise RuntimeError("journal does not prove no mutation before UI resolution")
        reason = (
            "reconciliation_clear_after_country_confirmation_no_primary_application" if clear_country_confirm
            else "reconciliation_clear_after_form_open_no_primary_application" if clear_form_open
            else "no_mutation_occurred_before_ui_resolution"
        )
        db.execute("UPDATE candidate_application_processes SET state='prepared',detail=?,updated_at=? WHERE candidate_id=? AND vacancy_id=?", (reason, datetime.now(UTC).isoformat(), candidate_id, vacancy_id))
        for intent_id in intent_ids:
            db.execute("UPDATE action_intents SET status='prepared' WHERE id=?", (intent_id,))
            db.execute("INSERT INTO action_attempts(intent_id,outcome,detail,attempted_at) VALUES(?,?,?,?)", (intent_id, "prepared", reason, datetime.now(UTC).isoformat()))
        db.execute("INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (f"event:{uuid.uuid4()}", candidate_id, binding["binding_id"], None, "application_prepared", json.dumps({"vacancy_id": vacancy_id, "reason": reason}, ensure_ascii=False), datetime.now(UTC).isoformat()))
    return True


def record_confirmed_payload_unknown(database: Path, vacancy_id: str, *, evidence: dict[str, object]) -> None:
    """Terminalize a visibly confirmed HH response when HH hides sent payload."""
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    binding = context.get("account_binding") or {}
    if not candidate_id or not binding:
        raise RuntimeError("active candidate context is incomplete")
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute(
            "SELECT decision_id,resume_id,cover_sha256 FROM candidate_application_processes WHERE candidate_id=? AND vacancy_id=?",
            (candidate_id, vacancy_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("application process missing for confirmed HH response")
        _decision_id, resume_id, cover_hash = map(str, row)
        detail = "HH visibly confirms primary application; sent resume/cover payload unavailable in UI"
        db.execute(
            "UPDATE candidate_application_processes SET state='application_confirmed_payload_unknown',detail=?,updated_at=? WHERE candidate_id=? AND vacancy_id=?",
            (detail, datetime.now(UTC).isoformat(), candidate_id, vacancy_id),
        )
        intents = db.execute("SELECT id,target_json FROM action_intents WHERE kind='application'").fetchall()
        for intent_id, target in intents:
            if json.loads(str(target)).get("vacancy_id") == vacancy_id:
                db.execute("UPDATE action_intents SET status='application_confirmed_payload_unknown' WHERE id=?", (intent_id,))
                db.execute("INSERT INTO action_attempts(intent_id,outcome,detail,attempted_at) VALUES(?,?,?,?)", (intent_id, "application_confirmed", detail, datetime.now(UTC).isoformat()))
                db.execute("INSERT OR REPLACE INTO proofs(intent_id,rule,status,detail,verified_at) VALUES(?,?,?,?,?)", (intent_id, "HH visible application state + candidate/vacancy/binding", "application_confirmed_payload_unknown", json.dumps({"vacancy_id": vacancy_id, "candidate_id": candidate_id, "binding_id": binding["binding_id"], "bound_resume_id": resume_id, "cover_sha256": cover_hash, "evidence": evidence}, ensure_ascii=False), datetime.now(UTC).isoformat()))
        db.execute("INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (f"event:{uuid.uuid4()}", candidate_id, binding["binding_id"], None, "application_confirmed_payload_unknown", json.dumps({"vacancy_id": vacancy_id, "bound_resume_id": resume_id, "cover_sha256": cover_hash, "evidence": evidence}, ensure_ascii=False), datetime.now(UTC).isoformat()))


async def preflight_application_plan(root: Path, *, cdp_endpoint: str, plan: list[ApplicationPlanItem]) -> list[dict[str, object]]:
    """Internal preflight primitive. Caller supplies only system-built queue."""
    context = bootstrap(root / "var" / "autohunt.sqlite")
    binding = context.get("account_binding")
    if not context.get("candidate_id") or not binding:
        raise RuntimeError("active candidate context is incomplete")
    database = root / "var" / "autohunt.sqlite"
    session = ReadOnlyHHSession(root / "var" / "profile", root / "var" / "controller.lock")
    rows: list[dict[str, object]] = []
    try:
        await session.attach_existing_cdp(cdp_endpoint)
        for item in plan:
            if not item.vacancy_url:
                raise RuntimeError("authoritative vacancy URL missing")
            response_url = item.vacancy_url
            page = await session.open_read_only(response_url)
            try:
                adapter = HHApplicationAdapter(page, vacancy_id=item.vacancy_id, resume_id=item.resume_id)
                reconciliation = await adapter.reconcile(item.vacancy_id, item.resume_id)
                capabilities = await adapter.inspect(item.vacancy_id, item.resume_id)
                snapshot = await adapter.live_identity_snapshot()
                identity_status = observed_state(str(binding["hh_identity"]), snapshot.get("account_identity"))
                observed_resume_id = str(capabilities.get("resume_id") or "")
                resume_status = resume_state(str(binding["resume_id"]), snapshot["resume_options"], None if observed_resume_id == "unknown" else observed_resume_id or None)
                required = [str(question) for question in capabilities.get("question_keys", [])]
                answers_known = not required or all(question in item.question_answers and item.question_answers[question].strip() for question in required)
                opening_preflight = reconciliation == "vacancy_ready" and bool(capabilities.get("apply_button_available"))
                clear = (opening_preflight or (reconciliation == "clear" and resume_status == MATCH and bool(capabilities.get("resume_url")) and not capabilities.get("resume_ambiguous") and answers_known)) and capabilities.get("vacancy_id") == item.vacancy_id and identity_status == MATCH and not capabilities.get("country_confirmation_required") and not capabilities.get("unknown_modal")
                status = "clear" if clear else "blocked"
                evidence = {"reconciliation": reconciliation, "capabilities": capabilities, "identity": snapshot}
                preflight_id = f"preflight:{uuid.uuid4()}"
                with closing(sqlite3.connect(database)) as db, db:
                    if reconciliation != "vacancy_ready":
                        db.execute("INSERT INTO account_binding_observations(observation_id,candidate_id,binding_id,observed_hh_identity,observed_resume_id,observed_resume_url,observed_resume_title,identity_status,resume_status,evidence_json,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"identity:{uuid.uuid4()}", context["candidate_id"], binding["binding_id"], snapshot.get("account_identity"), capabilities.get("resume_id"), capabilities.get("resume_url"), capabilities.get("resume_title"), identity_status, resume_status, json.dumps(evidence, ensure_ascii=False), datetime.now(UTC).isoformat()))
                    db.execute("INSERT INTO application_preflights(preflight_id,candidate_id,binding_id,vacancy_id,decision_id,resume_id,status,detail,evidence_json,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (preflight_id, context["candidate_id"], binding["binding_id"], item.vacancy_id, item.decision_id, item.resume_id, status, "all live checks matched" if clear else "live identity/resume/capability check blocked", json.dumps(evidence, ensure_ascii=False), datetime.now(UTC).isoformat()))
                rows.append({"vacancy_id": item.vacancy_id, "response_url": response_url, "preflight_id": preflight_id, "preflight": status, "reconciliation": reconciliation, "capabilities": capabilities, "identity_status": identity_status, "resume_status": resume_status})
            finally:
                await page.close()
    finally:
        await session.stop()
    return rows


async def verify_live_account_binding(root: Path, *, cdp_endpoint: str) -> dict[str, object]:
    """Read-only HH account/resume proof. Never changes binding configuration."""
    database = root / "var" / "autohunt.sqlite"
    context = bootstrap(database)
    binding = context.get("account_binding")
    if not context.get("candidate_id") or not binding:
        raise RuntimeError("active candidate context is incomplete")
    session = ReadOnlyHHSession(root / "var" / "profile", root / "var" / "controller.lock")
    page = None
    try:
        await session.attach_existing_cdp(cdp_endpoint)
        page = await session.open_read_only("https://hh.ru/applicant/resumes")
        # Profile card is React-hydrated after cookie/header shell; do not read
        # the initial shell as a zero-resume answer.
        await page.wait_for_timeout(10_000)
        adapter = HHApplicationAdapter(page, vacancy_id="0", resume_id=str(binding["resume_id"]), resume_title=str(binding["resume_title"]))
        snapshot = await adapter.live_identity_snapshot()
        options = snapshot["resume_options"]
        matching = [item for item in options if item["id"] == binding["resume_id"]]
        selected = [item for item in options if item["selected"]]
        identity_status = observed_state(str(binding["hh_identity"]), snapshot.get("account_identity"))
        resume_status = resume_state(str(binding["resume_id"]), options)
        evidence = {"identity": snapshot, "matching_resume_options": matching}
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("INSERT INTO account_binding_observations(observation_id,candidate_id,binding_id,observed_hh_identity,observed_resume_id,observed_resume_url,observed_resume_title,identity_status,resume_status,evidence_json,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"identity:{uuid.uuid4()}", context["candidate_id"], binding["binding_id"], snapshot.get("account_identity"), selected[0]["id"] if len(selected) == 1 else None, selected[0]["url"] if len(selected) == 1 else None, selected[0]["title"] if len(selected) == 1 else None, identity_status, resume_status, json.dumps(evidence, ensure_ascii=False), datetime.now(UTC).isoformat()))
        return {"candidate_id": context["candidate_id"], "binding_id": binding["binding_id"], "identity_status": identity_status, "resume_status": resume_status, "observed_identity": snapshot.get("account_identity"), "resume_options": options}
    finally:
        if page:
            await page.close()
        await session.stop()


async def diagnose_live_account_binding(root: Path, *, cdp_endpoint: str) -> dict[str, object]:
    """Multi-surface HH diagnostic. Navigation/read/screenshot only; persists incident evidence."""
    stamp = _stamp()
    evidence_root = root / "var" / "evidence" / "account_binding_incidents" / stamp
    evidence_root.mkdir(parents=True, exist_ok=True)
    session = ReadOnlyHHSession(root / "var" / "profile", root / "var" / "controller.lock")
    surfaces = {
        "resumes": "https://hh.ru/applicant/resumes",
        "negotiations": "https://hh.ru/applicant/negotiations",
        "settings": "https://hh.ru/applicant/settings",
    }
    findings: list[dict[str, object]] = []
    try:
        await session.attach_existing_cdp(cdp_endpoint)
        for label, url in surfaces.items():
            page = await session.open_read_only(url)
            try:
                body = await rendered_body(page)
                adapter = HHApplicationAdapter(page, vacancy_id="0", resume_id="")
                identity = await adapter.live_identity_snapshot()
                anchors = await page.locator("a").evaluate_all("els => els.map(e => ({href:e.href,text:(e.innerText||'').trim()})).filter(x => /resume|applicant|profile/i.test(x.href+x.text))")
                resume_links = [item for item in anchors if "/resume/" in str(item.get("href") or "")]
                html = await page.content()
                (evidence_root / f"{label}.html").write_text(html, encoding="utf-8")
                (evidence_root / f"{label}.txt").write_text(body, encoding="utf-8")
                await page.screenshot(path=str(evidence_root / f"{label}.png"), full_page=True)
                findings.append({"surface": label, "requested_url": url, "final_url": page.url, "title": await page.title(), "visible_text_chars": len(body), "resume_links": resume_links, "identity_signals": anchors[:100], "account_snapshot": identity, "looks_login": "/account/login" in page.url or "Войти" in body[:1000]})
            finally:
                await page.close()
    finally:
        await session.stop()
    report = {"evidence_root": str(evidence_root), "surfaces": findings}
    (evidence_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


async def preflight_authoritative_application_queue(root: Path, *, cdp_endpoint: str) -> list[dict[str, object]]:
    """Only production preflight entry point: queue comes from authoritative SQLite."""
    return await preflight_application_plan(root, cdp_endpoint=cdp_endpoint, plan=build_authoritative_application_queue(root / "var" / "autohunt.sqlite"))


async def execute_authoritative_application_queue(root: Path, *, cdp_endpoint: str, limit: int = 1) -> ApplicationBatchReport:
    """Only live application entrypoint.

    It accepts no plan, vacancy, resume, cover, or response URL from a caller.
    Every request is rebuilt from current SQLite then must receive a fresh clear
    preflight in this invocation before it reaches the Gateway.
    """
    if not 1 <= limit <= 10:
        raise ValueError("limit must be 1..10")
    started = time.monotonic()
    planned = build_authoritative_application_queue(root / "var" / "autohunt.sqlite")[:limit]
    batch_id = f"applications-{_stamp()}"
    if not planned:
        return ApplicationBatchReport(batch_id=batch_id, planned=0, applied=[], stopped=None, elapsed_seconds=0.0)
    preflights = await preflight_application_plan(root, cdp_endpoint=cdp_endpoint, plan=planned)
    clear = {str(row["vacancy_id"]): row for row in preflights if row["preflight"] == "clear"}
    selected = [item.model_copy(update={"preflight_id": str(clear[item.vacancy_id]["preflight_id"]), "response_url": str(clear[item.vacancy_id]["response_url"])}) for item in planned if item.vacancy_id in clear]
    if not selected:
        return ApplicationBatchReport(batch_id=batch_id, planned=len(planned), applied=[], stopped="no clear mandatory preflight", elapsed_seconds=round(time.monotonic() - started, 2))

    session = ReadOnlyHHSession(root / "var" / "profile", root / "var" / "controller.lock")
    pages = []
    applied: list[str] = []
    stopped: str | None = None
    by_request = {(item.vacancy_id, item.resume_id): item for item in selected}

    async def browser_for(request: ApplicationRequest) -> AsyncApplicationBrowser:
        item = by_request[(request.vacancy_id, request.resume_id)]
        page = await session.open_hh_page(item.response_url)
        pages.append(page)
        return HHApplicationAdapter(page, vacancy_id=request.vacancy_id, resume_id=request.resume_id)

    try:
        await session.attach_existing_cdp(cdp_endpoint)
        gateway = ApplicationGateway(root / "var" / "autohunt.sqlite", _UnusedSyncBrowser(), production=True)
        outcomes = await apply_batch_async(gateway, selected, browser_for)
        applied = [item.vacancy_id for item, outcome in zip(selected, outcomes, strict=True) if outcome == "applied"]
    except (ApplicationCircuitOpen, RuntimeError) as exc:
        stopped = f"{type(exc).__name__}: {exc}"
    finally:
        for page in pages:
            await page.close()
        await session.stop()
    return ApplicationBatchReport(batch_id=batch_id, planned=len(planned), applied=applied, stopped=stopped, elapsed_seconds=round(time.monotonic() - started, 2))


def load_application_plan(path: Path) -> list[ApplicationPlanItem]:
    """Dev/test parser only. Production CLI deliberately never calls this."""
    items = [
        ApplicationPlanItem.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not 1 <= len(items) <= 10:
        raise ValueError("application plan must contain 1..10 items")
    return items


class LiveApplicationBatch:
    """Staged 2/3/5 batch. First unsafe or ambiguous item opens circuit permanently."""

    def __init__(self, root: Path, *, cdp_endpoint: str, plan: list[ApplicationPlanItem]) -> None:
        raise RuntimeError("direct production plan execution is disabled; use authoritative queue plus persisted preflight")
        self.root = root
        self.cdp_endpoint = cdp_endpoint
        self.plan = plan
        self.batch_id = f"applications-{_stamp()}"
        self.evidence_path = root / "var" / "evidence" / "application_batches" / f"{self.batch_id}.json"

    async def run(self) -> ApplicationBatchReport:
        started = time.monotonic()
        session = ReadOnlyHHSession(self.root / "var" / "profile", self.root / "var" / "controller.lock")
        pages = []
        applied: list[str] = []
        stopped: str | None = None
        by_request = {(item.vacancy_id, item.resume_id): item for item in self.plan}

        async def browser_for(request: ApplicationRequest) -> AsyncApplicationBrowser:
            item = by_request[(request.vacancy_id, request.resume_id)]
            page = await session.open_hh_page(item.response_url)
            pages.append(page)
            return HHApplicationAdapter(page, vacancy_id=request.vacancy_id, resume_id=request.resume_id)

        try:
            await session.attach_existing_cdp(self.cdp_endpoint)
            gateway = ApplicationGateway(self.root / "var" / "autohunt.sqlite", _UnusedSyncBrowser(), production=True)
            for stage in (self.plan[:2], self.plan[2:5], self.plan[5:]):
                if not stage:
                    continue
                outcomes = await apply_batch_async(gateway, stage, browser_for)
                applied.extend(item.vacancy_id for item, outcome in zip(stage, outcomes, strict=True) if outcome == "applied")
        except (ApplicationCircuitOpen, RuntimeError) as exc:
            stopped = f"{type(exc).__name__}: {exc}"
        finally:
            for page in pages:
                await page.close()
            await session.stop()
            report = ApplicationBatchReport(
                batch_id=self.batch_id,
                planned=len(self.plan),
                applied=applied,
                stopped=stopped,
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
            self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
            self.evidence_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report


class _UnusedSyncBrowser:
    """Async production path has no sync browser; prevents accidental sync calls."""

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"sync browser API is forbidden in live batch: {name}")


def run_live_application_batch(root: Path, *, cdp_endpoint: str, plan_path: Path) -> ApplicationBatchReport:
    raise RuntimeError("arbitrary plan execution is disabled; build from authoritative SQLite only")
