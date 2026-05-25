"""ANR dumper.

Writes raw logcat slice + structured incident JSON.
Best-effort: tries to pull the latest ANR trace from `/data/anr/` (root-only
on user builds). On failure, records `fallback_reason` and continues.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from ..adb import Adb, AdbError
from ..detection import StabilityEvent
from . import (
    base_name_for,
    build_incident_dict,
    fetch_and_write_dropbox,
    write_incident,
    write_raw_slice,
)

log = logging.getLogger(__name__)


def _latest_anr_trace(adb: Adb) -> Optional[str]:
    try:
        r = adb.shell(
            "ls -t /data/anr/ 2>/dev/null | head -1",
            check=False, timeout=5.0,
        )
    except AdbError:
        return None
    if r.returncode != 0:
        return None
    name = r.stdout.strip()
    if not name:
        return None
    return f"/data/anr/{name}"


def run(
    adb: Adb,
    event: StabilityEvent,
    incidents_dir: Path,
    *,
    pull_anr_trace: bool = True,
) -> Dict:
    incidents_dir.mkdir(parents=True, exist_ok=True)
    base = base_name_for(event)
    slice_path = incidents_dir / f"{base}.txt"
    trace_path = incidents_dir / f"{base}.trace"
    json_path = incidents_dir / f"{base}.json"

    slice_name = write_raw_slice(slice_path, event)
    trace_name: Optional[str] = None
    fallback: Optional[str] = None

    if pull_anr_trace:
        remote = _latest_anr_trace(adb)
        if remote is None:
            fallback = "no accessible ANR trace (likely non-root user build)"
        else:
            try:
                adb.pull(remote, str(trace_path), check=True, timeout=30.0)
                if trace_path.exists() and trace_path.stat().st_size > 0:
                    trace_name = trace_path.name
                else:
                    fallback = "ANR trace pull produced empty file"
            except AdbError as e:
                fallback = f"ANR trace pull failed: {e}"
    else:
        fallback = "ANR trace pull disabled by config"

    dropbox_name = fetch_and_write_dropbox(adb, event, incidents_dir, base)
    incident = build_incident_dict(
        event,
        logcat_slice_file=slice_name,
        trace_file=trace_name,
        fallback_reason=fallback,
        dropbox_file=dropbox_name,
    )
    write_incident(json_path, incident)
    log.info("anr incident written: %s (trace=%s, dropbox=%s)",
             json_path.name, trace_name, dropbox_name)
    return incident
