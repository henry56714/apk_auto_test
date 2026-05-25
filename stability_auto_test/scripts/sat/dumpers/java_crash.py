"""Java crash dumper. Writes raw logcat slice + structured incident JSON."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from ..adb import Adb
from ..detection import StabilityEvent
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
) -> Dict:
    incidents_dir.mkdir(parents=True, exist_ok=True)
    base = base_name_for(event)
    slice_path = incidents_dir / f"{base}.txt"
    json_path = incidents_dir / f"{base}.json"

    slice_name = write_raw_slice(slice_path, event)
    dropbox_name = fetch_and_write_dropbox(adb, event, incidents_dir, base)
    incident = build_incident_dict(
        event,
        logcat_slice_file=slice_name,
        trace_file=None,
        fallback_reason=None,
        dropbox_file=dropbox_name,
    )
    write_incident(json_path, incident)
    log.info("java_crash incident written: %s (dropbox=%s)", json_path.name, dropbox_name)
    return incident
