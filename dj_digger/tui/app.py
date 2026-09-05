"""The crate browser application itself: state, actions and workers."""

import asyncio
import logging
import traceback
from collections.abc import Sequence
from copy import deepcopy
from functools import partial
from pathlib import Path
from threading import Event

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, ListView, Static

from .. import links as links_module
from ..crate_models import CrateRecord
from ..diagnostics import log_safe_text
from ..models import LinkRecord
from ..services import collection as dig_module
from ..services.runtime import ApplicationServices
from ..state import TrackState
from .audio import PlayerBar, PlayerControls
from .crates import CrateController
from .digging import DiggingController
from .downloads import DownloadController
from .filters import FilterController
from .jobs import JobController
from .keymap import (
    KEY_DISPLAY,
    KEYMAP,
    PRIORITY_KEYS,
    QUICK_FILTER_KEYS,
)
from .library_scan import LibraryScanController
from .opening import OpeningController
from .playback import PlaybackController
from .presentation import (
    AudioState,
    CartState,
    DownloadState,
    PlaylistState,
    ScanState,
    SidebarState,
)
from .render import RenderController
from .screens import AskLinkScreen, ContextMenuScreen, HelpScreen, SettingsScreen
from .theme import FALLBACK_PALETTE, Palette, palette_for
from .widgets import ErrorBanner, FittedFooter, SearchInput, StatusBar, TrackTable

LOGGER = logging.getLogger(__name__)

# The table needs 79 columns before the title stops shrinking, and the sidebar
# and its border cost 29. Below the sum of the two the sidebar is crate names
# paid for with Genre, Time and half the title, so it folds itself away.
NARROW_WIDTH = 110


class DiggerApp(App):
    """The crate browser.

    Controllers own presentation and operations through explicit dependencies.
    The application composes the screen, routes actions and manages lifecycle.
    """
    # The built-in palette showed up in the footer as an unexplained "palette".
    # Off: it brings Textual's own Screenshot, Maximize and Theme commands
    # along, none of which belongs in a crate browser. Settings and ? cover
    # everything the app itself offers.
    ENABLE_COMMAND_PALETTE = False
    # Otherwise the terminal's window and tab say "DiggerApp", which is the name
    # of the class rather than of anything the user installed.
    TITLE = "dj-digger"

    CSS = """
    #error-banner {
        width: 100%;
    }
    #body {
        height: 1fr;
    }
    #sidebar {
        width: 28;
        border-right: solid $panel;
    }
    #sidebar.collapsed {
        display: none;
    }
    /* Centred over the list it heads, with a blank row under it: the crate
       names started immediately below and read as one more crate. */
    #sidebar-title, #explorer-title {
        padding: 0 1;
        text-align: center;
        color: $text-muted;
    }
    #sidebar-title { margin-bottom: 1; }
    /* Auto height so the add button sits right under the last crate, not pinned
       to the bottom of the sidebar. */
    #crates {
        height: auto;
        max-height: 1fr;
        border: none;
        background: transparent;
    }
    CrateItem {
        layout: horizontal;
        height: 1;
    }
    .crate-name {
        width: 1fr;
        height: 1;
    }
    /* The icons cost six of the sidebar's columns, which the crate names need
       more than a row you are not pointing at does. They come back on the row
       under the cursor or the mouse, which is the only row you can act on. */
    .crate-icon {
        display: none;
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
    }
    CrateItem.-highlight .crate-icon,
    CrateItem.-hovered .crate-icon {
        display: block;
    }
    .crate-icon:hover {
        background: $accent;
    }
    #crate-add, #folder-open, #folder-next {
        width: 1fr;
        height: 1;
        border: none;
        background: transparent;
        color: $text-muted;
        content-align: left middle;
        padding: 0 1;
    }
    #crate-add:hover, #folder-open:hover, #folder-next:hover {
        background: $accent;
        color: $text;
    }
    /* Exactly one line. Left to wrap, this bar grew back into the three rows of
       chrome it was meant to replace. */
    /* One line, like the status bar: the default Input spends three rows on a
       border to hold one row of text, and this sits above the list you are
       filtering. */
    #search {
        display: none;
        height: 1;
        border: none;
        padding: 0 1;
        background: $panel;
    }
    #search.visible {
        display: block;
    }
    DataTable {
        height: 1fr;
    }
    """

    CSS = CSS + """
    #playlist-pane, #explorer-pane { height: 50%; }
    #explorer { height: 1fr; scrollbar-size: 1 1; }
    #explorer-title { height: 1; }
    #folder-next { display: none; }
    """

    BINDINGS = [
        Binding(
            key,
            action,
            label,
            show=show,
            key_display=KEY_DISPLAY.get(key),
            priority=key in PRIORITY_KEYS,
        )
        for key, action, label, _group, show, _detail in KEYMAP
    ] + [
        # Textual 8 answers ctrl+c with a toast saying to press ctrl+q, which
        # is not what anyone reaching for ctrl+c wants. A binding on the app
        # replaces the base one for the same key; priority puts it ahead of
        # the search box, where Input would otherwise take ctrl+c as "copy".
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ] + [
        # 0 is declared in KEYMAP so it shows in the footer as the way back.
        Binding(str(index), f"filter_index({index})", f"Store {index}", show=False)
        for index in range(1, QUICK_FILTER_KEYS + 1)
    ]

    def __init__(
        self,
        records: Sequence[LinkRecord] = (),
        *,
        state: TrackState | None = None,
        services: ApplicationServices | None = None,
        crate_title: str = "",
        export_format: str = "json",
        export_path: Path | None = None,
        dig_options: dig_module.DigOptions | None = None,
        crate_record: CrateRecord | None = None,
        shutdown_started=lambda: None,
    ) -> None:
        super().__init__()
        self.shutdown_started = shutdown_started
        self.playlist_state = PlaylistState()
        self.audio_state = AudioState()
        self.download_state = DownloadState()
        self.cart_state = CartState()
        self.sidebar_state = SidebarState()
        self.scan_state = ScanState()
        self.services = services or ApplicationServices(state=state)
        self.state = self.services.state
        self.state.get("")  # Warm the mirrors before mounting any widgets.
        self.config = self.services.config
        self.playlist_state.crate = crate_record
        self.playlist_state.crate_title = crate_title or (crate_record.title if crate_record else "")
        self.sub_title = self.playlist_state.crate_title
        self.export_format = export_format
        self.export_path = export_path
        self.dig_options = dig_options or dig_module.DigOptions()
        self._dig_cancel = Event()
        self._narrow: bool | None = None
        # The interface's colour roles under the active theme, recomputed
        # whenever the theme changes (see tui/theme.py).
        self.palette: Palette = FALLBACK_PALETTE
        self.crate_controller = CrateController(
            run_worker=self.run_worker,
            _start_dig=lambda *a, **k: self._start_dig(*a, **k),
            action_dig_link=lambda *a, **k: self.action_dig_link(*a, **k),
            call_next=lambda *a, **k: self.call_next(*a, **k),
            current_row=lambda *a, **k: self.filter_controller.current_row(*a, **k),
            notify=lambda *a, **k: self.notify(*a, **k),
            playlist_state=self.playlist_state,
            push_screen=lambda *a, **k: self.push_screen(*a, **k),
            query_one=lambda *a, **k: self.query_one(*a, **k),
            refresh_rows=lambda *a, **k: self.table_controller.refresh_rows(*a, **k),
            selected_rows=lambda *a, **k: self.filter_controller.selected_rows(*a, **k),
            sidebar_state=self.sidebar_state,
            state=self.state,
            library_service=self.services.library,
            io=self.services.io,
            set_subtitle=lambda title: setattr(self, "sub_title", title),
        )
        self.filter_controller = FilterController(
            _paint_headers=lambda *a, **k: self.table_controller._paint_headers(*a, **k),
            _paint_row=lambda *a, **k: self.table_controller._paint_row(*a, **k),
            cart_state=self.cart_state,
            enabled_columns=lambda *a, **k: self.table_controller.enabled_columns(*a, **k),
            notify=lambda *a, **k: self.notify(*a, **k),
            playlist_state=self.playlist_state,
            query_one=lambda *a, **k: self.query_one(*a, **k),
            refresh_rows=lambda *a, **k: self.table_controller.refresh_rows(*a, **k),
            state=self.state,
            update_status=lambda *a, **k: self.table_controller.update_status(*a, **k),
        )
        self.table_controller = RenderController(
            _drop_stale_preparation=lambda *a, **k: self.playback_controller._drop_stale_preparation(*a, **k),
            _playing_index=lambda *a, **k: self.playback_controller._playing_index(*a, **k),
            action_play_step=lambda *a, **k: self.playback_controller.action_play_step(*a, **k),
            audio_state=self.audio_state,
            call_after_refresh=lambda *a, **k: self.call_after_refresh(*a, **k),
            get_config=lambda: self.config,
            current_row=lambda *a, **k: self.filter_controller.current_row(*a, **k),
            download_state=self.download_state,
            get_job=lambda: self.job,
            matching_rows=lambda *a, **k: self.filter_controller.matching_rows(*a, **k),
            get_muted=lambda: self.muted,
            notify=lambda *a, **k: self.notify(*a, **k),
            get_palette=lambda: self.palette,
            get_player=lambda: self.player,
            playlist_state=self.playlist_state,
            query=lambda *a, **k: self.query(*a, **k),
            query_one=lambda *a, **k: self.query_one(*a, **k),
            record_to_open=lambda *a, **k: self.filter_controller.record_to_open(*a, **k),
            role=lambda *a, **k: self.role(*a, **k),
            selected_rows=lambda *a, **k: self.filter_controller.selected_rows(*a, **k),
            set_timer=lambda *a, **k: self.set_timer(*a, **k),
            soft_matching_rows=lambda *a, **k: self.filter_controller.soft_matching_rows(*a, **k),
            state=self.state,
            status_of=lambda *a, **k: self.filter_controller.status_of(*a, **k),
            io=self.services.io,
        )
        self.playback_controller = PlaybackController(
            _paint_key=lambda *a, **k: self.table_controller._paint_key(*a, **k),
            _playing_key=lambda *a, **k: self.table_controller._playing_key(*a, **k),
            _update_track_progress=lambda *a, **k: self.download_controller._update_track_progress(*a, **k),
            get_animation_level=lambda: self.animation_level,
            audio_state=self.audio_state,
            call_from_thread=lambda *a, **k: self.call_from_thread(*a, **k),
            get_client=lambda: self.client,
            current_row=lambda *a, **k: self.filter_controller.current_row(*a, **k),
            download_state=self.download_state,
            get_job=lambda: self.job,
            notify=lambda *a, **k: self.notify(*a, **k),
            get_player=lambda: self.player,
            playlist_state=self.playlist_state,
            query=lambda *a, **k: self.query(*a, **k),
            query_one=lambda *a, **k: self.query_one(*a, **k),
            run_worker=lambda *a, **k: self.run_worker(*a, **k),
            update_status=lambda *a, **k: self.table_controller.update_status(*a, **k),
            worker_scope=self.services.worker,
        )
        self.download_controller = DownloadController(
            _find_gate_url=lambda *a, **k: self.opening_controller._find_gate_url(*a, **k),
            _main_available=lambda *a, **k: self._main_available(*a, **k),
            _mark_existing_local_file=lambda *a, **k: self.scan_controller._mark_existing_local_file(*a, **k),
            _paint_key=lambda *a, **k: self.table_controller._paint_key(*a, **k),
            _set_records=lambda *a, **k: self.crate_controller._set_records(*a, **k),
            call_from_thread=lambda *a, **k: self.call_from_thread(*a, **k),
            call_later=lambda *a, **k: self.call_later(*a, **k),
            get_client=lambda: self.client,
            get_config=lambda: self.config,
            current_row=lambda *a, **k: self.filter_controller.current_row(*a, **k),
            get_dig_options=lambda: self.dig_options,
            download_service=self.services.downloads,
            download_state=self.download_state,
            job_progress=lambda *a, **k: self.job_progress(*a, **k),
            notify=lambda *a, **k: self.notify(*a, **k),
            operations=self.services.operations,
            playlist_state=self.playlist_state,
            push_screen=lambda *a, **k: self.push_screen(*a, **k),
            refresh_rows=lambda *a, **k: self.table_controller.refresh_rows(*a, **k),
            adopt_login=self.services.adopt_login,
            accounts=self.services.accounts,
            run_worker=lambda *a, **k: self.run_worker(*a, **k),
            show_error=lambda *a, **k: self.show_error(*a, **k),
            start_job=lambda *a, **k: self.start_job(*a, **k),
            state=self.state,
            status_of=lambda *a, **k: self.filter_controller.status_of(*a, **k),
            targets=lambda *a, **k: self.filter_controller.targets(*a, **k),
            update_status=lambda *a, **k: self.table_controller.update_status(*a, **k),
            worker_scope=self.services.worker,
            io=self.services.io,
        )
        self.opening_controller = OpeningController(
            get__cart_session=lambda: self._cart_session,
            _download_directory=lambda *a, **k: self.download_controller._download_directory(*a, **k),
            _main_available=lambda *a, **k: self._main_available(*a, **k),
            _paint_key=lambda *a, **k: self.table_controller._paint_key(*a, **k),
            get_browser=lambda: self.browser,
            call_from_thread=lambda *a, **k: self.call_from_thread(*a, **k),
            cart_state=self.cart_state,
            current_row=lambda *a, **k: self.filter_controller.current_row(*a, **k),
            finish_job=lambda *a, **k: self.finish_job(*a, **k),
            job_progress=lambda *a, **k: self.job_progress(*a, **k),
            notify=lambda *a, **k: self.notify(*a, **k),
            operations=self.services.operations,
            playlist_state=self.playlist_state,
            push_screen=lambda *a, **k: self.push_screen(*a, **k),
            push_screen_wait=lambda *a, **k: self.push_screen_wait(*a, **k),
            record_to_open=lambda *a, **k: self.filter_controller.record_to_open(*a, **k),
            run_worker=lambda *a, **k: self.run_worker(*a, **k),
            show_error=lambda *a, **k: self.show_error(*a, **k),
            start_job=lambda *a, **k: self.start_job(*a, **k),
            state=self.state,
            opening_service=self.services.opening,
            library_service=self.services.library,
            io=self.services.io,
            status_of=lambda *a, **k: self.filter_controller.status_of(*a, **k),
            targets=lambda *a, **k: self.filter_controller.targets(*a, **k),
            update_status=lambda *a, **k: self.table_controller.update_status(*a, **k),
            worker_scope=self.services.worker,
        )
        self.scan_controller = LibraryScanController(
            _download_directory=lambda *a, **k: self.download_controller._download_directory(*a, **k),
            _main_available=lambda *a, **k: self._main_available(*a, **k),
            _paint_key=lambda *a, **k: self.table_controller._paint_key(*a, **k),
            call_from_thread=lambda *a, **k: self.call_from_thread(*a, **k),
            get_config=lambda: self.config,
            current_row=lambda *a, **k: self.filter_controller.current_row(*a, **k),
            download_service=self.services.downloads,
            finish_job=lambda *a, **k: self.finish_job(*a, **k),
            notify=lambda *a, **k: self.notify(*a, **k),
            operations=self.services.operations,
            playlist_state=self.playlist_state,
            refresh_rows=lambda *a, **k: self.table_controller.refresh_rows(*a, **k),
            run_worker=lambda *a, **k: self.run_worker(*a, **k),
            scan_state=self.scan_state,
            show_error=lambda *a, **k: self.show_error(*a, **k),
            start_job=lambda *a, **k: self.start_job(*a, **k),
            state=self.state,
            library_service=self.services.library,
            update_status=lambda *a, **k: self.table_controller.update_status(*a, **k),
            worker_scope=self.services.worker,
            io=self.services.io,
        )
        self.jobs = JobController(
            self.services.operations, changed=self.table_controller.update_status, wake=self.playback_controller._wake,
            sleep=self.playback_controller._sleep, playing=lambda: self.player.playing, notify=self.notify,
        )
        self.digging = DiggingController(
            self.services.collection, self.services.operations,
            run=lambda function: self.run_worker(self._owned_work(function), thread=True, group="dig"),
            dispatch=self.call_from_thread,
            prompt=lambda message, answer: self.push_screen(AskLinkScreen(message=message), answer),
            notify=self.notify, has_rows=lambda: bool(self.playlist_state.rows),
            view_generation=lambda: self.playlist_state._view_generation, options=lambda: self.dig_options,
            export_settings=lambda: (self.export_format, self.export_path),
            display=self._display_collection, changed=self._job_changed, exit_empty=self.exit,
        )
        self.crate_controller._set_records(records)

    def _owned_work(self, function):
        def run():
            with self.services.worker():
                return function()
        return run

    def _job_changed(self):
        self.table_controller.update_status()
        if self.job is not None:
            self.playback_controller._wake()
        elif not self.player.playing:
            self.playback_controller._sleep()

    def action_dig_link(self):
        self.digging.ask()

    def _start_dig(self, target):
        worker = self.digging.start(target)
        if self.job is not None and self.job.name == "Digging":
            self._dig_cancel = self.job.cancel
        return worker

    def _dig_running(self):
        handle = self.services.operations.active()
        return handle is not None and handle.name == "Digging"

    def _display_collection(self, result, view_generation):
        record = result.record
        if record is None:
            return
        if view_generation == self.playlist_state._view_generation:
            self.crate_controller.load_crate(record)
        self.call_next(self.crate_controller.reload_sidebar)
        records = links_module.categorise_all(record.active_tracks)
        message = f"{len(record.tracks)} tracks, {len(records)} links"
        if result.exported:
            message += f" - saved to {result.exported}"
        self.notify(message, timeout=5)

    @property
    def job(self):
        return self.services.operations.visible

    def _main_available(self):
        handle = self.services.operations.active()
        if handle is None:
            return True
        self.notify(f"{handle.name} is still running; ctrl+x stops it", timeout=3)
        return False

    def start_job(self, name, total=None, **kwargs):
        return self.jobs.start(name, total, **kwargs)

    def job_progress(self, done=0, *, handle=None, **kwargs):
        self.jobs.progress(handle or self.job, done, **kwargs)

    def finish_job(self, handle=None):
        self.jobs.finish(handle or self.job)

    def action_cancel_job(self):
        self.jobs.cancel()
        from .screens import GateProfileScreen, SoundCloudAuthScreen
        if isinstance(self.screen, (GateProfileScreen, SoundCloudAuthScreen)):
            self.screen.action_cancel()

    def compose(self) -> ComposeResult:
        yield ErrorBanner(id="error-banner")
        yield PlayerBar(self.player, id="player")
        yield PlayerControls(self.player, id="player-controls")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                with Vertical(id="playlist-pane"):
                    yield Static("Playlists", id="sidebar-title")
                    yield ListView(id="crates")
                    yield Button("+ Add playlist", id="crate-add", tooltip="Add a playlist (d)")
                with Vertical(id="explorer-pane"):
                    yield Static("Local files", id="explorer-title")
                    from textual.widgets import Tree
                    yield Tree("Directories", id="explorer")
                    yield Button("Next page", id="folder-next")
                    yield Button("+ Open folder", id="folder-open")
            with Vertical(id="main"):
                yield SearchInput(placeholder="Filter by artist, title, genre, tag or label", id="search")
                yield TrackTable(id="tracks", cursor_type="row", zebra_stripes=True)
        yield StatusBar(id="status")
        yield FittedFooter()

    def on_resize(self, event: events.Resize) -> None:
        """Give the narrow terminal back the columns it does not have.

        Both of these already had a manual switch - ``ctrl+b`` for the sidebar,
        ``?`` for the full key list - so this only decides for you at the widths
        where there is nothing to decide.
        """

        narrow = event.size.width < NARROW_WIDTH
        if narrow != self._narrow:
            self._narrow = narrow
            self.query_one("#sidebar").set_class(narrow, "collapsed")
        # The footer picks which bindings fit in its own compose, which resize
        # does not otherwise trigger. Queued on the footer rather than on the
        # app: composing a widget from the app's message pump breaks the data
        # binding Textual's Footer sets up on its own keys.
        footer = self.query_one(FittedFooter)
        footer.call_next(footer.recompose)
        if hasattr(self, "local_controller"):
            self.local_controller.layout()

    def _handle_exception(self, error: Exception) -> None:
        """Put the crash in the log before Textual tears the screen down.

        A crash report printed to the alternate screen is gone the moment the
        terminal is restored, which is how a session could die with nothing to
        show for it - the log file ended at the last ordinary record. Private
        API, pinned by the test that mounts and crashes an app on purpose.
        """

        LOGGER.error("Unhandled exception in the TUI: %s\n%s", log_safe_text(error),
                     "".join(traceback.format_tb(error.__traceback__)))
        # Keep Textual's test/exit bookkeeping, but never render locals or an
        # exception's provider-supplied Rich representation (which can hold tokens).
        self._return_code = 1
        if self._exception is None:
            self._exception = error
            self._exception_event.set()
        self.panic(Text(f"{type(error).__name__}: {log_safe_text(error)}"))

    def notify(self, message, *, markup=False, **kwargs):
        super().notify(log_safe_text(message), markup=False, **kwargs)

    def show_error(self, message: str) -> None:
        """Display an error/debug message in the top ErrorBanner."""
        message = log_safe_text(message)
        try:
            banner = self.query_one(ErrorBanner)
            banner.add_error(message)
        except Exception:
            LOGGER.error("Error: %s", message)
            self.notify(f"Error: {message}", severity="error", timeout=8)

    async def on_mount(self) -> None:
        table = self.query_one("#tracks", TrackTable)
        self.table_controller.build_columns(table)
        if self.config.theme in self.available_themes and self.theme != self.config.theme:
            self.theme = self.config.theme
        else:
            self.palette = palette_for(self.get_css_variables(), self.current_theme)
        from .local import LocalController
        self.local_controller = LocalController(
            services=self.services, playlist_state=self.playlist_state, crate_controller=self.crate_controller,
            audio_state=self.audio_state, config=self.config, jobs=self.jobs,
            notify=self.notify, push_screen=self.push_screen, run_worker=self.run_worker, query_one=self.query_one,
            refresh_rows=self.table_controller.refresh_rows, selected_rows=self.filter_controller.selected_rows,
            current_row=self.filter_controller.current_row,
            build_columns=self.table_controller.rebuild_columns)
        await self.local_controller.mount()
        await self.crate_controller.reload_sidebar()
        if not self.playlist_state.rows:
            # Someone with a library wants to see it, not be interrogated.
            latest = self.crate_controller.latest_crate()
            if latest is not None:
                await self.crate_controller.open_crate(latest)
        self.table_controller.refresh_rows()
        table.focus()
        # Needs a laid-out width to size itself against.
        self.call_after_refresh(table.fit_flexible_column)
        # Asleep until there is something to animate: waking thirty times a
        # second to look at a list nobody is playing is just a warm laptop.
        self.audio_state._ticker = self.set_interval(self.playback_controller.frame_interval, self.playback_controller._tick, pause=True)
        if self.config.first_run:
            # Nothing is configured yet, and one of the things being asked about
            # is which folders to scan, so the scan waits for the answer too.
            self.push_screen(await self._settings_screen(), lambda _: self._after_setup())
        else:
            self._after_setup()

    def _after_setup(self) -> None:
        # Off the interface thread: a first scan of a real music folder takes a
        # while, and the crate is usable long before it finishes.
        self.scan_controller.scan_local_files()
        if not self.playlist_state.rows:
            self.action_dig_link()

    async def on_unmount(self) -> None:
        """Let go of everything this screen owns, in one place.

        There were two of these, and Python kept the second - so the ticker went
        on running and the download pool was shut down twice over while the
        prefetched stream was closed by hand rather than through its own method.

        No ``workers.cancel_all()``: Textual runs one itself, and traced against
        8.2.8 it lands before this method is dispatched, not after.
        """

        # The async Playwright context lives on this same event loop. Textual has
        # cancelled its workers by now; close the persistent profile explicitly.
        self.shutdown_started()
        self.services.operations.stop_accepting()
        self.cart_state._cart_cancel.set()
        self.download_state._gate_cancel.set()
        self._dig_cancel.set()
        self.scan_state._scan_cancel.set()
        # A tick landing after the widgets have gone would go looking for a
        # player bar that no longer exists. Textual does stop its timers, but
        # only further down the same teardown, so this one goes first.
        if self.audio_state._ticker is not None:
            self.audio_state._ticker.stop()
            self.audio_state._ticker = None
        self.audio_state._playback_generation += 1
        self.playback_controller._discard_prepared()
        # This runs before Textual gives the terminal back, so a Playwright
        # that will not answer would hang the exit with no key able to reach
        # us. The independent process guard also covers this bounded close.
        try:
            if self.services._cart is not None:
                await asyncio.wait_for(self._cart_session.close(), timeout=5)
        except Exception as exc:  # TimeoutError included
            LOGGER.warning("Store browser did not close cleanly: %s", exc)
        await asyncio.to_thread(self.services.stop)

    @property
    def muted(self) -> str:
        """Rich style for secondary text under the current theme."""

        return self.palette.muted

    def role(self, style: str) -> str:
        """A keymap style such as "bold success" resolved to the theme's colour."""

        words = style.split()
        if not words:
            return style
        name = words[-1]
        colour = getattr(self.palette, name, None)
        if not isinstance(colour, str) or not colour:
            return style
        return " ".join([*words[:-1], colour])

    def watch_theme(self, theme: str) -> None:
        """Keep the colour roles and the saved preference in step with the theme."""

        try:
            self.palette = palette_for(self.get_css_variables(), self.current_theme)
        except Exception:
            self.palette = FALLBACK_PALETTE
        if self.config.theme != theme and not self.config.first_run:
            self.run_worker(
                partial(self.services.accounts.save_preferences, {"theme": theme}),
                thread=True, description="Save theme",
            )
        if self.is_mounted and self.query("#tracks"):
            self.table_controller.refresh_rows()

    @property
    def client(self):
        return self.services.client

    @property
    def player(self):
        return self.services.player

    @player.setter
    def player(self, value):
        self.services._player = value

    @property
    def _client(self):
        return self.services._client

    @_client.setter
    def _client(self, value):
        self.services._client = value

    @property
    def _cart_session(self):
        return self.services.cart

    @_cart_session.setter
    def _cart_session(self, value):
        self.services._cart = value

    @property
    def browser(self) -> str:
        """What Settings says. Empty means the system default.

        Read fresh each time rather than settled in __init__, so changing it in
        Settings takes effect on the next link instead of the next run.
        """

        return self.config.browser

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def _settings_screen(self):
        choices = await asyncio.to_thread(self.services.accounts.browser_choices)
        return SettingsScreen(self.config, self.services.accounts, choices)

    @work
    async def action_open_settings(self) -> None:
        self.push_screen(await self._settings_screen())

    @work
    async def action_export(self) -> None:
        if self.export_format == "none":
            self.notify("Export is disabled for this run", timeout=3)
            return
        records = deepcopy([record for row in self.filter_controller.targets() for record in row.records])
        if not records:
            self.notify("Nothing to export", timeout=2)
            return
        path = await self.services.io(links_module.export_records, records, self.export_format, self.export_path)
        if path:
            self.notify(f"Exported {len(records)} links to {path}", timeout=4)
        else:
            self.notify("Export failed - see the log", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.opening_controller.action_open_link()

    @work
    async def on_track_table_context_menu_requested(
        self, event: TrackTable.ContextMenuRequested
    ) -> None:
        event.stop()
        row = self.filter_controller.current_row()
        if row is None:
            return
        if await self.scan_controller._forget_missing_local_file(row.track):
            self.table_controller._paint_key(row.track.key)
        entries = [
            ("open", "Open best link", self.opening_controller.action_open_link),
            ("got", "Mark as got", self.action_mark_got),
            ("skip", "Mark as skipped", self.action_mark_skip),
            ("new", "Clear mark", self.action_mark_new),
            ("remove", "Remove track", self.action_remove_track),
        ]
        if row.track.local_path:
            entries.insert(1, ("copy", "Copy local file path", self.action_copy_path))
            if await self.scan_controller._local_file_needs_copy(row.track):
                entries.insert(
                    2, ("copy_file", "Copy file to playlist folder", self.action_copy_local_file)
                )

        actions = {key: handler for key, _label, handler in entries}
        self.push_screen(
            ContextMenuScreen(
                row.track.label, tuple((key, label) for key, label, _handler in entries)
            ),
            lambda action: actions.get(action, lambda: None)(),
        )

    def action_refresh_crate(self, *args, **kwargs):
        return self.crate_controller.action_refresh_crate(*args, **kwargs)

    def action_delete_crate(self, *args, **kwargs):
        return self.crate_controller.action_delete_crate(*args, **kwargs)

    def action_reset_crate_statuses(self, *args, **kwargs):
        return self.crate_controller.action_reset_crate_statuses(*args, **kwargs)

    def action_toggle_sidebar(self, *args, **kwargs):
        return self.crate_controller.action_toggle_sidebar(*args, **kwargs)

    def on_list_view_selected(self, *args, **kwargs):
        return self.crate_controller.on_list_view_selected(*args, **kwargs)

    def on_button_pressed(self, event):
        if event.button.id == 'folder-next':
            event.stop()
            self.local_controller.next_page()
            return
        if event.button.id == 'folder-open':
            event.stop()
            self.local_controller.choose_folder()
            return
        return self.crate_controller.on_button_pressed(event)

    def on_tree_node_selected(self, event):
        if event.control.id == 'explorer' and event.node.data is not None:
            event.stop()
            self.local_controller.open(event.node.data, node=event.node)

    def action_local_folder(self):
        self.local_controller.choose_folder()

    def action_local_export(self):
        self.local_controller.export_options()

    def action_local_analyze(self):
        self.local_controller.analyze()

    def action_local_edit(self):
        self.local_controller.edit()

    def action_local_playlist(self):
        self.local_controller.save_playlist()

    def action_local_page(self):
        self.local_controller.next_page()

    def action_local_pin(self):
        self.local_controller.pin()

    def action_local_split(self):
        self.local_controller.resize_split()

    def action_profile_playlists(self):
        self.local_controller.profile()

    def action_remove_track(self, *args, **kwargs):
        self.run_worker(self.crate_controller.action_remove_track(*args, **kwargs), description="remove_track")

    def action_undo_remove(self, *args, **kwargs):
        self.run_worker(self.crate_controller.action_undo_remove(*args, **kwargs), description="undo_remove")

    def action_filter_index(self, *args, **kwargs):
        return self.filter_controller.action_filter_index(*args, **kwargs)

    def action_sort_next(self, *args, **kwargs):
        return self.filter_controller.action_sort_next(*args, **kwargs)

    def action_sort_flip(self, *args, **kwargs):
        return self.filter_controller.action_sort_flip(*args, **kwargs)

    def action_toggle_select(self, *args, **kwargs):
        return self.filter_controller.action_toggle_select(*args, **kwargs)

    def action_select_range(self, *args, **kwargs):
        return self.filter_controller.action_select_range(*args, **kwargs)

    def action_select_visible(self, *args, **kwargs):
        return self.filter_controller.action_select_visible(*args, **kwargs)

    def action_toggle_handled(self, *args, **kwargs):
        return self.filter_controller.action_toggle_handled(*args, **kwargs)

    def action_start_search(self, *args, **kwargs):
        return self.filter_controller.action_start_search(*args, **kwargs)

    def action_leave_search(self, *args, **kwargs):
        return self.filter_controller.action_leave_search(*args, **kwargs)

    def action_clear_filters(self, *args, **kwargs):
        return self.filter_controller.action_clear_filters(*args, **kwargs)

    def on_input_changed(self, *args, **kwargs):
        return self.filter_controller.on_input_changed(*args, **kwargs)

    def on_input_submitted(self, *args, **kwargs):
        return self.filter_controller.on_input_submitted(*args, **kwargs)

    def action_mark_got(self, *args, **kwargs):
        self.run_worker(self.table_controller.action_mark_got(*args, **kwargs), description="mark_got")

    def action_mark_skip(self, *args, **kwargs):
        self.run_worker(self.table_controller.action_mark_skip(*args, **kwargs), description="mark_skip")

    def action_mark_new(self, *args, **kwargs):
        self.run_worker(self.table_controller.action_mark_new(*args, **kwargs), description="mark_new")

    def action_play_pause(self, *args, **kwargs):
        return self.playback_controller.action_play_pause(*args, **kwargs)

    def action_toggle_loaded(self, *args, **kwargs):
        return self.playback_controller.action_toggle_loaded(*args, **kwargs)

    def action_close_player(self, *args, **kwargs):
        return self.playback_controller.action_close_player(*args, **kwargs)

    def action_seek(self, *args, **kwargs):
        return self.playback_controller.action_seek(*args, **kwargs)

    def action_play_step(self, *args, **kwargs):
        return self.playback_controller.action_play_step(*args, **kwargs)

    def action_volume(self, *args, **kwargs):
        return self.playback_controller.action_volume(*args, **kwargs)

    def action_mute(self, *args, **kwargs):
        return self.playback_controller.action_mute(*args, **kwargs)

    def action_download_track(self, *args, **kwargs):
        self.run_worker(self.download_controller.action_download_track(*args, **kwargs), description="download_track")

    def action_batch_download(self, *args, **kwargs):
        self.run_worker(self.download_controller.action_batch_download(*args, **kwargs), description="batch_download")

    def action_open_link(self, *args, **kwargs):
        row = self.filter_controller.current_row()
        if row is not None and row.track.local_id:
            return self.playback_controller.action_play_pause()
        return self.opening_controller.action_open_link(*args, **kwargs)

    def action_search(self, *args, **kwargs):
        return self.opening_controller.action_search(*args, **kwargs)

    def action_cart_track(self, *args, **kwargs):
        return self.opening_controller.action_cart_track(*args, **kwargs)

    def action_cart_visible(self, *args, **kwargs):
        return self.opening_controller.action_cart_visible(*args, **kwargs)

    def action_setup_store_logins(self, *args, **kwargs):
        return self.opening_controller.action_setup_store_logins(*args, **kwargs)

    def action_check_store_logins(self, *args, **kwargs):
        return self.opening_controller.action_check_store_logins(*args, **kwargs)

    def action_reset_store_profile(self, *args, **kwargs):
        return self.opening_controller.action_reset_store_profile(*args, **kwargs)

    def action_open_visible(self, *args, **kwargs):
        return self.opening_controller.action_open_visible(*args, **kwargs)

    def action_open_beatport_tracks(self, *args, **kwargs):
        return self.opening_controller.action_open_beatport_tracks(*args, **kwargs)

    def action_copy_path(self, *args, **kwargs):
        self.run_worker(self.scan_controller.action_copy_path(*args, **kwargs), description="copy_path")

    def action_copy_local_file(self, *args, **kwargs):
        self.run_worker(self.scan_controller.action_copy_local_file(*args, **kwargs), description="copy_local_file")

    def update_status(self):
        self.table_controller.update_status()

    def action_local_resume(self):
        self.run_worker(self.local_controller.resume())

    def action_local_section(self):
        self.local_controller.toggle_section()
