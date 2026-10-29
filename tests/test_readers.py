from autohunt.models import PageFamily
from autohunt.readers import ChatThreadReader, VacancyReader, detect_page


def test_vacancy_reader_detects_applied_and_cover():
    html = "<h1>Operations coordinator</h1>Вы откликнулись Приложить сопроводительное письмо"
    item = VacancyReader().read("https://hh.ru/vacancy/900001", html)
    assert item.vacancy_id == "900001"
    assert item.observation.family is PageFamily.VACANCY
    assert item.observation.capabilities.already_applied
    assert item.observation.capabilities.has_post_application_cover_action


def test_chat_reader_keeps_unknown_direction_not_false_confidence():
    item = ChatThreadReader().read(
        "https://chatik.hh.ru/chat/5529323254", "<div>Сообщение работодателя.</div>"
    )
    assert item.chat_id == "5529323254"
    assert item.observation.family is PageFamily.CHAT_THREAD
    assert item.messages[0].direction == "unknown"


def test_unknown_page_is_unknown():
    assert (
        detect_page("https://hh.ru/odd", "<div>unrelated banner</div>").family is PageFamily.UNKNOWN
    )
