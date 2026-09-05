"""Shared data structures.

Lives in its own module so ``soundcloud``, ``html_fallback``, ``links`` and
``tui`` can all speak the same vocabulary without importing each other.
"""

import shlex
import threading
from dataclasses import dataclass, field
from typing import Any, Self

NEW = "new"
OPENED = "opened"
SKIP = "skip"
GOT = "got"
STATUSES = (NEW, OPENED, SKIP, GOT)

def parse_tags(tag_list: str) -> list[str]:
    """Split SoundCloud's tag_list, where multi-word tags are quoted."""

    if not tag_list:
        return []
    try:
        return shlex.split(tag_list)
    except ValueError:
        # An artist left a quote unclosed; we lose multi-word tags, not the lot.
        return tag_list.replace('"', " ").split()




class Cancelled(Exception):
    """Raised inside long-running work when its cancel event was set.

    Distinct from the network and provider errors so a caller can tell "the
    user stopped this" from "this failed" - a stopped dig must not be saved
    as a crate, and a stopped download is not a failure to report.
    """


def check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()

def track_key(value) -> str:
    """One identity rule for live tracks and persisted snapshots."""
    get = value.get if isinstance(value, dict) else lambda name, default=None: getattr(value, name, default)
    local_id = get("local_id")
    if local_id:
        return f"local:{local_id}"
    return str(get("id")) if get("id") else (get("permalink_url") or "")


@dataclass
class Track:
    """A SoundCloud track, however it was discovered."""

    title: str
    permalink_url: str
    id: int | None = None
    artist: str = ""
    purchase_url: str | None = None
    purchase_title: str | None = None
    download_url: str | None = None
    description: str = ""
    downloadable: bool = False
    # Artists cap how many free downloads they hand out, and the cap is reached
    # more often than not: `downloadable` alone promises a file that is gone.
    has_downloads_left: bool = False
    duration: int = 0
    genre: str = ""
    tags: list[str] = field(default_factory=list)
    # Links found outside the structured fields, e.g. scraped from a track page.
    extra_links: list[tuple[str, str]] = field(default_factory=list)
    local_path: str | None = None
    local_id: str | None = None
    # What a DJ sorts a crate by. SoundCloud fills these in for many uploads
    # and leaves them empty for the rest, so every one has an "unknown" value.
    bpm: float | None = None
    key_signature: str = ""
    release_year: int | None = None
    label_name: str = ""

    @property
    def key(self) -> str:
        """Stable identity used for persisted status, across playlists."""

        return track_key(self)

    @property
    def free_download(self) -> bool:
        """SoundCloud itself will hand over the file, and has not run out."""

        return self.downloadable and self.has_downloads_left

    @property
    def has_direct_download(self) -> bool:
        """The API says the artist currently offers a concrete download URL."""

        return self.free_download and bool(self.download_url)

    @property
    def duration_label(self) -> str:
        if self.duration <= 0:
            return ""
        seconds = round(self.duration / 1000)
        return f"{seconds // 60}:{seconds % 60:02d}"

    @property
    def label(self) -> str:
        if self.artist and self.artist.lower() not in self.title.lower():
            return f"{self.artist} - {self.title}"
        return self.title

    @property
    def genre_label(self) -> str:
        """Genre if the artist set one, otherwise their first tag."""

        return self.genre or (self.tags[0] if self.tags else "")

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Self:
        user = payload.get("user") or {}

        def clean(value: Any) -> str:
            return (value or "").strip() if isinstance(value, str) else ""

        return cls(
            id=payload.get("id"),
            title=clean(payload.get("title")) or "Unknown title",
            artist=clean(user.get("username")),
            permalink_url=clean(payload.get("permalink_url")),
            purchase_url=clean(payload.get("purchase_url")) or None,
            purchase_title=clean(payload.get("purchase_title")) or None,
            download_url=clean(payload.get("download_url")) or None,
            description=payload.get("description") or "",
            downloadable=bool(payload.get("downloadable")),
            has_downloads_left=bool(payload.get("has_downloads_left")),
            duration=int(payload.get("full_duration") or payload.get("duration") or 0),
            genre=clean(payload.get("genre")),
            tags=parse_tags(payload.get("tag_list") or ""),
            bpm=_number(payload.get("bpm")),
            key_signature=clean(payload.get("key_signature")),
            release_year=_year(payload.get("release_date")) or _year(payload.get("created_at")),
            label_name=clean(payload.get("label_name")),
        )

    @property
    def bpm_label(self) -> str:
        if not self.bpm:
            return ""
        return f"{self.bpm:g}"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _year(value: Any) -> int | None:
    """The year off an ISO-ish date; SoundCloud sends '2024/03/01 00:00:00 +0000' too."""

    if not isinstance(value, str) or len(value) < 4 or not value[:4].isdigit():
        return None
    year = int(value[:4])
    return year if 1900 <= year <= 2100 else None


@dataclass
class Crate:
    """A batch of tracks pulled from one source."""

    source: str
    tracks: list[Track] = field(default_factory=list)
    title: str = ""
    declared_count: int | None = None
    provider_id: int | None = None


@dataclass
class LinkRecord:
    """One categorised link belonging to one track."""

    category: str
    track: Track
    link_url: str
    link_text: str

    def as_dict(self) -> dict[str, Any]:
        """Export shape. Keeps the v0.1 keys so old summaries stay readable."""

        return {
            "title": self.track.title,
            "track_url": self.track.permalink_url,
            "shop_link": self.link_url,
            "artist": self.track.artist,
            "track_id": self.track.id,
            "link_text": self.link_text,
            "bpm": self.track.bpm,
            "key": self.track.key_signature,
            "release_year": self.track.release_year,
            "label": self.track.label_name,
        }
