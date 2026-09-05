"""Local-file reconciliation and field-specific playlist edits, independent of UI."""

from pathlib import Path

from ..crate_models import CrateHeader, CrateRecord
from ..models import GOT, NEW
from ..scanner import SCAN_BATCH, LocalScanner, confirmed_missing
from ..state import FileMatch


class LibraryService:
    def __init__(self, state):
        self.state = state

    def headers(self):
        headers = [CrateHeader(**row) for row in self.state.db.list_crate_headers() if row.get("source")]
        return sorted(headers, key=lambda header: header.title.lower())

    def load(self, source):
        raw = self.state.db.load_crate(source.strip())
        if raw is not None and source.startswith('local-playlist:'):
            from dataclasses import asdict

            from .local_library import media_track
            raw['tracks'] = [asdict(media_track(self.state.db, record)) for record in self.state.db.local_playlist_media(source)]
        return CrateRecord.from_json(raw) if raw is not None else None

    def reset(self, keys):
        for key in keys:
            self.state.set(key, NEW)

    def delete(self, source):
        record = self.load(source)
        if not source.startswith('local-playlist:'):
            self.reset([track.key for track in record.tracks] if record else [])
        self.state.db.delete_crate(source.strip())

    def remember_beatport(self, source, generation, outcome):
        raw = self.state.db.remember_beatport(source, generation, outcome)
        return CrateRecord.from_json(raw) if raw is not None else None

    def remove_tracks(self, source, generation, keys, *, removed):
        raw = self.state.db.set_removed_tracks(source, generation, keys, removed)
        return self.load(source) if raw is not None else None

    def forget_missing(self, track):
        observed = self.state.observe_file(track.key)
        path = observed.path or track.local_path
        if not path or not confirmed_missing(Path(path), self.state.db):
            return False
        self.state.apply_file_matches([FileMatch(track.key, None, False, True, observed.revision)])
        track.local_path = self.state.local_file(track.key)
        return track.local_path is None

    def mark_existing(self, track):
        remembered = self.state.local_file(track.key)
        if remembered:
            track.local_path = remembered
        if self.forget_missing(track) or not track.local_path:
            return False
        if self.state.get(track.key) != GOT or remembered != track.local_path:
            self.state.set_local_file(track.key, track.local_path)
        return True

    def needs_copy(self, path, directory):
        if not path:
            return False
        source = Path(path)
        return source.is_file() and not source.resolve().is_relative_to(directory.resolve())

    def scanner(self, directories):
        return LocalScanner([Path(d).expanduser() for d in directories], db=self.state.db)

    def match_tracks(self, tracks, scanner):
        """Resolve paths outside transactions; persist certain matches in short batches."""
        paths = {}
        pending = []
        for track in tracks:
            observed = self.state.observe_file(track.key)
            remembered = observed.path or track.local_path
            stale = bool(remembered) and confirmed_missing(Path(remembered), self.state.db)
            if remembered and not stale:
                path, confident = remembered, True
            else:
                match = scanner.match_track(track)
                path, confident = (match.path, match.confident) if match else (None, False)
                stale = stale or scanner.had_stale_match(track)
            if path is not None or stale or track.local_path is not None:
                paths[track.key] = path
            pending.append(FileMatch(track.key, path, confident, stale, observed.revision))
            if len(pending) == SCAN_BATCH:
                self.state.apply_file_matches(pending)
                pending.clear()
        self.state.apply_file_matches(pending)
        # A newer completed transfer can supersede a missing-file observation.
        return {key: self.state.local_file(key) or path for key, path in paths.items()}
