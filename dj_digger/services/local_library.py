"""Local file identities, lazy folder pages and centrally hydrated metadata."""

import json
import os
import re
from pathlib import Path

from ..media import FORMATS, MediaError, probe, signature
from ..models import Track, check_cancelled

PAGE_SIZE = 250


def media_track(db, record: dict) -> Track:
    metadata = json.loads(record['metadata_json'])
    values = db.media_values(record['id'])
    manual = json.loads(values.get('manual_json', '{}'))
    analysis = (json.loads(values.get('result_json', '{}'))
                if values.get('signature') == record['signature'] else {})
    tags = metadata.get('tags', {})
    try:
        bpm = float(tags.get('bpm') or tags.get('tbpm') or 0) or None
    except (TypeError, ValueError):
        bpm = None
    return Track(title=tags.get('title') or Path(record['path']).stem,
                 permalink_url='', artist=tags.get('artist', ''), local_id=record['id'],
                 local_path=record['path'], duration=int(metadata.get('duration', 0) * 1000),
                 bpm=manual.get('bpm') or analysis.get('bpm') or bpm,
                 key_signature=manual.get('key') or analysis.get('key') or tags.get('initialkey', ''))


class LocalLibrary:
    def __init__(self, db):
        self.db = db

    def register(self, path: Path, *, inspect=False, cancel=None) -> Track:
        selected_path = path.absolute()
        path = path.resolve(strict=True)
        if path.suffix.lower() not in FORMATS:
            raise MediaError('Unsupported audio file')
        current_signature = signature(path)
        for existing in self.db.media_at_identity(current_signature):
            if (existing['path'] != str(path) and json.loads(existing['signature'])[:4] == json.loads(current_signature)[:4]):
                from ..scanner import confirmed_missing
                if confirmed_missing(Path(existing['path']), self.db):
                    self.db.relocate_media(existing['id'], existing['path'], str(path), existing['signature'], current_signature)
        record = self.db.register_media(str(path), current_signature)
        if inspect and record['metadata_json'] == '{}':
            metadata = probe(path, cancel)
            if not self.db.update_media_metadata(record['id'], record['signature'], metadata):
                raise MediaError('File changed during inspection')
            record = self.db.media(record['id'])
        track = media_track(self.db, record)
        track.local_path = str(selected_path)
        return track

    def page(self, folder: Path, offset=0, *, cancel=None):
        """Read direct entries only. Permission errors never mark records deleted.

        Sorting names uses O(directory entries) memory, but probing/rows are paged.
        No recursion, hashing or decoding is triggered by opening a directory.
        """
        names, directories = [], []
        with os.scandir(folder) as entries:
            for entry in entries:
                check_cancelled(cancel)
                if entry.is_dir(follow_symlinks=False):
                    directories.append(entry.name)
                elif entry.name.lower().endswith(tuple(FORMATS)) and not re.search(r'\.[0-9a-f]{32}\.partial\.', entry.name) and entry.is_file():
                    names.append(entry.name)
        check_cancelled(cancel)
        folder = folder.resolve(strict=True)
        folder_stat = folder.stat()
        if self.db.observe_root(str(folder), folder_stat.st_dev, folder_stat.st_ino):
            self.db.mark_directory_missing(str(folder), {str(folder / name) for name in names}, folder_stat.st_dev)
        names.sort(key=str.casefold)
        directories.sort(key=str.casefold)
        tracks, failures = [], []
        for name in names[offset:offset + PAGE_SIZE]:
            check_cancelled(cancel)
            try:
                tracks.append(self.register(folder / name))
            except OSError as exc:
                failures.append(f'{name}: {exc}')
        return tracks, directories, len(names), failures

    def selection(self, folder: Path, *, recursive=False, cancel=None):
        """Frozen complete selection, independent of the currently displayed page."""
        result, seen = [], set()
        pending = [folder]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    check_cancelled(cancel)
                    if recursive and entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.dj-digger-'):
                        pending.append(Path(entry.path))
                    elif entry.name.lower().endswith(tuple(FORMATS)) and not re.search(r'\.[0-9a-f]{32}\.partial\.', entry.name) and entry.is_file():
                        path = Path(entry.path).resolve(strict=True)
                        stat = path.stat()
                        identity = (stat.st_dev, stat.st_ino)
                        if identity not in seen:
                            seen.add(identity)
                            result.append(Path(entry.path).absolute())
        return tuple(sorted(result, key=lambda path: str(path).casefold()))
