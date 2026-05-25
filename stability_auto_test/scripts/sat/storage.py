"""Streaming writers with hourly rotation and schema header.

Two writer classes share the same rotation strategy:
- CsvStreamWriter — append rows to `<prefix>_YYYY-MM-DD_HH.csv` with a leading
  schema-tag comment line so the reporter can verify what it's reading.
- LogStreamWriter — append free-form text lines to `<prefix>_YYYY-MM-DD_HH.log`.
  Used for raw logcat capture (one logcat line per row) so a 24h run does not
  produce a single multi-GB file.

Thread-safe (collector threads write concurrently). Auto-rotate on UTC hour
boundary. Periodic flush keeps data durable if the process is killed.
"""

from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

EVENTS_COLUMNS: Sequence[str] = (
    "timestamp", "event_type", "process_name", "pid", "severity", "summary",
)
EVENTS_SCHEMA_TAG = "stability_auto_test/events/v1"

LIFECYCLE_COLUMNS: Sequence[str] = (
    "timestamp", "process_name", "event", "old_pid", "new_pid", "gap_sec",
)
LIFECYCLE_SCHEMA_TAG = "stability_auto_test/lifecycle/v1"

LOGCAT_PREFIX = "logcat"
LOGCAT_SCHEMA_TAG = "stability_auto_test/logcat/v1"


def _current_hour_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d_%H")


class _RotatingWriterBase:
    """Shared hourly-rotation plumbing for CSV / log writers."""

    def __init__(
        self,
        output_dir: Path,
        name_prefix: str,
        suffix: str,
        *,
        flush_every: int = 50,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.output_dir = Path(output_dir)
        self.name_prefix = name_prefix
        self.suffix = suffix
        self.flush_every = flush_every
        self._clock = clock

        self._lock = threading.Lock()
        self._fh = None
        self._current_key: Optional[str] = None
        self._rows_since_flush = 0

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, key: str) -> Path:
        return self.output_dir / f"{self.name_prefix}_{key}.{self.suffix}"

    def _open_for_key(self, key: str) -> None:
        path = self._path_for_key(key)
        is_new = not path.exists() or path.stat().st_size == 0
        self._fh = open(path, "a", encoding="utf-8", newline=("" if self.suffix == "csv" else None))
        if is_new:
            self._on_new_file()
        self._current_key = key
        self._rows_since_flush = 0

    def _on_new_file(self) -> None:
        """Override to write headers/schema markers when a fresh file is created."""

    def _ensure_open(self, now: datetime) -> None:
        key = _current_hour_key(now)
        if self._current_key != key:
            self._close_locked()
            self._open_for_key(key)

    def _close_locked(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._current_key = None
        self._rows_since_flush = 0

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                self._rows_since_flush = 0

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def files(self) -> List[Path]:
        return sorted(self.output_dir.glob(f"{self.name_prefix}_*.{self.suffix}"))

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class CsvStreamWriter(_RotatingWriterBase):
    def __init__(
        self,
        output_dir: Path,
        name_prefix: str,
        columns: Sequence[str],
        schema_tag: str,
        *,
        flush_every: int = 50,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        super().__init__(output_dir, name_prefix, "csv",
                         flush_every=flush_every, clock=clock)
        self.columns: List[str] = list(columns)
        self.schema_tag = schema_tag
        self._writer: Optional[csv.DictWriter] = None

    def _on_new_file(self) -> None:
        assert self._fh is not None
        self._fh.write(f"# {self.schema_tag}\n")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.columns,
                                      extrasaction="ignore")
        self._writer.writeheader()
        self._fh.flush()

    def _open_for_key(self, key: str) -> None:
        super()._open_for_key(key)
        if self._writer is None:
            # Re-attached to an existing file: rebuild a writer without
            # rewriting the header.
            self._writer = csv.DictWriter(self._fh, fieldnames=self.columns,
                                          extrasaction="ignore")

    def _close_locked(self) -> None:
        super()._close_locked()
        self._writer = None

    def write_row(self, row: Dict) -> None:
        now = self._clock()
        with self._lock:
            self._ensure_open(now)
            assert self._writer is not None
            self._writer.writerow(row)
            self._rows_since_flush += 1
            if self._rows_since_flush >= self.flush_every:
                self._fh.flush()
                self._rows_since_flush = 0


class LogStreamWriter(_RotatingWriterBase):
    """Free-form line writer for raw logcat capture.

    Each `write_line` appends one `\\n`-terminated line to the current hour
    file. A fresh file starts with `# <schema_tag>\\n` so consumers can
    distinguish a stability_auto_test logcat dump from any other text file.
    """

    def __init__(
        self,
        output_dir: Path,
        *,
        name_prefix: str = LOGCAT_PREFIX,
        schema_tag: str = LOGCAT_SCHEMA_TAG,
        flush_every: int = 200,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        super().__init__(output_dir, name_prefix, "log",
                         flush_every=flush_every, clock=clock)
        self.schema_tag = schema_tag

    def _on_new_file(self) -> None:
        assert self._fh is not None
        self._fh.write(f"# {self.schema_tag}\n")
        self._fh.flush()

    def write_line(self, line: str) -> None:
        now = self._clock()
        if not line.endswith("\n"):
            line = line + "\n"
        with self._lock:
            self._ensure_open(now)
            assert self._fh is not None
            self._fh.write(line)
            self._rows_since_flush += 1
            if self._rows_since_flush >= self.flush_every:
                self._fh.flush()
                self._rows_since_flush = 0
