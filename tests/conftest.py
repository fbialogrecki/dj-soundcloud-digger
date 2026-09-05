import json
import os
from pathlib import Path
from typing import Any

import pytest

from dj_digger import auth, config, db
from dj_digger.models import Track

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    """A requests-shaped reply: status, headers, text, and a json() that can fail."""

    def __init__(self, status_code=200, payload=None, text="{}", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def close(self):
        pass

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

# Land animations on their final value at once rather than over 200ms. Textual
# reads this when it is imported, so it has to be set before anything pulls it
# in - a test that had to wait out an animation is a test that fails on a loaded
# machine.
os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")


@pytest.fixture(autouse=True)
def isolate_user_data(tmp_path, monkeypatch):
    """Never let a test read or write the real crate library or status file.

    Config and auth are in here too: ``AppConfig()`` writes a default file the
    first time it cannot find one, and ``SoundCloudClient`` reads auth.json on
    construction - so without this a test run edits the developer's own settings
    and can pick up their live OAuth token.

    And the scan folders, because the crate browser starts a library scan on
    mount. Left alone it defaults to ~/Music and ~/Downloads, so every test that
    opens the app would walk the developer's actual music collection.
    """

    # Not "music": macOS and Windows have case-insensitive filesystems, so this
    # directory and a test's own tmp_path/"Music" are the same one there, and
    # whichever ran second failed with FileExistsError. A name no test would
    # reach for by accident keeps that from happening again.
    scan_dir = tmp_path / "isolated-scan-root"
    scan_dir.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"scan_directories": [str(scan_dir)]}), encoding="utf-8"
    )

    # Everything that goes through paths.data_dir() / config_dir() - the store
    # browser profile, cart diagnostics - lands under tmp_path as well; a test
    # run once left eight diagnostics folders in the developer's real data dir.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    from dj_digger.tui.local import LocalController
    monkeypatch.setattr(LocalController, "mounts", staticmethod(lambda: []))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setattr(config, "default_config_path", lambda: config_path)
    monkeypatch.setattr(db, "default_db_path", lambda: tmp_path / "digger.db")
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth" / "auth.json")
    yield
    for instance in list(db._DATABASES):
        instance.close()
    db._INSTANCES.clear()


@pytest.fixture(autouse=True)
def no_real_exit(monkeypatch):
    """run_tui may end the process when a thread lingers; never from a test.

    The exit codes are recorded rather than raised, so a test about that very
    shutdown can read them back - and must clear what it expected, because
    anything still in the list at teardown fails the test.
    """

    from dj_digger import tui

    exits: list[int] = []
    monkeypatch.setattr(tui, "HARD_EXIT", exits.append)
    yield exits
    assert exits == [], f"run_tui hard-exited with {exits}"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def track_payloads() -> list[dict[str, Any]]:
    """Four real tracks, trimmed, covering every categorisation branch."""

    return load_fixture("tracks.json")


@pytest.fixture
def tracks(track_payloads: list[dict[str, Any]]) -> list[Track]:
    return [Track.from_api(payload) for payload in track_payloads]


@pytest.fixture
def tracks_by_id(tracks: list[Track]) -> dict[int, Track]:
    return {track.id: track for track in tracks if track.id}


@pytest.fixture
def playlist_payload() -> dict[str, Any]:
    """A real /resolve reply: full envelope, id-only track stubs."""

    return load_fixture("playlist_resolve.json")


@pytest.fixture(autouse=True)
def offline_requests_only(request, monkeypatch):
    """An omitted fake must fail locally instead of reaching a provider."""
    if any(request.node.get_closest_marker(name) for name in ("live", "shop_live", "hypeddit_live", "shop_mutate")):
        return
    import requests
    def blocked(*args, **kwargs):
        raise AssertionError("Offline test attempted an unfaked HTTP request")
    monkeypatch.setattr(requests.Session, "send", blocked)
