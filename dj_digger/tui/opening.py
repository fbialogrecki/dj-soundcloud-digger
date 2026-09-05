"""Handing links to the browser: the best one, a shop search, or everything shown.

Composed by ``DiggerApp`` with explicit state and presentation callbacks.
"""

import asyncio
import logging
import urllib.parse
from collections import Counter
from copy import deepcopy
from functools import partial
from threading import Event

from dj_digger import automation_errors, cart_models
from dj_digger.diagnostics import log_safe_text

from .. import links as links_module
from ..models import GOT, OPENED, SKIP
from ..services import purchases as cart_module
from ..services.downloads import find_gate_url
from .keymap import (
    OPEN_ALL_CONFIRM_THRESHOLD,
)
from .rows import Row
from .screens import (
    CartManualScreen,
    CartPlanScreen,
    CartProgressScreen,
    CartResultScreen,
    ConfirmScreen,
)

LOGGER = logging.getLogger(__name__)

SEARCH_URLS = {
    "bandcamp": "https://bandcamp.com/search?q={query}",
    "beatport": "https://www.beatport.com/search?q={query}",
}


class OpeningController:
    """Handing links to the browser: the best one, a shop search, or everything shown."""

    def __init__(
        self,
        *,
        get__cart_session,
        _download_directory,
        _main_available,
        _paint_key,
        get_browser,
        call_from_thread,
        cart_state,
        current_row,
        finish_job,
        job_progress,
        notify,
        operations,
        playlist_state,
        push_screen,
        push_screen_wait,
        record_to_open,
        run_worker,
        show_error,
        start_job,
        state,
        status_of,
        targets,
        update_status,
        worker_scope,
        opening_service,
        library_service,
        io,
    ):
        self.get__cart_session = get__cart_session
        self._download_directory = _download_directory
        self._main_available = _main_available
        self._paint_key = _paint_key
        self.get_browser = get_browser
        self.call_from_thread = call_from_thread
        self.cart_state = cart_state
        self.current_row = current_row
        self.finish_job = finish_job
        self.job_progress = job_progress
        self.notify = notify
        self.operations = operations
        self.playlist_state = playlist_state
        self.push_screen = push_screen
        self.push_screen_wait = push_screen_wait
        self.record_to_open = record_to_open
        self.run_worker = run_worker
        self.show_error = show_error
        self.start_job = start_job
        self.state = state
        self.opening_service = opening_service
        self.library_service = library_service
        self.io = io
        self.status_of = status_of
        self.targets = targets
        self.update_status = update_status
        self.worker_scope = worker_scope

    @property
    def _cart_session(self):
        return self.get__cart_session()

    @property
    def browser(self):
        return self.get_browser()

    def action_open_link(self) -> None:
        row = self.current_row()
        if row is None:
            return
        record = self.record_to_open(row)
        if record is None:
            self.notify('Local audio: press Space to play', timeout=3)
            return
        if record.link_text == links_module.NO_STORE_LINK:
            self.notify("No link for this track - opening it on SoundCloud", timeout=3)
        elif record.link_text == links_module.FREE_DOWNLOAD:
            self.notify("Use w to download the artist-provided file", timeout=4)
        url = (
            record.track.permalink_url
            if record.link_text in {links_module.NO_STORE_LINK, links_module.FREE_DOWNLOAD}
            else record.link_url
        )
        self.open_link_in_background(url, row)

    def open_link_in_background_work(self, url: str, key: str | None, browser: str) -> None:
        with self.worker_scope():
            """Hand one link to the browser off the interface thread.

            On WSL the handoff is a subprocess that can take seconds - twenty at
            the limit - and while it ran nothing on screen answered, Ctrl+C
            included. The mark is written back on the UI thread as before; a
            shop search has no row to mark.
            """

            opened = self.opening_service.open_one(url, key, browser)
            self.call_from_thread(self._link_opened, key, opened)

    def _link_opened(self, key: str | None, opened: bool) -> None:
        if not opened:
            what = "link" if key is not None else "search"
            self.notify(f"Could not open the {what}", severity="error")
            return
        if key is None:
            return
        self._paint_key(key)
        self.update_status()

    def _find_gate_url(self, row: Row) -> str | None:
        return find_gate_url(row.records)

    def action_search(self, store: str) -> None:
        row = self.current_row()
        if row is None:
            return
        query = urllib.parse.quote_plus(row.track.label)
        self.notify(f"Searching {store.capitalize()} for {row.track.label}...", timeout=3)
        self.open_link_in_background(SEARCH_URLS[store].format(query=query))

    def _cart_store_order(self) -> tuple[str, ...]:
        supported = {"bandcamp", "beatport"}
        if not self.playlist_state.store_filters:
            return ("bandcamp", "beatport")
        selected = self.playlist_state.store_filters & supported
        return tuple(store for store in ("bandcamp", "beatport") if store in selected)

    def _cart_request(self, row: Row) -> cart_models.CartRequest:
        links = []
        for store in self._cart_store_order():
            record = row.record_for(store)
            if record is not None and record.link_url:
                links.append((store, record.link_url))
        return cart_models.CartRequest(row.track, tuple(links))

    def _cart_requests(self, row: Row) -> list[cart_models.CartRequest]:
        request = self._cart_request(row)
        if not request.links:
            return []
        if {"bandcamp", "beatport"} <= self.playlist_state.store_filters:
            return [
                cart_models.CartRequest(row.track, ((store, url),))
                for store, url in request.links
            ]
        return [request]

    def action_cart_track(self) -> None:
        row = self.current_row()
        if row is None:
            return
        requests = self._cart_requests(row)
        if not requests:
            self.notify("The selected track has no eligible Bandcamp or Beatport link", timeout=4)
            return
        self._start_cart_preflight(requests, single=len(requests) == 1)

    def action_cart_visible(self) -> None:
        if not self._cart_store_order():
            self.notify(
                "The active store filters contain neither Bandcamp nor Beatport",
                severity="warning",
                timeout=4,
            )
            return
        rows = [
            row for row in self.targets() if self.status_of(row) not in (GOT, SKIP)
        ]
        if not rows:
            self.notify("No unhandled visible tracks to add", timeout=3)
            return
        requests = [request for row in rows for request in self._cart_requests(row)]
        self._start_cart_preflight(requests, single=False)

    def _claim_cart(self, taken: str = "The dedicated store browser is busy") -> bool:
        """Take the store browser for one job; False, and a word to the user, if it is taken.

        Whoever claims it hands it back by clearing ``_cart_busy`` when done.
        """

        if self.cart_state._cart_busy or not self._main_available():
            self.notify(taken, timeout=3)
            return False
        self.cart_state._cart_busy = True
        self.cart_state._cart_handle = self.start_job("Cart", cancel=self.cart_state._cart_cancel)
        return True

    def _start_cart_preflight(
        self, requests: list[cart_models.CartRequest], *, single: bool
    ) -> None:
        if not self._claim_cart("The dedicated store browser is already open"):
            return
        self.cart_state._cart_cancel.clear()
        self._capture_cart_context()
        self._run_cart_batch(deepcopy(requests), single)

    def _capture_cart_context(self):
        record = self.playlist_state.crate
        source = record.source if record else ""
        context = (source, self.state.db.crate_generation(source),
                   self.playlist_state._view_generation, self._download_directory(),
                   self.playlist_state.crate_title)
        self.cart_state._cart_context = context
        return context

    def _show_cart_progress(self) -> CartProgressScreen:
        screen = CartProgressScreen(self.cart_state._cart_cancel)
        self.cart_state._cart_progress_screen = screen
        self.push_screen(screen)
        return screen

    def _hide_cart_progress(self) -> None:
        screen = self.cart_state._cart_progress_screen
        self.cart_state._cart_progress_screen = None
        if screen is not None and screen.is_mounted:
            try:
                screen.dismiss(None)
            except Exception:
                pass

    def _cart_progress(self, progress: cart_models.CartProgress) -> None:
        screen = self.cart_state._cart_progress_screen
        if screen is not None:
            screen.update_progress(progress)

    async def _wait_cart_screen(self, screen, *, restore_progress: bool = True):
        """Show one cart-owned modal in place of the progress screen and await it.

        The modal is always removed, even if the worker is cancelled under it.
        With ``restore_progress`` a yes brings the progress screen back; a
        refusal leaves it down, since the worker is about to stop.
        """

        self._hide_cart_progress()
        answer = asyncio.create_task(self.push_screen_wait(screen))
        cancelled = asyncio.create_task(self.cart_state._cart_cancel.wait())
        try:
            done, _ = await asyncio.wait((answer, cancelled), return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                if screen.is_mounted:
                    screen.dismiss(None)
                answer.cancel()
                result = None
            else:
                result = await answer
        finally:
            cancelled.cancel()
            if not answer.done():
                answer.cancel()
            await asyncio.gather(answer, cancelled, return_exceptions=True)
            if screen.is_mounted:
                try:
                    screen.dismiss(None)
                except Exception:
                    pass
        if restore_progress and result and not self.cart_state._cart_cancel.is_set():
            self._show_cart_progress()
        return result

    async def _approve_cart_async(
        self, plan: cart_models.CartPlan, single: bool
    ) -> cart_models.CartPlan | None:
        if single and len(plan.items) == 1 and not plan.items[0].price_editable:
            item = plan.items[0]
            if item.store == "beatport":
                message = f"Preparing {item.track_label} for a Beatport playlist"
            else:
                message = f"Adding {item.track_label} — {item.currency} {item.price:.2f}"
            self.notify(message, timeout=4)
            return plan
        LOGGER.info("Cart review opened: items=%d", len(plan.items))
        approved = await self._wait_cart_screen(CartPlanScreen(plan))
        LOGGER.info(
            "Cart review closed: approved=%d cancelled=%s",
            len(approved.items) if approved is not None else 0,
            approved is None,
        )
        return approved

    async def _manual_cart_async(self, items: list[cart_models.CartItem]) -> bool:
        """Let the person finish the staged pages; True once they say they are done."""

        LOGGER.info("Manual cart completion opened: items=%d", len(items))
        done = await self._wait_cart_screen(CartManualScreen(items))
        LOGGER.info("Manual cart completion closed: done=%s", bool(done))
        return bool(done)

    async def _install_cart_chromium(self) -> bool:
        confirmed = await self._wait_cart_screen(
            ConfirmScreen(
                "Store carts need Playwright Chromium. Download it now? "
                "This is a one-time download for the installed Playwright version."
            ),
            restore_progress=False,
        )
        if not confirmed:
            return False
        self.notify("Installing Chromium in the background...", timeout=4)
        install_cancel = Event()
        install_task = asyncio.create_task(
            self.io(cart_module.install_chromium, install_cancel)
        )
        try:
            while not install_task.done():
                if self.cart_state._cart_cancel.is_set():
                    install_cancel.set()
                await asyncio.sleep(0.1)
            await install_task
        finally:
            if not install_task.done():
                install_cancel.set()
                await asyncio.shield(install_task)
        self._show_cart_progress()
        return True

    async def _run_cart_batch_work(
        self, requests: list[cart_models.CartRequest], single: bool
    ) -> None:
        current_requests = list(requests)
        try:
            while current_requests:
                self._show_cart_progress()
                try:
                    outcome = await self._cart_session.run_batch(
                        current_requests,
                        self.cart_state._cart_cancel,
                        approve=lambda plan: self._approve_cart_async(plan, single),
                        progress=self._cart_progress,
                        manual=self._manual_cart_async,
                    )
                except automation_errors.ChromiumMissing:
                    if not await self._install_cart_chromium():
                        self.notify("Chromium installation cancelled", timeout=3)
                        return
                    continue
                except Exception as exc:
                    if not self.cart_state._cart_cancel.is_set():
                        self._cart_failed(str(exc))
                    return
                finally:
                    self._hide_cart_progress()

                if outcome.cancelled and not outcome.results:
                    self.notify("Cart addition cancelled", timeout=3)
                    return
                self._cart_results_finished(outcome.results)
                action = await self._wait_cart_screen(
                    CartResultScreen(outcome), restore_progress=False
                )
                if action == "focus":
                    await self._cart_session.focus_carts()
                    return
                if action == "manual":
                    await self._finish_cart_manually(outcome)
                    return
                if action == "playlist":
                    await self._prepare_beatport_playlist(current_requests, outcome)
                    return
                if action != "retry":
                    return
                self.cart_state._cart_cancel.clear()
                current_requests, single = self._retry_subset(current_requests, outcome)
        finally:
            self._hide_cart_progress()
            self.cart_state._cart_busy = False
            if self.cart_state._cart_handle is not None:
                self.finish_job(self.cart_state._cart_handle)
                self.cart_state._cart_handle = None

    async def _finish_cart_manually(self, outcome: cart_models.CartBatchOutcome) -> None:
        """Hand the items the automation could not add to the person at the browser."""

        settled = await self._cart_session.finish_manually(
            list(outcome.manual_candidates), self._manual_cart_async, self.cart_state._cart_cancel
        )
        self._cart_results_finished(tuple(settled))
        await self._wait_cart_screen(
            CartResultScreen(cart_models.CartBatchOutcome(tuple(settled))),
            restore_progress=False,
        )

    async def _prepare_beatport_playlist(self, requests, outcome) -> None:
        source, generation, view, directory, title = self.cart_state._cart_context or self._capture_cart_context()
        if source:
            record = await asyncio.to_thread(self.library_service.remember_beatport, source, generation, outcome)
            if record is not None and view == self.playlist_state._view_generation:
                self.playlist_state.crate = record
        try:
            result = await cart_module.prepare_playlist(requests, outcome, title, directory, self.browser, io=self.io)
        except OSError:
            self.show_error("Could not save the Beatport playlist")
            self.notify("Could not save the Beatport playlist", severity="error", timeout=6)
            return
        if not result.count:
            self.notify("No Beatport tracks were available for the playlist", severity="warning", timeout=5)
            return
        if result.import_failed:
            message = f"Playlist saved to {result.path}, but Soundiiz import failed"
        elif result.opened and result.copied:
            message = f"Beatport playlist ready in Soundiiz ({result.count} tracks)"
        elif result.opened:
            message = f"Beatport playlist saved to {result.path}; upload it in Soundiiz"
        else:
            message = f"Beatport playlist saved to {result.path}; Soundiiz did not open"
        self.notify(message, severity="warning" if not result.opened else "information", timeout=9)

    def _retry_subset(
        self, requests: list[cart_models.CartRequest], outcome: cart_models.CartBatchOutcome
    ) -> tuple[list[cart_models.CartRequest], bool]:
        """The requests worth another pass, and whether it may skip the review."""

        retryable = outcome.retryable_targets
        remaining = [
            request
            for request in requests
            if any((request.track.key, store) in retryable for store, _url in request.links)
        ]
        force_review = any(
            result.code == "price_changed" and result.retryable for result in outcome.results
        )
        return remaining, len(remaining) == 1 and not force_review

    def _cart_failed(self, message: str) -> None:
        LOGGER.error("Cart automation failed: %s", log_safe_text(message))
        self.show_error(f"Cart automation failed: {message}")
        self.notify("Cart automation failed", severity="error", timeout=6)

    def _cart_results_finished(self, results: tuple[cart_models.CartResult, ...]) -> None:
        counts = Counter(result.status for result in results)
        grouped: Counter[tuple[str, str, str]] = Counter(
            (result.store, result.code, result.reason)
            for result in results
            if result.status in {"skipped", "failed"} and result.code != "not_selected"
        )
        for (store, _code, reason), count in grouped.items():
            suffix = f" ({count} tracks)" if count > 1 else ""
            LOGGER.warning(
                "Cart result group: store=%s code=%s tracks=%d reason=%s",
                store or "none",
                _code or "none",
                count,
                log_safe_text(reason),
            )
            self.show_error(f"{store or 'no store'}: {reason}{suffix}")
        self.notify(
            f"Purchases: {counts['added']} added, {counts['already_in_cart']} already there, "
            f"{counts['playlist_ready']} in Beatport playlist, "
            f"{counts['skipped']} skipped, {counts['failed']} failed",
            timeout=6,
        )

    async def _run_cart_op_work(
        self, operation, success, *, timeout: int = 4, progress: bool = False
    ) -> None:
        """One store-browser chore on a claimed browser: a toast when it works, the banner when not.

        ``operation`` returns the awaitable; ``success`` turns its result into
        the toast, or into an empty string when there is nothing to say.
        """

        if progress:
            self._show_cart_progress()
        try:
            result = await operation()
        except Exception as exc:
            self._cart_failed(str(exc))
        else:
            message = success(result)
            if message:
                self.notify(message, timeout=timeout)
        finally:
            if progress:
                self._hide_cart_progress()
            self.cart_state._cart_busy = False
            if self.cart_state._cart_handle is not None:
                self.finish_job(self.cart_state._cart_handle)
                self.cart_state._cart_handle = None

    def action_setup_store_logins(self) -> None:
        if not self._claim_cart():
            return
        self.cart_state._cart_cancel.clear()
        self._run_cart_op(
            lambda: self._cart_session.setup_logins(
                ("bandcamp",), self.cart_state._cart_cancel, self._cart_progress
            ),
            lambda _: "Bandcamp session is ready",
            progress=True,
        )

    def action_check_store_logins(self) -> None:
        if not self._claim_cart():
            return
        self._run_cart_op(
            lambda: self._cart_session.check_logins(("bandcamp",)),
            lambda states: ", ".join(
                f"{store.capitalize()}: {'signed in' if ready else 'not signed in'}"
                for store, ready in states.items()
            ),
            timeout=6,
        )

    def action_reset_store_profile(self) -> None:
        if not self._claim_cart():
            return

        async def reset() -> bool:
            confirmed = await self._wait_cart_screen(
                ConfirmScreen(
                    "Reset the dedicated store browser? This removes its cookies and logins."
                ),
                restore_progress=False,
            )
            if confirmed:
                await self._cart_session.reset_profile()
            return bool(confirmed)

        self._run_cart_op(reset, lambda done: "Store browser profile reset" if done else "")

    def action_open_visible(self) -> None:
        target_rows = [row for row in self.targets() if self.status_of(row) not in (GOT, OPENED)]
        if not target_rows:
            self.notify("Nothing to open (all visible tracks are marked as 'got' or already opened)", timeout=3)
            return

        if not self._confirm_many(len(target_rows), "shift+O"):
            return
        self.notify(f"Opening {len(target_rows)} links in background...", timeout=3)
        self.open_visible_in_background(target_rows)

    def action_open_beatport_tracks(self) -> None:
        """Beatport carts are not automated; its exact track pages open where you are logged in.

        A release link cannot be turned into a track page without a lookup, so
        those rows are counted and left out rather than opened at the album.
        """

        rows: list[Row] = []
        urls: list[str] = []
        release_only = 0
        for row in self.targets():
            if self.status_of(row) in (GOT, SKIP):
                continue
            record = row.record_for("beatport")
            if record is None or not record.link_url:
                continue
            direct = cart_module._direct_beatport_track_url(record.link_url)
            if direct is None:
                release_only += 1
                continue
            rows.append(row)
            urls.append(direct)
        if not rows:
            self.notify(
                "No exact Beatport track pages to open"
                + (f" ({release_only} release links skipped)" if release_only else ""),
                timeout=4,
            )
            return
        count = len(rows)
        if not self._confirm_many(count, "shift+P", "Beatport tabs"):
            return
        message = f"Opening {count} Beatport track pages; press Add to cart on each"
        if release_only:
            message += f" ({release_only} release links skipped)"
        self.notify(message, timeout=5)
        self.open_visible_in_background(rows, urls)

    def _confirm_many(self, count: int, key: str, what: str = "tabs") -> bool:
        """Above the threshold, the first press of ``key`` only asks; the second goes ahead.

        Changing the filter clears the question (see filters.py), since the
        count it was about is gone.
        """

        if count <= OPEN_ALL_CONFIRM_THRESHOLD or self.cart_state._pending_open == key:
            self.cart_state._pending_open = None
            return True
        self.cart_state._pending_open = key
        self.notify(
            f"That opens {count} {what}. Press {key} again to confirm, "
            "or filter the list down first.",
            severity="warning",
            timeout=6,
        )
        return False

    def open_visible_in_background(self, rows: list[Row], urls: list[str] | None = None):
        if not self._main_available():
            return None
        if urls is None:
            rows = [row for row in rows if self.record_to_open(row) is not None]
            urls = [self.record_to_open(row).link_url for row in rows]
        handle = self.start_job("Opening", len(rows), cancel=Event())
        return self._open_visible_worker(deepcopy(rows), list(urls), self.browser, handle)

    def _open_visible_worker_work(self, rows, urls, browser, handle):
        with self.worker_scope():
            try:
                def on_success(idx, url):
                    key = rows[idx].track.key
                    self.call_from_thread(self._paint_key, key)
                    self.call_from_thread(self.job_progress, 1, handle=handle)

                def handle_error(message):
                    self.call_from_thread(self.show_error, message)
                    self.call_from_thread(self.job_progress, failed=1, handle=handle)

                opened = self.opening_service.open_many(
                    urls, [row.track.key for row in rows], browser, on_success=on_success, on_error=handle_error, cancel=handle.cancel,
                )
                self.call_from_thread(self._open_visible_finished, opened, len(rows))
            finally:
                self.operations.finish(handle)
                try:
                    self.call_from_thread(self.update_status)
                except RuntimeError:
                    pass

    def _open_visible_finished(self, opened: int, total: int) -> None:
        if opened < total:
            self.show_error(
                f"Opened {opened}/{total} tabs. {total - opened} failed to open "
                "(OS process / browser tab opening limit reached)."
            )
        self.notify(f"Opened {opened}/{total} links", timeout=3)

    def open_link_in_background(self, url, row=None):
        return self.run_worker(
            partial(self.open_link_in_background_work, url, row.track.key if row else None, self.browser), thread=True, group='open_link',
            description="open_link_in_background",
        )

    def _run_cart_batch(self, *args, **kwargs):
        return self.run_worker(
            partial(self._run_cart_batch_work, *args, **kwargs), exclusive=True, group='cart', exit_on_error=False,
            description="_run_cart_batch",
        )

    def _run_cart_op(self, *args, **kwargs):
        return self.run_worker(
            partial(self._run_cart_op_work, *args, **kwargs), exclusive=True, group='cart', exit_on_error=False,
            description="_run_cart_op",
        )

    def _open_visible_worker(self, *args, **kwargs):
        return self.run_worker(
            partial(self._open_visible_worker_work, *args, **kwargs), thread=True, group='open_all',
            description="_open_visible_worker",
        )
