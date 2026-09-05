"""Recognize the shipped schema and register it without rewriting user data."""

import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

DDL = (
    'CREATE TABLE track_states (key TEXT PRIMARY KEY, status TEXT NOT NULL, updated TEXT NOT NULL)',
    'CREATE TABLE local_files (path TEXT PRIMARY KEY, mtime REAL NOT NULL, normalized_stem TEXT NOT NULL)',
    'CREATE TABLE track_local_files (key TEXT PRIMARY KEY, path TEXT NOT NULL)',
    'CREATE INDEX idx_local_normalized ON local_files(normalized_stem)',
    'CREATE TABLE crates (source TEXT PRIMARY KEY, title TEXT NOT NULL, updated TEXT NOT NULL, record_json TEXT NOT NULL)',
)
MEDIA_DDL = (
    "CREATE TABLE media_roots (path TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL)",
    "CREATE TABLE media_files (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, signature TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', parent_id TEXT, available INTEGER NOT NULL DEFAULT 1)",
    "CREATE INDEX media_identity ON media_files(json_extract(signature, '$[0]'), json_extract(signature, '$[1]'))",
    "CREATE TABLE media_analysis (media_id TEXT PRIMARY KEY REFERENCES media_files(id), signature TEXT NOT NULL DEFAULT '', algorithm TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}', manual_json TEXT NOT NULL DEFAULT '{}')",
    "CREATE TABLE local_playlist_items (source TEXT NOT NULL REFERENCES crates(source) ON DELETE CASCADE, position INTEGER NOT NULL, media_id TEXT NOT NULL REFERENCES media_files(id), PRIMARY KEY (source, position))",
    "CREATE TABLE playlist_aliases (provider_id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES crates(source) ON DELETE CASCADE)",
    "CREATE TABLE media_operations (id TEXT PRIMARY KEY, record_json TEXT NOT NULL)",
)
BACKUP_TIMEOUT = 30.0


class UnsupportedSchema(RuntimeError):
    """Storage cannot be opened safely by this version of the application."""


def signature(conn: sqlite3.Connection) -> tuple:
    # Compare the actual SQL too: extra CHECKs, triggers or collations are not
    # the schema we know even when table_info reports the same column names.
    return tuple(
        (kind, name, ''.join(sql.lower().split()).replace('ifnotexists', ''))
        for kind, name, sql in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        )
    )


def expected_signature(version=1) -> tuple:
    with closing(sqlite3.connect(':memory:')) as conn:
        for statement in DDL + (MEDIA_DDL if version == 2 else ()):
            conn.execute(statement)
        return signature(conn)


def recognize(conn: sqlite3.Connection) -> tuple:
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    shape = signature(conn)
    if version not in (0, 1, 2) or shape != expected_signature(2 if version == 2 else 1):
        raise UnsupportedSchema(f'Unsupported library schema (version {version}); database left unchanged')
    return version, shape


def backup(conn: sqlite3.Connection, path: Path) -> Path:
    directory = path.parent / 'backups'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name+'-', suffix='.tmp', dir=directory)
    os.close(fd)  # mkstemp creates mode 0600 before SQLite writes any data.
    temporary = Path(name)
    deadline = time.monotonic() + BACKUP_TIMEOUT

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError('Library backup timed out; schema version unchanged')

    try:
        with closing(sqlite3.connect(temporary, timeout=BACKUP_TIMEOUT)) as destination:
            conn.backup(destination, pages=128, progress=progress, sleep=0.05)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise UnsupportedSchema('Library backup failed integrity check')
        with temporary.open('r+b') as output:
            os.fsync(output.fileno())
        final = temporary.with_suffix('.db')
        os.replace(temporary, final)
        from .media import fsync_directory
        fsync_directory(directory)
        return final
    finally:
        temporary.unlink(missing_ok=True)


def open_database(path: Path) -> sqlite3.Connection:
    observed = None
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as reader:
            observed = recognize(reader)
    if observed is None:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        # Reserve the writer before backing up via a separate committed reader.
        # The backup API on the active writer itself can wait forever.
        conn.execute('BEGIN IMMEDIATE')
        if observed is None:
            if signature(conn) or conn.execute('PRAGMA user_version').fetchone()[0]:
                raise UnsupportedSchema('Library changed while opening it')
            for statement in DDL + MEDIA_DDL:
                conn.execute(statement)
        else:
            current = recognize(conn)
            if current != observed:
                raise UnsupportedSchema('Library schema changed while opening it')
            if current[0] < 2:
                with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as reader:
                    backup(reader, path)
                for statement in MEDIA_DDL:
                    conn.execute(statement)
        conn.execute('PRAGMA user_version=2')
        conn.commit()
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.row_factory = sqlite3.Row
        return conn
    except BaseException:
        conn.rollback()
        conn.close()
        raise
