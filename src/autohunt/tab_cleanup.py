"""Explicit browser hygiene for tabs opened by AutoHunt evidence runs.

This is intentionally separate from the read-only session. It is never called by a
reader automatically and requires an explicit cleanup invocation after a checkpoint.
"""

from collections.abc import Iterable
from urllib.parse import urlparse

from playwright.async_api import Page

HH_HOSTS = {"hh.ru", "www.hh.ru", "chatik.hh.ru"}


def is_hh_page(page: Page) -> bool:
    return urlparse(page.url).hostname in HH_HOSTS


async def close_duplicate_hh_pages(pages: Iterable[Page], keep: int = 1) -> int:
    """Close only surplus HH/Chatik tabs; caller decides when cleanup is safe."""
    if keep < 1:
        raise ValueError("keep must be at least one")
    targets = [page for page in pages if is_hh_page(page)]
    closed = 0
    for page in targets[keep:]:
        await page.close()
        closed += 1
    return closed
