# Changelog

What changed in each release of jefit2garmin, newest first. Each entry says whether you need to
do anything when you update.

Version numbers are MAJOR.MINOR.PATCH:

- **PATCH** (1.0.0 → 1.0.1): bug fixes, built-in map additions, docs. Just update.
- **MINOR** (1.0.x → 1.1.0): new features. Existing settings and files keep working.
- **MAJOR** (1.x → 2.0.0): something in `userdata/` has to change. The entry says what.

## 1.0.0 — 2026-11-12

First release.

- Copies Jefit strength workouts (exercises, sets, reps, weights) into Garmin Connect: adds them
  to the strength activity recorded on your watch, or creates an activity when there is none.
- Syncs the last 28 days on each run, and any day with `--date`, `--since` and `--until`.
  Workouts changed in Jefit are sent again; sets you edited in Garmin Connect are left alone
  unless you use `--overwrite-garmin-edits`.
- Built-in exercise map covering 81 of Jefit's exercises; your own `exercise_map.toml` adds to
  it or overrides it. New exercises are added to your map for you to review.
- `--dry-run` to see what would change, `--refresh-caches`, `--reset`, `--version`.
- Log file in `userdata/logs/`, and a report in `userdata/state/failed/` when Garmin rejects a
  workout.
