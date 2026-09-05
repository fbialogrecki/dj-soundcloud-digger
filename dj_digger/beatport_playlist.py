"""Beatport carts are not automated; its results become playlist entries.

Everything that turns a Beatport request or result into a line Soundiiz can
read lives here, on both sides of the cart batch.
"""

import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

import requests

from .cart_models import CartBatchOutcome, CartRequest, CartResult
from .http import REQUEST_HEADERS
from .links import redact_url
from .store_urls import canonical_store_url


def _beatport_playlist_result(
    request: CartRequest,
    label: str,
    reason: str,
    url: str = "",
) -> CartResult:
    """Keep a Beatport request useful when read-only product lookup is blocked."""

    return CartResult(
        request.track.key,
        label,
        "beatport",
        "playlist_ready",
        reason,
        "playlist_ready",
        canonical_store_url(url, "beatport") or "",
    )


SOUNDIIZ_IMPORT_URL = "https://soundiiz.com/go/import-playlist"
PROMO_PREFIX = re.compile(
    r"^\s*(?:(?:full|tb)\s+)?(?:premiere|teaser)\s*:\s*", re.IGNORECASE
)
TRAILING_PROMO = re.compile(
    r"\s*(?:\[(?![^]]*\b(?:mix|remix|edit|dub|version)\b)[^]]+\]|"
    r"\|\s*[^|]+|\((?:excerpt|preview)\)|\*[^*]*(?:records?|recordings?)[^*]*\*|"
    r"\[?out\s+now\]?!?)\s*$",
    re.IGNORECASE,
)


def _beatport_playlist_requests(
    requests: Sequence[CartRequest], outcome: CartBatchOutcome
) -> tuple[CartRequest, ...]:
    requests_by_target = {
        (request.track.key, store): request
        for request in requests
        for store, _url in request.links
    }
    selected: list[CartRequest] = []
    seen_keys: set[str] = set()
    for result in outcome.results:
        if (
            result.store != "beatport"
            or result.code != "playlist_ready"
            or result.track_key in seen_keys
        ):
            continue
        request = requests_by_target.get((result.track_key, "beatport"))
        if request is not None:
            seen_keys.add(result.track_key)
            selected.append(request)
    return tuple(selected)


def _beatport_playlist_lines(
    requests: list[CartRequest], outcome: CartBatchOutcome
) -> tuple[str, ...]:
    """Soundiiz-compatible entries, exact when Beatport exposed a track URL."""

    selected = _beatport_playlist_requests(requests, outcome)
    results_by_key = {
        result.track_key: result
        for result in outcome.results
        if result.store == "beatport" and result.code == "playlist_ready"
    }
    lines: list[str] = []
    for request in selected:
        result = results_by_key[request.track.key]
        url = canonical_store_url(result.url, "beatport")
        if url and "/track/" in urlparse(url).path:
            lines.append(redact_url(url))
            continue
        artist = " ".join(request.track.artist.split())
        title = " ".join(request.track.title.split())
        lines.append(f"{artist} - {title}" if artist else title)
    return tuple(line for line in lines if line)


def _soundiiz_metadata(request: CartRequest) -> dict[str, object]:
    """Turn promo-upload metadata into the catalog-shaped hint Soundiiz expects."""

    title = " ".join(PROMO_PREFIX.sub("", request.track.title).split())

    def strip_promo(value: str) -> str:
        cleaned = value
        while True:
            shorter = TRAILING_PROMO.sub("", cleaned).strip()
            if shorter == cleaned:
                return cleaned
            cleaned = shorter

    title = strip_promo(title)
    separators = list(re.finditer(r"\s+[-–—]\s+", title))
    artist = " ".join(request.track.artist.split())
    split = separators[0] if separators else re.search(
        r"\s+[-–—]\s*|(?<=\w)[–—](?=\w)|(?<=\w)-(?=[A-Z])", title
    )
    if split is not None:
        candidate_artist = title[: split.start()].strip()
        title = title[split.end() :].strip()
        artist = candidate_artist
        # Promo uploads commonly append the label as a third dash-separated
        # field. Keeping it makes Soundiiz search for a title that never existed.
        if len(separators) >= 2 and " - " in title:
            title = title.rsplit(" - ", 1)[0].strip()
        title = strip_promo(title)
    title = re.sub(r"\(\s*([^()]*?)\s*\)", r"(\1)", title)
    artists = [artist] if artist else []
    credits = re.findall(
        r"\(([^()]*(?:remix|rmx|rework|edit)[^()]*)\)", title, re.IGNORECASE
    )
    featured = re.search(r"\b(?:feat(?:uring)?\.?|ft\.?)\s+(.+)$", artist, re.IGNORECASE)
    if featured:
        credits.append(featured.group(1))
    for credit in credits:
        credit = re.sub(
            r"\s+(?:remix|rmx|rework|edit)(?:es)?\s*$", "", credit, flags=re.IGNORECASE
        ).strip()
        if credit and credit.casefold() not in {name.casefold() for name in artists}:
            artists.append(credit)
    return {"title": title, "artists": artists}


def _create_soundiiz_import(
    requests_: Sequence[CartRequest], outcome: CartBatchOutcome, title: str
) -> str:
    """Create Soundiiz's documented temporary review page for this tracklist."""

    selected = _beatport_playlist_requests(requests_, outcome)
    if not 1 <= len(selected) <= 200:
        raise ValueError("Soundiiz accepts between 1 and 200 tracks per import")
    response = requests.post(
        SOUNDIIZ_IMPORT_URL,
        headers={
            **REQUEST_HEADERS,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={
            "title": title or "DJ Digger Beatport playlist",
            "sourceName": "dj-digger",
            "destination": "beatport",
            "tracklist": [_soundiiz_metadata(request) for request in selected],
        },
        timeout=15,
        allow_redirects=False,
    )
    response.raise_for_status()
    if not 200 <= response.status_code < 300:
        raise ValueError("Soundiiz redirected the playlist import")
    share_url = str(response.json().get("shareUrl", ""))
    parsed = urlparse(share_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "soundiiz.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/go/import-playlist/")
    ):
        raise ValueError("Soundiiz returned an invalid import URL")
    return share_url


def _write_beatport_playlist(lines: tuple[str, ...], directory: Path) -> Path:
    """Create, never overwrite, a plain-text playlist accepted by Soundiiz."""

    directory.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        suffix = "" if index == 1 else f" ({index})"
        path = directory / f"Beatport playlist{suffix}.txt"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines) + "\n")
        except FileExistsError:
            index += 1
            continue
        return path
