"""Interactive crate browser.

Opening every link at once means 287 browser tabs on a big playlist, which is
not a workflow. This screen lets you walk the list, open one link at a time,
filter down to a single store and mark what you already own - and the marks
survive between runs because they live in ``state.TrackState``.

One row is one track, not one link. A track selling on Bandcamp and gated on
Hypeddit is a single decision, so it gets a single row with a badge per store;
the store filter doubles as the way to say which of them ``o`` should follow.

It can also start from nothing: with no records it asks for a link, digs it in a
worker thread so the interface stays responsive, and fills itself in.
"""

import logging
import os
import signal
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from ..crate_models import CrateRecord
from ..models import LinkRecord
from ..services import collection as dig_module
from ..services.runtime import ApplicationServices
from ..state import TrackState
from .app import DiggerApp

LOGGER = logging.getLogger(__name__)

# How long a finished app waits for its background threads before leaving
# without them. A dig or a download that is mid-request cannot be interrupted;
# asyncio would otherwise join it for up to five minutes with the terminal
# already restored and nothing on screen to say why.
EXIT_GRACE = 3.0
# Swapped out by the tests, where a real os._exit would take pytest with it.
HARD_EXIT = os._exit


def _lingering_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread is not threading.main_thread() and not thread.daemon and thread.is_alive()
    ]


def _finish_or_exit(grace: float, code: int) -> None:
    """Return once the worker threads are gone, or end the process after ``grace``."""

    deadline = time.monotonic() + grace
    while _lingering_threads():
        if time.monotonic() >= deadline:
            names = ", ".join(thread.name for thread in _lingering_threads())
            LOGGER.warning("Forcing exit: %s still running", names)
            logging.shutdown()
            from ..media_processes import terminate_owned
            terminate_owned()
            HARD_EXIT(code)
            return
        time.sleep(0.1)


def _interrupt_again(_signum, _frame) -> None:
    # Only reachable once Textual has restored the terminal: while it owns the
    # screen ctrl+c is a key, not a signal. So this is the second ctrl+c, from
    # someone watching a shutdown that is taking too long.
    LOGGER.warning("Interrupted again during shutdown, exiting now")
    logging.shutdown()
    from ..media_processes import terminate_owned
    terminate_owned()
    HARD_EXIT(130)


def run_tui(
    records: Sequence[LinkRecord] = (),
    *,
    state: TrackState | None = None,
    services: ApplicationServices | None = None,
    crate_title: str = "",
    export_format: str = "json",
    export_path: Path | None = None,
    dig_options: dig_module.DigOptions | None = None,
    crate_record: CrateRecord | None = None,
    keep_logging: bool = False,
    grace: float = EXIT_GRACE,
) -> None:
    services = services or ApplicationServices(state=state)
    # The guard must run during asyncio's teardown, before app.run can return.
    # It never closes resources underneath a live worker.
    def force_shutdown():
        LOGGER.warning("Forcing exit: application teardown exceeded its deadline")
        from ..media_processes import terminate_owned
        terminate_owned()
        HARD_EXIT(0)

    guard = threading.Timer(grace, force_shutdown)
    guard.daemon = True
    shutdown_deadline = None

    def shutdown_started():
        nonlocal shutdown_deadline
        if shutdown_deadline is None:
            shutdown_deadline = time.monotonic() + grace
            guard.start()

    app = DiggerApp(
        records,
        shutdown_started=shutdown_started,
        state=state,
        services=services,
        crate_title=crate_title,
        export_format=export_format,
        export_path=export_path,
        dig_options=dig_options,
        crate_record=crate_record,
    )
    # Textual draws the interface on stderr, so anything logged to the terminal
    # lands in the middle of the crate list: our own records at any level, and at
    # DEBUG the libraries' too, since that level puts a handler on the root
    # logger. Both are muted for as long as the app owns the screen - unless
    # ``keep_logging`` says --log-file has given the log somewhere else to go, in
    # which case silencing it is the opposite of what was asked for.
    silenced = [] if keep_logging else [logging.getLogger("dj_digger"), logging.getLogger()]
    levels = [(logger, logger.level) for logger in silenced]
    for logger in silenced:
        logger.setLevel(logging.CRITICAL + 1)
    previous_handler = None
    on_main_thread = threading.current_thread() is threading.main_thread()
    if on_main_thread:
        previous_handler = signal.signal(signal.SIGINT, _interrupt_again)
    code = 0
    try:
        app.run()
    except KeyboardInterrupt:
        code = 130
        raise
    finally:
        for logger, level in levels:
            logger.setLevel(level)
        if on_main_thread:
            signal.signal(signal.SIGINT, previous_handler)
        try:
            services.stop()
        finally:
            guard.cancel()
        remaining = max(0, shutdown_deadline - time.monotonic()) if shutdown_deadline is not None else grace
        _finish_or_exit(remaining, code)
