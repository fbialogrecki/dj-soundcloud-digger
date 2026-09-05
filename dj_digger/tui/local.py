"""Explorer, local playlists, analysis and export presentation coordinator."""
import asyncio
import os
import uuid
from pathlib import Path

from textual.widgets import Button, Tree

from ..analysis import analyze_track
from ..export import execute, plan_export, recover, resume_plan
from ..models import Cancelled
from ..services.local_library import PAGE_SIZE, LocalLibrary, media_track
from ..services.profile_import import import_profile
from .local_screens import (
    AnalysisEdit,
    ExportOptions,
    ExportReview,
    ProfileImportOptions,
    TextPrompt,
)


class LocalController:
    def __init__(self, *, services, playlist_state, crate_controller, audio_state, config,
                 jobs, notify, push_screen, run_worker, query_one, refresh_rows, selected_rows,
                 current_row, build_columns):
        self.services, self.playlist_state, self.crates = services, playlist_state, crate_controller
        self.audio_state, self.config, self.jobs = audio_state, config, jobs
        self.notify, self.push_screen, self.run_worker, self.query_one = notify, push_screen, run_worker, query_one
        self.refresh_rows, self.selected_rows, self.current_row = refresh_rows, selected_rows, current_row
        self.build_columns = build_columns
        self.library = LocalLibrary(services.state.db)
        self.folder = None
        self.offset, self.total, self.generation = 0, 0, 0
        self._metadata_lock = asyncio.Lock()
        self._heavy_handle = None

    async def mount(self):
        tree = self.query_one('#explorer', Tree)
        tree.root.expand()
        roots = list(dict.fromkeys([*self.config.pinned_directories, *self.config.scan_directories,
                                  self.config.download_directory, str(Path.home())]))
        mounts = await self.services.io(self.mounts)
        for path in dict.fromkeys([*roots, *mounts]):
            tree.root.add(Path(path).name or path, data=Path(path))
        messages = await self.services.io(recover, self.services.state.db)
        await self.services.io(self.services.state.reload_file_paths)
        for message in messages:
            self.notify(message, severity='error', timeout=15)
        self.layout()

    @staticmethod
    def mounts():
        if os.name == 'nt':
            return [f'{letter}:/' for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(f'{letter}:/').is_dir()]
        found = []
        for base in (Path('/media') / os.environ.get('USER', ''), Path('/run/media') / os.environ.get('USER', ''), Path('/Volumes')):
            try:
                found.extend(str(path) for path in base.iterdir() if path.is_dir())
            except OSError:
                pass
        return found

    def layout(self):
        small = self.query_one('#sidebar').size.height < 25
        mode = self.config.sidebar_mode
        if small and mode == 'both':
            mode = 'playlists'
        for name, visible, height in (
            ('playlist-pane', mode != 'explorer', self.config.sidebar_split),
            ('explorer-pane', mode != 'playlists', 100 - self.config.sidebar_split),
        ):
            widget = self.query_one('#' + name)
            widget.display = visible
            widget.styles.height = f'{height}%' if mode == 'both' else '100%'

    def resize_split(self):
        self.config.sidebar_split = {50: 70, 70: 30, 30: 50}.get(self.config.sidebar_split, 50)
        self.layout()
        self.run_worker(self.services.io(self.config.save))

    def choose_folder(self):
        self.push_screen(TextPrompt('Open a music directory', str(self.folder or Path.home() / 'Music')),
                         lambda value: self.open(Path(value).expanduser()) if value else None)

    def open(self, folder, offset=0, node=None):
        self.generation += 1
        generation = self.generation
        self.run_worker(self.load(folder, offset, generation, node), group='explorer', exclusive=True)

    async def load(self, folder, offset, generation, node):
        try:
            tracks, directories, total, failures = await self.services.io(self.library.page, folder, offset)
            if generation != self.generation:
                return
            self.folder, self.offset, self.total = folder, offset, total
            self.playlist_state.crate = None
            self.crates.load_records([], title=str(folder))
            self.crates.set_tracks(tracks)
            view = self.playlist_state._view_generation
            self.query_one('#folder-next', Button).display = total > PAGE_SIZE
            if node is not None and not node.children:
                for name in directories[:1000]:
                    node.add(name, data=folder / name)
                node.expand()
            if failures:
                self.notify(f'{len(failures)} files unavailable', severity='warning')
            async with self._metadata_lock:
                for index, track in enumerate(tracks):
                    if generation != self.generation or view != self.playlist_state._view_generation:
                        return
                    try:
                        fresh = await self.services.io(self.library.register, Path(track.local_path), inspect=True)
                    except Exception:
                        continue
                    if generation != self.generation or view != self.playlist_state._view_generation:
                        return
                    self.playlist_state.rows[index].track = fresh
                    if index % 20 == 19 or index == len(tracks) - 1:
                        self.refresh_rows()
        except (OSError, RuntimeError) as exc:
            self.notify(str(exc), severity='error')

    def next_page(self):
        if self.folder:
            self.open(self.folder, self.offset + PAGE_SIZE if self.offset + PAGE_SIZE < self.total else 0)

    def pin(self):
        if self.folder and str(self.folder) not in self.config.pinned_directories:
            self.config.pinned_directories.append(str(self.folder))
            self.run_worker(self.services.io(self.config.save))
            self.query_one('#explorer', Tree).root.add(self.folder.name, data=self.folder)
            self.notify('Directory pinned')

    def tracks(self):
        return [row.track for row in (self.selected_rows() or self.playlist_state.visible_rows) if row.track.local_id]

    def save_playlist(self):
        tracks = self.tracks()
        if not tracks:
            self.notify('Select local audio files first')
            return
        self.push_screen(TextPrompt('Local playlist name (an existing name appends selected files)'),
                         lambda title: self.run_worker(self._save_playlist(title, tracks)) if title else None)

    async def _save_playlist(self, title, tracks):
        headers = await self.services.io(self.services.library.headers)
        matches = [header for header in headers if header.source.startswith('local-playlist:') and header.title == title]
        if len(matches) > 1:
            self.notify('Ambiguous playlist name; choose a unique name', severity='error')
            return
        source = matches[0].source if matches else 'local-playlist:' + uuid.uuid4().hex
        await self.services.io(self.services.state.db.save_local_playlist, source, title, [track.local_id for track in tracks])
        await self.crates.reload_sidebar()
        self.notify(f'Added {len(tracks)} files to {title}')

    def export_options(self):
        self.push_screen(ExportOptions(self.folder or Path(self.config.download_directory)),
                         lambda options: self.run_worker(self._plan(options)) if options else None)

    async def job(self, name, operation):
        handle = None
        try:
            handle = self.jobs.start(name)
            self._heavy_handle = handle
            return await self.services.io(operation, handle.cancel)
        except Cancelled:
            self.notify('Cancelled; completed results have been kept')
        except Exception as exc:
            self.notify(str(exc), severity='error', timeout=10)
        finally:
            if handle is not None:
                self.jobs.finish(handle)
                if self._heavy_handle is handle:
                    self._heavy_handle = None

    async def _plan(self, options):
        selected = self.selected_rows()
        folder = self.folder if self.playlist_state.crate is None else None
        paths = tuple(Path(track.local_path) for track in self.tracks())
        search = self.playlist_state.search_term
        hide_handled = self.playlist_state.hide_handled

        def build(cancel):
            sources = paths
            if folder and not selected:
                sources = self.library.selection(folder, recursive=options['recursive'], cancel=cancel)
                if search or hide_handled:
                    from .playlist import filter_rows
                    from .rows import Row
                    matching = []
                    for path in sources:
                        track = self.library.register(path, inspect=bool(search), cancel=cancel)
                        row = Row(1, track, [])
                        if filter_rows([row], search, hide_handled, lambda value: self.services.state.get(value.track.key)):
                            matching.append(path)
                    sources = tuple(matching)
            return plan_export(sources, options['folder'], options['profile'], mode=options['mode'], cancel=cancel)

        plan = await self.job('Inspecting export', build)
        if plan:
            self.push_screen(ExportReview(plan), lambda confirmed: self.run_worker(self._export(plan)) if confirmed else None)

    async def _export(self, plan, resume=False):
        from ..local_audio import LEASE_LOCK, LEASES

        def protected():
            with LEASE_LOCK:
                return tuple(LEASES)

        report = await self.job('Exporting audio', lambda cancel: execute(plan, self.services.state.db, resume=resume, cancel=cancel, protected=protected, progress=lambda done, total: self._progress(done, total)))
        if report:
            await self.services.io(self.services.state.reload_file_paths)
            self.notify(f"Export {report['status']}: {len(report['missing'])} missing files. " + (plan.folder if plan.mode == 'copy' else 'See operation report.'), timeout=12)
            if plan.mode == 'replace':
                from ..paths import data_dir
                from ..private_json import write_private_json
                await self.services.io(write_private_json, data_dir() / f'export-{plan.id}.json', report)
            if self.folder and self.playlist_state.crate is None:
                self.open(self.folder, self.offset)

    def analyze(self):
        tracks = self.tracks()
        if not tracks:
            self.notify('Select local files to analyze')
            return
        self.run_worker(self._analyze(tracks))

    async def _analyze(self, tracks):
        def work(cancel):
            failures = []
            for index, track in enumerate(tracks):
                self._progress(index, len(tracks))
                try:
                    self.library.register(Path(track.local_path))
                    analyze_track(self.services.state.db, track, cancel)
                except Cancelled:
                    raise
                except Exception as exc:
                    failures.append(f'{track.title}: {exc}')
            return failures

        failures = await self.job('Analyzing audio', work)
        if failures is not None:
            self.notify(f'Analysis finished: {len(failures)} failures', timeout=8)
            if failures:
                self.notify(failures[0], severity='warning', timeout=12)
            await self.refresh_metadata()

    def edit(self):
        row = self.current_row()
        if row and row.track.local_id:
            self.push_screen(AnalysisEdit(row.track), lambda values: self.run_worker(self._edit(row.track.local_id, values)) if values is not None else None)

    async def _edit(self, media_id, values):
        await self.services.io(self.services.state.db.set_media_manual, media_id, values)
        await self.refresh_metadata()

    async def refresh_metadata(self):
        # A folder probe begun before a manual correction must not repaint it.
        self.generation += 1
        view = self.playlist_state._view_generation
        for row in list(self.playlist_state.rows):
            if row.track.local_id:
                record = await self.services.io(self.services.state.db.media, row.track.local_id)
                if view != self.playlist_state._view_generation:
                    return
                row.track = await self.services.io(media_track, self.services.state.db, record)
        self.config.columns = list(dict.fromkeys([*self.config.columns, 'bpm', 'key']))
        self.build_columns()
        self.refresh_rows()

    def profile(self):
        self.push_screen(ProfileImportOptions(), lambda answer: self.run_worker(self._profile(*answer)) if answer else None)

    async def _profile(self, url, private):
        client = self.services.client
        def work(cancel):
            from ..soundcloud_errors import SoundCloudError
            try:
                return import_profile(client, self.services.state.db, url, private=private, cancel=cancel,
                                      current=lambda: self.services._client is client)
            except SoundCloudError as exc:
                return [*getattr(exc, 'report', []), {'status': 'failed', 'error': str(exc)}]

        report = await self.job('Importing profile playlists', work)
        await self.crates.reload_sidebar()
        if report is not None:
            from ..paths import data_dir
            from ..private_json import write_private_json
            output = data_dir() / f'profile-import-{uuid.uuid4().hex}.json'
            await self.services.io(write_private_json, output, {'profile': url, 'results': report})
            self.notify(f"Imported {sum(item['status'] == 'imported' for item in report)} / {len(report)} playlists. Report: {output}", timeout=10)
            failures = [item for item in report if item['status'] != 'imported']
            if failures:
                self.notify(failures[0].get('error') or failures[0].get('message') or 'Some playlists were incomplete; previous contents retained. See report.', severity='warning', timeout=12)

    async def resume(self):
        records = await self.services.io(self.services.state.db.media_operations)
        plans = [resume_plan(record) for record in records.values() if record.get('kind') == 'copy' and record['stage'] != 'done']
        if not plans:
            self.notify('No unfinished folder exports')
            return
        plan = plans[-1]
        self.push_screen(ExportReview(plan), lambda confirmed: self.run_worker(self._export(plan, resume=True)) if confirmed else None)

    def toggle_section(self):
        modes = ['both', 'playlists', 'explorer']
        self.config.sidebar_mode = modes[(modes.index(self.config.sidebar_mode) + 1) % len(modes)]
        self.layout()
        self.run_worker(self.services.io(self.config.save))

    def _progress(self, done, total):
        handle = self._heavy_handle
        if handle is not None and self.services.operations.current(handle):
            handle.total = total
            self.services.operations.progress(handle, max(0, done - handle.done))
