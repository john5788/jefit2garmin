"""Client for Jefit's unofficial web API: the calls www.jefit.com makes for itself.

Nothing here is documented by Jefit. Endpoints and field names come from responses observed
in October 2026 and may change without notice. Every failure raises `JefitError` naming the
call that broke.

Two kinds of source are used:

- JSON endpoints under `/api/v2/...`.
- Page data: some information only exists inside server-rendered pages. Requesting those pages
  with the header `rsc: 1` returns React's page-data stream instead of HTML (see `parse_flight`).

Times are returned as timezone-aware datetimes in UTC.

The exercise list and routines are kept in a local cache (see disk_cache.py), so a run can go on
when Jefit can't be reached or the page data changes: `exercise_list()`, `exercise()` and
`routine_for_day()` use it. `exercise_catalogue()`, `routine_index()` and `routine()` always
download.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .disk_cache import DiskCache

log = logging.getLogger(__name__)  # requests are logged as method, path and status only

# --- Endpoints -----------------------------------------------------------------------------------
# Everything the client calls, in one place. Paths are relative to BASE_URL; {placeholders} are
# filled in per call.

BASE_URL = "https://www.jefit.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


class Endpoint:
    # JSON API
    LOGIN = "/api/v2/auth/login"  # POST {"platform": "web", "username", "passwordMd5"}
    USER = "/api/v2/user"
    CALENDAR = "/api/v2/users/{user_id}/sessions/calendar"  # ?timezone_offset=-07:00
    SESSIONS = "/api/v2/users/{user_id}/sessions"  # ?startDate=<unix time of local midnight>
    CUSTOM_EXERCISES = "/api/v2/user/exercises"
    ROUTINE = "/api/v2/routines/{routine_id}"  # with ROUTINE_PARAMS

    # Page data, requested with the header `rsc: 1`
    HISTORY_PAGE = "/my-jefit/progress/history"  # holds preLoadedExercises
    WORKOUTS_PAGE = "/my-jefit/workouts"  # holds every routine with its day IDs


# Exactly what the website sends; without `expand` the routine comes back without its days.
ROUTINE_PARAMS = {
    "expand": "routine.days.day_exercises.interval,routine.user,routine.days.day_exercises.sets",
    "sort": (
        "days.day_exercises.sort_order,days.day_exercises.index,"
        "days.day_exercises.local_id,days.day_exercises.sets.index"
    ),
}


class JefitError(RuntimeError):
    """A Jefit call failed or returned something unexpected."""


# --- Data returned by the client ---------------------------------------------------------------


@dataclass(frozen=True)
class Exercise:
    """An exercise definition. `id` is `d_...` for built-in, `u_...` for the user's own."""

    id: str
    name: str
    input_format: str | None  # weight_and_reps, reps, duration, reps_and_duration, cardio
    body_parts: tuple[str, ...]
    equipment: tuple[str, ...]
    nicknames: tuple[str, ...]  # other names Jefit knows it by
    raw: dict = field(repr=False, compare=False)

    @property
    def custom(self) -> bool:
        return self.id.startswith("u_")


@dataclass(frozen=True)
class JefitExerciseList:
    """Every built-in and custom exercise, as of `fetched_at`."""

    exercises: dict[str, Exercise]
    fetched_at: datetime

    def get(self, exercise_id: str) -> Exercise | None:
        return self.exercises.get(exercise_id)


@dataclass(frozen=True)
class LogSet:
    """One logged set. Fields Jefit did not record are None."""

    index: int | None
    logged_at: datetime | None  # when the set was logged, i.e. when it ended; None in old logs
    reps: int | None
    weight: float | None  # in the session's unit system
    duration_s: float | None  # timed and cardio sets
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class ExerciseLog:
    """All sets of one exercise within a session."""

    exercise_id: str
    day_exercise_id: str | None  # links to RoutineExercise.id
    record_type: str | None  # weight_and_reps, reps, duration, reps_and_duration, cardio
    unit_system: str  # "imperial" (lb) or "metric" (kg)
    logged_at: datetime | None  # first set logged
    sets: tuple[LogSet, ...]
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Session:
    """One logged workout."""

    id: int
    day_id: str | None  # routine day; links to Routine.days[].id
    started_at: datetime
    total_time_s: int | None
    workout_time_s: int | None
    rest_time_s: int | None
    unit_system: str
    logs: tuple[ExerciseLog, ...]
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class RoutineSummary:
    id: str
    name: str
    day_ids: tuple[str, ...]


@dataclass(frozen=True)
class RoutineIndex:
    """Every routine with its day IDs, plus the user's default rest timer."""

    routines: tuple[RoutineSummary, ...]
    default_rest_s: int | None
    fetched_at: datetime

    def routine_for_day(self, day_id: str) -> RoutineSummary | None:
        return next((r for r in self.routines if day_id in r.day_ids), None)


@dataclass(frozen=True)
class RoutineSet:
    index: int | None
    rest_time_s: int | None  # rest timer after this set
    reps: int | None
    weight_lbs: float | None
    duration_s: int | None


@dataclass(frozen=True)
class RoutineExercise:
    id: str  # matches ExerciseLog.day_exercise_id
    exercise_id: str | None
    superset_id: str | None  # None when not in a superset
    sets: tuple[RoutineSet, ...]


@dataclass(frozen=True)
class RoutineDay:
    id: str
    name: str
    exercises: tuple[RoutineExercise, ...]


@dataclass(frozen=True)
class Routine:
    """A routine as it was at `fetched_at`. Old sessions may have been logged against an earlier version."""

    id: str
    name: str
    days: tuple[RoutineDay, ...]
    fetched_at: datetime
    raw: dict = field(repr=False, compare=False)

    def day(self, day_id: str) -> RoutineDay | None:
        return next((d for d in self.days if d.id == day_id), None)


# --- Client --------------------------------------------------------------------------------------


class JefitClient:
    """Read-only access to one Jefit account.

    Requests are spaced at least `min_interval` seconds apart. Rate limiting (429) and server
    errors are retried with backoff; an expired token (401) triggers one fresh login.
    """

    CACHE_FILE = "jefit_cache.json"  # in cache_dir

    def __init__(
        self,
        username: str,
        password: str,
        *,
        cache_dir: Path | None = None,
        max_cache_age: timedelta = timedelta(days=30),
        refresh_cache: bool = False,
        warn: Callable[[str], None] | None = None,
        min_interval: float = 0.5,
        max_retries: int = 4,
        timeout: float = 30,
        http: requests.Session | None = None,
    ) -> None:
        """
        Args:
            username, password: The Jefit account.
            cache_dir: Folder for the local cache file; None keeps it in memory for one run.
            max_cache_age: Cached copies older than this are downloaded again.
            refresh_cache: Download every cached item once, whatever its age.
            warn: Called when a download fails and a cached copy is used instead.
            min_interval, max_retries, timeout: Request pacing and retries.
            http: requests session to use (tests).
        """
        self._username = username
        # Jefit's login takes the unsalted MD5 of the password; keep only that.
        self._password_md5 = hashlib.md5(password.encode()).hexdigest()
        self._http = http or requests.Session()
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._timeout = timeout
        self._token: str | None = None
        self._refresh_token: str | None = None  # returned by login; how to use it is unknown
        self._user_id: int | None = None
        self._last_request = 0.0
        self._cache = DiskCache(
            cache_dir / self.CACHE_FILE if cache_dir else None,
            max_age=max_cache_age,
            refresh_all=refresh_cache,
            warn=warn,
        )
        self._exercise_list: JefitExerciseList | None = None  # parsed from the cache

    # -- account ----------------------------------------------------------------------------

    def login(self) -> None:
        resp = self._request(
            "POST",
            Endpoint.LOGIN,
            json_body={"platform": "web", "username": self._username, "passwordMd5": self._password_md5},
            authed=False,
        )
        body = _json(resp, "login")
        if "accessToken" not in body:
            raise JefitError(f"login: no accessToken in response (keys: {sorted(body)})")
        self._token = body["accessToken"]
        self._refresh_token = body.get("refreshToken")

    @property
    def user_id(self) -> int:
        if self._user_id is None:
            data = self._get_json(Endpoint.USER)["data"]
            self._user_id = int(data["id"])
        return self._user_id

    # -- workouts ---------------------------------------------------------------------------

    def workout_dates(self, tz: ZoneInfo) -> list[date]:
        """Every local date with a logged workout, oldest first."""
        offset = datetime.now(tz).strftime("%z")  # e.g. -0700
        body = self._get_json(
            Endpoint.CALENDAR.format(user_id=self.user_id),
            params={"timezone_offset": f"{offset[:3]}:{offset[3:]}"},
        )
        return sorted(date.fromisoformat(d["date"]) for d in body.get("data") or [] if d.get("has_logs"))

    def sessions_on(self, day: date, tz: ZoneInfo) -> list[Session]:
        """Sessions that started on `day` (local date in `tz`)."""
        start = int(datetime.combine(day, dtime.min, tz).timestamp())
        body = self._get_json(Endpoint.SESSIONS.format(user_id=self.user_id), params={"startDate": start})
        sessions = [_session(s) for s in body.get("data") or []]
        # Whether startDate returns one day or everything after it is unverified; filter either way.
        return [s for s in sessions if s.started_at.astimezone(tz).date() == day]

    # -- exercises (cached) -----------------------------------------------------------------

    def exercise_list(self, needed_as_of: datetime | None = None) -> JefitExerciseList:
        """Every built-in and custom exercise, from the cache when it is recent enough.

        Args:
            needed_as_of: Download again if the cached copy is older than this (once per run).

        Raises:
            JefitError: There is no cached copy and the download failed.
        """
        cached = self._cache.get(
            "exercises",
            lambda: {k: e.raw for k, e in self.exercise_catalogue().items()},
            what="Jefit exercise list",
            errors=(JefitError,),
            needed_as_of=needed_as_of,
        )
        if self._exercise_list is None or self._exercise_list.fetched_at != cached.fetched_at:
            exercises = {k: _exercise(raw) for k, raw in cached.data.items()}
            self._exercise_list = JefitExerciseList(exercises, cached.fetched_at)
        return self._exercise_list

    def exercise(self, exercise_id: str, seen_at: datetime | None = None) -> Exercise | None:
        """Look up one exercise.

        Args:
            exercise_id: `d_...` or `u_...`.
            seen_at: When the exercise was used (a session's start). If it isn't in the cached
                list and the list is older than this, the exercise may be new: the list is
                downloaded again (once per run). Without it, only the cache is looked at.

        Returns:
            The exercise, or None if Jefit doesn't have it (for example a retired ID).

        Raises:
            JefitError: It isn't in the cached list, and the newer list it may be in couldn't
                be downloaded, so whether it exists is unknown.
        """
        exercises = self.exercise_list()
        found = exercises.get(exercise_id)
        if found is None and seen_at is not None and seen_at > exercises.fetched_at:
            exercises = self.exercise_list(needed_as_of=seen_at)
            found = exercises.get(exercise_id)
            if found is None and seen_at > exercises.fetched_at:
                raise JefitError(f"exercise {exercise_id}: not in the cached exercise list, and the "
                                 "current list couldn't be downloaded")
        return found

    # -- exercises (always downloaded) ------------------------------------------------------

    def custom_exercises(self) -> dict[str, Exercise]:
        """The user's own (`u_...`) exercises."""
        body = self._get_json(Endpoint.CUSTOM_EXERCISES)
        return {e["id"]: _exercise(e) for e in body.get("data") or []}

    def exercise_catalogue(self) -> dict[str, Exercise]:
        """Every built-in and custom exercise, from the history page's `preLoadedExercises`."""
        rows = self._get_page_data(Endpoint.HISTORY_PAGE)
        for node in _walk(rows):
            if isinstance(node, dict) and node.get("preLoadedExercises") and isinstance(node["preLoadedExercises"], dict):
                return {k: _exercise(v) for k, v in node["preLoadedExercises"].items()}
        raise JefitError(f"exercise catalogue: preLoadedExercises not found on {Endpoint.HISTORY_PAGE}")

    # -- routines (cached) ------------------------------------------------------------------

    def routine_for_day(self, day_id: str, as_of: datetime) -> Routine | None:
        """The routine a session's day belongs to, as it was at `as_of` or later if possible.

        Routines change, so a cached copy older than `as_of` (a session's start) is downloaded
        again, once per run. If that fails the older copy is returned with a warning; compare
        the result's `fetched_at` with `as_of` to tell.

        Returns:
            The routine, or None if the day belongs to no routine (the routine or day was
            deleted).

        Raises:
            JefitError: Nothing usable is cached and the download failed.
        """
        index = self._routine_index_cached()
        summary = index.routine_for_day(day_id)
        if summary is None and index.fetched_at < as_of:
            index = self._routine_index_cached(needed_as_of=as_of)  # the day may be newer than our copy
            summary = index.routine_for_day(day_id)
            if summary is None and index.fetched_at < as_of:
                raise JefitError(f"routine day {day_id}: not in the cached routine list, and the "
                                 "current list couldn't be downloaded")
        if summary is None:
            return None
        cached = self._cache.get(
            f"routine:{summary.id}",
            lambda: self._get_json(Endpoint.ROUTINE.format(routine_id=summary.id), params=ROUTINE_PARAMS)["data"],
            what=f"Jefit routine {summary.name}",
            errors=(JefitError,),
            needed_as_of=as_of,
        )
        return _routine(cached.data, cached.fetched_at)

    def _routine_index_cached(self, needed_as_of: datetime | None = None) -> RoutineIndex:
        cached = self._cache.get(
            "routine_index",
            lambda: _routine_index_to_json(self.routine_index()),
            what="Jefit routine list",
            errors=(JefitError,),
            needed_as_of=needed_as_of,
        )
        return _routine_index_from_json(cached.data, cached.fetched_at)

    # -- routines (always downloaded) -------------------------------------------------------

    def routine_index(self) -> RoutineIndex:
        """Every routine with its day IDs, from the workouts page."""
        rows = self._get_page_data(Endpoint.WORKOUTS_PAGE)
        routines: dict[str, RoutineSummary] = {}
        default_rest = None
        for node in _walk(rows):
            if not isinstance(node, dict):
                continue
            days = node.get("days")
            if "day_count" in node and isinstance(days, list) and all(isinstance(d, dict) for d in days):
                routines[node["id"]] = RoutineSummary(
                    id=node["id"], name=_clean(node.get("name")), day_ids=tuple(d["id"] for d in days)
                )
            elif "timer" in node and "currentRoutine" in node:
                default_rest = _int(node["timer"])
        if not routines:
            raise JefitError(f"routine index: no routines found on {Endpoint.WORKOUTS_PAGE}")
        return RoutineIndex(
            routines=tuple(routines.values()), default_rest_s=default_rest, fetched_at=datetime.now(timezone.utc)
        )

    def routine(self, routine_id: str) -> Routine:
        """One routine with its days, exercises and per-set rest timers."""
        data = self._get_json(Endpoint.ROUTINE.format(routine_id=routine_id), params=ROUTINE_PARAMS)["data"]
        return _routine(data, datetime.now(timezone.utc))

    # -- HTTP -------------------------------------------------------------------------------

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        return _json(self._request("GET", path, params=params), path)

    def _get_page_data(self, path: str) -> dict[str, Any]:
        resp = self._request("GET", path, headers={"rsc": "1"})
        return parse_flight(resp.content)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
        authed: bool = True,
        relogin: bool = True,
    ) -> requests.Response:
        call = f"{method} {path}"
        hdrs = {"user-agent": USER_AGENT, **(headers or {})}
        if authed:
            if self._token is None:
                self.login()
            hdrs["cookie"] = f"jefitAccessToken={self._token}"

        for attempt in range(self._max_retries + 1):
            self._pace()
            try:
                resp = self._http.request(
                    method, BASE_URL + path, params=params, json=json_body, headers=hdrs, timeout=self._timeout
                )
            except requests.RequestException as exc:
                log.debug("%s: %s (attempt %d)", call, exc, attempt + 1)
                if attempt == self._max_retries:
                    raise JefitError(f"{call}: {exc}") from exc
                time.sleep(_backoff(attempt))
                continue
            log.debug("%s → HTTP %s (%.1fs)", call, resp.status_code, resp.elapsed.total_seconds())

            if resp.status_code == 401 and authed and relogin:
                self.login()
                return self._request(
                    method, path, params=params, json_body=json_body, headers=headers, relogin=False
                )
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self._max_retries:
                time.sleep(_backoff(attempt, resp.headers.get("retry-after")))
                continue
            if not resp.ok:
                hint = " (check username and password)" if path == Endpoint.LOGIN else ""
                raise JefitError(f"{call}: HTTP {resp.status_code}{hint}: {resp.text[:300]}")
            return resp
        raise JefitError(f"{call}: gave up after {self._max_retries} retries")

    def _pace(self) -> None:
        wait = self._last_request + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


# --- Page data (React Server Components "flight" stream) -----------------------------------------


def parse_flight(data: bytes | str) -> dict[str, Any]:
    """Parse a Next.js page-data stream into {row id: value}.

    The stream is a sequence of rows `<hex id>:<payload>`. Most payloads are one line of JSON.
    Text rows look like `<id>:T<hex byte length>,<text>` and may contain newlines. Other rows
    (module references `I[...]`, hints `HL[...]`) are kept as plain strings.
    """
    buf = data.encode() if isinstance(data, str) else data
    rows: dict[str, Any] = {}
    pos = 0
    while pos < len(buf):
        if buf[pos : pos + 1] == b"\n":
            pos += 1
            continue
        colon = buf.find(b":", pos)
        if colon == -1:
            break
        row_id = buf[pos:colon].decode()
        pos = colon + 1
        if buf[pos : pos + 1] == b"T":
            comma = buf.index(b",", pos)
            length = int(buf[pos + 1 : comma], 16)
            rows[row_id] = buf[comma + 1 : comma + 1 + length].decode("utf-8", "replace")
            pos = comma + 1 + length
            continue
        end = buf.find(b"\n", pos)
        end = len(buf) if end == -1 else end
        line = buf[pos:end].decode("utf-8", "replace")
        try:
            rows[row_id] = json.loads(line)
        except json.JSONDecodeError:
            rows[row_id] = line
        pos = end + 1
    return rows


def _walk(node: Any) -> Iterator[Any]:
    """Every dict and list nested anywhere inside `node`."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            yield item
            stack.extend(item)


# --- Parsing helpers -----------------------------------------------------------------------------


def _json(resp: requests.Response, call: str) -> dict:
    try:
        body = resp.json()
    except ValueError as exc:
        raise JefitError(f"{call}: response is not JSON: {resp.text[:300]}") from exc
    if not isinstance(body, dict):
        raise JefitError(f"{call}: unexpected response shape: {type(body).__name__}")
    return body


def _clean(name: str | None) -> str:
    """Jefit names can carry trailing spaces and HTML entities (`90 &#176; ...`)."""
    return html.unescape(name or "").strip()


def _parts(values: list | None) -> tuple[str, ...]:
    return tuple(v for v in values or [] if v and v != "none")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc(ts: Any) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None


def _backoff(attempt: int, retry_after: str | None = None) -> float:
    seconds = _float(retry_after)
    return min(60.0, seconds if seconds is not None else 2.0 ** (attempt + 1))


def _exercise(e: dict) -> Exercise:
    return Exercise(
        id=e["id"],
        name=_clean(e.get("name")),
        input_format=e.get("input_format"),
        body_parts=_parts(e.get("body_parts")),
        equipment=_parts(e.get("equipment")),
        nicknames=tuple(_clean(n) for n in e.get("nicknames") or [] if n and n.strip()),
        raw=e,
    )


def _log_set(s: dict) -> LogSet:
    return LogSet(
        index=_int(s.get("index")),
        logged_at=_utc(s.get("date")),
        reps=_int(s.get("reps")),
        weight=_float(s.get("weight")),
        duration_s=_float(s.get("duration")),
        raw=s,
    )


def _exercise_log(log: dict, unit_system: str) -> ExerciseLog:
    return ExerciseLog(
        exercise_id=log["exercise_id"],
        day_exercise_id=log.get("day_exercise_id"),
        record_type=log.get("record_type"),
        unit_system=log.get("unit_system") or unit_system,
        logged_at=_utc(log.get("date")),
        sets=tuple(_log_set(s) for s in log.get("log_sets") or []),
        raw=log,
    )


def _session(s: dict) -> Session:
    unit_system = s.get("unit_system") or "imperial"
    return Session(
        id=int(s["id"]),
        day_id=s.get("day_id"),
        started_at=_utc(s["date"]),
        total_time_s=_int(s.get("total_time")),
        workout_time_s=_int(s.get("workout_time")),
        rest_time_s=_int(s.get("rest_time")),
        unit_system=unit_system,
        logs=tuple(_exercise_log(log, unit_system) for log in s.get("logs") or []),
        raw=s,
    )


def _routine_exercise(e: dict) -> RoutineExercise:
    superset = (e.get("superset") or {}).get("id")
    return RoutineExercise(
        id=e["id"],
        exercise_id=(e.get("exercise") or {}).get("id"),
        superset_id=None if superset in (None, "u_000000000000") else superset,
        sets=tuple(
            RoutineSet(
                index=_int(s.get("index")),
                rest_time_s=_int(s.get("rest_time")),
                reps=_int(s.get("reps")),
                weight_lbs=_float(s.get("weight_lbs")),
                duration_s=_int(s.get("duration")),
            )
            for s in e.get("sets") or []
        ),
    )


def _routine(data: dict, fetched_at: datetime) -> Routine:
    return Routine(
        id=data["id"],
        name=_clean(data.get("name")),
        days=tuple(
            RoutineDay(
                id=day["id"],
                name=_clean(day.get("name")),
                exercises=tuple(_routine_exercise(e) for e in day.get("day_exercises") or []),
            )
            for day in data.get("days") or []
        ),
        fetched_at=fetched_at,
        raw=data,
    )


def _routine_index_to_json(index: RoutineIndex) -> dict:
    return {
        "routines": [{"id": r.id, "name": r.name, "day_ids": list(r.day_ids)} for r in index.routines],
        "default_rest_s": index.default_rest_s,
    }


def _routine_index_from_json(data: dict, fetched_at: datetime) -> RoutineIndex:
    return RoutineIndex(
        routines=tuple(RoutineSummary(r["id"], r["name"], tuple(r["day_ids"])) for r in data["routines"]),
        default_rest_s=data.get("default_rest_s"),
        fetched_at=fetched_at,
    )
