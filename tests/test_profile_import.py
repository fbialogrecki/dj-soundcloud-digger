
import pytest

from dj_digger.db import Database
from dj_digger.services.profile_import import import_profile
from dj_digger.soundcloud import API_ROOT
from dj_digger.soundcloud_errors import SoundCloudError


class Client:
    oauth_token = 'test-token'

    def __init__(self):
        self.pages = 0
        self.hydrations = 0
        self.ids = [1, 2, 1]
        self.repeat = False
        self.private_owner = 5
        self.missing = False
        self.title = 'Playlist'
        self.url = 'https://soundcloud.com/owner/sets/list'
        self.hook = lambda: None

    def resolve(self, url):
        return dict(id=5, kind='user')

    def _request(self, url, params):
        self.pages += 1
        return {'collection': [{'id': 10, 'user_id': 5, 'sharing': 'public'}],
                'next_href': f'{API_ROOT}/users/5/playlists' if self.repeat else None}

    def _get(self, url, **params):
        if url == '/me':
            return {'id': self.private_owner}
        if url == '/tracks':
            self.hydrations += 1
            self.hook()
            ids = [int(value) for value in params['ids'].split(',')]
            return [dict(id=value, title=str(value), permalink_url=f'https://soundcloud.com/owner/{value}') for value in reversed(ids) if not (self.missing and value == 2)]
        return dict(id=10, title=self.title, track_count=len(self.ids), tracks=[{'id': value} for value in self.ids], permalink_url=self.url)


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / 'test.db')
    yield value
    value.close()


def test_order_duplicates_and_permalink_change(db):
    client = Client()
    assert import_profile(client, db, 'https://soundcloud.com/owner')[0]['status'] == 'imported'
    assert [track['id'] for track in db.load_crate(client.url)['tracks']] == [1, 2, 1]
    old_url = client.url
    client.url = 'https://soundcloud.com/owner/sets/new-name'
    import_profile(client, db, 'https://soundcloud.com/owner')
    assert len(db.list_crate_headers()) == 1
    assert db.load_crate(old_url)['preserve_order']


def test_missing_response_retains_complete_snapshot(db):
    client = Client()
    import_profile(client, db, 'https://soundcloud.com/owner')
    client.missing = True
    assert import_profile(client, db, 'https://soundcloud.com/owner')[0]['status'] == 'incomplete'
    assert len(db.load_crate(client.url)['tracks']) == 3


def test_owner_and_session_guards(db):
    client = Client()
    client.private_owner = 6
    with pytest.raises(SoundCloudError, match='owner'):
        import_profile(client, db, 'https://soundcloud.com/owner', private=True)
    client.hook = lambda: setattr(client, 'oauth_token', 'changed')
    with pytest.raises(SoundCloudError, match='session changed'):
        import_profile(client, db, 'https://soundcloud.com/owner')
    assert db.list_crate_headers() == []


def test_repeated_cursor_terminates(db):
    client = Client()
    client.repeat = True
    with pytest.raises(SoundCloudError, match='cursor'):
        import_profile(client, db, 'https://soundcloud.com/owner')
    assert client.pages == 1


def test_deleting_during_hydration_is_not_undone(db):
    client = Client()
    import_profile(client, db, 'https://soundcloud.com/owner')
    client.hook = lambda: db.delete_crate(client.url)
    assert import_profile(client, db, 'https://soundcloud.com/owner')[0]['status'] == 'locally_changed'
    assert db.load_crate(client.url) is None
