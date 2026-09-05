"""Dig purchase and free-download links out of SoundCloud playlists."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from the installed distribution rather than repeated here, because a
    # second copy of the version drifts: this said 0.4.20 while pyproject said
    # 0.5.1, so `dj-digger --version` had been wrong for two releases.
    __version__ = version("dj-digger")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
