"""Checks that the Garmin client still works against the real service (read only).

Uses the credentials in userdata/config.toml; skipped without it. Login tokens are kept where
the app keeps them (userdata/state/garmin_tokens/), so you log in (and enter an MFA code, if
Garmin asks) only once; for the MFA prompt to show, run: .venv/bin/python -m pytest -s
"""

import tomllib
from datetime import date, timedelta
from pathlib import Path

import pytest

from jefit2garmin.clients.garmin import GarminClient, fetch_exercise_catalogue

USERDATA = Path(__file__).resolve().parents[1] / "userdata"
CONFIG_FILE = USERDATA / "config.toml"
if not CONFIG_FILE.exists():
    pytest.skip("needs userdata/config.toml (run jefit2garmin once to create it)", allow_module_level=True)
CONFIG = tomllib.loads(CONFIG_FILE.read_text())


def logged_in_client() -> GarminClient:
    client = GarminClient(
        CONFIG["garmin"]["email"],
        CONFIG["garmin"]["password"],
        USERDATA / "state" / "garmin_tokens",
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    client.login()
    return client


def test_login():
    client = logged_in_client()
    assert client.logged_in


def test_download_strength_activities():
    client = logged_in_client()
    today = date.today()
    activities = client.strength_activities(today - timedelta(days=60), today)
    assert all(a.type_key == "strength_training" for a in activities)


def test_download_exercise_list():
    exercises = fetch_exercise_catalogue()  # public; needs no login
    assert exercises["SQUAT"]["BARBELL_BACK_SQUAT"].display == "Barbell Back Squat"
