"""Build the canonical structured report (`report.json`).

Reads from the run's output directory (CSVs + incidents JSON files) and
returns the result dict that all other report formats render from. This is
the single source of truth that AI agents / CI consumers should rely on.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

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
    """Concatenate CSVs that have a `# schema_tag` comment header line."""
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


def _stats_dict(series: pd.Series) -> Optional[Dict[str, float]]:
    if series is None or series.empty:
        return None
    return {
        "mean": round(float(series.mean()), 2),
        "p50": round(float(series.quantile(0.5)), 2),
        "p90": round(float(series.quantile(0.9)), 2),
        "p95": round(float(series.quantile(0.95)), 2),
        "max": round(float(series.max()), 2),
        "samples": int(series.count()),
    }


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


def _incidents_count_for(incidents: List[Dict], process_name: str) -> Dict[str, int]:
    cpu = sum(1 for i in incidents
              if i.get("process") == process_name and i.get("type") == "cpu_threshold")
    mem = sum(1 for i in incidents
              if i.get("process") == process_name and i.get("type") == "mem_threshold")
    return {"cpu": cpu, "mem": mem}


def _load_incidents(incidents_dir: Path) -> List[Dict]:
    if not incidents_dir.exists():
        return []
    out: List[Dict] = []
    for json_file in sorted(incidents_dir.glob("*.json")):
        # Skip parsed-meminfo siblings; they have a different shape and aren't
        # standalone incidents. They're referenced from the incident itself.
        if json_file.name.endswith(".meminfo.json"):
            continue
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
    cpu_df: pd.DataFrame,
    mem_df: pd.DataFrame,
    life_df: pd.DataFrame,
    run_start: datetime,
    run_end: datetime,
    incidents: List[Dict],
    sample_failures: Dict[str, Dict[str, int]],
) -> Dict:
    proc_cpu = (cpu_df[cpu_df["process_name"] == name]
                if not cpu_df.empty and "process_name" in cpu_df.columns
                else pd.DataFrame())
    proc_mem = (mem_df[mem_df["process_name"] == name]
                if not mem_df.empty and "process_name" in mem_df.columns
                else pd.DataFrame())
    proc_life = (life_df[life_df["process_name"] == name]
                 if not life_df.empty and "process_name" in life_df.columns
                 else pd.DataFrame())

    cpu_stats = (_stats_dict(proc_cpu["cpu_pct"])
                 if "cpu_pct" in proc_cpu.columns else None)
    mem_stats = (_stats_dict(proc_mem["pss_mb"])
                 if "pss_mb" in proc_mem.columns else None)

    first_seen, last_seen, intervals = _alive_intervals(proc_life, run_start, run_end)
    alive_sec = sum((end - start).total_seconds() for start, end in intervals)
    total_sec = max(1e-9, (run_end - run_start).total_seconds())
    uptime_ratio = min(1.0, alive_sec / total_sec) if alive_sec > 0 else 0.0
    restart_count = int((proc_life["event"] == "restart").sum()) if not proc_life.empty else 0

    fails = sample_failures.get(name, {})
    return {
        "name": name,
        "first_seen_at": _iso(first_seen),
        "last_seen_at": _iso(last_seen),
        "uptime_ratio": round(uptime_ratio, 4),
        "restart_count": restart_count,
        "stats": {
            "cpu_pct": cpu_stats,
            "mem_pss_mb": mem_stats,
        },
        "alerts": _incidents_count_for(incidents, name),
        "sample_failures": {
            "cpu": int(fails.get("cpu", 0)),
            "mem": int(fails.get("mem", 0)),
        },
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
    sample_failures: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict:
    """Construct the canonical report dict from the run output directory."""
    output_dir = Path(output_dir)
    incidents_dir = output_dir / "incidents"
    sample_failures = sample_failures or {}

    cpu_files = sorted(output_dir.glob("cpu_*.csv"))
    mem_files = sorted(output_dir.glob("mem_*.csv"))
    life_files = sorted(output_dir.glob("lifecycle_*.csv"))

    cpu_df = _read_csvs(cpu_files)
    mem_df = _read_csvs(mem_files)
    life_df = _read_csvs(life_files)

    incidents = _load_incidents(incidents_dir)

    process_names = set()
    for df in (cpu_df, mem_df, life_df):
        if not df.empty and "process_name" in df.columns:
            process_names.update(df["process_name"].dropna().unique())
    for inc in incidents:
        if inc.get("process"):
            process_names.add(inc["process"])
    # Processes that only ever produced failures (no CSV rows, no lifecycle,
    # no incidents) should still appear in the report.
    process_names.update(sample_failures.keys())

    processes = [
        _build_process(name, cpu_df, mem_df, life_df, started_at, ended_at,
                       incidents, sample_failures)
        for name in sorted(process_names)
    ]

    duration_sec = max(0.0, (ended_at - started_at).total_seconds())

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
            "cpu": [p.name for p in cpu_files],
            "mem": [p.name for p in mem_files],
            "lifecycle": [p.name for p in life_files],
        },
    }


def write(result: Dict, output_dir: Path) -> Path:
    """Write the report to `<output_dir>/report.json`. Returns the path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / REPORT_FILENAME
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return path
