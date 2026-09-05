"""Previewing tracks: the frame ticker, prefetching the next one, and the transport keys.

Composed by ``DiggerApp`` with explicit state and presentation callbacks.
"""

import logging
from copy import deepcopy
from functools import partial

from textual.widgets import DataTable

from dj_digger.services.playback import Prepared, Stream, fetch_waveform, resolve_stream

from ..models import Track
from ..player import SEEK_STEP, VOLUME_STEP, PlaybackUnavailable, open_source
from ..soundcloud_errors import SoundCloudError
from .audio import PlayerBar
from .keymap import (
    CALM_TICK,
    PREFETCH_LEAD,
    SPINNER_EVERY,
    TICK,
)

LOGGER = logging.getLogger(__name__)


class PlaybackController:
    """Previewing tracks: the frame ticker, prefetching the next one, and the transport keys."""

    def __init__(
        self,
        *,
        _paint_key,
        _playing_key,
        _update_track_progress,
        get_animation_level,
        audio_state,
        call_from_thread,
        get_client,
        current_row,
        download_state,
        get_job,
        notify,
        get_player,
        playlist_state,
        query,
        query_one,
        run_worker,
        update_status,
        worker_scope,
    ):
        self._paint_key = _paint_key
        self._playing_key = _playing_key
        self._update_track_progress = _update_track_progress
        self.get_animation_level = get_animation_level
        self.audio_state = audio_state
        self.call_from_thread = call_from_thread
        self.get_client = get_client
        self.current_row = current_row
        self.download_state = download_state
        self.get_job = get_job
        self.notify = notify
        self.get_player = get_player
        self.playlist_state = playlist_state
        self.query = query
        self.query_one = query_one
        self.run_worker = run_worker
        self.update_status = update_status
        self.worker_scope = worker_scope
        self._playing_row = None
        self._requested_row = None

    @property
    def animation_level(self):
        return self.get_animation_level()

    @property
    def client(self):
        return self.get_client()

    @property
    def job(self):
        return self.get_job()

    @property
    def player(self):
        return self.get_player()

    def _player_bar(self) -> PlayerBar:
        return self.query_one("#player", PlayerBar)

    def _tick(self) -> None:
        # The timer belongs to the app and the bar to the screen, so on the way
        # out a tick can arrive after the bar has already gone.
        if not self.query("#player"):
            return
        with self.download_state._progress_lock:
            pending, self.download_state._pending_progress = self.download_state._pending_progress, {}
        for key, (operation_id, pct) in pending.items():
            self._update_track_progress(key, pct, operation_id)
        self.audio_state._frame += 1
        animating = self.job is not None and self.job.animate
        if animating and self.audio_state._frame % SPINNER_EVERY == 0:
            self.update_status()
        if event := self.player.take_event():
            if event.kind == "error":
                self._player_op(self.player.stop)
                self._playback_failed(f"Playback failed ({event.message})")
            else:
                # Auditioning a crate means hearing all of it, not pressing a key
                # between every track.
                self._advance_playback()
            return
        if self.player.playing:
            self._player_bar().refresh_bar()
            self._prepare_next()
        elif not animating:
            self._sleep()

    @property
    def frame_interval(self) -> float:
        return TICK if self.animation_level == "full" else CALM_TICK

    def _wake(self) -> None:
        if self.audio_state._ticker is not None:
            self.audio_state._ticker.resume()

    def _sleep(self) -> None:
        if self.audio_state._ticker is not None:
            self.audio_state._ticker.pause()

    def _prepare_next(self) -> None:
        """Get the next track ready while this one plays it out.

        Everything a track needs - a signed URL, a waveform, the audio itself -
        used to be fetched after the previous one ended, which put a second of
        "Loading" between every pair of tracks in the crate.
        """

        duration = self.player.duration
        if not duration or duration - self.player.position > PREFETCH_LEAD:
            return
        index = self._step_from_playing(1)
        if index is None:
            return
        track = self.playlist_state.visible_rows[index].track
        if (not track.id and not track.local_id) or self.audio_state._preparing == track.key:
            return
        if self.audio_state._prepared is not None and self.audio_state._prepared.key == track.key:
            return
        self._discard_prepared()
        self.audio_state._preparing = track.key
        self.prepare_track(deepcopy(track), self.audio_state._preparation_generation)

    def prepare_track_work(self, track: Track, generation: int | None = None) -> None:
        with self.worker_scope():
            try:
                if track.local_id and track.local_path:
                    from ..local_audio import prepare_local
                    prepared = prepare_local(track)
                    stream, samples, source = prepared.stream, prepared.waveform, prepared.source
                else:
                    stream = resolve_stream(self.client, track.id)
                    samples = fetch_waveform(self.client, stream.waveform_url)
                    source = open_source(self.client.session, stream.url)
            except Exception as exc:
                # Nothing is owed here: if this fails the track loads the ordinary
                # way in its own time, and says so then.
                LOGGER.debug("Could not prepare %s: %s", track.label, exc)
                self.call_from_thread(self._preparation_done, track.key, None, generation)
                return
            prepared = Prepared(track=track, stream=stream, waveform=samples, source=source)
            try:
                self.call_from_thread(self._preparation_done, track.key, prepared, generation)
            except RuntimeError:
                prepared.close()

    def _preparation_done(self, key: str, prepared: Prepared | None, generation: int | None = None) -> None:
        if self.audio_state._preparing != key or (generation is not None and generation != self.audio_state._preparation_generation):
            # The list moved under it while it was working.
            if prepared is not None:
                prepared.close()
            return
        self.audio_state._preparing = ""
        self.audio_state._prepared = prepared

    def _discard_prepared(self) -> None:
        self.audio_state._preparation_generation += 1
        self.audio_state._preparing = ""
        if self.audio_state._prepared is not None:
            self.audio_state._prepared.close()
            self.audio_state._prepared = None

    def _drop_stale_preparation(self) -> None:
        """A filter that changes what comes next makes the prepared track useless."""

        pending = self.audio_state._prepared.key if self.audio_state._prepared else self.audio_state._preparing
        if not pending:
            return
        index = self._step_from_playing(1)
        following = self.playlist_state.visible_rows[index].track.key if index is not None else None
        if pending != following:
            self._discard_prepared()

    def _take_prepared(self, track: Track) -> Prepared | None:
        if self.audio_state._prepared is None or self.audio_state._prepared.key != track.key:
            return None
        prepared, self.audio_state._prepared = self.audio_state._prepared, None
        return prepared

    def _advance_playback(self) -> None:
        """Roll on by itself, taking the cursor only if it was keeping up.

        Asking the question here, rather than watching every cursor move, keeps
        it out of the way of the redraw - which moves the cursor too.
        """

        table = self.query_one("#tracks", DataTable)
        self.audio_state._cursor_follows = self._playing_index() == table.cursor_row
        self._play_at(self._step_from_playing(1))

    def _playing_index(self) -> int | None:
        loaded = self.player.loaded
        if loaded is None:
            return None
        for index, row in enumerate(self.playlist_state.visible_rows):
            if row is self._playing_row and row.track.key == loaded.track.key:
                return index
        if self._playing_row is not None:
            return None
        for index, row in enumerate(self.playlist_state.visible_rows):
            if row.track.key == loaded.track.key:
                return index
        return None

    def _player_op(self, operation) -> None:
        """Run a player call. Every one of them can hit a missing audio device."""

        try:
            operation()
        except PlaybackUnavailable as exc:
            self._playback_failed(str(exc))
            return
        except Exception as exc:
            # The device guard in Player catches what it has seen before, but a
            # backend has more ways to fail than anyone has met yet - and from
            # here an exception goes out through the message pump and takes the
            # whole TUI with it. logger.exception, so --log-file gets the
            # traceback the crash screen used to swallow.
            LOGGER.exception("A player operation failed")
            self._playback_failed(f"Playback failed ({exc})")
            return
        self._player_bar().refresh_bar()

    def action_play_pause(self) -> None:
        row = self.current_row()
        if row is None:
            return
        loaded = self.player.loaded
        if (loaded is not None and loaded.track.key == row.track.key
                and (self._playing_row is None or self._playing_row is row)):
            self.audio_state._playback_generation += 1
            self._player_bar().message = ""
            self._player_op(self.player.toggle)
            self._wake()
            return
        # Playing what the cursor is on re-couples the two.
        self.audio_state._cursor_follows = True
        self._start_playback(row.track)

    def action_toggle_loaded(self) -> None:
        """Pause or resume what is playing, wherever the cursor has wandered to.

        ``space`` deliberately plays the row you are looking at. The button
        under the waveform of another track cannot mean that.
        """

        if self.player.loaded is None:
            self.action_play_pause()
            return
        self._player_op(self.player.toggle)
        self._wake()

    def action_close_player(self) -> None:
        """Stop, forget the track, and let the bar fold itself away.

        The message goes too: a bar left saying "No audio output" is a bar that
        will not close, since that alone is enough to keep it on screen.
        """

        self.audio_state._playback_generation += 1
        self._player_bar().message = ""
        was_playing = self._playing_key()
        self._player_op(self.player.unload)
        self._discard_prepared()
        self._sleep()
        if was_playing is not None:
            self._paint_key(was_playing)

    def _start_playback(self, track: Track) -> None:
        self._requested_row = next((row for row in self.playlist_state.visible_rows if row.track is track), None)
        self.audio_state._playback_generation += 1
        if not track.id and not track.local_id:
            self.notify("No track id, so there is nothing to stream", timeout=4)
            return
        self._wake()
        prepared = self._take_prepared(track)
        self._discard_prepared()
        if prepared is not None:
            self._audio_ready(track, prepared.stream, prepared.waveform, prepared.source)
            return
        bar = self._player_bar()
        bar.message = f"Loading {track.label}"
        bar.refresh_bar()
        self.fetch_audio(deepcopy(track), self.audio_state._playback_generation)

    def fetch_audio_work(self, track: Track, generation: int | None = None) -> None:
        with self.worker_scope():
            """Everything that touches the network for this track, off the UI thread.

            The source is opened here rather than left to ``Player.play`` - that runs
            on the interface thread, and the connect inside it waits up to thirty
            seconds for a CDN that is slow to answer with the whole app frozen behind
            it. This is the same call the prefetch path has always made off-thread.
            """

            try:
                if track.local_id and track.local_path:
                    from ..local_audio import prepare_local
                    prepared = prepare_local(track)
                    stream, samples, source = prepared.stream, prepared.waveform, prepared.source
                else:
                    stream = resolve_stream(self.client, track.id)
                    samples = fetch_waveform(self.client, stream.waveform_url)
                    source = open_source(self.client.session, stream.url)
            except (SoundCloudError, PlaybackUnavailable, OSError, RuntimeError) as exc:
                self.call_from_thread(self._playback_failed, str(exc), generation)
                return
            try:
                self.call_from_thread(self._audio_ready, track, stream, samples, source, generation)
            except RuntimeError:
                if source is not None:
                    source.close()
                return
    def _local_waveform_work(self, loaded, source):
        from ..local_audio import waveform
        class Closed:
            def is_set(self):
                return source._closed
        with self.worker_scope():
            try:
                samples = waveform(loaded.track.local_path, Closed())
                self.call_from_thread(self._waveform_ready, loaded, samples)
            except Exception as exc:
                LOGGER.debug('Local waveform unavailable: %s', exc)

    def _waveform_ready(self, loaded, samples):
        if self.player.loaded is loaded:
            loaded.waveform = samples
            self._player_bar().refresh_bar()

    def _audio_ready(
        self, track: Track, stream: Stream, samples: list[int], source=None, generation: int | None = None
    ) -> None:
        if generation is not None and generation != self.audio_state._playback_generation:
            if source is not None:
                source.close()
            return
        bar = self._player_bar()
        bar.message = ""
        previously_playing = self._playing_key()
        try:
            self.player.load(track, stream, None if track.local_id else self.client.session, samples, source)
            self.player.play()
        except PlaybackUnavailable as exc:
            self._playback_failed(str(exc))
            return
        except Exception as exc:  # a bad stream must not take the app down
            self._playback_failed(f"Could not start the stream ({exc})")
            return
        self._playing_row = self._requested_row
        # Resolving the stream takes about half a second, and a frame landing in
        # the middle of it finds nothing playing and puts the timer back to
        # sleep - so this is where it has to be woken, not where it was asked for.
        self._wake()
        # Repaint the two rows the marker moves between, then chase it. A full
        # rebuild here made every play a visible flicker on a long crate.
        if previously_playing is not None:
            self._paint_key(previously_playing)
        self._paint_key(track.key)
        self._focus_playing_track()
        bar.refresh_bar()
        if track.local_id and source is not None:
            loaded = self.player.loaded
            self.run_worker(lambda: self._local_waveform_work(loaded, source),
                            thread=True, group='local-waveform', exclusive=True)

    def _playback_failed(self, message: str, generation: int | None = None) -> None:
        if generation is not None and generation != self.audio_state._playback_generation:
            return
        bar = self._player_bar()
        bar.message = message
        bar.refresh_bar()
        self.notify(message, severity="warning", timeout=6)

    def _focus_playing_track(self) -> None:
        """Drag the cursor to what is playing, unless you steered it away.

        Wandering down the list while something plays is normal, and having the
        cursor yanked back on every auto-advance would make it impossible.
        """

        if not self.audio_state._cursor_follows:
            return
        index = self._playing_index()
        if index is not None:
            self.query_one("#tracks", DataTable).move_cursor(row=index)

    def action_seek(self, direction: int) -> None:
        if self.player.loaded is None:
            return
        self._player_op(lambda: self.player.nudge(direction * SEEK_STEP))

    def _step_from_playing(self, step: int) -> int | None:
        if not self.playlist_state.visible_rows:
            return None
        playing = self._playing_index()
        # Nothing of ours is playing, so step from wherever you are looking.
        start = playing if playing is not None else self.query_one("#tracks", DataTable).cursor_row
        index = start + step
        return index if 0 <= index < len(self.playlist_state.visible_rows) else None

    def _play_at(self, index: int | None) -> None:
        if index is None:
            if self.playlist_state.visible_rows:
                self.notify("End of the list", timeout=2)
            return
        self._start_playback(self.playlist_state.visible_rows[index].track)

    def action_play_step(self, step: int) -> None:
        # Asking for the next track means you want to be taken there.
        self.audio_state._cursor_follows = True
        self._play_at(self._step_from_playing(step))

    def action_volume(self, direction: int) -> None:
        self._player_op(lambda: self.player.change_volume(direction * VOLUME_STEP))

    def action_mute(self) -> None:
        self._player_op(self.player.toggle_mute)

    def prepare_track(self, *args, **kwargs):
        return self.run_worker(
            partial(self.prepare_track_work, *args, **kwargs), thread=True, exclusive=True, group='prefetch',
            description="prepare_track",
        )

    def fetch_audio(self, *args, **kwargs):
        return self.run_worker(
            partial(self.fetch_audio_work, *args, **kwargs), thread=True, exclusive=True, group='audio',
            description="fetch_audio",
        )
