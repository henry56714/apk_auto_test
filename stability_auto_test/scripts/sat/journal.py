"""Append-only event journal (`incident_journal.jsonl`).

The journal is the authoritative event-fact source for the final report:
every event that passes dedup is recorded as `detected` at dispatch time, and
a terminal record (`persisted`, `failed`, `timed_out`, `dropped_by_cap`) is
appended when that state is reached. The report builder derives pipeline
counts from the journal, and incident JSON files are treated as evidence
details rather than the sole proof that an event happened.

Reading tolerates a truncated final line (e.g. the process was killed while
appending); that line is ignored and a recovery warning is returned so the
report can mark `collection_health=degraded`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .utils import utc_now_iso

JOURNAL_FILENAME = "incident_journal.jsonl"
JOURNAL_VERSION = 1

STATUS_DETECTED = "detected"
STATUS_PERSISTED = "persisted"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_DROPPED_BY_CAP = "dropped_by_cap"
STATUS_DROPPED_BY_BACKPRESSURE = "dropped_by_backpressure"

TERMINAL_STATUSES = (
    STATUS_PERSISTED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    STATUS_DROPPED_BY_CAP,
    STATUS_DROPPED_BY_BACKPRESSURE,
)


class IncidentJournal:
    """Thread-safe append-only JSONL writer."""

    def __init__(
        self,
        path: Path,
        *,
        now_iso_fn: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now_iso = now_iso_fn
        self._lock = threading.Lock()
        self._fh = open(self.path, "a", encoding="utf-8")
        self._writes = 0

    def detected(self, event_id: str, event) -> None:
        self.append({
            "journal_version": JOURNAL_VERSION,
            "ts": self._now_iso(),
            "event_id": event_id,
            "status": STATUS_DETECTED,
            "run_id": getattr(event, "run_id", None),
            "event_type": event.event_type,
            "process": event.process,
            "pid": event.pid,
            "triggered_at": event.triggered_at,
            "severity": event.severity,
            "summary": event.summary,
            "source": event.source,
            "device_ts": event.device_ts,
        })

    def terminal(
        self,
        event_id: str,
        status: str,
        *,
        error_type: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid journal terminal status: {status!r}")
        record = {
            "journal_version": JOURNAL_VERSION,
            "ts": self._now_iso(),
            "event_id": event_id,
            "status": status,
        }
        if error_type is not None:
            record["error_type"] = error_type
        if error is not None:
            record["error"] = error
        self.append(record)

    def append(self, record: Dict) -> None:
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._writes += 1
            # Every record is flushed: the journal is the crash-recovery fact
            # source, so an unexpected process death must not lose records
            # that were already appended.
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
            finally:
                self._fh.close()

    def __enter__(self) -> "IncidentJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_journal(path: Path) -> Tuple[List[Dict], List[str]]:
    """Read journal records; tolerate a truncated final line.

    Returns ``(records, recovery_warnings)``. A partial last line is ignored
    and reported as a warning; malformed interior lines are also warned about
    but do not abort recovery of the earlier complete events.
    """
    path = Path(path)
    records: List[Dict] = []
    warnings: List[str] = []
    if not path.exists():
        return records, warnings

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        warnings.append(f"journal unreadable: {e}")
        return records, warnings

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                warnings.append(
                    f"truncated journal line {i + 1} ignored (process likely "
                    "exited mid-write)"
                )
            else:
                warnings.append(f"invalid journal line {i + 1} skipped")
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, warnings
