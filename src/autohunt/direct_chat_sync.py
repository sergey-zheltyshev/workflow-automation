"""Direct-Playwright Chatik reader shared by READ and APPLY browser phases."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .chat import triage_changed_chats
from .context import bootstrap
from .models import Capabilities, ChatMessageDTO, ChatThreadDTO, PageFamily, PageObservation
from .sync import RegistrySync
from .ui_memory import open_incident


async def _all_chat_links(page) -> tuple[list[dict[str, str]], bool]:
    """Drain Chatik's virtualized list instead of trusting visible anchors."""
    seen: dict[str, dict[str, str]] = {}
    stable_passes = 0
    at_bottom = False
    for _ in range(80):
        raw = await page.locator('a[data-qa^="chatik-open-chat-"]').evaluate_all(
            "els => els.map(e => ({href:e.href,text:(e.innerText||'').trim(),qa:e.getAttribute('data-qa')}))"
        )
        before = len(seen)
        for row in raw:
            if row.get("href"):
                seen[str(row["href"])] = {"href": str(row["href"]), "title": str(row.get("text") or ""), "qa": str(row.get("qa") or "")}
        state = await page.locator('a[data-qa^="chatik-open-chat-"]').evaluate_all(
            """els => { let n=els[0]; while(n) { const s=getComputedStyle(n); if(n.scrollHeight>n.clientHeight && /(auto|scroll)/.test(s.overflowY)) { const next=Math.min(n.scrollTop+n.clientHeight*0.85,n.scrollHeight); const bottom=next>=n.scrollHeight-n.clientHeight; n.scrollTop=next; n.dispatchEvent(new Event('scroll',{bubbles:true})); return {found:true,bottom}; } n=n.parentElement; } return {found:false,bottom:true}; }"""
        )
        at_bottom = bool(isinstance(state, dict) and state.get("bottom"))
        await page.wait_for_timeout(140)
        stable_passes = stable_passes + 1 if len(seen) == before else 0
        if at_bottom and stable_passes >= 2:
            return list(seen.values()), True
    return list(seen.values()), False


async def sync_direct_chats(database: Path, context: BrowserContext, *, run_id: str) -> dict[str, int]:
    """Read Chatik only; no reply, cover or external-form mutation occurs here."""
    state = bootstrap(database)
    candidate_id = str(state.get("candidate_id") or "")
    if not candidate_id:
        raise RuntimeError("active candidate required")
    page = await context.new_page()
    try:
        await page.goto("https://hh.ru/chat", wait_until="domcontentloaded")
        await page.wait_for_timeout(800)
        raw, complete = await _all_chat_links(page)
    except (PlaywrightError, PlaywrightTimeoutError, RuntimeError) as exc:
        open_incident(
            database, surface="CHATTIK_LIST_READ", vacancy_id=None,
            detail=f"direct chat list read: {type(exc).__name__}: {exc}", evidence={"url": page.url},
            safe_resume={"action": "reread_chat_list"},
        )
        return {"threads": 0, "links": 0, "list_complete": 0, "incidents": 1}
    finally:
        await page.close()
    by_url: dict[str, dict[str, str]] = {str(x["href"]): {"title": str(x["title"]), "qa": str(x.get("qa") or "")} for x in raw if x.get("href")}
    if not complete:
        open_incident(database, surface="CHATTIK_LIST_READ", vacancy_id=None, detail="virtualized chat list did not reach a stable end", evidence={"links_seen": len(by_url)}, safe_resume={"action": "reread_chat_list"})
    threads: list[ChatThreadDTO] = []
    links: list[tuple[str, str]] = []
    for url, item in by_url.items():
        thread = await context.new_page()
        try:
            await thread.goto(url, wait_until="domcontentloaded")
            await thread.wait_for_timeout(450)
            messages = await thread.locator('[data-qa^="chatik-chat-message-"]').evaluate_all(
                """els => els.filter(e => /^chatik-chat-message-\\d+$/.test(e.getAttribute('data-qa')||''))
                .map(e => ({text:(e.querySelector('[data-qa$=\"-text\"]')?.innerText||e.innerText||'').trim(),
                author:(e.querySelector('[data-qa=\"chat-bubble-author-name\"]')?.innerText||'').trim(),
                receivedAt:(e.querySelector('time[datetime]')?.getAttribute('datetime')||''), html:e.outerHTML||''}))"""
            )
            chat_id_match = re.search(r"/chat/(\d+)", url)
            if not chat_id_match:
                continue
            if not messages:
                raise RuntimeError("no Chatik message nodes found on opened thread")
            parsed = [
                ChatMessageDTO(
                    direction="incoming" if str(row.get("author") or "").strip() else "unknown",
                    text=" ".join(str(row.get("text") or "").split()),
                    received_at=str(row.get("receivedAt") or "").strip() or None,
                )
                for row in messages if str(row.get("text") or "").strip()
            ]
            threads.append(ChatThreadDTO(
                chat_id=chat_id_match.group(1), url=url, title=item["title"].split("\n", 1)[0] or None,
                messages=parsed,
                observation=PageObservation(url=url, family=PageFamily.CHAT_THREAD, capabilities=Capabilities(), confidence="confirmed", evidence=["direct Chatik read"]),
            ))
            vacancy = thread.locator('a[href*="/vacancy/"]')
            for href in await vacancy.evaluate_all("els=>els.map(e=>e.href)"):
                match = re.search(r"/vacancy/(\d+)", str(href))
                if match:
                    links.append((chat_id_match.group(1), match.group(1)))
                    break
        except (PlaywrightError, PlaywrightTimeoutError, RuntimeError) as exc:
            chat_id = re.search(r"/chat/(\d+)", url)
            open_incident(
                database, surface="CHATTIK_THREAD_READ", vacancy_id=None,
                detail=f"direct chat read: {type(exc).__name__}: {exc}", evidence={"url": url},
                safe_resume={"action": "reread_chat", "chat_id": chat_id.group(1) if chat_id else None},
            )
        finally:
            try:
                await thread.close()
            except PlaywrightError:
                pass
    sync = RegistrySync(database, candidate_id=candidate_id)
    sync.sync_threads(threads)
    for chat_id, vacancy_id in links:
        sync.link_chat_to_vacancy(chat_id, vacancy_id, "direct Chatik vacancy link")
    changed_ids = [str(thread.chat_id) for thread in threads if thread.chat_id]
    triage = triage_changed_chats(database, changed_ids, run_id=run_id)
    return {"threads": len(threads), "links": len(links), "list_complete": int(complete), **triage}
