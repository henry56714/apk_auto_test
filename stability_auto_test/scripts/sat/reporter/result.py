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

from ..analyzers.exit_correlation import correlate_exit_info
from ..analyzers.fingerprint import group_incidents
from ..atomic_io import atomic_write_json
from ..collectors.resource_risk import correlate_resource_risk
from ..detection import ALL_EVENT_TYPES
from ..health import compute_verdict
from ..journal import (
    JOURNAL_FILENAME,
    STATUS_DETECTED,
    STATUS_DROPPED_BY_BACKPRESSURE,
    STATUS_DROPPED_BY_CAP,
    STATUS_FAILED,
    STATUS_PERSISTED,
    STATUS_TIMED_OUT,
    read_journal,
)
from ..policy import evaluate_policy, policy_from_dict

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.15"
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
    return out


def _journal_counts(records: List[Dict]) -> Dict[str, int]:
    """Fold journal records by event_id to produce unique terminal-state counts.

    Each event_id may appear multiple times (one ``detected`` record + one or
    more terminal records).  The LAST terminal status wins per event_id so the
    pipeline identity holds:

        detected = persisted + failed + timed_out + dropped_by_cap + dropped_by_backpressure

    If an event_id appears with two different terminal statuses (e.g. timed_out
    followed by persisted) a warning is logged and only the LAST status is
    counted — the report must never double-count a single event.
    """
    detected: set = set()
    # Per-event unique terminal status (last-write-wins).
    terminal_by_event: Dict[str, str] = {}
    for rec in records:
        event_id = rec.get("event_id")
        status = rec.get("status")
        if not event_id:
            continue
        if status == STATUS_DETECTED:
            detected.add(event_id)
            continue
        if status in (
            STATUS_PERSISTED,
            STATUS_FAILED,
            STATUS_TIMED_OUT,
            STATUS_DROPPED_BY_CAP,
            STATUS_DROPPED_BY_BACKPRESSURE,
        ):
            prev = terminal_by_event.get(event_id)
            if prev is not None and prev != status:
                log.warning(
                    "journal event %s has multiple terminal states: %s → %s (using last)",
                    event_id,
                    prev,
                    status,
                )
            terminal_by_event[event_id] = status

    persisted = failed = timed_out = dropped = dropped_bp = 0
    for status in terminal_by_event.values():
        if status == STATUS_PERSISTED:
            persisted += 1
        elif status == STATUS_FAILED:
            failed += 1
        elif status == STATUS_TIMED_OUT:
            timed_out += 1
        elif status == STATUS_DROPPED_BY_CAP:
            dropped += 1
        elif status == STATUS_DROPPED_BY_BACKPRESSURE:
            dropped_bp += 1

    return {
        "detected_count": len(detected),
        "persisted_count": persisted,
        "failed_count": failed,
        "timed_out_count": timed_out,
        "dropped_by_cap_count": dropped,
        "dropped_by_backpressure_count": dropped_bp,
    }


def _placeholder_incident(
    rec: Dict,
    evidence_status: str,
    details: Optional[Dict] = None,
) -> Dict:
    src = details or rec
    evidence = {
        "evidence_status": evidence_status,
        "source": src.get("source", "logcat"),
        "reason": "incident evidence not persisted",
    }
    if rec.get("error_type"):
        evidence["error_type"] = rec["error_type"]
    if rec.get("error"):
        evidence["error"] = rec["error"]
    return {
        "event_id": src.get("event_id") or rec.get("event_id"),
        "type": src.get("event_type", "unknown"),
        "process": src.get("process", ""),
        "pid": src.get("pid", 0),
        "triggered_at": src.get("triggered_at", ""),
        "severity": src.get("severity", "error"),
        "summary": src.get("summary", ""),
        "evidence": evidence,
    }


def _build_incidents(
    incidents_dir: Path,
    journal_records: List[Dict],
) -> Tuple[List[Dict], List[str]]:
    """Merge incident JSON evidence with journal event facts.

    Journal records are authoritative for *whether* an event happened; incident
    JSON files supply evidence details. Failed/timed-out/dropped events get a
    placeholder incident so a broken dumper can never make a run look clean.

    Each event_id is folded to a single unique terminal status (last-wins).
    Returns ``(incidents, warnings)``.
    """
    loaded = _load_incidents(incidents_dir)
    by_id: Dict[str, Dict] = {}
    without_id: List[Dict] = []
    for inc in loaded:
        eid = inc.get("event_id")
        if eid:
            by_id[eid] = inc
        else:
            without_id.append(inc)

    incidents: List[Dict] = list(without_id)
    warnings: List[str] = []
    # Fold to unique terminal status per event_id (last-wins).
    terminal_by_event: Dict[str, str] = {}
    terminal_rec_by_event: Dict[str, Dict] = {}
    detected_ids: set = set()
    detected_by_id: Dict[str, Dict] = {}
    for rec in journal_records:
        event_id = rec.get("event_id")
        status = rec.get("status")
        if not event_id:
            continue
        if status == STATUS_DETECTED:
            detected_ids.add(event_id)
            detected_by_id[event_id] = rec
            continue
        terminal_by_event[event_id] = status
        terminal_rec_by_event[event_id] = rec

    for event_id, status in terminal_by_event.items():
        if status == STATUS_DROPPED_BY_CAP:
            continue  # counted in the pipeline, but not an incident

        incident = by_id.pop(event_id, None)
        if incident is None:
            # Map terminal status to a truthful evidence_status.
            if status == STATUS_PERSISTED:
                evidence_status = STATUS_PERSISTED
            elif status == STATUS_FAILED:
                evidence_status = STATUS_FAILED
            elif status == STATUS_DROPPED_BY_BACKPRESSURE:
                evidence_status = STATUS_DROPPED_BY_BACKPRESSURE
            else:
                evidence_status = STATUS_TIMED_OUT

            rec = terminal_rec_by_event[event_id]
            incident = _placeholder_incident(
                rec,
                evidence_status,
                details=detected_by_id.get(event_id),
            )
            if status not in (STATUS_PERSISTED, STATUS_DROPPED_BY_BACKPRESSURE):
                warnings.append(
                    f"incident evidence missing for {event_id} (journal status={status})"
                )
        else:
            evidence = incident.setdefault("evidence", {})
            evidence["evidence_status"] = status
        incident["event_id"] = event_id
        incidents.append(incident)

    # Leftover incident JSONs with no journal record: keep for backward
    # compatibility and mark them persisted (they did make it to disk).
    for incident in by_id.values():
        incident.setdefault("evidence", {}).setdefault("evidence_status", STATUS_PERSISTED)
        incidents.append(incident)

    # Journal entries that never reached a terminal state (e.g. process was
    # killed mid-run): surface them as failed so they cannot be silently lost.
    orphaned = detected_ids - set(terminal_by_event)
    if orphaned:
        warnings.append(f"{len(orphaned)} journal event(s) ended without a terminal status")
        for rec in journal_records:
            eid = rec.get("event_id")
            if eid in orphaned:
                placeholder = _placeholder_incident(
                    rec,
                    STATUS_FAILED,
                    details=detected_by_id.get(eid),
                )
                placeholder["evidence"]["reason"] = (
                    "journal ended before a terminal status was recorded"
                )
                incidents.append(placeholder)
                orphaned.discard(eid)

    incidents.sort(key=lambda d: d.get("triggered_at", ""))
    for i, incident in enumerate(incidents, start=1):
        incident["id"] = f"incident-{i:03d}"
    return incidents, warnings


def _build_process(
    name: str,
    life_df: pd.DataFrame,
    run_start: datetime,
    run_end: datetime,
    incidents: List[Dict],
    sample_failures: Dict[str, int],
) -> Dict:
    proc_life = (
        life_df[life_df["process_name"] == name]
        if not life_df.empty and "process_name" in life_df.columns
        else pd.DataFrame()
    )

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
        out.append(
            {
                "timestamp": row["timestamp"],
                "process": row["process_name"],
                "event": row["event"],
                "old_pid": int(row["old_pid"]) if pd.notna(row.get("old_pid")) else 0,
                "new_pid": int(row["new_pid"]) if pd.notna(row.get("new_pid")) else 0,
                "gap_sec": float(row["gap_sec"]) if pd.notna(row.get("gap_sec")) else 0.0,
            }
        )
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
    event_pipeline: Optional[Dict[str, int]] = None,
    run_id: Optional[str] = None,
    app_metadata: Optional[Dict] = None,
    exit_info: Optional[List[Dict]] = None,
    device_events: Optional[List[Dict]] = None,
    resource_risk: Optional[List[Dict]] = None,
    self_resource: Optional[Dict] = None,
    collector_health: Optional[Dict] = None,
    collectors: Optional[Dict[str, Dict]] = None,
    policy_config: Optional[Dict] = None,
    ci_mode: bool = False,
    recovered: bool = False,
    recovered_at: Optional[str] = None,
    duration_sec: Optional[float] = None,
    phase_timings: Optional[Dict] = None,
    quota_audit: Optional[List[Dict]] = None,
    capabilities: Optional[List[Dict]] = None,
    source_mode: str = "live",
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
    event_pipeline = {
        "detected_count": 0,
        "persisted_count": 0,
        "failed_count": 0,
        "timed_out_count": 0,
        "dropped_by_cap_count": 0,
        "dropped_by_backpressure_count": 0,
        **(event_pipeline or {}),
    }

    events_files = sorted(output_dir.glob("events_*.csv"))
    life_files = sorted(output_dir.glob("lifecycle_*.csv"))
    logcat_files = sorted(output_dir.glob("logcat_*.log"))
    journal_path = output_dir / JOURNAL_FILENAME
    journal_records, recovery_warnings = read_journal(journal_path)

    life_df = _read_csvs(life_files)
    incidents, incident_warnings = _build_incidents(incidents_dir, journal_records)
    # DropBox evidence counts as a supporting source for cross-source
    # traceability (spec 4.2: every source that saw the failure is listed).
    for inc in incidents:
        evidence = inc.setdefault("evidence", {})
        sources = list(evidence.get("supporting_sources") or [])
        if evidence.get("dropbox_file") and "dropbox" not in sources:
            sources.append("dropbox")
        if sources:
            evidence["supporting_sources"] = sources
    exit_records = correlate_exit_info(incidents, list(exit_info or []))
    correlate_resource_risk(incidents, list(resource_risk or []))
    recovery_warnings = recovery_warnings + incident_warnings
    if journal_records:
        event_pipeline = _journal_counts(journal_records)
    collector_health = collector_health or {
        "health": "healthy",
        "coverage_ratio": 1.0,
        "reasons": [],
    }
    collection_health = collector_health.get("health", "healthy")
    if recovery_warnings and collection_health == "healthy":
        collection_health = "degraded"
    health_reasons = list(collector_health.get("reasons") or [])
    if recovery_warnings and "journal recovery warnings" not in health_reasons:
        health_reasons.append(f"journal recovery warnings: {len(recovery_warnings)}")
    coverage_ratio = round(float(collector_health.get("coverage_ratio", 1.0)), 4)
    verdict_result = compute_verdict(collection_health, incidents=incidents)
    verdict = verdict_result.verdict

    process_names = set()
    if not life_df.empty and "process_name" in life_df.columns:
        process_names.update(life_df["process_name"].dropna().unique())
    for inc in incidents:
        if inc.get("process"):
            process_names.add(inc["process"])

    processes = [
        _build_process(name, life_df, started_at, ended_at, incidents, sample_failures)
        for name in sorted(process_names)
    ]

    policy = policy_from_dict(policy_config or {})
    policy_result = evaluate_policy(
        incidents,
        processes,
        coverage_ratio,
        policy,
    )
    policy_result["enabled"] = bool(ci_mode)

    if duration_sec is None:
        duration_sec = max(0.0, (ended_at - started_at).total_seconds())
    else:
        duration_sec = max(0.0, float(duration_sec))

    return {
        "schema_version": SCHEMA_VERSION,
        "source_mode": source_mode,
        "run": {
            "started_at": _iso(started_at),
            "ended_at": _iso(ended_at),
            "duration_sec": round(duration_sec, 3),
            "exit_code": int(exit_code),
            "exit_reason": exit_reason,
            "run_id": run_id,
            "app_version_name": (app_metadata or {}).get("app_version_name", ""),
            "app_version_code": (app_metadata or {}).get("app_version_code", ""),
            "build_id": (app_metadata or {}).get("build_id", ""),
            "git_sha": (app_metadata or {}).get("git_sha", ""),
            "recovered": bool(recovered),
            "recovered_at": recovered_at,
            "phase_timings": phase_timings or {},
            "device": device,
            "package": package,
            "config_effective": config_effective,
        },
        "processes": processes,
        "incidents": incidents,
        "issue_groups": group_incidents(incidents),
        "exit_info": exit_records,
        "device_events": list(device_events or []),
        "resource_risk": list(resource_risk or []),
        "self_resource": self_resource or {},
        "event_pipeline": event_pipeline,
        "collection_health": collection_health,
        "coverage_ratio": coverage_ratio,
        "verdict": verdict,
        "verdict_reason": list(verdict_result.reasons),
        "verdict_confidence": verdict_result.confidence,
        "expected_exit_count": verdict_result.expected_count,
        "collectors": collectors or {},
        "capabilities": list(capabilities or []),
        "disk_audit": list(quota_audit or []),
        "policy": policy_result,
        "recovery_warnings": recovery_warnings,
        "lifecycle_events": _build_lifecycle_events(life_df),
        "bookmarks": list(bookmarks or []),
        "data_files": {
            "events": [p.name for p in events_files],
            "lifecycle": [p.name for p in life_files],
            "logcat": [p.name for p in logcat_files],
            "journal": [journal_path.name] if journal_path.exists() else [],
        },
    }


def write(result: Dict, output_dir: Path) -> Path:
    path = Path(output_dir) / REPORT_FILENAME
    return atomic_write_json(path, result)
