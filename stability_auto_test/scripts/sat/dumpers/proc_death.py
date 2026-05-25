"""Process-death dumper.

Process death is usually detected by the process watcher (pid disappears at
the next reconcile) — there's no logcat block to capture by the time we hear
about it. The `am_proc_died` / `am_kill` events buffer entries do give a
reason; if the event carries `raw_lines` we save them as the slice too.

Result: a single incident JSON with the metadata the watcher / events-buffer
captured. No remote file pull.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from ..adb import Adb
from ..detection import StabilityEvent
from . import base_name_for, build_incident_dict, write_incident, write_raw_slice

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
    incident = build_incident_dict(
        event,
        logcat_slice_file=slice_name,
        trace_file=None,
        fallback_reason=None,
    )
    write_incident(json_path, incident)
    log.info("process_death incident written: %s", json_path.name)
    return incident
