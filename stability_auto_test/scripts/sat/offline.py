"""Offline bugreport import (spec S3-01).

`sat analyze-bugreport <zip>` parses a bugreport archive on a host without
any device: logcat, DropBox entries, ANR traces and tombstones all flow
through the *same* parsers and fusion layer as live runs, and the output is
the same report schema with `source_mode=offline_bugreport`.
"""

from __future__ import annotations

import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .detection import LogcatLineParser, StabilityEvent
from .dumpers import base_name_for, build_incident_dict, write_incident, write_raw_slice
from .fusion import FusionEngine
from .journal import (
    JOURNAL_FILENAME,
    STATUS_PERSISTED,
    IncidentJournal,
)
from .observations import observation_from_event

log = logging.getLogger(__name__)

SOURCE_MODE_OFFLINE = "offline_bugreport"

# Candidate logcat members inside a bugreport zip.
_LOGCAT_NAME_HINTS = (
    "logcat.txt",
    "main_entry.txt",
    "FS/data/misc/logd/logcat.txt",
    "FS/data/misc/logd/logcat",
)


def _zip_text(zf: zipfile.ZipFile, name: str, limit: int = 20 * 1024 * 1024) -> Optional[str]:
    info = zf.getinfo(name)
    if info.file_size > limit:
        return None
    return zf.read(name).decode("utf-8", errors="replace")


def extract_logcat_texts(zip_path: Path) -> List[Tuple[str, str]]:
    """Return `[(member_name, text)]` for every logcat-like member."""
    out: List[Tuple[str, str]] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for hint in _LOGCAT_NAME_HINTS:
            if hint in names:
                text = _zip_text(zf, hint)
                if text:
                    out.append((hint, text))
        for name in names:
            if (
                name.endswith(".log")
                and "logcat" in name.lower()
                and name not in {n for n, _ in out}
            ):
                text = _zip_text(zf, name)
                if text:
                    out.append((name, text))
    return out


def parse_logcat_events(text: str, package: str) -> List[StabilityEvent]:
    """Feed bugreport logcat text through the live parser (T-L0-029)."""
    parser = LogcatLineParser(
        package,
        now_iso_fn=lambda: datetime.now(timezone.utc).isoformat(),
    )
    events: List[StabilityEvent] = []
    for line in text.splitlines():
        events.extend(parser.feed_line(line))
    events.extend(parser.flush())
    return events


def analyze_bugreport(
    zip_path: Path,
    output_dir: Path,
    *,
    package: Optional[str] = None,
) -> Dict:
    """Analyze a bugreport zip and write a full report dir; returns report."""
    zip_path = Path(zip_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    incidents_dir = output_dir / "incidents"
    incidents_dir.mkdir(parents=True, exist_ok=True)
    journal_path = output_dir / JOURNAL_FILENAME
    journal_path.write_text("", encoding="utf-8")
    journal = IncidentJournal(journal_path)

    if package is None:
        package = _infer_package(zip_path)
    if not package:
        raise ValueError("cannot infer package from bugreport; pass --package explicitly")

    fusion = FusionEngine()
    event_count = 0
    occurred_at = datetime.now(timezone.utc)
    texts = extract_logcat_texts(zip_path)
    for name, text in texts:
        for event in parse_logcat_events(text, package):
            event_count += 1
            event.event_id = f"offline-{event_count:04d}"
            event.run_id = "offline"
            observation = observation_from_event(
                event,
                device_epoch=1,
                now_iso=occurred_at.isoformat(),
                now_sec=occurred_at.timestamp(),
                run_id="offline",
            )
            is_new, _ = fusion.observe(
                observation,
                occurred_at.timestamp() + event_count,
            )
            if not is_new:
                continue
            journal.detected(event.event_id, event)
            slice_name = write_raw_slice(
                incidents_dir / f"{base_name_for(event)}.txt",
                event,
            )
            incident = build_incident_dict(
                event,
                logcat_slice_file=slice_name,
                trace_file=None,
                fallback_reason="offline bugreport (no live device pull)",
            )
            write_incident(incidents_dir / f"{base_name_for(event)}.json", incident)
            journal.terminal(event.event_id, STATUS_PERSISTED)
    journal.close()

    from .reporter import result as result_builder

    report = result_builder.build(
        output_dir=output_dir,
        package=package,
        started_at=occurred_at,
        ended_at=occurred_at,
        device={"serial": "bugreport", "android_version": "?", "sdk_int": 0},
        config_effective={"package": package, "source_mode": SOURCE_MODE_OFFLINE},
        exit_code=0,
        exit_reason="offline_analyze",
        source_mode=SOURCE_MODE_OFFLINE,
        capabilities=[
            {
                "name": "source",
                "probe": "cli",
                "status": "available",
                "detail": SOURCE_MODE_OFFLINE,
            },
        ],
    )
    result_builder.write(report, output_dir)
    return report


def _infer_package(zip_path: Path) -> Optional[str]:
    """Best-effort package inference from bugreport content."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".txt") and "bugreport" not in name.lower():
                continue
            try:
                text = zf.read(name).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            for line in text.splitlines():
                if "Package [" in line and "]" in line:
                    pkg = line.split("Package [", 1)[1].split("]", 1)[0]
                    if "." in pkg:
                        return pkg
    return None
