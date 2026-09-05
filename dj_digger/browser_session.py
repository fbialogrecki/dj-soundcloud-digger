"""The one Playwright lifecycle layer, shared by the store cart and the gate browser.

Both the Bandcamp cart and the Hypeddit fallback drive the same kind of
persistent Chromium profile, and each used to carry its own copy of the launch
error wording. This is the single place that knows how Chromium is started,
what its failures mean, and how to install it.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Event
from typing import Any

from .automation_errors import AutomationError, ChromiumMissing
from .paths import data_dir

LOGGER = logging.getLogger(__name__)

# Default Playwright action timeout for pages in a managed context.
ACTION_TIMEOUT_MS = 15_000

# What every persistent profile is launched with, headed or not.
LAUNCH_OPTIONS = {"locale": "en-US", "chromium_sandbox": True}


def profile_path(name: str) -> Path:
    """Create a private, persistent Chromium profile directory outside the repository."""

    path = data_dir() / name
    # mkdir's mode is masked by the umask and ignored when the directory already
    # exists, so the explicit chmod is what actually guarantees 0700.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def store_profile_path() -> Path:
    """Create the private, persistent Chromium profile outside the repository."""

    return profile_path("store-browser")


def require_display() -> None:
    """A headed Chromium needs somewhere to draw; say so in one sentence."""

    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        raise AutomationError("Store cart needs a desktop window; on WSL, enable WSLg")


def _require_chromium(playwright: Any) -> None:
    if not Path(playwright.chromium.executable_path).is_file():
        raise ChromiumMissing("Chromium is required for store carts")


def _profile_locked(exc: Exception) -> bool:
    message = str(exc).lower()
    return "singleton" in message or "user data directory is already in use" in message


def classify_launch_error(exc: Exception, *, subject: str = "the dedicated store browser") -> Exception:
    """Turn a Playwright launch failure into the error the user can act on."""

    if "executable doesn't exist" in str(exc).lower():
        return ChromiumMissing("Chromium is required for store carts")
    if _profile_locked(exc):
        return AutomationError(f"{subject} profile is already open in another process")
    detail = f"could not start {subject}"
    if sys.platform.startswith("linux"):
        detail += (
            "; install required system libraries with "
            f"'{sys.executable} -m playwright install --with-deps chromium'"
        )
    return AutomationError(detail)


def _cancelled(cancel: Event) -> None:
    if cancel.is_set():
        raise AutomationError("cart operation was cancelled")


def install_chromium(cancel: Event) -> None:
    """Download Playwright's matching Chromium build in the current environment."""

    _cancelled(cancel)
    popen_options = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "shell": False,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            **popen_options,
        )
    except OSError as exc:
        raise AutomationError(
            "could not start Chromium installation; run "
            f"'{sys.executable} -m playwright install chromium'"
        ) from exc
    while process.poll() is None:
        if not cancel.wait(0.1):
            continue
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except OSError:
            pass
        _cancelled(cancel)
    _cancelled(cancel)
    if process.returncode:
        raise AutomationError(
            "Chromium installation failed; run "
            f"'{sys.executable} -m playwright install chromium'"
        )


# Chromium releases its profile lock a moment after the previous context
# closed; a relaunch that hits that moment is retried rather than reported.
PROFILE_LOCK_ATTEMPTS = 5
PROFILE_LOCK_WAIT = 1.0


_USER_AGENT_PLATFORMS = {
    "win32": "Windows NT 10.0; Win64; x64",
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
}
_headed_user_agent: str | None = None


def headed_user_agent(playwright: Any) -> str:
    """What this Chromium calls itself in a window.

    Hidden, it says HeadlessChrome instead, and a provider login (Spotify's
    OAuth, say) may treat that differently from the browser the profile
    signed in with. Chrome reports only its major version, so the string is
    composed from a throwaway launch's version, once per process.
    """

    global _headed_user_agent
    if _headed_user_agent is None:
        browser = playwright.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            major = str(browser.version).split(".", 1)[0]
        finally:
            browser.close()
        platform = _USER_AGENT_PLATFORMS.get(sys.platform, "X11; Linux x86_64")
        _headed_user_agent = (
            f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36"
        )
    return _headed_user_agent


def _launch_options(playwright: Any, headless: bool) -> dict[str, Any]:
    options = dict(LAUNCH_OPTIONS)
    if headless:
        options["user_agent"] = headed_user_agent(playwright)
    return options


@contextmanager
def sync_browser_context(
    profile: Path | None = None, *, accept_downloads: bool = False, headless: bool = False
):
    """A persistent context for thread-side callers (gates, SoundCloud login).

    Headed unless asked otherwise; a hidden context needs no display. The
    gate browser closes a hidden context and reopens the same profile in a
    window, so the profile lock is retried the way the async twin retries it.
    """

    if not headless:
        require_display()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AutomationError(
            "the required Playwright dependency is missing; reinstall dj-digger"
        ) from exc

    with sync_playwright() as playwright:
        _require_chromium(playwright)
        for attempt in range(1, PROFILE_LOCK_ATTEMPTS + 1):
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile or store_profile_path()),
                    headless=headless,
                    accept_downloads=accept_downloads,
                    **_launch_options(playwright, headless),
                )
                break
            except Exception as exc:
                if _profile_locked(exc) and attempt < PROFILE_LOCK_ATTEMPTS:
                    LOGGER.debug("Browser profile still locked, retrying (%d)", attempt)
                    time.sleep(PROFILE_LOCK_WAIT)
                    continue
                raise classify_launch_error(exc) from exc
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        try:
            yield context
        finally:
            with suppress(Exception):
                context.close()


async def launch_persistent_context(
    playwright: Any,
    profile: Path | None = None,
    *,
    headless: bool = True,
) -> Any:
    """The async twin, for the cart session on Textual's loop.

    Headless by default: the store work happens out of sight, and a window is
    opened separately (see ``launch_viewer``) only when there is something to
    show the user.
    """

    if not headless:
        require_display()
    _require_chromium(playwright)
    for attempt in range(1, PROFILE_LOCK_ATTEMPTS + 1):
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile or store_profile_path()),
                headless=headless,
                accept_downloads=False,
                **LAUNCH_OPTIONS,
            )
            break
        except Exception as exc:
            if _profile_locked(exc) and attempt < PROFILE_LOCK_ATTEMPTS:
                LOGGER.debug("Store profile still locked, retrying (%d)", attempt)
                await asyncio.sleep(PROFILE_LOCK_WAIT)
                continue
            raise classify_launch_error(exc) from exc
    context.set_default_timeout(ACTION_TIMEOUT_MS)
    return context


async def launch_viewer(playwright: Any, cookies: list[dict[str, Any]]) -> tuple[Any, Any]:
    """A visible browser carrying the hidden session's cookies.

    The persistent profile can only be open once, and switching it between
    headless and headed raced its lock; a separate browser with the same
    cookies shows the same cart with none of that. Returns (browser, context).
    """

    require_display()
    _require_chromium(playwright)
    try:
        browser = await playwright.chromium.launch(headless=False, chromium_sandbox=True)
        context = await browser.new_context(locale="en-US", accept_downloads=False)
        if cookies:
            await context.add_cookies(cookies)
    except Exception as exc:
        raise classify_launch_error(exc, subject="the store browser window") from exc
    context.set_default_timeout(ACTION_TIMEOUT_MS)
    return browser, context
