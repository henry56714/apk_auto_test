"""Per-event evidence dumpers.

Each dumper takes (`adb`, `event`, `incidents_dir`) and writes:
- `<base>.txt`  — raw logcat slice (the parser's accumulated lines for the
  crash block). Always written when raw_lines is non-empty.
- `<base>.json` — structured incident metadata (the AI/reporter source).
- Optional: `<base>.tombstone` / `<base>.trace` pulled from the device when
  accessible (root or app-private dirs); recorded with a `fallback_reason`
  on the incident when not.

The dispatcher in `pool.py` picks the dumper based on `event.event_type`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from ..detection import StabilityEvent
from ..utils import safe_filename, safe_ts

log = logging.getLogger(__name__)


def base_name_for(event: StabilityEvent) -> str:
    ts_safe = safe_ts(event.triggered_at)
    proc_safe = safe_filename(event.process or "unknown")
    return f"{event.event_type}_{ts_safe}_{proc_safe}_pid{event.pid}"


def write_raw_slice(path: Path, event: StabilityEvent) -> Optional[str]:
    if not event.raw_lines:
        return None
    path.write_text("\n".join(event.raw_lines) + "\n", encoding="utf-8")
    return path.name


def fetch_and_write_dropbox(
    adb,
    event: StabilityEvent,
    incidents_dir: Path,
    base: str,
) -> Optional[str]:
    """Pull matching dropbox entry and write to <base>_dropbox.txt. Returns filename or None."""
    from ..collectors.dropbox import DropboxFetcher
    body = DropboxFetcher(adb).fetch(event.event_type, event.process, event.device_ts)
    if not body:
        return None
    path = incidents_dir / f"{base}_dropbox.txt"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path.name


def write_incident(path: Path, incident: Dict) -> None:
    path.write_text(json.dumps(incident, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def build_incident_dict(
    event: StabilityEvent,
    *,
    logcat_slice_file: Optional[str],
    trace_file: Optional[str],
    fallback_reason: Optional[str],
    dropbox_file: Optional[str] = None,
    extra_evidence: Optional[Dict] = None,
) -> Dict:
    evidence: Dict = {
        "logcat_slice_file": logcat_slice_file,
        "trace_file": trace_file,
        "dropbox_file": dropbox_file,
        "exception_class": event.exception_class,
        "signal": event.signal,
        "fault_addr": event.fault_addr,
        "reason": event.reason,
        "top_frames": list(event.top_frames),
        "source": event.source,
        "dedup_count": 1,
        "fallback_reason": fallback_reason,
        "device_ts": event.device_ts,
    }
    if extra_evidence:
        evidence.update(extra_evidence)
    return {
        "type": event.event_type,
        "process": event.process,
        "pid": event.pid,
        "triggered_at": event.triggered_at,
        "severity": event.severity,
        "summary": event.summary,
        "evidence": evidence,
    }
