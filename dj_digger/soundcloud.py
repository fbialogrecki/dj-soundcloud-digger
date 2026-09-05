"""Client for SoundCloud's public (but undocumented) api-v2.

Why this exists: a playlist page only renders the first handful of tracks in the
DOM, so the old workflow needed you to scroll to the bottom and save the HTML by
hand. The API does not have that problem - ``/resolve`` returns every track id in
one response, however long the playlist is. Tracks are then hydrated in batches
of 50, which is the server-side cap.

The API needs no account and no key, but it does need a ``client_id`` lifted from
SoundCloud's own JS bundles. That id rotates, so it is cached and re-discovered
whenever a request comes back unauthorised.
"""

import logging
import os
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from itertools import batched
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from dj_digger import gate_models

from . import auth, files
from .diagnostics import log_safe_text
from .files import _extension_for, _save_stream, _WebPageNotFile
from .gates import providers as gates
from .http import USER_AGENT, UnsafeRedirect, follow_redirects, is_openable
from .links import host_matches, host_of
from .models import Cancelled, Crate, Track, check_cancelled
from .soundcloud_errors import SoundCloudError, SoundCloudLoginRequired, SoundCloudTokenRejected

API_ROOT = "https://api-v2.soundcloud.com"
DISCOVER_URL = "https://soundcloud.com/discover"

# The /tracks endpoint answers 400 for more than 50 ids.
HYDRATE_BATCH = 50
PAGE_SIZE = 200

CLIENT_ID_RE = re.compile(r'client_id[=:]"?([A-Za-z0-9]{32})')
ASSET_RE = re.compile(r"https://a-v2\.sndcdn\.com/assets/[^\"']+\.js")

# Two-segment SoundCloud paths that mean "a collection belonging to this user"
# rather than "a track by this user", mapped to their API endpoint.
USER_COLLECTIONS = {"likes": "likes", "tracks": "tracks", "reposts": "reposts"}

LOGGER = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int | None], None]




def create_requests_session(max_retries: int = 5, backoff_factor: float = 0.5) -> requests.Session:
    """Return a session that backs off on rate limits and transient failures."""

    retry_strategy = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        allowed_methods=["HEAD", "GET", "OPTIONS"],
        status_forcelist=[429, 500, 502, 503, 504],
        backoff_factor=backoff_factor,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def is_soundcloud_url(value: str) -> bool:
    value = value.strip()
    return is_openable(value) and host_matches(host_of(value), "soundcloud.com")


def _client_id_cache() -> Path:
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "dj-digger"
    return cache_dir / "client_id.txt"


def split_user_collection(url: str) -> tuple[str | None, str]:
    """Split ``/someone/likes`` into ``("likes", "https://soundcloud.com/someone")``.

    ``/resolve`` does not understand the collection suffixes, so they have to be
    peeled off and turned into a dedicated endpoint call.
    """

    parsed = urlparse(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) == 2 and segments[1].lower() in USER_COLLECTIONS:
        base = f"{parsed.scheme}://{parsed.netloc}/{segments[0]}"
        return USER_COLLECTIONS[segments[1].lower()], base
    return None, url


class SoundCloudClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 20.0,
        client_id: str | None = None,
        oauth_token: str | None = None,
        config=None,
        public_session: requests.Session | None = None,
    ) -> None:
        self._session = session or create_requests_session()
        self._public_session = public_session
        self._injected_transfer_session = public_session
        self._timeout = timeout
        self._client_id = client_id
        self._oauth_token = oauth_token

        self.config = gates.config_or_default(config)

    def close(self) -> None:
        self._session.close()
        if self._public_session is not None and self._public_session is not self._session:
            self._public_session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def client_id(self) -> str:
        if self._client_id is None:
            self._client_id = self._discover_client_id()
        return self._client_id

    def _discover_client_id(self, *, force: bool = False) -> str:
        cache = _client_id_cache()
        if not force:
            try:
                cached = cache.read_text(encoding="utf-8").strip()
            except OSError:
                cached = ""
            if len(cached) == 32:
                LOGGER.debug("Using cached client_id")
                return cached

        LOGGER.debug("Discovering client_id from SoundCloud JS bundles")
        try:
            page = self._session.get(DISCOVER_URL, timeout=self._timeout).text
        except requests.RequestException as exc:
            raise SoundCloudError(f"Could not reach soundcloud.com: {log_safe_text(exc)}") from exc

        # Later bundles carry the API config, so search from the back.
        for asset in sorted(set(ASSET_RE.findall(page)), reverse=True):
            try:
                bundle = self._session.get(asset, timeout=self._timeout).text
            except requests.RequestException:
                continue
            match = CLIENT_ID_RE.search(bundle)
            if not match:
                continue
            client_id = match.group(1)
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(client_id, encoding="utf-8")
            except OSError as exc:
                LOGGER.debug("Could not cache client_id: %s", exc)
            return client_id

        raise SoundCloudError(
            "Could not find a client_id in SoundCloud's JS bundles. "
            "SoundCloud may have changed its site - try the saved-HTML fallback."
        )

    @property
    def oauth_token(self):
        return os.environ.get("SOUNDCLOUD_OAUTH_TOKEN", "").strip() or (
            self._oauth_token if self._oauth_token is not None else auth.get_stored_token()
        )

    def _request(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET with one automatic retry against a freshly discovered client_id."""

        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "api-v2.soundcloud.com" or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise SoundCloudError("Refusing an untrusted SoundCloud API address")
        for attempt in (0, 1):
            merged = dict(params or {})
            merged["client_id"] = self.client_id
            kwargs: dict[str, Any] = {"params": merged, "timeout": self._timeout, "allow_redirects": False}
            token = self.oauth_token
            if token:
                kwargs["headers"] = {"Authorization": f"OAuth {token}"}
            try:
                response = self._session.get(url, **kwargs)
            except requests.RequestException as exc:
                raise SoundCloudError(f"Request to {url} failed: {log_safe_text(exc)}") from exc

            try:
                if response.status_code in (401, 403) and attempt == 0:
                    LOGGER.info("client_id rejected (%s), refreshing", response.status_code)
                    self._client_id = self._discover_client_id(force=True)
                    continue

                if 300 <= response.status_code < 400:
                    raise SoundCloudError("SoundCloud API redirected unexpectedly")
                if response.status_code == 404:
                    raise SoundCloudError(
                        "SoundCloud returned 404. Check the link, and note that private "
                        "or unlisted content needs the saved-HTML fallback."
                    )
                if response.status_code >= 400:
                    raise SoundCloudError(
                        f"SoundCloud returned HTTP {response.status_code} for {url}", status_code=response.status_code
                    )

                try:
                    return response.json()
                except ValueError as exc:
                    raise SoundCloudError(f"SoundCloud sent a non-JSON reply for {url}") from exc
            finally:
                response.close()

        raise SoundCloudError("SoundCloud kept rejecting our client_id")

    def _get(self, path: str, **params: Any) -> Any:
        return self._request(f"{API_ROOT}{path}", params)

    @property
    def session(self) -> requests.Session:
        """For plain downloads, e.g. an audio stream, that are not API calls."""

        if self._public_session is None:
            self._public_session = create_requests_session()
        return self._public_session

    def fetch_track(self, track_id: int) -> dict[str, Any]:
        """The raw payload for one track.

        ``Track`` deliberately keeps only what the link digger needs, so playback
        has to come here for ``media`` and ``track_authorization``.
        """

        payload = self._get("/tracks", ids=str(int(track_id)))
        if not isinstance(payload, list) or not payload:
            raise SoundCloudError(f"Track {track_id} is no longer available")
        return payload[0]

    def authorize(self, url: str, **params: Any) -> dict[str, Any]:
        """Call an absolute api-v2 URL, such as a media transcoding."""

        payload = self._request(url, params)
        if not isinstance(payload, dict):
            raise SoundCloudError(f"Unexpected reply from {url}")
        return payload

    def _artist_download_url(self, track: Track, session: requests.Session) -> str | None:
        """Where the artist's own file is, from the account-only /download endpoint."""

        token = self.oauth_token
        if not token:
            raise SoundCloudLoginRequired(
                "SoundCloud login is required for this artist-provided download; "
                "run 'dj-digger auth login'"
            )
        try:
            response = self._session.get(
                f"{API_ROOT}/tracks/{track.id}/download",
                params={"client_id": self.client_id},
                headers={"Authorization": f"OAuth {token}"},
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SoundCloudError(
                f"SoundCloud download resolution failed: {log_safe_text(exc)}"
            ) from exc
        try:
            if response.status_code in (401, 403):
                raise SoundCloudTokenRejected(
                    "The saved SoundCloud login expired or was rejected; "
                    "run 'dj-digger auth login' again"
                )
            if response.status_code not in (200, 302):
                raise SoundCloudError(
                    f"SoundCloud returned HTTP {response.status_code} while resolving "
                    "the download"
                )
            if response.status_code == 302:
                return response.headers.get("Location")
            try:
                payload = response.json()
            except ValueError as exc:
                raise SoundCloudError(
                    "SoundCloud returned an unreadable download reply"
                ) from exc
            return payload.get("redirectUri") or payload.get("url")
        finally:
            response.close()

    def _resolve_download_url(
        self, track: Track, gate_url: str | None, session: requests.Session, cancel=None
    ) -> tuple[str, bool]:
        """The URL to fetch and whether a gate produced it (which recolours errors)."""

        download_url: str | None = None
        gate_derived = False

        if gate_url:
            download_url = gates.resolve_gate_download_url(
                gate_url, session, timeout=self._timeout, config=self.config, cancel=cancel
            )
            gate_derived = download_url is not None

        if not download_url and track.has_direct_download and track.download_url:
            download_url = track.download_url

        if not download_url and track.free_download and track.id:
            download_url = self._artist_download_url(track, session)

        if not download_url:
            if gate_url:
                raise SoundCloudError(
                    "Gate link requires browser completion - press 'o' to open"
                )
            raise SoundCloudError("This track has no active direct download or resolved gate link")
        return download_url, gate_derived

    def _open_download(
        self, session: requests.Session, download_url: str
    ) -> tuple[Any, str, int | None]:
        """Start the transfer: the streaming response, its suffix and declared size."""

        host = (urlparse(download_url).hostname or "").lower()
        # Domain-boundary match: "soundcloud.com" in host is also true of
        # evil-soundcloud.com.attacker.net, which would then be handed our
        # client_id along with the request.
        ours = host_matches(host, "soundcloud.com")
        try:
            response, _landed = follow_redirects(
                session,
                download_url,
                params={"client_id": self.client_id} if ours else None,
                timeout=(self._timeout, self._timeout),
                stream=True,
            )
        except UnsafeRedirect as exc:
            raise SoundCloudError(str(exc)) from exc
        except requests.RequestException as exc:
            raise SoundCloudError("Download request failed") from exc
        try:
            if response.status_code >= 400:
                raise SoundCloudError(f"Server returned HTTP {response.status_code} for download")

            content_disp = response.headers.get("Content-Disposition", "")
            content_type = response.headers.get("Content-Type", "")
            try:
                total_size = int(response.headers.get("Content-Length", 0)) or None
            except ValueError:
                total_size = None

            if total_size and total_size > files.MAX_DOWNLOAD_BYTES:
                raise SoundCloudError(
                    f"Refusing a {total_size // (1024 * 1024)} MB download - "
                    f"the limit is {files.MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB"
                )
            # A gate that has not been satisfied answers 200 with its own page
            # rather than a file.
            if content_type.lower().startswith(("text/html", "application/xhtml")):
                raise _WebPageNotFile()
            return response, _extension_for(content_disp, content_type), total_size
        except BaseException:
            response.close()
            raise

    def download_track(
        self,
        track: Track,
        directory: Path,
        *,
        gate_url: str | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
        session: requests.Session | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """Save artist-provided download file directly or via resolved gate URL.

        ``session`` exists for callers downloading several tracks at once. A gate
        is a multi-step flow held together by its own cookies, so two of them
        sharing one jar overwrite each other's state - the same reason
        ``dig._expand_one`` builds a session per track. Left out, this uses the
        client's own, which is right for a single download.
        """

        if session is None:
            transfer = self._injected_transfer_session or create_requests_session()
            try:
                return self.download_track(track, directory, gate_url=gate_url,
                    on_progress=on_progress, session=transfer, cancel=cancel)
            finally:
                if transfer is not self._injected_transfer_session:
                    transfer.close()
        check_cancelled(cancel)
        download_url, gate_derived = self._resolve_download_url(track, gate_url, session, cancel)
        check_cancelled(cancel)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        try:
            response, suffix, total_size = self._open_download(session, download_url)
            try:
                return _save_stream(
                    response, track, directory, suffix, on_progress, total_size, cancel
                )
            finally:
                response.close()
        except (gate_models.GateError, Cancelled):
            raise
        except _WebPageNotFile as exc:
            if gate_derived:
                raise gate_models.GateProtocolChanged(str(exc)) from exc
            raise
        except SoundCloudError as exc:
            if gate_derived:
                raise gate_models.GateDownloadError(str(exc)) from exc
            raise
        except Exception as exc:
            raise SoundCloudError(f"Download failed: {log_safe_text(exc)}") from exc

    def resolve(self, url: str) -> dict[str, Any]:
        payload = self._get("/resolve", url=url)
        if not isinstance(payload, dict):
            raise SoundCloudError(f"Unexpected reply when resolving {url}")
        return payload

    def hydrate_tracks(
        self,
        track_ids: Sequence[int],
        *,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> list[Track]:
        """Turn bare track ids into full track objects, 50 per request."""

        ids = [int(tid) for tid in track_ids]
        if not ids:
            return []

        position = {track_id: index for index, track_id in enumerate(ids)}
        tracks: list[Track] = []
        for chunk in batched(ids, HYDRATE_BATCH):
            check_cancelled(cancel)
            payload = self._get("/tracks", ids=",".join(str(i) for i in chunk))
            if isinstance(payload, list):
                tracks.extend(Track.from_api(item) for item in payload if isinstance(item, dict))
            if on_progress:
                on_progress(len(tracks), len(ids))

        # The endpoint neither preserves order nor returns deleted tracks.
        tracks.sort(key=lambda track: position.get(track.id or -1, len(ids)))
        return tracks

    def _paginate(
        self,
        path: str,
        *,
        limit: int | None = None,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> list[Track]:
        tracks: list[Track] = []
        check_cancelled(cancel)
        payload = self._get(path, limit=PAGE_SIZE)
        while True:
            if not isinstance(payload, dict):
                break
            for item in payload.get("collection") or []:
                if not isinstance(item, dict):
                    continue
                # Likes and reposts wrap the track; /tracks returns it bare.
                data = item.get("track") if "track" in item else item
                if isinstance(data, dict) and data.get("kind") == "track":
                    tracks.append(Track.from_api(data))
            if on_progress:
                on_progress(len(tracks), limit)
            if limit is not None and len(tracks) >= limit:
                break
            next_href = payload.get("next_href")
            if not next_href:
                break
            check_cancelled(cancel)
            payload = self._request(next_href)

        return tracks[:limit] if limit is not None else tracks

    def collect(
        self,
        url: str,
        *,
        limit: int | None = None,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> Crate:
        """Pull every track behind a SoundCloud link."""

        collection, base_url = split_user_collection(url)
        check_cancelled(cancel)
        payload = self.resolve(base_url)
        kind = payload.get("kind")

        if kind == "user":
            username = payload.get("username") or base_url
            user_id = payload.get("id")
            if not user_id:
                raise SoundCloudError(f"Resolved {base_url} to a user without an id")
            endpoint = collection or "tracks"
            tracks = self._paginate(
                f"/users/{user_id}/{endpoint}",
                limit=limit,
                on_progress=on_progress,
                cancel=cancel,
            )
            return Crate(
                source=url,
                tracks=tracks,
                title=f"{username} - {endpoint}",
                declared_count=len(tracks),
            )

        if kind == "track":
            return Crate(
                source=url,
                tracks=[Track.from_api(payload)],
                title=payload.get("title") or url,
                declared_count=1,
            )

        raw_tracks = payload.get("tracks")
        if isinstance(raw_tracks, list):
            track_ids = [
                item["id"]
                for item in raw_tracks
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            ]
            declared = payload.get("track_count") or len(track_ids)
            if limit is not None:
                track_ids = track_ids[:limit]
            tracks = self.hydrate_tracks(track_ids, on_progress=on_progress, cancel=cancel)
            return Crate(
                source=url,
                tracks=tracks,
                title=payload.get("title") or url,
                declared_count=declared,
                provider_id=payload.get("id"),
            )

        raise SoundCloudError(
            f"Nothing diggable behind that link (SoundCloud calls it '{kind}'). "
            "Supported: a playlist, an artist profile, /likes or a single track."
        )


def collect_tracks(
    url: str,
    *,
    limit: int | None = None,
    timeout: float = 20.0,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> Crate:
    """Convenience wrapper for one-shot use."""

    with SoundCloudClient(timeout=timeout) as client:
        return client.collect(url, limit=limit, on_progress=on_progress, cancel=cancel)


def hydrate_ids(
    track_ids: Iterable[int],
    *,
    timeout: float = 20.0,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> list[Track]:
    with SoundCloudClient(timeout=timeout) as client:
        return client.hydrate_tracks(list(track_ids), on_progress=on_progress, cancel=cancel)
