
import asyncio
import io
import json
import logging
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from rich.console import Console
from textual import events
from textual.coordinate import Coordinate
from textual.widgets import Button, DataTable, Input, Label, ListView, Select, Static

from dj_digger import (
    automation_errors,
    beatport_playlist,
    cart_models,
    crate_models,
    gate_models,
    library,
    links,
    soundcloud,
    store_match,
)
from dj_digger.config import AppConfig
from dj_digger.models import GOT, OPENED, SKIP, Cancelled, Crate, LinkRecord, Track
from dj_digger.player import Loaded, PlaybackUnavailable
from dj_digger.scanner import LocalMatch
from dj_digger.services import purchases as cart
from dj_digger.services.collection import DigOptions, TargetNotFound
from dj_digger.services.playback import Prepared, Stream
from dj_digger.state import TrackState
from dj_digger.tui import DiggerApp, keymap
from dj_digger.tui.audio import (
    PAUSE_GLYPH,
    PLAYER_HEIGHT,
    VOLUME_TRACK,
    VOLUME_TRACK_START,
    PlayerBar,
    PlayerControls,
    VolumeSlider,
)
from dj_digger.tui.rows import Row
from dj_digger.tui.screens import (
    AskLinkScreen,
    CartPlanScreen,
    CartResultScreen,
    ConfirmScreen,
    ContextMenuScreen,
    GateProfileScreen,
    HelpScreen,
    SettingsScreen,
    SoundCloudAuthScreen,
)
from dj_digger.tui.widgets import CrateButton, CrateItem, ErrorBanner, TrackTable


def run(scenario):
    """Drive an async Textual pilot from a plain sync test."""

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "group"),
    [
        (gate_models.GateAuthenticationRequired("Deezer"), "auth"),
        (gate_models.GateCaptchaRequired("captcha"), "captcha"),
        (gate_models.GateManualActionRequired("future"), "manual"),
        (gate_models.GateProtocolChanged("changed"), "protocol"),
        (gate_models.GateRejected("rejected"), "rejected"),
        (gate_models.GateSocialActionsDisabled("consent"), "consent"),
        (gate_models.GateDownloadError("download"), "download"),
        (soundcloud.SoundCloudError("download"), "download"),
    ],
)
def test_batch_gate_failures_have_actionable_summary_groups(error, group):
    from dj_digger.services.downloads import _gate_failure_group

    assert _gate_failure_group(error) == group


def test_error_banner_starts_collapsed_and_opens_on_the_summary(state):
    """Thirteen gate failures must not take half the screen the moment they land."""

    app = make_app(synthetic_records(1), state)

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            banner = app.query_one(ErrorBanner)
            banner.add_error("Batch failed [Artist - Track]: bad [response]")
            for index in range(12):
                banner.add_error(f"Failure {index}: " + "long message " * 8)
            await pilot.pause()

            summary = app.query_one("#error-summary", Static)
            assert "13 errors" in str(summary.render())
            assert not banner.has_class("expanded")
            assert banner.region.y == 0
            assert banner.size.width == app.size.width
            assert banner.size.height == 1
            assert app.query_one("#body").region.y >= banner.region.bottom

            await pilot.click("#error-summary")
            await pilot.pause()
            assert banner.has_class("expanded")
            message = app.query_one("#error-text", Static)
            assert "[Artist - Track]" in str(message.render())
            assert banner.size.height <= 12

            await pilot.click("#error-close")
            await pilot.pause()
            assert banner.errors == []
            assert not banner.has_class("visible")
            assert not banner.has_class("expanded")

    run(scenario)


def test_batch_summary_toast_renders_literal_failure_groups(state):
    app = make_app(synthetic_records(1), state)

    async def scenario():
        async with app.run_test(notifications=True) as pilot:
            app.download_controller._on_batch_download_complete(
                1,
                5,
                6,
                failure_groups={"manual": 2, "download": 2, "other": 1},
            )
            toasts = []
            for _ in range(20):
                await pilot.pause(0.05)
                toasts = list(app.query("Toast"))
                if toasts:
                    break
            assert toasts
            rendered = str(toasts[-1].render())
            assert "manual=2" in rendered
            assert "download=2" in rendered
            assert "other=1" in rendered

    run(scenario)


async def scroll_table(pilot, table, y):
    """Wait for Textual to size the table before setting a test viewport."""

    deadline = asyncio.get_running_loop().time() + 5
    await pilot.pause()
    while table.max_scroll_y <= 0:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("table never became scrollable")
        await pilot.pause(0.01)
    target = min(y, table.max_scroll_y)
    table.call_after_refresh(
        table.scroll_to,
        y=target,
        animate=False,
        force=True,
        immediate=True,
    )
    await pilot.pause()
    while table.scroll_offset.y != target:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"table never reached scroll offset {target}")
        await pilot.pause(0.01)
    await pilot.pause()
    assert table.scroll_offset.y == target


def bar_text(app, width=200):
    """The bottom bar as plain text - it is a Rich grid, not a bare string."""

    # force_terminal, or Rich clamps a non-tty to 80 columns whatever we ask for.
    console = Console(width=width, file=io.StringIO(), force_terminal=True)
    console.print(app.query_one("#status-legend", Static).content)
    console.print(app.query_one("#status-job", Static).content)
    return console.file.getvalue()


# Cell offsets into a table row: local/playing, mark, number, title, stores, genre, time.
MARK_CELL = 1
TITLE_CELL = 3
STORES_CELL = 4
GENRE_CELL = 5
TIME_CELL = 6


@pytest.fixture
def state(tmp_path):
    return TrackState(tmp_path / "digger.db")


@pytest.fixture
def records(tracks):
    return links.categorise_all(tracks)


def make_app(records, state, **kwargs):
    return DiggerApp(records, state=state, crate_title="test crate", **kwargs)


def synthetic_records(count, category="bandcamp"):
    """Ids run from 1: SoundCloud has no track 0, and 0 reads as "no id at all"."""

    return [
        LinkRecord(
            category=category,
            track=Track(
                title=f"Track {index}",
                permalink_url=f"https://soundcloud.com/a/{index}",
                id=index + 1,
            ),
            link_url=f"https://label.bandcamp.com/track/{index}",
            link_text="Buy",
        )
        for index in range(count)
    ]


def test_help_documents_every_key(records, state):
    """The footer only shows a handful, so help must not drift from the keymap."""

    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

            text = str(app.screen.query_one(Static).render())
            for _key, _action, _label, _group, _show, detail in keymap.KEYMAP:
                assert detail in text
            for section in (keymap.SELECTED, keymap.WHOLE_LIST, keymap.CRATES, keymap.OTHER):
                assert section in text

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

    run(scenario)


def test_optional_columns_follow_the_settings(records, state):
    app = make_app(records, state)
    records[0].track.bpm = 128.0
    records[0].track.release_year = 2024

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", DataTable)
            assert len(table.columns) == 7
            app.config.columns = ["bpm", "year"]
            app.table_controller.rebuild_columns()
            await pilot.pause()
            assert len(table.columns) == 9
            row = table.get_row_at(0)
            assert str(row[6]) == "128" and str(row[7]) == "2024"
            assert str(row[8]) == records[0].track.duration_label or str(row[8]) == "-"

    run(scenario)


def test_the_dim_colour_follows_the_theme(records, state):
    """bright_black vanished on light themes; the dim tone now comes from the theme."""

    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            dark = app.muted
            assert dark.startswith("#")
            app.theme = "textual-light"
            await settle(app, pilot)
            assert app.muted != dark and app.muted.startswith("#")
            assert app.config.theme == "textual-light", "the choice is saved"

    run(scenario)


def test_the_interface_colours_come_from_the_theme(records, state):
    """Store badges, marks and the waveform used to be terminal cyan/green/yellow whatever the theme."""

    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.theme = "gruvbox"
            await pilot.pause()
            primary = app.palette.primary.lower()
            assert primary.startswith("#")
            assert app.role("bold success").lower() == f"bold {app.palette.success.lower()}"
            legend = app.table_controller._store_line()
            assert primary in str(legend.spans[-2].style).lower() or any(
                primary in str(span.style).lower() for span in legend.spans
            )
            cells = app.table_controller._cells(app.playlist_state.visible_rows[0], None)
            assert any(primary in str(cell.style).lower() for cell in cells), "badges wear the theme's primary"

    run(scenario)


def test_choosing_a_theme_in_settings_persists_it(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            app.screen.query_one("#input-theme", Select).value = "nord"
            # The dialog scrolls; the button may sit below the screen edge.
            app.screen.query_one("#btn-save-settings", Button).press()
            await pilot.pause()
            await pilot.pause()

    run(scenario)
    assert app.theme == "nord"
    assert app.config.theme == "nord"
    assert AppConfig(app.config.path).theme == "nord"


def _beatport_record(track_id, url):
    return LinkRecord(
        category="beatport",
        track=Track(id=track_id, title=f"Track {track_id}", permalink_url=f"https://soundcloud.com/a/{track_id}"),
        link_url=url,
        link_text="Buy",
    )


def test_open_beatport_tracks_opens_only_exact_track_urls(state, monkeypatch):
    records = [
        _beatport_record(1, "http://www.beatport.com/track/signal/123456?utm=x"),
        _beatport_record(2, "https://www.beatport.com/release/album/99"),
    ]
    opened = []

    def fake_open_urls(urls, browser="default", **kwargs):
        for index, url in enumerate(urls):
            opened.append(url)
            kwargs["on_success"](index, url)
        return len(urls)

    monkeypatch.setattr("dj_digger.browser.open_urls", fake_open_urls)
    app = make_app(records, state)
    toasts = []
    app.notify = lambda message, **kwargs: toasts.append(message)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("P")
            await app.workers.wait_for_complete()
            await pilot.pause()

    run(scenario)
    assert opened == ["https://www.beatport.com/track/signal/123456"]
    assert any("1 release links skipped" in message for message in toasts)
    assert state.get(records[0].track.key) == OPENED


def test_open_beatport_tracks_asks_above_the_threshold(state, monkeypatch):
    records = [_beatport_record(n, f"https://www.beatport.com/track/t{n}/{100 + n}") for n in range(25)]
    opened = []
    monkeypatch.setattr(
        "dj_digger.browser.open_urls",
        lambda urls, browser="default", **kwargs: opened.extend(urls) or len(urls),
    )
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("P")
            await pilot.pause()
            assert opened == []
            await pilot.press("P")
            await app.workers.wait_for_complete()
            await pilot.pause()

    run(scenario)
    assert len(opened) == 25


def test_readme_lists_every_keymap_key():
    """The README tables are prose, so they are checked against the keymap, not generated."""

    from pathlib import Path

    from dj_digger.tui.keymap import KEY_DISPLAY

    readme = Path(__file__).resolve().parent.parent.joinpath("README.md").read_text(
        encoding="utf-8"
    ).lower()
    missing = []
    for key, *_rest in keymap.KEYMAP:
        shown = KEY_DISPLAY.get(key, key)
        for token in shown.split(", "):
            candidates = {f"`{token.lower()}`", f"`{key.lower()}`"}
            if token.lower() == "escape":
                candidates.add("`escape`")
            if not any(candidate in readme for candidate in candidates):
                missing.append(token)
    assert missing == [], f"README does not document: {missing}"


def test_primary_footer_actions_are_visible_and_grouped():
    visible = [(key, action) for key, action, _label, _group, show, _detail in keymap.KEYMAP if show]

    assert visible[:2] == [("o,enter", "open_link"), ("O", "open_visible")]
    assert visible[4:8] == [
        ("b", "search('bandcamp')"),
        ("B", "search('beatport')"),
        ("c", "cart_track"),
        ("C", "cart_visible"),
    ]
    assert all(
        shown == f"shift+{key.lower()}"
        for key, shown in keymap.KEY_DISPLAY.items()
        if len(key) == 1 and key.isupper()
    )
    assert [(key, action) for key, action in visible if key in {"g", "k", "u", "s"}] == [
        ("g", "mark_got"),
        ("k", "mark_skip"),
        ("u", "mark_new"),
        ("s", "open_settings"),
    ]


def test_the_command_palette_is_off(records, state):
    """It brought Textual's own Screenshot / Maximize / Theme commands into a DJ tool."""

    assert make_app(records, state).ENABLE_COMMAND_PALETTE is False


def test_reset_statuses_asks_first(records, state):
    state.set(records[0].track.key, GOT)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("U")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause()
            assert state.get(records[0].track.key) == GOT
            await pilot.press("U")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

    run(scenario)
    assert state.get(records[0].track.key) == "new"


def test_the_bottom_bar_is_the_legend_with_the_view_state_on_the_right(records, state):
    """One bar: every store on the left, only what changes the view on the right."""

    app = make_app(records, state)

    async def scenario():
        async with app.run_test(size=(160, 24)) as pilot:
            await settle(app, pilot)
            legend = str(app.query_one("#status-legend", Static).content)
            job = str(app.query_one("#status-job", Static).content)
            assert "0 all" in legend and all(store in legend for store in app.playlist_state.present)
            assert job == "", "no track counts: they were never looked at"
            assert app.query_one("#status").size.height == 1
            await pilot.press("h")
            await pilot.pause()
            assert "hiding handled" in str(app.query_one("#status-job", Static).content)
            # The sidebar says which crate this is, so the bar does not repeat it.
            assert "test crate" not in bar_text(app)

    run(scenario)


def test_the_bar_stays_one_line_and_scrolls_when_cramped(records, state):
    """Wrapping would grow it back into the stack of bars it replaced."""

    app = make_app(records, state)

    async def scenario():
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            bar = app.query_one("#status")
            assert bar.size.height == 1
            assert bar.max_scroll_x > 0
            assert all(store in str(app.query_one("#status-legend", Static).content) for store in app.playlist_state.present)

    run(scenario)


def test_the_bar_sits_below_the_table(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test():
            order = [
                widget.id
                for widget in app.screen.children
                if widget.id in {"body", "status"}
            ]
            assert order == ["body", "status"]

    run(scenario)


def test_every_track_gets_a_row(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test():
            assert app.query_one("#tracks", DataTable).row_count == len(records)

    run(scenario)


def test_a_track_in_two_stores_is_still_one_row(state):
    """Buying it on Bandcamp or earning it on a gate is one decision, not two."""

    track = Track(
        title="Everywhere",
        permalink_url="https://soundcloud.com/a/b",
        id=77,
        purchase_url="https://hypeddit.com/x/y",
        description="also at https://label.bandcamp.com/album/x",
    )
    records = links.categorise_all([track])
    assert len(records) == 2
    app = make_app(records, state)

    async def scenario():
        async with app.run_test():
            assert app.query_one("#tracks", DataTable).row_count == 1
            assert app.playlist_state.rows[0].categories == ["bandcamp", "gate"]

    run(scenario)


def test_marking_got_persists_and_moves_on(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("g")
            await settle(app, pilot)
            assert state.get(records[0].track.key) == GOT
            # Cursor should have advanced so you can keep hammering the key.
            assert app.query_one("#tracks", DataTable).cursor_row == 1

    run(scenario)

    # A fresh state object reads the same verdict back off disk.
    assert TrackState(state.path).get(records[0].track.key) == GOT


def test_skipping_persists(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("k")
            await settle(app, pilot)

    run(scenario)
    assert state.get(records[0].track.key) == SKIP


@pytest.mark.parametrize("key,status", [("k", SKIP), ("g", GOT)])
def test_pressing_the_same_mark_again_clears_it(records, state, key, status):
    """Pressing the same mark again is the natural way to undo it."""

    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#tracks", DataTable)
            await pilot.press(key)
            await settle(app, pilot)
            assert state.get(records[0].track.key) == status
            assert table.cursor_row == 1

            table.move_cursor(row=0)
            await pilot.press(key)
            await settle(app, pilot)
            assert state.get(records[0].track.key) == "new"
            # Undoing should not march the cursor onwards.
            assert table.cursor_row == 0

    run(scenario)


def test_a_crate_name_with_brackets_survives(state):
    """Label renders Textual markup, so [2026] would vanish from the sidebar."""

    saved_crate(1, source="https://soundcloud.com/a/sets/b", title="Techno [2026] vinyl")
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            label = app.query_one(".crate-name", Label)
            assert "[2026]" in str(label.render())

    run(scenario)


def test_unmarking_clears_the_status(records, state):
    state.set(records[0].track.key, GOT)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("u")
            await settle(app, pilot)

    run(scenario)
    assert state.get(records[0].track.key) == "new"


def test_opening_a_link_marks_it_opened(records, state, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "dj_digger.browser.open_url",
        lambda url, browser="default": opened.append(url) or True,
    )
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("o")

    run(scenario)
    assert opened == [records[0].link_url]
    assert state.get(records[0].track.key) == OPENED


def test_opening_a_link_repaints_only_that_row(records, state, monkeypatch):
    """A rebuilt table flickers and loses the scroll; one row changed, so paint one."""

    monkeypatch.setattr(
        "dj_digger.browser.open_url", lambda url, browser="default": True
    )
    app = make_app(records, state)
    clears = []

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            monkeypatch.setattr(DataTable, "clear", lambda self, *a, **k: clears.append(1))
            await pilot.press("o")
            await pilot.pause()

    run(scenario)
    assert state.get(records[0].track.key) == OPENED
    assert clears == [], "opening a link must not rebuild the table"


def test_ctrl_c_quits_like_q(records, state, monkeypatch):
    app = make_app(records, state)
    exits = []
    monkeypatch.setattr(app, "exit", lambda *a, **k: exits.append(1))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()

    run(scenario)
    assert exits == [1]


def test_ctrl_c_quits_from_the_search_box(records, state, monkeypatch):
    """Input binds ctrl+c to copy; quitting must win there too."""

    app = make_app(records, state)
    exits = []
    monkeypatch.setattr(app, "exit", lambda *a, **k: exits.append(1))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("slash")
            await pilot.pause()
            assert isinstance(app.focused, Input)
            await pilot.press("ctrl+c")
            await pilot.pause()

    run(scenario)
    assert exits == [1]


def test_a_slow_browser_does_not_block_the_interface(records, state, monkeypatch):
    """On WSL the handoff to Windows can take seconds; the cursor must keep moving."""

    release = Event()
    monkeypatch.setattr(
        "dj_digger.browser.open_url",
        lambda url, browser="default": release.wait(5),
    )
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.press("down")
            await pilot.pause()
            assert app.query_one("#tracks", DataTable).cursor_row == 1
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

    run(scenario)
    assert state.get(records[0].track.key) == OPENED


def test_enter_opens_the_link_exactly_once(records, state, monkeypatch):
    """The table binds enter itself, so the app binding must not fire as well."""

    opened = []
    monkeypatch.setattr(
        "dj_digger.browser.open_url",
        lambda url, browser="default": opened.append(url) or True,
    )
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("enter")

    run(scenario)
    assert opened == [records[0].link_url]


def test_single_click_only_selects_the_track(records, state, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "dj_digger.browser.open_url",
        lambda url, browser="default": opened.append(url) or True,
    )
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.click("#tracks", offset=(10, 1))
            await pilot.pause()
            assert app.query_one("#tracks", DataTable).cursor_row == 0
            assert opened == []
            assert state.get(records[0].track.key) != OPENED

            await pilot.press("enter")

    run(scenario)
    assert opened == [records[0].link_url]


def test_right_click_opens_the_track_menu_without_opening_a_link(
    state, monkeypatch
):
    opened = []
    monkeypatch.setattr(
        "dj_digger.browser.open_url",
        lambda url, browser="default": opened.append(url) or True,
    )
    records = synthetic_records(2)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.click("#tracks", offset=(10, 2), button=3)
            await pilot.pause()
            assert isinstance(app.screen, ContextMenuScreen)
            assert "Track 1" in str(app.screen.query_one(Label).render())
            assert opened == []

            await pilot.press("enter")
            await pilot.pause()

    run(scenario)
    assert opened == [records[1].link_url]


def cart_plan_for(record, *, already=False):
    return cart_models.CartPlan(
        items=(
            cart_models.CartItem(
                track_key=record.track.key,
                track_label=record.track.label,
                store=record.category,
                source_url=record.link_url,
                product_url=record.link_url,
                product_id="123",
                product_title=record.track.title,
                price=Decimal("1.25"),
                currency="GBP",
                already_in_cart=already,
            ),
        )
    )


def patch_cart_session(monkeypatch, handler):
    async def run_batch(_self, requests, cancel, *, approve, progress=None, manual=None):
        return await handler(list(requests), cancel, approve, progress)

    monkeypatch.setattr(cart.CartBrowserSession, "run_batch", run_batch)


def test_c_preflights_and_adds_the_selected_track(records, state, monkeypatch):
    bandcamp_record = next(record for record in records if record.category == "bandcamp")
    prepared = []
    executed = []

    async def run_cart(requests, _cancel, approve, _progress):
        prepared.extend(requests)
        plan = cart_plan_for(bandcamp_record)
        assert await approve(plan)
        executed.append(plan)
        return cart_models.CartBatchOutcome(
            (
                cart_models.CartResult(
                    bandcamp_record.track.key,
                    bandcamp_record.track.label,
                    "bandcamp",
                    "added",
                ),
            ),
            ("bandcamp",),
        )

    patch_cart_session(monkeypatch, run_cart)
    app = make_app([bandcamp_record], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("c")
            for _ in range(10):
                await pilot.pause()
                if executed:
                    break

    run(scenario)
    assert prepared[0].links == (("bandcamp", bandcamp_record.link_url),)
    assert len(executed) == 1


def test_beatport_result_creates_playlist_and_opens_supported_transfer(
    state, monkeypatch, tmp_path
):
    candidate = Track(
        title="Signal",
        artist="Artist",
        permalink_url="https://soundcloud.com/artist/signal",
        id=4242,
    )
    record = LinkRecord(
        "beatport",
        candidate,
        "https://www.beatport.com/track/signal/123?token=secret",
        "Buy",
    )
    outcome = cart_models.CartBatchOutcome(
        (
            cart_models.CartResult(
                candidate.key,
                candidate.label,
                "beatport",
                "playlist_ready",
                "ready for Beatport playlist transfer",
                "playlist_ready",
                record.link_url,
            ),
        )
    )
    opened = []
    copied = []

    async def run_cart(_requests, _cancel, _approve, _progress):
        return outcome

    def open_url(url, browser):
        opened.append((url, browser))
        return True

    patch_cart_session(monkeypatch, run_cart)
    monkeypatch.setattr("dj_digger.browser.open_url", open_url)
    monkeypatch.setattr(
        "dj_digger.beatport_playlist._create_soundiiz_import",
        lambda requests, outcome, title: "https://soundiiz.com/go/import-playlist/test-token",
    )
    monkeypatch.setattr(
        "dj_digger.clipboard.copy_to_clipboard",
        lambda text: copied.append(text) or True,
    )
    app = make_app([record], state)
    app.config.download_directory = str(tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("c")
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, CartResultScreen):
                    break
            assert isinstance(app.screen, CartResultScreen)
            assert app.screen.query_one("#cart-result-playlist", Button)
            app.screen.dismiss("playlist")
            for _ in range(20):
                await pilot.pause()
                if opened:
                    break

    run(scenario)
    safe_url = "https://www.beatport.com/track/signal/123"
    assert copied == [safe_url]
    assert opened[0][0] == "https://soundiiz.com/go/import-playlist/test-token"
    playlist = next(tmp_path.rglob("Beatport playlist.txt"))
    assert playlist.read_text(encoding="utf-8") == safe_url + "\n"


def test_beatport_playlist_uses_metadata_for_a_release_link(tmp_path):
    candidate = Track(
        title="Lights On",
        artist="Revan",
        permalink_url="https://soundcloud.com/revan/lights-on",
        id=4243,
    )
    request = cart_models.CartRequest(
        candidate,
        (("beatport", "https://www.beatport.com/release/lights-on/123"),),
    )
    outcome = cart_models.CartBatchOutcome(
        (
            cart_models.CartResult(
                candidate.key,
                candidate.label,
                "beatport",
                "playlist_ready",
                "ready for Beatport playlist transfer",
                "playlist_ready",
                request.links[0][1],
            ),
        )
    )

    lines = beatport_playlist._beatport_playlist_lines([request], outcome)
    first = beatport_playlist._write_beatport_playlist(lines, tmp_path)
    second = beatport_playlist._write_beatport_playlist(lines, tmp_path)

    assert lines == ("Revan - Lights On",)
    assert first.name == "Beatport playlist.txt"
    assert second.name == "Beatport playlist (2).txt"


def test_soundiiz_import_posts_the_tracklist_and_returns_its_review_url(monkeypatch):
    candidate = Track(
        title="Lights On",
        artist="Revan",
        permalink_url="https://soundcloud.com/revan/lights-on",
        id=4243,
    )
    request = cart_models.CartRequest(
        candidate,
        (("beatport", "https://www.beatport.com/track/lights-on/123"),),
    )
    outcome = cart_models.CartBatchOutcome(
        (
            cart_models.CartResult(
                candidate.key,
                candidate.label,
                "beatport",
                "playlist_ready",
                code="playlist_ready",
            ),
        )
    )
    posted = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"shareUrl": "https://soundiiz.com/go/import-playlist/token"}

    def post(url, **kwargs):
        posted.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr("dj_digger.beatport_playlist.requests.post", post)

    url = beatport_playlist._create_soundiiz_import([request], outcome, "Dig finds")

    assert url == "https://soundiiz.com/go/import-playlist/token"
    assert posted["url"] == "https://soundiiz.com/go/import-playlist"
    assert posted["json"] == {
        "title": "Dig finds",
        "sourceName": "dj-digger",
        "destination": "beatport",
        "tracklist": [{"title": "Lights On", "artists": ["Revan"]}],
    }


@pytest.mark.parametrize(
    ("raw_title", "uploader", "title", "artists"),
    [
        (
            "Full Premiere: Bambook & Mennie feat. Cari Golden – Slip Away (Original Mix)",
            "DHA FM (Deep House Amsterdam)",
            "Slip Away (Original Mix)",
            ["Bambook & Mennie feat. Cari Golden", "Cari Golden"],
        ),
        (
            "PREMIERE : André Hommen - Sensory [Objektivity]",
            "Sweet Music",
            "Sensory",
            ["André Hommen"],
        ),
        (
            "Jimmy & Fred - Red (Preview) | Exploited",
            "Exploited",
            "Red",
            ["Jimmy & Fred"],
        ),
        (
            "Aaron Jackson - Follow(Original Mix)*Nite Records*",
            "Aaron Jackson",
            "Follow(Original Mix)",
            ["Aaron Jackson"],
        ),
        (
            "Argy & MAMA - Who Am I (Rampa Remix) - BPitch Control",
            "Rampa",
            "Who Am I (Rampa Remix)",
            ["Argy & MAMA", "Rampa"],
        ),
        (
            "Andre Winter-Dogma",
            "AndreWinter",
            "Dogma",
            ["Andre Winter"],
        ),
        (
            "Skinnybit -Superstition [OUT NOW]",
            "Black Lizard Records",
            "Superstition",
            ["Skinnybit"],
        ),
        (
            "Dense & Pika feat. Melodys Enemy - From Nothing - Kneaded Pains",
            "Dense & Pika",
            "From Nothing",
            ["Dense & Pika feat. Melodys Enemy", "Melodys Enemy"],
        ),
        (
            "Vijay & Sofia Zlatko - I Like It ( Vintage Culture remix )",
            "Uploader",
            "I Like It (Vintage Culture remix)",
            ["Vijay & Sofia Zlatko", "Vintage Culture"],
        ),
    ],
)
def test_soundiiz_metadata_removes_promo_uploader_noise(
    raw_title, uploader, title, artists
):
    track = Track(title=raw_title, artist=uploader, permalink_url="https://soundcloud.com/x/y")
    request = cart_models.CartRequest(track, (("beatport", "https://www.beatport.com/release/x/1"),))

    assert beatport_playlist._soundiiz_metadata(request) == {
        "title": title,
        "artists": artists,
    }


def test_soundiiz_metadata_adds_remixer_to_catalog_artists():
    track = Track(
        title="Jochen Pash - Keep On Trying (Return Of The Jaded Remix)",
        artist="Return of the Jaded",
        permalink_url="https://soundcloud.com/x/keep-on-trying",
    )
    request = cart_models.CartRequest(track, (("beatport", "https://www.beatport.com/release/x/1"),))

    assert beatport_playlist._soundiiz_metadata(request) == {
        "title": "Keep On Trying (Return Of The Jaded Remix)",
        "artists": ["Jochen Pash", "Return Of The Jaded"],
    }


def test_exact_beatport_result_replaces_the_saved_release_link():
    track = Track(
        title="Artist - Signal",
        artist="Uploader",
        permalink_url="https://soundcloud.com/x/signal",
        purchase_url="https://www.beatport.com/release/signal/12",
    )
    record = crate_models.CrateRecord("source", "Playlist", [track])
    exact = "https://www.beatport.com/track/signal/34"
    outcome = cart_models.CartBatchOutcome(
        (
            cart_models.CartResult(
                track.key,
                track.label,
                "beatport",
                "playlist_ready",
                code="playlist_ready",
                url=exact,
            ),
        )
    )

    assert store_match._remember_exact_beatport_links(record, outcome)
    assert track.purchase_url == exact

    legacy = Track(
        title="Keep On Trying",
        permalink_url="https://soundcloud.com/x/keep",
        purchase_url="https://pro.beatport.com/release/keep-on-trying-part-2/1491414",
    )
    legacy_record = crate_models.CrateRecord("source", "Playlist", [legacy])
    assert store_match._remember_exact_beatport_links(
        legacy_record, cart_models.CartBatchOutcome(())
    )
    assert legacy.purchase_url == (
        "https://www.beatport.com/release/keep-on-trying-part-2/1491414"
    )


def test_store_settings_only_open_the_bandcamp_session(records, state, monkeypatch):
    stores_seen = []

    async def setup(_self, stores, _cancel, _progress):
        stores_seen.append(tuple(stores))

    monkeypatch.setattr(cart.CartBrowserSession, "setup_logins", setup)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            app.action_setup_store_logins()
            for _ in range(20):
                await pilot.pause()
                if not app.cart_state._cart_busy:
                    break

    run(scenario)
    assert stores_seen == [("bandcamp",)]


def test_c_installs_missing_chromium_then_retries_preflight(records, state, monkeypatch):
    bandcamp_record = next(record for record in records if record.category == "bandcamp")
    installed = []
    prepared = []
    executed = []

    async def run_cart(requests, _cancel, approve, _progress):
        prepared.append(requests)
        if not installed:
            raise automation_errors.ChromiumMissing("Chromium is required for store carts")
        plan = cart_plan_for(bandcamp_record)
        assert await approve(plan)
        executed.append(plan)
        return cart_models.CartBatchOutcome(
            (
                cart_models.CartResult(
                    bandcamp_record.track.key,
                    bandcamp_record.track.label,
                    "bandcamp",
                    "added",
                ),
            ),
            ("bandcamp",),
        )

    def install(_cancel):
        installed.append(True)

    patch_cart_session(monkeypatch, run_cart)
    monkeypatch.setattr(cart, "install_chromium", install)
    app = make_app([bandcamp_record], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("c")
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ConfirmScreen):
                    break
            assert isinstance(app.screen, ConfirmScreen)
            assert "Chromium" in str(app.screen.query_one(Label).render())
            await pilot.press("y")
            for _ in range(30):
                await pilot.pause()
                if executed:
                    break

    run(scenario)
    assert installed == [True]
    assert len(prepared) == 2
    assert len(executed) == 1


def test_shift_c_confirms_the_visible_preflight_before_mutating(state, monkeypatch):
    record = synthetic_records(1)[0]
    plan = cart_plan_for(record)
    executed = []

    async def run_cart(_requests, _cancel, approve, _progress):
        approved = await approve(plan)
        if approved is None:
            return cart_models.CartBatchOutcome((), cancelled=True)
        executed.append(approved)
        return cart_models.CartBatchOutcome(
            (cart_models.CartResult(record.track.key, record.track.label, "bandcamp", "added"),),
            ("bandcamp",),
        )

    patch_cart_session(monkeypatch, run_cart)
    app = make_app([record], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("C")
            for _ in range(10):
                await pilot.pause()
                if isinstance(app.screen, CartPlanScreen):
                    break
            assert isinstance(app.screen, CartPlanScreen)
            assert executed == []
            row = app.screen.query_one("#cart-plan-table", DataTable).get_row_at(0)
            assert "GBP 1.25" in str(row)
            await pilot.press("y")
            for _ in range(10):
                await pilot.pause()
                if executed:
                    break

    run(scenario)
    assert executed == [plan]


def test_cart_plan_accepts_a_comma_price_and_recalculates_the_total(state):
    record = synthetic_records(1)[0]
    base = cart_plan_for(record).items[0]
    plan = cart_models.CartPlan(
        items=(
            replace(
                base,
                price=Decimal("1.00"),
                minimum_price=Decimal("1.00"),
                price_step=Decimal("0.25"),
                price_editable=True,
            ),
        )
    )
    app = make_app([record], state)
    answer = []

    async def scenario():
        async with app.run_test() as pilot:
            screen = CartPlanScreen(plan)
            app.push_screen(screen, answer.append)
            await pilot.pause()
            price = screen.query_one("#cart-plan-price", Input)
            await pilot.press("e")
            await pilot.pause()
            assert price.has_focus
            price.value = "1,50"
            screen.action_approve()
            await pilot.pause()

    run(scenario)
    assert answer[0].items[0].price == Decimal("1.50")


def test_cart_plan_keeps_continue_visible_in_a_short_terminal(state):
    record = synthetic_records(1)[0]
    base = cart_plan_for(record).items[0]
    plan = cart_models.CartPlan(
        tuple(
            replace(base, track_key=str(index), track_label=f"Track {index}")
            for index in range(6)
        )
    )
    app = make_app([record], state)
    answer = []

    async def scenario():
        async with app.run_test(size=(90, 20)) as pilot:
            screen = CartPlanScreen(plan)
            app.push_screen(screen, answer.append)
            await pilot.pause()
            button = screen.query_one("#cart-plan-add", Button)
            assert button.region.y + button.region.height <= app.size.height
            await pilot.press("enter")
            for _ in range(5):
                await pilot.pause()
                if answer:
                    break

    run(scenario)
    assert answer == [plan]


def test_batch_cart_refuses_a_filter_without_supported_stores(state, monkeypatch):
    record = synthetic_records(1)[0]

    async def fail(*_args, **_kwargs):
        pytest.fail("unsupported filter must not open a browser")

    monkeypatch.setattr(cart.CartBrowserSession, "run_batch", fail)
    app = make_app([record], state)
    app.playlist_state.store_filters = {"gate"}

    app.action_cart_visible()

    assert app.cart_state._cart_busy is False


def test_cart_fallback_request_keeps_bandcamp_first(state):
    track = Track(
        title="Signal",
        permalink_url="https://soundcloud.com/a/signal",
        id=42,
        purchase_url="https://label.bandcamp.com/album/release",
        description="https://www.beatport.com/release/release/99",
    )
    app = make_app(links.categorise(track), state)
    app.playlist_state.store_filters = {"beatport", "bandcamp"}

    request = app.opening_controller._cart_request(app.playlist_state.rows[0])

    assert [store for store, _url in request.links] == ["bandcamp", "beatport"]


def test_explicit_bandcamp_and_beatport_filters_create_independent_requests(state):
    track = Track(
        title="Signal",
        permalink_url="https://soundcloud.com/a/signal",
        id=43,
        purchase_url="https://label.bandcamp.com/album/release",
        description="https://www.beatport.com/release/release/99",
    )
    app = make_app(links.categorise(track), state)
    app.playlist_state.store_filters = {"beatport", "bandcamp"}

    requests = app.opening_controller._cart_requests(app.playlist_state.rows[0])

    assert [request.links for request in requests] == [
        (("bandcamp", "https://label.bandcamp.com/album/release"),),
        (("beatport", "https://www.beatport.com/release/release/99"),),
    ]


def test_batch_cart_leaves_got_and_skipped_tracks_out(state, monkeypatch):
    records = synthetic_records(3)
    state.set(records[0].track.key, GOT)
    state.set(records[1].track.key, SKIP)
    seen = []

    async def run_cart(requests, _cancel, _approve, _progress):
        seen.extend(requests)
        return cart_models.CartBatchOutcome(
            (
                cart_models.CartResult(
                    records[2].track.key,
                    records[2].track.label,
                    "bandcamp",
                    "skipped",
                ),
            )
        )

    patch_cart_session(monkeypatch, run_cart)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("C")
            for _ in range(10):
                await pilot.pause()
                if seen:
                    break

    run(scenario)
    assert [item.track.key for item in seen] == [records[2].track.key]


def test_number_keys_select_the_stores_this_crate_actually_has(records, state):
    """`1` is the first store present, not a fixed category - crates differ."""

    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            assert app.playlist_state.present == ["no-link", "bandcamp", "others"]

            await pilot.press("2")
            assert app.playlist_state.store_filters == {"bandcamp"}
            expected = sum(1 for record in records if record.category == "bandcamp")
            assert app.query_one("#tracks", DataTable).row_count == expected

            await pilot.press("3")
            assert app.playlist_state.store_filters == {"bandcamp", "others"}

            await pilot.press("0")
            assert app.playlist_state.store_filters == set()
            assert app.query_one("#tracks", DataTable).row_count == len(records)

    run(scenario)


def test_a_number_key_beyond_the_stores_present_is_a_no_op(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("9")
            assert app.playlist_state.store_filters == set()

    run(scenario)


def test_hiding_handled_rows(records, state):
    state.set(records[0].track.key, GOT)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("h")
            assert app.query_one("#tracks", DataTable).row_count == len(records) - 1

    run(scenario)


def test_search_filters_by_artist_and_title(records, state):
    app = make_app(records, state)
    target = records[0].track.label

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("slash")
            app.query_one("#search").value = target
            await pilot.pause()
            rows = app.query_one("#tracks", DataTable).row_count
            assert 1 <= rows < len(records)

    run(scenario)


def test_search_matches_any_word_in_any_order(state):
    records = synthetic_records(3)
    records[1].track.artist = "Bonobo"
    records[1].track.title = "Kerala (Extended Mix)"
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("slash")
            app.query_one("#search").value = "extended bonobo"
            await pilot.pause()
            assert [row.track.artist for row in app.playlist_state.visible_rows] == ["Bonobo"]

    run(scenario)


def test_search_reaches_genre_and_tags(state):
    records = synthetic_records(3)
    records[2].track.genre = "Dub Techno"
    records[0].track.tags = ["minimal", "berlin"]
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("slash")
            app.query_one("#search").value = "dub"
            await pilot.pause()
            assert [row.position for row in app.playlist_state.visible_rows] == [3]
            app.query_one("#search").value = "berlin"
            await pilot.pause()
            assert [row.position for row in app.playlist_state.visible_rows] == [1]

    run(scenario)


def test_escape_drops_the_selection_before_the_search(state):
    records = synthetic_records(3)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("slash")
            app.query_one("#search").value = "track"
            await pilot.pause()
            await pilot.press("escape")  # leaves the box, keeps the term
            await pilot.pause()
            await pilot.press("v")
            assert app.playlist_state.selected == {records[0].track.key}
            await pilot.press("escape")
            assert app.playlist_state.selected == set() and app.playlist_state.search_term == "track"
            await pilot.press("escape")
            assert app.playlist_state.search_term == ""

    run(scenario)


def test_t_sorts_by_time_and_shows_it_in_the_header(state):
    records = synthetic_records(3)
    records[0].track.duration = 300_000
    records[1].track.duration = 100_000
    records[2].track.duration = 200_000
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("t")  # title
            await pilot.press("t")  # time
            await pilot.pause()
            assert [row.position for row in app.playlist_state.visible_rows] == [2, 3, 1]
            table = app.query_one("#tracks", DataTable)
            assert "\u25b2" in str(table.columns[app.playlist_state._column_keys["Time"]].label)
            await pilot.press("T")
            await pilot.pause()
            assert [row.position for row in app.playlist_state.visible_rows] == [1, 3, 2]
            assert "\u25bc" in str(table.columns[app.playlist_state._column_keys["Time"]].label)
            assert "sort: time" in str(app.table_controller._progress_line())

    run(scenario)


def test_sorting_survives_a_mark(state):
    records = synthetic_records(3)
    records[0].track.duration = 300_000
    records[1].track.duration = 100_000
    records[2].track.duration = 200_000
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("t")
            await pilot.press("t")
            await pilot.press("g")
            await settle(app, pilot)
            assert [row.position for row in app.playlist_state.visible_rows] == [2, 3, 1]

    run(scenario)


def test_v_selects_and_shift_v_extends(state):
    records = synthetic_records(4)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("v")
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("V")
            await pilot.pause()
            assert app.playlist_state.selected == {records[i].track.key for i in range(3)}
            assert "3 selected" in bar_text(app)
            await pilot.press("ctrl+a")
            assert len(app.playlist_state.selected) == 4
            await pilot.press("ctrl+a")
            assert app.playlist_state.selected == set()

    run(scenario)


def test_batch_download_uses_the_selection_when_there_is_one(state, monkeypatch):
    records = synthetic_records(3, category="soundcloud")
    for record in records:
        record.track.downloadable = True
        record.track.has_downloads_left = True
    app = make_app(records, state)
    started = []
    monkeypatch.setattr(app.download_controller, "batch_download_in_background", lambda items, handle=None: started.extend(items))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("down")
            await pilot.press("v")
            await pilot.press("D")
            await pilot.pause()

    run(scenario)
    assert [row.position for row, _url in started] == [2]


def test_marking_a_selection_marks_every_selected_row(state):
    records = synthetic_records(3)
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("v")
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("v")
            await pilot.press("g")
            await settle(app, pilot)

    run(scenario)
    assert [state.get(r.track.key) for r in records] == [GOT, "new", GOT]


def test_escape_clears_every_filter(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("2")
            await pilot.press("h")
            await pilot.press("escape")
            assert app.playlist_state.store_filters == set()
            assert app.playlist_state.hide_handled is False
            assert app.query_one("#tracks", DataTable).row_count == len(records)

    run(scenario)


def test_open_all_asks_before_flooding_the_browser(state, monkeypatch):
    """The whole point of the TUI is not opening 282 tabs by accident."""

    opened = []
    monkeypatch.setattr(
        "dj_digger.browser.open_urls",
        lambda urls, browser="default", **kwargs: opened.extend(urls) or len(urls),
    )
    app = make_app(synthetic_records(25), state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("O")
            assert opened == []  # first press only warns
            await pilot.press("O")
            assert len(opened) == 25

    run(scenario)


def test_open_all_goes_straight_through_for_a_short_list(state, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "dj_digger.browser.open_urls",
        lambda urls, browser="default", **kwargs: opened.extend(urls) or len(urls),
    )
    app = make_app(synthetic_records(3), state)

    async def scenario():
        async with app.run_test() as pilot:
            assert opened == []
            await pilot.press("O")
            assert len(opened) == 3

    run(scenario)


def crate_of(count, *, title="Fresh crate", source="https://soundcloud.com/a/sets/b"):
    return Crate(
        source=source,
        title=title,
        declared_count=count,
        tracks=[
            Track(
                title=f"Dug {index}",
                permalink_url=f"https://soundcloud.com/a/{index}",
                id=1000 + index,
                purchase_url=f"https://label.bandcamp.com/track/{index}",
            )
            for index in range(count)
        ],
    )


async def settle(app, pilot):
    """Wait for background workers to finish and the UI to catch up."""

    await app.workers.wait_for_complete()
    await pilot.pause()


def test_an_empty_app_asks_for_a_link(state):
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, AskLinkScreen)

    run(scenario)


def test_entering_a_link_fills_the_table(state, monkeypatch, tmp_path):
    monkeypatch.setattr("dj_digger.services.collection.dig", lambda target, **kwargs: crate_of(3))
    app = make_app([], state, export_path=tmp_path / "out.json")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("#ask-input", Input).value = "https://soundcloud.com/a/sets/b"
            await pilot.press("enter")
            await settle(app, pilot)

            assert app.query_one("#tracks", DataTable).row_count == 3
            assert app.playlist_state.present == ["bandcamp"]
            assert app.sub_title == "Fresh crate"

    run(scenario)

    # A dig started from inside the browser still writes the export.
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["bandcamp"]


def test_the_target_is_passed_through_with_the_dig_options(state, monkeypatch):
    seen = {}

    def fake_dig(target, **kwargs):
        seen["target"] = target
        seen["kwargs"] = kwargs
        return crate_of(1)

    monkeypatch.setattr("dj_digger.services.collection.dig", fake_dig)
    app = make_app([], state, dig_options=DigOptions(limit=7, timeout=5.0, delay=0.0))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("#ask-input", Input).value = "playlist.html"
            await pilot.press("enter")
            await settle(app, pilot)

    run(scenario)
    assert seen["target"] == "playlist.html"
    assert seen["kwargs"]["limit"] == 7
    assert seen["kwargs"]["timeout"] == 5.0


def test_cancelling_with_nothing_loaded_quits(state, monkeypatch):
    app = make_app([], state)
    exited = []
    monkeypatch.setattr(app.digging, "exit_empty", lambda: exited.append(True))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

    run(scenario)
    assert exited == [True]


def test_cancelling_keeps_a_crate_you_already_have(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AskLinkScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#tracks", DataTable).row_count == len(records)

    run(scenario)


def test_digging_a_second_link_replaces_the_crate(records, state, monkeypatch):
    monkeypatch.setattr("dj_digger.services.collection.dig", lambda target, **kwargs: crate_of(2, title="Second"))
    app = make_app(records, state, export_format="none")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            app.screen.query_one("#ask-input", Input).value = "https://soundcloud.com/a/sets/c"
            await pilot.press("enter")
            await settle(app, pilot)

            assert app.query_one("#tracks", DataTable).row_count == 2
            assert app.sub_title == "Second"

    run(scenario)


def test_a_failed_dig_reports_and_asks_again(state, monkeypatch):
    def boom(target, **kwargs):
        raise TargetNotFound("nope.html")

    monkeypatch.setattr("dj_digger.services.collection.dig", boom)
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("#ask-input", Input).value = "nope.html"
            await pilot.press("enter")
            await settle(app, pilot)

            # Back at the prompt rather than dead or stuck on a spinner.
            assert isinstance(app.screen, AskLinkScreen)
            assert app.job is None

    run(scenario)


def test_a_crate_with_no_tracks_is_treated_as_a_failure(state, monkeypatch):
    monkeypatch.setattr("dj_digger.services.collection.dig", lambda target, **kwargs: crate_of(0))
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("#ask-input", Input).value = "https://soundcloud.com/a/sets/empty"
            await pilot.press("enter")
            await settle(app, pilot)
            assert isinstance(app.screen, AskLinkScreen)

    run(scenario)


def saved_crate(count=3, *, source="https://soundcloud.com/a/sets/saved", title="Saved crate"):
    record = crate_models.CrateRecord.from_crate(
        Crate(
            source=source,
            title=title,
            tracks=[
                Track(
                    title=f"Kept {index}",
                    permalink_url=f"https://soundcloud.com/a/k{index}",
                    id=500 + index,
                    purchase_url=f"https://label.bandcamp.com/track/k{index}",
                )
                for index in range(count)
            ],
        )
    )
    library.save(record)
    return record


def test_the_sidebar_lists_saved_crates(state):
    saved_crate(title="Alpha")
    saved_crate(source="https://soundcloud.com/a/sets/two", title="Beta")
    app = make_app([], state)

    async def scenario():
        async with app.run_test():
            assert [record.title for record in app.sidebar_state.crates] == ["Alpha", "Beta"]
            assert app.query_one("#crates", ListView).children

    run(scenario)


def test_a_library_is_opened_instead_of_being_asked_for_a_link(state):
    """Someone with saved crates wants to see them, not be interrogated."""

    saved_crate(3, title="Already here")
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, AskLinkScreen)
            assert app.playlist_state.crate is not None and app.playlist_state.crate.title == "Already here"
            assert app.query_one("#tracks", DataTable).row_count == 3

    run(scenario)


def test_selecting_a_crate_switches_to_it(state):
    saved_crate(2, source="https://soundcloud.com/a/sets/one", title="One")
    saved_crate(4, source="https://soundcloud.com/a/sets/two", title="Two")
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            listing = app.query_one("#crates", ListView)
            listing.index = 1
            listing.action_select_cursor()
            await pilot.pause()
            await settle(app, pilot)

            assert app.playlist_state.crate.title == "Two"
            assert app.query_one("#tracks", DataTable).row_count == 4

    run(scenario)


def test_selecting_a_crate_loads_it_on_demand(state, monkeypatch):
    """The sidebar holds headers; the tracks are read when a crate is chosen."""

    saved_crate(2, source="https://soundcloud.com/a/sets/one", title="One")
    saved_crate(4, source="https://soundcloud.com/a/sets/two", title="Two")
    loads: list[str] = []
    real_load = library.load
    monkeypatch.setattr(
        "dj_digger.services.library.LibraryService.load",
        lambda self, source: (loads.append(source), real_load(source))[1],
    )
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            assert not hasattr(app.sidebar_state.crates[0], "tracks")
            listing = app.query_one("#crates", ListView)
            listing.index = 1
            listing.action_select_cursor()
            await pilot.pause()  # Dispatch Selected before waiting for its worker.
            await settle(app, pilot)

            assert app.playlist_state.crate.title == "Two"
            assert loads[-1] == "https://soundcloud.com/a/sets/two"

    run(scenario)


def test_refreshing_redigs_the_saved_source_and_keeps_deletions(state, monkeypatch):
    record = saved_crate(3)
    record.remove("501")
    library.save(record)

    # Like the real dig, which reports back the source it was given.
    monkeypatch.setattr(
        "dj_digger.services.collection.dig",
        lambda target, **kwargs: crate_of(4, title="Refreshed", source=target),
    )
    app = make_app([], state, export_format="none")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await settle(app, pilot)

    run(scenario)

    reloaded = library.load(record.source)
    assert reloaded.refreshed_at
    assert len(reloaded.tracks) == 4
    assert reloaded.removed_track_keys == ["501"]


def test_deleting_a_crate_asks_first(state):
    record = saved_crate(2)
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("X")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause()

    run(scenario)
    assert library.load(record.source).title == "Saved crate"


def test_confirming_deletes_the_crate(state):
    saved_crate(2)
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("X")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

    run(scenario)
    assert library.list_crates() == []


def test_the_sidebar_collapses(records, state):
    app = make_app(records, state)

    async def scenario():
        # Wide, or the narrow-terminal rule below would have collapsed it first.
        async with app.run_test(size=(140, 42)) as pilot:
            sidebar = app.query_one("#sidebar")
            assert not sidebar.has_class("collapsed")
            await pilot.press("ctrl+b")
            assert sidebar.has_class("collapsed")
            await pilot.press("ctrl+b")
            assert not sidebar.has_class("collapsed")

    run(scenario)


def test_a_narrow_terminal_collapses_the_sidebar_by_itself(records, state):
    """28 of 80 columns on crate names costs the title and the right-hand columns."""

    app = make_app(records, state)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            sidebar = app.query_one("#sidebar")
            assert sidebar.has_class("collapsed")
            await pilot.resize_terminal(140, 42)
            await pilot.pause()
            assert not sidebar.has_class("collapsed")

    run(scenario)


def test_the_add_button_digs(records, state, monkeypatch):
    app = make_app(records, state)
    called = []
    monkeypatch.setattr(app, "action_dig_link", lambda: called.append("dig"))

    async def scenario():
        async with app.run_test() as pilot:
            app.query_one("#crate-add", Button).press()
            await pilot.pause()

    run(scenario)
    assert called == ["dig"]


def test_the_add_button_sits_under_the_last_crate(state):
    """Not pinned to the bottom of the sidebar - it belongs with the list."""

    saved_crate(1, source="https://soundcloud.com/a/sets/one", title="One")
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            listing = app.query_one("#crates", ListView)
            add = app.query_one("#crate-add", Button)
            assert add.region.y == listing.region.y + listing.region.height

    run(scenario)


@pytest.mark.parametrize("intent,expected", [("refresh", "refresh_crate"), ("delete", "confirm_delete_crate")])
def test_each_crate_row_carries_its_own_buttons(state, monkeypatch, intent, expected):
    """Icons act on the crate in that row, not on whatever is highlighted."""

    saved_crate(1, source="https://soundcloud.com/a/sets/one", title="One")
    target = saved_crate(1, source="https://soundcloud.com/a/sets/two", title="Two")
    app = make_app([], state)
    called = []
    monkeypatch.setattr(app.crate_controller, expected, lambda record: called.append(record.title))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            items = list(app.query(CrateItem))
            assert len(items) == 2
            # Icons only exist on the row you are pointing at.
            app.query_one("#crates", ListView).index = 1
            await pilot.pause()
            button = next(
                child
                for child in items[1].children
                if isinstance(child, CrateButton) and child.intent == intent
            )
            button.press()
            await pilot.pause()

    run(scenario)
    assert called == [target.title]


def test_crate_icons_keep_out_of_the_way_of_the_name(state):
    """Six columns of icons on every row is six columns the names need more."""

    saved_crate(1, source="https://soundcloud.com/a/sets/one", title="One")
    saved_crate(1, source="https://soundcloud.com/a/sets/two", title="Two")
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#crates", ListView).index = 0
            await pilot.pause()
            items = list(app.query(CrateItem))
            shown = [
                [child.display for child in item.children if isinstance(child, CrateButton)]
                for item in items
            ]
            assert shown == [[True, True], [False, False]]

    run(scenario)


def test_a_long_crate_name_is_trimmed_rather_than_wrapped_away(state):
    """height: 1 means a wrapped name loses its second half entirely."""

    saved_crate(1, source="https://soundcloud.com/a/sets/x", title="Hard Techno Ressurection")
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            label = app.query_one(".crate-name", Label)
            assert label.content.no_wrap is True
            assert label.content.overflow == "ellipsis"
            # The full name is still reachable, just not all at once.
            assert label.tooltip == "Hard Techno Ressurection"

    run(scenario)


def test_a_dig_lands_in_the_library(state, monkeypatch):
    monkeypatch.setattr("dj_digger.services.collection.dig", lambda target, **kwargs: crate_of(2, title="Dug"))
    app = make_app([], state, export_format="none")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("#ask-input", Input).value = "https://soundcloud.com/a/sets/new"
            await pilot.press("enter")
            await settle(app, pilot)

    run(scenario)

    crates = library.list_crates()
    assert [record.title for record in crates] == ["Dug"]
    assert len(crates[0].tracks) == 2


def test_removing_a_track_persists_and_undo_brings_it_back(state):
    record = saved_crate(3)
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            assert app.query_one("#tracks", DataTable).row_count == 3

            await pilot.press("x")
            await settle(app, pilot)
            assert app.query_one("#tracks", DataTable).row_count == 2
            assert library.load(record.source).removed_track_keys == ["500"]

            await pilot.press("ctrl+z")
            await settle(app, pilot)
            assert app.query_one("#tracks", DataTable).row_count == 3
            assert library.load(record.source).removed_track_keys == []

    run(scenario)


def test_removing_keeps_the_active_filter(state):
    saved_crate(3)
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("1")  # bandcamp, the only store here
            assert app.playlist_state.store_filters == {"bandcamp"}
            await pilot.press("x")
            await settle(app, pilot)
            # A removal must not reset what you filtered down to.
            assert app.playlist_state.store_filters == {"bandcamp"}
            assert app.query_one("#tracks", DataTable).row_count == 2

    run(scenario)


def test_removing_without_a_saved_crate_is_refused_not_a_crash(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            assert app.playlist_state.crate is None
            await pilot.press("x")
            await settle(app, pilot)
            assert app.query_one("#tracks", DataTable).row_count == len(records)

    run(scenario)


def test_undo_with_nothing_removed_is_harmless(state):
    saved_crate(2)
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+z")
            await settle(app, pilot)
            assert app.query_one("#tracks", DataTable).row_count == 2

    run(scenario)


def test_the_genre_column_shows_genre_then_tag_then_nothing(state):
    tracks = [
        Track(title="A", permalink_url="u/a", id=1, genre="Techno", purchase_url="https://l.bandcamp.com/a"),
        Track(title="B", permalink_url="u/b", id=2, tags=["Acid"], purchase_url="https://l.bandcamp.com/b"),
        Track(title="C", permalink_url="u/c", id=3, purchase_url="https://l.bandcamp.com/c"),
    ]
    app = make_app(links.categorise_all(tracks), state)

    async def scenario():
        async with app.run_test():
            table = app.query_one("#tracks", DataTable)
            genres = [str(table.get_row_at(index)[GENRE_CELL]) for index in range(3)]
            assert genres == ["Techno", "Acid", "-"]

    run(scenario)


def test_the_time_column_reads_as_minutes_and_seconds(state):
    tracks = [
        Track(title="A", permalink_url="u/a", id=1, duration=254_000),
        Track(title="B", permalink_url="u/b", id=2),
    ]
    app = make_app(links.categorise_all(tracks), state)

    async def scenario():
        async with app.run_test():
            table = app.query_one("#tracks", DataTable)
            assert [str(table.get_row_at(index)[TIME_CELL]) for index in range(2)] == ["4:14", "-"]

    run(scenario)


def test_the_store_column_badges_every_store_and_picks_out_the_one_o_opens(state):
    track = Track(
        title="Everywhere",
        permalink_url="https://soundcloud.com/a/b",
        id=5,
        purchase_url="https://hypeddit.com/x/y",
        description="also at https://label.bandcamp.com/album/x",
    )
    app = make_app(links.categorise_all([track]), state)

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#tracks", DataTable)
            # One character over the column, so it arrives elided rather than
            # clipped by the table into "gate(hypedd".
            assert str(table.get_row_at(0)[STORES_CELL]) == "bandcamp gate(hypeddi…"
            # Bandcamp comes first, so that is what o would follow.
            assert app.filter_controller.record_to_open(app.playlist_state.rows[0]).category == "bandcamp"

            # Filtering to the gate is how you say you want the gate instead.
            await pilot.press("2")
            assert app.playlist_state.store_filters == {"gate"}
            assert app.filter_controller.record_to_open(app.playlist_state.rows[0]).category == "gate"

    run(scenario)


def test_a_free_soundcloud_download_is_badged_and_opened_first(state):
    track = Track(
        title="Handed out",
        permalink_url="https://soundcloud.com/a/b",
        id=6,
        downloadable=True,
        has_downloads_left=True,
        download_url="https://api-v2.soundcloud.com/tracks/6/download",
        purchase_url="https://label.bandcamp.com/album/x",
    )
    app = make_app(links.categorise_all([track]), state)

    async def scenario():
        async with app.run_test():
            table = app.query_one("#tracks", DataTable)
            assert str(table.get_row_at(0)[STORES_CELL]) == "\u2193soundcloud bandcamp"
            chosen = app.filter_controller.record_to_open(app.playlist_state.rows[0])
            assert chosen.category == "soundcloud"
            assert chosen.link_url == track.download_url

    run(scenario)


def test_shops_and_others_are_badged_with_their_domain(state):
    """"others" as a word says nothing; the domain is the only identification."""

    tracks = [
        Track(title="A", permalink_url="u/a", id=1, purchase_url="https://www.nofu.de/redirect/?r=X"),
        Track(title="B", permalink_url="u/b", id=2, purchase_url="https://boomkat.com/products/x"),
    ]
    app = make_app(links.categorise_all(tracks), state)

    async def scenario():
        async with app.run_test():
            table = app.query_one("#tracks", DataTable)
            badges = [str(table.get_row_at(index)[STORES_CELL]) for index in range(2)]
            assert badges == ["nofu.de", "boomkat.com"]

    run(scenario)


def _many_store_records():
    records = []
    for index, category in enumerate(("bandcamp", "beatport", "gate", "others", "traxsource", "juno")):
        for n in range(3):
            records.append(
                LinkRecord(
                    category=category,
                    track=Track(
                        title=f"{category} {n}",
                        permalink_url=f"https://soundcloud.com/a/{index * 10 + n}",
                        id=index * 10 + n + 1,
                    ),
                    link_url=f"https://example.com/{category}/{n}",
                    link_text="Buy",
                )
            )
    return records


def test_the_legend_lists_every_store_and_scrolls_instead_of_clipping(state):
    """Like the footer: the whole legend is there, and the bar scrolls sideways."""

    app = make_app(_many_store_records(), state)

    async def scenario():
        async with app.run_test(size=(40, 24)) as pilot:
            await pilot.pause()
            line = app.table_controller._store_line()
            assert all(store in str(line) for store in app.playlist_state.present)
            assert "\u00b73" in str(line)
            assert [idx for _s, _e, idx in app.playlist_state._badge_click_regions] == list(range(len(app.playlist_state.present) + 1))
            bar = app.query_one("#status")
            assert bar.max_scroll_x > 0, "wider than the terminal, so it can scroll"
            assert bar.styles.scrollbar_size_horizontal == 0
            legend = app.query_one("#status-legend")
            # A plain wheel over the bar, no shift: Textual would take that as
            # a vertical scroll and do nothing on a one-line bar.
            x, y = legend.region.x + 3, legend.region.y
            legend.post_message(events.MouseScrollDown(legend, 3, 0, 0, 0, 0, False, False, False, x, y))
            await pilot.pause()
            await pilot.pause()
            assert bar.scroll_x > 0, "the wheel scrolls it sideways"
            legend.post_message(events.MouseScrollUp(legend, 3, 0, 0, 0, 0, False, False, False, x, y))
            await pilot.pause()
            await pilot.pause()
            assert bar.scroll_x == 0

    run(scenario)


def test_the_title_column_takes_the_width_left_over(state):
    """A fixed title column left half the terminal empty and cut the titles."""

    app = make_app(synthetic_records(3), state)

    async def scenario():
        async with app.run_test(size=(160, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", TrackTable)
            spent = sum(column.get_render_width(table) for column in table.columns.values())
            assert spent == table.size.width
            assert table.columns[table.flexible_column].width > keymap.MIN_TITLE_WIDTH

    run(scenario)


def test_an_80_column_terminal_needs_no_horizontal_scrollbar(state):
    """Genre and Time used to sit off the right edge behind a scrollbar."""

    app = make_app(synthetic_records(60), state)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", TrackTable)
            # Enough rows for a vertical scrollbar, whose two columns the title
            # has to leave alone.
            assert table.show_vertical_scrollbar
            assert not table.show_horizontal_scrollbar

    run(scenario)


def test_the_footer_drops_keys_rather_than_cutting_one_in_half(state):
    """Thirteen bindings want 161 columns; the row is as wide as the terminal."""

    app = make_app(synthetic_records(3), state)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            # The footer builds its keys on mount and rebuilds them whenever
            # focus moves, so one pause is not a guarantee that they exist yet -
            # on a slow runner this read an empty set and asserted nothing.
            for _ in range(3):
                await pilot.pause()
            keys = [key for key in app.query("FooterKey") if key.display]
            assert keys, "the footer never composed"

            spent = sum(len(k.key_display) + len(k.description) + 3 for k in keys)
            assert spent <= 80
            # The ones you cannot do without survive the cut.
            shown = {key.action for key in keys}
            assert {"open_link", "play_pause", "help", "quit"} <= shown

    run(scenario)


def test_folding_the_sidebar_gives_the_title_more_room(state):
    app = make_app(synthetic_records(3), state)

    async def scenario():
        async with app.run_test(size=(160, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", TrackTable)
            before = table.columns[table.flexible_column].width
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert table.columns[table.flexible_column].width > before

    run(scenario)


def a_stream(duration=300.0):
    return Stream(url="https://cdn/x.mp3", waveform_url="https://wave/x.json", duration=duration)


class FakePlayer:
    """The slice of Player the TUI leans on, without touching a sound card."""

    def __init__(self):
        self.loaded = None
        self.playing = False
        self.position = 0.0
        self.duration = 300.0
        self.fraction = 0.0
        self.volume = 0.8
        self.seeks = []
        self.closed = False
        self.muted = False
        self.finished = False
        self.level = 0.0

    def take_level(self):
        return self.level

    def take_finished(self):
        finished, self.finished = self.finished, False
        return finished

    def take_event(self):
        if not self.finished:
            return None
        self.finished = False
        return SimpleNamespace(kind="finished", message="")

    def load(self, track, stream, session, waveform=None, source=None):
        self.loaded = SimpleNamespace(
            track=track, stream=stream, duration=self.duration, waveform=waveform or []
        )
        self.source = source
        return self.loaded

    def play(self):
        self.playing = True

    def pause(self):
        self.playing = False

    def toggle(self):
        self.playing = not self.playing

    def stop(self):
        self.playing = False

    def unload(self):
        self.stop()
        self.loaded = None

    def seek(self, seconds):
        self.seeks.append(seconds)
        self.position = seconds

    def nudge(self, seconds):
        self.seek(self.position + seconds)

    def set_volume(self, volume):
        self.volume = max(0.0, min(1.0, volume))

    def change_volume(self, delta):
        self.set_volume(self.volume + delta)

    def toggle_mute(self):
        self.muted = not self.muted

    def close(self):
        self.closed = True


def two_tracks_three_links():
    """One track sold in two shops, plus a plain one: three links, two rows."""

    both = Track(
        title="Sold twice",
        permalink_url="https://soundcloud.com/a/both",
        id=901,
        purchase_url="https://label.bandcamp.com/track/x",
        description="also https://www.beatport.com/track/x/1",
    )
    single = Track(
        title="Sold once",
        permalink_url="https://soundcloud.com/a/single",
        id=902,
        purchase_url="https://label.bandcamp.com/track/y",
    )
    return links.categorise_all([both, single])


def player_app(records, state, **kwargs):
    app = make_app(records, state, **kwargs)
    app.player = FakePlayer()
    return app


def loading_fetch(app, started):
    """Stand in for the audio worker, doing what _audio_ready would do."""

    def fetch(track, generation=None):
        started.append(track.id)
        app.playback_controller._player_bar().message = ""
        app.player.load(track, a_stream(), None)
        app.player.play()
        app.table_controller.refresh_rows()
        app.playback_controller._focus_playing_track()
        app.playback_controller._player_bar().refresh_bar()

    return fetch


def test_three_links_over_two_tracks_make_two_rows():
    records = two_tracks_three_links()
    assert len(records) == 3

    app = DiggerApp(records, state=None)

    async def scenario():
        async with app.run_test():
            assert [row.track.id for row in app.playlist_state.rows] == [901, 902]

    run(scenario)


def test_playback_start_repaints_the_two_rows_involved(state, monkeypatch):
    app = player_app(two_tracks_three_links(), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation=None: app.playback_controller._audio_ready(track, a_stream(), []))
    clears = []
    painted = []
    real_paint = app.table_controller._paint_key

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            monkeypatch.setattr(DataTable, "clear", lambda self, *a, **k: clears.append(1))
            monkeypatch.setattr(app.table_controller, "_paint_key", lambda key: (painted.append(key), real_paint(key)))
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()

    run(scenario)
    assert clears == [], "starting playback must not rebuild the table"
    assert painted == ["901", "901", "902"], "the old row loses the marker, the new one gains it"


def test_next_track_moves_on_to_the_next_track(state, monkeypatch):
    app = player_app(two_tracks_three_links(), state)
    started = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation=None: started.append(track.id))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()

    run(scenario)
    assert started == [901, 902]


def test_previous_track_walks_back(state, monkeypatch):
    app = player_app(two_tracks_three_links(), state)
    started = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation=None: started.append(track.id))

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#tracks", DataTable)
            table.move_cursor(row=1)  # the second track
            await pilot.press("p")
            await pilot.pause()

    run(scenario)
    assert started == [901]


def test_a_finished_track_rolls_on_to_the_next(state, monkeypatch):
    """Auditioning a crate should not need a keypress between every track."""

    app = player_app(synthetic_records(4), state)
    started = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, started))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            app.player.finished = True
            app.playback_controller._tick()
            await pilot.pause()
            assert app.query_one("#tracks", DataTable).cursor_row == 1

    run(scenario)
    assert started == [1, 2]


def test_the_end_of_the_list_stops_instead_of_wrapping(state, monkeypatch):
    app = player_app(synthetic_records(2), state)
    started = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, started))

    async def scenario():
        async with app.run_test() as pilot:
            app.query_one("#tracks", DataTable).move_cursor(row=1)
            await pilot.press("space")
            await pilot.pause()
            app.player.finished = True
            app.playback_controller._tick()
            await pilot.pause()

    run(scenario)
    assert started == [2]


def test_wandering_off_leaves_the_cursor_where_you_put_it(state, monkeypatch):
    """Browsing ahead while something plays must survive the auto-advance."""

    app = player_app(synthetic_records(4), state)
    started = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, started))

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#tracks", DataTable)
            await pilot.press("space")
            await pilot.pause()
            table.move_cursor(row=3)
            await pilot.pause()

            app.player.finished = True
            app.playback_controller._tick()
            await pilot.pause()
            assert table.cursor_row == 3

    run(scenario)
    # Playback moved on all the same.
    assert started == [1, 2]


def test_the_playing_row_carries_a_marker(state, monkeypatch):
    app = player_app(synthetic_records(3), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#tracks", DataTable)
            await pilot.press("space")
            await pilot.pause()
            markers = [str(table.get_row_at(index)[0]) for index in range(3)]
            assert markers == [" " + keymap.PLAYING_GLYPH, "  ", "  "]

    run(scenario)


def test_marking_the_track_you_are_hearing_moves_listening_on_too(state, monkeypatch):
    """Ruling on a track mid-triage should not leave it playing to the end."""

    app = player_app(synthetic_records(4), state)
    started = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, started))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("k")
            await settle(app, pilot)
            assert app.query_one("#tracks", DataTable).cursor_row == 1

    run(scenario)
    assert started == [1, 2]


def test_marking_a_track_you_are_not_hearing_leaves_playback_alone(state, monkeypatch):
    app = player_app(synthetic_records(4), state)
    started = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, started))

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#tracks", DataTable)
            await pilot.press("space")
            await pilot.pause()
            table.move_cursor(row=2)
            await pilot.press("k")
            await settle(app, pilot)

    run(scenario)
    assert started == [1]


# Getting the next track ready


class FakeSource:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def prepared_for(app, index, source=None):
    track = app.playlist_state.visible_rows[index].track
    return Prepared(track=track, stream=a_stream(), waveform=[1, 2], source=source)


def test_the_next_track_is_got_ready_before_this_one_ends(state, monkeypatch):
    """Otherwise every track in the crate is followed by a second of Loading."""

    app = player_app(synthetic_records(3), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))
    asked = []
    monkeypatch.setattr(app.playback_controller, "prepare_track", lambda track, generation=None: asked.append(track.id))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()

            app.player.position = 290.0  # ten seconds left of three hundred
            app.playback_controller._tick()
            await pilot.pause()

    run(scenario)
    assert asked == [2]


def test_nothing_is_got_ready_while_there_is_time_left(state, monkeypatch):
    app = player_app(synthetic_records(3), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))
    asked = []
    monkeypatch.setattr(app.playback_controller, "prepare_track", lambda track, generation=None: asked.append(track.id))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            app.player.position = 100.0
            app.playback_controller._tick()
            await pilot.pause()

    run(scenario)
    assert asked == []


def test_the_last_track_has_nothing_to_get_ready(state, monkeypatch):
    app = player_app(synthetic_records(1), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))
    asked = []
    monkeypatch.setattr(app.playback_controller, "prepare_track", lambda track, generation=None: asked.append(track.id))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            app.player.position = 295.0
            app.playback_controller._tick()
            await pilot.pause()

    run(scenario)
    assert asked == []


def test_a_prepared_track_plays_without_asking_for_it_again(state, monkeypatch):
    app = player_app(synthetic_records(3), state)
    started = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation=None: started.append(track.id))

    async def scenario():
        async with app.run_test() as pilot:
            source = FakeSource()
            app.audio_state._prepared = prepared_for(app, 1, source)
            app.query_one("#tracks", DataTable).move_cursor(row=1)
            await pilot.press("space")
            await pilot.pause()

            assert started == [], "the worker was asked for what was already here"
            assert app.player.playing is True
            assert app.player.source is source

    run(scenario)


def test_using_the_prepared_track_leaves_nothing_behind(state, monkeypatch):
    app = player_app(synthetic_records(3), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation=None: None)

    async def scenario():
        async with app.run_test() as pilot:
            app.audio_state._prepared = prepared_for(app, 1)
            app.query_one("#tracks", DataTable).move_cursor(row=1)
            await pilot.press("space")
            await pilot.pause()
            assert app.audio_state._prepared is None

    run(scenario)


def test_a_filter_that_changes_what_comes_next_throws_it_away(state, monkeypatch):
    records = synthetic_records(2) + synthetic_records(1, category="beatport")
    app = player_app(records, state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")  # the first track
            await pilot.pause()
            source = FakeSource()
            app.audio_state._prepared = prepared_for(app, 1, source)

            await pilot.press("2")  # only the beatport track survives
            await pilot.pause()

            assert app.audio_state._prepared is None
            assert source.closed is True

    run(scenario)


def test_a_preparation_that_arrives_too_late_is_thrown_away(state):
    app = player_app(synthetic_records(3), state)

    async def scenario():
        async with app.run_test():
            source = FakeSource()
            app.audio_state._preparing = "someone else"
            app.playback_controller._preparation_done("2", prepared_for(app, 1, source))

            assert app.audio_state._prepared is None
            assert source.closed is True

    run(scenario)


def test_leaving_the_app_lets_go_of_the_prepared_track(state):
    app = player_app(synthetic_records(3), state)
    source = FakeSource()

    async def scenario():
        async with app.run_test():
            app.audio_state._prepared = prepared_for(app, 1, source)

    run(scenario)
    assert source.closed is True


# Showing that a keypress landed


def styles_on(table, index):
    return {span.style for cell in table.get_row_at(index) for span in cell.spans}


def test_marking_a_track_lights_the_row_then_lets_it_settle(state):
    app = make_app(synthetic_records(3), state)

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#tracks", TrackTable)
            await pilot.press("g")
            await settle(app, pilot)
            lit = app.role(keymap.STATUS_STYLES[GOT][1])
            row_cells = table.get_row_at(0)
            if len(row_cells) > TITLE_CELL and row_cells[TITLE_CELL].spans:
                assert str(row_cells[TITLE_CELL].spans[-1].style) == lit

            await pilot.pause(keymap.FLASH + 0.1)
            assert lit not in styles_on(table, 0)
            assert str(table.get_row_at(0)[MARK_CELL]) == "\u2713"

    run(scenario)


def test_marking_a_track_does_not_redraw_the_whole_table(state, monkeypatch):
    """Rebuilding every row to change one glyph is the flicker you could see."""

    app = make_app(synthetic_records(4), state)

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#tracks", TrackTable)
            rebuilds = []
            monkeypatch.setattr(table, "clear", lambda *a, **k: rebuilds.append(1))

            await pilot.press("g")
            await settle(app, pilot)

            assert rebuilds == []
            assert str(table.get_row_at(0)[MARK_CELL]) == "\u2713"

    run(scenario)


def test_download_progress_does_not_move_the_viewport(state, monkeypatch):
    app = make_app(synthetic_records(60), state)

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            table = app.query_one("#tracks", TrackTable)
            table.move_cursor(row=30)
            await scroll_table(pilot, table, 20)
            cursor = table.cursor_row
            viewport = table.scroll_offset
            rebuilds = []
            monkeypatch.setattr(table, "clear", lambda *a, **k: rebuilds.append(1))

            row = app.playlist_state.visible_rows[35]
            app.download_controller._update_track_progress(row.track.key, 0.42)
            await pilot.pause()

            assert rebuilds == []
            assert table.cursor_row == cursor
            assert table.scroll_offset == viewport
            assert "[42%]" in str(table.get_row_at(35)[TITLE_CELL])

    run(scenario)


def test_batch_progress_repaints_every_row_waiting_for_the_throttle(state):
    app = make_app(synthetic_records(4), state)

    async def scenario():
        async with app.run_test():
            first, second = app.playlist_state.visible_rows[:2]
            app.download_state._last_progress_redraw = float("inf")
            app.download_controller._update_track_progress(first.track.key, 0.21)
            app.download_controller._update_track_progress(second.track.key, 0.37)

            app.download_state._last_progress_redraw = 0
            app.download_controller._update_track_progress(first.track.key, 0.42)

            table = app.query_one("#tracks", TrackTable)
            assert "[42%]" in str(table.get_row_at(0)[TITLE_CELL])
            assert "[37%]" in str(table.get_row_at(1)[TITLE_CELL])

    run(scenario)


class ProgressProbeClient:
    def __init__(self, app, seen, output):
        self.app = app
        self.seen = seen
        self.output = output

    def download_track(self, track, directory, **kwargs):
        self.seen.append(self.app.download_state.download_progress[track.key])
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(b"audio")
        return self.output

    def close(self):
        pass


class ProfileRetryClient:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def download_track(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise gate_models.GateProfileRequired("real email required")
        return self.output

    def close(self):
        pass


def test_single_download_uses_a_playlist_named_subdirectory(state, tmp_path):
    track = Track(
        id=80,
        title="Download",
        permalink_url="https://soundcloud.com/a/download",
        downloadable=True,
        has_downloads_left=True,
        download_url="https://api-v2.soundcloud.com/tracks/80/download",
    )
    app = make_app(links.categorise(track), state)
    app.playlist_state.crate_title = "Warehouse / Session: 01"
    app.config.download_directory = str(tmp_path / "Downloads")
    directories = []

    class Client:
        def download_track(self, _track, directory, **_kwargs):
            directories.append(directory)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "track.wav"
            path.write_bytes(b"audio")
            return path

        def close(self):
            pass

    app._client = Client()

    async def scenario():
        async with app.run_test():
            worker = app.download_controller.download_track_in_background(track)
            await worker.wait()

    run(scenario)
    assert directories == [tmp_path / "Downloads" / "Warehouse Session 01"]
    assert state.get(track.key) == GOT


@pytest.mark.parametrize("save_profile", [False, True])
def test_gate_profile_wizard_retries_a_single_download_at_most_once(
    state, tmp_path, save_profile
):
    record = LinkRecord(
        category="gate",
        track=Track(
            title="Gate", artist="Artist",
            permalink_url="https://soundcloud.com/a/gate", id=81,
        ),
        link_url="https://hypeddit.com/a/gate",
        link_text="Download",
    )
    app = make_app([record], state)
    client = ProfileRetryClient(tmp_path / "gate.mp3")
    app._client = client

    async def scenario():
        async with app.run_test() as pilot:
            row = app.playlist_state.visible_rows[0]
            worker = app.download_controller.download_track_in_background(row.track, record.link_url)
            for _ in range(50):
                await pilot.pause()
                if isinstance(app.screen, GateProfileScreen):
                    break
            assert app.job is not None, "The operation owns its dialog"
            assert not worker.is_finished
            await pilot.pause()
            assert isinstance(app.screen, GateProfileScreen)
            if save_profile:
                app.screen.query_one("#gate-profile-name", Input).value = "Filip"
                app.screen.query_one("#gate-profile-email", Input).value = "filip@example.com"
                await pilot.click("#gate-profile-save")
                await asyncio.wait_for(worker.wait(), timeout=30)
                await pilot.pause()
            else:
                await pilot.click("#gate-profile-cancel")
                await asyncio.wait_for(worker.wait(), timeout=30)
                await pilot.pause()

    run(scenario)
    assert client.calls == (2 if save_profile else 1)
    assert (state.get(record.track.key) == GOT) is save_profile


def test_a_repeated_prerequisite_error_does_not_open_a_wizard_loop(state, tmp_path):
    record = LinkRecord(
        category="gate",
        track=Track(
            title="Gate", permalink_url="https://soundcloud.com/a/gate-loop", id=82
        ),
        link_url="https://hypeddit.com/a/gate-loop",
        link_text="Download",
    )
    app = make_app([record], state)

    class Client:
        calls = 0

        def download_track(self, *_args, **_kwargs):
            self.calls += 1
            raise gate_models.GateProfileRequired("still missing")

        def close(self):
            pass

    client = Client()
    app._client = client

    async def scenario():
        async with app.run_test() as pilot:
            worker = app.download_controller.download_track_in_background(
                app.playlist_state.visible_rows[0].track, record.link_url
            )
            for _ in range(50):
                await pilot.pause()
                if isinstance(app.screen, GateProfileScreen):
                    break
            assert app.job is not None, "The operation owns its dialog"
            assert not worker.is_finished
            await pilot.pause()
            app.screen.query_one("#gate-profile-name", Input).value = "Filip"
            app.screen.query_one("#gate-profile-email", Input).value = "filip@example.com"
            await pilot.click("#gate-profile-save")
            for _ in range(25):
                await pilot.pause()
                if client.calls == 2:
                    break
            await pilot.pause()
            assert not isinstance(app.screen, GateProfileScreen)

    run(scenario)
    assert client.calls == 2
    assert state.get(record.track.key) != GOT


def test_batch_finishes_independent_tracks_before_prompting_and_retries_only_pending(
    state, tmp_path, monkeypatch
):
    records = [
        LinkRecord(
            category="gate",
            track=Track(
                title="Needs email", artist="Artist",
                permalink_url="https://soundcloud.com/a/1", id=91,
            ),
            link_url="https://hypeddit.com/a/1",
            link_text="Download",
        ),
        LinkRecord(
            category="soundcloud",
            track=Track(
                title="Ready", artist="Artist",
                permalink_url="https://soundcloud.com/a/2", id=92,
                downloadable=True, has_downloads_left=True,
                download_url="https://api-v2.soundcloud.com/tracks/92/download",
            ),
            link_url="https://api-v2.soundcloud.com/tracks/92/download",
            link_text=links.FREE_DOWNLOAD,
        ),
    ]
    app = make_app(records, state)

    class Client:
        def __init__(self):
            self.calls = {91: 0, 92: 0}

        def download_track(self, track, *_args, **_kwargs):
            self.calls[track.id] += 1
            if track.id == 91 and self.calls[91] == 1:
                raise gate_models.GateProfileRequired("email")
            path = tmp_path / f"{track.id}.mp3"
            path.write_bytes(b"audio")
            return path

        def close(self):
            pass

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    client = Client()
    app._client = client
    monkeypatch.setattr("dj_digger.soundcloud.create_requests_session", Session)

    async def scenario():
        async with app.run_test() as pilot:
            items = [(row, app.opening_controller._find_gate_url(row)) for row in app.playlist_state.visible_rows]
            worker = app.download_controller.batch_download_in_background(items)
            async with asyncio.timeout(5):
                while not isinstance(app.screen, GateProfileScreen) or not app.screen.is_mounted:
                    await pilot.pause(.01)
            assert app.job is not None, "The operation owns its dialog"
            assert not worker.is_finished
            await pilot.pause()
            assert state.get(records[1].track.key) == GOT
            assert state.get(records[0].track.key) != GOT
            assert isinstance(app.screen, GateProfileScreen)

            app.screen.query_one("#gate-profile-name", Input).value = "Filip"
            app.screen.query_one("#gate-profile-email", Input).value = "filip@example.com"
            app.screen.query_one("#gate-profile-save", Button).press()
            async with asyncio.timeout(5):
                await worker.wait()
            assert state.get(records[0].track.key) == GOT

    run(scenario)
    assert client.calls == {91: 2, 92: 1}


def test_browser_required_batch_is_one_call_for_several_tracks(
    state, tmp_path, monkeypatch
):
    records = [
        LinkRecord(
            category="gate",
            track=Track(
                id=index,
                title=f"Gate {index}",
                permalink_url=f"https://soundcloud.com/a/{index}",
            ),
            link_url=f"https://hypeddit.com/track/{index}",
            link_text="Download",
        )
        for index in (201, 202)
    ]
    app = make_app(records, state)

    class Client:
        def download_track(self, *_args, **_kwargs):
            raise gate_models.GateManualActionRequired("browser")

        def close(self):
            pass

    class Session:
        def close(self):
            pass

    calls = []

    def browser_batch(items, _directory, _cancel, **_kwargs):
        calls.append(items)
        completed = []
        for track, _url in items:
            path = tmp_path / f"{track.id}.mp3"
            path.write_bytes(b"audio")
            completed.append((track.key, path))
        return gate_models.HypedditBrowserBatchResult(completed=tuple(completed))

    app._client = Client()
    monkeypatch.setattr(
        "dj_digger.soundcloud.create_requests_session", Session
    )
    monkeypatch.setattr(
        "dj_digger.gates.browser.download_hypeddit_batch_in_browser",
        browser_batch,
    )

    async def scenario():
        async with app.run_test():
            items = [(row, app.opening_controller._find_gate_url(row)) for row in app.playlist_state.visible_rows]
            worker = app.download_controller.batch_download_in_background(items)
            await worker.wait()

    run(scenario)
    assert len(calls) == 1
    assert len(calls[0]) == 2
    assert all(state.get(record.track.key) == GOT for record in records)


def test_batch_starts_downloads_before_every_hypeddit_preflight_finishes(
    state, tmp_path, monkeypatch
):
    records = []
    for index in (205, 206):
        gate_url = f"https://hypeddit.com/track/{index}"
        track = Track(
            id=index,
            title=f"Gate {index}",
            permalink_url=f"https://soundcloud.com/a/{index}",
            extra_links=[(gate_url, "Download")],
        )
        records.append(
            LinkRecord(
                category="gate",
                track=track,
                link_url=gate_url,
                link_text="Download",
            )
        )
    app = make_app(records, state)
    app.playlist_state.crate = crate_models.CrateRecord(
        source="https://soundcloud.com/a/sets/batch",
        title="Batch",
        tracks=[record.track for record in records],
    )
    first_download = Event()
    started = []

    def normalise(self, track, gate_url):
        if track.id == 206:
            assert first_download.wait(2)
        return gate_url, False

    class Client:
        def download_track(self, track, directory, **_kwargs):
            started.append(track.id)
            if track.id == 205:
                first_download.set()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{track.id}.wav"
            path.write_bytes(b"audio")
            return path

        def close(self):
            pass

    class Session:
        def close(self):
            pass

    app._client = Client()
    monkeypatch.setattr("dj_digger.services.downloads.DownloadWorkflow.normalise", normalise)
    monkeypatch.setattr(
        "dj_digger.soundcloud.create_requests_session", Session
    )

    async def scenario():
        async with app.run_test():
            items = [(row, app.opening_controller._find_gate_url(row)) for row in app.playlist_state.visible_rows]
            worker = app.download_controller.batch_download_in_background(items)
            await worker.wait()

    run(scenario)
    assert sorted(started) == [205, 206]


def test_the_job_line_counts_and_names_the_cancel_key(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.start_job("Downloading", 9, cancel=Event())
            app.job_progress(3, failed=1)
            await pilot.pause()
            text = bar_text(app)
            assert "Downloading" in text and "3/9" in text and "1 failed" in text
            assert "^X stop" in text
            app.finish_job()
            await pilot.pause()
            assert "Downloading" not in bar_text(app)

    run(scenario)


def test_a_dig_keeps_the_rows_on_screen(records, state, monkeypatch):
    """Refreshing a big crate used to blank the table for as long as the dig took."""

    entered = Event()
    release = Event()

    def slow_dig(target, cancel=None, **kwargs):
        entered.set()
        release.wait(5)
        return crate_of(1)

    monkeypatch.setattr("dj_digger.services.collection.dig", slow_dig)
    app = make_app(records, state, export_format="none")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_dig("https://soundcloud.com/x/sets/y")
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(20):
                await pilot.pause()
                if "Digging" in bar_text(app):
                    break
            table = app.query_one("#tracks", DataTable)
            assert table.row_count == len(records)
            assert not table.loading
            assert "Digging" in bar_text(app)
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

    run(scenario)


def test_an_unreadable_scan_folder_is_reported_in_the_banner(records, state, monkeypatch):
    class NoisyScanner:
        errors = ["/music/locked: Permission denied"]

        def __init__(self, *_args, **_kwargs):
            pass

        def scan(self, cancel=None):
            return 0

        def match_track(self, _track):
            return None

        def had_stale_match(self, _track):
            return False

    monkeypatch.setattr("dj_digger.services.library.LocalScanner", NoisyScanner)
    app = make_app(records, state)
    errors = []

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.scan_controller.scan_local_files()
            await app.workers.wait_for_complete()
            await pilot.pause()
            errors.extend(app.query_one(ErrorBanner).errors)
            assert app.job is None, "the scan job is finished"

    run(scenario)
    assert any("locked" in error and "unreadable" in error for error in errors)


def test_ctrl_x_stops_a_dig(records, state, monkeypatch):
    entered = Event()

    def slow_dig(target, cancel=None, **kwargs):
        entered.set()
        assert cancel.wait(2), "the UI did not signal the dig"
        raise Cancelled()

    monkeypatch.setattr("dj_digger.services.collection.dig", slow_dig)
    app = make_app(records, state, export_format="none")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_dig("https://soundcloud.com/x/sets/y")
            assert await asyncio.to_thread(entered.wait, 2)
            await pilot.press("ctrl+x")
            await app.workers.wait_for_complete()
            await pilot.pause()

    run(scenario)
    assert app._dig_cancel.is_set()
    assert app.job is None
    assert len(app.playlist_state.rows) == len(records), "a stopped dig leaves the crate as it was"


def test_unmount_signals_every_cancel_event(records, state):
    app = make_app(records, state)

    async def scenario():
        async with app.run_test():
            pass

    run(scenario)
    assert app._dig_cancel.is_set()
    assert app.download_state._gate_cancel.is_set()
    assert app.scan_state._scan_cancel.is_set()
    assert app.cart_state._cart_cancel.is_set()


def _hypeddit_record(track_id=203, title="Refused gate"):
    return LinkRecord(
        category="gate",
        track=Track(id=track_id, title=title, permalink_url=f"https://soundcloud.com/a/{track_id}"),
        link_url=f"https://hypeddit.com/track/{track_id}",
        link_text="Download",
    )


def test_rejected_hypeddit_gate_falls_back_to_the_browser_with_its_reason(
    state, tmp_path, monkeypatch
):
    """A refused unlock is something a person in the browser can get past."""

    record = _hypeddit_record()
    app = make_app([record], state)

    class Client:
        def download_track(self, *_args, **_kwargs):
            raise gate_models.GateRejected("did not unlock")

        def close(self):
            pass

    app._client = Client()
    browser_calls = []
    monkeypatch.setattr(
        "dj_digger.gates.browser.download_hypeddit_in_browser",
        lambda track, url, directory, cancel, **_kwargs: browser_calls.append(url)
        or (_ for _ in ()).throw(gate_models.GateManualActionRequired("closed the tab")),
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()

    run(scenario)
    assert browser_calls == [record.link_url]
    assert state.get(record.track.key) != GOT


def test_a_batch_hands_at_most_eight_refused_gates_to_the_browser(state, monkeypatch):
    records = [_hypeddit_record(300 + n, f"Gate {n}") for n in range(10)]
    app = make_app(records, state)

    class Client:
        def download_track(self, *_args, **_kwargs):
            raise gate_models.GateRejected("did not unlock")

        def close(self):
            pass

    class Session:
        def close(self):
            pass

    handed = []

    def browser_batch(items, _directory, cancel, **_kwargs):
        handed.extend(track.key for track, _url in items)
        return gate_models.HypedditBrowserBatchResult(
            failures=tuple((track.key, gate_models.GateManualActionRequired("skipped")) for track, _ in items)
        )

    app._client = Client()
    monkeypatch.setattr("dj_digger.soundcloud.create_requests_session", Session)
    monkeypatch.setattr(
        "dj_digger.gates.browser.download_hypeddit_batch_in_browser", browser_batch
    )

    errors = []

    async def scenario():
        async with app.run_test() as pilot:
            worker = app.download_controller.batch_download_in_background(
                [(row, row.records[0].link_url) for row in app.playlist_state.visible_rows]
            )
            await worker.wait()
            await pilot.pause()
            errors.extend(app.query_one(ErrorBanner).errors)

    run(scenario)
    assert len(handed) == 8
    assert any("after: did not unlock" in error for error in errors)
    assert all(state.get(record.track.key) != GOT for record in records)


def test_stop_browser_batch_leaves_unfinished_tracks_new(
    state, tmp_path, monkeypatch
):
    record = LinkRecord(
        category="gate",
        track=Track(
            id=203,
            title="Manual gate",
            permalink_url="https://soundcloud.com/a/203",
        ),
        link_url="https://hypeddit.com/track/203",
        link_text="Download",
    )
    app = make_app([record], state)

    class Client:
        def download_track(self, *_args, **_kwargs):
            raise gate_models.GateManualActionRequired("browser")

        def close(self):
            pass

    class Session:
        def close(self):
            pass

    entered = Event()

    def browser_batch(items, _directory, cancel, **_kwargs):
        entered.set()
        assert cancel.wait(2), "the UI did not signal the browser worker"
        return gate_models.HypedditBrowserBatchResult(
            failures=((items[0][0].key, gate_models.GateManualActionRequired("cancelled")),),
            cancelled=True,
        )

    app._client = Client()
    monkeypatch.setattr(
        "dj_digger.soundcloud.create_requests_session", Session
    )
    monkeypatch.setattr(
        "dj_digger.gates.browser.download_hypeddit_batch_in_browser",
        browser_batch,
    )

    async def scenario():
        async with app.run_test() as pilot:
            row = app.playlist_state.visible_rows[0]
            worker = app.download_controller.batch_download_in_background([(row, record.link_url)])
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(20):
                await pilot.pause(0.01)
                if app.download_state._browser_batch_active:
                    break
            app.action_cancel_job()
            await worker.wait()
            await pilot.pause()

    run(scenario)
    assert app.download_state._gate_cancel.is_set()
    assert state.get(record.track.key) != GOT
    assert app.download_state._browser_batch_active is False


def test_saved_hypeddit_hub_is_normalised_before_batch_and_never_opens_chromium(
    state, monkeypatch
):
    wrapper = "https://hypeddit.com/duxnbass/epitome"
    track = Track(
        id=204,
        title="Epitome",
        permalink_url="https://soundcloud.com/duxnbass/epitome",
        description=f"Download: {wrapper}",
    )
    app = make_app(links.categorise(track), state)
    app.playlist_state.crate = crate_models.CrateRecord(source="saved", title="Saved", tracks=[track])

    class Client:
        def download_track(self, *_args, **_kwargs):
            pytest.fail("a pure hub is not a downloadable gate")

        def close(self):
            pass

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        "dj_digger.gates.hubs.inspect_link_page",
        lambda *_args, **_kwargs: gate_models.LinkPageInspection(
            shops=(
                ("https://www.beatport.com/release/epitome/4194268", "Beatport"),
                ("https://duxnbass.bandcamp.com/album/epitome", "Bandcamp"),
            ),
            recognized=True,
        ),
    )
    monkeypatch.setattr(
        "dj_digger.soundcloud.create_requests_session", Session
    )
    monkeypatch.setattr(
        "dj_digger.gates.browser.download_hypeddit_batch_in_browser",
        lambda *_args, **_kwargs: pytest.fail("a hub must not enter Chromium"),
    )
    app._client = Client()

    async def scenario():
        async with app.run_test() as pilot:
            row = app.playlist_state.visible_rows[0]
            worker = app.download_controller.batch_download_in_background([(row, wrapper)])
            await worker.wait()
            await pilot.pause()

    run(scenario)
    assert sorted(app.playlist_state.rows[0].categories) == ["bandcamp", "beatport"]
    assert wrapper not in track.description
    assert state.get(track.key) != GOT


def test_soundcloud_login_refreshes_the_client_then_retries_once(
    state, tmp_path, monkeypatch
):
    record = LinkRecord(
        category="soundcloud",
        track=Track(
            title="Account download", artist="Artist",
            permalink_url="https://soundcloud.com/a/account", id=101,
            downloadable=True, has_downloads_left=True,
        ),
        link_url="https://soundcloud.com/a/account",
        link_text=links.FREE_DOWNLOAD,
    )
    app = make_app([record], state)

    class OldClient:
        client_id = "client-id"

        def __init__(self):
            self.calls = 0
            self.closed = False

        def download_track(self, *_args, **_kwargs):
            self.calls += 1
            raise soundcloud.SoundCloudLoginRequired("login")

        def close(self):
            self.closed = True

    class NewClient:
        def __init__(self, **_kwargs):
            self.calls = 0

        def download_track(self, *_args, **_kwargs):
            self.calls += 1
            path = tmp_path / "account.mp3"
            path.write_bytes(b"audio")
            return path

        def close(self):
            pass

    old = OldClient()
    new = NewClient()
    refreshed_with = []
    app._client = old
    monkeypatch.setattr(
        "dj_digger.soundcloud.SoundCloudClient",
        lambda **kwargs: refreshed_with.append(kwargs.get("oauth_token")) or new,
    )
    monkeypatch.setattr(
        "dj_digger.auth.verify_and_save",
        lambda token, client_id: (token, "DJ", 1),
    )

    async def scenario():
        async with app.run_test() as pilot:
            row = app.playlist_state.visible_rows[0]
            worker = app.download_controller.download_track_in_background(row.track)
            for _ in range(50):
                await pilot.pause()
                if isinstance(app.screen, SoundCloudAuthScreen):
                    break
            assert not worker.is_finished
            await pilot.pause()
            assert isinstance(app.screen, SoundCloudAuthScreen)
            app.screen.query_one("#soundcloud-token", Input).value = "hidden-token"
            await pilot.click("#soundcloud-paste")
            for _ in range(30):
                await pilot.pause()
                if state.get(row.track.key) == GOT:
                    break

    run(scenario)
    assert old.calls == 1
    assert old.closed is True
    assert new.calls == 1
    assert refreshed_with == [None], "the adopted client must read current persisted credentials"
    assert state.get(record.track.key) == GOT


def test_a_soundcloud_login_retires_the_client_after_workers_settle(state):
    app = make_app(synthetic_records(1), state)
    class Client:
        closed = False
        def close(self):
            self.closed = True
    client = Client()
    app._client = client
    async def scenario():
        async with app.run_test():
            with app.services.worker():
                assert app.download_controller._adopt_login("fresh-token") is True
                assert app._client is not client
                assert client.closed is False
                assert app._client._oauth_token is None
    run(scenario)
    assert client.closed is True


@pytest.mark.parametrize("batch", [False, True])
def test_download_stays_at_zero_while_the_link_is_being_resolved(
    state, monkeypatch, tmp_path, batch
):
    record = LinkRecord(
        category="soundcloud",
        track=Track(
            title="Free",
            permalink_url="https://soundcloud.com/a/free",
            id=7,
            downloadable=True,
            has_downloads_left=True,
        ),
        link_url="https://soundcloud.com/a/free",
        link_text=links.FREE_DOWNLOAD,
    )
    app = make_app([record], state)
    seen = []
    app._client = ProgressProbeClient(app, seen, tmp_path / "free.mp3")

    class Session:
        def close(self):
            pass

    monkeypatch.setattr("dj_digger.soundcloud.create_requests_session", Session)

    async def scenario():
        async with app.run_test():
            row = app.playlist_state.visible_rows[0]
            worker = (
                app.download_controller.batch_download_in_background([(row, None)])
                if batch
                else app.download_controller.download_track_in_background(row.track)
            )
            await worker.wait()

    run(scenario)
    assert seen == [0.0]


@pytest.mark.parametrize(
    ("outcome", "completed"),
    [
        ("single_success", True),
        ("single_failure", False),
        ("batch_success", True),
        ("batch_failure", False),
        ("batch_complete", False),
    ],
)
def test_download_results_do_not_move_the_viewport(state, monkeypatch, tmp_path, outcome, completed):
    app = make_app(synthetic_records(60), state)

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            await settle(app, pilot)  # exclude the initial scan/layout from completion assertions
            table = app.query_one("#tracks", TrackTable)
            table.move_cursor(row=30)
            await scroll_table(pilot, table, 20)
            row = app.playlist_state.visible_rows[35]
            key = row.track.key
            app.download_state.download_progress[key] = 0.42
            app.table_controller._paint_key(key)
            await pilot.pause()
            cursor = table.cursor_row
            viewport = table.scroll_offset
            rebuilds = []
            monkeypatch.setattr(table, "clear", lambda *a, **k: rebuilds.append(1))

            if completed:
                await asyncio.to_thread(app.services.downloads.record, key, tmp_path / "track.mp3")
            if outcome == "single_success":
                app.download_controller._download_finished(key, tmp_path / "track.mp3")
            elif outcome == "single_failure":
                app.download_controller._download_failed(key, "network broke")
            elif outcome == "batch_success":
                app.download_controller._download_finished(key, str(tmp_path / "track.mp3"), toast=False)
            elif outcome == "batch_failure":
                app.download_controller._download_failed(key, "network broke", banner_label=row.track.label)
            else:
                app.download_controller._on_batch_download_complete(0, 1, 1)
            await pilot.pause()

            assert rebuilds == []
            assert table.cursor_row == cursor
            assert table.scroll_offset == viewport
            assert "[42%]" not in str(table.get_row_at(35)[TITLE_CELL])
            assert (state.get(key) == GOT) is completed

    run(scenario)


def test_hidden_completion_outside_the_current_view_does_not_rebuild(state, monkeypatch, tmp_path):
    app = make_app(synthetic_records(4), state)

    async def scenario():
        async with app.run_test():
            hidden_row = app.playlist_state.visible_rows[0]
            app.playlist_state.search_term = "Track 3"
            app.playlist_state.hide_handled = True
            app.table_controller.refresh_rows(keep_cursor=False)
            table = app.query_one("#tracks", TrackTable)
            rebuilds = []
            monkeypatch.setattr(table, "clear", lambda *a, **k: rebuilds.append(1))

            app.download_controller._download_finished(hidden_row.track.key, tmp_path / "track.mp3")

            assert rebuilds == []
            assert len(app.playlist_state.visible_rows) == 1

    run(scenario)


@pytest.mark.parametrize(("cursor_row", "scroll_y"), [(30, 20), (10, 40)])
def test_refresh_rows_preserves_the_viewport(state, cursor_row, scroll_y):
    app = make_app(synthetic_records(60), state)

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            table = app.query_one("#tracks", TrackTable)
            table.move_cursor(row=cursor_row)
            await scroll_table(pilot, table, scroll_y)
            cursor = table.cursor_row
            viewport = table.scroll_offset

            app.table_controller.refresh_rows()
            await pilot.pause()

            assert table.cursor_row == cursor
            assert table.scroll_offset == viewport

    run(scenario)


def test_refresh_rows_keeps_the_same_tracks_after_rows_above_are_removed(state):
    app = make_app(synthetic_records(60), state)

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            table = app.query_one("#tracks", TrackTable)
            table.move_cursor(row=45)
            await scroll_table(pilot, table, 40)
            cursor_key = app.playlist_state.visible_rows[table.cursor_row].track.key
            top_key = app.playlist_state.visible_rows[table.scroll_offset.y].track.key

            app.playlist_state.hide_handled = True
            state.set(app.playlist_state.visible_rows[10].track.key, GOT)
            app.table_controller.refresh_rows()
            await pilot.pause()

            assert app.playlist_state.visible_rows[table.cursor_row].track.key == cursor_key
            assert app.playlist_state.visible_rows[table.scroll_offset.y].track.key == top_key

    run(scenario)


def test_back_to_back_refreshes_keep_the_same_tracks(state):
    app = make_app(synthetic_records(60), state)

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            table = app.query_one("#tracks", TrackTable)
            table.move_cursor(row=10)
            await scroll_table(pilot, table, 40)
            cursor_key = app.playlist_state.visible_rows[table.cursor_row].track.key
            top_key = app.playlist_state.visible_rows[table.scroll_offset.y].track.key
            assert table.scroll_offset.y > 0

            app.playlist_state.hide_handled = True
            state.set(app.playlist_state.rows[20].track.key, GOT)
            app.table_controller.refresh_rows()
            state.set(app.playlist_state.rows[21].track.key, GOT)
            app.table_controller.refresh_rows()
            await pilot.pause()

            assert app.playlist_state.visible_rows[table.cursor_row].track.key == cursor_key
            assert app.playlist_state.visible_rows[table.scroll_offset.y].track.key == top_key

    run(scenario)


def test_a_row_that_is_about_to_be_hidden_is_not_lit(state):
    """With handled rows hidden, the row leaves rather than flashing in place."""

    app = make_app(synthetic_records(3), state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("h")  # hide handled
            await pilot.press("g")
            await settle(app, pilot)
            assert app.query_one("#tracks", DataTable).row_count == 2

    run(scenario)


def test_the_frame_timer_sleeps_until_something_plays(state, monkeypatch):
    """Waking thirty times a second to watch a still list is just a warm laptop."""

    app = player_app(synthetic_records(3), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.audio_state._ticker._active.is_set() is False

            await pilot.press("space")
            await pilot.pause()
            assert app.audio_state._ticker._active.is_set() is True

    run(scenario)


def test_the_frame_timer_wakes_when_the_audio_actually_arrives(state, monkeypatch):
    """It slept through the half second the stream took to resolve, and stayed
    asleep - so the clock read 0:00 for the whole track."""

    app = player_app(synthetic_records(2), state)
    asked = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation=None: asked.append(track))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            app.playback_controller._tick()  # a frame lands while the stream is still resolving
            assert app.audio_state._ticker._active.is_set() is False

            app.playback_controller._audio_ready(asked[0], a_stream(), [1, 2])
            await pilot.pause()
            assert app.audio_state._ticker._active.is_set() is True

    run(scenario)


def test_the_frame_timer_stops_again_when_playback_does(state, monkeypatch):
    app = player_app(synthetic_records(3), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            app.player.playing = False
            app.playback_controller._tick()
            assert app.audio_state._ticker._active.is_set() is False

    run(scenario)


def test_turning_animation_off_slows_the_frame_timer_down(state):
    """The thing that repaints most cannot ignore the setting that says not to."""

    app = player_app(synthetic_records(1), state)

    async def scenario():
        async with app.run_test():
            app.animation_level = "full"
            assert app.playback_controller.frame_interval == keymap.TICK
            app.animation_level = "none"
            assert app.playback_controller.frame_interval == keymap.CALM_TICK

    run(scenario)


def test_the_player_bar_grows_instead_of_appearing_from_nothing(state, monkeypatch):
    app = player_app(synthetic_records(2), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))

    async def scenario():
        async with app.run_test() as pilot:
            bar = app.query_one("#player", PlayerBar)
            assert bar.wanted_height == 0

            await pilot.press("space")
            await pilot.pause()
            assert bar.wanted_height == PLAYER_HEIGHT
            assert bar.styles.height.value == PLAYER_HEIGHT

    run(scenario)


def test_any_player_failure_becomes_a_message_not_a_crash(state, monkeypatch):
    """miniaudio raises its own numbered errors, which the pump would die on."""

    app = player_app(synthetic_records(1), state)

    async def scenario():
        async with app.run_test(notifications=True) as pilot:
            def refuse():
                raise RuntimeError("failed to start audio device, -1")

            app.player.load(app.playlist_state.visible_rows[0].track, a_stream(), None)
            app.player.toggle = refuse
            await pilot.press("space")
            await pilot.pause()
            # Still alive, and the failure is on screen rather than a traceback.
            assert "failed to start audio device" in str(
                app.query_one("#player-title", Static).render()
            )

    run(scenario)


def test_a_tui_crash_reaches_the_log_before_the_screen_is_torn_down(state, caplog):
    app = make_app(synthetic_records(1), state)

    async def scenario():
        async with app.run_test() as pilot:
            with caplog.at_level(logging.ERROR, logger="dj_digger"):
                app.call_later(_boom)
                await pilot.pause()

    def _boom():
        raise RuntimeError("wires crossed")

    with pytest.raises(RuntimeError, match="wires crossed"):
        run(scenario)
    assert "Unhandled exception in the TUI" in caplog.text
    assert "wires crossed" in caplog.text


def test_the_audio_worker_opens_the_stream_off_the_ui_thread(state, monkeypatch):
    """A connect on the interface thread freezes the whole app until it answers."""

    app = player_app(synthetic_records(1), state)
    threads = []

    def fake_open(session, url):
        threads.append(threading.current_thread() is threading.main_thread())
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr("dj_digger.tui.playback.resolve_stream", lambda client, tid: a_stream())
    monkeypatch.setattr("dj_digger.tui.playback.fetch_waveform", lambda client, url: [1, 2, 3])
    monkeypatch.setattr("dj_digger.tui.playback.open_source", fake_open)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert threads == [False]
            assert app.player.source is not None

    run(scenario)


def test_the_transport_row_comes_and_goes_with_the_bar_and_closes_it(state, monkeypatch):
    """The waveform had no way out: nothing stopped it short of quitting."""

    app = player_app(synthetic_records(2), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))

    async def scenario():
        async with app.run_test() as pilot:
            controls = app.query_one(PlayerControls)
            assert not controls.display

            await pilot.press("space")
            await pilot.pause()
            assert controls.display
            assert str(app.query_one("#player-play", Button).label) == PAUSE_GLYPH

            await pilot.wait_for_scheduled_animations()
            assert await pilot.click("#player-close")
            await pilot.pause()
            assert app.player.loaded is None
            assert not app.player.playing
            assert not controls.display
            assert app.query_one("#player", PlayerBar).wanted_height == 0

    run(scenario)


def test_the_volume_slider_maps_a_click_to_a_level(state):
    app = player_app(synthetic_records(1), state)

    async def scenario():
        async with app.run_test():
            slider = app.query_one(VolumeSlider)
            slider.set_from_x(VOLUME_TRACK_START)
            assert app.player.volume == pytest.approx(0.0)
            slider.set_from_x(VOLUME_TRACK_START + VOLUME_TRACK)
            assert app.player.volume == pytest.approx(1.0)
            slider.set_from_x(VOLUME_TRACK_START + VOLUME_TRACK // 2)
            assert app.player.volume == pytest.approx(0.5)
            # Off either end of the track is the end of the track, not an error.
            slider.set_from_x(0)
            assert app.player.volume == pytest.approx(0.0)

    run(scenario)


def test_the_player_bar_folds_away_when_there_is_nothing_to_say(state, monkeypatch):
    app = player_app(synthetic_records(2), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", loading_fetch(app, []))

    async def scenario():
        async with app.run_test() as pilot:
            bar = app.query_one("#player", PlayerBar)
            await pilot.press("space")
            await pilot.pause()

            app.player.loaded = None
            bar.refresh_bar()
            assert bar.wanted_height == 0

    run(scenario)


def test_digging_shows_something_turning(state, monkeypatch):
    """A spinner is the difference between "working" and "hung"."""

    monkeypatch.setattr("dj_digger.services.collection.dig", lambda target, **kwargs: crate_of(1))
    app = make_app([], state, export_format="none")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.start_job("Digging", cancel=app._dig_cancel, detail="Fetching tracks 3/9")

            app.audio_state._frame = keymap.SPINNER_EVERY - 1
            app.playback_controller._tick()
            first = bar_text(app)
            app.audio_state._frame = 2 * keymap.SPINNER_EVERY - 1
            app.playback_controller._tick()

            assert "Fetching tracks 3/9" in first
            assert any(glyph in first for glyph in keymap.SPINNER)
            assert bar_text(app) != first
            app.finish_job()

    run(scenario)


def test_space_toggles_a_track_that_is_already_loaded(records, state, monkeypatch):
    app = player_app(records, state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation=None: None)

    async def scenario():
        async with app.run_test() as pilot:
            track = app.playlist_state.visible_rows[0].track
            app.player.load(track, a_stream(), None)
            await pilot.press("space")
            assert app.player.playing is True
            await pilot.press("space")
            assert app.player.playing is False

    run(scenario)


def test_seek_keys_nudge_the_position(records, state):
    app = player_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            app.player.load(app.playlist_state.visible_rows[0].track, a_stream(), None)
            app.player.position = 100.0
            await pilot.press("right_square_bracket")
            await pilot.press("left_square_bracket")

    run(scenario)
    assert app.player.seeks == [110.0, 100.0]


def test_volume_and_mute_keys(records, state):
    app = player_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("minus")
            assert app.player.volume == pytest.approx(0.7)
            await pilot.press("equals_sign")
            assert app.player.volume == pytest.approx(0.8)
            await pilot.press("m")
            assert app.player.muted is True

    run(scenario)


def test_a_track_without_an_id_cannot_be_previewed(state, monkeypatch):
    track = Track(title="No id", permalink_url="https://soundcloud.com/a/x")
    app = player_app(links.categorise_all([track]), state)
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda t: pytest.fail("should not fetch"))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()

    run(scenario)


def test_pressing_play_twice_without_audio_does_not_crash(records, state, monkeypatch):
    """The first press went through the guarded loader, the second did not."""

    app = make_app(records, state)  # the real Player, so the real toggle path runs

    def no_device(*args, **kwargs):
        raise PlaybackUnavailable("No audio output on this machine")

    monkeypatch.setattr(app.player, "_device_for", no_device)
    track = records[0].track

    async def scenario():
        async with app.run_test() as pilot:
            app.player._loaded = Loaded(track=track, stream=a_stream(duration=200.0))
            app.player._info = SimpleNamespace(sample_rate=44100, nchannels=2)
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            # Loaded, so the bar itself is the waveform; the message goes where
            # the title would be.
            assert "No audio output" in str(app.query_one("#player-title", Static).render())

    run(scenario)


def test_a_dead_audio_device_is_only_probed_once(monkeypatch):
    from dj_digger.player import Player

    attempts = []

    class Boom:
        def __init__(self, **kwargs):
            attempts.append(kwargs)
            raise RuntimeError("failed to init device")

    subject = Player()
    subject._miniaudio = SimpleNamespace(PlaybackDevice=Boom)

    for _ in range(3):
        with pytest.raises(PlaybackUnavailable):
            subject._device_for(44100, 2)

    assert len(attempts) == 1


def test_a_playback_failure_shows_a_message_instead_of_crashing(records, state):
    app = player_app(records, state)

    async def scenario():
        async with app.run_test() as pilot:
            app.playback_controller._playback_failed("This machine has no audio output")
            await pilot.pause()
            bar = app.query_one("#player", PlayerBar)
            assert "no audio output" in str(bar.render())

    run(scenario)


def test_clicking_the_waveform_maps_to_a_time(records, state):
    app = player_app(records, state)

    async def scenario():
        async with app.run_test():
            bar = app.query_one("#player", PlayerBar)
            app.player.load(app.playlist_state.visible_rows[0].track, a_stream(), None)
            width = bar._bar_width()
            assert bar.seconds_at(1) == pytest.approx(0.0)
            assert bar.seconds_at(1 + width) == pytest.approx(app.player.duration)

    run(scenario)


def test_the_player_is_closed_on_exit(records, state):
    app = player_app(records, state)

    async def scenario():
        async with app.run_test():
            pass

    run(scenario)
    assert app.player.closed is True


def test_export_writes_the_visible_rows(records, state, tmp_path):
    output = tmp_path / "view.json"
    app = make_app(records, state, export_path=output)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("2")  # bandcamp only
            await pilot.press("e")

    run(scenario)

    written = json.loads(output.read_text(encoding="utf-8"))
    assert sum(len(items) for items in written.values()) == sum(
        1 for record in records if record.category == "bandcamp"
    )


def test_clean_gate_badge_name(state):
    rec = LinkRecord(
        track=Track("Test Track", "Artist", "https://soundcloud.com/test/track"),
        category="gate",
        link_url="https://hypeddit.com/exaltation/krvzyintotheabyss-1",
        link_text="Download",
    )
    app = make_app([rec], state)
    row = app.playlist_state.rows[0]
    badges = app.table_controller._store_badges(row)
    assert str(badges) == "gate(hypeddit)"


def test_batch_download_skips_skipped_tracks(state, monkeypatch):
    rec1 = LinkRecord(
        track=Track("Track 1", "Artist", "https://soundcloud.com/1", downloadable=True),
        category="gate",
        link_url="https://hypeddit.com/test1",
        link_text="Download",
    )
    rec2 = LinkRecord(
        track=Track("Track 2", "Artist", "https://soundcloud.com/2", downloadable=True),
        category="gate",
        link_url="https://hypeddit.com/test2",
        link_text="Download",
    )
    state.set(rec2.track.key, SKIP)
    app = make_app([rec1, rec2], state)

    started = []
    monkeypatch.setattr(app.download_controller, "batch_download_in_background", lambda items, handle=None: started.extend(items))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("D")

    run(scenario)
    assert len(started) == 1
    assert started[0][0].track.key == rec1.track.key


def test_batch_marks_an_existing_local_file_got_without_downloading(
    state, tmp_path, monkeypatch
):
    track = Track(
        id=303,
        title="Already here",
        permalink_url="https://soundcloud.com/a/already-here",
        downloadable=True,
        has_downloads_left=True,
        download_url="https://api-v2.soundcloud.com/tracks/303/download",
    )
    local_file = tmp_path / "Already here.wav"
    local_file.write_bytes(b"RIFF-audio")
    track.local_path = str(local_file)
    state.set_local_file(track.key, local_file)
    app = make_app(links.categorise(track), state)
    started = []
    monkeypatch.setattr(
        app.download_controller, "batch_download_in_background", lambda items, handle=None: started.extend(items)
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("D")

    run(scenario)
    assert started == []
    assert state.get(track.key) == GOT


def test_a_missing_file_clears_its_path_and_file_backed_got(state, tmp_path):
    track = Track(
        id=304,
        title="Gone",
        permalink_url="https://soundcloud.com/a/gone",
    )
    missing = tmp_path / "deleted.wav"
    track.local_path = str(missing)
    state.set_local_file(track.key, missing)
    app = make_app(links.categorise(track), state)

    async def scenario():
        async with app.run_test() as pilot:
            await app.scan_controller.apply_local_file_matches(StubScanner({}))
            await pilot.pause()

            assert track.local_path is None
            assert state.local_file(track.key) is None
            assert state.get(track.key) == "new"

            await pilot.click("#tracks", offset=(10, 1), button=3)
            await pilot.pause()
            assert isinstance(app.screen, ContextMenuScreen)
            assert not any(
                action in {"copy", "copy_file"}
                for action, _label in app.screen.options
            )

    run(scenario)


def test_a_legacy_stale_cache_match_clears_its_old_got_status(records, state):
    app = make_app(records, state)
    key = records[0].track.key
    state.set(key, GOT)

    class StaleScanner:
        def match_track(self, _track):
            return None

        def had_stale_match(self, track):
            return track.key == key

    async def scenario():
        async with app.run_test():
            await app.scan_controller.apply_local_file_matches(StaleScanner())

    run(scenario)
    assert state.get(key) == "new"
    assert records[0].track.local_path is None


def test_context_menu_can_copy_a_local_track_into_the_playlist_folder(
    state, tmp_path
):
    source = tmp_path / "Music" / "Artist - Track.wav"
    source.parent.mkdir()
    source.write_bytes(b"RIFF-local-audio")
    track = Track(
        id=305,
        title="Track",
        artist="Artist",
        permalink_url="https://soundcloud.com/a/track",
        local_path=str(source),
    )
    state.set_local_file(track.key, source)
    app = make_app(links.categorise(track), state)
    app.playlist_state.crate_title = "Playlist / One"
    app.config.download_directory = str(tmp_path / "Downloads")

    async def scenario():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.click("#tracks", offset=(10, 1), button=3)
            await pilot.pause()
            assert isinstance(app.screen, ContextMenuScreen)
            assert ("copy_file", "Copy file to playlist folder") in app.screen.options
            await pilot.press("escape")

            worker = app.scan_controller.copy_local_file_in_background(track)
            await worker.wait()
            await pilot.pause()

    run(scenario)
    target = tmp_path / "Downloads" / "Playlist One" / source.name
    assert target.read_bytes() == b"RIFF-local-audio"
    assert track.local_path == str(target)
    assert state.local_file(track.key) == str(target)


class ClosingSource:
    """A prepared stream that records whether anybody let go of it."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_leaving_stops_the_ticker_and_lets_go_of_everything(records, state, monkeypatch):
    """There were two on_unmount methods, so only the second one ever ran.

    Which meant the thirty-a-second ticker was left running - and nothing
    noticed, because no test covered the way out at all.
    """

    app = make_app(records, state)
    closed = []
    source = ClosingSource()

    async def scenario():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            monkeypatch.setattr(
                type(app.player), "close", lambda self: closed.append("player")
            )
            app.audio_state._prepared = Prepared(
                track=Track(title="next", permalink_url="https://soundcloud.com/a/2", id=2),
                stream=Stream(url="https://cdn/2.mp3"),
                source=source,
            )
            app.exit()

        assert app.audio_state._ticker is None, "the ticker was left running"
        assert closed == ["player"], "the audio device was not handed back"
        assert source.closed, "the prefetched stream was left open"
        assert app.cart_state._cart_cancel.is_set(), "the store browser worker was not signalled"

    run(scenario)


def gate_row(*pairs):
    """One row carrying (category, url) links, in the order given."""

    track = Track(title="T", permalink_url="https://soundcloud.com/a/b", id=5)
    return Row(
        position=1,
        track=track,
        records=[
            LinkRecord(category=category, track=track, link_url=url, link_text="Buy")
            for category, url in pairs
        ],
    )


@pytest.mark.parametrize(
    "row,expected",
    [
        # An explicit gate beats a shop that happens to come first.
        (
            gate_row(("bandcamp", "https://x.bandcamp.com/a"), ("gate", "https://hypeddit.com/track/z")),
            "https://hypeddit.com/track/z",
        ),
        # Cloud storage has no category of its own, but gates can still unwrap it.
        (
            gate_row(("others", "https://www.mediafire.com/file/abc")),
            "https://www.mediafire.com/file/abc",
        ),
        (
            gate_row(("others", "https://drive.google.com/file/d/abc/view")),
            "https://drive.google.com/file/d/abc/view",
        ),
        # A shop page is not something a resolver can turn into a file.
        (gate_row(("bandcamp", "https://x.bandcamp.com/a"), ("beatport", "https://beatport.com/t/1")), None),
        (gate_row(("streaming", "https://open.spotify.com/track/1")), None),
        # Anything else unrecognised is worth handing over as a last resort.
        (gate_row(("smartlink", "https://lnk.to/abc")), "https://lnk.to/abc"),
        # Nothing to hand over at all.
        (gate_row(("soundcloud", "https://soundcloud.com/a/b")), None),
    ],
)
def test_the_gate_link_w_would_use(state, row, expected):
    """Three passes: the declared gate, then a host gates knows, then anything left."""

    assert make_app([], state).opening_controller._find_gate_url(row) == expected


def test_app_lifecycle_and_routes_cannot_be_shadowed():
    """Keep the regression for the accidentally shadowed on_unmount handler."""
    import ast
    import inspect

    from textual.app import App
    assert DiggerApp.__bases__ == (App,)
    tree = ast.parse(Path(inspect.getfile(DiggerApp)).read_text())
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DiggerApp")
    methods = [node.name for node in app_class.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and not any(isinstance(d, ast.Attribute) and d.attr == "setter"
                           for d in node.decorator_list)]
    assert methods.count("on_unmount") == 1
    assert len(methods) == len(set(methods))


class StubScanner:
    """Answers match_track from a dict, so no test touches a real music folder."""

    def __init__(self, matches):
        self.matches = matches

    def match_track(self, track):
        return self.matches.get(track.key)

    def had_stale_match(self, _track):
        return False


def test_a_confident_match_marks_an_untouched_track_as_got(records, state):
    app = make_app(records, state)
    key = records[0].track.key
    scanner = StubScanner({key: LocalMatch("/music/a.mp3", confident=True)})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.scan_controller.apply_local_file_matches(scanner)

            assert state.get(key) == GOT
            assert app.playlist_state.rows[0].track.local_path == "/music/a.mp3"

    run(scenario)


def test_a_confident_match_promotes_an_opened_track_to_got(records, state):
    app = make_app(records, state)
    key = records[0].track.key
    state.set(key, OPENED)
    scanner = StubScanner({key: LocalMatch("/music/a.mp3", confident=True)})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.scan_controller.apply_local_file_matches(scanner)

            assert state.get(key) == GOT
            assert app.playlist_state.rows[0].track.local_path == "/music/a.mp3"

    run(scenario)


def test_a_loose_match_points_at_the_file_without_claiming_you_have_it(records, state):
    """A title that happens to agree is not evidence you own the track."""

    app = make_app(records, state)
    key = records[0].track.key
    scanner = StubScanner({key: LocalMatch("/music/maybe.mp3", confident=False)})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.scan_controller.apply_local_file_matches(scanner)

            assert app.playlist_state.rows[0].track.local_path == "/music/maybe.mp3"
            assert state.get(key) == "new", "a loose match must not mark anything"

    run(scenario)


def test_a_confident_scan_marks_even_a_skipped_track_as_got(records, state):
    """A confirmed file on disk is stronger evidence than a stale status."""

    app = make_app(records, state)
    key = records[0].track.key
    state.set(key, SKIP)
    scanner = StubScanner({key: LocalMatch("/music/a.mp3", confident=True)})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.scan_controller.apply_local_file_matches(scanner)

            assert state.get(key) == GOT
            assert app.playlist_state.rows[0].track.local_path == "/music/a.mp3", "the badge still belongs"

    run(scenario)


def test_a_matched_track_is_badged_in_the_table(records, state):
    app = make_app(records, state)
    key = records[0].track.key
    scanner = StubScanner({key: LocalMatch("/music/a.mp3", confident=True)})

    async def scenario():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await app.scan_controller.apply_local_file_matches(scanner)
            await settle(app, pilot)

            table = app.query_one("#tracks", DataTable)
            leading = table.get_cell_at(Coordinate(0, 0))
            title = table.get_cell_at(Coordinate(0, TITLE_CELL))
            assert str(leading) == keymap.LOCAL_FILE_GLYPH + " "
            assert "\U0001f4c1" not in str(title)

    run(scenario)


def test_copying_the_path_says_so_either_way(records, state, monkeypatch, tmp_path):
    app = make_app(records, state)
    key = records[0].track.key
    local_file = tmp_path / "a.mp3"
    local_file.write_bytes(b"audio")
    said = []
    monkeypatch.setattr(DiggerApp, "notify", lambda self, msg, **kw: said.append(msg))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("y")
            assert "No local file matched" in said[-1]

            await app.scan_controller.apply_local_file_matches(
                StubScanner({key: LocalMatch(str(local_file), confident=True)})
            )
            monkeypatch.setattr(
                "dj_digger.tui.library_scan.copy_to_clipboard", lambda text: True
            )
            await pilot.press("y")
            assert str(local_file) in said[-1]

    run(scenario)


def test_the_first_launch_asks_for_the_settings_before_anything_else(state, tmp_path):
    """No config file means nothing is configured, including the scan folders."""

    (tmp_path / "config.json").unlink()  # conftest wrote one; a first run has none
    app = make_app([], state)
    assert app.config.first_run is True

    async def scenario():
        # Left on screen deliberately: dismissing it would start the library
        # scan, and this profile still points at the real ~/Music.
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)

    run(scenario)


def test_tracks_a_refresh_brought_in_are_marked_and_sorted_to_the_top(state):
    record = saved_crate(2, title="Grown")
    library.refresh(record, Crate(
        source=record.source,
        title=record.title,
        tracks=list(record.tracks) + [
            Track(
                title="Arrived",
                permalink_url="https://soundcloud.com/a/new",
                id=900,
                purchase_url="https://label.bandcamp.com/track/new",
            )
        ],
    ))
    library.save(record)
    app = make_app([], state)

    async def scenario():
        async with app.run_test() as pilot:
            app.crate_controller.load_crate(record)
            await pilot.pause()
            table = app.query_one("#tracks", DataTable)
            first = table.get_cell_at(Coordinate(0, TITLE_CELL))
            assert first.plain.startswith("NEW ")
            assert "Arrived" in first.plain
            second = table.get_cell_at(Coordinate(1, TITLE_CELL))
            assert not second.plain.startswith("NEW ")

    run(scenario)


def test_playback_a_b_a_rejects_the_first_a_result(state, monkeypatch):
    app = player_app(synthetic_records(2), state)
    asked = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation: asked.append((track, generation)))

    async def scenario():
        async with app.run_test():
            controller = app.playback_controller
            a, b = [row.track for row in app.playlist_state.rows]
            for track in (a, b, a):
                controller._start_playback(track)
            old = FakeSource()
            track, generation = asked[0]
            controller._audio_ready(track, a_stream(), [], old, generation)
            assert old.closed
            assert app.player.loaded is None
            track, generation = asked[-1]
            controller._audio_ready(track, a_stream(), [], None, generation)
            assert app.player.loaded.track.key == a.key

    run(scenario)


def test_prefetch_a_b_a_rejects_old_generation_even_when_key_matches(state):
    app = player_app(synthetic_records(2), state)

    async def scenario():
        async with app.run_test():
            controller = app.playback_controller
            app.audio_state._preparing = "2"
            generation = app.audio_state._preparation_generation
            controller._discard_prepared()
            app.audio_state._preparing = "1"
            controller._discard_prepared()
            app.audio_state._preparing = "2"
            source = FakeSource()
            controller._preparation_done("2", prepared_for(app, 1, source), generation)
            assert source.closed
            assert app.audio_state._prepared is None
            assert app.audio_state._preparing == "2"

    run(scenario)


def test_cancel_job_dismisses_a_cart_owned_dialog_and_releases_after_return(state):
    app = make_app(synthetic_records(1), state)
    returned = []

    async def waiting():
        controller = app.opening_controller
        try:
            returned.append(await controller._wait_cart_screen(ConfirmScreen("Continue?")))
        finally:
            app.services.operations.finish(app.cart_state._cart_handle)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.opening_controller._claim_cart()
            worker = app.run_worker(waiting(), group="cart")
            async with asyncio.timeout(5):
                while not isinstance(app.screen, ConfirmScreen) or not app.screen.is_mounted:
                    await pilot.pause(.01)
            assert isinstance(app.screen, ConfirmScreen)
            app.action_cancel_job()
            await worker.wait()
            await pilot.pause()
            assert returned == [None]
            assert app.services.operations.active() is None
            assert not isinstance(app.screen, ConfirmScreen)

    run(scenario)


def test_stale_coalesced_bytes_cannot_change_a_new_operation(state):
    app = make_app(synthetic_records(1), state)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            old = app.services.operations.start("Downloading")
            app.services.operations.finish(old)
            newer = app.services.operations.start("Downloading")
            app.download_state._download_handle = newer
            app.download_controller._update_track_progress("1", .5, old.id)
            assert "1" not in app.download_state.download_progress
            app.services.operations.finish(newer)

    run(scenario)


def test_keyboard_navigation_remains_available_while_a_status_write_waits(state, monkeypatch):
    app = make_app(synthetic_records(3), state)
    entered, release = Event(), Event()
    original = state.db.set_track_state

    def blocked(*args):
        entered.set()
        assert release.wait(3), "test must release its fake database lock"
        return original(*args)

    monkeypatch.setattr(state.db, "set_track_state", blocked)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#tracks", TrackTable)
            mark = asyncio.create_task(pilot.press("g"))
            move = None
            try:
                while not entered.is_set():
                    await asyncio.sleep(.001)
                move = asyncio.create_task(pilot.press("down"))
                await asyncio.sleep(.15)
                cursor_during_write = table.cursor_row
            finally:
                release.set()
                await asyncio.gather(mark, *([move] if move else []))
                await settle(app, pilot)
            assert cursor_during_write == 1
            assert table.cursor_row == 1, "late status completion must not advance the moved cursor"

    run(scenario)


def test_loaded_a_keyboard_intent_invalidates_pending_b(state, monkeypatch):
    app = player_app(synthetic_records(2), state)
    asked = []
    monkeypatch.setattr(app.playback_controller, "fetch_audio", lambda track, generation: asked.append((track, generation)))

    async def scenario():
        async with app.run_test() as pilot:
            controller = app.playback_controller
            a, b = [row.track for row in app.playlist_state.rows]
            controller._audio_ready(a, a_stream(), [])
            table = app.query_one("#tracks", TrackTable)
            table.move_cursor(row=1)
            await pilot.press("space")
            table.move_cursor(row=0)
            await pilot.press("space")
            assert asked[-1][0].key == b.key
            source = FakeSource()
            controller._audio_ready(b, a_stream(), [], source, asked[-1][1])
            assert source.closed
            assert app.player.loaded.track.key == a.key

    run(scenario)


@pytest.mark.parametrize('count', [1, 3])
def test_rapid_mark_keys_keep_toggle_and_advance_order_while_database_waits(state, monkeypatch, count):
    app = make_app(synthetic_records(count), state)
    entered, release = Event(), Event()
    original = state.db.set_track_state

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(state.db, 'set_track_state', blocked)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press('g')
            assert await asyncio.to_thread(entered.wait, 2)
            try:
                await pilot.press('g')
            finally:
                release.set()
            await app.workers.wait_for_complete()
            keys = [row.track.key for row in app.playlist_state.rows]
            if count == 1:
                assert state.get(keys[0]) == 'new'
            else:
                assert [state.get(key) for key in keys] == [GOT, GOT, 'new']
                assert app.query_one('#tracks', TrackTable).cursor_row == 2

    run(scenario)


def test_cancelling_profile_write_holds_download_slot_and_does_not_open_late_auth(state, monkeypatch):
    records = synthetic_records(2)
    for record in records:
        record.track.downloadable = record.track.has_downloads_left = True
    app = make_app(records, state)
    entered, release = Event(), Event()
    calls = []

    class Client:
        def download_track(self, track, *args, **kwargs):
            calls.append(track.key)
            if track.key == records[0].track.key:
                raise gate_models.GateProfileRequired('profile')
            raise soundcloud.SoundCloudLoginRequired('login')

        def close(self):
            pass

    def save(answer):
        entered.set()
        assert release.wait(3)

    app._client = Client()
    monkeypatch.setattr(app.services.accounts, 'save_profile', save)

    async def scenario():
        async with app.run_test() as pilot:
            worker = app.download_controller.batch_download_in_background([(row, None) for row in app.playlist_state.rows])
            try:
                for _ in range(50):
                    await pilot.pause()
                    if isinstance(app.screen, GateProfileScreen):
                        break
                app.screen.query_one('#gate-profile-name', Input).value = 'Test'
                app.screen.query_one('#gate-profile-email', Input).value = 'test@example.com'
                app.screen.query_one('#gate-profile-save', Button).press()
                assert await asyncio.to_thread(entered.wait, 2)
                handle = app.services.operations.active()
                await pilot.press('ctrl+x')
                await asyncio.sleep(.1)
                assert handle.state == 'cancelling'
                assert app.services.operations.active() is handle
                assert not worker.is_finished
            finally:
                release.set()
            await worker.wait()
            await pilot.pause()
            assert app.services.operations.active() is None
            assert not isinstance(app.screen, SoundCloudAuthScreen)
            assert len(calls) == 2

    run(scenario)


def test_late_sidebar_read_cannot_replace_a_more_recent_selection(state, monkeypatch):
    first = saved_crate(1, source='https://soundcloud.com/a/sets/one', title='One')
    second = saved_crate(1, source='https://soundcloud.com/a/sets/two', title='Two')
    app = make_app(synthetic_records(1), state)
    entered, release = Event(), Event()
    original = app.services.library.load

    def load(source):
        record = original(source)
        if source == first.source:
            entered.set()
            assert release.wait(3)
        return record

    monkeypatch.setattr(app.services.library, 'load', load)

    async def scenario():
        async with app.run_test():
            slow = app.run_worker(app.crate_controller.open_crate(crate_models.CrateHeader(first.source, first.title, '')))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                await app.crate_controller.open_crate(crate_models.CrateHeader(second.source, second.title, ''))
                assert app.playlist_state.crate.source == second.source
            finally:
                release.set()
            await slow.wait()
            assert app.playlist_state.crate.source == second.source

    run(scenario)


def test_queued_mark_is_discarded_when_the_playlist_changes(state, monkeypatch):
    app = make_app(synthetic_records(1), state)
    entered, release = Event(), Event()
    original = state.db.set_track_state

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(state.db, 'set_track_state', blocked)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press('g')
            assert await asyncio.to_thread(entered.wait, 2)
            try:
                await pilot.press('g')
                new_track = Track(id=500, title='Another playlist', permalink_url='https://soundcloud.com/a/500')
                app.crate_controller.load_records(links.categorise(new_track))
            finally:
                release.set()
            await app.workers.wait_for_complete()
            assert state.get('500') == 'new'

    run(scenario)


def test_local_explorer_and_export_dialog_defaults(state, tmp_path, monkeypatch):
    from textual.widgets import Checkbox, Tree

    from dj_digger.services.local_library import LocalLibrary
    from dj_digger.tui.local_screens import ExportOptions

    path = tmp_path / 'local.wav'
    path.write_bytes(b'not decoded in this UI test')
    local = LocalLibrary(state.db)
    track = local.register(path)
    monkeypatch.setattr(LocalLibrary, 'register', lambda self, path, **kwargs: track)
    app = make_app([], state)

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            app.local_controller.open(tmp_path)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app.playlist_state.rows) == 1
            assert app.playlist_state.rows[0].records == []
            assert app.playlist_state.rows[0].track.local_id
            assert app.query_one('#explorer', Tree)
            app.action_local_export()
            await pilot.pause()
            assert isinstance(app.screen, ExportOptions)
            assert app.screen.profile().bits == 24
            assert app.screen.profile().rate == 48000
            assert not app.screen.query_one('#replace', Checkbox).value
            await pilot.press('escape')
            app.action_local_edit()
            await pilot.pause()
            app.screen.query_one('#bpm', Input).value = '128'
            app.screen.query_one('#save', Button).press()
            await pilot.pause()  # dispatch Button.Pressed before waiting for its worker
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.playlist_state.rows[0].track.bpm == 128

    run(scenario)


def test_playback_advances_past_duplicate_playlist_occurrences(state, monkeypatch):
    from copy import deepcopy
    app = player_app(synthetic_records(2), state)
    asked = []
    monkeypatch.setattr(app.playback_controller, 'fetch_audio', lambda track, generation: asked.append((track, generation)))

    async def scenario():
        async with app.run_test():
            controller = app.playback_controller
            first, second = [row.track for row in app.playlist_state.rows]
            app.crate_controller.set_tracks([first, second, deepcopy(first)])
            last = app.playlist_state.visible_rows[2]
            controller._start_playback(last.track)
            track, generation = asked[-1]
            controller._audio_ready(track, a_stream(), [], None, generation)
            assert controller._playing_index() == 2
            assert controller._step_from_playing(1) is None
            assert controller._step_from_playing(-1) == 1

    run(scenario)


def test_local_waveform_arrives_after_pause_and_rejects_replaced_audio(state, tmp_path, monkeypatch):
    import shutil
    import subprocess

    from dj_digger import local_audio
    from dj_digger.services.local_library import LocalLibrary

    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg is not installed')
    path = tmp_path / 'waveform.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.2', str(path)], check=True)
    track = LocalLibrary(state.db).register(path)
    app = player_app([], state)
    entered, release = Event(), Event()
    actual_waveform = local_audio.waveform

    def delayed_waveform(*args):
        entered.set()
        assert release.wait(10)
        return actual_waveform(*args)

    monkeypatch.setattr(local_audio, 'waveform', delayed_waveform)

    async def scenario():
        async with app.run_test() as pilot:
            app.crate_controller.set_tracks([track])
            source = SimpleNamespace(_closed=False, close=lambda: None)
            controller = app.playback_controller
            controller._audio_ready(track, a_stream(), [], source)
            loaded = app.player.loaded
            bar = app.query_one('#player', PlayerBar)
            empty_shape = list(bar._rows(loaded))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                controller.action_play_pause()
                assert not app.player.playing
            finally:
                release.set()
            await settle(app, pilot)
            assert len(loaded.waveform) == 1024
            assert max(loaded.waveform) > 0
            assert bar._rows(loaded) != empty_shape
            # A new load of the same file must not receive the old result.
            replacement = app.player.load(track, a_stream(), None, [])
            controller._waveform_ready(loaded, [99])
            assert replacement.waveform == []

    run(scenario)
