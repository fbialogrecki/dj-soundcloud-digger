"""Remember which tracks you already grabbed, across every playlist.

Status lives in SQLite, keyed by SoundCloud track id, so buying a track once
marks it in every crate that contains it.

Until 0.9 every change was also mirrored into state.json, which nothing ever
read back - ``get`` has always asked SQLite. The mirror existed to be migrated
from, and it cost a full rewrite of the file on every single mark, which is why
a library scan needed ``batched()`` to hold it back. The current database has no
JSON import path; a state.json written by an older version is left alone.
"""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from .db import database
from .models import GOT, NEW, OPENED, STATUSES

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileObservation:
    path: str | None
    revision: int


@dataclass(frozen=True)
class FileMatch:
    key: str
    path: str | None
    confident: bool
    stale: bool
    revision: int


class TrackState:
    """Track status, stored in SQLite - the shared database unless a path says otherwise."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db = database(db_path)
        self._lock = threading.Lock()
        # Mirrors of the two small tables, loaded on first read. Painting a
        # crate asks for every row's status two or three times over, and a
        # SQLite round trip per ask made a 500-track crate stutter on each
        # keystroke in the search box. Every write goes through here, so the
        # mirror cannot drift from what this process wrote; another process
        # writing the same database is not something the TUI has ever handled.
        self._statuses: dict[str, str] | None = None
        self._files: dict[str, str] | None = None
        self._revisions: dict[str, int] = {}

    @property
    def path(self) -> Path:
        return self.db.path

    def _load(self) -> None:
        """Fill the mirrors; callers hold ``_lock``."""

        if self._statuses is None:
            self._statuses = {
                key: status
                for key, status in self.db.all_track_statuses().items()
                if status in STATUSES
            }
        if self._files is None:
            self._files = self.db.all_track_local_files()

    def get(self, key: str) -> str:
        if self._statuses is None:
            with self._lock:
                self._load()
        return self._statuses.get(str(key), NEW)

    def set(self, key: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status: {status}")
        with self._lock:
            self._load()
            self.db.set_track_state(key, status, None)
            # A direct user decision is no longer contingent on a particular
            # file. Automated scans/downloads use set_local_file instead.
            self._remember(key, status)
            self._files.pop(str(key), None)

    def mark_opened(self, key: str) -> None:
        """A completed browser handoff never replaces a later got/skip decision."""
        with self._lock:
            self._load()
            if self.get(key) == NEW:
                self.db.set_track_state(key, OPENED, None)
                self._remember(key, OPENED)
                self._files.pop(str(key), None)

    def _remember(self, key: str, status: str) -> None:
        self._revisions[str(key)] = self._revisions.get(str(key), 0) + 1
        if status == NEW:
            self._statuses.pop(str(key), None)
        else:
            self._statuses[str(key)] = status

    def local_file(self, key: str) -> str | None:
        if self._files is None:
            with self._lock:
                self._load()
        return self._files.get(str(key))

    def set_local_file(self, key: str, path: str | Path) -> None:
        with self._lock:
            self._load()
            self.db.set_track_state(key, GOT, str(path))
            self._remember(key, GOT)
            self._files[str(key)] = str(path)

    def clear_local_file(self, key: str) -> bool:
        """Forget a missing file and undo only the GOT that depended on it."""

        with self._lock:
            self._load()
            if self._files.get(str(key)) is None:
                return False
            status = NEW if self._statuses.get(str(key)) == GOT else self._statuses.get(str(key), NEW)
            self.db.set_track_state(key, status, None)
            self._files.pop(str(key), None)
            self._remember(key, status)
            return True

    def observe_file(self, key: str) -> FileObservation:
        """Read provenance and its revision together before inspecting the disk."""
        with self._lock:
            self._load()
            return FileObservation(self._files.get(str(key)), self._revisions.get(str(key), 0))

    def apply_file_matches(self, matches: list[FileMatch]) -> None:
        """Commit one scanner batch, then publish its status/provenance mirrors."""
        with self._lock:
            self._load()
            updates = []
            for match in matches:
                key, path = match.key, match.path
                confident, stale = match.confident, match.stale
                if path and confident:
                    updates.append((key, GOT, path))
                elif stale and match.revision == self._revisions.get(key, 0) and (key in self._files or self.get(key) == GOT):
                    status = NEW if self.get(key) == GOT else self.get(key)
                    updates.append((key, status, None))
            self.db.set_track_states(updates)
            for key, status, path in updates:
                self._remember(key, status)
                if path is None:
                    self._files.pop(key, None)
                else:
                    self._files[key] = path

    def reload_file_paths(self):
        """Refresh the mirror after a journaled replacement, invalidating old scans."""
        with self._lock:
            paths = self.db.all_track_local_files()
            for key in set(paths) | set(self._files or {}):
                if paths.get(key) != (self._files or {}).get(key):
                    self._revisions[key] = self._revisions.get(key, 0) + 1
            self._files = paths
