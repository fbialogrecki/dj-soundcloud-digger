"""Every key the crate browser binds, and the constants its display is built on.

One source for the bindings, the footer and the help screen, so the three
cannot drift apart.
"""

from ..models import GOT, NEW, OPENED, SKIP

# A mark is one glyph in a one-cell gutter. Spelling "skipped" out cost seven
# columns on every row to say "new" on nearly all of them; the width belongs to
# the track title instead. HelpScreen carries the words.
# The style is a palette role (see tui/theme.py), resolved against the active
# theme when a row is painted.
STATUS_STYLES = {
    NEW: ("\u00b7", "muted", "not looked at yet"),
    OPENED: ("\u25cb", "secondary", "link opened, outcome unknown"),
    SKIP: ("\u2717", "muted", "skipped"),
    GOT: ("\u2713", "bold success", "got it"),
}

PLAYING_GLYPH = "\u25b6"
LOCAL_FILE_GLYPH = "\u25a3"
LEADING_WIDTH = 2
OPEN_ALL_CONFIRM_THRESHOLD = 20
# How long before the end of a track we start getting the next one ready. Long
# enough to cover a signed URL, a waveform and the first megabytes of audio on a
# poor connection; short enough that a filter change rarely wastes the work.
PREFETCH_LEAD = 20.0

# Thirty frames a second, which is what a pulse needs to read as one rather than
# as a stutter. It only costs anything while a track is playing: with nothing
# going out, _tick leaves on its first line. Redrawing a waveform this often is
# only affordable because a frame is now a few style ranges - see paint_waveform.
TICK = 1 / 30
# Turning animation off - TEXTUAL_ANIMATIONS=none, which is what you do over a
# slow link - has to turn this off too, or the one thing that repaints the most
# would carry on regardless. The clock and the auto-advance still need a pulse,
# just not thirty of them a second.
CALM_TICK = 0.25
# The spinner is slower than the frame rate on purpose; braille that turns thirty
# times a second is a smear.
SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
SPINNER_EVERY = 4
# Long enough to catch the eye, short enough that holding a mark key still works.
FLASH = 0.25
# Number keys select the nth store that this crate actually contains, so `1` is
# always the first store you have rather than a fixed category.
QUICK_FILTER_KEYS = 9

# The footer wants 161 columns to show every binding below and has never had
# them, so Textual clipped the last one mid-word. These are the actions it gives
# up instead, least useful first; `?` still lists all of them.
FOOTER_OPTIONAL = (
    "batch_download",
    "download_track",
    "search('bandcamp')",
    "search('beatport')",
    "open_visible",
    "dig_link",
    "open_settings",
    "cart_visible",
    "cart_track",
    "mark_new",
    "mark_skip",
    "mark_got",
    "start_search",
)

# Everything except the title gets a fixed budget; the title takes the rest, so
# a wide terminal shows long titles instead of an empty margin.
MARK_WIDTH = 1
INDEX_WIDTH = 4
STORES_WIDTH = 22
GENRE_WIDTH = 14
TIME_WIDTH = 5
# The optional columns, switched on in Settings: (config name, header, width).
OPTIONAL_COLUMN_SPECS = (
    ("bpm", "BPM", 5),
    ("key", "Key", 4),
    ("year", "Year", 4),
    ("label", "Label", 14),
)
# 16, not 20: an 80-column terminal has 17 columns left for the title once the
# fixed ones, their padding and the vertical scrollbar are paid for, so a higher
# floor pushed the table past the screen and hung a horizontal scrollbar under
# it with the last digit of Time behind the edge. It is a floor for terminals
# this narrow only - at 140 columns the title still takes 49.
MIN_TITLE_WIDTH = 16

# These two say nothing as a word - "shop" and "others" are what is left after
# every recognised store, so the domain is the only thing that identifies them.
DOMAIN_BADGE_CATEGORIES = {"shop", "others"}

# Categories whose link goes to a shop page, which is not something a gate
# resolver can unwrap into a file.


SELECTED = "Selected track"
WHOLE_LIST = "Whole visible list"
CRATES = "Playlists"
PLAYBACK = "Playback"
OTHER = "Other"

# One source for the footer and the help screen, so they cannot drift apart:
# (key, action, footer label, section, show in footer, longer help text).
# Footer labels stay short because it gets one line; help has the room to explain.
KEYMAP = [
    ('ctrl+u', 'local_resume', 'Resume export', OTHER, False, 'Resume the most recent unfinished folder export'),
    ('ctrl+r', 'local_section', 'Sidebar section', OTHER, False, 'Toggle playlists, explorer or both; useful in small terminals'),
    ('ctrl+f', 'local_folder', 'Folder', OTHER, False, 'Open a local music directory'),
    ('ctrl+e', 'local_export', 'Audio export', OTHER, False, 'Prepare a folder of deck-compatible audio'),
    ('j', 'local_analyze', 'Analyze', OTHER, False, 'Estimate BPM and key for selected local audio'),
    ('ctrl+k', 'local_edit', 'BPM/key', OTHER, False, 'Edit manual BPM and key; double/halve tempo'),
    ('ctrl+l', 'local_playlist', 'Local playlist', OTHER, False, 'Add local files to a local playlist'),
    ('ctrl+n', 'local_page', 'Next page', OTHER, False, 'Next page of the current directory'),
    ('ctrl+p', 'local_pin', 'Pin folder', OTHER, False, 'Pin the current directory in the explorer'),
    ('ctrl+t', 'local_split', 'Panel split', OTHER, False, 'Change sidebar split: 50/50, 70/30, 30/70'),
    ('i', 'profile_playlists', 'Import profile', OTHER, False, 'Import playlists created by a SoundCloud profile'),
    ("o,enter", "open_link", "Open", SELECTED, True, "Open its best link, or the filtered store"),
    ("O", "open_visible", "Open all", WHOLE_LIST, True, "Open every link shown, asks above 20"),
    ("d", "download_track", "Download", SELECTED, True, "Download an artist-provided SoundCloud file"),
    ("D", "batch_download", "Download all", WHOLE_LIST, True, "Download all free & gate tracks in view"),
    ("ctrl+x", "cancel_job", "Stop", OTHER, False, "Stop the running dig, batch, scan or cart; what finished is kept"),
    ("b", "search('bandcamp')", "Search in Bandcamp", SELECTED, True, "Search Bandcamp for highlighted track"),
    ("B", "search('beatport')", "Search in Beatport", SELECTED, True, "Search Beatport for highlighted track"),
    ("c", "cart_track", "Cart/playlist", SELECTED, True, "Add the exact track to the Bandcamp cart, or line it up for a Beatport playlist"),
    ("C", "cart_visible", "Cart/playlist all", WHOLE_LIST, True, "Preflight every store track shown: Bandcamp into the cart, Beatport into a playlist"),
    ("y", "copy_path", "Copy path", SELECTED, False, "Copy the path of the local file that matches"),
    ("g", "mark_got", "Got", SELECTED, True, "Mark as got, press again to undo"),
    ("k", "mark_skip", "Skip", SELECTED, True, "Mark as skipped, press again to undo"),
    ("u", "mark_new", "Unmark", SELECTED, True, "Clear the mark either way"),
    ("x", "remove_track", "Remove", SELECTED, False, "Remove from this playlist, locally only"),
    ("ctrl+z", "undo_remove", "Undo", SELECTED, False, "Put back the last removed track"),
    ("space", "play_pause", "Play", PLAYBACK, True, "Play or pause the highlighted track"),
    ("left_square_bracket", "seek(-1)", "Back", PLAYBACK, False, "Back 10 seconds"),
    ("right_square_bracket", "seek(1)", "Forward", PLAYBACK, False, "Forward 10 seconds"),
    ("n", "play_step(1)", "Next", PLAYBACK, False, "Play the next track in the list"),
    ("p", "play_step(-1)", "Previous", PLAYBACK, False, "Play the previous track"),
    ("minus", "volume(-1)", "Quieter", PLAYBACK, False, "Turn it down"),
    ("equals_sign", "volume(1)", "Louder", PLAYBACK, False, "Turn it up"),
    ("m", "mute", "Mute", PLAYBACK, False, "Mute or unmute"),
    ("ctrl+w", "close_player", "Close player", PLAYBACK, False, "Stop and fold the player away"),
    ("P", "open_beatport_tracks", "Beatport pages", WHOLE_LIST, False, "Open every exact Beatport track page shown in your browser, to add to cart by hand"),
    ("e", "export", "Export", WHOLE_LIST, False, "Write the rows shown to the export file"),
    ("slash", "start_search", "Search", WHOLE_LIST, True, "Filter by artist, title, genre, tag or label"),
    ("t", "sort_next", "Sort", WHOLE_LIST, False, "Sort by title, time, genre, status or store; again for the next"),
    ("T", "sort_flip", "Reverse sort", WHOLE_LIST, False, "Reverse the current sort"),
    ("v", "toggle_select", "Select", SELECTED, False, "Select or deselect this row"),
    ("V", "select_range", "Select to here", SELECTED, False, "Select from the last selected row to this one"),
    ("ctrl+a", "select_visible", "Select all", WHOLE_LIST, False, "Select every row shown, again to clear"),
    ("0", "filter_index(0)", "Show all", WHOLE_LIST, False, "Drop the store filter, show everything"),
    ("h", "toggle_handled", "Hide handled", WHOLE_LIST, False, "Hide what is got or skipped"),
    ("escape", "clear_filters", "Clear filters", WHOLE_LIST, False, "Clear the selection, then the search, then store filters and hiding"),
    ("a", "dig_link", "Add playlist", CRATES, True, "Dig a link into a new playlist"),
    ("r", "refresh_crate", "Refresh", CRATES, False, "Re-dig this playlist from SoundCloud"),
    ("X", "delete_crate", "Delete", CRATES, False, "Delete this playlist, after confirming"),
    ("U", "reset_crate_statuses", "Reset statuses", CRATES, False, "Reset all track statuses to 'new' for this playlist"),
    ("ctrl+b", "toggle_sidebar", "Playlists", CRATES, False, "Show or hide the playlist sidebar"),
    ("question_mark", "help", "Help", OTHER, True, "This screen"),
    ("s", "open_settings", "Settings", OTHER, True, "Configure profile name, email and gate comments"),
    ("q", "quit", "Quit", OTHER, True, "Leave (ctrl+c does the same)"),
]

# Bound ahead of the focused widget: Input takes ctrl+x as "cut", and a stop
# key that only works when the table has focus is not a stop key.
PRIORITY_KEYS = frozenset({"ctrl+x"})

# What each group actually operates on. The old footer never said, so it was
# impossible to tell whether a key hit one row or the whole list.
HELP_SCOPES = {
    SELECTED: "acts on the highlighted row only",
    WHOLE_LIST: "acts on the selection, or every row shown after filters",
    CRATES: "loads another playlist",
    PLAYBACK: "click the waveform to seek",
    OTHER: "",
}

# Textual's key identifiers are not what anyone wants to read in a help screen.
KEY_DISPLAY = {
    "slash": "/",
    "question_mark": "?",
    "minus": "-",
    "equals_sign": "=",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "o,enter": "o, enter",
    # Every single capital letter is a shifted key; show the physical letter
    # in lowercase so paired shortcuts read consistently as b / shift+b.
    **{key: f"shift+{key.lower()}" for key, *_rest in KEYMAP if len(key) == 1 and key.isupper()},
}
