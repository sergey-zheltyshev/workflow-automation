"""HH chat mutation adapter; only ChatReplyGateway can call it."""

from __future__ import annotations

import hashlib

from playwright.async_api import Page


class HHChatAdapter:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._sent = False

    async def fill_and_verify(self, expected: str) -> None:
        # Full HH Chatik uses a stable control.  The generic fallback keeps
        # older thread surfaces working, but must never turn multiple textareas
        # (hidden drafts, search boxes) into an arbitrary send target.
        field = self.page.locator('textarea[data-qa="chatik-new-message-text"]')
        if await field.count() == 0:
            field = self.page.locator('textarea:not([disabled]), [contenteditable="true"]:not([disabled])')
        if await field.count() != 1:
            raise RuntimeError("chat input cardinality mismatch")
        # HH's React composer can reflect `fill()` in the native textarea
        # while leaving the send control disabled.  Do not use
        # press_sequentially(expected): a newline is interpreted as Enter and
        # can send a partial cover.  A reversible space/backspace pair fires
        # normal input events without changing the final payload.
        await field.fill(expected)
        await field.press("End")
        await field.press(" ")
        await field.press("Backspace")
        first = await field.input_value() if await field.evaluate("e => e.tagName === 'TEXTAREA'") else await field.inner_text()
        if first != expected or hashlib.sha256(first.encode()).hexdigest() != hashlib.sha256(expected.encode()).hexdigest():
            raise RuntimeError("chat exact text mismatch before send")

    async def send_once(self) -> None:
        if self._sent:
            raise RuntimeError("duplicate chat send blocked")
        button = self.page.locator('button[data-qa="chatik-do-send-message"]')
        if await button.count() == 0:
            button = self.page.get_by_role("button", name="Отправить", exact=True)
        if await button.count() != 1:
            raise RuntimeError("chat send control cardinality mismatch")
        self._sent = True
        await button.click()

    async def choose_once(self, label: str) -> None:
        if self._sent:
            raise RuntimeError("duplicate chat send blocked")
        option = self.page.get_by_role("button", name=label, exact=True)
        for _ in range(20):
            if await option.count() == 1:
                break
            await self.page.wait_for_timeout(500)
        if await option.count() != 1:
            raise RuntimeError("chat option cardinality mismatch")
        self._sent = True
        await option.click()

    async def prove_sent(self, expected: str) -> bool:
        await self.page.wait_for_timeout(1_000)
        messages = await self.page.locator('[data-qa^="chatik-chat-message-"]').all_inner_texts()
        if any(message.strip() == expected for message in messages):
            return True
        # HH can render a newly sent message in the ordinary thread body before
        # it assigns the message-level data-qa.  Treat the exact rendered body
        # as a second proof surface; this avoids a duplicate retry after a real
        # send merely because Chatik hydration lagged its selector.
        body = await self.page.locator("body").inner_text()
        return expected in body

    async def prove_cover_attached(self, expected: str) -> bool:
        """Proof for HH's dedicated applicant action, not a normal reply.

        HH currently consumes the one-time "Добавить сопроводительное" action
        after accepting text. Both exact rendered text and removal of that
        action are required; one signal alone is ambiguous.
        """
        if not await self.prove_sent(expected):
            return False
        action = self.page.locator('a[data-qa="chatik-chat-message-applicant-action"]')
        await self.page.wait_for_timeout(750)
        return await action.count() == 0

    async def open_cover_attachment(self) -> None:
        """Open HH's post-response cover surface; this is not a primary apply."""
        action = self.page.locator('a[data-qa="chatik-chat-message-applicant-action"]')
        # Direct chat navigation has a short React hydration phase.  This is
        # still PREPARED UI resolution, never a primary application mutation.
        for _ in range(20):
            if await action.count() == 1:
                break
            await self.page.wait_for_timeout(500)
        if await action.count() != 1:
            raise RuntimeError("cover attachment action cardinality mismatch")
        if (await action.inner_text()).strip() != "Добавить сопроводительное":
            raise RuntimeError("unexpected applicant chat action")
        await action.click()
        await self.page.wait_for_timeout(750)

    async def cover_surface(self) -> dict[str, object]:
        """Read rendered cover form after the authoritative action opened it."""
        fields = await self.page.locator('textarea, [contenteditable="true"]').evaluate_all(
            "els => els.map(e => ({tag:e.tagName, qa:e.getAttribute('data-qa'), placeholder:e.getAttribute('placeholder'), value:e.value || e.innerText || ''}))"
        )
        buttons = await self.page.locator('button, input[type="submit"]').evaluate_all(
            "els => els.map(e => ({qa:e.getAttribute('data-qa'), text:(e.innerText || e.value || '').trim(), disabled:!!e.disabled}))"
        )
        return {"url": self.page.url, "fields": fields, "buttons": buttons}
