"""Local library dialogs with explicit, reviewable export choices."""
from pathlib import Path

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Input, Label, Select, Static

from ..analysis import NOTES, camelot
from ..decks import Profile, compatibility
from .screens import _Modal


class TextPrompt(_Modal):
    DEFAULT_CSS = "TextPrompt .modal-box { width: 75; }"
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, title, value=''):
        super().__init__()
        self.prompt, self.value = title, value

    def compose(self):
        with Vertical(classes='modal-box'):
            yield Label(self.prompt)
            yield Input(value=self.value, id='value')
            yield Button('Continue', id='accept')
        yield Footer()

    def on_input_submitted(self, event):
        event.stop()
        self.accept()

    def on_button_pressed(self, event):
        event.stop()
        self.accept()

    def accept(self):
        value = self.query_one('#value', Input).value.strip()
        if value:
            self.dismiss(value)


class ExportOptions(_Modal):
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = 'ExportOptions .modal-box { width: 90; max-height: 95%; } ExportOptions Horizontal { height: 3; } ExportOptions Label { width: 1fr; height: auto; }'

    def __init__(self, folder):
        super().__init__()
        self.folder = str(folder)

    def compose(self):
        with VerticalScroll(classes='modal-box'):
            yield Label('Prepare music folder — no rekordbox library or USB formatting')
            yield Label('Format for files requiring conversion (compatible formats are kept)')
            yield Select([('WAV', 'wav'), ('AIFF', 'aiff'), ('FLAC', 'flac')], value='wav', allow_blank=False, id='format')
            with Horizontal():
                yield Select([('16-bit maximum', 16), ('24-bit maximum', 24)], value=24, allow_blank=False, id='bits')
                yield Select([(f'{rate / 1000:g} kHz maximum', rate) for rate in (44100, 48000, 88200, 96000)], value=48000, allow_blank=False, id='rate')
            yield Static('', id='compatibility', markup=False)
            yield Input(value=self.folder, placeholder='Destination parent folder / mounted USB', id='folder')
            yield Checkbox('Replace originals (no permanent backup)', id='replace')
            yield Checkbox('Include subfolders of the open directory', id='recursive')
            yield Label('Default: a new folder with COPIES of every selected audio file, including unchanged files.')
            yield Button('Inspect files and review plan', id='plan', variant='primary')
        yield Footer()

    def on_mount(self):
        self.update_compatibility()

    def profile(self):
        return Profile(self.query_one('#format', Select).value, self.query_one('#bits', Select).value,
                       self.query_one('#rate', Select).value)

    def on_select_changed(self, event):
        event.stop()
        self.update_compatibility()

    def update_compatibility(self):
        states = compatibility([self.profile().media()])
        self.query_one('#compatibility', Static).update('Profile, according to documentation:\n' + '\n'.join(f'{name}: {value}' for name, value in states.items()))

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss(dict(profile=self.profile(), folder=Path(self.query_one('#folder', Input).value).expanduser(),
                          mode='replace' if self.query_one('#replace', Checkbox).value else 'copy',
                          recursive=self.query_one('#recursive', Checkbox).value))


class ExportReview(_Modal):
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = 'ExportReview .modal-box { width: 95; max-height: 95%; }'

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def compose(self):
        with VerticalScroll(classes='modal-box'):
            yield Label(f'{len(self.plan.items)} audio files · {self.plan.mode}')
            yield Static('Actual planned set, according to documentation:\n' + '\n'.join(f'{deck}: {state}' for deck, state in self.plan.compatibility().items()), markup=False)
            yield Static('\n'.join(f'{item.action}: {Path(item.source).name} → {Path(item.destination).name}' + (f' — {item.reason}' if item.reason else '') for item in self.plan.items[:200]), markup=False)
            if len(self.plan.items) > 200:
                yield Label('First 200 shown; the saved report includes the complete plan.')
            yield Label('Exceptions remain unexported and are listed in the report.')
            if self.plan.mode == 'replace':
                yield Label('Successful replacement removes the original permanently. Close playback first.')
            yield Button('Execute this plan', id='execute', variant='warning' if self.plan.mode == 'replace' else 'primary')
        yield Footer()

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss(True)


class AnalysisEdit(_Modal):
    DEFAULT_CSS = "AnalysisEdit .modal-box { width: 75; max-height: 95%; overflow-y: auto; } AnalysisEdit Horizontal { height: 3; }"
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, track):
        super().__init__()
        self.track = track

    def compose(self):
        with Vertical(classes='modal-box'):
            yield Label('Manual BPM / key — automatic values are estimates')
            yield Input(value=self.track.bpm_label, placeholder='BPM (empty = use analysis/tags)', id='bpm')
            with Horizontal():
                yield Button('÷2', id='half')
                yield Button('×2', id='double')
            keys = [('', '')] + [(f'{note}{suffix} / {camelot(note + suffix)}', note + suffix) for note in NOTES for suffix in ('', 'm')]
            yield Select(keys, value=self.track.key_signature if self.track.key_signature in [value for _, value in keys] else '', allow_blank=False, id='key')
            yield Button('Save manual values', id='save')
            yield Button('Clear manual overrides', id='clear')
        yield Footer()

    def on_button_pressed(self, event):
        event.stop()
        field = self.query_one('#bpm', Input)
        try:
            bpm = float(field.value) if field.value.strip() else None
            if bpm is not None and not 0 < bpm <= 999:
                raise ValueError
        except ValueError:
            self.notify('BPM must be between 0 and 999', severity='error')
            return
        if event.button.id in ('half', 'double'):
            if bpm:
                field.value = f'{bpm * (.5 if event.button.id == "half" else 2):g}'
        else:
            values = {}
            if event.button.id != 'clear':
                if bpm:
                    values['bpm'] = bpm
                key = self.query_one('#key', Select).value
                if key:
                    values['key'] = key
            self.dismiss(values)


class ProfileImportOptions(_Modal):
    DEFAULT_CSS = "ProfileImportOptions .modal-box { width: 75; }"
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def compose(self):
        with Vertical(classes='modal-box'):
            yield Label('Import playlists created by a SoundCloud profile')
            yield Input(placeholder='https://soundcloud.com/profile', id='profile')
            yield Checkbox('Include private playlists (requires owner login)', id='private')
            yield Button('Import playlists', id='import')
        yield Footer()

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss((self.query_one('#profile', Input).value.strip(), self.query_one('#private', Checkbox).value))
