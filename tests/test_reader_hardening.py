import inspect

from autohunt.models import PageFamily
from autohunt.readers import (
    ApplicationFlowReader,
    ChatListReader,
    ChatThreadReader,
    NegotiationsReader,
    SearchReader,
    VacancyReader,
    detect_page,
)


def test_captcha_is_classified_and_never_offers_submit_capability():
    result = detect_page(
        "https://hh.ru/captcha", "<main>CAPTCHA: подтвердите, что вы человек</main>"
    )
    assert result.family is PageFamily.CAPTCHA
    assert not result.capabilities.has_submit_control


def test_readers_accept_empty_or_malformed_evidence_without_mutation_contract():
    assert SearchReader().read({}).observation.confidence == "partial"
    assert NegotiationsReader().read({}).visible_item_count == 0
    assert ChatListReader().read({"rows": [{"chat_id": "1"}]}).items == []
    assert ChatThreadReader().read("fixture://chat", "").messages == []
    assert ApplicationFlowReader().read_employer_answers({}).employer_question_count == 0
    assert VacancyReader().read("fixture://vacancy", "").title is None


def test_reader_public_methods_have_no_action_or_submit_surface():
    reader_types = [
        VacancyReader,
        SearchReader,
        NegotiationsReader,
        ApplicationFlowReader,
        ChatListReader,
        ChatThreadReader,
    ]
    public_methods = {
        name
        for reader_type in reader_types
        for name, member in inspect.getmembers(reader_type, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public_methods.isdisjoint({"click", "type", "submit", "send", "apply", "navigate"})
