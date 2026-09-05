"""The crate library: loading one, refreshing it, deleting it, and the sidebar that lists them.

Composed by ``DiggerApp`` with explicit state and presentation callbacks.
"""

from collections.abc import Sequence

from textual.widgets import Button, DataTable, Input, ListView

from .. import links as links_module
from ..crate_models import CrateHeader, CrateRecord
from ..models import LinkRecord
from .rows import Row
from .screens import ConfirmScreen
from .widgets import CrateButton, CrateItem


class CrateController:
    """The crate library: loading one, refreshing it, deleting it, and the sidebar that lists them."""

    def __init__(
        self,
        *,
        _start_dig,
        action_dig_link,
        call_next,
        current_row,
        notify,
        playlist_state,
        push_screen,
        query_one,
        refresh_rows,
        selected_rows,
        sidebar_state,
        state,
        library_service,
        io,
        run_worker,
        set_subtitle,
    ):
        self.io = io
        self.run_worker = run_worker
        self.set_subtitle = set_subtitle
        self._start_dig = _start_dig
        self.action_dig_link = action_dig_link
        self.call_next = call_next
        self.current_row = current_row
        self.notify = notify
        self.playlist_state = playlist_state
        self.push_screen = push_screen
        self.query_one = query_one
        self.refresh_rows = refresh_rows
        self.selected_rows = selected_rows
        self.sidebar_state = sidebar_state
        self.state = state
        self.library_service = library_service

    def _set_records(self, records: Sequence[LinkRecord]) -> None:
        self.playlist_state.rows = [
            Row(position=index + 1, track=group[0].track, records=group)
            for index, group in enumerate(links_module.group_by_track(records))
        ]
        self.playlist_state.present = links_module.present_categories(records)
        self.playlist_state.store_filters = {c for c in self.playlist_state.store_filters if c in self.playlist_state.present}

    def all_records(self) -> list[LinkRecord]:
        return [record for row in self.playlist_state.rows for record in row.records]

    def latest_crate(self) -> CrateHeader | None:
        if not self.sidebar_state.crates:
            return None
        # ``updated`` is refreshed_at or imported_at, whichever the crate has.
        return max(self.sidebar_state.crates, key=lambda header: header.updated)

    async def reload_sidebar(self) -> None:
        # clear() only queues the removal, so appending without awaiting it
        # leaves the old items in place and duplicates the list.
        self.sidebar_state.crates = await self.io(self.library_service.headers)
        listing = self.query_one("#crates", ListView)
        await listing.clear()
        for header in self.sidebar_state.crates:
            listing.append(CrateItem(header))
        if self.playlist_state.crate is not None:
            sources = [header.source for header in self.sidebar_state.crates]
            if self.playlist_state.crate.source in sources:
                listing.index = sources.index(self.playlist_state.crate.source)

    def highlighted_crate(self) -> CrateHeader | None:
        highlighted = self.query_one("#crates", ListView).highlighted_child
        if isinstance(highlighted, CrateItem):
            return highlighted.record
        if self.playlist_state.crate is None:
            return None
        return CrateHeader(
            self.playlist_state.crate.source,
            self.playlist_state.crate.title,
            self.playlist_state.crate.refreshed_at or self.playlist_state.crate.imported_at or "",
            self.playlist_state.crate.partial,
        )

    async def open_crate(self, header: CrateHeader) -> None:
        """Load the full record behind a sidebar entry and show it."""

        self.sidebar_state._load_generation += 1
        request = self.sidebar_state._load_generation
        view = self.playlist_state._view_generation
        generation = self.state.db.crate_generation(header.source)
        record = await self.io(self.library_service.load, header.source)
        if (request != self.sidebar_state._load_generation
                or view != self.playlist_state._view_generation
                or generation != self.state.db.crate_generation(header.source)):
            return
        if record is None:
            self.notify(f"'{header.title}' is gone from the library", severity="warning")
            self.call_next(self.reload_sidebar)
            return
        self.load_crate(record)

    def load_crate(self, record: CrateRecord) -> None:
        self.playlist_state.crate = record
        records = links_module.categorise_all(record.active_tracks)
        self.load_records(records, title=record.title)
        if record.preserve_order or any(track.local_id for track in record.active_tracks):
            self.set_tracks(record.active_tracks)

    def set_tracks(self, tracks) -> None:
        self.playlist_state.rows = [Row(index + 1, track, [] if track.local_id else links_module.categorise_all([track])) for index, track in enumerate(tracks)]
        self.playlist_state.present = links_module.present_categories(self.all_records())
        self.playlist_state.store_filters.clear()
        self.refresh_rows(keep_cursor=False)

    def load_records(self, records: Sequence[LinkRecord], *, title: str = "") -> None:
        self.playlist_state._view_generation += 1
        self._set_records(records)
        self.playlist_state.selected.clear()
        self.playlist_state._anchor = None
        if title:
            self.playlist_state.crate_title = title
            self.set_subtitle(title)
        self.playlist_state.search_term = ""
        search = self.query_one("#search", Input)
        search.value = ""
        search.remove_class("visible")
        self.refresh_rows(keep_cursor=False)
        self.query_one("#tracks", DataTable).focus()

    def refresh_crate(self, header: CrateHeader | None) -> None:
        if header is None:
            self.notify("No playlist to refresh", timeout=2)
            return
        if not header.source:
            self.notify("This playlist has no source to refresh from", severity="warning")
            return
        if header.source.startswith('local-playlist:'):
            self.run_worker(self.open_crate(header))
            return
        self._start_dig(header.source)

    def confirm_delete_crate(self, header: CrateHeader | None) -> None:
        if header is None:
            self.notify("No playlist to delete", timeout=2)
            return
        self.push_screen(
            ConfirmScreen(f"Delete the playlist '{header.title}'? This cannot be undone."),
            lambda confirmed: self.run_worker(self._crate_delete_answered(header, bool(confirmed))),
        )

    def action_refresh_crate(self) -> None:
        self.refresh_crate(self.highlighted_crate())

    def action_delete_crate(self) -> None:
        self.confirm_delete_crate(self.highlighted_crate())

    def action_reset_crate_statuses(self) -> None:
        """Asks first: statuses are global by track, and there is no undo for this."""

        if not self.playlist_state.rows:
            return
        title = self.playlist_state.crate_title or "this playlist"
        self.push_screen(
            ConfirmScreen(
                f"Reset the marks on {len(self.playlist_state.rows)} tracks in '{title}' to new? "
                "This cannot be undone."
            ),
            lambda confirmed: self.run_worker(self._reset_statuses()) if confirmed else None,
        )

    async def _reset_statuses(self) -> None:
        keys = [row.track.key for row in self.playlist_state.rows]
        await self.io(self.library_service.reset, keys)
        self.refresh_rows()
        self.notify("Reset all track statuses to 'new' for this playlist", timeout=3)

    async def _crate_delete_answered(self, header: CrateHeader, confirmed: bool) -> None:
        if not confirmed:
            return
        await self.io(self.library_service.delete, header.source)
        if self.playlist_state.crate is not None and self.playlist_state.crate.source == header.source:
            self.playlist_state.crate = None
            self.playlist_state.crate_title = ""
            self.load_records([])
        self.sidebar_state.crates = await self.io(self.library_service.headers)
        self.notify(f"Deleted '{header.title}'", timeout=3)
        remaining = self.latest_crate()
        if not self.playlist_state.rows and remaining is not None:
            await self.open_crate(remaining)
        self.call_next(self.reload_sidebar)

    def action_toggle_sidebar(self) -> None:
        self.query_one("#sidebar").toggle_class("collapsed")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        header = self.highlighted_crate()
        if header is not None:
            self.run_worker(self.open_crate(header))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button
        if isinstance(button, CrateButton):
            if button.intent == "refresh":
                self.refresh_crate(button.record)
            else:
                self.confirm_delete_crate(button.record)
            return
        if button.id == "crate-add":
            self.action_dig_link()

    def _reload_from_crate(self) -> None:
        """Rebuild the rows from the crate, keeping filters and cursor in place."""

        if self.playlist_state.crate is None:
            return
        if self.playlist_state.crate.preserve_order or any(track.local_id for track in self.playlist_state.crate.active_tracks):
            self.set_tracks(self.playlist_state.crate.active_tracks)
            return
        self._set_records(links_module.categorise_all(self.playlist_state.crate.active_tracks))
        self.refresh_rows()

    async def action_remove_track(self) -> None:
        """Drop a track from your copy. SoundCloud is read-only to us."""

        rows = self.selected_rows() or [self.current_row()]
        if rows == [None]:
            return
        if self.playlist_state.crate is None:
            self.notify("This is not a saved playlist, nothing to remove from", timeout=4)
            return
        source = self.playlist_state.crate.source
        generation = self.state.db.crate_generation(source)
        view = self.playlist_state._view_generation
        keys = [row.track.key for row in rows]
        record = await self.io(
            self.library_service.remove_tracks, source, generation, keys, removed=True,
        )
        if record is None or view != self.playlist_state._view_generation:
            return
        self.playlist_state.crate = record
        self.playlist_state._undone.extend(keys)
        self.playlist_state.selected.clear()
        self._reload_from_crate()
        if len(rows) == 1:
            self.notify(f"Removed {rows[0].track.label} - ctrl+z to undo", timeout=4)
        else:
            self.notify(f"Removed {len(rows)} tracks - ctrl+z puts them back one by one", timeout=4)

    async def action_undo_remove(self) -> None:
        if self.playlist_state.crate is None or not self.playlist_state._undone:
            self.notify("Nothing to undo", timeout=2)
            return
        key = self.playlist_state._undone[-1]
        source = self.playlist_state.crate.source
        generation = self.state.db.crate_generation(source)
        view = self.playlist_state._view_generation
        record = await self.io(
            self.library_service.remove_tracks, source, generation, [key], removed=False,
        )
        if record is None or view != self.playlist_state._view_generation:
            return
        self.playlist_state._undone.pop()
        self.playlist_state.crate = record
        self._reload_from_crate()
        self.notify("Restored", timeout=2)
