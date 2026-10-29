import asyncio

from autohunt.live_adapter import rendered_body
from autohunt.live_run import _structured_facts


def test_rendered_body_waits_past_blank_react_shell():
    class Locator:
        def __init__(self):
            self.reads = 0

        async def inner_text(self, timeout):
            self.reads += 1
            return "" if self.reads == 1 else "Loaded HH content"

    class Page:
        url = "https://hh.ru/vacancy/1"

        def __init__(self):
            self.body = Locator()
            self.waits = 0

        def locator(self, selector):
            assert selector == "body"
            return self.body

        async def wait_for_timeout(self, milliseconds):
            self.waits += 1

    page = Page()
    assert asyncio.run(rendered_body(page, timeout_ms=1_000)) == "Loaded HH content"
    assert page.waits == 1


def test_structured_facts_keep_unknowns_and_parse_salary_geo_format():
    facts = _structured_facts("Удалённая работа из России, график 5/2. Зарплата по договорённости.", "от 120 000 ₽ за месяц", "Операционный ассистент")
    assert facts["salary_from"] == 120000
    assert facts["currency"] == "RUB"
    assert facts["country"] == "Россия"
    assert facts["work_format"] == "remote"
    assert facts["schedule"] == "5/2"
    assert _structured_facts("Локация не указана", "", "Роль")["currency"] is None
