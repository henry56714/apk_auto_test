"""Native crash dumper.

Writes raw logcat slice + structured incident JSON.
Best-effort: tries to pull a tombstone from `/data/tombstones/` (root-only on
user builds). On failure, records `fallback_reason` and continues.
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


def _latest_tombstone(adb: Adb) -> Optional[str]:
    """Return the path of the most recently modified tombstone, or None."""
    try:
        r = adb.shell(
            "ls -t /data/tombstones/ 2>/dev/null | head -1",
            check=False, timeout=5.0,
        )
    except AdbError:
        return None
    if r.returncode != 0:
        return None
    name = r.stdout.strip()
    if not name:
        return None
    return f"/data/tombstones/{name}"


def run(
    adb: Adb,
    event: StabilityEvent,
    incidents_dir: Path,
    *,
    pull_tombstone: bool = True,
) -> Dict:
    incidents_dir.mkdir(parents=True, exist_ok=True)
    base = base_name_for(event)
    slice_path = incidents_dir / f"{base}.txt"
    tombstone_path = incidents_dir / f"{base}.tombstone"
    json_path = incidents_dir / f"{base}.json"

    slice_name = write_raw_slice(slice_path, event)
    trace_name: Optional[str] = None
    fallback: Optional[str] = None

    if pull_tombstone:
        remote = _latest_tombstone(adb)
        if remote is None:
            fallback = "no accessible tombstone (likely non-root user build)"
        else:
            try:
                adb.pull(remote, str(tombstone_path), check=True, timeout=30.0)
                if tombstone_path.exists() and tombstone_path.stat().st_size > 0:
                    trace_name = tombstone_path.name
                else:
                    fallback = "tombstone pull produced empty file"
            except AdbError as e:
                fallback = f"tombstone pull failed: {e}"
    else:
        fallback = "tombstone pull disabled by config"

    dropbox_name = fetch_and_write_dropbox(adb, event, incidents_dir, base)
    incident = build_incident_dict(
        event,
        logcat_slice_file=slice_name,
        trace_file=trace_name,
        fallback_reason=fallback,
        dropbox_file=dropbox_name,
    )
    write_incident(json_path, incident)
    log.info("native_crash incident written: %s (trace=%s, dropbox=%s)",
             json_path.name, trace_name, dropbox_name)
    return incident
