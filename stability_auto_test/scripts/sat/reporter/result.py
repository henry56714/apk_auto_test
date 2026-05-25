"""Build the canonical structured report (`report.json`).

Reads from the run's output directory (events.csv + lifecycle.csv + incidents/
*.json files) and returns the result dict that all other report formats
render from. This is the single source of truth for AI / CI consumers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..detection import ALL_EVENT_TYPES

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
REPORT_FILENAME = "report.json"


def _iso(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat(timespec="milliseconds").replace("T", " ").replace("+00:00", "")


def _read_csvs(paths: List[Path]) -> pd.DataFrame:
    dfs = []
    for p in paths:
        try:
            df = pd.read_csv(p, comment="#")
        except (pd.errors.EmptyDataError, FileNotFoundError):
            continue
        except pd.errors.ParserError as e:
            log.warning("could not parse %s: %s", p, e)
            continue
        if df.empty:
            continue
        dfs.append(df)
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def _alive_intervals(
    proc_life: pd.DataFrame,
    run_start: datetime,
    run_end: datetime,
) -> Tuple[Optional[datetime], Optional[datetime], List[Tuple[datetime, datetime]]]:
    if proc_life.empty:
        return None, None, []
    df = proc_life.copy()
    df["_ts"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("_ts")

    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    intervals: List[Tuple[datetime, datetime]] = []
    alive_start: Optional[datetime] = None

    for _, row in df.iterrows():
        ts = row["_ts"].to_pydatetime()
        event = row["event"]
        if event in ("new", "restart"):
            if first_seen is None:
                first_seen = ts
            if alive_start is None:
                alive_start = ts
        elif event == "gone":
            if alive_start is not None:
                intervals.append((alive_start, ts))
                last_seen = ts
                alive_start = None

    if alive_start is not None:
        intervals.append((alive_start, run_end))
        last_seen = run_end if last_seen is None else max(last_seen, run_end)

    return first_seen, last_seen, intervals


def _event_counts_for(incidents: List[Dict], process_name: str) -> Dict[str, int]:
    counts = {t: 0 for t in ALL_EVENT_TYPES}
    for i in incidents:
        if i.get("process") != process_name:
            continue
        t = i.get("type")
        if t in counts:
            counts[t] += 1
    return counts


def _load_incidents(incidents_dir: Path) -> List[Dict]:
    if not incidents_dir.exists():
        return []
    out: List[Dict] = []
    for json_file in sorted(incidents_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("skipping unreadable incident %s: %s", json_file, e)
            continue
        if not isinstance(data, dict):
            continue
        if "type" not in data or "process" not in data:
            continue
        data["_source_file"] = json_file.name
        out.append(data)
    out.sort(key=lambda d: d.get("triggered_at", ""))
    for i, incident in enumerate(out, start=1):
        incident["id"] = f"incident-{i:03d}"
    return out


def _build_process(
    name: str,
    life_df: pd.DataFrame,
    run_start: datetime,
    run_end: datetime,
    incidents: List[Dict],
    sample_failures: Dict[str, int],
) -> Dict:
    proc_life = (life_df[life_df["process_name"] == name]
                 if not life_df.empty and "process_name" in life_df.columns
                 else pd.DataFrame())

    first_seen, last_seen, intervals = _alive_intervals(proc_life, run_start, run_end)
    alive_sec = sum((end - start).total_seconds() for start, end in intervals)
    total_sec = max(1e-9, (run_end - run_start).total_seconds())
    uptime_ratio = min(1.0, alive_sec / total_sec) if alive_sec > 0 else 0.0
    restart_count = int((proc_life["event"] == "restart").sum()) if not proc_life.empty else 0

    return {
        "name": name,
        "first_seen_at": _iso(first_seen),
        "last_seen_at": _iso(last_seen),
        "uptime_ratio": round(uptime_ratio, 4),
        "restart_count": restart_count,
        "events": _event_counts_for(incidents, name),
        "sample_failures": dict(sample_failures),
    }


def _build_lifecycle_events(life_df: pd.DataFrame) -> List[Dict]:
    if life_df.empty:
        return []
    out: List[Dict] = []
    for _, row in life_df.iterrows():
        out.append({
            "timestamp": row["timestamp"],
            "process": row["process_name"],
            "event": row["event"],
            "old_pid": int(row["old_pid"]) if pd.notna(row.get("old_pid")) else 0,
            "new_pid": int(row["new_pid"]) if pd.notna(row.get("new_pid")) else 0,
            "gap_sec": float(row["gap_sec"]) if pd.notna(row.get("gap_sec")) else 0.0,
        })
    return out


def build(
    *,
    output_dir: Path,
    package: str,
    started_at: datetime,
    ended_at: datetime,
    device: Dict[str, Any],
    config_effective: Dict[str, Any],
    exit_code: int,
    exit_reason: str,
    bookmarks: Optional[List[Dict]] = None,
    sample_failures: Optional[Dict[str, int]] = None,
    duration_sec: Optional[float] = None,
) -> Dict:
    """Build the canonical report dict.

    `duration_sec`: explicit active runtime (e.g. from `time.monotonic()`
    captured in `api.StabilityTest`). If omitted, falls back to wall-clock
    `(ended_at - started_at)` — which over-counts when the OS suspends the
    process (system sleep). Callers that care about budget fidelity should
    always pass this.
    """
    output_dir = Path(output_dir)
    incidents_dir = output_dir / "incidents"
    sample_failures = sample_failures or {}

    events_files = sorted(output_dir.glob("events_*.csv"))
    life_files = sorted(output_dir.glob("lifecycle_*.csv"))
    logcat_files = sorted(output_dir.glob("logcat_*.log"))

    life_df = _read_csvs(life_files)
    incidents = _load_incidents(incidents_dir)

    process_names = set()
    if not life_df.empty and "process_name" in life_df.columns:
        process_names.update(life_df["process_name"].dropna().unique())
    for inc in incidents:
        if inc.get("process"):
            process_names.add(inc["process"])

    processes = [
        _build_process(name, life_df, started_at, ended_at,
                       incidents, sample_failures)
        for name in sorted(process_names)
    ]

    if duration_sec is None:
        duration_sec = max(0.0, (ended_at - started_at).total_seconds())
    else:
        duration_sec = max(0.0, float(duration_sec))

    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "started_at": _iso(started_at),
            "ended_at": _iso(ended_at),
            "duration_sec": round(duration_sec, 3),
            "exit_code": int(exit_code),
            "exit_reason": exit_reason,
            "device": device,
            "package": package,
            "config_effective": config_effective,
        },
        "processes": processes,
        "incidents": incidents,
        "lifecycle_events": _build_lifecycle_events(life_df),
        "bookmarks": list(bookmarks or []),
        "data_files": {
            "events": [p.name for p in events_files],
            "lifecycle": [p.name for p in life_files],
            "logcat": [p.name for p in logcat_files],
        },
    }


def write(result: Dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / REPORT_FILENAME
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
