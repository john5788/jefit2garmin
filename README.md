# jefit2garmin

Copies the strength workouts you log in **Jefit** (exercises, sets, reps, weights) into
**Garmin Connect**.

If your watch recorded the workout, the sets are added to that activity, so it keeps its heart
rate data and gains the exercise list, reps and weights. If the watch recorded nothing, a new
strength activity is created for the workout.

> Jefit has no public API. jefit2garmin uses the same web requests the Jefit website makes, so
> it can stop working if Jefit changes their site. See [When something stops working](#when-something-stops-working).

## What you need

- Python 3.12 or newer
- A Jefit account and a Garmin Connect account
- Basic comfort with a terminal and editing a text file

## Install

In the project folder:

```console
$ python3 -m venv .venv
$ .venv/bin/pip install -e .
```

This installs the `jefit2garmin` command inside the project's `.venv` folder. Run it as
`.venv/bin/jefit2garmin`, or activate the environment once per terminal session and then just
type `jefit2garmin`:

```console
$ source .venv/bin/activate
```

The examples below assume it's activated. Always run the command from the project folder.

## Set up

Run jefit2garmin once. It creates your settings file and stops:

```console
$ jefit2garmin
Created /…/jefit2garmin/userdata/config.toml.
Fill in your Jefit and Garmin logins there, then run jefit2garmin again.
```

Open `userdata/config.toml` and fill in your accounts and time zone:

```toml
time_zone = "America/Los_Angeles"   # for activities jefit2garmin creates

[jefit]
username = "you@example.com"
password = "..."

[garmin]
email = "you@example.com"
password = "..."
```

Your own files all live in the `userdata/` folder. `config.toml` holds your passwords in plain
text: keep it private (everything in `userdata/` is excluded from git). Every other setting in
it is optional and explained in the file.

## First run

**1. Do a dry run.** It reads your last 28 days of Jefit workouts and creates
`userdata/exercise_map.toml`, but changes nothing in Garmin Connect:

```console
$ jefit2garmin --dry-run
```

Garmin may ask for a multi-factor authentication (MFA) code the first time. After that the
login is remembered.

**2. Review `userdata/exercise_map.toml`.** Garmin only accepts exercises from its own list, so every
Jefit exercise has to be matched to a Garmin one. Many of Jefit's built-in exercises are already
matched by jefit2garmin's **built-in map** (see [Built-in exercise map](#built-in-exercise-map));
those need nothing from you. For every other exercise it found, including all your custom ones,
the dry run added a line to your file:

```toml
[unreviewed]
d_000000000012 = { jefit = "Barbell Squat", category = "SQUAT", name = "BARBELL_BACK_SQUAT" }
# No confident match. Candidates:
#   category = "HIP_STABILITY", name = "STANDING_ADDUCTION"   # Standing Adduction
d_000000000217 = { jefit = "Machine Hip Adduction", category = "", name = "" }
```

- Lines with a `category` filled in were matched automatically. Check they're right.
- Lines with `category = ""` need you to choose. Pick one of the candidates or look one up in
  `userdata/state/garmin_exercises.md`, which lists every valid `category` and `name`. Until
  you fill it in, that exercise is left out of Garmin with a warning.
- `name = ""` means the generic exercise for that category.
- Once checked, move lines up into `[exercises]`. Exercises you never want sent to Garmin go in
  `[skip]`: move the line as it is, or shorten it to `d_000000000319 = "Step Machine"`. Lines
  left in `[unreviewed]` still work.
- Keep `[unreviewed]` as the last section: new exercises are added at the end of the file.

**3. Sync for real:**

```console
$ jefit2garmin
```

It prints what it did and ends with a summary such as `Summary: 9 updated, 2 created`.

## Everyday use

Just run `jefit2garmin` after your workouts, as often as you like. Each run:

- looks at your last 28 days of workouts, plus any older day it hasn't synced yet;
- sends only workouts that are new or changed, so running it twice does nothing the second
  time;
- adds new Jefit exercises to `exercise_map.toml` (check them when it says so).

To copy older history, give it a date range:

```console
$ jefit2garmin --since 2025-01-01                     # from a date until today
$ jefit2garmin --since 2025-01-01 --until 2025-06-30  # a range
$ jefit2garmin --date 2026-10-12                      # one day
```

Add `--dry-run` to any of these to see what would happen first. If a run would create more than
5 activities, it asks before doing so.

## How workouts end up in Garmin Connect

- **Watch recorded the workout:** the strength activity overlapping the Jefit workout gets the
  sets. Its heart rate and other data stay as they are.
- **No watch activity:** after 24 hours (to give the watch time to sync) a new private strength
  activity named "Strength" is created, with the note "Created by jefit2garmin from Jefit
  session …". You can rename it; jefit2garmin tracks it by ID.
- **Watch activity shows up later:** the sets move onto it and the created activity is deleted.
- **Cardio** logged in Jefit is left out; Garmin records it as separate activities.
- **Set times:** Jefit records when you finish each set. jefit2garmin works out how long each
  set took from that and your routine's rest timer, so durations and rest periods in Garmin are
  close estimates.

## Changing things later

| You… | Next run |
|---|---|
| edit a workout in Jefit | updates it in Garmin (if it's within the last 28 days; otherwise use `--since`) |
| fix a mapping in `exercise_map.toml` | updates the affected workouts in the last 28 days; use `--since` for older ones |
| update jefit2garmin and the built-in map changed | the same, for exercises you haven't mapped yourself |
| edit a synced workout's sets in Garmin Connect | leaves it alone and warns you; `--overwrite-garmin-edits` replaces your edits with Jefit's version |
| delete a synced activity in Garmin Connect | never recreates it |
| delete a workout in Jefit | warns you, leaves the Garmin activity alone |

## Built-in exercise map

`src/jefit2garmin/builtin_exercise_map.toml` ships with jefit2garmin: Garmin matches for Jefit's
built-in exercises (`d_…` IDs, the same for every Jefit user), each checked against Garmin's
exercise list before a release. Open it to see what's covered; don't edit it, since updates
replace it.

Your own `exercise_map.toml` always wins. For any exercise that has a line in your file, in any
section, the built-in line is ignored:

| You want to… | In your `exercise_map.toml` |
|---|---|
| map a built-in exercise differently | copy its line from the built-in map into `[exercises]` and change it |
| never send it | put its line (or `d_… = "Name"`) in `[skip]` |
| go back to the built-in mapping | delete your line |

Mappings that came from the built-in map are marked "(built-in map)" in the log and in the
report of a workout Garmin refused.

## Your files

Everything is in `userdata/`. Keep that folder when you update jefit2garmin; the rest of the
project can be replaced freely.

| File | What it is |
|---|---|
| `config.toml` | your settings and passwords |
| `exercise_map.toml` | your Jefit → Garmin exercise matches; yours to edit |
| `logs/jefit2garmin.log` | what each run did, with times and more detail than the screen shows. Starts a new file at 1 MB and keeps the last 3 (`.log.1` … `.log.3`), so it stays under 4 MB. |
| `state/` | jefit2garmin's own files. Don't edit them; `--reset` deletes the folder. |
| `state/sync_state.db` | the record of what was synced where. Losing it means starting over. |
| `state/garmin_exercises.md` | every valid Garmin `category` and `name`, for editing the map (kept up to date) |
| `state/failed/` | details of workouts Garmin refused (see below); removed once they go through |
| `state/…` | copies of the exercise lists and routines, and your Garmin login |

## Settings

Optional settings in `userdata/config.toml`, with their defaults:

```toml
cache_max_days = 30          # download the exercise lists and routines again after this many days

[sync]
window_days = 28             # days looked at on a normal run
create_after_hours = 24      # wait this long for the watch before creating an activity
confirm_creates_over = 5     # ask before one run creates more activities than this
activity_name = "Strength"   # name of created activities
```

## All options

```
jefit2garmin [--date DAY | --since DAY [--until DAY]] [--dry-run] [--yes]
             [--overwrite-garmin-edits] [--refresh-caches] [--reset] [--config FILE]
jefit2garmin --version
```

| Option | Does |
|---|---|
| `--date`, `--since`, `--until` | which days to sync (`YYYY-MM-DD`, inclusive) instead of the last 28 days |
| `--dry-run` | show what would change; nothing is written to Garmin |
| `--yes` | don't ask before creating many activities (or before `--reset`) |
| `--overwrite-garmin-edits` | replace sets you edited in Garmin Connect with Jefit's version |
| `--refresh-caches` | download fresh copies of the exercise lists and routines |
| `--reset` | delete `userdata/state/` and start over; asks first. Keeps `config.toml` and `exercise_map.toml` and touches nothing in Garmin or Jefit. |
| `--config FILE` | use a different settings file; jefit2garmin keeps its files next to it (default: `userdata/config.toml`) |
| `--version` | show which version is installed |

`jefit2garmin --help` shows the same with examples.

## When something stops working

**Check that Jefit and Garmin still respond.** The tests log in to both services and download
a little data, using your `userdata/config.toml`. They change nothing:

```console
$ .venv/bin/pip install -e ".[dev]"     # once, to get pytest
$ .venv/bin/python -m pytest -s
```

If a Jefit test fails, Jefit has probably changed their site; if a Garmin test fails, Garmin or
the `garminconnect` library has. If both pass, the problem is in jefit2garmin itself.

**Read the log.** `userdata/logs/jefit2garmin.log` has every run's output with timestamps, plus
what the screen leaves out: each request to Jefit and Garmin and its result, what was decided
for each workout and which exercises were sent, and the full error when something fails. It
never contains your passwords.

**Common messages:**

- *"… is not mapped; its sets were left out"*: fill in that exercise in `exercise_map.toml`.
- *"exercise_map.toml has invalid Garmin exercises"*: a `category`/`name` doesn't exist in
  Garmin's list; the message gives the line number. Check spelling against
  `userdata/state/garmin_exercises.md`.
- *"Garmin rejected the sets (… API Error 400 …)"*: Garmin refused one of the exercises in that
  workout. The message lists every exercise it sent and its line in `exercise_map.toml`; change
  the suspect mapping and run again. The full request is saved in `userdata/state/failed/`.
  Garmin's exercise list contains a few entries Garmin then refuses; the known ones are caught
  before anything is sent (see `REJECTED_NAMES` in `src/jefit2garmin/clients/garmin.py`; add a
  newly found one there).
- *"sets on Garmin activity … were edited in Garmin Connect; not overwriting them"*: you (or
  Garmin) changed the sets. Keep your edits, or run with `--overwrite-garmin-edits`.
- *"… download failed …; using the copy from …"*: Jefit or Garmin couldn't be reached; the run
  carried on with saved copies. Usually fixes itself on a later run.
- *"no watch activity yet; will create one if none appears within 24 hours"*: nothing to do;
  give the watch time to sync.

## How it works

See [docs/DESIGN.md](docs/DESIGN.md) for the design and diagrams of the code.

## License

jefit2garmin is free software under the [GNU Affero General Public License v3.0 or later](LICENSE).
You may use, modify and share it, including commercially. If you distribute a modified version,
or let others use one over a network (for example as a hosted sync service), you must make its
source code available under the same license.

jefit2garmin is not affiliated with Jefit or Garmin. It uses their unofficial web interfaces,
which may change or stop working at any time.

