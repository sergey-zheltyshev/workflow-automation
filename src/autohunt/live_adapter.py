"""Phase 3 Playwright adapter. Deliberately exposes read-only operations only."""

import asyncio
import json
import os
import re
import subprocess
import traceback
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .lock import ControllerLock


class ReadOnlyViolation(RuntimeError):
    pass


class BrowserLaunchError(RuntimeError):
    """Stable Chrome could not be launched with the isolated AutoHunt profile."""


class ProfileInUseError(BrowserLaunchError):
    """The selected normal Chrome profile is already locked by another Chrome process."""


_CHROME_VERSION_URL = "chrome://version"
_DEFAULT_STARTUP_TIMEOUT_MS = 60_000
_DEFAULT_NAVIGATION_TIMEOUT_MS = 45_000


def automation_browser_profile(root: Path) -> Path:
    """Return AutoHunt's own persistent Chrome data root, creating it on first launch."""
    profile = root / "var" / "browser_profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def stable_chrome_executable() -> Path:
    """Find installed Google Chrome Stable only; never select Chrome Canary/SxS."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise BrowserLaunchError("Google Chrome Stable executable was not found")
    return executable


@dataclass(frozen=True)
class TargetIdentity:
    url: str
    title: str


@dataclass(frozen=True)
class ChromeProfile:
    user_data_dir: Path
    profile_directory: str
    profile_path: Path
    profile_display_name: str | None
    discovery_source: Path


def discover_normal_chrome_profile() -> ChromeProfile:
    """Use Chrome's Local State selection; never infer or create an alternate profile."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise BrowserLaunchError("normal Chrome profile discovery failed: LOCALAPPDATA is unavailable")
    user_data_dir = Path(local_app_data) / "Google" / "Chrome" / "User Data"
    local_state = user_data_dir / "Local State"
    if not local_state.is_file():
        raise BrowserLaunchError(f"normal Chrome profile discovery failed: Local State not found: {local_state}")
    try:
        state = json.loads(local_state.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BrowserLaunchError(f"normal Chrome profile discovery failed: cannot read {local_state}: {type(exc).__name__}: {exc}") from exc
    profile_state = state.get("profile")
    if not isinstance(profile_state, dict) or not isinstance(profile_state.get("last_used"), str):
        raise BrowserLaunchError("normal Chrome profile discovery failed: profile.last_used is absent")
    profile_directory = profile_state["last_used"]
    profile_path = user_data_dir / profile_directory
    if not profile_path.is_dir():
        raise BrowserLaunchError(f"normal Chrome profile discovery failed: selected profile missing: {profile_path}")
    info_cache = profile_state.get("info_cache") if isinstance(profile_state.get("info_cache"), dict) else {}
    profile_info = info_cache.get(profile_directory) if isinstance(info_cache.get(profile_directory), dict) else {}
    display_name = profile_info.get("name") if isinstance(profile_info.get("name"), str) else None
    return ChromeProfile(user_data_dir, profile_directory, profile_path, display_name, local_state)


async def rendered_body(page: Page, *, timeout_ms: int = 45_000) -> str:
    """HH can commit navigation long before React renders content; never treat blank DOM as ready."""
    deadline = monotonic() + timeout_ms / 1000
    while monotonic() < deadline:
        remaining = max(1, int((deadline - monotonic()) * 1000))
        try:
            text = await page.locator("body").inner_text(timeout=min(5_000, remaining))
        except PlaywrightTimeoutError:
            text = ""
        if text.strip():
            return text
        await page.wait_for_timeout(250)
    raise RuntimeError(f"HH rendered body did not become non-empty: {page.url}")


class ReadOnlyHHSession:
    """One isolated persistent Chromium context. No action or form-submission API exists."""

    def __init__(self, profile_path: Path, lock_path: Path, *, profile_directory: str | None = None) -> None:
        self.profile_path = profile_path
        self.profile_directory = profile_directory
        self.lock = ControllerLock(lock_path)
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.browser: Browser | None = None
        self._owns_context = False
        self._attached_to_user_browser = False
        self._chrome_process: subprocess.Popen[bytes] | None = None
        self._chrome_stdout = None
        self._chrome_stderr = None
        self.browser_endpoint: str | None = None
        self.chrome_executable: Path | None = None
        self.chrome_command: list[str] = []

    async def start(
        self,
        *,
        timeout_ms: int = _DEFAULT_STARTUP_TIMEOUT_MS,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> None:
        """Launch owned Stable Chrome with an ephemeral CDP endpoint and attach Playwright."""
        if timeout_ms < 1:
            raise ValueError("browser startup timeout must be positive")
        self.lock.acquire()
        try:
            self.profile_path.mkdir(parents=True, exist_ok=True)
            if (self.profile_path / "SingletonLock").exists():
                raise ProfileInUseError(f"PROFILE_IN_USE: Chrome profile lock exists: {self.profile_path / 'SingletonLock'}")
            active_port = self.profile_path / "DevToolsActivePort"
            if active_port.exists():
                active_port.unlink()
            self.chrome_executable = stable_chrome_executable()
            self.chrome_command = [
                str(self.chrome_executable),
                f"--user-data-dir={self.profile_path}",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ]
            if stdout_path:
                self._chrome_stdout = stdout_path.open("wb")
            if stderr_path:
                self._chrome_stderr = stderr_path.open("wb")
            self._chrome_process = subprocess.Popen(
                self.chrome_command,
                stdin=subprocess.DEVNULL,
                stdout=self._chrome_stdout or subprocess.DEVNULL,
                stderr=self._chrome_stderr or subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            endpoint = await self._wait_for_devtools_endpoint(active_port, timeout_ms)
            self._playwright = await async_playwright().start()
            self.browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
            if len(self.browser.contexts) != 1:
                raise BrowserLaunchError("owned Chrome did not expose exactly one browser context")
            self.context = self.browser.contexts[0]
            self._owns_context = False
            self._attached_to_user_browser = False
        except ProfileInUseError:
            self.lock.release()
            raise
        except Exception as exc:
            await self._cleanup_owned_browser()
            self.lock.release()
            raise BrowserLaunchError(
                f"stable Chrome launch failed for profile {self.profile_path}: {type(exc).__name__}: {exc}"
            ) from exc

    async def _wait_for_devtools_endpoint(self, active_port: Path, timeout_ms: int) -> str:
        deadline = monotonic() + timeout_ms / 1000
        while monotonic() < deadline:
            if self._chrome_process and self._chrome_process.poll() is not None:
                raise BrowserLaunchError(f"Stable Chrome exited during startup with code {self._chrome_process.returncode}")
            if active_port.is_file():
                lines = active_port.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].isdigit():
                    self.browser_endpoint = f"http://127.0.0.1:{lines[0]}"
                    return self.browser_endpoint
            await asyncio.sleep(0.1)
        raise BrowserLaunchError(f"timed out waiting for Chrome DevToolsActivePort: {active_port}")

    async def attach_existing_cdp(self, endpoint: str) -> None:
        """Attach to user-authorized Chrome; read-only API remains the only public surface."""
        self.lock.acquire()
        try:
            self._playwright = await async_playwright().start()
            self.browser = await self._playwright.chromium.connect_over_cdp(endpoint)
            if len(self.browser.contexts) != 1:
                raise RuntimeError("expected exactly one existing Chrome context")
            self.context = self.browser.contexts[0]
            self._owns_context = False
            self._attached_to_user_browser = True
        except Exception:
            self.lock.release()
            if self._playwright:
                await self._playwright.stop()
            raise

    async def stop(self) -> None:
        close_error: Exception | None = None
        try:
            if self.context and self._owns_context:
                await self.context.close()
            if self.browser and not self._attached_to_user_browser:
                await self.browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:  # Cleanup must continue even when CDP disconnects mid-close.
            close_error = exc
        finally:
            await self._cleanup_owned_browser()
            self.context = None
            self.browser = None
            self._owns_context = False
            self._attached_to_user_browser = False
            self._playwright = None
            self.lock.release()
        if close_error:
            raise close_error

    async def _cleanup_owned_browser(self) -> None:
        process = self._chrome_process
        self._chrome_process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 10)
        for stream_name in ("_chrome_stdout", "_chrome_stderr"):
            stream = getattr(self, stream_name)
            if stream:
                stream.close()
                setattr(self, stream_name, None)

    async def open_read_only(self, url: str) -> Page:
        """Reader compatibility API. Application code uses ``open_hh_page`` via its gateway."""
        return await self.open_hh_page(url)

    async def open_hh_page(self, url: str) -> Page:
        """Open trusted HH route; this navigation never fills, clicks, sends or submits."""
        self._validate_hh_url(url)
        page = await self.new_hh_page()
        try:
            await self.navigate_hh_page(page, url)
        except Exception:
            await page.close()
            raise
        return page

    async def new_hh_page(self) -> Page:
        """Create an HH tab so read-only callers can install diagnostics before navigation."""
        if not self.context:
            raise RuntimeError("session not started")
        return await self.context.new_page()

    async def navigate_hh_page(self, page: Page, url: str) -> None:
        """Navigate existing tab to trusted HH route; never fills, clicks, sends or submits."""
        self._validate_hh_url(url)

        await page.goto(url, wait_until="commit", timeout=_DEFAULT_NAVIGATION_TIMEOUT_MS)

    @staticmethod
    def _validate_hh_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "about"}:
            raise ReadOnlyViolation(f"unsupported read-only route: {url}")
        if parsed.scheme == "https" and parsed.hostname not in {
            "hh.ru",
            "www.hh.ru",
            "chatik.hh.ru",
        }:
            raise ReadOnlyViolation(f"untrusted host: {parsed.hostname}")

    async def identify(self, page: Page) -> TargetIdentity:
        return TargetIdentity(url=page.url, title=await page.title())

    async def existing_hh_pages(self) -> list[Page]:
        """Return existing HH tabs without navigating or changing the user browser."""
        if not self.context:
            raise RuntimeError("session not started")
        return [
            page
            for page in self.context.pages
            if urlparse(page.url).hostname in {"hh.ru", "www.hh.ru", "chatik.hh.ru"}
        ]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _chrome_version_facts(text: str) -> dict[str, str]:
    """Parse only values exposed by the actual chrome://version page; absent values stay UNKNOWN."""
    facts = {
        "chrome_version": "UNKNOWN",
        "chrome_executable_path": "UNKNOWN",
        "chrome_reported_profile_path": "UNKNOWN",
        "chrome_command_line": "UNKNOWN",
    }
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    localized_labels = {
        "chrome_executable_path": ("Executable Path", "Путь к исполняемому файлу"),
        "chrome_reported_profile_path": ("Profile Path", "Путь к профилю"),
        "chrome_command_line": ("Command Line", "Командная строка"),
    }
    for index, normalized in enumerate(lines):
        chrome_match = re.match(r"^Google Chrome\s+(.+)$", normalized)
        if chrome_match:
            facts["chrome_version"] = chrome_match.group(1).strip() or "UNKNOWN"
        elif normalized == "Google Chrome" and index + 1 < len(lines):
            facts["chrome_version"] = lines[index + 1]
        for key, labels in localized_labels.items():
            label = next((candidate for candidate in labels if normalized.startswith(candidate)), None)
            if label:
                value = normalized.removeprefix(label).strip()
                facts[key] = value or (lines[index + 1] if index + 1 < len(lines) else "UNKNOWN")
                break
    return facts


async def _hh_auth_evidence(page: Page) -> dict[str, object]:
    """Classify only explicit visible login or applicant UI, never URL/non-empty DOM alone."""
    result = await page.evaluate(
        """() => {
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const textNodes = [...document.querySelectorAll('a,button,h1,h2,h3,label,span,p')]
            .filter(visible).map(el => (el.innerText || '').trim()).filter(Boolean);
          const hasText = values => values.filter(value => textNodes.some(text => text.includes(value)));
          const login = [];
          if ([...document.querySelectorAll('input[type="password"]')].some(visible)) login.push('visible_password_input');
          if (hasText(['Войти', 'Вход']).length) login.push('visible_login_text');
          if ([...document.querySelectorAll('form')].some(form => visible(form) && /login|auth/i.test(form.action || ''))) login.push('visible_login_form_action');
          const authenticated = [];
          if (hasText(['Мои резюме', 'Создать резюме', 'Настроить видимость']).length) authenticated.push('visible_applicant_resume_text');
          if ([...document.querySelectorAll('a[href*="/applicant/resumes/"]')].some(visible)) authenticated.push('visible_resume_detail_link');
          return {login_indicators: [...new Set(login)], authenticated_indicators: [...new Set(authenticated)]};
        }"""
    )
    login = list(result.get("login_indicators") or [])
    authenticated = list(result.get("authenticated_indicators") or [])
    if login and authenticated:
        state = "UNKNOWN"
    elif login:
        state = "AUTH_REQUIRED"
    elif authenticated:
        state = "AUTHENTICATED"
    else:
        state = "UNKNOWN"
    return {"login_indicators": login, "authenticated_indicators": authenticated, "hh_auth_state": state}


async def run_browser_acceptance(
    root: Path,
    *,
    run_label: str,
    target_url: str = "https://hh.ru/applicant/resumes",
    execution_command: str = "UNKNOWN",
    wait_for_auth_seconds: int = 0,
) -> dict[str, object]:
    """Execute one owned AutoHunt browser lifecycle and persist concise runtime evidence."""
    if run_label not in {"run1", "run2"}:
        raise ValueError("run_label must be run1 or run2")
    if wait_for_auth_seconds < 0:
        raise ValueError("wait_for_auth_seconds must not be negative")

    report_dir = root / "reports" / "browser_runtime_final"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"{run_label}.json"
    stdout_path = report_dir / f"{run_label}_stdout.txt"
    stderr_path = report_dir / f"{run_label}_stderr.txt"
    chrome_stdout_path = report_dir / f"{run_label}_chrome_stdout.txt"
    chrome_stderr_path = report_dir / f"{run_label}_chrome_stderr.txt"
    screenshot_path = report_dir / f"{run_label}_hh.png"
    version_text_path = report_dir / f"{run_label}_chrome_version.txt"
    dom_path = report_dir / f"{run_label}_hh_dom.txt"
    commands_path = report_dir / "commands.txt"
    profile = automation_browser_profile(root)
    historical_path = report_dir / "historical_setup.json"
    historical_path.write_text(
        json.dumps(
            {
                "observed_fact": {
                    "source": str(root / "reports" / "browser_acceptance" / "run2.json"),
                    "previous_stable_chrome_executable": r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                    "previous_automation_user_data_dir": str(root / "var" / "profile"),
                    "previous_playwright_control": "launch_persistent_context; external CDP used=false",
                },
                "current_runtime": {
                    "automation_user_data_dir": str(profile),
                    "method": "AutoHunt starts Stable Chrome with --remote-debugging-port=0 and Playwright connects over the discovered local endpoint.",
                },
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    session = ReadOnlyHHSession(profile, root / "var" / "controller.lock")
    started_at = _utc_now()
    started_monotonic = monotonic()
    stdout_lines = [f"started_at_utc={started_at}", f"execution_command={execution_command}"]
    stderr_lines: list[str] = []
    facts: dict[str, object] = {
        "run_id": f"browser-acceptance-{run_label}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}",
        "started_at_utc": started_at,
        "finished_at_utc": "UNKNOWN",
        "duration_monotonic_seconds": None,
        "launch_method": "subprocess Stable Chrome --remote-debugging-port=0 then playwright.chromium.connect_over_cdp",
        "playwright_channel": "chrome",
        "actual_user_data_dir": str(profile),
        "actual_profile_path": "UNKNOWN",
        "historical_setup_evidence": str(historical_path),
        "chrome_version": "UNKNOWN",
        "chrome_executable_path": "UNKNOWN",
        "chrome_reported_profile_path": "UNKNOWN",
        "chrome_command_line": "UNKNOWN",
        "debugging_endpoint": "UNKNOWN",
        "chrome_version_evidence": str(version_text_path),
        "target_url": target_url,
        "final_url": "UNKNOWN",
        "page_title": "UNKNOWN",
        "positive_authenticated_indicators": [],
        "login_indicators": [],
        "auth_detector_conflict": False,
        "hh_auth_state": "UNKNOWN",
        "startup_duration_seconds": None,
        "navigation_duration_seconds": None,
        "external_cdp_used": False,
        "playwright_connected": False,
        "secondary_hh_read": None,
        "profile_in_use": False,
        "exception_type": None,
        "exception_message": None,
    }
    hh_page: Page | None = None
    version_page: Page | None = None
    try:
        launch_started = monotonic()
        await session.start(
            timeout_ms=_DEFAULT_STARTUP_TIMEOUT_MS,
            stdout_path=chrome_stdout_path,
            stderr_path=chrome_stderr_path,
        )
        facts["startup_duration_seconds"] = round(monotonic() - launch_started, 6)
        stdout_lines.append("stable_chrome_launched=true")
        if not session.context:
            raise RuntimeError("stable Chrome launched without a browser context")
        facts["playwright_connected"] = True
        facts["chrome_executable_path"] = str(session.chrome_executable) if session.chrome_executable else "UNKNOWN"
        facts["chrome_command_line"] = session.chrome_command
        facts["debugging_endpoint"] = session.browser_endpoint or "UNKNOWN"

        version_page = await session.context.new_page()
        await version_page.goto(_CHROME_VERSION_URL, wait_until="load", timeout=_DEFAULT_NAVIGATION_TIMEOUT_MS)
        version_text = await version_page.locator("body").inner_text(timeout=_DEFAULT_NAVIGATION_TIMEOUT_MS)
        version_text_path.write_text(version_text + "\n", encoding="utf-8", newline="\n")
        version_facts = _chrome_version_facts(version_text)
        facts["chrome_version"] = version_facts["chrome_version"]
        facts["chrome_reported_profile_path"] = version_facts["chrome_reported_profile_path"]
        facts["actual_profile_path"] = facts["chrome_reported_profile_path"]
        stdout_lines.append("chrome_version_observed=true")
        await version_page.close()
        version_page = None

        navigation_started = monotonic()
        hh_page = await session.open_read_only(target_url)
        body = await rendered_body(hh_page, timeout_ms=_DEFAULT_NAVIGATION_TIMEOUT_MS)
        facts["navigation_duration_seconds"] = round(monotonic() - navigation_started, 6)
        facts["final_url"] = hh_page.url
        facts["page_title"] = await hh_page.title()
        auth_evidence = await _hh_auth_evidence(hh_page)
        facts["positive_authenticated_indicators"] = auth_evidence["authenticated_indicators"]
        facts["login_indicators"] = auth_evidence["login_indicators"]
        facts["auth_detector_conflict"] = bool(auth_evidence["authenticated_indicators"] and auth_evidence["login_indicators"])
        facts["hh_auth_state"] = auth_evidence["hh_auth_state"]
        await hh_page.screenshot(path=str(screenshot_path))
        dom_path.write_text(body[:4_000] + "\n", encoding="utf-8", newline="\n")
        stdout_lines.append(f"hh_final_url={hh_page.url}")
        stdout_lines.append(f"hh_auth_state={facts['hh_auth_state']}")
        if facts["hh_auth_state"] == "AUTH_REQUIRED" and wait_for_auth_seconds:
            stdout_lines.append(f"waiting_for_manual_hh_login_seconds={wait_for_auth_seconds}")
            deadline = monotonic() + wait_for_auth_seconds
            while monotonic() < deadline:
                await hh_page.wait_for_timeout(1_000)
                body = await rendered_body(hh_page, timeout_ms=5_000)
                auth_evidence = await _hh_auth_evidence(hh_page)
                if auth_evidence["hh_auth_state"] != "AUTH_REQUIRED":
                    facts["final_url"] = hh_page.url
                    facts["page_title"] = await hh_page.title()
                    facts["positive_authenticated_indicators"] = auth_evidence["authenticated_indicators"]
                    facts["login_indicators"] = auth_evidence["login_indicators"]
                    facts["hh_auth_state"] = auth_evidence["hh_auth_state"]
                    await hh_page.screenshot(path=str(screenshot_path))
                    dom_path.write_text(body[:4_000] + "\n", encoding="utf-8", newline="\n")
                    stdout_lines.append(f"hh_auth_state_after_wait={facts['hh_auth_state']}")
                    break
        if facts["hh_auth_state"] == "AUTHENTICATED":
            secondary_page = await session.open_read_only("https://hh.ru/search/vacancy?text=%D0%BE%D0%BF%D0%B5%D1%80%D0%B0%D1%86%D0%B8%D0%BE%D0%BD%D0%BD%D1%8B%D0%B9+%D0%B0%D1%81%D1%81%D0%B8%D1%81%D1%82%D0%B5%D0%BD%D1%82")
            try:
                secondary_body = await rendered_body(secondary_page, timeout_ms=_DEFAULT_NAVIGATION_TIMEOUT_MS)
                facts["secondary_hh_read"] = {
                    "url": secondary_page.url,
                    "title": await secondary_page.title(),
                    "dom_excerpt": secondary_body[:1_000],
                }
                stdout_lines.append(f"secondary_hh_url={secondary_page.url}")
            finally:
                await secondary_page.close()
    except Exception as exc:  # Evidence must survive every browser/runtime failure.
        if isinstance(exc, ProfileInUseError):
            facts["profile_in_use"] = True
        facts["exception_type"] = type(exc).__name__
        facts["exception_message"] = str(exc)
        stderr_lines.append(f"{type(exc).__name__}: {exc}")
        stderr_lines.append(traceback.format_exc())
    finally:
        if version_page:
            try:
                await version_page.close()
            except Exception as exc:  # noqa: BLE001 - preserve primary failure in evidence
                stderr_lines.append(f"version_page_close {type(exc).__name__}: {exc}")
        if hh_page:
            try:
                await hh_page.close()
            except Exception as exc:  # noqa: BLE001 - preserve primary failure in evidence
                stderr_lines.append(f"hh_page_close {type(exc).__name__}: {exc}")
        try:
            await session.stop()
            stdout_lines.append("browser_closed=true")
        except Exception as exc:  # noqa: BLE001 - shutdown evidence must be explicit
            stderr_lines.append(f"browser_close {type(exc).__name__}: {exc}")
            if facts["exception_type"] is None:
                facts["exception_type"] = type(exc).__name__
                facts["exception_message"] = f"browser shutdown failed: {exc}"
        facts["finished_at_utc"] = _utc_now()
        facts["duration_monotonic_seconds"] = round(monotonic() - started_monotonic, 6)
        stdout_path.write_text("\n".join(stdout_lines) + "\n", encoding="utf-8", newline="\n")
        stderr_path.write_text("\n".join(stderr_lines) + ("\n" if stderr_lines else ""), encoding="utf-8", newline="\n")
        json_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        with commands_path.open("a", encoding="utf-8", newline="\n") as commands_file:
            commands_file.write(execution_command + "\n")
    return facts
