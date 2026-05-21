"""CSV streaming writer with hourly rotation and schema header.

Design:
- Append-only files named `<prefix>_YYYY-MM-DD_HH.csv` in `output_dir`.
- First line of each file is a schema marker comment (e.g. `# perf_auto_test/cpu/v1`)
  so the reporter and AI consumers can verify what they're reading.
- Thread-safe (collector threads write concurrently).
- Auto-rotate on UTC hour boundary so single files don't grow unbounded over 24h.
- Periodic flush to keep data durable if the process is killed.
"""

from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

# Column orders are part of the on-disk schema. Bump the schema_tag if you change them.
CPU_COLUMNS: Sequence[str] = ("timestamp", "process_name", "pid", "cpu_pct")
CPU_SCHEMA_TAG = "perf_auto_test/cpu/v1"

MEM_COLUMNS: Sequence[str] = (
    "timestamp", "process_name", "pid", "pss_mb",
    "java_heap_mb", "native_heap_mb", "graphics_mb", "code_mb", "stack_mb",
)
MEM_SCHEMA_TAG = "perf_auto_test/mem/v1"

LIFECYCLE_COLUMNS: Sequence[str] = (
    "timestamp", "process_name", "event", "old_pid", "new_pid", "gap_sec",
)
LIFECYCLE_SCHEMA_TAG = "perf_auto_test/lifecycle/v1"


def _current_hour_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d_%H")


class CsvStreamWriter:
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
        self.output_dir = Path(output_dir)
        self.name_prefix = name_prefix
        self.columns: List[str] = list(columns)
        self.schema_tag = schema_tag
        self.flush_every = flush_every
        self._clock = clock

        self._lock = threading.Lock()
        self._fh = None
        self._writer: Optional[csv.DictWriter] = None
        self._current_key: Optional[str] = None
        self._rows_since_flush = 0

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, key: str) -> Path:
        return self.output_dir / f"{self.name_prefix}_{key}.csv"

    def _open_for_key(self, key: str) -> None:
        path = self._path_for_key(key)
        is_new = not path.exists() or path.stat().st_size == 0
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.columns,
                                      extrasaction="ignore")
        if is_new:
            self._fh.write(f"# {self.schema_tag}\n")
            self._writer.writeheader()
            self._fh.flush()
        self._current_key = key
        self._rows_since_flush = 0

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
        self._writer = None
        self._current_key = None
        self._rows_since_flush = 0

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

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                self._rows_since_flush = 0

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def files(self) -> List[Path]:
        """Return all files written by this writer so far (sorted by name)."""
        return sorted(self.output_dir.glob(f"{self.name_prefix}_*.csv"))

    def __enter__(self) -> "CsvStreamWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
