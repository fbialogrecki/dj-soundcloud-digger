"""Previewing a track before you buy it.

SoundCloud offers a ``progressive`` transcoding next to HLS, which is a plain
MP3 behind a signed URL. Nothing is downloaded to disk: the MP3 is decoded
straight off the socket through miniaudio's ``stream_any``, so audio starts after
about 0.5 s instead of waiting out a 6.6 MB download.

A copy is kept in memory as it goes, though. Without one, seeking ten seconds
back into audio that just played meant a fresh connection and half a second of
silence, and nothing about the next track was known until the current one ended.
The copy fixes both: the decoder reads from a bytearray, and a seek is a move of
an index.

``just-playback`` was the first choice but has no wheel for Python 3.14 and fails
to build without system headers, so this drives miniaudio directly.

Everything degrades: a machine with no audio sink refuses to open a device, and
that must never take the app down.
"""

import array
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from queue import Empty, SimpleQueue
from typing import Literal

from .models import Track
from .services.playback import Stream

LOGGER = logging.getLogger(__name__)

SEEK_STEP = 10.0
VOLUME_STEP = 0.1
SAMPLE_RATE = 44100
CHANNELS = 2
# int16, so this is the loudest a sample can be.
FULL_SCALE = 32768.0
# One reading per frame of the interface. A callback hands over about a tenth of
# a second at a time, and the loudest sample in a tenth of a second of techno is
# a kick every single time - so a reading per callback is a meter that sits still.
LEVEL_WINDOW = SAMPLE_RATE * CHANNELS // 30
# A quarter of a second of readings. Past that the meter would be showing the
# past rather than falling behind gracefully, so the oldest go.
LEVEL_QUEUE = 8

DOWNLOAD_CHUNK = 64 * 1024
# A two hour set is not a track, and a response that will not declare its size
# could be anything. Both stream off the socket the way everything used to.
MAX_BUFFER_BYTES = 50 * 1024 * 1024
SOURCE_TIMEOUT = 30.0
class PlaybackUnavailable(RuntimeError):
    """No audio output, or miniaudio is missing."""


def _import_miniaudio():
    try:
        import miniaudio
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise PlaybackUnavailable(
            "Audio preview needs miniaudio: pip install 'dj-digger[play]'"
        ) from exc
    return miniaudio


class HttpSourceMixin:
    """Feeds miniaudio from an HTTP response, keeping a copy of it as it goes.

    A thread pulls the response into memory alongside playback, so the decoder
    reads out of a bytearray instead of a socket. Playback still starts on the
    first bytes to land - nothing waits for the download to finish - but by a few
    seconds in the whole track is here, and seeking into it costs nothing.

    A response too large to hold, or one that will not say how large it is, takes
    the unbuffered path instead: reads go straight to the socket and a seek
    reopens it with a Range header, which is what everything used to do.

    The logic lives in a mixin because miniaudio's ``StreamableSource`` base is
    only importable when miniaudio is, and the digger has to work without it.
    """

    def __init__(
        self,
        session,
        url: str,
        max_buffer: int = MAX_BUFFER_BYTES,
    ) -> None:
        self.session = session
        self.url = url
        self.timeout = SOURCE_TIMEOUT
        self.offset = 0
        self.length: int | None = None
        self._response = None
        self._buffer = bytearray()
        # Where the buffer starts in the file. It only moves when a seek lands
        # somewhere we neither hold nor are on our way to.
        self._base = 0
        self._buffering = False
        self._done = False
        self._failed = False
        self._closed = False
        # Bumped on every restart, so a thread left over from the previous one
        # knows the bytes it is holding are no longer wanted.
        self._generation = 0
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self._open(0)
        if self.length and self.length <= max_buffer:
            self._buffering = True
            self._spawn()

    # Connection

    def _open(self, offset: int) -> None:
        self._close_response()
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        self._response = self.session.get(
            self.url, headers=headers, stream=True, timeout=self.timeout
        )
        if self.length is None:
            declared = self._response.headers.get("Content-Length")
            self.length = int(declared) + offset if declared else None
        self.offset = offset

    def _close_response(self) -> None:
        if self._response is not None:
            self._response.close()
            self._response = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1
            self._arrived.notify_all()
        self._close_response()

    # Buffering

    def _spawn(self) -> None:
        thread = threading.Thread(
            target=self._download,
            args=(self._response, self._generation),
            daemon=True,
        )
        thread.start()

    def _download(self, response, generation: int) -> None:
        """Pull the rest of the response into the buffer, off the audio thread."""

        while True:
            try:
                chunk = response.raw.read(DOWNLOAD_CHUNK)
            except Exception as exc:
                # Half a track in memory is still better than none: reads inside
                # it stay instant, and anything past it goes back to the socket.
                LOGGER.debug("Buffering %s stopped early: %s", self.url, exc)
                chunk = None
            with self._lock:
                if generation != self._generation or self._closed:
                    return
                if chunk:
                    self._buffer.extend(chunk)
                else:
                    self._done = True
                    self._failed = chunk is None
                self._arrived.notify_all()
                if not chunk:
                    return

    def _outside_buffer(self) -> bool:
        """Is the read head somewhere this buffer does not and will not hold?"""

        with self._lock:
            if self.offset < self._base:
                return True
            complete = self._done and not self._failed
            return self.offset > self._base + len(self._buffer) and not complete

    def _restart(self, target: int) -> None:
        """Point the buffer at a new part of the file, dropping what it held."""

        with self._lock:
            self._generation += 1
            self._buffer = bytearray()
            self._base = target
            self._done = False
            self._failed = False
        try:
            self._open(target)
        except Exception as exc:
            LOGGER.debug("Could not reopen %s at %d: %s", self.url, target, exc)
            with self._lock:
                self._done = True
                self._failed = True
                self._arrived.notify_all()
            return
        self._spawn()

    # Reading

    def read(self, num_bytes: int) -> bytes:
        if num_bytes <= 0:
            return b""
        if self._buffering:
            return self._read_buffered(num_bytes)
        return self._read_direct(num_bytes)

    def _read_buffered(self, num_bytes: int) -> bytes:
        if self._outside_buffer():
            self._restart(self.offset)
        data = self._take(num_bytes)
        if len(data) < num_bytes and self._died_early():
            # The download dropped with file still to come, so go back to the
            # socket for the rest rather than reporting the track as over.
            self._restart(self.offset)
            data += self._take(num_bytes - len(data))
        return data

    def _take(self, num_bytes: int) -> bytes:
        """Bytes from the buffer, waiting for the download only if it is behind."""

        deadline = time.monotonic() + self.timeout
        with self._lock:
            while True:
                start = self.offset - self._base
                end = start + num_bytes
                if end <= len(self._buffer) or self._done or self._closed:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._arrived.wait(remaining):
                    break
            data = bytes(self._buffer[start:end])
        self.offset += len(data)
        return data

    def _died_early(self) -> bool:
        with self._lock:
            if self._closed or not self._failed:
                return False
        return self.length is not None and self.offset < self.length

    def _read_direct(self, num_bytes: int) -> bytes:
        # raw.read can come up short on a socket; the decoder reads a short
        # answer as the end of the file, so keep pulling until it is satisfied.
        parts = []
        remaining = num_bytes
        while remaining > 0:
            try:
                chunk = self._response.raw.read(remaining)
            except Exception as exc:
                LOGGER.debug("Stream read failed: %s", exc)
                break
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)
        data = b"".join(parts)
        self.offset += len(data)
        return data

    # Seeking

    def seek(self, offset: int, origin) -> bool:
        target = max(0, self._target(offset, origin))
        if self._buffering:
            # Nothing else to do: the read that follows either finds the bytes
            # here, waits the moment out, or sends us back for them.
            self.offset = target
            return True
        try:
            self._open(target)
        except Exception as exc:
            LOGGER.debug("Range seek failed: %s", exc)
            return False
        return True

    def _target(self, offset: int, origin) -> int:
        if getattr(origin, "value", origin) == 1:  # SeekOrigin.CURRENT
            return self.offset + offset
        if getattr(origin, "value", origin) == 2 and self.length:  # END
            return self.length + offset
        return offset


def open_source(session, url: str):
    """Start pulling a track into memory before anything has asked to hear it."""

    return http_source_type(_import_miniaudio())(session, url)


@lru_cache(maxsize=None)
def http_source_type(miniaudio):
    """The mixin welded onto miniaudio's StreamableSource, built once."""

    return type("HttpSource", (HttpSourceMixin, miniaudio.StreamableSource), {})


@dataclass
class Loaded:
    track: Track
    stream: Stream
    waveform: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.stream.duration


@dataclass(frozen=True)
class PlaybackEvent:
    """One terminal event from the audio callback, tagged against stale playback."""

    kind: Literal["finished", "error"]
    generation: int
    message: str = ""


class Player:
    """Play, pause, seek and volume over an MP3 streamed straight from SoundCloud."""

    def __init__(self) -> None:
        self._miniaudio = None
        self._device = None
        self._loaded: Loaded | None = None
        self._session = None
        self._source = None
        self._generator = None
        self._frames = 0
        self._offset = 0.0
        self._playing = False
        self._ended = False
        self._generation = 0
        self._events: SimpleQueue[PlaybackEvent] = SimpleQueue()
        self._volume = 0.8
        self._muted = False
        self._level = 0.0
        # Written on the audio thread and read on the interface's, which a deque
        # is safe for on its own - appends and pops are single bytecodes.
        self._levels: deque[float] = deque(maxlen=LEVEL_QUEUE)
        self.unavailable_reason: str | None = None

    def _device_for(self, sample_rate: int, channels: int):
        if self.unavailable_reason:
            # Already established there is no output; stop hammering the backend.
            raise PlaybackUnavailable(self.unavailable_reason)
        miniaudio = self._miniaudio or _import_miniaudio()
        self._miniaudio = miniaudio
        if self._device is not None:
            return self._device
        try:
            self._device = miniaudio.PlaybackDevice(
                sample_rate=sample_rate, nchannels=channels
            )
        except Exception as exc:
            # The raw miniaudio error is a numbered tuple, no use to anyone here.
            LOGGER.debug("Could not open an audio device: %s", exc)
            self.unavailable_reason = "No audio output on this machine or session"
            raise PlaybackUnavailable(self.unavailable_reason) from exc
        return self._device

    # State

    @property
    def loaded(self) -> Loaded | None:
        return self._loaded

    @property
    def playing(self) -> bool:
        return self._playing

    def take_event(self) -> PlaybackEvent | None:
        """Return the next terminal event for the current playback generation."""

        while True:
            try:
                event = self._events.get_nowait()
            except Empty:
                return None
            if event.generation == self._generation:
                return event

    @property
    def duration(self) -> float:
        return self._loaded.duration if self._loaded else 0.0

    @property
    def position(self) -> float:
        if self._loaded is None:
            return 0.0
        return min(self.duration, self._offset + self._frames / SAMPLE_RATE)

    @property
    def fraction(self) -> float:
        return self.position / self.duration if self.duration else 0.0

    @property
    def volume(self) -> float:
        return 0.0 if self._muted else self._volume

    def _silence(self) -> None:
        """Nothing is going out, so nothing measured before it still applies."""

        self._level = 0.0
        self._levels.clear()

    def take_level(self) -> float:
        """The next reading of how loud the audio going out is, 0 to 1.

        Read off the samples on their way to the device, which is the only place
        the actual sound exists - the waveform picture is an average of the whole
        track and says nothing about this instant.

        Oldest first, one per call, because the readings are made faster than
        anything asks for them. When they run out the last one stands, which is
        better than dropping to silence between callbacks.
        """

        if self._levels:
            self._level = self._levels.popleft()
        elif not self._playing:
            self._level = 0.0
        return self._level

    # Controls

    def load(
        self,
        track: Track,
        stream: Stream,
        session,
        waveform: list[int] | None = None,
        source=None,
    ) -> Loaded:
        """``source`` is a stream someone opened ahead of time, already filling."""

        self._miniaudio = self._miniaudio or _import_miniaudio()
        self.stop()
        self._session = session
        self._source = source
        self._loaded = Loaded(track=track, stream=stream, waveform=waveform or [])
        self._frames = 0
        self._offset = 0.0
        return self._loaded

    def _open_stream(self, seek_frame: int):
        miniaudio = self._miniaudio
        self._drop_generator()
        # The source outlives a seek. Replacing it is what used to throw away
        # the buffered track and put a connection in front of every seek.
        if self._source is None:
            self._source = open_source(self._session, self._loaded.stream.url)
        if hasattr(self._source, "stream"):
            return self._source.stream(seek_frame)
        return miniaudio.stream_any(
            self._source,
            source_format=miniaudio.FileFormat.MP3,
            sample_rate=SAMPLE_RATE,
            nchannels=CHANNELS,
            seek_frame=seek_frame,
        )

    def _drop_generator(self) -> None:
        if self._generator is not None:
            self._generator.close()
            self._generator = None

    def _close_source(self) -> None:
        self._drop_generator()
        if self._source is not None:
            self._source.close()
            self._source = None

    def _measure(self, chunk) -> None:
        """Note how loud each frame's worth of this chunk is.

        Runs on the audio callback thread, so it is two calls into C per slice
        and nothing else. Taken before the volume scaling, because it is the
        music that should show and not the fader.
        """

        for start in range(0, len(chunk), LEVEL_WINDOW):
            window = chunk[start : start + LEVEL_WINDOW]
            if len(window):
                self._levels.append(max(max(window), -min(window)) / FULL_SCALE)

    def _feed(self, stream, generation: int):
        # miniaudio sends a frame count into the callback generator, so the first
        # yield must happen before any decoding. It also makes an empty stream end
        # on the callback thread rather than raising while ``play`` primes us.
        required = yield b""
        first = True
        while True:
            if generation != self._generation:
                return
            try:
                # miniaudio can send 0; asking the decoder for nothing reads as EOF.
                frames = required or 1024
                chunk = next(stream) if first else stream.send(frames)
                first = False
                if not len(chunk):
                    raise StopIteration
                self._frames += (self._source.last_frames if hasattr(self._source, "last_frames") else len(chunk) // CHANNELS)
                self._measure(chunk)
                volume = self.volume
                out = (
                    chunk
                    # >= 0.999 rather than == 1.0: a float comparison guard, and at
                    # full volume the per-sample rescale loop is skipped entirely.
                    if volume >= 0.999
                    else array.array("h", [int(sample * volume) for sample in chunk])
                )
            except StopIteration:
                if generation == self._generation:
                    self._playing = False
                    self._ended = True
                    self._generator = None
                    self._silence()
                    self._events.put(PlaybackEvent("finished", generation))
                return
            except Exception as exc:
                if generation == self._generation:
                    self._playing = False
                    self._generator = None
                    self._silence()
                    self._events.put(PlaybackEvent("error", generation, str(exc)))
                return
            required = yield out

    def _stop_device(self) -> None:
        """Stop the output, and let go of a device that will not stop."""

        if self._device is None:
            return
        try:
            self._device.stop()
        except Exception as exc:
            LOGGER.debug("Stopping the audio device complained: %s", exc)
            self._drop_device()

    def _drop_device(self) -> None:
        """Let go of a device that has misbehaved, so the next play rebuilds it.

        Not ``unavailable_reason``: that is for a machine with no output at all,
        and stands until the app is restarted. A device that fails once after
        working deserves another try.
        """

        if self._device is not None:
            try:
                self._device.close()
            except Exception as exc:
                LOGGER.debug("Closing a failed audio device complained: %s", exc)
        self._device = None
        self._playing = False
        self._silence()

    def play(self) -> None:
        if self._loaded is None:
            return
        device = self._device_for(SAMPLE_RATE, CHANNELS)
        if self._ended:
            # At the end of the list, pressing play means replay this track rather
            # than asking an exhausted decoder to seek to its own end again.
            self._drop_generator()
            self._offset = 0.0
            self._frames = 0
            self._ended = False
            if hasattr(self._source, "restart"):
                self._source.restart(0)
        if self._generator is None:
            # Reopening the socket costs about half a second, so a plain resume
            # keeps the existing generator and only a seek reopens it.
            self._offset = self.position
            self._frames = 0
            self._generation += 1
            self._generator = self._feed(
                self._open_stream(int(self._offset * SAMPLE_RATE)), self._generation
            )
            # miniaudio sends into the generator without priming it first, and
            # its own docstring says the caller must start it.
            next(self._generator)
        # A very short or broken stream can finish before ``start`` returns, so
        # publish the intended state first and let the callback have the last word.
        self._playing = True
        self._ended = False
        try:
            device.start(self._generator)
        except Exception as exc:
            # miniaudio answers with a numbered failure nobody can act on, and a
            # device that has just been stopped is enough to produce one -
            # pressing play twice in quick succession did it. Raised as the
            # degraded state the app already knows how to show, rather than out
            # through the message pump, where it took the whole TUI with it.
            LOGGER.debug("Could not start the audio device: %s", exc)
            self._drop_device()
            raise PlaybackUnavailable("The audio device would not start - try again") from exc

    def pause(self) -> None:
        if self._playing:
            self._stop_device()
        self._playing = False
        self._silence()

    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    def stop(self) -> None:
        # Invalidate a callback before asking the device to stop. A late EOF from
        # the old generator must not advance whatever is loaded next.
        self._generation += 1
        self._stop_device()
        self._close_source()
        self._playing = False
        self._ended = False
        self._frames = 0
        self._offset = 0.0
        self._silence()

    def seek(self, seconds: float) -> None:
        if self._loaded is None:
            return
        # Half a second short of the end: seeking to the exact end delivers an
        # immediate end-of-stream and the track reads as finished.
        target = max(0.0, min(max(0.0, self.duration - 0.5), seconds))
        was_playing = self._playing
        self._generation += 1
        self._stop_device()
        # Only the decoder is rebuilt at the new frame. The source stays, and
        # with it the copy of the track, which is what makes this instant.
        self._drop_generator()
        self._offset = target
        self._frames = 0
        self._playing = False
        self._ended = False
        self._silence()
        if was_playing:
            self.play()

    def nudge(self, seconds: float) -> None:
        self.seek(self.position + seconds)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        self._muted = False

    def change_volume(self, delta: float) -> None:
        self.set_volume(self._volume + delta)

    def toggle_mute(self) -> None:
        self._muted = not self._muted

    def unload(self) -> None:
        """Stop and forget the track, so the bar has nothing left to say.

        ``stop`` on its own rewinds and keeps the track loaded, which is what
        the end of a track wants; closing the player wants it gone.
        """

        self.stop()
        self._loaded = None

    def close(self) -> None:
        try:
            self.stop()
            if self._device is not None:
                self._device.close()
        except Exception as exc:
            LOGGER.debug("Closing the audio device complained: %s", exc)
        self._device = None
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                LOGGER.debug("Closing the playback session complained: %s", exc)


