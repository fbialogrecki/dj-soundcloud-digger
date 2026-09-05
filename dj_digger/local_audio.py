"""Bounded PCM playback; subprocess and pipe work stays off the audio callback."""
import array
import threading
from pathlib import Path

from .media import MediaError, pcm_blocks, probe
from .services.playback import Prepared, Stream

# Protect loaded and prefetched media, including the short replacement commit.
LEASE_LOCK = threading.RLock()
LEASES: dict[Path, int] = {}
SOURCES = set()


class LocalSource:
    def __init__(self, path: Path):
        self.path = path.resolve(strict=True)
        self._lock = threading.Condition()
        self._buffer = bytearray()
        self._cancel = threading.Event()
        self._generation = 0
        self._request = 0.0
        self._eof = False
        self._error = None
        self.last_frames = 0
        self._closed = False
        with LEASE_LOCK:
            LEASES[self.path] = LEASES.get(self.path, 0) + 1
            SOURCES.add(self)
        self._thread = threading.Thread(target=self._produce, name='local-audio-decoder')
        self._thread.start()

    def restart(self, seconds):
        with self._lock:
            self._cancel.set()
            self._generation += 1
            self._request = seconds
            self._buffer.clear()
            self._eof, self._error = False, None
            self._lock.notify_all()

    def _produce(self):
        try:
            while True:
                with self._lock:
                    while self._request is None and not self._closed:
                        self._lock.wait()
                    if self._closed:
                        return
                    seconds, generation = self._request, self._generation
                    self._request = None
                    cancel = self._cancel = threading.Event()
                try:
                    for chunk in pcm_blocks(self.path, rate=44100, channels=2, sample_format='s16le', seek=seconds, cancel=cancel):
                        with self._lock:
                            while len(self._buffer) + len(chunk) > 44100 * 4 * 2 and not cancel.is_set():
                                self._lock.wait(.1)
                            if cancel.is_set() or generation != self._generation:
                                break
                            self._buffer.extend(chunk)
                except Exception as exc:
                    with self._lock:
                        if generation == self._generation and not cancel.is_set():
                            self._error = str(exc)
                finally:
                    with self._lock:
                        if generation == self._generation:
                            self._eof = True
                        self._lock.notify_all()
        finally:
            with LEASE_LOCK:
                LEASES[self.path] -= 1
                if not LEASES[self.path]:
                    del LEASES[self.path]
                SOURCES.discard(self)

    def take(self, frames):
        with self._lock:
            count = min(frames * 4, len(self._buffer)) // 4 * 4
            chunk = bytes(self._buffer[:count])
            del self._buffer[:count]
            self.last_frames = count // 4
            self._lock.notify_all()
            if not count and self._eof:
                if self._error:
                    raise MediaError(self._error)
                return array.array('h')
            if not self._eof:
                chunk += bytes(frames * 4 - count)
        result = array.array('h')
        result.frombytes(chunk)
        import sys
        if sys.byteorder != 'little':
            result.byteswap()
        return result

    def stream(self, seek_frame):
        if seek_frame:
            self.restart(seek_frame / 44100)
        frames = 1024
        while True:
            frames = (yield self.take(frames)) or 1024

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        with self._lock:
            self._lock.notify_all()

    def join(self):
        self.close()
        self._thread.join()


def prepare_local(track):
    path = Path(track.local_path)
    with LEASE_LOCK:
        metadata = probe(path)
        source = LocalSource(path)
    return Prepared(track, Stream(str(path), duration=metadata['duration']), source=source)


def close_all():
    with LEASE_LOCK:
        sources = tuple(SOURCES)
    for source in sources:
        source.close()
    for source in sources:
        source.join()


def waveform(path, cancel=None):
    """Independent low-resolution envelope; bounded cache, never a playback gate."""
    import hashlib
    import json
    import os

    from .media import signature
    root = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'dj-digger' / 'waveforms'
    path = Path(path)
    before = signature(path)
    cache = root / (hashlib.sha256((str(path) + before).encode()).hexdigest() + '.json')
    try:
        values = json.loads(cache.read_text())
        if isinstance(values, list) and len(values) <= 1024:
            return values
    except (OSError, ValueError):
        pass
    metadata = probe(path, cancel)
    bins = [0] * 1024
    frames_per_bin = max(1, round(metadata['duration'] * 4000 / 1024))
    frame = 0
    for block in pcm_blocks(path, rate=4000, channels=2, sample_format='s16le', cancel=cancel):
        values = array.array('h')
        values.frombytes(block)
        import sys
        if sys.byteorder != 'little':
            values.byteswap()
        for index in range(0, len(values), 2):
            bucket = min(1023, frame // frames_per_bin)
            bins[bucket] = max(bins[bucket], abs(values[index]), abs(values[index + 1]))
            frame += 1
    if signature(path) != before:
        return []
    root.mkdir(parents=True, exist_ok=True)
    from .private_json import write_private_json
    write_private_json(cache, bins)
    files = sorted(root.glob('*.json'), key=lambda entry: entry.stat().st_mtime, reverse=True)
    for old in files[128:]:
        old.unlink(missing_ok=True)
    return bins
