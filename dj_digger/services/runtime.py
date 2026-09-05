"""Lazy application composition and ownership of worker resources."""

import asyncio
import logging
from contextlib import contextmanager
from threading import Condition

from ..config import AppConfig
from ..models import Cancelled
from ..state import TrackState
from .downloads import DownloadService
from .operations import OperationCoordinator

LOGGER = logging.getLogger(__name__)


class ApplicationServices:
    def __init__(self, *, state=None, config=None):
        self._state = state
        self._config = config
        self.operations = OperationCoordinator()
        self._collection = None
        self._downloads = None
        self._library = None
        self._accounts = None
        self._opening = None
        self._client = None
        self._player = None
        self._cart = None
        self._condition = Condition()
        self._active = 0
        self._closing = False
        self._closed = False
        self._retired = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stop()

    @property
    def collection(self):
        if self._collection is None:
            from .collection import CollectionService

            self._collection = CollectionService(lambda: self.state.db)
        return self._collection

    @property
    def state(self):
        if self._state is None:
            self._state = TrackState()
        return self._state

    @property
    def config(self):
        if self._config is None:
            self._config = AppConfig()
        return self._config

    @property
    def opening(self):
        if self._opening is None:
            from .opening import OpeningService

            self._opening = OpeningService(self.state)
        return self._opening

    @property
    def accounts(self):
        if self._accounts is None:
            from .accounts import AccountService

            self._accounts = AccountService(self.config, lambda: self.client.client_id, self.worker)
        return self._accounts

    @property
    def library(self):
        if self._library is None:
            from .library import LibraryService

            self._library = LibraryService(self.state)
        return self._library

    @property
    def downloads(self):
        if self._downloads is None:
            self._downloads = DownloadService(self.state, self.state.db)
        return self._downloads

    @property
    def client(self):
        with self._condition:
            if self._client is None:
                from ..soundcloud import SoundCloudClient

                self._client = SoundCloudClient(config=self.config)
            return self._client

    @property
    def player(self):
        if self._player is None:
            from ..player import Player

            self._player = Player()
        return self._player

    @property
    def cart(self):
        if self._cart is None:
            from dj_digger.services.purchases import CartBrowserSession

            self._cart = CartBrowserSession()
        return self._cart

    @contextmanager
    def worker(self):
        with self._condition:
            if self._closing:
                raise Cancelled("Application is closing")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()
                if not self._active:
                    if self._closing:
                        self._close_sync()
                    else:
                        self._close_retired()

    def adopt_login(self, oauth_token):
        from ..soundcloud import SoundCloudClient

        self.retire_client(SoundCloudClient(config=self.config))
        return True

    async def io(self, function, *args, **kwargs):
        """Await actual thread completion even if the awaiting coroutine is cancelled."""

        def call():
            with self.worker():
                return function(*args, **kwargs)

        task = asyncio.create_task(asyncio.to_thread(call))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            # Retrieve failures, too, before reporting cancellation to the parent.
            try:
                task.result()
            finally:
                raise asyncio.CancelledError
        return task.result()

    def retire_client(self, new_client):
        with self._condition:
            if self._client is not None:
                self._retired.append(self._client)
            self._client = new_client
            if not self._active:
                self._close_retired()

    @staticmethod
    def _close_resource(resource):
        try:
            resource.close()
        except Exception as exc:
            LOGGER.warning("Resource close failed (%s)", type(exc).__name__)

    def _close_retired(self):
        for client in self._retired:
            self._close_resource(client)
        self._retired.clear()

    def _close_sync(self):
        if self._closed:
            return
        self._closed = True
        if self._player is not None:
            self._close_resource(self._player)
        from ..local_audio import close_all
        close_all()
        if self._client is not None:
            self._close_resource(self._client)
            self._client = None
        self._close_retired()
        if self._state is not None:
            self._close_resource(self._state.db)

    def stop(self):
        self.operations.stop_accepting()
        with self._condition:
            self._closing = True
            if not self._active:
                self._close_sync()
