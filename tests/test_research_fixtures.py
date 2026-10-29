import hashlib
import json
from pathlib import Path

from autohunt.models import PageFamily
from autohunt.readers import ChatThreadReader, VacancyReader

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "research_reference" / "fixtures"
MANIFEST = ROOT / "research_reference" / "FIXTURE_MANIFEST.json"


def test_materialized_fixture_hashes_match_manifest():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for fixture in manifest["fixtures"]:
        for key, hash_key in (("local", "sha256"), ("related_local", "related_sha256")):
            if key not in fixture:
                continue
            path = ROOT / "research_reference" / fixture[key]
            actual = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            assert actual == fixture[hash_key]


def test_vacancy_reader_handles_synthetic_post_application_fixture():
    raw = json.loads((FIXTURES / "post_application_cover.json").read_text(encoding="utf-8"))
    html = "<h1>{}</h1> {}".format(raw["heading"], " ".join(a["text"] for a in raw["actions"]))
    item = VacancyReader().read(raw["url"], html)
    assert item.vacancy_id == "900001"
    assert item.observation.family is PageFamily.VACANCY
    assert item.observation.capabilities.has_post_application_cover_action


def test_chat_reader_handles_synthetic_chat_transcript_without_guessing_direction():
    transcript = (FIXTURES / "ordinary_chat.txt").read_text(encoding="utf-8")
    item = ChatThreadReader().read("https://chatik.hh.ru/chat/800001", transcript)
    assert item.observation.family is PageFamily.CHAT_THREAD
    assert item.messages
    assert {message.direction for message in item.messages} == {"unknown"}
