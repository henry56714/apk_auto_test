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
from ..evidence.trace_matcher import match_trace, verify_local_trace
from . import (
    base_name_for,
    build_incident_dict,
    fetch_and_write_dropbox,
    write_incident,
    write_raw_slice,
)

log = logging.getLogger(__name__)


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
    match_info: Dict = {
        "evidence_match_confidence": "none",
        "evidence_match_reasons": [],
        "trace_verified": False,
    }

    if pull_tombstone:
        match = match_trace(adb, event, "/data/tombstones/")
        match_info["evidence_match_confidence"] = match.confidence
        match_info["evidence_match_reasons"] = list(match.reasons)
        if not match.bound:
            fallback = "no_confident_match"
        else:
            remote = match.candidate.path
            try:
                adb.pull(remote, str(tombstone_path), check=True, timeout=30.0)
                if tombstone_path.exists() and tombstone_path.stat().st_size > 0:
                    trace_name = tombstone_path.name
                    ok, reason = verify_local_trace(tombstone_path, event)
                    match_info["trace_verified"] = ok
                    if not ok:
                        match_info["trace_verify_reason"] = reason
                        match_info["evidence_match_confidence"] = "low"
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
        extra_evidence=match_info,
    )
    write_incident(json_path, incident)
    log.info("native_crash incident written: %s (trace=%s, dropbox=%s)",
             json_path.name, trace_name, dropbox_name)
    return incident
