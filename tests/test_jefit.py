"""Checks that the Jefit client still works against the real service (read only).

Uses the credentials in userdata/config.toml; skipped without it. Run: .venv/bin/python -m pytest
"""

import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jefit2garmin.clients.jefit import JefitClient

CONFIG_FILE = Path(__file__).resolve().parents[1] / "userdata" / "config.toml"
if not CONFIG_FILE.exists():
    pytest.skip("needs userdata/config.toml (run jefit2garmin once to create it)", allow_module_level=True)
CONFIG = tomllib.loads(CONFIG_FILE.read_text())
TIME_ZONE = ZoneInfo(CONFIG["time_zone"])


def logged_in_client() -> JefitClient:
    client = JefitClient(CONFIG["jefit"]["username"], CONFIG["jefit"]["password"])
    client.login()
    return client


def test_login():
    client = logged_in_client()
    assert client.user_id > 0


def test_download_latest_workout():
    client = logged_in_client()
    days = client.workout_dates(TIME_ZONE)
    assert days, "no workouts in the calendar"
    sessions = client.sessions_on(days[-1], TIME_ZONE)
    assert sessions, f"no session returned for {days[-1]}"
    assert any(log.sets for log in sessions[0].logs), "latest workout has no sets"


def test_download_exercise_list():
    client = logged_in_client()
    exercises = client.exercise_catalogue()
    assert len(exercises) > 1000
