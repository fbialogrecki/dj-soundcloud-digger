"""SQLite repositories with a single connection owner and explicit transactions."""

import json
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4
from weakref import WeakSet

from .models import track_key
from .paths import data_dir
from .schema import open_database

_DATABASES = WeakSet()

# One Database per file for the whole process. Before this, library._db() built a
# fresh one on every call, and each one opened its own connection, re-ran every
# CREATE TABLE, and closed nothing. The lock covers _INSTANCES, which is
# read-then-written from worker threads (the library scan, downloads).
_INSTANCES: dict[Path, "Database"] = {}
_LOCK = threading.Lock()


def default_db_path() -> Path:
    return data_dir() / "digger.db"


def database(db_path: Path | None = None) -> "Database":
    """The shared Database for this file, built on first use."""

    path = (Path(db_path) if db_path else default_db_path()).expanduser().resolve()
    with _LOCK:
        instance = _INSTANCES.get(path)
        if instance is None or instance._closed:
            instance = Database(path)
            _INSTANCES[path] = instance
        return instance

def owned(method):
    """Run one repository call on the connection's owning thread."""
    @wraps(method)
    def invoke(self, *args, **kwargs):
        if threading.get_ident() == self._owner:
            return method(self, *args, **kwargs)
        return self._executor.submit(method, self, *args, **kwargs).result()
    return invoke


class Database:
    """One connection, created, used and closed on a dedicated thread.

    Public calls are synchronous for CLI/worker use. UI callers must await
    asyncio.to_thread; no cursor or connection crosses this boundary.
    """

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="digger-db")
        self._owner = None
        self._closed = False
        self._generations: dict[str, str] = {}
        self._generation_lock = threading.Lock()
        try:
            self._executor.submit(self._open).result()
        except BaseException:
            self._executor.shutdown()
            raise
        _DATABASES.add(self)

    def _open(self) -> None:
        self._owner = threading.get_ident()
        self._conn = open_database(self.path)

    def close(self) -> None:
        if not self._closed:
            self._executor.submit(self._conn.close).result()
            self._closed = True
            self._executor.shutdown()

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if threading.get_ident() != self._owner:
            raise RuntimeError("SQLite connection belongs to its database thread")
        conn = self._conn
        # Nested repository calls participate in the outer transaction.
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @owned
    def set_track_state(self, key: str, status: str, path: str | None) -> None:
        """Commit status and provenance together, including manual removal."""
        with self.connection(write=True):
            self.set_track_status(key, status)
            if path is None:
                self.delete_track_local_file(key)
            else:
                self.set_track_local_file(key, path)

    @owned
    def set_track_states(self, updates: list[tuple[str, str, str | None]]) -> None:
        with self.connection(write=True):
            for key, status, path in updates:
                self.set_track_state(key, status, path)

    # --- Track State API ---
    @owned
    def set_track_status(self, key: str, status: str) -> None:
        updated = datetime.now(UTC).isoformat(timespec="seconds")
        with self.connection(write=True) as conn:
            if status == "new":
                conn.execute("DELETE FROM track_states WHERE key = ?", (str(key),))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO track_states (key, status, updated) VALUES (?, ?, ?)",
                    (str(key), status, updated)
                )

    @owned
    def all_track_statuses(self) -> dict[str, str]:
        """Every non-new status at once; the table only holds the marked rows."""
        with self.connection() as conn:
            rows = conn.execute("SELECT key, status FROM track_states").fetchall()
            return {row["key"]: row["status"] for row in rows}

    @owned
    def all_track_local_files(self) -> dict[str, str]:
        with self.connection() as conn:
            rows = conn.execute("SELECT key, path FROM track_local_files").fetchall()
            return {row["key"]: row["path"] for row in rows}

    @owned
    def set_track_local_file(self, key: str, path: str) -> None:
        with self.connection(write=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO track_local_files (key, path) VALUES (?, ?)",
                (str(key), path),
            )

    @owned
    def delete_track_local_file(self, key: str) -> None:
        with self.connection(write=True) as conn:
            conn.execute("DELETE FROM track_local_files WHERE key = ?", (str(key),))

    def crate_generation(self, source: str) -> str:
        with self._generation_lock:
            return self._generations.get(source, "initial")

    def snapshot_generations(self):
        with self._generation_lock:
            return dict(self._generations)

    @owned
    def remember_collection(self, incoming: dict, generation: str | None = None):
        """Refresh current collection fields, retaining unrelated local decisions."""
        source = incoming["source"]
        with self.connection(write=True) as conn:
            provider_id = incoming.get('provider_id')
            if provider_id:
                alias = conn.execute('SELECT source FROM playlist_aliases WHERE provider_id=?', (str(provider_id),)).fetchone()
                if alias is not None:
                    source = alias['source']
                    incoming = dict(incoming, source=source)
            if isinstance(generation, dict):
                if generation.get(source, "initial") != self.crate_generation(source):
                    return None
            elif generation is not None and self.crate_generation(source) != generation:
                return None
            current = self.load_crate(source)
            if current is None:
                current = incoming
            else:
                def key(track):
                    return track_key(track)
                known = {key(track) for track in current.get("tracks", [])}
                arrived = [key(track) for track in incoming["tracks"] if key(track) not in known]
                if arrived:
                    current["new_track_keys"] = arrived
                current["tracks"] = incoming["tracks"]
                current["title"] = incoming.get("title") or current["title"]
                current["partial"] = incoming["partial"]
                if "preserve_order" in incoming:
                    current["preserve_order"] = incoming["preserve_order"]
                current["refreshed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            if provider_id:
                current['provider_id'] = provider_id
            self.save_crate(current)
            if provider_id:
                conn.execute('INSERT OR REPLACE INTO playlist_aliases VALUES(?,?)', (str(provider_id), source))
            return current

    @owned
    def set_removed_tracks(self, source: str, generation: str, keys: list[str], removed: bool):
        with self.connection(write=True):
            if self.crate_generation(source) != generation:
                return None
            record = self.load_crate(source)
            if record is None:
                return None
            kept = list(record.get("removed_track_keys", []))
            for key in keys:
                if removed and key not in kept:
                    kept.append(key)
                elif not removed and key in kept:
                    kept.remove(key)
            record["removed_track_keys"] = kept
            self.save_crate(record)
            return record

    @owned
    def remember_beatport(self, source, generation, outcome):
        from .crate_models import CrateRecord
        from .store_match import _remember_exact_beatport_links
        with self.connection(write=True):
            if self.crate_generation(source) != generation:
                return None
            raw = self.load_crate(source)
            if raw is None:
                return None
            record = CrateRecord.from_json(raw)
            if _remember_exact_beatport_links(record, outcome):
                raw = record.to_json()
                self.save_crate(raw)
            return raw

    @owned
    def merge_track_metadata(self, source: str, generation: str, updates: dict) -> bool:
        """Patch link/file fields of current tracks; never recreate a deleted crate."""
        allowed = {"purchase_url", "purchase_title", "extra_links", "download_url", "local_path", "description"}
        if any(set(fields) - allowed for fields in updates.values()):
            raise ValueError("Not a track metadata patch")
        with self.connection(write=True):
            if self.crate_generation(source) != generation:
                return False
            record = self.load_crate(source)
            if record is None:
                return False
            removed = set(record.get("removed_track_keys", []))
            for track in record.get("tracks", []):
                key = track_key(track)
                if key in updates and key not in removed:
                    track.update(updates[key])
            self.save_crate(record)
            return True

    # --- Crates API ---
    @owned
    def save_crate(self, record: dict[str, Any]) -> None:
        """Store a whole ``CrateRecord.to_json()``.

        source, title and updated are kept as columns as well, so a listing can
        be ordered without parsing every record.
        """

        updated = record.get("refreshed_at") or record.get("imported_at") or ""
        with self.connection(write=True) as conn:
            conn.execute(
                """INSERT INTO crates (source, title, updated, record_json)
                   VALUES (?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET
                   title=excluded.title, updated=excluded.updated, record_json=excluded.record_json""",
                (
                    record["source"],
                    record.get("title") or "",
                    updated,
                    json.dumps(record, ensure_ascii=False),
                ),
            )

    @owned
    def load_crate(self, source: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT record_json FROM crates WHERE source = ?", (source,)
            ).fetchone()
            return json.loads(row["record_json"]) if row else None

    @owned
    def list_crate_headers(self) -> list[dict[str, Any]]:
        """source, title, updated and the partial flag, without parsing a record.

        The sidebar only shows titles, but the listing used to deserialise
        every track of every crate to draw it - at startup and again after each
        dig. ``partial`` lives inside the JSON, so it comes out through SQLite's
        own parser (json_extract is built in since SQLite 3.38).
        """

        with self.connection() as conn:
            rows = conn.execute(
                """SELECT source, title, updated,
                          json_extract(record_json, '$.partial') AS partial
                   FROM crates ORDER BY updated DESC"""
            ).fetchall()
        return [
            {
                "source": row["source"],
                "title": row["title"],
                "updated": row["updated"],
                "partial": bool(row["partial"]),
            }
            for row in rows
        ]

    @owned
    def delete_crate(self, source: str) -> None:
        with self.connection(write=True) as conn:
            conn.execute("DELETE FROM crates WHERE source = ?", (source,))
        with self._generation_lock:
            self._generations[source] = uuid4().hex

    # --- Local File Cache API ---
    @owned
    def get_cached_files(self) -> dict[str, tuple[float, str]]:
        """Return dict of path -> (mtime, normalized_stem)."""
        with self.connection() as conn:
            cur = conn.execute("SELECT path, mtime, normalized_stem FROM local_files")
            return {row["path"]: (row["mtime"], row["normalized_stem"]) for row in cur.fetchall()}

    @owned
    def upsert_local_files(self, rows: list[tuple[str, float, str]]) -> None:
        """Write a batch of ``(path, mtime, normalized_stem)``.

        One transaction for the lot: the scan used to commit per file, which on
        a Windows drive mounted into WSL meant an fsync per track.
        """
        if not rows:
            return
        with self.connection(write=True) as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO local_files (path, mtime, normalized_stem)
                   VALUES (?, ?, ?)""",
                rows,
            )

    @owned
    def delete_local_files(self, paths: list[str]) -> None:
        if not paths:
            return
        with self.connection(write=True) as conn:
            conn.executemany(
                "DELETE FROM local_files WHERE path = ?",
                ((path,) for path in paths),
            )

    @owned
    def find_local_match(self, normalized_stem: str) -> str | None:
        with self.connection() as conn:
            cur = conn.execute("SELECT path FROM local_files WHERE normalized_stem = ? LIMIT 1", (normalized_stem,))
            row = cur.fetchone()
            return row["path"] if row else None

    @owned
    def find_unique_local_match(
        self, containing: str, also_containing: str = ""
    ) -> str | None:
        """Return a decorated filename match only when it is unambiguous."""
        condition = "instr(normalized_stem, ?) > 0"
        values = [containing]
        if also_containing:
            condition += " AND instr(normalized_stem, ?) > 0"
            values.append(also_containing)
        with self.connection() as conn:
            row = conn.execute(
                f"""SELECT MIN(path) AS path,
                           COUNT(DISTINCT normalized_stem) AS variants
                    FROM local_files WHERE {condition}""",
                values,
            ).fetchone()
            return row["path"] if row and row["variants"] == 1 else None

    @owned
    def register_media(self, path: str, signature: str, *, parent_id=None) -> dict:
        with self.connection(write=True) as conn:
            row = conn.execute('SELECT * FROM media_files WHERE path=?', (path,)).fetchone()
            if row is None:
                media_id = uuid4().hex
                conn.execute('INSERT INTO media_files(id,path,signature,parent_id) VALUES(?,?,?,?)',
                             (media_id, path, signature, parent_id))
            else:
                media_id = row['id']
                if row['signature'] != signature:
                    conn.execute("UPDATE media_files SET signature=?,metadata_json='{}',available=1 WHERE id=?",
                                 (signature, media_id))
                else:
                    conn.execute('UPDATE media_files SET available=1 WHERE id=?', (media_id,))
            return dict(conn.execute('SELECT * FROM media_files WHERE id=?', (media_id,)).fetchone())

    @owned
    def media(self, media_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM media_files WHERE id=?', (media_id,)).fetchone()
            return dict(row) if row is not None else None

    @owned
    def update_media_metadata(self, media_id, signature, metadata) -> bool:
        with self.connection(write=True) as conn:
            return bool(conn.execute('UPDATE media_files SET metadata_json=? WHERE id=? AND signature=?',
                                     (json.dumps(metadata), media_id, signature)).rowcount)

    @owned
    def media_values(self, media_id) -> dict:
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM media_analysis WHERE media_id=?', (media_id,)).fetchone()
            if row is None:
                return {}
            return dict(row)

    @owned
    def save_analysis(self, media_id, signature, algorithm, result) -> bool:
        with self.connection(write=True) as conn:
            row = conn.execute('SELECT signature FROM media_files WHERE id=?', (media_id,)).fetchone()
            if row is None or row['signature'] != signature:
                return False
            conn.execute('''INSERT INTO media_analysis(media_id,signature,algorithm,result_json)
                         VALUES(?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET
                         signature=excluded.signature,algorithm=excluded.algorithm,result_json=excluded.result_json''',
                         (media_id, signature, algorithm, json.dumps(result)))
            return True

    @owned
    def set_media_manual(self, media_id, values):
        with self.connection(write=True) as conn:
            conn.execute('''INSERT INTO media_analysis(media_id,manual_json) VALUES(?,?)
                         ON CONFLICT(media_id) DO UPDATE SET manual_json=excluded.manual_json''',
                         (media_id, json.dumps(values)))

    @owned
    def save_local_playlist(self, source, title, media_ids):
        with self.connection(write=True) as conn:
            existing = self.load_crate(source)
            self.save_crate(existing or {'source': source, 'title': title, 'tracks': [], 'version': 2})
            start = conn.execute('SELECT COALESCE(MAX(position),-1)+1 FROM local_playlist_items WHERE source=?', (source,)).fetchone()[0]
            conn.executemany('INSERT INTO local_playlist_items VALUES(?,?,?)',
                             ((source, start+i, mid) for i, mid in enumerate(media_ids)))

    @owned
    def local_playlist_media(self, source):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute('''SELECT m.* FROM local_playlist_items p
                   JOIN media_files m ON m.id=p.media_id WHERE p.source=? ORDER BY p.position''', (source,))]

    @owned
    def record_media_operation(self, operation_id, record):
        with self.connection(write=True) as conn:
            conn.execute('INSERT OR REPLACE INTO media_operations VALUES(?,?)', (operation_id, json.dumps(record)))

    @owned
    def media_operations(self):
        with self.connection() as conn:
            return {row['id']: json.loads(row['record_json']) for row in conn.execute('SELECT * FROM media_operations')}

    @owned
    def commit_media_replacement(self, media_id, old_path, new_path, signature, metadata):
        with self.connection(write=True) as conn:
            existing = conn.execute('SELECT path FROM media_files WHERE id=?', (media_id,)).fetchone()
            if existing is None or existing['path'] not in (old_path, new_path):
                raise ValueError('Media identity changed before replacement commit')
            conn.execute('UPDATE media_files SET path=?,signature=?,metadata_json=? WHERE id=? AND path IN (?,?)',
                         (new_path, signature, json.dumps(metadata), media_id, old_path, new_path))
            conn.execute('UPDATE track_local_files SET path=? WHERE path=?', (new_path, old_path))
            conn.execute('DELETE FROM local_files WHERE path=?', (old_path,))
            for row in conn.execute("SELECT source,record_json FROM crates WHERE instr(record_json,?) > 0", (json.dumps(old_path, ensure_ascii=False)[1:-1],)).fetchall():
                raw = json.loads(row['record_json'])
                for track in raw.get('tracks', []):
                    if track.get('local_path') == old_path:
                        track['local_path'] = new_path
                self.save_crate(raw)


    @owned
    def remember_provider_playlist(self, provider_id, incoming, generation):
        with self.connection(write=True) as conn:
            row = conn.execute('SELECT source FROM playlist_aliases WHERE provider_id=?', (str(provider_id),)).fetchone()
            if row is not None:
                incoming = dict(incoming, source=row['source'])
            saved = self.remember_collection(incoming, generation)
            if saved is not None:
                conn.execute('INSERT OR REPLACE INTO playlist_aliases VALUES(?,?)', (str(provider_id), saved['source']))
            return saved

    @owned
    def media_at_identity(self, signature):
        info = json.loads(signature)
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM media_files WHERE json_extract(signature,'$[0]')=? AND json_extract(signature,'$[1]')=? LIMIT 2", info[:2])]

    @owned
    def relocate_media(self, media_id, old_path, new_path, old_signature, new_signature):
        with self.connection(write=True) as conn:
            changed = conn.execute('UPDATE media_files SET path=?,signature=? WHERE id=? AND path=? AND signature=?',
                                   (new_path, new_signature, media_id, old_path, old_signature)).rowcount
            if changed:
                conn.execute('UPDATE media_analysis SET signature=? WHERE media_id=? AND signature=?', (new_signature, media_id, old_signature))
                conn.execute('UPDATE track_local_files SET path=? WHERE path=?', (new_path, old_path))
            return bool(changed)

    @owned
    def observe_root(self, path, device, inode):
        with self.connection(write=True) as conn:
            old = conn.execute('SELECT device,inode FROM media_roots WHERE path=?', (path,)).fetchone()
            if old is None:
                conn.execute('INSERT INTO media_roots VALUES(?,?,?)', (path, device, inode))
                return True
            return (old['device'], old['inode']) == (device, inode)

    @owned
    def media_roots(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM media_roots')]

    @owned
    def mark_directory_missing(self, folder, seen, device):
        prefix = folder.rstrip('/\\') + str(Path('/').anchor or '/')
        # Paths are canonical absolute paths, supplied by the completed scan.
        with self.connection(write=True) as conn:
            rows = conn.execute('SELECT id,path,signature FROM media_files WHERE path >= ? AND path < ?', (prefix, prefix + chr(0x10ffff))).fetchall()
            missing = [(row['id'],) for row in rows if str(Path(row['path']).parent) == folder and row['path'] not in seen and json.loads(row['signature'])[0] == device]
            conn.executemany('UPDATE media_files SET available=0 WHERE id=?', missing)

    @owned
    def link_playlist_alias(self, provider_id, source, generations):
        with self.connection(write=True) as conn:
            if self.load_crate(source) is not None and generations.get(source, 'initial') == self.crate_generation(source):
                conn.execute('INSERT OR IGNORE INTO playlist_aliases VALUES(?,?)', (str(provider_id), source))

    @owned
    def unaliased_playlists(self):
        with self.connection() as conn:
            return [row['source'] for row in conn.execute('SELECT source FROM crates WHERE source NOT IN (SELECT source FROM playlist_aliases)')]
