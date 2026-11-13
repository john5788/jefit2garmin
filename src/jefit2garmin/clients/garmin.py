"""Garmin Connect access for jefit2garmin, on top of python-garminconnect.

Only the handful of calls this project needs, with consistent errors (`GarminError` naming the
call), pacing between requests, and retry with backoff when Garmin rate-limits (429).

Exercise sets are identified by a Garmin `category` plus `name` (the specific exercise within
the category, or None for the generic category entry). Valid pairs come from Garmin's public
exercise list; Garmin rejects a whole write if any pair is invalid. `GarminClient.exercise_list()`
keeps a local copy (see disk_cache.py), so a run can go on if Garmin stops publishing it;
`fetch_exercise_catalogue()` always downloads.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

import requests
from garminconnect import Garmin, GarminConnectNotFoundError, GarminConnectTooManyRequestsError

from .disk_cache import DiskCache

log = logging.getLogger(__name__)  # calls are logged by name only, never with their data

# --- Endpoints -----------------------------------------------------------------------------------
# Account calls go through python-garminconnect, which owns those URLs:
#   get_activities_by_date       GET    /activitylist-service/activities/search/activities
#   get_activity_exercise_sets   GET    /activity-service/activity/{id}/exerciseSets
#   set_activity_exercise_sets   PUT    /activity-service/activity/{id}/exerciseSets
#   create_manual_activity       POST   /activity-service/activity
#   set_activity_description     PUT    /activity-service/activity/{id}
#   delete_activity              DELETE /activity-service/activity/{id}
# The exercise catalogue is public and fetched directly:

EXERCISES_URL = "https://connect.garmin.com/web-data/exercises/Exercises.json"
DISPLAY_NAMES_URL = "https://connect.garmin.com/web-translations/exercise_types/exercise_types.properties"

STRENGTH = "strength_training"

# Found by testing against Garmin Connect (2026-10-15): the public exercise list has an entry
# named after its own category in these categories, e.g. CALF_RAISE/CALF_RAISE "Calf Raise", but
# writing it is refused with "API Error 400 - Invalid Sub-Category". It stands for the category's
# generic exercise, which Garmin takes as the category with no name. These entries are left out
# of the exercise list. When a write is refused for another pair, add it here.
#
# Not every same-named entry is affected: LEG_CURL/LEG_CURL, SQUAT/SQUAT, PLANK/PLANK and others
# are real exercises and are accepted.
REJECTED_NAMES: dict[str, set[str]] = {
    category: {category}
    for category in (
        "BATTLE_ROPE", "BENCH_PRESS", "CALF_RAISE", "CARRY", "CHOP", "CORE", "CURL", "DEADLIFT",
        "HIP_STABILITY", "HIP_SWING", "HYPEREXTENSION", "LEG_RAISE", "SHOULDER_STABILITY", "SHRUG",
        "SLEDGE_HAMMER", "TIRE",
    )
}


class GarminError(RuntimeError):
    """A Garmin call failed or returned something unexpected."""


class GarminNotFoundError(GarminError):
    """The activity asked for does not exist (for example, deleted in Garmin Connect)."""


# --- Data ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Activity:
    id: int
    name: str
    type_key: str
    start: datetime  # UTC
    duration_s: float
    raw: dict = field(repr=False, compare=False)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_s)


@dataclass(frozen=True)
class ExerciseSet:
    """One entry of an activity's exercise set list.

    `step` groups the sets of one exercise (Garmin's wktStepIndex). For REST entries only
    `start`, `duration_s` and `step` matter.
    """

    kind: Literal["ACTIVE", "REST"]
    start: datetime  # timezone-aware
    duration_s: float
    step: int
    category: str | None = None
    name: str | None = None  # None = generic entry for the category
    reps: int | None = None
    weight_g: float | None = None  # None for bodyweight and timed sets


@dataclass(frozen=True)
class GarminExercise:
    category: str
    name: str
    display: str
    primary_muscles: tuple[str, ...]
    secondary_muscles: tuple[str, ...]


@dataclass(frozen=True)
class GarminExerciseList:
    """Every valid category/name pair, as of `fetched_at`."""

    categories: dict[str, dict[str, GarminExercise]]  # category -> name -> exercise
    fetched_at: datetime

    def is_valid(self, category: str, name: str | None) -> bool:
        """`name` None or empty means the generic entry for the category."""
        return category in self.categories and (not name or name in self.categories[category])

    def display(self, category: str, name: str | None) -> str:
        """The name Garmin Connect shows; the category for a generic entry."""
        if name and self.is_valid(category, name):
            return self.categories[category][name].display
        return category

    def all(self) -> Iterator[GarminExercise]:
        for exercises in self.categories.values():
            yield from exercises.values()


# --- Payload -------------------------------------------------------------------------------------


def exercise_sets_payload(activity_id: int, sets: list[ExerciseSet]) -> dict[str, Any]:
    """The body Garmin's exerciseSets endpoint expects. `messageIndex` follows list order."""
    entries = []
    for index, s in enumerate(sets):
        entry: dict[str, Any] = {
            "exercises": [],
            "setType": s.kind,
            "duration": float(round(s.duration_s, 3)),
            "startTime": s.start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0"),
            "wktStepIndex": s.step,
            "messageIndex": index,
        }
        if s.kind == "ACTIVE":
            # probability 0 makes Connect show the exercise as "Unknown"; its own edits send 100.
            entry["exercises"] = [{"category": s.category, "name": s.name, "probability": 100}]
            entry["repetitionCount"] = s.reps
            entry["weight"] = None if s.weight_g is None else float(round(s.weight_g))
        entries.append(entry)
    return {"activityId": activity_id, "exerciseSets": entries}


# --- Client --------------------------------------------------------------------------------------


class GarminClient:
    """One Garmin Connect account.

    Logs in on the first account call, not before; `exercise_list()` needs no login. Login
    tokens are kept in `token_dir` so later runs skip the password and MFA.
    """

    CACHE_FILE = "garmin_cache.json"  # in cache_dir

    def __init__(
        self,
        email: str,
        password: str,
        token_dir: Path,
        *,
        cache_dir: Path | None = None,
        max_cache_age: timedelta = timedelta(days=30),
        refresh_cache: bool = False,
        warn: Callable[[str], None] | None = None,
        prompt_mfa: Callable[[], str] | None = None,
        min_interval: float = 1.0,
        max_retries: int = 4,
    ) -> None:
        """
        Args:
            email, password: The Garmin Connect account.
            token_dir: Where login tokens are kept between runs.
            cache_dir: Folder for the local cache file; None keeps it in memory for one run.
            max_cache_age: Cached copies older than this are downloaded again.
            refresh_cache: Download every cached item once, whatever its age.
            warn: Called when a download fails and a cached copy is used instead.
            prompt_mfa: Called for a code when Garmin asks for one.
            min_interval, max_retries: Request pacing and retries.
        """
        self._email = email
        self._password = password
        self._token_dir = Path(token_dir)
        self._prompt_mfa = prompt_mfa
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._api: Garmin | None = None
        self._last_request = 0.0
        self._cache = DiskCache(
            cache_dir / self.CACHE_FILE if cache_dir else None,
            max_age=max_cache_age,
            refresh_all=refresh_cache,
            warn=warn,
        )

    @property
    def logged_in(self) -> bool:
        return self._api is not None

    def login(self) -> None:
        """Use saved tokens if they work, otherwise log in with email and password."""
        self._token_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        api = Garmin(self._email, self._password, prompt_mfa=self._prompt_mfa)
        self._call("login", api.login, str(self._token_dir))
        self._api = api

    # -- activities -------------------------------------------------------------------------

    def strength_activities(self, start: date, end: date) -> list[Activity]:
        """Strength activities starting between the two dates (inclusive)."""
        found = self._call(
            "list activities", self._client.get_activities_by_date, start.isoformat(), end.isoformat()
        )
        return [_activity(a) for a in found or [] if _type_key(a) == STRENGTH]

    def create_strength_activity(
        self, start: datetime, duration_s: float, name: str, time_zone: str
    ) -> int:
        """Create a private manual strength activity and return its ID.

        `start` must be timezone-aware; it is sent as local time in `time_zone`, for example
        "America/Los_Angeles". Garmin stores the duration in whole minutes.
        """
        local = start.astimezone(ZoneInfo(time_zone))
        created = self._call(
            "create activity",
            self._client.create_manual_activity,
            start_datetime=local.strftime("%Y-%m-%dT%H:%M:%S.000"),
            time_zone=time_zone,
            type_key=STRENGTH,
            distance_km=0,
            duration_min=max(1, round(duration_s / 60)),
            activity_name=name,
        )
        if not isinstance(created, dict) or "activityId" not in created:
            raise GarminError(f"create activity: unexpected response {created!r:.300}")
        return int(created["activityId"])

    def set_description(self, activity_id: int, text: str) -> None:
        self._call("set description", self._client.set_activity_description, str(activity_id), text)

    def delete_activity(self, activity_id: int) -> None:
        self._call("delete activity", self._client.delete_activity, str(activity_id))

    # -- exercise sets ----------------------------------------------------------------------

    def exercise_sets(self, activity_id: int) -> list[dict[str, Any]]:
        """The activity's current exercise set list, as Garmin returns it."""
        body = self._call("read exercise sets", self._client.get_activity_exercise_sets, activity_id)
        return list((body or {}).get("exerciseSets") or [])

    def replace_exercise_sets(self, activity_id: int, sets: list[ExerciseSet]) -> None:
        """Replace the activity's whole exercise set list (Garmin has no partial update)."""
        payload = exercise_sets_payload(activity_id, sets)
        self._call("write exercise sets", self._client.set_activity_exercise_sets, activity_id, payload)

    # -- exercise list (public, cached) -----------------------------------------------------

    def exercise_list(self, refresh: bool = False) -> GarminExerciseList:
        """Every valid category/name pair, from the cache when it is recent enough. No login.

        Args:
            refresh: Download it again even if the copy is recent (at most once per run), for
                example before calling a pair invalid: Garmin may have added it since.

        Returns:
            The list. If a download was needed and failed, the older copy, with a warning;
            compare `fetched_at` with the time of the call to tell.

        Raises:
            GarminError: There is no cached copy and the download failed.
        """
        cached = self._cache.get(
            "exercises",
            lambda: _catalogue_to_json(fetch_exercise_catalogue()),
            what="Garmin exercise list",
            errors=(GarminError,),
            needed_as_of=datetime.now(timezone.utc) if refresh else None,
        )
        return GarminExerciseList(_catalogue_from_json(cached.data), cached.fetched_at)

    # -- internals --------------------------------------------------------------------------

    @property
    def _client(self) -> Garmin:
        if self._api is None:
            self.login()
        return self._api

    def _call(self, what: str, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(self._max_retries + 1):
            self._pace()
            log.debug("Garmin: %s%s", what, f" (attempt {attempt + 1})" if attempt else "")
            try:
                return fn(*args, **kwargs)
            except GarminConnectTooManyRequestsError as exc:
                if attempt == self._max_retries:
                    raise GarminError(f"{what}: rate limited by Garmin, try again later") from exc
                time.sleep(min(60.0, 2.0 ** (attempt + 2)))
            except GarminConnectNotFoundError as exc:
                raise GarminNotFoundError(f"{what}: not found") from exc
            except Exception as exc:  # the library raises several types; report them uniformly
                raise GarminError(f"{what}: {exc}") from exc
        raise GarminError(f"{what}: gave up after {self._max_retries} retries")

    def _pace(self) -> None:
        wait = self._last_request + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


# --- Exercise catalogue (public, no login) -------------------------------------------------------


def fetch_exercise_catalogue(http: requests.Session | None = None) -> dict[str, dict[str, GarminExercise]]:
    """Every valid exercise as {category: {name: GarminExercise}}, without REJECTED_NAMES."""
    http = http or requests.Session()
    try:
        exercises = http.get(EXERCISES_URL, timeout=30)
        exercises.raise_for_status()
        names = http.get(DISPLAY_NAMES_URL, timeout=30)
        names.raise_for_status()
    except requests.RequestException as exc:
        raise GarminError(f"fetch exercise catalogue: {exc}") from exc

    display = {}
    for line in names.content.decode("utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            display[key.strip()] = value.strip()

    catalogue: dict[str, dict[str, GarminExercise]] = {}
    for category, body in exercises.json()["categories"].items():
        catalogue[category] = {
            name: GarminExercise(
                category=category,
                name=name,
                display=display.get(f"{category}_{name}", name),
                primary_muscles=tuple(info.get("primaryMuscles") or ()),
                secondary_muscles=tuple(info.get("secondaryMuscles") or ()),
            )
            for name, info in body["exercises"].items()
            if name not in REJECTED_NAMES.get(category, ())
        }
    if not catalogue:
        raise GarminError("fetch exercise catalogue: no categories in response")
    return catalogue


def _catalogue_to_json(catalogue: dict[str, dict[str, GarminExercise]]) -> dict:
    return {
        category: {
            name: {"display": e.display, "primary_muscles": list(e.primary_muscles),
                   "secondary_muscles": list(e.secondary_muscles)}
            for name, e in sorted(exercises.items(), key=lambda kv: kv[1].display)
        }
        for category, exercises in sorted(catalogue.items())
    }


def _catalogue_from_json(data: dict) -> dict[str, dict[str, GarminExercise]]:
    return {
        category: {
            name: GarminExercise(category, name, e["display"], tuple(e["primary_muscles"]),
                                 tuple(e["secondary_muscles"]))
            for name, e in exercises.items()
            if name not in REJECTED_NAMES.get(category, ())  # copies cached before the list existed
        }
        for category, exercises in data.items()
    }


# --- Parsing helpers -----------------------------------------------------------------------------


def _type_key(a: dict) -> str:
    return (a.get("activityType") or {}).get("typeKey", "")


def _activity(a: dict) -> Activity:
    start = datetime.strptime(a["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return Activity(
        id=int(a["activityId"]),
        name=a.get("activityName") or "",
        type_key=_type_key(a),
        start=start,
        duration_s=float(a.get("duration") or 0),
        raw=a,
    )
