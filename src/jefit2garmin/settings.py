"""Settings from config.toml, and the options given on the command line.

Users edit config.toml, never this file; the first run creates it from CONFIG_TEMPLATE below.
The folder config.toml is in (userdata/ by default) holds everything of the user's:

    userdata/
      config.toml          the user's settings
      exercise_map.toml    the user's exercise mapping
      logs/                jefit2garmin.log, rotated at 1 MB (kept by --reset)
      state/               the app's own files; --reset deletes this folder
        sync_state.db, garmin_exercises.md, jefit_cache.json, garmin_cache.json, garmin_tokens/
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# What a new config.toml starts as (written by the first run).
CONFIG_TEMPLATE = """\
# jefit2garmin settings. Fill in your Jefit and Garmin logins; everything else is optional.
# This file holds your passwords: keep it private (userdata/ is kept out of git).

# Time zone for activities jefit2garmin creates in Garmin Connect.
time_zone = "America/Los_Angeles"

# Optional: cached copies of the Jefit and Garmin exercise lists and your Jefit routines are
# downloaded again once older than this many days. Remove the # to change it.
# cache_max_days = 30

[jefit]
username = ""
password = ""

[garmin]
email = ""
password = ""

# --- Optional. Remove the # in front of a line to change it from the default shown. ---

[sync]
# Days looked at on a run without --date/--since/--until.
# window_days = 28

# Hours to wait for the watch to sync before creating an activity for a workout.
# create_after_hours = 24

# Ask before one run creates more activities than this (--yes skips the question).
# confirm_creates_over = 5

# Name given to activities jefit2garmin creates.
# activity_name = "Strength"
"""

# The optional [sync] keys and the type each must have. Defaults are on Settings below.
SYNC_KEYS = {"window_days": int, "create_after_hours": int, "confirm_creates_over": int, "activity_name": str}


@dataclass(frozen=True)
class Settings:
    """Everything read from config.toml."""

    jefit_username: str
    jefit_password: str
    garmin_email: str
    garmin_password: str
    time_zone: ZoneInfo
    data_dir: Path  # the folder config.toml is in (userdata/)
    cache_max_days: int = 30  # cached exercise lists and routines older than this are downloaded again

    # [sync] in config.toml; these are the defaults when a key is left out
    window_days: int = 28  # days looked at on every run
    create_after_hours: int = 24  # give the watch this long to sync before creating an activity
    confirm_creates_over: int = 5  # ask before a run creates more activities than this
    activity_name: str = "Strength"  # name of activities the app creates

    @classmethod
    def load(cls, config_file: Path) -> Settings:
        """Read config.toml. Raises ValueError naming the setting that is wrong."""
        data = tomllib.loads(config_file.read_text())
        for section, keys in (("jefit", ("username", "password")), ("garmin", ("email", "password"))):
            if not all(str(data.get(section, {}).get(key, "")).strip() for key in keys):
                raise ValueError(f"{config_file}: fill in your {section.capitalize()} {' and '.join(keys)} under [{section}]")
        cache_max_days = data.get("cache_max_days", cls.cache_max_days)
        if not isinstance(cache_max_days, int) or isinstance(cache_max_days, bool) or cache_max_days < 0:
            raise ValueError(f"{config_file}: cache_max_days must be a whole number of 0 or more, not {cache_max_days!r}")

        sync = data.get("sync", {})
        for key, value in sync.items():
            if key not in SYNC_KEYS:
                raise ValueError(f"{config_file}: unknown setting [sync] {key} (known: {', '.join(SYNC_KEYS)})")
            if not isinstance(value, SYNC_KEYS[key]) or isinstance(value, bool):
                kind = "a whole number" if SYNC_KEYS[key] is int else "text in quotes"
                raise ValueError(f"{config_file}: [sync] {key} must be {kind}, not {value!r}")
            if value == "" or isinstance(value, int) and value < 0:
                raise ValueError(f"{config_file}: [sync] {key} can't be {value!r}")

        return cls(
            jefit_username=data["jefit"]["username"],
            jefit_password=data["jefit"]["password"],
            garmin_email=data["garmin"]["email"],
            garmin_password=data["garmin"]["password"],
            time_zone=ZoneInfo(data["time_zone"]),
            data_dir=config_file.resolve().parent,
            cache_max_days=cache_max_days,
            **sync,
        )

    @property
    def create_after(self) -> timedelta:
        return timedelta(hours=self.create_after_hours)

    @property
    def cache_max_age(self) -> timedelta:
        return timedelta(days=self.cache_max_days)

    # The user's files, next to config.toml
    @property
    def exercise_map_file(self) -> Path:
        return self.data_dir / "exercise_map.toml"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"  # kept by --reset

    # The app's own files; --reset deletes this whole folder
    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def sync_state_file(self) -> Path:
        return self.state_dir / "sync_state.db"

    @property
    def garmin_reference_file(self) -> Path:
        return self.state_dir / "garmin_exercises.md"

    @property
    def garmin_token_dir(self) -> Path:
        return self.state_dir / "garmin_tokens"


@dataclass(frozen=True)
class Options:
    """What the command line asked for."""

    since: date | None = None  # first day to sync; None with `until` None = the default window
    until: date | None = None  # last day to sync
    dry_run: bool = False  # show what would change; write nothing to Garmin or the sync record
    assume_yes: bool = False  # don't ask before creating many activities
    overwrite_garmin_edits: bool = False  # replace sets even if they were edited in Garmin Connect
    refresh_caches: bool = False  # download the exercise lists and routines again, whatever their age
