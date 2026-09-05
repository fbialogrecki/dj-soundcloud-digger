"""What the app remembers about you, in ~/.config/dj-digger/config.json.

Your name and email, which the gate resolvers submit on your behalf; the
comments they leave; where to look for music you already own; and which browser
opens links.
"""

import json
import logging
import random
import re
from pathlib import Path

from .paths import config_dir
from .private_json import write_private_json

LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Music Listener"
# ``gates`` submits this address to third-party download gates automatically, so
# the default must not be deliverable to anybody. RFC 2606 reserves .invalid for
# exactly this. The previous default was a real-looking address at a real
# provider, which meant every unconfigured install was signing a stranger up for
# artist mailing lists - so it is retired rather than merely changed.
DEFAULT_EMAIL = "dj-digger@example.invalid"
RETIRED_EMAILS = frozenset({"music.listener@yahoo.com"})
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEFAULT_COMMENTS = ["Love it!", "Amazing track!", "Dope tune!", "Fire!", "Banger!", "Great tune!"]

# The attributes save() persists; load() coerces each one on the way back in.
PERSISTED_FIELDS = (
    "user_name",
    "user_email",
    "custom_comments",
    "scan_directories",
    "browser",
    "download_directory",
    "gate_social_actions",
    "columns",
    "theme",
    "pinned_directories",
    "sidebar_split",
    "sidebar_mode",
)

# Optional track-table columns, in the order they appear when switched on.
OPTIONAL_COLUMNS = ("bpm", "key", "year", "label")


def is_real_email(value: str) -> bool:
    email = value.strip()
    return bool(EMAIL_RE.fullmatch(email)) and not email.lower().endswith(".invalid")


def default_config_path() -> Path:
    return config_dir() / "config.json"


class AppConfig:
    """User profile and preferences, persisted as JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_config_path()
        self.user_name: str = DEFAULT_NAME
        self.user_email: str = DEFAULT_EMAIL
        self.custom_comments: list[str] = list(DEFAULT_COMMENTS)
        self.scan_directories: list[str] = [
            str(Path.home() / "Music"),
            str(Path.home() / "Downloads"),
        ]
        # Where `d` and `D` put the files they fetch. It was ~/Downloads written
        # into two places in the download code, which is not where anybody with a
        # sorted collection wants their records to land.
        self.download_directory: str = str(Path.home() / "Downloads")
        # Empty means the system default. Anything else is checked against what
        # the machine reports before it is used - see browser.resolve_choice.
        self.browser: str = ""
        # Whether a gate may record a repost, a follow and a comment against your
        # SoundCloud account in exchange for the file. Every version up to 0.8
        # did this and said so nowhere: `is_repost` and `is_subscribe` were
        # hard-coded into the Hypeddit step calls, and only the comment text was
        # ever visible in Settings. On by default, because turning it off is what
        # changes behaviour - but now it is a sentence on the first-run screen
        # rather than a line in somebody else's source.
        self.gate_social_actions: bool = True
        # Which of OPTIONAL_COLUMNS the track table shows. Off by default: the
        # title column is what an 80-column terminal has room for.
        self.columns: list[str] = []
        # Textual theme name; empty means Textual's default.
        self.theme: str = ""
        self.pinned_directories: list[str] = []
        self.sidebar_split: int = 50
        self.sidebar_mode: str = "both"
        # True when there was no config file to read, i.e. this is the first
        # launch. The TUI uses it to ask for the settings before anything needs
        # them - gates submit the name and email without asking again.
        self.first_run: bool = False
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.user_name = str(raw.get("user_name") or DEFAULT_NAME).strip()
                self.user_email = str(raw.get("user_email") or DEFAULT_EMAIL).strip()
                if self.user_email.lower() in RETIRED_EMAILS:
                    # Saved by an older version that shipped a stranger's address
                    # as the default. Nobody chose it, so it does not survive.
                    self.user_email = DEFAULT_EMAIL
                comments = raw.get("custom_comments")
                if isinstance(comments, list) and comments:
                    cleaned = [str(c).strip() for c in comments if str(c).strip()]
                    if cleaned:
                        self.custom_comments = cleaned
                scan_dirs = raw.get("scan_directories")
                if isinstance(scan_dirs, list) and scan_dirs:
                    self.scan_directories = [str(d).strip() for d in scan_dirs if str(d).strip()]
                self.browser = str(raw.get("browser") or "").strip()
                download_dir = str(raw.get("download_directory") or "").strip()
                if download_dir:
                    self.download_directory = download_dir
                if "gate_social_actions" in raw:
                    self.gate_social_actions = bool(raw["gate_social_actions"])
                self.theme = str(raw.get("theme") or "").strip()
                self.pinned_directories = [str(value) for value in raw.get('pinned_directories', []) if isinstance(value, str)] if isinstance(raw.get('pinned_directories'), list) else []
                self.sidebar_mode = raw.get("sidebar_mode") if raw.get("sidebar_mode") in ("both", "playlists", "explorer") else "both"
                self.sidebar_split = raw.get('sidebar_split') if raw.get('sidebar_split') in (30, 50, 70) else 50
                columns = raw.get("columns")
                if isinstance(columns, list):
                    self.columns = [
                        name for name in OPTIONAL_COLUMNS if name in {str(c) for c in columns}
                    ]
        except FileNotFoundError:
            self.first_run = True
            self.save()
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not load config from %s: %s", self.path, exc)

    def save(self) -> None:
        payload = {name: getattr(self, name) for name in PERSISTED_FIELDS}
        try:
            # The 0600 write is deliberate: the profile email lives here.
            write_private_json(self.path, payload, ensure_ascii=False)
        except OSError as exc:
            LOGGER.warning("Could not save config to %s: %s", self.path, exc)

    def random_comment(self) -> str:
        pool = self.custom_comments or DEFAULT_COMMENTS
        return random.choice(pool)

    def has_real_email(self) -> bool:
        """False while the profile still carries the unroutable placeholder."""

        return is_real_email(self.user_email)
