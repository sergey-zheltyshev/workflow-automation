"""Only supported production browser lifecycle for AutoHunt.

Direct persistent Playwright owns the Chrome it starts.  No CDP attach, remote
debugging port, Canary or daily user profile is involved.
"""
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from playwright.async_api import BrowserContext as AsyncBrowserContext
from playwright.async_api import async_playwright
from playwright.sync_api import BrowserContext, sync_playwright


def project_profile(project_root: Path) -> Path:
    profile = (project_root / "var" / "browser_profile").resolve()
    if not profile.is_dir():
        raise FileNotFoundError(f"AutoHunt browser profile missing: {profile}")
    return profile


@contextmanager
def production_browser_context(project_root: Path, *, timeout_ms: int = 30_000) -> Iterator[BrowserContext]:
    """Yield one Chrome Stable context for an entire business batch.

    The caller owns all pages in this context. Closing happens exactly once on
    context-manager exit, including errors; it never closes user Chrome.
    """
    profile = project_profile(project_root)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False, timeout=timeout_ms,
        )
        try:
            yield context
        finally:
            context.close()


@asynccontextmanager
async def async_production_browser_context(project_root: Path, *, timeout_ms: int = 30_000) -> AsyncIterator[AsyncBrowserContext]:
    """Async equivalent used by existing HH adapters; one context per batch."""
    profile = project_profile(project_root)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False, timeout=timeout_ms,
        )
        try:
            yield context
        finally:
            await context.close()
