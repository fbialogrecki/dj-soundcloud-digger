"""Owner playlist import without expensive external shop/gate resolution."""
from collections import OrderedDict
from itertools import batched
from threading import Event
from urllib.parse import urlparse

from ..crate_models import CrateRecord
from ..models import Crate, Track, check_cancelled
from ..soundcloud import (
    API_ROOT,
    HYDRATE_BATCH,
    SoundCloudClient,
    create_requests_session,
    is_soundcloud_url,
)
from ..soundcloud_errors import SoundCloudError


def _import_profile(client, db, url, *, private=False, cancel=None, current=lambda: True, progress=lambda done: None, _report=None):
    if not is_soundcloud_url(url):
        raise SoundCloudError('Paste a SoundCloud profile URL')
    generations = db.snapshot_generations()
    token = client.oauth_token

    def check():
        check_cancelled(cancel)
        if not current() or client.oauth_token != token:
            raise SoundCloudError('SoundCloud session changed; import stopped')

    owner = client.resolve(url)
    check()
    if owner.get('kind') != 'user' or not owner.get('id'):
        raise SoundCloudError('This link is not a SoundCloud profile')
    if private:
        if not token:
            raise SoundCloudError('Sign into the profile owner account to import private playlists')
        me = client._get('/me')
        check()
        if me.get('id') != owner['id']:
            raise SoundCloudError('Private playlists require the signed-in profile owner')
    # Resolve legacy URL-only entries once before importing their renamed IDs.
    # No title-based merge: only a provider-confirmed playlist/owner is accepted.
    profile_path = urlparse(url).path.rstrip('/') + '/sets/'
    for source in db.unaliased_playlists():
        if is_soundcloud_url(source) and urlparse(source).path.startswith(profile_path):
            check()
            try:
                legacy = client.resolve(source)
            except SoundCloudError:
                continue
            check()
            if legacy.get('kind') == 'playlist' and (legacy.get('user_id') or (legacy.get('user') or {}).get('id')) == owner['id']:
                db.link_playlist_alias(legacy['id'], source, generations)
    endpoint = f"{API_ROOT}/users/{owner['id']}/playlists"
    cursors, playlist_ids, cache = set(), set(), OrderedDict()
    report = _report if _report is not None else []
    while endpoint:
        check()
        if endpoint in cursors:
            raise SoundCloudError('SoundCloud repeated a page cursor; existing playlists have been kept')
        cursors.add(endpoint)
        payload = client._request(endpoint, {'limit': 200, 'linked_partitioning': 1})
        check()
        if not isinstance(payload, dict) or not isinstance(payload.get('collection'), list):
            raise SoundCloudError('Incomplete playlist page; existing playlists have been kept')
        before_page = len(playlist_ids)
        for header in payload['collection']:
            check()
            identifier = header.get('id')
            if identifier in playlist_ids:
                continue
            if not identifier or (header.get('user_id') or (header.get('user') or {}).get('id')) != owner['id']:
                raise SoundCloudError('Playlist owner or ID missing from response')
            playlist_ids.add(identifier)
            if not private and header.get('sharing') == 'private':
                continue
            detail = client._get(f'/playlists/{identifier}')
            check()
            raw = detail.get('tracks')
            count = detail.get('track_count')
            if not isinstance(raw, list) or count is None or len(raw) != count:
                report.append({'id': identifier, 'status': 'incomplete', 'message': 'Old playlist retained'})
                continue
            ids = [item.get('id') for item in raw]
            if any(not isinstance(value, int) for value in ids):
                report.append({'id': identifier, 'status': 'incomplete', 'message': 'Missing track IDs; old playlist retained'})
                continue
            missing = list(dict.fromkeys(value for value in ids if value not in cache))
            resolved = {}
            for chunk in batched(missing, HYDRATE_BATCH):
                check()
                tracks = client._get('/tracks', ids=','.join(map(str, chunk)))
                check()
                if not isinstance(tracks, list):
                    raise SoundCloudError('Incomplete track hydration response')
                for item in tracks:
                    if isinstance(item, dict) and item.get('id') in chunk:
                        resolved[item['id']] = Track.from_api(item)
            unavailable = [value for value in ids if value not in resolved and value not in cache]
            if unavailable:
                # A missing stub cannot safely be distinguished from partial API
                # failure. Report it, preserve the complete old snapshot.
                report.append({'id': identifier, 'status': 'incomplete', 'missing_tracks': unavailable})
                continue
            tracks = [resolved[value] if value in resolved else cache[value] for value in ids]
            cache.update(resolved)
            while len(cache) > 1000:
                cache.popitem(last=False)
            source = detail.get('permalink_url')
            if not is_soundcloud_url(source or ''):
                raise SoundCloudError('Playlist permalink missing')
            incoming = CrateRecord.from_crate(Crate(source, tracks, detail.get('title') or source)).to_json()
            incoming['preserve_order'] = True
            incoming['provider_id'] = identifier
            check()
            saved = db.remember_provider_playlist(identifier, incoming, generations)
            report.append({'id': identifier, 'status': 'imported' if saved else 'locally_changed', 'tracks': len(tracks)})
            progress(len(report))
        endpoint = payload.get('next_href')
        if endpoint and len(playlist_ids) == before_page:
            raise SoundCloudError('SoundCloud pagination made no progress; import stopped')
    return report


class _ImportClient(SoundCloudClient):
    """Import-only retry policy: bounded, interruptible exponential backoff."""
    def _request(self, url, params=None):
        for attempt in range(4):
            check_cancelled(self.import_cancel)
            try:
                return super()._request(url, params)
            except SoundCloudError as exc:
                if exc.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
                if self.import_cancel.wait(min(8, 2 ** attempt)):
                    check_cancelled(self.import_cancel)
        raise AssertionError('unreachable')


def import_profile(client, db, url, *, private=False, cancel=None, current=lambda: True, progress=lambda done: None):
    report = []

    def perform(adapter, stop, valid):
        try:
            return _import_profile(adapter, db, url, private=private, cancel=stop, current=valid, progress=progress, _report=report)
        except SoundCloudError as exc:
            exc.report = report
            raise

    if not isinstance(client, SoundCloudClient):
        return perform(client, cancel, current)
    token = client.oauth_token
    with _ImportClient(session=create_requests_session(max_retries=0), client_id=client.client_id,
                       oauth_token=token or '', config=client.config, timeout=client._timeout) as isolated:
        isolated.import_cancel = cancel if cancel is not None else Event()
        return perform(isolated, isolated.import_cancel, lambda: current() and client.oauth_token == token)
