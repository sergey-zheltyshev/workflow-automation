import asyncio
import sqlite3
from pathlib import Path

import pytest

from autohunt.candidate_facts import record_profile_version
from autohunt.context import evaluate_and_record_eligibility, provision_candidate, select_candidate
from autohunt.gateway import (
    ApplicationCircuitOpen,
    ApplicationGateway,
    ApplicationRequest,
    CoverAttachmentGateway,
    CoverAttachmentRequest,
    DryRunGateway,
    Intent,
    apply_batch_async,
)


class FakeApplicationBrowser:
    def __init__(self, *, state="clear", vacancy_id="1", resume_id="2", questions=False, mutate=False, proof=True):
        self.state, self.vacancy_id, self.resume_id = state, vacancy_id, resume_id
        self.questions, self.mutate, self.proof = questions, mutate, proof
        self.submits = 0

    def reconcile(self, vacancy_id, resume_id):
        return self.state

    def inspect(self, vacancy_id, resume_id):
        return {"vacancy_id": self.vacancy_id, "resume_id": self.resume_id, "required_questions": self.questions}

    def fill_and_verify(self, expected):
        if self.mutate:
            raise RuntimeError("exact text mismatch")

    def submit_once(self):
        self.submits += 1

    def prove_applied(self, vacancy_id, expected):
        return self.proof


def application_request():
    return ApplicationRequest(vacancy_id="1", resume_id="2", expected_cover_letter="Точный текст", decision_id="d1", source_revision="r1")


@pytest.mark.parametrize(
    ("browser", "error"),
    [
        (FakeApplicationBrowser(state="already_applied"), "already applied"),
        (FakeApplicationBrowser(vacancy_id="9"), "target or resume mismatch"),
        (FakeApplicationBrowser(resume_id="9"), "target or resume mismatch"),
        (FakeApplicationBrowser(questions=True), "unsupported required employer questions"),
        (FakeApplicationBrowser(mutate=True), "exact text mismatch"),
    ],
)
def test_application_gateway_blocks_unsafe_branches(browser, error):
    database = Path(f"var/test_gateway_application_block_{id(browser)}.sqlite")
    database.unlink(missing_ok=True)
    with pytest.raises(RuntimeError, match=error):
        ApplicationGateway(database, browser).apply_to_vacancy(application_request())
    assert browser.submits == 0


def test_application_gateway_is_idempotent_and_requires_executable_proof():
    browser = FakeApplicationBrowser()
    database = Path("var/test_gateway_application_idempotent.sqlite")
    database.unlink(missing_ok=True)
    gateway = ApplicationGateway(database, browser)
    assert gateway.apply_to_vacancy(application_request()) == "applied"
    assert gateway.apply_to_vacancy(application_request()) == "applied"
    assert browser.submits == 1


def test_application_gateway_ambiguous_submit_requires_reconciliation():
    browser = FakeApplicationBrowser(proof=False)
    database = Path("var/test_gateway_application_reconcile.sqlite")
    database.unlink(missing_ok=True)
    gateway = ApplicationGateway(database, browser)
    assert gateway.apply_to_vacancy(application_request()) == "needs_reconciliation"
    assert gateway.apply_to_vacancy(application_request()) == "needs_reconciliation"
    assert browser.submits == 1


def test_react_rerender_letter_change_blocks_submit():
    browser = FakeApplicationBrowser(mutate=True)
    database = Path("var/test_gateway_application_react.sqlite")
    database.unlink(missing_ok=True)
    with pytest.raises(RuntimeError, match="exact text mismatch"):
        ApplicationGateway(database, browser).apply_to_vacancy(application_request())
    assert browser.submits == 0


def test_async_gateway_submits_once_after_checks():
    class AsyncFake:
        def __init__(self): self.submits = 0
        async def reconcile(self, *_): return "clear"
        async def inspect(self, *_): return {"vacancy_id": "1", "resume_id": "2", "already_applied": False, "required_questions": False}
        async def fill_and_verify(self, expected): assert expected == "Точный текст"
        async def submit_once(self): self.submits += 1
        async def prove_applied(self, *_): return True
    browser = AsyncFake()
    database = Path("var/test_gateway_application_async.sqlite")
    database.unlink(missing_ok=True)
    gateway = ApplicationGateway(database, FakeApplicationBrowser())
    assert asyncio.run(gateway.apply_to_vacancy_async(application_request(), browser)) == "applied"
    assert asyncio.run(gateway.apply_to_vacancy_async(application_request(), browser)) == "applied"
    assert browser.submits == 1


def test_async_gateway_persists_block_and_never_submits():
    class AsyncFake:
        def __init__(self): self.submits = 0
        async def reconcile(self, *_): return "clear"
        async def inspect(self, *_): return {"vacancy_id": "1", "resume_id": "2", "already_applied": False, "required_questions": True}
        async def fill_and_verify(self, expected): raise AssertionError("must not fill")
        async def submit_once(self): self.submits += 1
        async def prove_applied(self, *_): return False
    browser = AsyncFake()
    database = Path("var/test_gateway_application_async_block.sqlite")
    database.unlink(missing_ok=True)
    with pytest.raises(ApplicationCircuitOpen, match="capability blocked"):
        asyncio.run(ApplicationGateway(database, FakeApplicationBrowser()).apply_to_vacancy_async(application_request(), browser))
    assert browser.submits == 0
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT status FROM action_intents").fetchone()[0] == "blocked"
        assert db.execute("SELECT outcome FROM action_attempts").fetchone()[0] == "blocked"


def test_country_confirmation_can_never_be_classified_as_no_application():
    class AsyncFake:
        def __init__(self): self.state, self.fills, self.submits = "clear", 0, 0
        async def reconcile(self, *_): return self.state
        async def inspect(self, *_): return {"vacancy_id": "1", "resume_id": "2", "already_applied": self.state == "already_applied", "required_questions": False, "country_confirmation_required": self.state == "clear"}
        async def confirm_country_application(self): self.state = "already_applied"
        async def fill_and_verify(self, *_): self.fills += 1
        async def submit_once(self): self.submits += 1
        async def prove_applied(self, *_): return True
    browser = AsyncFake()
    database = Path("var/test_gateway_country_confirmation.sqlite")
    database.unlink(missing_ok=True)
    gateway = ApplicationGateway(database, FakeApplicationBrowser())
    assert asyncio.run(gateway.apply_to_vacancy_async(application_request(), browser)) == "application_confirmed_cover_pending"
    assert browser.fills == browser.submits == 0
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT status FROM action_intents").fetchone()[0] == "application_confirmed_cover_pending"


def test_production_gateway_rejects_arbitrary_request_before_browser():
    database = Path("var/test_gateway_arbitrary_production.sqlite")
    database.unlink(missing_ok=True)
    provision_candidate(database, candidate_id="a", display_name="A", job_policy={"allowed_work_formats": ["remote"]}, automation_policy={"hh_applications": "enabled"}, account_binding={"hh_identity": "a", "resume_id": "resume-a", "resume_title": "A"}, job_policy_version="job-a", automation_policy_version="auto-a", binding_id="bind-a")
    select_candidate(database, "a")
    record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
    evaluate_and_record_eligibility(database, {"vacancy_id": "1", "work_format": "remote"})
    class AsyncFake:
        async def reconcile(self, *_): raise AssertionError("browser must not open")
        async def inspect(self, *_): raise AssertionError("browser must not open")
    with pytest.raises(ApplicationCircuitOpen, match="mismatch"):
        asyncio.run(ApplicationGateway(database, FakeApplicationBrowser(), production=True).apply_to_vacancy_async(application_request(), AsyncFake()))


def test_production_gateway_requires_persisted_preflight_and_live_binding_proof():
    database = Path("var/test_gateway_authoritative_preflight.sqlite")
    database.unlink(missing_ok=True)
    provision_candidate(database, candidate_id="a", display_name="A", job_policy={"allowed_work_formats": ["remote"], "allowed_countries": ["Россия"]}, automation_policy={"hh_applications": "enabled"}, account_binding={"hh_identity": "live-account", "resume_id": "resume://stable", "resume_title": "A"}, job_policy_version="job-a", automation_policy_version="auto-a", binding_id="bind-a")
    select_candidate(database, "a")
    record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
    evaluate_and_record_eligibility(database, {"vacancy_id": "1", "work_format": "remote", "schedule": "5/2", "country": "Россия"})
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO candidate_vacancy_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("a", "d1", "1", "profile-a", "job-a", "r1", "run", "apply", 80, "fit", "Точный текст", "{}", "[]", "now"))
        db.execute("INSERT INTO account_binding_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("obs", "a", "bind-a", None, None, None, None, "UNKNOWN", "UNKNOWN", "{}", "now"))
        db.execute("INSERT INTO application_preflights VALUES(?,?,?,?,?,?,?,?,?,?)", ("pf", "a", "bind-a", "1", "d1", "resume://stable", "clear", "all checks", "{}", "now"))
    class AsyncFake:
        def __init__(self): self.submits = 0
        async def reconcile(self, *_): return "clear"
        async def inspect(self, *_): return {"vacancy_id": "1", "resume_id": "resume://stable", "account_identity": "live-account", "already_applied": False, "required_questions": False}
        async def fill_and_verify(self, *_): pass
        async def submit_once(self): self.submits += 1
        async def prove_applied(self, *_): return True
    browser = AsyncFake()
    request = ApplicationRequest(vacancy_id="1", resume_id="resume://stable", expected_cover_letter="Точный текст", decision_id="d1", source_revision="r1", preflight_id="pf")
    with pytest.raises(ApplicationCircuitOpen, match="identity is not verified"):
        asyncio.run(ApplicationGateway(database, FakeApplicationBrowser(), production=True).apply_to_vacancy_async(request, browser))
    assert browser.submits == 0
    with sqlite3.connect(database) as db:
        db.execute("UPDATE account_binding_observations SET observed_hh_identity='live-account',observed_resume_id='resume://stable',identity_status='MATCH',resume_status='MATCH' WHERE observation_id='obs'")
    assert asyncio.run(ApplicationGateway(database, FakeApplicationBrowser(), production=True).apply_to_vacancy_async(request, browser)) == "applied"
    assert browser.submits == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT kind FROM runtime_events WHERE candidate_id='a' ORDER BY created_at").fetchall()


def test_async_batch_stops_after_first_unsafe_item():
    class AsyncFake:
        def __init__(self, questions=False): self.questions, self.submits = questions, 0
        async def reconcile(self, *_): return "clear"
        async def inspect(self, *_): return {"vacancy_id": "1", "resume_id": "2", "already_applied": False, "required_questions": self.questions}
        async def fill_and_verify(self, expected): pass
        async def submit_once(self): self.submits += 1
        async def prove_applied(self, *_): return not self.questions
    first, second = AsyncFake(questions=True), AsyncFake()
    database = Path("var/test_gateway_application_batch.sqlite")
    database.unlink(missing_ok=True)
    requests = [application_request(), ApplicationRequest(vacancy_id="3", resume_id="2", expected_cover_letter="Ещё текст", decision_id="d2", source_revision="r2")]
    async def browser_for(request): return first if request.vacancy_id == "1" else second
    with pytest.raises(ApplicationCircuitOpen, match="capability blocked"):
        asyncio.run(apply_batch_async(ApplicationGateway(database, FakeApplicationBrowser()), requests, browser_for))
    assert first.submits == 0
    assert second.submits == 0


def test_async_batch_rejects_duplicate_before_browser_creation():
    database = Path("var/test_gateway_application_duplicates.sqlite")
    database.unlink(missing_ok=True)
    called = False
    async def browser_for(request):
        nonlocal called
        called = True
        raise AssertionError("must not construct a browser")
    with pytest.raises(ApplicationCircuitOpen, match="duplicate"):
        asyncio.run(apply_batch_async(ApplicationGateway(database, FakeApplicationBrowser()), [application_request(), application_request()], browser_for))
    assert not called


def test_dry_run_is_idempotent_and_timeout_never_retries():
    database = Path("var/test_gateway.sqlite")
    database.unlink(missing_ok=True)
    gateway = DryRunGateway(database)
    intent = Intent(id="i1", idempotency_key="k1", kind="chat_reply", target={"chat_id": "c1"}, text="Hello")
    assert gateway.prepare(intent) == "i1"
    assert gateway.prepare(intent) == "i1"
    assert gateway.execute_dry_run("i1", simulate_timeout=True) == "needs_reconciliation"
    assert gateway.execute_dry_run("i1") == "needs_reconciliation"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM action_attempts").fetchone()[0] == 1


def test_gateway_blocks_corrupted_text():
    gateway = DryRunGateway(Path("var/test_gateway_text.sqlite"))
    with pytest.raises(ValueError, match="unsafe text"):
        gateway.prepare(Intent(id="i2", idempotency_key="k2", kind="cover_letter", target={"vacancy_id": "v1"}, text="bad\x00text"))


def test_cover_attachment_has_own_authorization_and_two_signal_proof():
    database = Path("var/test_gateway_cover_attachment.sqlite")
    database.unlink(missing_ok=True)
    provision_candidate(database, candidate_id="a", display_name="A", job_policy={}, automation_policy={"hh_applications": "enabled"}, account_binding={"hh_identity": "a", "resume_id": "resume-a", "resume_title": "A"}, job_policy_version="job-a", automation_policy_version="auto-a", binding_id="bind-a")
    select_candidate(database, "a")
    record_profile_version(database, "profile-a", {"name": "A"}, {"name": "test"})
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO candidate_vacancy_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("a", "d1", "1", "profile-a", "job-a", "r1", "run", "apply", 80, "fit", "Точный текст", "{}", "[]", "now"))
        db.execute("INSERT INTO candidate_application_processes VALUES(?,?,?,?,?,?,?,?,?)", ("a", "1", "bind-a", "d1", "resume-a", "hash", "application_confirmed_cover_pending", "HH confirmed", "now"))
        db.execute("INSERT INTO candidate_chat_vacancy_links VALUES(?,?,?,?,?)", ("a", "chat-1", "1", "linked", "live proof"))

    class Browser:
        def __init__(self): self.opened = self.sent = False
        async def open_cover_attachment(self): self.opened = True
        async def fill_and_verify(self, value): assert value == "Точный текст"
        async def send_once(self): self.sent = True
        async def prove_cover_attached(self, value): return self.opened and self.sent and value == "Точный текст"

    browser = Browser()
    request = CoverAttachmentRequest(vacancy_id="1", chat_id="chat-1", expected_text="Точный текст")
    assert asyncio.run(CoverAttachmentGateway(database).attach(request, browser)) == "attached"
    assert asyncio.run(CoverAttachmentGateway(database).attach(request, browser)) == "applied"
    assert browser.opened and browser.sent
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT state FROM candidate_application_processes").fetchone()[0] == "confirmed"
        assert db.execute("SELECT status FROM proofs").fetchone()[0] == "attached"


def test_hh_chat_adapter_prefers_current_chatik_controls():
    from autohunt.hh_chat_adapter import HHChatAdapter

    class Control:
        def __init__(self, count=1):
            self.count_value, self.value, self.clicked = count, "", False

        async def count(self): return self.count_value
        async def fill(self, value): self.value = value
        async def input_value(self): return self.value
        async def inner_text(self): return self.value
        async def evaluate(self, _): return True
        async def press(self, _): pass
        async def click(self): self.clicked = True

    class Page:
        def __init__(self): self.field, self.send = Control(), Control()
        def locator(self, selector):
            return self.field if "chatik-new-message-text" in selector else self.send
        def get_by_role(self, *_args, **_kwargs): return Control(0)

    page = Page()
    adapter = HHChatAdapter(page)  # type: ignore[arg-type]
    import asyncio
    asyncio.run(adapter.fill_and_verify("Проверенный ответ"))
    asyncio.run(adapter.send_once())
    assert page.field.value == "Проверенный ответ"
    assert page.send.clicked
