"""Checks of the built-in exercise map (src/jefit2garmin/builtin_exercise_map.toml).

Needs no config.toml or login. Run before each release: .venv/bin/python -m pytest
"""

import re

import pytest

from jefit2garmin.clients.garmin import REJECTED_NAMES, GarminError, fetch_exercise_catalogue
from jefit2garmin.exercise_map import BUILTIN_MAP_FILE, load_builtin_map

ENTRIES = load_builtin_map()  # a malformed file fails here, naming each problem
LINES = BUILTIN_MAP_FILE.read_text(encoding="utf-8").splitlines()


def comments_above(exercise_id: str) -> list[str]:
    """The comment lines directly above an entry (its links)."""
    index = next(i for i, line in enumerate(LINES) if line.startswith(f"{exercise_id} "))
    above = []
    for line in reversed(LINES[:index]):
        if not line.startswith("#"):
            break
        above.append(line)
    return above


def test_has_entries():
    assert ENTRIES


@pytest.mark.parametrize("exercise_id", sorted(ENTRIES))
def test_links_match_the_entry(exercise_id):
    entry = ENTRIES[exercise_id]
    for line in comments_above(exercise_id):
        jefit = re.search(r"jefit\.com/my-jefit/exercises/(\d+)/", line)
        if jefit:
            assert int(jefit.group(1)) == int(exercise_id[2:]), f"Jefit link is for another exercise: {line}"
        garmin = re.search(r"connect\.garmin\.com/app/exercises/(\w+)/(\w+)", line)
        if garmin:
            assert (garmin.group(1), garmin.group(2)) == (entry.category, entry.name or ""), \
                f"Garmin link doesn't match {entry.category}/{entry.name}: {line}"


def test_every_pair_is_accepted_by_garmin():
    try:
        garmin = fetch_exercise_catalogue()  # public, no login
    except GarminError as exc:
        pytest.skip(f"Garmin's exercise list couldn't be downloaded: {exc}")
    bad = []
    for entry in ENTRIES.values():
        if entry.category not in garmin:
            bad.append(f"{entry.exercise_id} {entry.jefit_label}: no category {entry.category}")
        elif entry.name and entry.name in REJECTED_NAMES.get(entry.category, ()):
            bad.append(f'{entry.exercise_id} {entry.jefit_label}: {entry.category}/{entry.name} is refused by Garmin; use name = ""')
        elif entry.name and entry.name not in garmin[entry.category]:
            bad.append(f"{entry.exercise_id} {entry.jefit_label}: {entry.category}/{entry.name} isn't in Garmin's list")
    assert not bad, "\n".join(bad)
