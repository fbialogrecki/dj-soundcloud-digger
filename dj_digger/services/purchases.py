"""Safe, user-initiated store cart automation."""

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dj_digger.automation_errors import AutomationError

from ..beatport_playlist import _beatport_playlist_result
from ..browser_session import launch_persistent_context, launch_viewer
from ..cart_models import (
    MANUAL_AFTER_UNVERIFIED,
    MANUAL_TABS_MAX,
    VERIFY_BUDGET_SECONDS,
    ApprovalCallback,
    CartBatchOutcome,
    CartCancelled,
    CartItem,
    CartPlan,
    CartProgress,
    CartRequest,
    CartResult,
    CartResultCode,
    CartStatus,
    CartUnverified,
    ManualCallback,
    ProductUnavailable,
    ProgressCallback,
    SecurityChallengeBlocked,
    StoreStructureError,
    UnsafeMatch,
    UnsafeRedirect,
    UserActionTimeout,
    _display_text,
)
from ..links import redact_url
from ..paths import data_dir
from ..store_urls import (
    STORE_HOME,
    STORE_HOSTS,
    _direct_beatport_track_url,
    canonical_store_url,
)
from ..stores import bandcamp

LOGGER = logging.getLogger(__name__)
NAVIGATION_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 15_000
LOGIN_TIMEOUT_SECONDS = 300
BANDCAMP_CART_URL = "https://bandcamp.com/cart"




def _cancelled_result(key: str, label: str, store: str) -> CartResult:
    return CartResult(key, label, store, "failed", "cart operation was cancelled", "cancelled")


BEATPORT_BY_TITLE = "Beatport will match this track by artist and title"


# How a failed preflight lookup is reported, by its most specific exception
# type; anything not listed is an unexpected browser failure.
_PREFLIGHT_FAILURES: dict[type[Exception], tuple[CartStatus, CartResultCode]] = {
    UnsafeMatch: ("skipped", "unsafe_match"),
    UnsafeRedirect: ("failed", "unsafe_redirect"),
    SecurityChallengeBlocked: ("failed", "browser_failure"),
    UserActionTimeout: ("failed", "user_action_timeout"),
    StoreStructureError: ("failed", "store_structure"),
    CartCancelled: ("failed", "cancelled"),
    AutomationError: ("failed", "browser_failure"),
}


def _preflight_failure(
    request: CartRequest, label: str, store: str, url: str, exc: Exception
) -> CartResult:
    """The result for one link that could not be resolved.

    Beatport is never a failure: its lookup is best effort and the playlist
    matches by artist and title instead - unless the person stopped the batch
    or a manual step timed out, which end every store the same way.
    """

    if store == "beatport" and not isinstance(exc, (UserActionTimeout, CartCancelled)):
        reason = str(exc) if isinstance(exc, SecurityChallengeBlocked) else BEATPORT_BY_TITLE
        # A link that redirected off Beatport is not one worth keeping.
        kept_url = "" if isinstance(exc, UnsafeRedirect) else url
        return _beatport_playlist_result(request, label, reason, kept_url)
    spec = next(
        (_PREFLIGHT_FAILURES[cls] for cls in type(exc).__mro__ if cls in _PREFLIGHT_FAILURES),
        None,
    )
    if spec is None:
        return CartResult(
            request.track.key,
            label,
            store,
            "failed",
            "unexpected store interaction failure",
            "browser_failure",
        )
    status, code = spec
    shown_url = ""
    if isinstance(exc, SecurityChallengeBlocked):
        shown_url = canonical_store_url(url, store) or ""
    return CartResult(request.track.key, label, store, status, str(exc), code, shown_url)


class _StructureFailures:
    """Stores whose pages keep losing their shape.

    The same structural error on two different tracks means the store, not
    the track, changed: it is marked broken and the rest of its links are
    reported without another lookup.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, set[str]]] = {}
        self.broken: set[str] = set()

    def record(self, store: str, signature: str, track_key: str) -> None:
        earlier, keys = self._seen.get(store, (signature, set()))
        keys = {track_key} if earlier != signature else keys | {track_key}
        self._seen[store] = (signature, keys)
        if len(keys) >= 2:
            self.broken.add(store)

    def clear(self, store: str) -> None:
        self._seen.pop(store, None)


def _split_direct_beatport(
    requests: tuple[CartRequest, ...],
) -> tuple[list[CartResult], list[CartRequest]]:
    """Beatport-only requests with a track URL need no browser: playlist entries at once."""

    direct: list[CartResult] = []
    pending: list[CartRequest] = []
    for request in requests:
        direct_url = (
            _direct_beatport_track_url(request.links[0][1])
            if len(request.links) == 1 and request.links[0][0] == "beatport"
            else None
        )
        if direct_url is None:
            pending.append(request)
            continue
        result = _beatport_playlist_result(
            request,
            _display_text(request.track.label),
            "ready for Beatport playlist transfer",
            direct_url,
        )
        direct.append(result)
        bandcamp._log_cart_result("playlist", result)
    return direct, pending


def _partition_outcomes(
    approved: CartPlan, results: list[CartResult]
) -> tuple[dict[str, list[CartItem]], dict[str, list[CartItem]]]:
    """Approved items by store: those in the cart, and those whose click is uncertain."""

    successful_keys = {
        (result.track_key, result.store)
        for result in results
        if result.status in {"added", "already_in_cart"}
    }
    uncertain_keys = {
        (result.track_key, result.store)
        for result in results
        if result.code == "cart_unverified"
    }
    successful: dict[str, list[CartItem]] = defaultdict(list)
    uncertain: dict[str, list[CartItem]] = defaultdict(list)
    for item in approved.items:
        target = (item.track_key, item.store)
        if target in successful_keys:
            successful[item.store].append(item)
        elif target in uncertain_keys:
            uncertain[item.store].append(item)
    return successful, uncertain


def _merge_manual(
    results: list[CartResult],
    settled: list[CartResult],
    successful: dict[str, list[CartItem]],
    uncertain: dict[str, list[CartItem]],
) -> list[CartResult]:
    """Fold the manual results in: settled items leave uncertain, verified ones join successful."""

    settled_keys = {(result.track_key, result.store) for result in settled}
    for store, items in list(uncertain.items()):
        uncertain[store] = [
            item for item in items if (item.track_key, item.store) not in settled_keys
        ]
        for item in items:
            if (item.track_key, item.store) in settled_keys and any(
                result.track_key == item.track_key and result.code == "manual_verified"
                for result in settled
            ):
                successful[item.store].append(item)
    return [
        result for result in results if (result.track_key, result.store) not in settled_keys
    ] + settled


def _log_batch_summary(outcome: CartBatchOutcome) -> None:
    counts: dict[str, int] = defaultdict(int)
    for result in outcome.results:
        counts[result.status] += 1
    LOGGER.info(
        "Cart batch finished: added=%d already=%d playlist=%d skipped=%d "
        "failed=%d carts=%s",
        counts["added"],
        counts["already_in_cart"],
        counts["playlist_ready"],
        counts["skipped"],
        counts["failed"],
        ",".join(outcome.cart_stores) or "none",
    )


async def _manual_result(
    item: CartItem, page: Any, done: bool, cancel: asyncio.Event
) -> CartResult:
    """One read-only cart check decides what the person's own click achieved."""

    if not done or cancel.is_set():
        return CartResult(
            item.track_key,
            item.track_label,
            item.store,
            "failed",
            "manual completion was given up",
            "cart_unverified",
        )
    try:
        present = await bandcamp._cart_contains_async(page, item, asyncio.Event())
    except Exception:
        present = False
    return CartResult(
        item.track_key,
        item.track_label,
        item.store,
        "manual" if present else "failed",
        "added by hand in the browser" if present else "not found in the cart after manual completion",
        "manual_verified" if present else "manual_unverified",
    )


class CartBrowserSession:
    """One lazy Playwright context shared by all cart batches in a TUI run.

    The work - product lookup, revalidation, the cart clicks - runs headless on
    the persistent profile, out of the user's way. A window opens only when
    there is something for them to do or see: the finished cart, or items to
    finish by hand. That window is a separate browser carrying the session's
    cookies (``browser_session.launch_viewer``), because relaunching the one
    profile from headless to headed raced Chromium's lock and lost batches.
    """

    def __init__(self, profile: Path | None = None) -> None:
        self.profile = profile
        self._playwright = None
        self._context = None
        # The visible browser and its context, once something needed showing.
        self._viewer: tuple[Any, Any] | None = None
        self._owned_pages: list[Any] = []
        self._cart_pages: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def _context_closed(self, _context: Any) -> None:
        self._context = None
        self._owned_pages.clear()
        self._cart_pages.clear()

    async def _playwright_handle(self) -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise AutomationError(
                "the required Playwright dependency is missing; reinstall dj-digger"
            ) from exc
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    async def _ensure_context(self) -> Any:
        if self._context is not None and not self._context.is_closed():
            return self._context
        playwright = await self._playwright_handle()
        context = await launch_persistent_context(playwright, self.profile, headless=True)
        context.on("close", self._context_closed)
        self._context = context
        self._owned_pages = list(context.pages[:1])
        for page in self._owned_pages:
            self._instrument_page(page)
        LOGGER.info("Store browser session ready: pages=%d", len(self._owned_pages))
        return context

    def _instrument_page(self, page: Any) -> Any:
        def response_received(response: Any) -> None:
            status = getattr(response, "status", 0)
            if status >= 400:
                LOGGER.debug(
                    "Store browser HTTP error: status=%s url=%s",
                    status,
                    redact_url(getattr(response, "url", "")),
                )

        def console_message(message: Any) -> None:
            kind = getattr(message, "type", "")
            if kind not in {"error", "warning"}:
                return
            location = getattr(message, "location", {}) or {}
            LOGGER.debug(
                "Store browser console message: type=%s source=%s",
                kind,
                redact_url(str(location.get("url") or "")),
            )

        try:
            page.on("response", response_received)
            page.on("console", console_message)
            page.on(
                "crash",
                lambda *_args: LOGGER.warning("Store browser page crashed"),
            )
        except Exception:
            # Minimal fake pages in tests do not implement Playwright events.
            pass
        return page

    async def _viewer_context(self) -> Any:
        """The visible browser context, opened on first need with the session's cookies."""

        if self._viewer is not None:
            browser, context = self._viewer
            try:
                if browser.is_connected():
                    return context
            except Exception:
                pass
            self._viewer = None
        cookies: list[dict[str, Any]] = []
        if self._context is not None and not self._context.is_closed():
            try:
                cookies = await self._context.cookies()
            except Exception:
                cookies = []
        playwright = await self._playwright_handle()
        browser, context = await launch_viewer(playwright, cookies)
        self._viewer = (browser, context)
        try:
            browser.on("disconnected", lambda *_args: setattr(self, "_viewer", None))
        except Exception:
            pass
        return context

    async def _close_viewer(self) -> None:
        viewer, self._viewer = self._viewer, None
        self._cart_pages.clear()
        if viewer is None:
            return
        browser, _context = viewer
        with contextlib.suppress(Exception):
            await browser.close()

    async def _work_pages(self, count: int = 2) -> list[Any]:
        context = await self._ensure_context()
        pages = [page for page in self._owned_pages if not page.is_closed()]
        while len(pages) < count:
            page = self._instrument_page(await context.new_page())
            pages.append(page)
        self._owned_pages = pages
        return pages[:count]

    async def _replace_page(self, old: Any) -> Any:
        context = await self._ensure_context()
        with contextlib.suppress(Exception):
            await old.close()
        new = self._instrument_page(await context.new_page())
        self._owned_pages = [new if page is old else page for page in self._owned_pages]
        return new

    async def _close_context(self) -> None:
        """Close the browser window; Playwright itself stays up for the next one."""

        context, self._context = self._context, None
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close(reason="dj-digger cart session closed")
        self._owned_pages.clear()
        self._cart_pages.clear()

    async def close(self) -> None:
        async with self._lock:
            await self._close_context()
            await self._close_viewer()
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None

    async def reset_profile(self) -> None:
        async with self._lock:
            await self._close_context()
            target = Path(self.profile) if self.profile else data_dir() / "store-browser"
            parent = data_dir().resolve()
            if target.name != "store-browser" or target.parent.resolve() != parent:
                raise AutomationError("refusing to reset an unexpected browser profile path")
            if target.is_symlink():
                raise AutomationError("refusing to reset a symlinked browser profile")
            if not target.exists():
                return
            quarantine = target.with_name(f".store-browser-reset-{os.getpid()}-{time.time_ns()}")
            target.rename(quarantine)
            try:
                shutil.rmtree(quarantine)
            except Exception:
                quarantine.rename(target)
                raise
            target.mkdir(parents=True, mode=0o700)
            if os.name != "nt":
                target.chmod(0o700)

    async def setup_logins(
        self,
        stores: Iterable[str],
        cancel: asyncio.Event,
        progress: ProgressCallback | None = None,
    ) -> None:
        wanted = tuple(dict.fromkeys(store for store in stores if store in STORE_HOSTS))
        if not wanted:
            return
        async with self._lock:
            # A login needs the real profile on screen, so the hidden context
            # steps aside and the profile is opened headed for as long as the
            # login takes; the next batch reopens it hidden.
            await self._close_context()
            playwright = await self._playwright_handle()
            context = await launch_persistent_context(playwright, self.profile, headless=False)
            pages = [self._instrument_page(await context.new_page()) for _ in wanted]
            try:
                await bandcamp._ensure_logins_async(
                    dict(zip(wanted, pages, strict=True)), cancel, progress
                )
            finally:
                with contextlib.suppress(Exception):
                    await context.close(reason="dj-digger login finished")

    async def check_logins(self, stores: Iterable[str]) -> dict[str, bool]:
        wanted = tuple(dict.fromkeys(store for store in stores if store in STORE_HOSTS))
        if not wanted:
            return {}
        async with self._lock:
            context = await self._ensure_context()
            pages = [self._instrument_page(await context.new_page()) for _ in wanted]
            try:
                states: dict[str, bool] = {}
                for store, page in zip(wanted, pages, strict=True):
                    await bandcamp._navigate_async(page, STORE_HOME[store], store)
                    states[store] = await bandcamp._is_logged_in_async(page, store)
                return states
            finally:
                for page in pages:
                    if not page.is_closed():
                        await page.close()

    async def _preflight_one(
        self,
        page: Any,
        request: CartRequest,
        cancel: asyncio.Event,
        failures: _StructureFailures,
    ) -> tuple[CartItem | CartResult, Any]:
        """Resolve one request over its links; the first eligible store wins.

        Returns the item, or the result that stands in for it, and the page to
        keep working on: a page that lost its shape is swapped for a fresh one.
        """

        label = _display_text(request.track.label)
        if cancel.is_set():
            store = request.links[0][0] if request.links else ""
            return _cancelled_result(request.track.key, label, store), page
        unavailable: list[str] = []
        for store, url in request.links:
            if store in failures.broken:
                if store == "beatport":
                    return _beatport_playlist_result(request, label, BEATPORT_BY_TITLE, url), page
                return CartResult(
                    request.track.key,
                    label,
                    store,
                    "failed",
                    "store automation stopped after repeated structural failures",
                    "store_structure",
                ), page
            try:
                try:
                    item = await bandcamp._resolve_cart_item_async(
                        page, request.track, store, url, cancel
                    )
                except StoreStructureError:
                    await bandcamp.save_cart_diagnostics(page, store, url, "store_structure")
                    page = await self._replace_page(page)
                    item = await bandcamp._resolve_cart_item_async(
                        page, request.track, store, url, cancel
                    )
            except ProductUnavailable as exc:
                if store == "beatport":
                    return _beatport_playlist_result(request, label, BEATPORT_BY_TITLE, url), page
                unavailable.append(str(exc))
                continue
            except Exception as exc:
                if not isinstance(exc, (UnsafeMatch, AutomationError)):
                    LOGGER.error(
                        "Unexpected cart preflight error: store=%s track=%r error=%s",
                        store,
                        label,
                        type(exc).__name__,
                    )
                elif isinstance(exc, StoreStructureError) and store != "beatport":
                    failures.record(store, str(exc), request.track.key)
                return _preflight_failure(request, label, store, url, exc), page
            failures.clear(store)
            return item, page
        return CartResult(
            request.track.key,
            label,
            request.links[-1][0] if request.links else "",
            "skipped",
            unavailable[-1] if unavailable else "no eligible Bandcamp or Beatport link",
            "unavailable",
        ), page

    async def _preflight(
        self,
        requests: tuple[CartRequest, ...],
        pages: list[Any],
        cancel: asyncio.Event,
        progress: ProgressCallback | None,
    ) -> CartPlan:
        queue: asyncio.Queue[tuple[int, CartRequest] | None] = asyncio.Queue()
        for index, request in enumerate(requests):
            queue.put_nowait((index, request))
        for _ in pages:
            queue.put_nowait(None)

        outcomes: list[CartItem | CartResult | None] = [None] * len(requests)
        failures = _StructureFailures()
        completed = 0

        async def worker(worker_index: int, page: Any) -> None:
            nonlocal completed
            while (entry := await queue.get()) is not None:
                index, request = entry
                try:
                    outcome, page = await self._preflight_one(page, request, cancel, failures)
                    pages[worker_index] = page
                    outcomes[index] = outcome
                    if isinstance(outcome, CartResult):
                        bandcamp._log_cart_result("preflight", outcome)
                    else:
                        LOGGER.info(
                            "Cart preflight ready: store=%s track=%r product=%s price=%s %s",
                            outcome.store,
                            outcome.track_label,
                            redact_url(outcome.product_url),
                            outcome.price,
                            outcome.currency,
                        )
                finally:
                    completed += 1
                    bandcamp._emit_progress(
                        progress,
                        CartProgress(
                            "preflight",
                            completed,
                            len(requests),
                            track_label=_display_text(request.track.label),
                        ),
                    )
                    queue.task_done()
            queue.task_done()

        async with asyncio.TaskGroup() as group:
            for index, page in enumerate(pages):
                group.create_task(worker(index, page))
        return CartPlan(
            tuple(outcome for outcome in outcomes if isinstance(outcome, CartItem)),
            tuple(outcome for outcome in outcomes if isinstance(outcome, CartResult)),
        )

    async def _execute_store(
        self,
        store: str,
        items: list[CartItem],
        page: Any,
        cancel: asyncio.Event,
        progress: ProgressCallback | None,
        progress_state: list[int],
        total: int,
    ) -> list[CartResult]:
        results: list[CartResult] = []
        unverified = 0
        clicked = False

        def _failed(item: CartItem, reason: str, code: CartResultCode) -> CartResult:
            return CartResult(item.track_key, item.track_label, store, "failed", reason, code)

        async def _click_and_verify(
            item: CartItem, ready: CartItem, count_before: int | None
        ) -> CartResult:
            nonlocal clicked, unverified
            await bandcamp._add_to_cart_async(page, ready, cancel)
            clicked = True
            outcome = await asyncio.wait_for(
                bandcamp._verify_bandcamp_click_async(page, ready, count_before),
                timeout=VERIFY_BUDGET_SECONDS,
            )
            if outcome.verified:
                return CartResult(item.track_key, item.track_label, store, "added")
            unverified += 1
            await bandcamp.save_cart_diagnostics(page, store, item.product_url, "cart_unverified")
            return _failed(
                item,
                f"cart click was not verified (gave up at the {outcome.stage} "
                f"stage after {outcome.elapsed:.0f}s); it was not retried",
                "cart_unverified",
            )

        for index, item in enumerate(items):
            if cancel.is_set():
                results.extend(
                    _cancelled_result(pending.track_key, pending.track_label, store)
                    for pending in items[index:]
                )
                break
            if unverified >= MANUAL_AFTER_UNVERIFIED:
                # Two clicks this store would not confirm: stop clicking. The
                # rest go to the person at the window (see _finish_manually).
                results.extend(
                    _failed(
                        pending,
                        "left for manual completion after repeated unverified clicks",
                        "cart_unverified",
                    )
                    for pending in items[index:]
                )
                break
            clicked = False
            try:
                current = await bandcamp._revalidated(
                    page, item, cancel, "product identity or price changed after preflight"
                )
                if isinstance(current, CartResult):
                    results.append(current)
                    continue
                if await bandcamp._cart_contains_async(page, current, cancel):
                    results.append(
                        CartResult(item.track_key, item.track_label, store, "already_in_cart")
                    )
                    continue
                ready = await bandcamp._revalidated(
                    page, item, cancel, "product identity or price changed after cart inspection"
                )
                if isinstance(ready, CartResult):
                    results.append(ready)
                    continue
                count_before = (
                    await bandcamp._bandcamp_cart_count_async(page) if store == "bandcamp" else None
                )
                results.append(await _click_and_verify(item, ready, count_before))
            except CartUnverified as exc:
                unverified += 1
                await bandcamp.save_cart_diagnostics(page, store, item.product_url, "cart_unverified")
                results.append(_failed(item, str(exc), "cart_unverified"))
            except CartCancelled:
                if clicked:
                    results.append(_failed(item, "cart state is uncertain", "cart_unverified"))
                else:
                    results.append(_cancelled_result(item.track_key, item.track_label, store))
            except UnsafeRedirect as exc:
                results.append(_failed(item, str(exc), "unsafe_redirect"))
            except AutomationError as exc:
                code = "cart_unverified" if clicked else "store_structure"
                results.append(_failed(item, str(exc), code))
            except Exception as exc:
                LOGGER.error(
                    "Unexpected cart execution error: store=%s track=%r error=%s",
                    store,
                    item.track_label,
                    type(exc).__name__,
                )
                code = "cart_unverified" if clicked else "browser_failure"
                results.append(_failed(item, "unexpected store interaction failure", code))
            finally:
                if results and results[-1].track_key == item.track_key:
                    bandcamp._log_cart_result("execution", results[-1])
                progress_state[0] += 1
                bandcamp._emit_progress(
                    progress,
                    CartProgress(
                        "adding",
                        progress_state[0],
                        total,
                        store,
                        item.track_label,
                    ),
                )
        return results

    async def _show_store_cart(
        self, store: str, items: list[CartItem], verified: list[CartItem]
    ) -> tuple[Any, list[CartItem]]:
        """A visible page on the store's cart, and the verified items it fails to show."""

        # Shown in the visible browser, which carries the session's cookies;
        # the hidden context stays as it is.
        viewer = await self._viewer_context()
        page = self._instrument_page(await viewer.new_page())
        await bandcamp._navigate_async(page, BANDCAMP_CART_URL, store)
        missing: list[CartItem] = []

        async def all_shown() -> bool:
            nonlocal missing
            present = await asyncio.gather(
                *(bandcamp._bandcamp_cart_contains_async(page, item) for item in verified)
            )
            missing = [
                item for item, found in zip(verified, present, strict=True) if not found
            ]
            return not missing

        await bandcamp._poll_async(all_shown, 3.0)
        return page, missing

    async def _open_final_carts(
        self,
        successful: dict[str, list[CartItem]],
        keep_open: dict[str, list[CartItem]] | None = None,
    ) -> tuple[tuple[str, ...], tuple[CartResult, ...]]:
        opened: list[str] = []
        warnings: list[CartResult] = []
        self._cart_pages.clear()
        targets: dict[str, list[CartItem]] = defaultdict(list)
        for source in (successful, keep_open or {}):
            for store, items in source.items():
                for item in items:
                    if item not in targets[store]:
                        targets[store].append(item)
        for store, items in targets.items():
            if not items:
                continue
            try:
                page, missing = await self._show_store_cart(
                    store, items, successful.get(store, [])
                )
                if missing:
                    warnings.append(
                        CartResult(
                            "",
                            "Bandcamp cart view",
                            store,
                            "failed",
                            f"final cart view did not expose {len(missing)} verified item(s)",
                            "cart_view_incomplete",
                        )
                    )
                await page.bring_to_front()
            except Exception as exc:
                LOGGER.warning(
                    "Could not expose final cart: store=%s error=%s",
                    store,
                    type(exc).__name__,
                )
                warnings.append(
                    CartResult(
                        "",
                        f"{store.capitalize()} cart view",
                        store,
                        "failed",
                        "the cart window could not be shown; the additions above still stand",
                        "cart_view_failed",
                    )
                )
                continue
            self._cart_pages[store] = page
            opened.append(store)
        return tuple(opened), tuple(warnings)

    async def run_batch(
        self,
        requests: Iterable[CartRequest],
        cancel: asyncio.Event,
        *,
        approve: ApprovalCallback,
        progress: ProgressCallback | None = None,
        manual: ManualCallback | None = None,
    ) -> CartBatchOutcome:
        request_list = tuple(requests)
        async with self._lock:
            LOGGER.info("Cart batch started: tracks=%d", len(request_list))
            bandcamp._emit_progress(progress, CartProgress("starting", 0, len(request_list)))
            direct_results, pending_requests = _split_direct_beatport(request_list)
            if not pending_requests:
                bandcamp._emit_progress(
                    progress,
                    CartProgress("ready", len(request_list), len(request_list)),
                )
                return CartBatchOutcome(tuple(direct_results))
            pages = await self._work_pages(2)
            try:
                plan = await self._preflight(
                    tuple(pending_requests), pages, cancel, progress
                )
            except UserActionTimeout as exc:
                return CartBatchOutcome(
                    tuple(direct_results) + tuple(
                        CartResult(
                            request.track.key,
                            request.track.label,
                            request.links[0][0] if request.links else "",
                            "failed",
                            str(exc),
                            "user_action_timeout",
                        )
                        for request in pending_requests
                    )
                )
            plan = CartPlan(plan.items, tuple(direct_results) + plan.results)
            if cancel.is_set():
                cancelled = tuple(
                    _cancelled_result(item.track_key, item.track_label, item.store)
                    for item in plan.items
                )
                return CartBatchOutcome(plan.results + cancelled, cancelled=True)
            if not plan.items:
                LOGGER.info(
                    "Cart batch stopped after preflight: ready=0 results=%d",
                    len(plan.results),
                )
                return CartBatchOutcome(plan.results)
            LOGGER.info(
                "Cart preflight completed: ready=%d results=%d",
                len(plan.items),
                len(plan.results),
            )
            bandcamp._emit_progress(progress, CartProgress("approval", 0, len(plan.items)))
            approved = await approve(plan)
            if approved is None or cancel.is_set():
                LOGGER.info("Cart batch approval cancelled")
                return CartBatchOutcome(plan.results, cancelled=True)
            LOGGER.info("Cart plan approved: items=%d", len(approved.items))

            by_store: dict[str, list[CartItem]] = defaultdict(list)
            for item in approved.items:
                by_store[item.store].append(item)
            store_pages = {
                store: pages[index]
                for index, store in enumerate(by_store)
            }
            progress_state = [0]
            all_results = list(approved.results)
            if "beatport" in by_store:
                playlist_items = [
                    CartResult(
                        item.track_key,
                        item.track_label,
                        "beatport",
                        "playlist_ready",
                        "ready for Beatport playlist transfer",
                        "playlist_ready",
                        item.product_url,
                    )
                    for item in by_store.pop("beatport")
                ]
                all_results.extend(playlist_items)
                for result in playlist_items:
                    bandcamp._log_cart_result("playlist", result)
            tasks = [
                self._execute_store(
                    store,
                    items,
                    store_pages[store],
                    cancel,
                    progress,
                    progress_state,
                    len(approved.items),
                )
                for store, items in by_store.items()
            ]
            for store_results in await asyncio.gather(*tasks):
                all_results.extend(store_results)
            successful, uncertain = _partition_outcomes(approved, all_results)
            if uncertain and manual is not None and not cancel.is_set():
                settled = await self._finish_manually(uncertain, manual, cancel, progress)
                all_results = _merge_manual(all_results, settled, successful, uncertain)
            opened, warnings = await self._open_final_carts(successful, uncertain)
            all_results.extend(warnings)
            bandcamp._emit_progress(progress, CartProgress("ready", len(approved.items), len(approved.items)))
            candidates = tuple(item for items in uncertain.values() for item in items)
            outcome = CartBatchOutcome(tuple(all_results), opened, cancel.is_set(), candidates)
            _log_batch_summary(outcome)
            return outcome

    async def _stage_manual_page(self, context: Any, item: CartItem) -> Any:
        """A visible tab on the product, Buy control expanded and the price filled in."""

        page = self._instrument_page(await context.new_page())
        try:
            await bandcamp._navigate_async(page, item.product_url, item.store)
            await bandcamp._dismiss_bandcamp_cookie_banner(page)
            price_input = await bandcamp._expand_buy_async(page, force=True)
            if price_input is not None:
                await price_input.fill(format(item.price, "f"), timeout=ACTION_TIMEOUT_MS)
        except Exception as exc:
            LOGGER.debug(
                "Manual staging could not prepare %r: %s", item.track_label, type(exc).__name__
            )
        return page

    async def _finish_manually(
        self,
        uncertain: dict[str, list[CartItem]],
        manual: ManualCallback,
        cancel: asyncio.Event,
        progress: ProgressCallback | None,
    ) -> list[CartResult]:
        """Open the unverified products for the person at the window, then check.

        Each page gets its Buy control expanded and the price filled, exactly
        as preflight does; the Add-to-cart click is theirs. Once they say they
        are done, one read-only cart check per item decides the result.
        """

        items = [item for store_items in uncertain.values() for item in store_items]
        items = items[:MANUAL_TABS_MAX]
        if not items:
            return []
        bandcamp._emit_progress(progress, CartProgress("manual", 0, len(items)))
        try:
            context = await self._viewer_context()
        except AutomationError as exc:
            return [
                CartResult(item.track_key, item.track_label, item.store, "failed",
                           f"could not open a browser window: {exc}", "cart_unverified")
                for item in items
            ]
        staged: list[tuple[CartItem, Any]] = []
        for item in items:
            if cancel.is_set():
                break
            staged.append((item, await self._stage_manual_page(context, item)))
        if staged:
            with contextlib.suppress(Exception):
                await staged[0][1].bring_to_front()
        done = await manual([item for item, _page in staged])
        results: list[CartResult] = []
        for item, page in staged:
            results.append(await _manual_result(item, page, done, cancel))
            with contextlib.suppress(Exception):
                if not page.is_closed():
                    await page.close()
        for result in results:
            bandcamp._log_cart_result("manual", result)
        return results

    async def finish_manually(
        self, items: list[CartItem], manual: ManualCallback, cancel: asyncio.Event
    ) -> list[CartResult]:
        """The result screen's 'Finish in browser' for items a batch left uncertain."""

        by_store: dict[str, list[CartItem]] = defaultdict(list)
        for item in items:
            by_store[item.store].append(item)
        async with self._lock:
            return await self._finish_manually(by_store, manual, cancel, None)

    async def focus_carts(self) -> None:
        async with self._lock:
            for page in self._cart_pages.values():
                if not page.is_closed():
                    await page.bring_to_front()


async def prepare_playlist(requests, outcome, title, directory, browser, *, io=asyncio.to_thread):
    """Publish a local playlist before handing its metadata to Soundiiz."""
    from requests import RequestException

    from .. import beatport_playlist
    from .. import browser as browser_adapter
    from ..clipboard import copy_to_clipboard
    lines = beatport_playlist._beatport_playlist_lines(requests, outcome)
    if not lines:
        return PlaylistExport(0)
    path = await io(beatport_playlist._write_beatport_playlist, lines, directory)
    try:
        import_url = await io(
            beatport_playlist._create_soundiiz_import, requests, outcome,
            title or "DJ Digger Beatport playlist",
        )
    except (OSError, ValueError, RequestException):
        return PlaylistExport(len(lines), path, import_failed=True)
    copied, opened = await asyncio.gather(
        io(copy_to_clipboard, "\n".join(lines)),
        io(browser_adapter.open_url, import_url, browser),
    )
    return PlaylistExport(len(lines), path, copied, opened)


@dataclass(frozen=True)
class PlaylistExport:
    count: int
    path: Path | None = None
    copied: bool = False
    opened: bool = False
    import_failed: bool = False


def install_chromium(cancel):
    """Install the browser required by the current cart suboperation."""
    from .. import browser_session
    return browser_session.install_chromium(cancel)
