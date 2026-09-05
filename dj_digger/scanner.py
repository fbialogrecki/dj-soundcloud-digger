"""Finding the tracks you already own.

Walks the configured folders for audio files, normalises their names, and offers
the crate browser a way to ask "do I have this one already?". The answer comes
with a confidence, because a filename is weak evidence: two different tracks can
easily share a title, and being wrong here would overwrite a decision the user
made by hand.
"""

import logging
import os
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from .db import Database, database
from .models import Track

LOGGER = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".aiff", ".m4a", ".aac", ".ogg", ".alac"}
# Files written to SQLite per transaction while scanning.
SCAN_BATCH = 200


def normalize_string(text: str) -> str:
    """Normalize string for fuzzy-safe track matching."""
    text = (text or "").lower()
    text = re.sub(r"[^\w]+", "", text, flags=re.UNICODE)
    return text


class LocalMatch(NamedTuple):
    """A file on disk that looks like a track, and how much it looks like it."""

    path: str
    # True when artist and title both matched. A title on its own is enough to
    # point at a file and nowhere near enough to mark a track as owned.
    confident: bool


class LocalScanner:
    """Background scanner for local audio files with mtime SQLite caching."""

    def __init__(self, directories: list[Path], db: Database | None = None) -> None:
        self.directories = directories
        # database(), not Database(): the scan runs on a worker thread and must
        # share the one process-wide instance the UI thread is already using.
        self.db = db or database()
        self._stale_stems: set[str] = set()
        self._exact_paths: dict[str, list[str]] = {}
        # Folders the walk could not enter, as "path: reason". The scan used to
        # step over them in silence, so a permissions problem on the music
        # drive looked like a library with nothing in it.
        self.errors: list[str] = []

    def _note_error(self, exc: OSError) -> None:
        self.errors.append(f"{exc.filename}: {exc.strerror or exc}")
        LOGGER.debug("Could not enter %s during scan: %s", exc.filename, exc)

    def scan(self, *, cancel: threading.Event | None = None) -> int:
        """Scan configured directories, updating the local_files cache in SQLite.

        ``cancel`` stops the walk at the next file; what was written stays, and
        the mtime cache makes the next scan pick up where this one left off.
        """
        cached = self.db.get_cached_files()
        self._stale_stems.clear()
        self.errors.clear()
        scanned = 0
        pending: list[tuple[str, float, str]] = []
        seen: set[str] = set()
        scanned_directories: set[Path] = set()
        visited: set[tuple[int, int]] = set()

        for root_dir in self.directories:
            if cancel is not None and cancel.is_set():
                self.db.upsert_local_files(pending)
                self._refresh_exact_paths()
                return scanned
            if not root_dir.exists():
                continue
            # Resolved once per root rather than once per file: resolve() is a
            # syscall per path component, and a music folder has thousands of
            # files. follow_symlinks keeps parity with the rglob this replaced,
            # which walked into linked folders too.
            # ponytail: a symlinked audio file is keyed by the link's path now.
            root = root_dir.resolve()
            root_stat = root.stat()
            same_volume = self.db.observe_root(str(root), root_stat.st_dev, root_stat.st_ino)
            for dirpath, _dirs, names in root.walk(
                follow_symlinks=True, on_error=self._note_error
            ):
                if cancel is not None and cancel.is_set():
                    self.db.upsert_local_files(pending)
                    self._refresh_exact_paths()
                    return scanned
                try:
                    info = dirpath.stat()
                except OSError as exc:
                    self._note_error(exc)
                    _dirs.clear()
                    continue
                identity = (info.st_dev, info.st_ino)
                if identity in visited:
                    _dirs.clear()
                    continue
                visited.add(identity)
                if same_volume:
                    scanned_directories.add(dirpath)
                for name in names:
                    if cancel is not None and cancel.is_set():
                        self.db.upsert_local_files(pending)
                        return scanned
                    if os.path.splitext(name)[1].lower() not in AUDIO_EXTENSIONS:
                        continue
                    entry = dirpath / name
                    path_str = str(entry)
                    seen.add(path_str)
                    try:
                        stat = os.stat(entry)
                    except OSError as exc:
                        LOGGER.debug("Skipping file %s during scan: %s", entry, exc)
                        continue
                    if path_str in cached and cached[path_str][0] == stat.st_mtime:
                        continue
                    pending.append((path_str, stat.st_mtime, normalize_string(entry.stem)))
                    scanned += 1
                    if len(pending) >= SCAN_BATCH:
                        self.db.upsert_local_files(pending)
                        pending = []
        self.db.upsert_local_files(pending)
        missing = [
            path
            for path in cached
            if path not in seen
            and Path(path).parent in scanned_directories
        ]
        for path in missing:
            self._stale_stems.add(cached[path][1])
        self.db.delete_local_files(missing)
        self._refresh_exact_paths()
        return scanned

    def _refresh_exact_paths(self) -> None:
        paths: dict[str, list[str]] = defaultdict(list)
        for path, (_mtime, stem) in self.db.get_cached_files().items():
            paths[stem].append(path)
        self._exact_paths = dict(paths)

    def match_track(self, track: Track) -> LocalMatch | None:
        """The local file that looks like this track, if there is one."""

        if not track.title:
            return None

        both = self._existing_match(
            normalize_string(f"{track.artist}{track.title}")
        )
        if both:
            return LocalMatch(both, confident=True)

        # A title alone. Short ones match far too much - "intro" is a filename
        # in every second folder - so there is a floor under how little evidence
        # is enough to even point at a file.
        title_stem = normalize_string(track.title)
        if len(title_stem) >= 6:
            loose = self._existing_match(title_stem)
            if loose:
                return LocalMatch(loose, confident=False)

            artist_stem = normalize_string(track.artist)
            if artist_stem:
                decorated = self._existing_match(
                    title_stem, self.db.find_unique_local_match, artist_stem
                )
                if decorated:
                    return LocalMatch(decorated, confident=True)

            # SoundCloud uploaders are often labels, while the actual artist is
            # written into the title. That full "Artist - Title" is still good
            # evidence when only one decorated filename contains it.
            if " - " in track.title and len(title_stem) >= 10:
                decorated = self._existing_match(title_stem, self.db.find_unique_local_match)
                if decorated:
                    return LocalMatch(decorated, confident=False)

        return None

    def _existing_match(
        self,
        normalized_stem: str,
        find: Callable[..., str | None] | None = None,
        *more: str,
    ) -> str | None:
        """What ``find`` (exact by default) returns for the stem, once it is still on disk.

        A cached row whose file has gone is dropped on the spot and the stem
        remembered, so a GOT that rested on that file can be undone.
        """

        if find is None:
            candidates = self._exact_paths.get(normalized_stem, [])
            while candidates:
                path = candidates[0]
                if Path(path).is_file():
                    return path
                if not confirmed_missing(Path(path), self.db):
                    return None
                self._stale_stems.add(normalized_stem)
                self.db.delete_local_files([path])
                candidates.remove(path)
            return None
        while path := find(normalized_stem, *more):
            if Path(path).is_file():
                return path
            if not confirmed_missing(Path(path), self.db):
                return None
            self._stale_stems.add(normalized_stem)
            self.db.delete_local_files([path])
        return None

    def had_stale_match(self, track: Track) -> bool:
        exact = normalize_string(f"{track.artist}{track.title}")
        title = normalize_string(track.title)
        return exact in self._stale_stems or (
            len(title) >= 6 and title in self._stale_stems
        )


def confirmed_missing(path: Path, db=None) -> bool:
    """Absence is established only by a successful complete parent listing."""
    try:
        if db is not None:
            for root in db.media_roots():
                if path.is_relative_to(root['path']):
                    stat = Path(root['path']).stat()
                    if (stat.st_dev, stat.st_ino) != (root['device'], root['inode']):
                        return False
        with os.scandir(path.parent) as entries:
            return all(entry.name != path.name for entry in entries)
    except OSError:
        return False
