"""Turns one Jefit session into the Garmin exercise sets for it.

Exercises, reps and weights always come from the Jefit log. Only set durations are worked out:

- A set ends when it was logged in Jefit.
- Its duration is the gap since the previous set of the same exercise minus that set's rest
  timer from the routine, when the result is plausible (MIN_SET_S..MAX_SET_S).
- Otherwise: the average of that exercise's other worked-out durations, else SECONDS_PER_REP
  per rep. Timed sets (planks) use the duration Jefit logged.
- Old logs without per-set times: sets are spread evenly over the session.

Gaps between sets become REST entries. Cardio is left out (recorded as separate activities).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property

from .clients.garmin import ExerciseSet, exercise_sets_payload
from .clients.jefit import ExerciseLog, LogSet, RoutineDay, Session
from .exercise_map import ExerciseMap

SECONDS_PER_REP = 3
MIN_SET_S = 10
MAX_SET_S = 180
DEFAULT_SET_S = 40  # a set with neither reps nor a duration
LB_TO_GRAMS = 453.59237

# Routine rest timers for one session: day_exercise_id -> seconds of rest after each set
# (None where unknown)
RestTimers = dict[str, list[int | None]]


@dataclass
class GarminWorkout:
    """What to send to Garmin for one Jefit session."""

    sets: list[ExerciseSet]  # ACTIVE sets with REST entries between them
    unmapped: list[str]  # exercise IDs left out because they have no Garmin mapping
    rest_timers: RestTimers  # the routine timers used; frozen in the sync record on first sync
    start: datetime
    end: datetime

    @property
    def has_sets(self) -> bool:
        return any(s.kind == "ACTIVE" for s in self.sets)

    @property
    def active_set_count(self) -> int:
        return sum(s.kind == "ACTIVE" for s in self.sets)

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()

    @cached_property
    def fingerprint(self) -> str:
        """Identifies the content; equal fingerprints mean nothing new to push."""
        body = exercise_sets_payload(0, self.sets)["exerciseSets"]
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@dataclass
class _PlannedSet:
    """One logged set on its way to becoming a Garmin set."""

    step: int  # which exercise of the workout (Garmin groups sets by it)
    log: ExerciseLog
    position: int  # position of the set within its exercise
    logged: LogSet
    category: str
    name: str | None
    duration: float | None = None  # worked out by the builder


class WorkoutBuilder:
    def __init__(self, exercise_map: ExerciseMap) -> None:
        self.exercise_map = exercise_map

    def build(self, session: Session, rest_timers: RestTimers) -> GarminWorkout:
        planned, unmapped = self._mapped_sets(session)
        used_timers = {p.log.day_exercise_id: rest_timers[p.log.day_exercise_id]
                       for p in planned if p.log.day_exercise_id in rest_timers}

        if planned and all(p.logged.logged_at for p in planned):
            sets, end = self._timed_from_timestamps(planned, used_timers, session.started_at)
        else:
            sets, end = self._spread_evenly(planned, session)
        end = max(end, session.started_at + timedelta(seconds=session.total_time_s or 0))
        return GarminWorkout(sets=sets, unmapped=unmapped, rest_timers=used_timers, start=session.started_at, end=end)

    @staticmethod
    def rest_timers(session: Session, routine_day: RoutineDay | None) -> RestTimers:
        """The routine's rest timers for this session's exercises.

        Only exercises where the routine still matches the log (same routine entry, same
        exercise) get timers; for the rest, set durations are estimated.

        Args:
            session: The logged workout.
            routine_day: The routine day it was logged against, as downloaded; None if unknown.
        """
        if routine_day is None:
            return {}
        by_id = {e.id: e for e in routine_day.exercises}
        timers: RestTimers = {}
        for log in session.logs:
            entry = by_id.get(log.day_exercise_id or "")
            if entry and entry.exercise_id == log.exercise_id:
                timers[log.day_exercise_id] = [s.rest_time_s for s in entry.sets]
        return timers

    # -- which sets go to Garmin ------------------------------------------------------------

    def _mapped_sets(self, session: Session) -> tuple[list[_PlannedSet], list[str]]:
        """Sets of every mapped exercise; IDs of exercises left out as unmapped."""
        planned: list[_PlannedSet] = []
        unmapped: list[str] = []
        step = 0
        for log in session.logs:
            if (log.record_type == "cardio" or self.exercise_map.is_skipped(log.exercise_id)
                    or self.exercise_map.is_held_back(log.exercise_id)):
                continue
            garmin_exercise = self.exercise_map.garmin_exercise_for(log.exercise_id)
            if garmin_exercise is None:
                if log.exercise_id not in unmapped:
                    unmapped.append(log.exercise_id)
                continue
            category, name = garmin_exercise
            planned += [_PlannedSet(step, log, i, s, category, name) for i, s in enumerate(log.sets)]
            step += 1
        return planned, unmapped

    # -- timing -----------------------------------------------------------------------------

    def _timed_from_timestamps(
        self, planned: list[_PlannedSet], rest_timers: RestTimers, session_start: datetime
    ) -> tuple[list[ExerciseSet], datetime]:
        planned.sort(key=lambda p: p.logged.logged_at)
        self._work_out_durations(planned, rest_timers)

        sets: list[ExerciseSet] = []
        previous_end = session_start
        for p in planned:
            end = p.logged.logged_at
            start = max(end - timedelta(seconds=p.duration), previous_end)  # never overlap the previous set
            duration = max(1.0, (end - start).total_seconds())
            self._add_set(sets, p, end - timedelta(seconds=duration), duration, previous_end)
            previous_end = end
        return sets, previous_end

    def _work_out_durations(self, planned: list[_PlannedSet], rest_timers: RestTimers) -> None:
        """Set `duration` on every planned set (they are in the order they were logged)."""
        previous_in_exercise: dict[int, _PlannedSet] = {}
        worked_out: dict[int, list[float]] = {}  # step -> durations derived from timestamps
        for p in planned:
            previous = previous_in_exercise.get(p.step)
            previous_in_exercise[p.step] = p
            if p.logged.duration_s:  # timed set: Jefit logged how long it took
                p.duration = p.logged.duration_s
                continue
            if previous is None:
                continue  # first set of the exercise: nothing to measure from
            rest = self._rest_after(rest_timers.get(p.log.day_exercise_id), previous.position)
            if rest is None:
                continue
            gap = (p.logged.logged_at - previous.logged.logged_at).total_seconds()
            if MIN_SET_S <= gap - rest <= MAX_SET_S:
                p.duration = gap - rest
                worked_out.setdefault(p.step, []).append(p.duration)

        for p in planned:
            if p.duration is None:
                others = worked_out.get(p.step)
                p.duration = sum(others) / len(others) if others else self._estimate(p.logged)

    def _spread_evenly(self, planned: list[_PlannedSet], session: Session) -> tuple[list[ExerciseSet], datetime]:
        """Old logs with no per-set times: give each set an equal slot of the session."""
        if not planned:
            return [], session.started_at
        total = max(session.total_time_s or 0, len(planned) * MIN_SET_S)
        slot = total / len(planned)
        sets: list[ExerciseSet] = []
        previous_end = session.started_at
        for n, p in enumerate(planned):
            start = session.started_at + timedelta(seconds=slot * n)
            duration = min(p.logged.duration_s or self._estimate(p.logged), slot)
            self._add_set(sets, p, start, duration, previous_end)
            previous_end = start + timedelta(seconds=duration)
        return sets, previous_end

    @staticmethod
    def _rest_after(rest: list[int | None] | None, position: int) -> int | None:
        """Rest timer after the set at `position`; extra logged sets take the last set's timer."""
        if not rest:
            return None
        return rest[min(position, len(rest) - 1)]

    @staticmethod
    def _estimate(logged: LogSet) -> float:
        if logged.duration_s:
            return logged.duration_s
        if logged.reps:
            return float(min(MAX_SET_S, max(MIN_SET_S, logged.reps * SECONDS_PER_REP)))
        return float(DEFAULT_SET_S)

    # -- Garmin sets ------------------------------------------------------------------------

    def _add_set(
        self, sets: list[ExerciseSet], p: _PlannedSet, start: datetime, duration: float, previous_end: datetime
    ) -> None:
        """Append the set, preceded by a REST entry for any gap since the previous one."""
        rest = (start - previous_end).total_seconds()
        if sets and rest > 0:
            sets.append(ExerciseSet("REST", previous_end, rest, step=sets[-1].step))
        sets.append(
            ExerciseSet(
                "ACTIVE",
                start,
                duration,
                step=p.step,
                category=p.category,
                name=p.name,
                reps=p.logged.reps,
                weight_g=self._grams(p.logged.weight, p.log.unit_system),
            )
        )

    @staticmethod
    def _grams(weight: float | None, unit_system: str) -> float | None:
        if weight is None:
            return None  # bodyweight and timed sets: Jefit recorded no weight
        return weight * (1000.0 if unit_system == "metric" else LB_TO_GRAMS)
