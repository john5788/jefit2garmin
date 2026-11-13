"""The app: one sync run that makes Garmin Connect match Jefit for the selected days.

How a run goes (App.run):

1. Check exercise_map.toml against both exercise lists; stop on errors.
2. Find the Jefit sessions on the selected days; add unseen exercises to [unreviewed].
3. Plan: build each session's Garmin sets and decide what to do with it (an Action).
4. Ask first if many activities would be created, then carry out the plan.

Garmin is only logged into if a session needs it. Each session is recorded as soon as it is
done, so an interrupted run resumes where it stopped. A dry run writes nothing to Garmin or the
sync record; local files (the caches, exercise_map.toml, garmin_exercises.md) are updated as in
a real run.

A failed download of an exercise list or routine never stops a run: the clients fall back to
their cached copies, and whatever can't be done without fresh data is left for a later run.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .clients.garmin import (
    Activity,
    GarminClient,
    GarminError,
    GarminExerciseList,
    GarminNotFoundError,
    exercise_sets_payload,
)
from .clients.jefit import JefitClient, JefitError, Session
from .exercise_map import BUILTIN_MAP_FILE, ExerciseMap, ExerciseMapError, NewEntry, write_garmin_reference
from .settings import Options, Settings
from .sync_state import SessionRecord, SyncState
from .workout_builder import GarminWorkout, RestTimers, WorkoutBuilder

log = logging.getLogger(__name__)


class Action(Enum):
    """What a run does with one Jefit session."""

    UNCHANGED = "unchanged"  # Garmin already has these sets
    UPDATE = "update"  # replace the sets on an existing activity
    CREATE = "create"  # no watch activity: create one and add the sets
    MERGE = "merge"  # a watch activity appeared for one the app created: move the sets, delete ours
    WAIT = "wait"  # no watch activity yet, and too recent to create one
    SKIP = "skip"  # nothing to send, or the Garmin side must not be touched


class GarminSets(Enum):
    """What a synced session's activity holds now, compared with the last push."""

    AS_PUSHED = "as pushed"  # exactly what the app last pushed
    EDITED = "edited"  # changed by hand in Garmin Connect since
    DELETED = "deleted"  # the activity no longer exists


@dataclass
class Job:
    """One Jefit session and what to do with it."""

    day: date
    session: Session
    workout: GarminWorkout
    record: SessionRecord | None  # from the sync record; None if never synced
    action: Action = Action.SKIP
    watch_activity: Activity | None = None  # for UPDATE of a new session, and MERGE
    freeze_timers: bool = True  # False: the rest timers came from an outdated routine copy

    @property
    def target_activity_id(self) -> int | None:
        if self.watch_activity:
            return self.watch_activity.id
        return self.record.activity_id if self.record else None


@dataclass(frozen=True)
class SentExercise:
    """One exercise of a push, as mapped."""

    exercise_id: str
    jefit_name: str
    category: str
    name: str | None
    builtin: bool  # the mapping came from the built-in map, not the user's exercise_map.toml

    def describe(self) -> str:
        source = "  (built-in map)" if self.builtin else ""
        return f"{self.exercise_id}  {self.jefit_name}  →  {self.category}/{self.name or '(generic)'}{source}"


class Report:
    """Counts for the summary at the end of a run."""

    LABELS = {
        Action.UNCHANGED: "unchanged",
        Action.UPDATE: "updated",
        Action.CREATE: "created",
        Action.MERGE: "merged",
        Action.WAIT: "waiting for watch",
        Action.SKIP: "skipped",
    }

    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.counts: Counter[Action] = Counter()
        self.failed = 0
        self.unmapped: dict[str, str] = {}  # exercise ID -> name

    def count(self, action: Action) -> None:
        self.counts[action] += 1

    def lines(self) -> list[str]:
        parts = [f"{self.counts[a]} {label}" for a, label in self.LABELS.items() if self.counts[a]]
        if self.failed:
            parts.append(f"{self.failed} failed")
        done = ", ".join(parts) or "nothing to do"
        out = [f"Dry run, nothing written. Would have: {done}" if self.dry_run else f"Summary: {done}"]
        if self.unmapped:
            out.append(f"{len(self.unmapped)} exercise(s) still need a Garmin mapping in exercise_map.toml:")
            out += [f"  {eid}  {name}" for eid, name in sorted(self.unmapped.items(), key=lambda kv: kv[1])]
        return out


class App:
    def __init__(
        self,
        settings: Settings,
        options: Options,
        *,
        out: Callable[[str], None] = print,
        ask: Callable[[str], str] = input,
    ) -> None:
        self.settings = settings
        self.options = options
        self._print = out  # see out() and warn()
        self.ask = ask
        self.report = Report(dry_run=options.dry_run)

        self.started_at = datetime.now(timezone.utc)

        settings.state_dir.mkdir(parents=True, exist_ok=True)
        cache = dict(
            cache_dir=settings.state_dir,
            max_cache_age=settings.cache_max_age,
            refresh_cache=options.refresh_caches,
            warn=self.warn,
        )
        self.jefit = JefitClient(settings.jefit_username, settings.jefit_password, **cache)
        self.garmin = GarminClient(  # logs in on the first account call; see garmin_account
            settings.garmin_email,
            settings.garmin_password,
            settings.garmin_token_dir,
            prompt_mfa=lambda: self.ask("Garmin MFA code: ").strip(),
            **cache,
        )
        self.state = SyncState(settings.sync_state_file)
        self.exercise_map = ExerciseMap(settings.exercise_map_file)
        self.builder = WorkoutBuilder(self.exercise_map)

        self._garmin_list: GarminExerciseList | None = None  # set by check_exercise_map
        self._watch_activities: list[Activity] | None = None  # see `watch_activities`
        self._garmin_days: tuple[date, date] | None = None  # days to look for watch activities in
        self._claimed: dict[int, int] = {}  # Garmin activity ID -> Jefit session ID holding it

    def run(self) -> Report:
        try:
            self.check_exercise_map()
            sessions = self.find_sessions()
            if not sessions:
                return self.report
            self.add_new_exercises(sessions)
            jobs = self.plan(sessions)
            if self.options.dry_run:
                self.show_dry_run(jobs)
            elif self.confirm_creates(jobs):
                for job in jobs:
                    self.execute(job)
            if not self.options.dry_run and self.state.first_run() is None:
                self.state.set_first_run(self.today().isoformat())
        finally:
            self.state.close()
        return self.report

    def check_exercise_map(self) -> None:
        """Step 1 of a run: make sure exercise_map.toml can be used before anything is synced.

        Gets both exercise lists (from the cache, downloading when missing or older than
        cache_max_days) and checks every line of the map against them. Problems that only make
        the file harder to maintain (a Jefit label that doesn't match, a retired Jefit ID) are
        printed as warnings. A Garmin category/name pair that doesn't exist would make Garmin
        reject the whole workout, so those stop the run, but only once a freshly downloaded
        Garmin list confirms it: Garmin may have added the exercise since the cached copy.

        When a list can't be downloaded, the run goes on:
        - No Jefit list at all: the label checks are skipped.
        - No Garmin list at all: nothing is checked against it, and new exercises can't be
          added to the map this run (see add_new_exercises).
        - Only an old Garmin copy, and a pair isn't in it: that exercise is left out of this
          run with a warning instead of stopping it.

        Also writes garmin_exercises.md when the Garmin list is newer than the file.

        Raises:
            ExerciseMapError: The map has invalid Garmin exercises; the message lists each one
                with its line number.
        """
        try:
            jefit_list = self.jefit.exercise_list()
        except JefitError as exc:
            jefit_list = None
            self.warn(f"Jefit's exercise list is unavailable ({exc}); exercise_map.toml labels not checked")
        try:
            garmin_list = self.garmin.exercise_list()
        except GarminError as exc:
            garmin_list = None
            self.warn(f"Garmin's exercise list is unavailable ({exc}); exercise_map.toml not checked against it")

        found = self.exercise_map.check(jefit_list, garmin_list)
        if (found.invalid or found.builtin_invalid) and garmin_list.fetched_at < self.started_at:
            garmin_list = self.garmin.exercise_list(refresh=True)  # Garmin may have added them since
            found = self.exercise_map.check(jefit_list, garmin_list)
        confirmed = garmin_list is not None and garmin_list.fetched_at >= self.started_at
        for warning in found.warnings:
            self.warn(f"exercise_map.toml: {warning}")
        if found.invalid and confirmed:
            raise ExerciseMapError(
                "exercise_map.toml has invalid Garmin exercises (valid ones are listed in "
                "state/garmin_exercises.md):\n  " + "\n  ".join(found.invalid.values())
            )
        for exercise_id, message in found.invalid.items():
            self.warn(f"exercise_map.toml {message} according to the cached Garmin list, which couldn't "
                      "be downloaded again; left out until it can be checked")
            self.exercise_map.hold_back(exercise_id)
        # A built-in line Garmin doesn't accept never stops a run: the exercise is left out (or,
        # when Garmin's current list confirms it, treated as new so it gets a line to map).
        for exercise_id, message in found.builtin_invalid.items():
            if confirmed:
                self.warn(f"built-in exercise map: {message} is no longer a valid Garmin exercise; map it in "
                          f"exercise_map.toml instead (built-in map: {BUILTIN_MAP_FILE})")
                self.exercise_map.drop_builtin(exercise_id)
            else:
                self.warn(f"built-in exercise map: {message} isn't in the cached Garmin list, which couldn't "
                          "be downloaded again; left out until it can be checked")
                self.exercise_map.hold_back(exercise_id)

        self._garmin_list = garmin_list
        if garmin_list is not None:
            write_garmin_reference(self.settings.garmin_reference_file, garmin_list)

    def find_sessions(self) -> list[tuple[date, Session]]:
        """Step 2 of a run: download the Jefit workouts this run should look at.

        The days come from select_days(): the --date/--since/--until range if given, otherwise
        the last window_days days plus older days never synced. Each of those days' sessions is
        downloaded from Jefit. A session the sync record has synced but Jefit no longer has
        (deleted in Jefit) gets a warning; its Garmin activity is left as it is.

        Nothing is written. Prints how many days are being checked, or that there are none.

        Returns:
            (day, session) for every session found, in date order. day is the local date of
            the workout. Empty when no selected day has a workout.

        Raises:
            JefitError: A Jefit call failed.
        """
        days = self.select_days()
        if not days:
            self.out("No Jefit workouts in the selected days.")
            return []
        self.out(f"Checking {len(days)} workout day(s): {days[0]} to {days[-1]}")
        found = []
        for day in days:
            sessions = self.jefit.sessions_on(day, self.settings.time_zone)
            found += [(day, s) for s in sessions]
            self.warn_about_deleted_sessions(day, {s.id for s in sessions})
        return found

    def select_days(self) -> list[date]:
        """Days with a Jefit workout: the date options, else the window plus missed days."""
        workout_days = self.jefit.workout_dates(self.settings.time_zone)
        since, until = self.options.since, self.options.until
        if since or until:
            return [d for d in workout_days if (since is None or d >= since) and (until is None or d <= until)]

        window_start = self.today() - timedelta(days=self.settings.window_days)
        selected = [d for d in workout_days if d >= window_start]
        first_run = self.state.first_run()
        if first_run:
            # Older workouts never synced since the app started being used (e.g. not run for weeks)
            earliest = date.fromisoformat(first_run) - timedelta(days=self.settings.window_days)
            synced = self.state.synced_days()
            selected += [d for d in workout_days if earliest <= d < window_start and d.isoformat() not in synced]
        return sorted(selected)

    def warn_about_deleted_sessions(self, day: date, session_ids: set[int]) -> None:
        for record in self.state.sessions_on(day.isoformat()):
            if record.session_id not in session_ids and record.activity_id:
                self.warn(f"{day}: Jefit session {record.session_id} no longer exists; "
                          f"Garmin activity {record.activity_id} left as it is")

    def add_new_exercises(self, sessions: list[tuple[date, Session]]) -> None:
        """Give every exercise not yet in exercise_map.toml a line in [unreviewed].

        Part of step 2. For each exercise in the sessions that has no line in any section of the
        map (cardio is ignored), ExerciseMap.suggest() proposes a Garmin exercise:

        - Filled in when a Garmin name has the same words as the Jefit name in any order
          ("Cable Seated Row" = "Seated Cable Row"). The exercise is synced from this run on.
        - Left empty otherwise, with up to three candidates written as comments above the line.
          The exercise is left out of Garmin, with a warning, until the user fills it in.

        An ID missing from the cached Jefit list is looked up in a newly downloaded one if the
        session is newer than the cached copy. One still missing (a retired ID) gets the label
        "(not in Jefit's exercise list)" so the user can map it by hand.

        A line is only written when both lists are available, because the app never changes a
        line afterwards. If Garmin's list or a needed Jefit download is unavailable, the
        exercise is left out of this run with a warning and added by a later run.

        The new lines are appended to the end of the file in one go, under a "# Added DATE"
        comment; nothing already in the file is changed. They are also added to the map in
        memory, so this run's build uses them. A dry run writes them too: the map is a local
        file, and reviewing it before the first real sync is what a dry run is for.

        Args:
            sessions: The (day, session) pairs from find_sessions().

        Raises:
            ExerciseMapError: [unreviewed] isn't the last section of the file, so new lines
                can't be appended.
        """
        new: dict[str, NewEntry] = {}
        postponed: list[str] = []  # new exercises that couldn't be added this run
        for _, session in sessions:
            for log in session.logs:
                exercise_id = log.exercise_id
                if (log.record_type == "cardio" or self.exercise_map.knows(exercise_id) or exercise_id in new
                        or self.exercise_map.is_held_back(exercise_id)):
                    continue
                try:
                    if self._garmin_list is None:
                        raise GarminError("Garmin's exercise list is unavailable")
                    jefit_exercise = self.jefit.exercise(exercise_id, seen_at=session.started_at)
                except (JefitError, GarminError):
                    self.exercise_map.hold_back(exercise_id)
                    postponed.append(exercise_id)
                    continue
                entry = self.exercise_map.suggest(exercise_id, jefit_exercise, self._garmin_list)
                new[exercise_id] = entry
                self.exercise_map.add(entry)
                if entry.category:
                    self.out(f"New exercise {exercise_id} {entry.jefit_label}: mapped to {entry.category}/{entry.name or '(generic)'}")
                else:
                    self.out(f"New exercise {exercise_id} {entry.jefit_label}: needs a mapping")
        if postponed:
            self.warn(f"{len(postponed)} new exercise(s) not added to exercise_map.toml yet, because an exercise "
                      f"list couldn't be downloaded; their sets are left out until a later run adds them: "
                      f"{', '.join(postponed)}")
        if new:
            self.exercise_map.append_unreviewed(list(new.values()), self.today())

    def plan(self, sessions: list[tuple[date, Session]]) -> list[Job]:
        """Step 3 of a run: work out what to do with each session, without changing Garmin.

        For each session, prepare() builds its Garmin sets and decide() picks an Action (see
        decide() for the rules). Sessions whose action needs nothing more (UNCHANGED, WAIT,
        SKIP) are counted in the report now; UPDATE, CREATE and MERGE are counted by step 4
        once they are done.

        Garmin is only logged into if a session needs it, i.e. it isn't unchanged or skipped
        before Garmin matters. It then reads the strength activities from the day before the
        first session to the day after the last (so workouts near midnight or in another time
        zone still match) and, for sessions already synced, their current sets. Activities
        already holding a session in the sync record can't be matched to another one.

        The only write: an activity found deleted in Garmin Connect is marked deleted in the
        sync record (not in a dry run), so it isn't recreated.

        Args:
            sessions: The (day, session) pairs from find_sessions(); not empty.

        Returns:
            One Job per session, in the same order, each with its action set.

        Raises:
            GarminError: Logging in to Garmin or reading from it failed.
        """
        days = [day for day, _ in sessions]
        self._garmin_days = (min(days) - timedelta(days=1), max(days) + timedelta(days=1))
        self._claimed = self.state.activities_in_use()
        jobs = []
        for day, session in sessions:
            job = self.prepare(day, session)
            job.action = self.decide(job)
            log.debug("%s: Jefit session %s → %s (%d sets, Garmin activity %s)", day, session.id,
                      job.action.value, job.workout.active_set_count, job.target_activity_id or "none")
            if job.action in (Action.UNCHANGED, Action.WAIT, Action.SKIP):
                self.report.count(job.action)  # nothing more to do; the rest are counted when done
            if job.action == Action.UNCHANGED:
                self.freeze_timers_if_due(job)
            jobs.append(job)
        return jobs

    def prepare(self, day: date, session: Session) -> Job:
        """Build the session's Garmin sets."""
        record = self.state.get_session(session.id)
        if record and record.timers is not None:
            rest_timers, freeze = record.timers, True  # frozen on first sync, so routine edits don't change old workouts
        else:
            rest_timers, freeze = self.routine_rest_timers(day, session)
        workout = self.builder.build(session, rest_timers)
        for exercise_id in workout.unmapped:
            name = self.exercise_name(exercise_id)
            self.report.unmapped[exercise_id] = name
            self.warn(f"{day}: {name} ({exercise_id}) is not mapped; its sets were left out")
        return Job(day, session, workout, record, freeze_timers=freeze)

    def routine_rest_timers(self, day: date, session: Session) -> tuple[RestTimers, bool]:
        """The routine's rest timers for a session, and whether they may be frozen.

        They may be frozen in the sync record when they come from a copy of the routine taken
        at or after the session's start. From an older copy (the download failed) or with no
        routine data at all, they are used for now and looked up again on a later run.
        """
        if not session.day_id:
            return {}, True  # not logged against a routine: estimates for good
        try:
            routine = self.jefit.routine_for_day(session.day_id, as_of=session.started_at)
        except JefitError as exc:
            self.warn(f"{day}: routine rest timers unavailable ({exc}); set durations estimated for now")
            return {}, False
        if routine is None:
            return {}, True  # the routine day was deleted in Jefit: estimates for good
        timers = self.builder.rest_timers(session, routine.day(session.day_id))
        return timers, routine.fetched_at >= session.started_at

    def freeze_timers_if_due(self, job: Job) -> None:
        """An unchanged session whose timers weren't frozen yet: freeze them now (no push needed)."""
        if job.record and job.record.timers is None and job.freeze_timers and not self.options.dry_run:
            job.record.timers = job.workout.rest_timers
            self.state.save_session(job.record)

    def decide(self, job: Job) -> Action:
        """Every rule for what happens to a session, in order."""
        record, workout = job.record, job.workout
        if not workout.has_sets:
            return Action.SKIP  # nothing mapped to send
        if record and record.activity_deleted:
            self.out(f"{job.day}: its Garmin activity was deleted; not recreating it")
            return Action.SKIP
        unchanged = record is not None and record.fingerprint == workout.fingerprint
        overwrite = self.options.overwrite_garmin_edits
        if unchanged and not record.created_by_app and not overwrite:
            return Action.UNCHANGED

        # The rest needs Garmin: is there a watch activity for this workout?
        watch_activity = self.find_watch_activity(job)

        if record and record.activity_id:  # synced before
            if record.created_by_app and watch_activity:
                job.watch_activity = watch_activity
                return Action.MERGE
            if unchanged and not overwrite:
                return Action.UNCHANGED  # the app's activity, and still no watch activity for it
            garmin_sets = self.garmin_sets_status(job)
            if garmin_sets == GarminSets.DELETED:
                self.out(f"{job.day}: Garmin activity {record.activity_id} was deleted; not recreating it")
                if not self.options.dry_run:
                    record.activity_deleted = True
                    self.state.save_session(record)
                return Action.SKIP
            if garmin_sets == GarminSets.EDITED:
                if not overwrite:
                    self.warn(f"{job.day}: sets on Garmin activity {record.activity_id} were edited in Garmin "
                              "Connect; not overwriting them (run with --overwrite-garmin-edits to replace them)")
                    return Action.SKIP
                overwriting = "would overwrite" if self.options.dry_run else "overwriting"
                self.out(f"{job.day}: sets on Garmin activity {record.activity_id} were edited in Garmin "
                         f"Connect; {overwriting} them (--overwrite-garmin-edits)")
                return Action.UPDATE
            return Action.UNCHANGED if unchanged else Action.UPDATE

        if watch_activity:  # first sync onto the watch's activity
            job.watch_activity = watch_activity
            self._claimed[watch_activity.id] = job.session.id
            return Action.UPDATE
        if datetime.now(timezone.utc) - workout.end >= self.settings.create_after:
            return Action.CREATE
        self.out(f"{job.day}: no watch activity yet; will create one if none appears "
                 f"within {self.settings.create_after_hours} hours")
        return Action.WAIT

    def find_watch_activity(self, job: Job) -> Activity | None:
        """The watch's strength activity overlapping the session most, unless another session has it."""
        best, best_overlap = None, 0.0
        for activity in self.watch_activities:
            if self._claimed.get(activity.id, job.session.id) != job.session.id:
                continue
            overlap = (min(activity.end, job.workout.end) - max(activity.start, job.workout.start)).total_seconds()
            if overlap > best_overlap:
                best, best_overlap = activity, overlap
        return best

    def garmin_sets_status(self, job: Job) -> GarminSets:
        """Whether the session's activity still has the sets last pushed to it.

        Compared field by field (exercise, reps, weight, duration, start time) with what Garmin
        stored after the last push, so a change made by hand in Garmin Connect shows as EDITED.

        Raises:
            GarminError: Reading the activity failed for a reason other than it being deleted.
        """
        try:
            current = self.garmin_account.exercise_sets(job.record.activity_id)
        except GarminNotFoundError:
            return GarminSets.DELETED
        if job.record.garmin_sets is not None and self._comparable(current) != job.record.garmin_sets:
            return GarminSets.EDITED
        return GarminSets.AS_PUSHED

    # --- 4. confirm and execute ------------------------------------------------------------------

    def show_dry_run(self, jobs: list[Job]) -> None:
        for job in jobs:
            if job.action in (Action.UPDATE, Action.CREATE, Action.MERGE):
                target = job.target_activity_id or "new activity"
                self.out(f"[dry run] {job.day}: would {job.action.value} ({target}) "
                         f"with {job.workout.active_set_count} sets")
                self.report.count(job.action)

    def confirm_creates(self, jobs: list[Job]) -> bool:
        creates = sum(job.action == Action.CREATE for job in jobs)
        if creates <= self.settings.confirm_creates_over or self.options.assume_yes:
            return True
        answer = self.ask(f"This run will create {creates} new Garmin activities. Continue? [y/N] ")
        if answer.strip().lower() == "y":
            return True
        self.out("Stopped; nothing was created.")
        return False

    def execute(self, job: Job) -> None:
        try:
            if job.action == Action.UPDATE:
                self.update_activity(job)
            elif job.action == Action.CREATE:
                self.create_activity(job)
            elif job.action == Action.MERGE:
                self.merge_into_watch_activity(job)
            else:
                return
            self.report.count(job.action)
            self.failure_file(job).unlink(missing_ok=True)  # an earlier failure is resolved
        except GarminNotFoundError:
            if job.record:
                job.record.activity_deleted = True
                self.state.save_session(job.record)
            self.out(f"{job.day}: Garmin activity was deleted; not recreating it")
            self.report.count(Action.SKIP)
        except GarminError as exc:
            self.report.failed += 1
            self.report_failure(job, exc)

    def report_failure(self, job: Job, exc: GarminError) -> None:
        """Explain a failed push: what was sent, and a file with the full details.

        Garmin's error doesn't say which set it objected to, so the warning lists every
        exercise the workout sent, as exercise_map.toml mapped it. The file holds the error and
        the exact body sent to Garmin.
        """
        rejected = " 400 " in f" {exc} "  # Garmin refused the data itself; retrying won't help
        if rejected:
            self.warn(f"{job.day}: Garmin rejected the sets ({exc}). Usually one exercise mapping "
                      "isn't accepted by Garmin; this workout sent:")
        else:
            self.warn(f"{job.day}: {exc}; will retry on the next run. This workout sent:")
        exercises = self.exercises_sent(job)
        for e in exercises:
            self.out(f"    {e.describe()}")

        path = self.failure_file(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        details = {
            "day": job.day.isoformat(),
            "jefit_session": job.session.id,
            "action": job.action.value,
            "garmin_activity": job.target_activity_id,
            "error": str(exc),
            "exercises": [
                {"jefit_id": e.exercise_id, "jefit_name": e.jefit_name, "category": e.category, "name": e.name,
                 "from": "built-in map" if e.builtin else "exercise_map.toml"}
                for e in exercises
            ],
            "sent_to_garmin": exercise_sets_payload(job.target_activity_id or 0, job.workout.sets),
        }
        path.write_text(json.dumps(details, indent=1, ensure_ascii=False))
        self.out(f"    details: {path}")

    def exercises_sent(self, job: Job) -> list[SentExercise]:
        """Each exercise in the push, once, with the mapping it was sent with."""
        sent: list[SentExercise] = []
        for log in job.session.logs:
            target = self.exercise_map.garmin_exercise_for(log.exercise_id)
            if log.record_type == "cardio" or target is None or any(log.exercise_id == s.exercise_id for s in sent):
                continue
            sent.append(SentExercise(log.exercise_id, self.exercise_name(log.exercise_id), *target,
                                     builtin=self.exercise_map.uses_builtin(log.exercise_id)))
        return sent

    def failure_file(self, job: Job) -> Path:
        return self.settings.state_dir / "failed" / f"{job.day.isoformat()}_{job.session.id}.json"

    def update_activity(self, job: Job) -> None:
        activity_id = job.target_activity_id
        record = job.record or SessionRecord(job.session.id, job.day.isoformat(), activity_id)
        if not record.created_by_app and not self.state.has_backup(activity_id):
            self.state.save_backup(activity_id, self.garmin_account.exercise_sets(activity_id))
        record.activity_id = activity_id
        self.push_sets(job, record)
        self.out(f"{job.day}: updated Garmin activity {activity_id}")

    def create_activity(self, job: Job) -> None:
        activity_id = self.garmin_account.create_strength_activity(
            job.workout.start,
            max(60.0, job.workout.duration_s),
            self.settings.activity_name,
            str(self.settings.time_zone),
        )
        # Record it before anything else can fail, so a rerun never creates a second one.
        record = SessionRecord(job.session.id, job.day.isoformat(), activity_id, created_by_app=True)
        self.state.save_session(record)
        self.garmin_account.set_description(activity_id, f"Created by jefit2garmin from Jefit session {job.session.id}")
        self.push_sets(job, record)
        self.out(f"{job.day}: created Garmin activity {activity_id}")

    def merge_into_watch_activity(self, job: Job) -> None:
        """Move the sets from the app-created activity onto the watch activity, then delete ours."""
        ours, watch = job.record.activity_id, job.watch_activity.id
        if not self.state.has_backup(watch):
            self.state.save_backup(watch, self.garmin_account.exercise_sets(watch))
        job.record.activity_id = watch
        job.record.created_by_app = False
        self.push_sets(job, job.record)
        try:
            self.garmin_account.delete_activity(ours)
        except GarminError as exc:
            self.warn(f"{job.day}: sets moved to watch activity {watch}, but deleting the activity "
                      f"jefit2garmin created ({ours}) failed: {exc}. Delete it in Garmin Connect.")
        self.out(f"{job.day}: merged into watch activity {watch} (removed {ours})")

    def push_sets(self, job: Job, record: SessionRecord) -> None:
        """Replace the activity's sets, then record what Garmin stored."""
        log.debug("%s: pushing %d sets to Garmin activity %s: %s", job.day, job.workout.active_set_count,
                  record.activity_id, "; ".join(e.describe() for e in self.exercises_sent(job)))
        self.garmin_account.replace_exercise_sets(record.activity_id, job.workout.sets)
        record.fingerprint = job.workout.fingerprint
        record.garmin_sets = self._comparable(self.garmin_account.exercise_sets(record.activity_id))
        record.timers = job.workout.rest_timers if job.freeze_timers else None  # None: look up again next run
        record.unmapped = job.workout.unmapped
        self.state.save_session(record)

    # --- Garmin, logged into on first use --------------------------------------------------------

    @property
    def garmin_account(self) -> GarminClient:
        """The Garmin client, logged in. Only account calls use this, so a run that needs
        nothing from the account never logs in."""
        if not self.garmin.logged_in:
            self.out("Logging in to Garmin")
            self.garmin.login()
        return self.garmin

    @property
    def watch_activities(self) -> list[Activity]:
        """Strength activities around the selected days, except ones the app created."""
        if self._watch_activities is None:
            first, last = self._garmin_days
            ours = self.state.activities_created_by_app()
            self._watch_activities = [a for a in self.garmin_account.strength_activities(first, last) if a.id not in ours]
        return self._watch_activities

    # --- helpers ---------------------------------------------------------------------------------

    def exercise_name(self, exercise_id: str) -> str:
        """Jefit's name for an exercise, for messages; "?" if unknown."""
        try:
            found = self.jefit.exercise(exercise_id)
        except JefitError:
            found = None
        return found.name if found else "?"

    def today(self) -> date:
        return datetime.now(self.settings.time_zone).date()

    def out(self, message: str) -> None:
        """Show a message, and record it in the log file."""
        log.info("%s", message)
        self._print(message)

    def warn(self, message: str) -> None:
        log.warning("%s", message)
        self._print(f"warning: {message}")

    @staticmethod
    def _comparable(sets: list[dict]) -> list[list]:
        """The parts of Garmin's stored sets that matter, for spotting edits made in Connect."""
        out = []
        for s in sets:
            exercise = (s.get("exercises") or [{}])[0]
            weight = s.get("weight")
            out.append([
                s.get("setType"),
                exercise.get("category"),
                exercise.get("name"),
                s.get("repetitionCount"),
                None if weight is None else round(weight),
                round(s.get("duration") or 0),
                (s.get("startTime") or "")[:19],
            ])
        return out
