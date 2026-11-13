"""The user's Jefit → Garmin exercise mapping file, exercise_map.toml.

Format (also in docs/DESIGN.md):

    format_version = 1

    [exercises]
    d_000000000012 = { jefit = "Barbell Squat", category = "SQUAT", name = "BARBELL_BACK_SQUAT" }

    [skip]
    d_000000000319 = "Step Machine"

    [unreviewed]
    d_000000000217 = { jefit = "Machine Hip Adduction", category = "", name = "" }

`category = ""` means not mapped yet (left out, with a warning). `name = ""` means the generic
entry for the category. The app only ever appends to `[unreviewed]`, which must stay last.

Under the user's file sits the built-in map (builtin_exercise_map.toml, shipped with the code):
mappings for Jefit's built-in exercises, in a single [base] section with the same line format.
An exercise in the user's file, in any section, uses the user's line; any other exercise uses
the built-in line if there is one. So the user's file only needs custom exercises and the
built-in mappings they want different.
"""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from importlib.resources import files
from pathlib import Path

from .clients.garmin import GarminExerciseList
from .clients.jefit import Exercise, JefitExerciseList

FORMAT_VERSION = 1
SECTIONS = ("exercises", "skip", "unreviewed")
UNKNOWN_LABEL = "(not in Jefit's exercise list)"
BUILTIN_MAP_FILE = files("jefit2garmin") / "builtin_exercise_map.toml"

TEMPLATE = f"""\
# jefit2garmin exercise mapping
#
# Tells jefit2garmin which Garmin exercise to record for each Jefit exercise.
# Every line maps one Jefit exercise ID:
#
#   d_000000000012 = {{ jefit = "Barbell Squat", category = "SQUAT", name = "BARBELL_BACK_SQUAT" }}
#
#   d_.../u_...  Jefit exercise ID (d_ = built-in, u_ = your custom exercises).
#   jefit        Jefit's name for it. Only a label; a mismatch with the ID is a warning.
#   category     Garmin exercise category. Empty = not mapped yet: the exercise is
#                not sent to Garmin, and each run prints a warning until you map it.
#   name         Garmin exercise within that category. Empty = generic for the category.
#
# Valid category and name values are listed in state/garmin_exercises.md.
#
# Exercises that aren't in this file use jefit2garmin's built-in map, which covers
# many of Jefit's built-in exercises (src/jefit2garmin/builtin_exercise_map.toml).
# To map one of those differently, copy its line into [exercises] here and edit it:
# a line in this file always wins.
#
# Sections:
#   [exercises]   Mappings you have reviewed.
#   [skip]        Exercises never sent to Garmin. Value is just the Jefit name, or move a
#                 line here unchanged (its category and name are ignored).
#   [unreviewed]  New exercises added by jefit2garmin. Move lines out once checked.
#                 Must stay the last section: new lines are appended to the end.
#
# jefit2garmin only ever appends to this file. It never edits, moves or removes
# your lines or comments. Delete a line and the exercise goes back to the built-in
# mapping, or is re-added here the next time it appears in a workout.

format_version = {FORMAT_VERSION}

[exercises]

[skip]

# ------------------------------------------------------------------------------
# New exercises are added below. Check each line, fix it if needed, then move it
# into [exercises] or [skip]. Lines left here are still used as they are.
# ------------------------------------------------------------------------------
[unreviewed]
"""


class ExerciseMapError(Exception):
    """exercise_map.toml can't be used; the message lists every problem found."""


@dataclass(frozen=True)
class MapEntry:
    """One line of [exercises] or [unreviewed], or of the built-in map's [base]."""

    exercise_id: str
    jefit_label: str
    category: str | None  # None = not mapped yet
    name: str | None  # None = generic for the category
    section: str


@dataclass(frozen=True)
class NewEntry:
    """A line the app appends to [unreviewed] for an exercise it hasn't seen before."""

    exercise_id: str
    jefit_label: str
    category: str | None  # None = no confident match; the user has to fill it in
    name: str | None
    candidates: tuple[tuple[str, str, str], ...] = ()  # (category, name, display), when unsure


@dataclass
class MapCheck:
    """What ExerciseMap.check() found."""

    invalid: dict[str, str] = field(default_factory=dict)  # the user's file: exercise ID -> message
    builtin_invalid: dict[str, str] = field(default_factory=dict)  # the built-in map, likewise
    warnings: list[str] = field(default_factory=list)


class ExerciseMap:
    """exercise_map.toml (created from the template if missing) over the built-in map."""

    def __init__(self, path: Path, builtin: dict[str, MapEntry] | None = None) -> None:
        """
        Args:
            path: The user's exercise_map.toml.
            builtin: The built-in map's entries; read from BUILTIN_MAP_FILE when not given.

        Raises:
            ExerciseMapError: Either file can't be read; the message lists every problem.
        """
        self.path = path
        self.entries: dict[str, MapEntry] = {}
        self.skipped: dict[str, str] = {}  # exercise ID -> Jefit name
        self.builtin = load_builtin_map() if builtin is None else dict(builtin)
        self._held_back: set[str] = set()  # left out this run; see hold_back()
        if not path.exists():
            path.write_text(TEMPLATE)
        self._text = path.read_text()  # kept for line numbers in messages
        self._read()

    # -- lookups ----------------------------------------------------------------------------

    def knows(self, exercise_id: str) -> bool:
        """The exercise has a line in the user's file (in any section) or in the built-in map."""
        return exercise_id in self.entries or exercise_id in self.skipped or exercise_id in self.builtin

    def uses_builtin(self, exercise_id: str) -> bool:
        """The exercise's mapping comes from the built-in map (the user's file doesn't have it)."""
        return exercise_id in self.builtin and exercise_id not in self.entries and exercise_id not in self.skipped

    def drop_builtin(self, exercise_id: str) -> None:
        """Stop using a built-in line this run (Garmin no longer accepts its exercise)."""
        self.builtin.pop(exercise_id, None)

    def is_skipped(self, exercise_id: str) -> bool:
        return exercise_id in self.skipped

    def is_held_back(self, exercise_id: str) -> bool:
        return exercise_id in self._held_back

    def hold_back(self, exercise_id: str) -> None:
        """Leave an exercise out of this run without calling it unmapped.

        For exercises that can't be dealt with until a download works: a new exercise that
        couldn't be added to the file, or a pair the Garmin list couldn't confirm.
        """
        self._held_back.add(exercise_id)

    def garmin_exercise_for(self, exercise_id: str) -> tuple[str, str | None] | None:
        """(category, name) to send to Garmin, or None when not mapped, skipped or unknown.

        The user's line wins; the built-in line is used only for exercises the user's file
        doesn't have at all.
        """
        if exercise_id in self._held_back or exercise_id in self.skipped:
            return None
        entry = self.entries.get(exercise_id) or self.builtin.get(exercise_id)
        if entry is None or entry.category is None:
            return None
        return entry.category, entry.name

    # -- checking ---------------------------------------------------------------------------

    def check(self, jefit: JefitExerciseList | None, garmin: GarminExerciseList | None) -> MapCheck:
        """Check every line against both exercise lists.

        Args:
            jefit: Jefit's exercise list; None if unavailable (the label checks are skipped).
            garmin: Garmin's exercise list; None if unavailable (nothing can be found invalid).

        Returns:
            invalid: each category/name pair in the user's file the Garmin list doesn't have;
            Garmin would reject a whole workout containing one. builtin_invalid: the same for
            built-in lines the user hasn't overridden. warnings: a Jefit label in the user's
            file that doesn't match, or an ID not in Jefit's list (retired IDs are legitimate,
            so that can't be an error).
        """
        result = MapCheck()
        if garmin is not None:
            for e in self.entries.values():
                if e.category and not garmin.is_valid(e.category, e.name):
                    result.invalid[e.exercise_id] = (
                        f"line {self._line_of(e.exercise_id, e.section)}: {e.exercise_id} ({e.jefit_label}): "
                        f"{e.category}/{e.name or '(generic)'} is not a valid Garmin exercise"
                        + ('; for the generic exercise use name = ""' if e.name == e.category else "")
                    )
            for e in self.builtin.values():
                if self.uses_builtin(e.exercise_id) and not garmin.is_valid(e.category, e.name):
                    result.builtin_invalid[e.exercise_id] = (
                        f"{e.exercise_id} ({e.jefit_label}): {e.category}/{e.name or '(generic)'}"
                    )

        if jefit is not None:
            labels = [(e.exercise_id, e.jefit_label) for e in self.entries.values()] + list(self.skipped.items())
            for exercise_id, label in labels:
                known = jefit.get(exercise_id)
                if known is None:
                    if label != UNKNOWN_LABEL:
                        result.warnings.append(f"{exercise_id} ({label}) is not in Jefit's exercise list (retired or mistyped)")
                elif known.name != label.strip():
                    result.warnings.append(f"{exercise_id}: labelled {label!r} but Jefit calls it {known.name!r}")
        return result

    # -- new exercises ----------------------------------------------------------------------

    def suggest(self, exercise_id: str, jefit_exercise: Exercise | None, garmin: GarminExerciseList) -> NewEntry:
        """A new [unreviewed] line: filled in when a Garmin name matches, else with candidates.

        Confident only when the Jefit name and a Garmin display name have the same words in any
        order ("Cable Seated Row" = "Seated Cable Row").

        Args:
            exercise_id: The Jefit ID.
            jefit_exercise: Jefit's entry for it; None if Jefit doesn't have it (retired).
            garmin: Garmin's exercise list, to match against.
        """
        if jefit_exercise is None:
            return NewEntry(exercise_id, UNKNOWN_LABEL, None, None)
        label = jefit_exercise.name
        names = [label, *jefit_exercise.nicknames]
        wanted = _words(label)

        scored = []
        for ex in garmin.all():
            words = _words(ex.display)
            if words == wanted:
                return NewEntry(exercise_id, label, ex.category, ex.name)
            score = max(_similarity(_words(n), words) for n in names)
            if score >= 0.5:
                scored.append((score, ex.category, ex.name, ex.display))
        scored.sort(reverse=True)
        return NewEntry(exercise_id, label, None, None, tuple((c, n, d) for _, c, n, d in scored[:3]))

    def add(self, new: NewEntry) -> None:
        """Use a new entry in this run. `append_unreviewed` writes it to the file."""
        self.entries[new.exercise_id] = MapEntry(new.exercise_id, new.jefit_label, new.category, new.name, "unreviewed")

    def append_unreviewed(self, new_entries: list[NewEntry], today: date) -> None:
        """Append lines to [unreviewed] without touching anything already in the file."""
        if not new_entries:
            return
        text = self.path.read_text()
        headers = re.findall(r"^\s*\[([^\]]+)\]", text, flags=re.M)
        if "unreviewed" in headers and headers[-1].strip() != "unreviewed":
            raise ExerciseMapError(
                f"{self.path.name}: [unreviewed] must be the last section so new exercises can be appended. "
                "Move it to the end of the file."
            )
        out = [] if text.endswith("\n") else [""]
        if "unreviewed" not in headers:
            out.append("\n[unreviewed]")
        out.append(f"\n# Added {today.isoformat()}")
        for entry in new_entries:
            if entry.category is None and entry.candidates:
                out.append("# No confident match. Candidates:")
                out += [f'#   category = "{c}", name = "{n}"   # {d}' for c, n, d in entry.candidates]
            out.append(
                f"{entry.exercise_id} = {{ jefit = {_toml(entry.jefit_label)}, "
                f"category = {_toml(entry.category or '')}, name = {_toml(entry.name or '')} }}"
            )
        with self.path.open("a") as f:
            f.write("\n".join(out) + "\n")

    # -- reading the file -------------------------------------------------------------------

    def _read(self) -> None:
        try:
            data = tomllib.loads(self._text)
        except tomllib.TOMLDecodeError as exc:
            raise ExerciseMapError(f"{self.path.name}: {exc}") from exc

        problems = []
        if data.get("format_version") != FORMAT_VERSION:
            problems.append(f"format_version must be {FORMAT_VERSION}, found {data.get('format_version')!r}")
        if "base" in data:
            problems.append("[base] is the built-in map's section; put your own lines in [exercises]")
        unexpected = set(data) - set(SECTIONS) - {"format_version", "base"}
        if unexpected:
            problems.append(f"unknown sections or keys: {', '.join(sorted(unexpected))}")

        seen: dict[str, str] = {}  # exercise ID -> section it was first found in
        for section in SECTIONS:
            table = data.get(section) or {}
            if not isinstance(table, dict):
                problems.append(f"[{section}] must be a section")
                continue
            for exercise_id, value in table.items():
                where = f"line {self._line_of(exercise_id, section)}: {exercise_id}"
                if exercise_id in seen:
                    problems.append(f"{where} is in both [{seen[exercise_id]}] and [{section}]")
                    continue
                seen[exercise_id] = section
                problem = self._read_line(section, exercise_id, value)
                if problem:
                    problems.append(f"{where}: {problem}")
        if problems:
            raise ExerciseMapError(f"{self.path.name} has problems:\n  " + "\n  ".join(problems))

    def _read_line(self, section: str, exercise_id: str, value) -> str | None:
        """Store one line; returns what's wrong with it, if anything."""
        if section == "skip":
            # Either just the name, or a line moved here unchanged from another section (its
            # category and name don't matter: nothing in [skip] is sent to Garmin).
            if isinstance(value, dict) and not set(value) - {"jefit", "category", "name"}:
                value = str(value.get("jefit", ""))
            if not isinstance(value, str):
                return 'in [skip] the value is the name, e.g. = "Step Machine", or a line moved from another section'
            self.skipped[exercise_id] = value
            return None
        if not isinstance(value, dict):
            return 'expected { jefit = "...", category = "...", name = "..." }'
        extra = set(value) - {"jefit", "category", "name"}
        if extra:
            return f"unknown keys {', '.join(sorted(extra))}"
        category = (value.get("category") or "").strip()
        name = (value.get("name") or "").strip()
        if name and not category:
            return "name is set but category is empty"
        self.entries[exercise_id] = MapEntry(
            exercise_id=exercise_id,
            jefit_label=str(value.get("jefit", "")),
            category=category or None,
            name=name or None,
            section=section,
        )
        return None

    def _line_of(self, key: str, section: str) -> int | str:
        """Line number of `key` inside `[section]`, for messages."""
        current = None
        for number, line in enumerate(self._text.splitlines(), 1):
            header = re.match(r"\s*\[([^\]]+)\]", line)
            if header:
                current = header.group(1).strip()
            elif current == section and re.match(rf"\s*{re.escape(key)}\s*=", line):
                return number
        return "?"


# --- The built-in map -----------------------------------------------------------------------------


def load_builtin_map(source=BUILTIN_MAP_FILE) -> dict[str, MapEntry]:
    """Read the built-in map's [base] section.

    Raises:
        ExerciseMapError: The file is malformed (a mistake in a release; the tests check it).
    """
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExerciseMapError(f"built-in exercise map ({source}): {exc}") from exc

    problems = []
    if data.get("format_version") != FORMAT_VERSION:
        problems.append(f"format_version must be {FORMAT_VERSION}, found {data.get('format_version')!r}")
    unexpected = set(data) - {"format_version", "map_version", "base"}
    if unexpected:
        problems.append(f"unknown sections or keys: {', '.join(sorted(unexpected))}")
    entries: dict[str, MapEntry] = {}
    for exercise_id, value in (data.get("base") or {}).items():
        if not re.fullmatch(r"d_\d{12}", exercise_id):
            problems.append(f"{exercise_id}: only Jefit's built-in exercises (d_ and 12 digits) belong here")
        elif not isinstance(value, dict) or set(value) != {"jefit", "category", "name"}:
            problems.append(f'{exercise_id}: expected {{ jefit = "...", category = "...", name = "..." }}')
        elif not str(value["category"]).strip():
            problems.append(f"{exercise_id}: category is empty")
        else:
            entries[exercise_id] = MapEntry(
                exercise_id, str(value["jefit"]), value["category"].strip(), value["name"].strip() or None, "base"
            )
    if problems:
        raise ExerciseMapError(f"built-in exercise map ({source}) has problems:\n  " + "\n  ".join(problems))
    return entries


# --- Name matching (used by suggest) ------------------------------------------------------------


def _words(text: str) -> frozenset[str]:
    """Lower-case words with a plural 's' dropped: "Cable Rows" -> {"cable", "row"}."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)


def _similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    overlap = len(a & b) / len(a | b)
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return (overlap + ratio) / 2


def _toml(value: str) -> str:
    """A TOML string (JSON string escapes are valid TOML)."""
    return json.dumps(value, ensure_ascii=False)


# --- garmin_exercises.md ---------------------------------------------------------------------------


def write_garmin_reference(path: Path, garmin: GarminExerciseList) -> bool:
    """Write garmin_exercises.md, every valid category/name, for editing exercise_map.toml.

    Only when the file is missing or older than the Garmin list it describes.

    Returns:
        True if the file was written.
    """
    if path.exists():
        written = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if written >= garmin.fetched_at:
            return False
    categories = garmin.categories
    total = sum(len(v) for v in categories.values())
    lines = [
        "# Garmin exercise reference",
        "",
        "Generated by jefit2garmin; do not edit. Rebuilt whenever the Garmin exercise list is downloaded again.",
        f"{len(categories)} categories, {total} exercises, from Garmin's list of {garmin.fetched_at:%Y-%m-%d}.",
        "",
        "## How to use this with `exercise_map.toml`",
        "",
        "- Each `##` heading below is a valid `category` value.",
        "- The first column under it is a valid `name` value for that category.",
        "- A `name` is only valid under its own category; the same key can exist in several.",
        '- For a generic entry, set `category` and leave `name = ""`.',
        "",
        "```toml",
        'd_000000000012 = { jefit = "Barbell Squat", category = "SQUAT", name = "BARBELL_BACK_SQUAT" }',
        "```",
        "",
        "## Categories",
        "",
        *[f"- `{c}` ({len(e)})" for c, e in categories.items()],
        "",
    ]
    for category, exercises in categories.items():
        width = max(len(n) for n in exercises)
        dwidth = max(len(e.display) for e in exercises.values())
        lines += [f"## {category}", "", "```", f'{"name".ljust(width)}  {"shown in Garmin Connect as".ljust(dwidth)}  primary muscles']
        for name, e in exercises.items():
            muscles = ", ".join(m.lower() for m in e.primary_muscles)
            lines.append(f"{name.ljust(width)}  {e.display.ljust(dwidth)}  {muscles}".rstrip())
        lines += ["```", ""]
    path.write_text("\n".join(lines))
    return True
