"""Persisted crate values and serialization, independent of SQLite and UI."""

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, NamedTuple, Self

from .models import Crate, Track

VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _track_from_json(data: dict[str, Any]) -> Track:
    # Filtered by field name so a crate written by another version still loads.
    known = {f.name for f in fields(Track)}
    values = {key: value for key, value in data.items() if key in known}
    values["extra_links"] = [tuple(pair) for pair in values.get("extra_links") or []]
    return Track(**values)


@dataclass
class CrateRecord:
    source: str
    title: str
    tracks: list[Track] = field(default_factory=list)
    removed_track_keys: list[str] = field(default_factory=list)
    # What the last refresh brought in that the crate did not already have.
    new_track_keys: list[str] = field(default_factory=list)
    imported_at: str = ""
    refreshed_at: str | None = None
    partial: bool = False
    preserve_order: bool = False
    provider_id: int | None = None

    @property
    def active_tracks(self) -> list[Track]:
        removed = set(self.removed_track_keys)
        kept = [track for track in self.tracks if track.key not in removed]
        # What the last refresh added goes to the top; sorted is stable, so the
        # playlist's own order survives inside each half.
        if self.preserve_order or self.source.startswith("local-playlist:"):
            return kept
        arrived = set(self.new_track_keys)
        return sorted(kept, key=lambda track: track.key not in arrived)

    def remove(self, track_key: str) -> None:
        if track_key not in self.removed_track_keys:
            self.removed_track_keys.append(track_key)

    def restore(self, track_key: str) -> None:
        if track_key in self.removed_track_keys:
            self.removed_track_keys.remove(track_key)

    @classmethod
    def from_crate(cls, crate: Crate, *, partial: bool = False) -> Self:
        return cls(
            source=crate.source,
            title=crate.title or crate.source,
            tracks=list(crate.tracks),
            imported_at=_now(),
            partial=partial,
            provider_id=crate.provider_id,
        )

    def to_json(self) -> dict[str, Any]:
        # asdict recurses into the Track dataclasses too.
        return {"version": VERSION, **asdict(self)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls(
            source=data.get("source") or "",
            title=data.get("title") or data.get("source") or "crate",
            tracks=[_track_from_json(item) for item in data.get("tracks") or []],
            removed_track_keys=list(data.get("removed_track_keys") or []),
            new_track_keys=list(data.get("new_track_keys") or []),
            imported_at=data.get("imported_at") or "",
            refreshed_at=data.get("refreshed_at"),
            partial=bool(data.get("partial")),
            preserve_order=bool(data.get("preserve_order")),
            provider_id=data.get("provider_id"),
        )


class CrateHeader(NamedTuple):
    """What the sidebar needs to list a crate: no tracks attached."""

    source: str
    title: str
    updated: str
    partial: bool = False


