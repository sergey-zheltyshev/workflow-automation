"""Internal HH mutation adapter. It is intentionally not a CLI or standalone script."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

from playwright.async_api import Page

from .hh_ui_contract import (
    SELECTORS,
    HHState,
    canonical_response_control_index,
    canonical_response_href,
)
from .live_adapter import rendered_body


class HHApplicationAdapter:
    """One-page adapter; gateway owns lifecycle and calls these methods in order."""

    def __init__(self, page: Page, *, vacancy_id: str, resume_id: str, resume_title: str | None = None) -> None:
        self.page, self.vacancy_id, self.resume_id = page, vacancy_id, resume_id
        self.resume_title = resume_title
        self._submitted = False
        self._application_started = False

    async def reconcile(self, vacancy_id: str, resume_id: str) -> str:
        await self._dismiss_cookie_shell()
        initial = " ".join((await rendered_body(self.page)).split())
        # Response route contains vacancyId too.  It must hydrate as a form,
        # never be mistaken for the original vacancy READY page.
        if urlparse(self.page.url).path == "/applicant/vacancy_response":
            initial = " ".join((await self._form_body()).split())
        elif vacancy_id in self.page.url and not self._application_started and "Вы откликнулись" not in initial and "Отклик на вакансию" not in initial:
            return "vacancy_ready"
        body = initial if "Отклик на вакансию" in initial or "Вы откликнулись" in initial else " ".join((await self._form_body()).split())
        if vacancy_id not in self.page.url and vacancy_id not in body:
            return "wrong_target"
        if self._captcha(body):
            return "captcha"
        return "already_applied" if "Вы откликнулись" in body else "clear"

    async def discover_response_url(self) -> str | None:
        """Read actual HH response route from vacancy DOM; never synthesize one."""
        links = await self.page.locator('a[href*="vacancy_response"], form[action*="vacancy_response"]').evaluate_all(
            """els => els.map(e => e.href || e.action || e.getAttribute('data-url') || '')
                .filter(Boolean)"""
        )
        for raw in links:
            url = urljoin(self.page.url, str(raw))
            parsed = urlparse(url)
            if parsed.hostname in {"hh.ru", "www.hh.ru"} and "vacancy_response" in parsed.path and self.vacancy_id in url:
                return url
        # React can keep the actual route only in a data attribute / bootstrap
        # value.  Accept it only when it names this exact vacancy.
        raw = await self.page.locator('[data-url*="vacancy_response"], [data-href*="vacancy_response"]').evaluate_all(
            "els => els.map(e => e.getAttribute('data-url') || e.getAttribute('data-href') || '').filter(Boolean)"
        )
        for value in raw:
            url = urljoin(self.page.url, str(value))
            if "vacancy_response" in url and self.vacancy_id in url:
                return url
        return None

    async def _canonical_apply_rows(self) -> list[dict[str, object]]:
        """Resolve RT-006 duplicate visual CTAs without clicking any of them."""
        return await self.page.locator(", ".join(SELECTORS[HHState.APPLY_AVAILABLE])).evaluate_all(
            """els => els.map((e,index) => { const s=getComputedStyle(e), r=e.getBoundingClientRect(); return {
                index, href:e.href || '', top:r.top, viewport:innerHeight,
                visible:s.visibility !== 'hidden' && s.display !== 'none' && r.width > 2 && r.height > 2
            }; })"""
        )

    async def _canonical_apply_href(self) -> str | None:
        return canonical_response_href(self.vacancy_id, self.page.url, await self._canonical_apply_rows())

    async def inspect(self, vacancy_id: str, resume_id: str) -> dict[str, object]:
        await self._dismiss_cookie_shell()
        await self._wait_for_vacancy_surface()
        initial = " ".join((await rendered_body(self.page)).split())
        if vacancy_id in self.page.url and not self._application_started and "Вы откликнулись" not in initial and "Отклик на вакансию" not in initial:
            identity = await self.live_identity_snapshot()
            canonical = await self._canonical_apply_href()
            return {"vacancy_id": self.vacancy_id, "resume_id": "unknown", "resume_url": None, "resume_title": None, "resume_ambiguous": True, "account_identity": identity["account_identity"], "required_questions": False, "question_keys": [], "already_applied": False, "country_confirmation_required": False, "unknown_modal": False, "apply_button_available": canonical is not None, "canonical_response_url": canonical}
        body = await self._form_body()
        dialogs = await self.page.locator('[role="dialog"], [aria-modal="true"]').count()
        if self.vacancy_id not in self.page.url and self.vacancy_id not in body:
            return {"vacancy_id": "wrong", "resume_id": "wrong", "required_questions": True}
        resume_options = await self._resume_options()
        selected = [item for item in resume_options if item["selected"]]
        observed_resume = selected[0] if len(selected) == 1 else None
        identity = await self.live_identity_snapshot()
        questions = await self._question_keys()
        mandatory_question_marker = any(
            marker in " ".join(body.split())
            for marker in ("Вопросы работодателя", "Для отклика необходимо ответить")
        )
        return {
            "vacancy_id": self.vacancy_id,
            # Never promote title-only match to stable identity.  The binding
            # value is a canonical HH resume URL/identifier in production.
            "resume_id": observed_resume["id"] if observed_resume else "unknown",
            "resume_url": observed_resume["url"] if observed_resume else None,
            "resume_title": observed_resume["title"] if observed_resume else None,
            "resume_ambiguous": len(selected) != 1,
            "account_identity": identity["account_identity"],
            # A visible HH marker without machine-readable labels is not an
            # empty questionnaire.  It must block submit and create UI-map
            # work rather than silently sending a generic response.
            "required_questions": bool(questions) or mandatory_question_marker,
            "question_keys": questions or (["__unmapped_required_questions__"] if mandatory_question_marker else []),
            "already_applied": "Вы откликнулись" in " ".join(body.split()),
            "country_confirmation_required": "Вы откликаетесь на вакансию в другой стране" in body,
            "unknown_modal": dialogs > 0 and "Вы откликаетесь на вакансию в другой стране" not in body,
            "apply_button_available": False,
        }

    async def live_identity_snapshot(self) -> dict[str, object]:
        """Read stable account/resume evidence from rendered HH UI, no click/fill."""
        options = await self._resume_options()
        canonical = sorted({str(item["url"]) for item in options if item["url"]})
        html = await self.page.content()
        crypted = re.search(r'"cryptedUserId"\s*:\s*"([^"]+)"', html)
        hhid = re.search(r'"hhid"\s*:\s*"([^"]+)"', html)
        # Resume-set hash is useful only as corroboration; account identity comes
        # from HH's own rendered bootstrap payload and survives zero resumes.
        source = crypted.group(1) if crypted else None
        identity = hashlib.sha256(("hh-account:" + source).encode()).hexdigest() if source else None
        return {
            "account_identity": identity,
            "account_signals": {"crypted_user_id": source, "hhid": hhid.group(1) if hhid else None, "resume_set_hash": hashlib.sha256(("hh-resume-set:" + "|".join(canonical)).encode()).hexdigest() if canonical else None},
            "resume_options": options,
            "url": self.page.url,
        }

    async def _resume_options(self) -> list[dict[str, object]]:
        anchors = self.page.locator('a[href*="/resume/"]')
        raw = await anchors.evaluate_all("els => els.map(e => ({href:e.href, text:(e.innerText||'').trim(), selected:e.getAttribute('aria-current') === 'true' || e.getAttribute('aria-selected') === 'true' || e.closest('[aria-selected=true]') !== null}))")
        options: list[dict[str, object]] = []
        for item in raw:
            href = str(item.get("href") or "")
            parsed = urlparse(href)
            marker = parsed.path.rstrip("/").split("/")[-1]
            if marker and marker != "resume":
                canonical = f"https://hh.ru/resume/{marker}"
                options.append({"id": canonical, "url": canonical, "title": str(item.get("text") or ""), "selected": bool(item.get("selected"))})
        # New HH applicant profile surface can expose a resume card through an
        # action URL/data attribute rather than a direct resume anchor.
        card_rows = await self.page.locator("[data-resume-id], [data-resume-hash], [data-qa]").evaluate_all(
            """els => els.map(e => ({
                text:(e.innerText||'').trim(), id:e.getAttribute('data-resume-id')||e.getAttribute('data-resume-hash'),
                qa:e.getAttribute('data-qa')||'', href:e.getAttribute('href')||''
            })).filter(x => x.id || /resume/i.test(x.qa+x.href))"""
        )
        for item in card_rows:
            raw_id = str(item.get("id") or "")
            href = str(item.get("href") or "")
            match = re.search(r"/resume/([^/?#]+)|[?&](?:resume_id|resumeId)=([^&#]+)", href)
            marker = next((part for part in (match.groups() if match else ()) if part), None)
            stable_id = f"https://hh.ru/resume/{marker}" if marker else (f"hh-resume:{raw_id}" if raw_id else "")
            if stable_id:
                options.append({"id": stable_id, "url": stable_id if stable_id.startswith("https://") else (urljoin(self.page.url, href) if href else None), "title": str(item.get("text") or ""), "selected": bool(self.resume_title and self.resume_title.lower() in str(item.get("text") or "").lower())})
        # Response form often keeps the selected stable resume in a hidden
        # radio/input/select value rather than an anchor/card.  Use only an
        # explicit stable value and explicit checked/selected state; never a
        # title-only guess.
        controls = await self.page.locator("input, option, select, button, [data-qa]").evaluate_all(
            """els => els.map(e => ({
                value:e.value || '', href:e.href || '', dataId:e.getAttribute('data-resume-id') || e.getAttribute('data-resume-hash') || '',
                name:e.getAttribute('name') || '', qa:e.getAttribute('data-qa') || '', text:(e.innerText || e.textContent || '').trim(),
                selected:!!e.checked || !!e.selected || e.getAttribute('aria-selected') === 'true' || e.getAttribute('aria-current') === 'true'
            })).filter(x => /resume|резюме/i.test(x.name+x.qa+x.href+x.value+x.dataId))"""
        )
        for item in controls:
            raw = " ".join(str(item.get(key) or "") for key in ("value", "href", "dataId"))
            match = re.search(r"/resume/([^/?#]+)|(?:resume_id|resumeId)=([^&#\s]+)|\b([0-9a-f]{32,})\b", raw)
            marker = next((part for part in (match.groups() if match else ()) if part), None)
            if marker:
                stable_id = f"https://hh.ru/resume/{marker}"
                options.append({"id": stable_id, "url": stable_id, "title": str(item.get("text") or ""), "selected": bool(item.get("selected"))})
        # The response form renders one already selected resume as a title,
        # often without an href/input.  The current binding supplies the
        # canonical resume ID and title; a single exact visible title is proof
        # of selection on this form, not a title-only identity guess.
        form_titles = [text.strip() for text in await self.page.locator('[data-qa="resume-title"]').all_inner_texts() if text.strip()]
        if len(form_titles) == 1 and self.resume_title and form_titles[0] == self.resume_title:
            options.append({"id": self.resume_id, "url": self.resume_id, "title": form_titles[0], "selected": True})
        unique: dict[str, dict[str, object]] = {}
        for item in options:
            key = str(item["id"])
            if key not in unique or bool(item["selected"]) or len(str(item["title"])) > len(str(unique[key]["title"])):
                unique[key] = item
        options = list(unique.values())
        # HH can render selected resume as text rather than a link. This is not
        # a stable identifier and must remain ambiguous/fail closed.
        return options

    async def read_capability_snapshot(self) -> dict[str, object]:
        """Read-only diagnostic used before a Gateway action; it never mutates page state."""
        body = await self._form_body()
        return {
            "url": self.page.url,
            "required_questions": any(
                marker in body
                for marker in ("Вопросы работодателя", "Для отклика необходимо ответить")
            ),
            "captcha": self._captcha(" ".join(body.split())),
            "textarea_count": await self.page.locator("textarea").count(),
            "resume_titles": await self.page.get_by_role("button").all_inner_texts(),
            "already_applied": "Вы откликнулись" in " ".join(body.split()),
        }

    async def fill_and_verify(self, expected: str) -> None:
        field = self.page.locator("textarea")
        if await field.count() != 1:
            raise RuntimeError("cover-letter field cardinality mismatch")
        await field.fill(expected)
        first = await field.input_value()
        if first != expected or len(first) != len(expected) or hashlib.sha256(first.encode()).hexdigest() != hashlib.sha256(expected.encode()).hexdigest():
            raise RuntimeError("exact text mismatch before blur")
        await field.evaluate("element => element.blur()")
        second = await field.input_value()
        if second != expected or len(second) != len(expected) or hashlib.sha256(second.encode()).hexdigest() != hashlib.sha256(expected.encode()).hexdigest():
            raise RuntimeError("exact text mismatch after blur")

    async def _question_keys(self) -> list[str]:
        """Stable visible labels only; anonymous controls block preflight."""
        return await self.page.locator('input[required], textarea[required], select[required]').evaluate_all(
            """els => els.map((e, i) => {
                const id=e.id, label=id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
                const text=(label?.innerText || e.getAttribute('aria-label') || e.getAttribute('name') || '').trim();
                return text || `__anonymous_${i}`;
            })"""
        )

    async def fill_questions_and_verify(self, answers: dict[str, str]) -> None:
        """Exact readback for text/textarea/select/radio/checkbox employer controls."""
        keys = await self._question_keys()
        if any(key.startswith("__anonymous_") or key not in answers for key in keys):
            raise RuntimeError("required employer answer is unknown")
        for key in keys:
            selector = f'input[aria-label="{key}"],textarea[aria-label="{key}"],select[aria-label="{key}"],input[name="{key}"],textarea[name="{key}"],select[name="{key}"]'
            control = self.page.locator(selector)
            if await control.count() != 1:
                raise RuntimeError(f"employer question selector ambiguous: {key}")
            tag = await control.evaluate("e => e.tagName.toLowerCase()")
            kind = await control.get_attribute("type")
            expected = answers[key]
            if tag == "select":
                await control.select_option(label=expected)
                actual = await control.input_value()
                if actual != expected and expected not in await control.locator("option:checked").all_inner_texts():
                    raise RuntimeError(f"employer select readback mismatch: {key}")
            elif kind in {"radio", "checkbox"}:
                if expected.strip().lower() not in {"true", "yes", "да", "1", "selected"}:
                    raise RuntimeError(f"non-boolean answer needs explicit option selector: {key}")
                await control.check()
                if not await control.is_checked():
                    raise RuntimeError(f"employer choice readback mismatch: {key}")
            else:
                await control.fill(expected)
                if await control.input_value() != expected:
                    raise RuntimeError(f"employer text readback mismatch: {key}")
            await control.evaluate("element => element.blur()")

    async def confirm_country_application(self) -> None:
        """Mutation-only transition; never called by reconcile/inspect/preflight."""
        confirm = self.page.get_by_role("button", name="Все равно откликнуться", exact=True)
        if await confirm.count() != 1:
            raise RuntimeError("cross-country confirmation control cardinality mismatch")
        await confirm.click()

    async def open_application_form(self) -> None:
        canonical = await self._canonical_apply_href()
        if not canonical:
            raise RuntimeError("unknown HH apply control contract after prepared resolution")
        # RT-006: identical top/sticky anchors are one business transition.
        # A direct GET can bounce to `forceQuickResponse` without invoking HH's
        # UI handler. Click one verified visible canonical control instead.
        controls = ", ".join(SELECTORS[HHState.APPLY_AVAILABLE])
        index = canonical_response_control_index(self.vacancy_id, self.page.url, await self._canonical_apply_rows())
        if index is None:
            raise RuntimeError("canonical HH apply control disappeared before click")
        # Locator.click dispatches a trusted user-like pointer event; page-side
        # element.click() is ignored by HH's response handler.
        await self.page.locator(controls).nth(index).click()
        self._application_started = True
        await self.page.wait_for_timeout(500)

    async def submit_once(self) -> None:
        if self._submitted:
            raise RuntimeError("duplicate submit blocked")
        submit = self.page.get_by_role("button", name="Откликнуться", exact=True)
        if await submit.count() != 1:
            raise RuntimeError("submit control cardinality mismatch")
        self._submitted = True
        await submit.click()

    async def prove_applied(self, vacancy_id: str, expected: str) -> bool:
        await self.page.wait_for_timeout(1_500)
        body = " ".join((await self._form_body()).split())
        # Text alone is deliberately not a full proof. Gateway persists a pending
        # process unless target, resume and post-action state have all been reread.
        snapshot = await self.live_identity_snapshot()
        selected = [item for item in snapshot["resume_options"] if item["selected"]]
        return "Вы откликнулись" in body and vacancy_id in (self.page.url + body) and len(selected) == 1 and selected[0]["id"] == self.resume_id

    async def _form_body(self) -> str:
        """Wait for HH application content, not the initial cookie/document shell."""
        for _ in range(90):
            body = await rendered_body(self.page)
            normalized = " ".join(body.split())
            if "Отклик на вакансию" in normalized or "Вы откликнулись" in normalized:
                return body
            if "Вы откликаетесь на вакансию в другой стране" in normalized:
                return body
            await self.page.wait_for_timeout(500)
        raise RuntimeError(f"application form did not render: {self.page.url}")

    async def _dismiss_cookie_shell(self) -> None:
        """HH can initially render cookie shell over an otherwise READY route.

        Consent dismissal is neither a vacancy response nor a form submit, so it
        remains outside application mutation lifecycle.
        """
        body = " ".join((await rendered_body(self.page)).split())
        if "Мы используем файлы cookie" not in body:
            return
        button = self.page.get_by_role("button", name="Понятно", exact=True)
        if await button.count() == 1:
            await button.click()
            await self.page.wait_for_timeout(1_000)

    async def _wait_for_vacancy_surface(self) -> None:
        """Do not classify a direct HH navigation while React is still shell-only."""
        controls = ", ".join(SELECTORS[HHState.APPLY_AVAILABLE])
        for _ in range(24):
            body = " ".join((await rendered_body(self.page)).split())
            if "Вы откликнулись" in body or "Отклик на вакансию" in body:
                return
            if await self.page.locator(controls).count() > 0:
                return
            await self.page.wait_for_timeout(500)

    @staticmethod
    def _captcha(body: str) -> bool:
        normalized = body.lower()
        return any(marker in normalized for marker in ("подтвердите, что вы не робот", "проверка безопасности"))
