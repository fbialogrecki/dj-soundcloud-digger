"""Narrowing the visible rows: by store, by search text, by whether you have handled them.

Composed by ``DiggerApp`` with explicit state and presentation callbacks.
"""

from textual.widgets import DataTable, Input

from ..models import LinkRecord
from .playlist import filter_rows, operation_targets, sort_rows
from .rows import Row

# What ``t`` cycles through, in order. The last three only when their column
# is switched on in Settings.
SORT_ORDER = ("title", "time", "genre", "status", "store", "bpm", "key", "year")
SORT_BASE = frozenset({"title", "time", "genre", "status", "store"})
# Which table column carries each sort key's arrow.
SORT_COLUMN = {
    "title": "Track", "time": "Time", "genre": "Genre", "status": "mark",
    "store": "Stores", "bpm": "BPM", "key": "Key", "year": "Year",
}


class FilterController:
    """Narrowing the visible rows: by store, by search text, by whether you have handled them."""

    def __init__(self, *, _paint_headers, _paint_row, cart_state, enabled_columns, notify, playlist_state, query_one, refresh_rows, state, update_status):
        self._paint_headers = _paint_headers
        self._paint_row = _paint_row
        self.cart_state = cart_state
        self.enabled_columns = enabled_columns
        self.notify = notify
        self.playlist_state = playlist_state
        self.query_one = query_one
        self.refresh_rows = refresh_rows
        self.state = state
        self.update_status = update_status

    def soft_matching_rows(self) -> list[Row]:
        """What search and hide-handled left, before the store filter.

        Split out because the store legend counts these: see ``_store_line``.
        """

        return filter_rows(
            self.playlist_state.rows, self.playlist_state.search_term,
            self.playlist_state.hide_handled, self.status_of,
        )

    def matching_rows(self) -> list[Row]:
        return sort_rows(
            self.soft_matching_rows(), self.playlist_state.store_filters,
            self.playlist_state.sort_key, self.playlist_state.sort_reverse, self.status_of,
        )

    def targets(self) -> list[Row]:
        return operation_targets(self.playlist_state.visible_rows, self.playlist_state.selected)

    def selected_rows(self) -> list[Row]:
        return operation_targets(self.playlist_state.visible_rows, self.playlist_state.selected, selected_only=True)

    def status_of(self, row: Row) -> str:
        return self.state.get(row.track.key)

    def record_to_open(self, row: Row) -> LinkRecord | None:
        """The link ``o`` would follow: the filtered store, else the best one."""

        if self.playlist_state.store_filters:
            for cat in self.playlist_state.present:
                if cat in self.playlist_state.store_filters:
                    chosen = row.record_for(cat)
                    if chosen is not None:
                        return chosen
        return row.records[0] if row.records else None

    def current_row(self) -> Row | None:
        table = self.query_one("#tracks", DataTable)
        if not self.playlist_state.visible_rows:
            return None
        index = table.cursor_row
        if 0 <= index < len(self.playlist_state.visible_rows):
            return self.playlist_state.visible_rows[index]
        return None

    def _apply_store_filter(self, category: str) -> None:
        if not category:
            self.playlist_state.store_filters.clear()
        else:
            if category in self.playlist_state.store_filters:
                self.playlist_state.store_filters.remove(category)
            else:
                self.playlist_state.store_filters.add(category)
        self.cart_state._pending_open = None
        self.refresh_rows(keep_cursor=False)

    def action_filter_index(self, index: int) -> None:
        """``0`` clears the filter, ``1``-``9`` toggle the nth store in this crate."""

        if index == 0:
            self._apply_store_filter("")
            return
        if index <= len(self.playlist_state.present):
            self._apply_store_filter(self.playlist_state.present[index - 1])
        else:
            self.notify(f"This playlist has no store {index}", timeout=2)

    # Sorting

    def action_sort_next(self) -> None:
        """Cycle the sort: source order, then each key in turn."""

        options = [None, *self._sort_options()]
        current = options.index(self.playlist_state.sort_key) if self.playlist_state.sort_key in options else 0
        self.playlist_state.sort_key = options[(current + 1) % len(options)]
        self.playlist_state.sort_reverse = False
        self._resort()

    def action_sort_flip(self) -> None:
        if self.playlist_state.sort_key is None:
            self.notify("Press t to choose what to sort by first", timeout=3)
            return
        self.playlist_state.sort_reverse = not self.playlist_state.sort_reverse
        self._resort()

    def _sort_options(self) -> list[str]:
        enabled = {name for name, _header, _width in self.enabled_columns()}
        return [name for name in SORT_ORDER if name in SORT_BASE or name in enabled]

    def _resort(self) -> None:
        self.refresh_rows(keep_cursor=False)
        self._paint_headers()
        label = self.playlist_state.sort_key or "playlist order"
        self.notify(f"Sorted by {label}{' (reversed)' if self.playlist_state.sort_reverse else ''}", timeout=2)

    # Selection

    def action_toggle_select(self) -> None:
        row = self.current_row()
        if row is None:
            return
        index = self.query_one("#tracks", DataTable).cursor_row
        if row.track.key in self.playlist_state.selected:
            self.playlist_state.selected.discard(row.track.key)
        else:
            self.playlist_state.selected.add(row.track.key)
            self.playlist_state._anchor = index
        self._paint_row(index)
        self.update_status()

    def action_select_range(self) -> None:
        """Select from the row selected last to the cursor, inclusive."""

        row = self.current_row()
        if row is None:
            return
        cursor = self.query_one("#tracks", DataTable).cursor_row
        start = self.playlist_state._anchor if self.playlist_state._anchor is not None else cursor
        low, high = sorted((start, cursor))
        for index in range(low, min(high, len(self.playlist_state.visible_rows) - 1) + 1):
            self.playlist_state.selected.add(self.playlist_state.visible_rows[index].track.key)
            self._paint_row(index)
        self.playlist_state._anchor = cursor
        self.update_status()

    def action_select_visible(self) -> None:
        if self.playlist_state.selected >= {row.track.key for row in self.playlist_state.visible_rows}:
            self.playlist_state.selected.clear()
            self.notify("Selection cleared", timeout=2)
        else:
            self.playlist_state.selected.update(row.track.key for row in self.playlist_state.visible_rows)
        self.refresh_rows()

    def clear_selection(self) -> None:
        if not self.playlist_state.selected:
            return
        self.playlist_state.selected.clear()
        self.playlist_state._anchor = None
        self.refresh_rows()

    def action_toggle_handled(self) -> None:
        self.playlist_state.hide_handled = not self.playlist_state.hide_handled
        self.refresh_rows(keep_cursor=False)

    def action_start_search(self) -> None:
        search = self.query_one("#search", Input)
        search.add_class("visible")
        search.focus()

    def action_leave_search(self) -> None:
        """Escape in the search box: back to the list, the filter still applied.

        Clearing it is one more Escape from the table; typing a term and losing
        it on the way back to the rows was the old behaviour.
        """

        search = self.query_one("#search", Input)
        if not self.playlist_state.search_term:
            search.remove_class("visible")
        self.query_one("#tracks", DataTable).focus()

    def action_clear_filters(self) -> None:
        """Escape peels one layer at a time: selection, then search, then the filters."""

        search = self.query_one("#search", Input)
        if self.playlist_state.selected:
            self.clear_selection()
        elif self.playlist_state.search_term or search.has_class("visible"):
            search.value = ""
            search.remove_class("visible")
            self.playlist_state.search_term = ""
            self.refresh_rows(keep_cursor=False)
        else:
            self.playlist_state.store_filters.clear()
            self.playlist_state.hide_handled = False
            self.cart_state._pending_open = None
            self.refresh_rows(keep_cursor=False)
        self.query_one("#tracks", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        self.playlist_state.search_term = event.value
        self.refresh_rows(keep_cursor=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search":
            return
        self.query_one("#tracks", DataTable).focus()
