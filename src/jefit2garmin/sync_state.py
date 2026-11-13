"""The sync record (sync_state.db): which Jefit session was synced to which Garmin activity.

One row per Jefit session. The app identifies its own Garmin activities by ID here, never by
name, so renaming them in Garmin Connect is safe. Unlike the caches, this file must be kept.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       INTEGER PRIMARY KEY,  -- Jefit session ID
    day              TEXT NOT NULL,        -- local date of the workout
    activity_id      INTEGER,              -- Garmin activity holding its sets
    created_by_app   INTEGER NOT NULL DEFAULT 0,
    activity_deleted INTEGER NOT NULL DEFAULT 0,
    fingerprint      TEXT,                 -- of the payload last pushed
    garmin_sets      TEXT,                 -- the sets as Garmin stored them after that push
    timers           TEXT,                 -- rest timers used, frozen on first sync
    unmapped         TEXT,                 -- exercise IDs left out as unmapped
    synced_at        TEXT
);
CREATE TABLE IF NOT EXISTS backups (       -- a watch activity's sets before its first overwrite
    activity_id INTEGER PRIMARY KEY,
    sets        TEXT NOT NULL,
    saved_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class SessionRecord:
    """What is known about one Jefit session's sync."""

    session_id: int
    day: str
    activity_id: int | None = None
    created_by_app: bool = False  # the app created the activity (no watch recording)
    activity_deleted: bool = False  # the activity was deleted in Garmin Connect; don't recreate
    fingerprint: str | None = None
    garmin_sets: list | None = None
    timers: dict | None = None
    unmapped: list[str] = field(default_factory=list)
    synced_at: str | None = None


class SyncState:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- sessions ---------------------------------------------------------------------------

    def get_session(self, session_id: int) -> SessionRecord | None:
        row = self._db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._to_record(row) if row else None

    def sessions_on(self, day: str) -> list[SessionRecord]:
        rows = self._db.execute("SELECT * FROM sessions WHERE day = ?", (day,)).fetchall()
        return [self._to_record(r) for r in rows]

    def save_session(self, r: SessionRecord) -> None:
        r.synced_at = _now()
        self._db.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, day, activity_id, created_by_app, activity_deleted, fingerprint,
                garmin_sets, timers, unmapped, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.session_id, r.day, r.activity_id, int(r.created_by_app), int(r.activity_deleted),
                r.fingerprint, _to_json(r.garmin_sets), _to_json(r.timers), _to_json(r.unmapped), r.synced_at,
            ),
        )
        self._db.commit()

    def synced_days(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT DISTINCT day FROM sessions WHERE activity_id IS NOT NULL")}

    def activities_in_use(self) -> dict[int, int]:
        """Garmin activity ID → Jefit session ID, for every activity already holding a session."""
        rows = self._db.execute("SELECT activity_id, session_id FROM sessions WHERE activity_id IS NOT NULL")
        return {r[0]: r[1] for r in rows}

    def activities_created_by_app(self) -> set[int]:
        rows = self._db.execute(
            "SELECT activity_id FROM sessions WHERE created_by_app = 1 AND activity_id IS NOT NULL"
        )
        return {r[0] for r in rows}

    # -- backups ----------------------------------------------------------------------------

    def has_backup(self, activity_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM backups WHERE activity_id = ?", (activity_id,)).fetchone() is not None

    def save_backup(self, activity_id: int, sets: list) -> None:
        self._db.execute("INSERT OR IGNORE INTO backups VALUES (?, ?, ?)", (activity_id, json.dumps(sets), _now()))
        self._db.commit()

    # -- first run --------------------------------------------------------------------------

    def first_run(self) -> str | None:
        """Date of the first real (not dry) run, as YYYY-MM-DD."""
        row = self._db.execute("SELECT value FROM meta WHERE key = 'first_run'").fetchone()
        return row[0] if row else None

    def set_first_run(self, day: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta VALUES ('first_run', ?)", (day,))
        self._db.commit()

    @staticmethod
    def _to_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            day=row["day"],
            activity_id=row["activity_id"],
            created_by_app=bool(row["created_by_app"]),
            activity_deleted=bool(row["activity_deleted"]),
            fingerprint=row["fingerprint"],
            garmin_sets=_from_json(row["garmin_sets"]),
            timers=_from_json(row["timers"]),
            unmapped=_from_json(row["unmapped"]) or [],
            synced_at=row["synced_at"],
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_json(value) -> str | None:
    return None if value is None else json.dumps(value)


def _from_json(value: str | None):
    return None if value is None else json.loads(value)
