"""Local copies of downloaded data, shared by the two clients.

Each client keeps one JSON file of entries, each with the time it was downloaded:

    {"exercises": {"fetched_at": "2026-10-13T19:11:00+00:00", "data": ...}, ...}

The rules, for every entry:

- A copy is used as long as it is younger than `max_age`.
- It is downloaded again when it is missing, too old, a refresh of everything was asked for, or
  the caller needs data from after the copy was made (`needed_as_of`).
- At most one download per entry per run; later calls get that copy.
- If a download fails and there is a copy, the copy is used and a warning given. Only with no
  copy at all does the error reach the caller.
- The file is written atomically, so an interrupted run never leaves half a file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cached:
    """One entry: the data as stored (plain JSON values) and when it was downloaded."""

    data: Any
    fetched_at: datetime


class DiskCache:
    def __init__(
        self,
        path: Path | None,
        *,
        max_age: timedelta,
        refresh_all: bool = False,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        """
        Args:
            path: The JSON file. None keeps copies in memory for this run only.
            max_age: Copies older than this are downloaded again.
            refresh_all: Download every entry once this run, whatever its age.
            warn: Called with a message when a download fails and a copy is used instead.
        """
        self._path = path
        self._max_age = max_age
        self._refresh_all = refresh_all
        self._warn = warn or log.warning
        self._entries: dict[str, Cached] = self._read()
        self._downloaded: set[str] = set()  # entries downloaded this run
        self._failed: dict[str, Exception] = {}  # entries whose download failed this run

    def get(
        self,
        key: str,
        download: Callable[[], Any],
        *,
        what: str,
        errors: tuple[type[Exception], ...],
        needed_as_of: datetime | None = None,
    ) -> Cached:
        """The entry `key`, downloaded with `download()` when the rules above say so.

        Args:
            key: Name of the entry in the file.
            download: Fetches the data; must return plain JSON values (dicts, lists, strings,
                numbers) and raise one of `errors` on failure.
            what: Name for messages, e.g. "Jefit exercise list".
            errors: The exceptions `download` raises when it fails.
            needed_as_of: The caller needs data from this time or later; an older copy is
                downloaded again (once per run).

        Returns:
            The copy now held. If a needed download failed, that is the old copy: compare its
            `fetched_at` with what was needed to tell.

        Raises:
            One of `errors`: The download failed and there is no copy at all.
        """
        cached = self._entries.get(key)
        if key in self._downloaded:
            return cached
        if key in self._failed:
            if cached is None:
                raise self._failed[key]
            return cached
        if cached is not None and not self._should_download(cached, needed_as_of):
            return cached

        try:
            data = download()
        except errors as exc:
            self._failed[key] = exc
            if cached is None:
                raise
            self._warn(f"{what}: download failed ({exc}); using the copy from {cached.fetched_at:%Y-%m-%d}")
            return cached
        log.debug("%s: downloaded", what)
        cached = Cached(data, datetime.now(timezone.utc))
        self._entries[key] = cached
        self._downloaded.add(key)
        self._write()
        return cached

    def _should_download(self, cached: Cached, needed_as_of: datetime | None) -> bool:
        if self._refresh_all:
            return True
        if datetime.now(timezone.utc) - cached.fetched_at > self._max_age:
            return True
        return needed_as_of is not None and needed_as_of > cached.fetched_at

    # -- the file ---------------------------------------------------------------------------

    def _read(self) -> dict[str, Cached]:
        if self._path is None:
            return {}
        try:
            raw = json.loads(self._path.read_text())
            return {
                key: Cached(entry["data"], datetime.fromisoformat(entry["fetched_at"]))
                for key, entry in raw.items()
            }
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError, AttributeError):
            return {}  # a damaged or old-format file is downloaded again

    def _write(self) -> None:
        if self._path is None:
            return
        body = {key: {"fetched_at": c.fetched_at.isoformat(timespec="seconds"), "data": c.data}
                for key, c in self._entries.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=self._path.name, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(body, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self._path)
