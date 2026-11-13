"""Command line: `jefit2garmin` (or `python -m jefit2garmin`).

main() parses the options and runs a sync through App, or `--reset` through reset_app_data().
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from collections.abc import Callable
from datetime import date
from importlib.metadata import version
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .app import App
from .clients.garmin import GarminError
from .clients.jefit import JefitError
from .exercise_map import ExerciseMapError
from .settings import CONFIG_TEMPLATE, Options, Settings

VERSION = version("jefit2garmin")  # from pyproject.toml, via the installed package

log = logging.getLogger("jefit2garmin")  # parent of every module's logger

LOG_MAX_BYTES = 1_000_000  # the log is rotated at this size ...
LOG_BACKUPS = 3  # ... keeping this many older files (jefit2garmin.log.1 to .3)

EXAMPLES = """\
examples:
  jefit2garmin                                   recent days ([sync] window_days, plus anything missed)
  jefit2garmin --date 2026-10-12                 one day
  jefit2garmin --since 2026-01-01                backfill from a date
  jefit2garmin --since 2026-01-01 --until 2026-03-31
  jefit2garmin --since 2026-01-01 --dry-run      show what would change
  jefit2garmin --dry-run                         first run: fill exercise_map.toml to review, sync nothing
  jefit2garmin --refresh-caches                  get new copies of the exercise lists and routines
  jefit2garmin --reset                           start again from a clean slate
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jefit2garmin",
        description="Copy Jefit strength workouts into Garmin Connect.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("userdata/config.toml"),
        help="settings file; the app keeps its files next to it (default: userdata/config.toml)",
    )
    parser.add_argument("--date", type=date.fromisoformat, help="sync one day (YYYY-MM-DD)")
    parser.add_argument("--since", type=date.fromisoformat, help="first day to sync (inclusive)")
    parser.add_argument("--until", type=date.fromisoformat, help="last day to sync (inclusive)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change; write nothing to Garmin or the sync record (local files such as "
             "exercise_map.toml and the caches are updated)",
    )
    parser.add_argument("--yes", action="store_true", help="don't ask before creating many activities, or before --reset")
    parser.add_argument(
        "--overwrite-garmin-edits",
        action="store_true",
        help="replace sets on the selected days even if they were edited in Garmin Connect",
    )
    parser.add_argument(
        "--refresh-caches",
        action="store_true",
        help="download the exercise lists and routines again, whatever the age of the cached copies",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete userdata/state/ (sync record, caches, Garmin login), then stop; keeps config.toml and exercise_map.toml",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args(argv)

    if args.reset and (args.date or args.since or args.until or args.overwrite_garmin_edits or args.refresh_caches):
        parser.error("--reset doesn't sync; run it on its own (with --yes or --dry-run if wanted)")
    if args.date and (args.since or args.until):
        parser.error("--date can't be combined with --since or --until")
    since, until = (args.date, args.date) if args.date else (args.since, args.until)
    if since and until and since > until:
        parser.error("--since is after --until")
    if not args.config.exists():
        return create_config(args.config, reset=args.reset)

    try:
        settings = Settings.load(args.config)
    except ValueError as exc:
        parser.error(str(exc))

    log_file = start_log(settings.log_dir)
    log.info("=== jefit2garmin %s: %s", VERSION, " ".join(sys.argv[1:] if argv is None else argv) or "(no options)")

    if args.reset:
        reset_app_data(settings, dry_run=args.dry_run, assume_yes=args.yes)
        return 0

    try:
        options = Options(
            since=since,
            until=until,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            overwrite_garmin_edits=args.overwrite_garmin_edits,
            refresh_caches=args.refresh_caches,
        )
        app = App(settings, options)
        report = app.run()
    except ExerciseMapError as exc:
        log.error("%s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (JefitError, GarminError) as exc:
        log.error("%s", exc, exc_info=True)
        print(f"error: {exc}\nDetails in {log_file}", file=sys.stderr)
        return 2
    except Exception:
        log.exception("unexpected error")
        print(f"Unexpected error; the full trace is in {log_file}", file=sys.stderr)
        raise
    for line in report.lines():
        log.info("%s", line)
        print(line)
    if report.failed:
        print(f"Details in {log_file}")
    return 2 if report.failed else 0


def start_log(log_dir: Path) -> Path:
    """Send this package's log messages to log_dir/jefit2garmin.log, rotated by size.

    Only jefit2garmin's own loggers write there: the libraries it uses log at a level of detail
    (and with data) that doesn't belong in the file.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "jefit2garmin.log"
    handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False  # the console gets its own output; don't print log lines twice
    return path


# --- first run -------------------------------------------------------------------------------------


def create_config(config_file: Path, *, reset: bool = False) -> int:
    """No config file yet: create it from the template, or point to a misplaced one.

    A config.toml in the current folder, when the app looks for userdata/config.toml, is the
    likely mistake; it holds passwords, so it is never moved automatically.

    Returns:
        The exit code: 1, since nothing was synced.
    """
    stray = Path("config.toml")
    if stray.exists() and stray.resolve() != config_file.resolve():
        print(f"Found {stray.resolve()}, but jefit2garmin reads {config_file}. Move it there.", file=sys.stderr)
        return 1
    if reset:
        print(f"{config_file} not found; nothing to reset.", file=sys.stderr)
        return 1
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(CONFIG_TEMPLATE)
    print(f"Created {config_file.resolve()}.")
    print("Fill in your Jefit and Garmin logins there, then run jefit2garmin again.")
    return 1


# --- --reset ---------------------------------------------------------------------------------------

RESET_WARNING = """\
The sync record is how jefit2garmin knows what it already did. Without it, the next run treats
every workout it looks at as never synced:
  - activities you deleted in Garmin Connect can be created again
  - sets you edited in Garmin Connect are overwritten without a warning
  - the backups of your watch activities' original sets are lost
  - Garmin asks you to log in again (with an MFA code if you use one)
Activities the app created before are matched again by time, so they aren't duplicated."""


def reset_app_data(
    settings: Settings,
    *,
    dry_run: bool = False,
    assume_yes: bool = False,
    out: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
) -> bool:
    """--reset: delete the state folder (userdata/state/), everything the app stored itself.

    That is the sync record, the caches, garmin_exercises.md and the Garmin login tokens.
    config.toml and exercise_map.toml are the user's and are kept. Nothing in Garmin Connect or
    Jefit is touched. Shows what is in the folder, prints what losing the sync record means,
    and asks before deleting.

    Args:
        settings: Where the folder is.
        dry_run: Only show what would be deleted.
        assume_yes: Don't ask first (--yes).
        out: Where messages go.
        ask: How to ask the question; returns the answer.

    Returns:
        True if the folder was deleted (or didn't exist), False if the user said no or it was
        a dry run.
    """
    state = settings.state_dir
    if not state.is_dir():
        out("Nothing to delete; jefit2garmin has no stored data.")
        return True

    out(f"This folder will be deleted: {state}/")
    for path in sorted(state.iterdir()):
        out(f"  {path.name}{'/' if path.is_dir() else ''}")
    out("Kept: config.toml, exercise_map.toml, and everything in Garmin Connect and Jefit.")
    if settings.sync_state_file.exists():
        out("")
        out(RESET_WARNING)
    if dry_run:
        out("Dry run; nothing deleted.")
        return False
    if not assume_yes:
        answer = ask("Delete it? [y/N] ")
        if answer.strip().lower() != "y":
            out("Nothing deleted.")
            return False

    shutil.rmtree(state)
    out("Deleted. The next run starts from scratch.")
    return True


if __name__ == "__main__":
    sys.exit(main())
