import asyncio
import inspect
from pathlib import Path

import pytest

from autohunt.live_adapter import ReadOnlyHHSession, ReadOnlyViolation


def test_live_adapter_has_no_mutation_methods():
    names = {name for name, member in inspect.getmembers(ReadOnlyHHSession, inspect.isfunction)}
    assert names.isdisjoint({"click", "fill", "submit", "send", "apply", "delete", "decline"})
    assert "existing_hh_pages" in names


def test_live_adapter_rejects_untrusted_route_before_session_start():
    session = ReadOnlyHHSession(Path("var/profile"), Path("var/lock"))

    async def attempt() -> None:
        with pytest.raises(ReadOnlyViolation):
            await session.open_read_only("https://example.com")

    asyncio.run(attempt())
