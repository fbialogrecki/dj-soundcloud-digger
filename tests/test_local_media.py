import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from dj_digger.db import Database
from dj_digger.decks import Profile, compatibility
from dj_digger.export import execute, plan_export, prepare, recover, replace_one
from dj_digger.instance import InstanceLock
from dj_digger.media import probe, signature
from dj_digger.models import Track, track_key
from dj_digger.schema import DDL
from dj_digger.services.local_library import LocalLibrary, media_track
from dj_digger.wav import inspect


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'library.db')
    yield database
    database.close()


def audio(tmp_path, name='source.wav', codec='pcm_s24le', rate=96000):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg is not installed')
    path = tmp_path / name
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.2', '-ar', str(rate), '-ac', '2', '-c:a', codec, str(path)], check=True)
    return path


def test_identity_and_central_manual_values(tmp_path, db):
    path = audio(tmp_path)
    other = audio(tmp_path, 'other.wav')
    library = LocalLibrary(db)
    a, b = library.register(path), library.register(other)
    assert a.key != b.key
    assert a.key == track_key({'local_id': a.local_id})
    assert Track('remote', 'url', id=3).key == '3'
    for source in ('local-playlist:a', 'local-playlist:b'):
        db.save_local_playlist(source, source, [a.local_id])
    db.save_local_playlist('local-playlist:a', 'renamed', [b.local_id])
    assert len(db.local_playlist_media('local-playlist:a')) == 2
    db.set_media_manual(a.local_id, {'bpm': 128})
    db.save_analysis(a.local_id, signature(path), 'test', {'bpm': 64})
    assert media_track(db, db.media(a.local_id)).bpm == 128
    assert not db.save_analysis(a.local_id, 'stale', 'test', {'bpm': 180})


@pytest.mark.parametrize('version', [0, 1])
def test_v2_migration_backs_up_committed_wal(tmp_path, version):
    path = tmp_path / 'library.db'
    with sqlite3.connect(path) as connection:
        for statement in DDL:
            connection.execute(statement)
        connection.execute(f'PRAGMA user_version={version}')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute("INSERT INTO track_states VALUES ('x', 'got', '')")
        connection.commit()
        database = Database(path)
        assert database.all_track_statuses() == {'x': 'got'}
        database.close()
    assert list((tmp_path / 'backups').iterdir())


def test_lock_rejects_second_instance(tmp_path):
    first = InstanceLock(tmp_path / 'lock')
    try:
        with pytest.raises(RuntimeError):
            InstanceLock(tmp_path / 'lock')
    finally:
        first.close()
    InstanceLock(tmp_path / 'lock').close()


def test_mixed_export_keeps_mp3_bytes_and_canonicalises_wav(tmp_path, db):
    wav = audio(tmp_path)
    mp3 = audio(tmp_path, 'lossy.mp3', 'libmp3lame', 44100)
    flac = audio(tmp_path, 'source.flac', 'flac')
    plan = plan_export([wav, mp3, flac], tmp_path / 'exports')
    assert [item.action for item in plan.items] == ['convert', 'copy', 'convert']
    report = execute(plan, db)
    assert report['status'] == 'complete', report
    assert Path(plan.items[1].destination).read_bytes() == mp3.read_bytes()
    for item in (plan.items[0], plan.items[2]):
        info = inspect(Path(item.destination))
        assert (info['code'], info['bits'], info['rate']) == (1, item.bits, 48000)
    assert all(value == 'compatible' for value in report['compatibility'].values())
    assert wav.exists() and flac.exists()


@pytest.mark.parametrize('stage', ['prepared', 'original_saved', 'installed', 'committed', 'done'])
def test_replacement_recovers_every_commit_boundary(tmp_path, db, stage):
    path = audio(tmp_path)
    record = db.register_media(str(path), signature(path))
    plan = plan_export([path], tmp_path, mode='replace')
    item = plan.items[0]
    result = tmp_path / 'prepared.wav'
    prepare(item, Profile(), result)

    def failpoint(current):
        if current == stage:
            raise RuntimeError('power failure')

    with pytest.raises(RuntimeError):
        replace_one(db, record['id'], item, result, failpoint=failpoint)
    assert recover(db) == []
    assert path.is_file()
    assert not list(tmp_path.glob('*.original'))
    assert probe(path)['rate'] in (48000, 96000)
    assert db.media(record['id']) is not None


def test_changed_source_refused_and_lossy_32khz_shows_actual_compatibility(tmp_path, db):
    path = audio(tmp_path)
    plan = plan_export([path], tmp_path / 'out')
    path.write_bytes(path.read_bytes() + b'changed')
    assert execute(plan, db)['status'] == 'partial'
    mp3 = audio(tmp_path, '32.mp3', 'libmp3lame', 32000)
    item = plan_export([mp3], tmp_path / 'out2').items[0]
    assert item.action == 'copy'
    states = compatibility([probe(mp3)])
    assert states['CDJ-3000'] == 'incompatible'


def test_symbolic_link_inplace_refused(tmp_path):
    path = audio(tmp_path)
    link = tmp_path / 'link.wav'
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip('symlinks unavailable')
    assert plan_export([link], tmp_path, mode='replace').items[0].action == 'exception'


def test_unavailable_folder_retains_records(tmp_path, db):
    path = audio(tmp_path)
    local = LocalLibrary(db)
    track = local.register(path)
    with pytest.raises(FileNotFoundError):
        local.page(tmp_path / 'unmounted')
    assert db.media(track.local_id)['available'] == 1


def test_same_file_rename_preserves_uuid_and_manual_values(tmp_path, db):
    path = audio(tmp_path)
    library = LocalLibrary(db)
    first = library.register(path)
    db.set_media_manual(first.local_id, {'bpm': 125})
    renamed = tmp_path / 'renamed.wav'
    path.rename(renamed)
    after = library.register(renamed)
    assert after.local_id == first.local_id
    assert after.bpm == 125


def test_copy_resume_verifies_finished_files(tmp_path, db):
    from threading import Event
    first = audio(tmp_path)
    second = audio(tmp_path, 'second.wav')
    plan = plan_export([first, second], tmp_path / 'out')
    stopped = Event()
    report = execute(plan, db, cancel=stopped, progress=lambda done, total: stopped.set())
    assert report['status'] == 'cancelled'
    assert len(report['missing']) == 1
    output = Path(plan.items[0].destination)
    before = output.read_bytes()
    assert execute(plan, db, resume=True)['status'] == 'complete'
    assert output.read_bytes() == before
    assert len(list(Path(plan.folder).glob('*.wav'))) == 2


def test_exclusive_install_never_overwrites_foreign_file(tmp_path):
    from dj_digger.media import install_new
    source, target = tmp_path / 'new', tmp_path / 'foreign'
    source.write_bytes(b'new')
    target.write_bytes(b'owned by someone else')
    with pytest.raises(FileExistsError):
        install_new(source, target)
    assert target.read_bytes() == b'owned by someone else'
    assert source.read_bytes() == b'new'


def test_missing_media_identity_preserves_recovery_original(tmp_path, db):
    path = audio(tmp_path)
    item = plan_export([path], tmp_path, mode='replace').items[0]
    output = tmp_path / 'verified.wav'
    prepare(item, Profile(), output)
    with pytest.raises(ValueError, match='identity'):
        replace_one(db, 'missing-record', item, output)
    assert list(tmp_path.glob('*.original'))
    assert recover(db)
    assert list(tmp_path.glob('*.original'))


@pytest.mark.parametrize('version', [0, 1])
def test_backup_failure_leaves_schema_untouched(tmp_path, monkeypatch, version):
    from dj_digger import schema
    path = tmp_path / 'v1.db'
    with sqlite3.connect(path) as connection:
        for statement in DDL:
            connection.execute(statement)
        connection.execute(f'PRAGMA user_version={version}')
    monkeypatch.setattr(schema, 'backup', lambda *args: (_ for _ in ()).throw(OSError('backup denied')))
    with pytest.raises(OSError, match='backup denied'):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == version
        assert schema.signature(connection) == schema.expected_signature()


def test_analysis_abstention_keeps_tag_fallback(tmp_path, db):
    path = audio(tmp_path)
    track = LocalLibrary(db).register(path)
    db.update_media_metadata(track.local_id, signature(path), {'tags': {'bpm': '126', 'initialkey': 'Am'}})
    db.save_analysis(track.local_id, signature(path), 'test', {'bpm': None, 'key': ''})
    loaded = media_track(db, db.media(track.local_id))
    assert (loaded.bpm, loaded.key_signature) == (126, 'Am')
    db.set_media_manual(track.local_id, {'bpm': 130, 'key': 'C'})
    loaded = media_track(db, db.media(track.local_id))
    assert (loaded.bpm, loaded.key_signature) == (130, 'C')


def test_rename_does_not_reuse_identity_from_changed_mount(tmp_path, db):
    path = audio(tmp_path)
    library = LocalLibrary(db)
    before = library.register(path)
    root_stat = tmp_path.stat()
    db.observe_root(str(tmp_path), root_stat.st_dev, root_stat.st_ino + 1)
    renamed = tmp_path / 'renamed.wav'
    path.rename(renamed)
    after = library.register(renamed)
    assert after.local_id != before.local_id
    assert db.media(before.local_id)['path'] == str(path)
