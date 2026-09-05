import array
import threading
import time

from dj_digger.local_audio import LEASES, LocalSource


def test_underrun_is_silence_not_eof_and_seek_discards_buffer(tmp_path, monkeypatch):
    from dj_digger import local_audio
    path = tmp_path / 'test.wav'
    path.touch()
    release = threading.Event()
    calls = []

    def blocks(path, *, seek, cancel, **kwargs):
        calls.append(seek)
        while not release.wait(.01):
            if cancel.is_set():
                return
        if not cancel.is_set():
            yield array.array('h', [1000 if seek == 0 else 2000] * 8192).tobytes()

    monkeypatch.setattr(local_audio, 'pcm_blocks', blocks)
    source = LocalSource(path)
    try:
        assert list(source.take(10)) == [0] * 20
        assert source.last_frames == 0
        for index in range(100):
            source.restart(index + 1)
        assert sum(thread.name == 'local-audio-decoder' for thread in threading.enumerate()) == 1
        release.set()
        deadline = time.monotonic() + 3
        while source.last_frames == 0 and time.monotonic() < deadline:
            chunk = source.take(10)
            time.sleep(.01)
        assert max(chunk) == 2000
        assert path in LEASES
    finally:
        source.join()
    assert path not in LEASES
