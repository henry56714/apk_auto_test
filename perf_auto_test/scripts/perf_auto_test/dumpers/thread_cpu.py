"""Thread-level CPU dumper triggered when a CPU threshold fires.

Writes 3 files into `incidents_dir`:
  - cpu_<ts>_<process>_pid<pid>.txt          — raw `top -H` output (human)
  - cpu_<ts>_<process>_pid<pid>.task_stat.txt — raw /proc task snapshot (offline)
  - cpu_<ts>_<process>_pid<pid>.json         — incident metadata + Top-N threads (AI/machine)

The .json is the single artifact a Step 6/7 reporter or AI needs to read to
explain what the alert was and which threads were most active.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from ..adb import Adb, AdbError
from ..alerting import AlertEvent
from ..discovery import Process
from ..utils import safe_filename, safe_ts, utc_now_iso

log = logging.getLogger(__name__)

DEFAULT_TOP_SAMPLES = 3
DEFAULT_TOP_DELAY_SEC = 1
DEFAULT_TOP_N_THREADS = 5


def _split_header_tokens(line: str) -> List[str]:
    """Split a `top -H` header into tokens, splitting toybox's `S[%CPU]`
    combined column into two separate tokens to match data-row layout.
    """
    out: List[str] = []
    for tok in line.split():
        if "[" in tok and "]" in tok and "%CPU" in tok.upper():
            lb = tok.find("[")
            rb = tok.rfind("]")
            prefix = tok[:lb]
            inner = tok[lb + 1: rb]
            if prefix:
                out.append(prefix)
            if inner:
                out.append(inner)
        else:
            out.append(tok)
    return out


def parse_top_h(text: str) -> List[Dict]:
    """Parse `top -H -b -n N` output into thread records.

    The header contains either `PID` (older Android / toybox) or `TID`
    (Android 15+ when `-H` shows threads). Toybox compacts state + %CPU into
    `S[%CPU]`; we split that back out so column indices match the data rows.

    Thread name column: prefer `THREAD` when it exists (Android 15+ layout
    `... THREAD PROCESS`, where the last column is the parent process); else
    fall back to `ARGS`/`NAME`; else fall back to the last token.

    Re-detect the header on each repeated snapshot. For each tid we keep the
    highest CPU% seen across snapshots.

    Returns a list sorted by `cpu_pct` descending: [{tid, name, cpu_pct}, ...]
    """
    threads: Dict[int, Dict] = {}
    pid_col: Optional[int] = None
    cpu_col: Optional[int] = None
    name_col: Optional[int] = None  # None means "use last column"

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        upper = line.upper()
        # Header has either PID or TID, plus %CPU.
        if ("PID" in upper or "TID" in upper) and "%CPU" in upper:
            cols = _split_header_tokens(line)
            upper_cols = [c.upper() for c in cols]
            pid_col = None
            for tag in ("PID", "TID"):
                if tag in upper_cols:
                    pid_col = upper_cols.index(tag)
                    break
            cpu_col = None
            for i, c in enumerate(upper_cols):
                if c == "%CPU":
                    cpu_col = i
                    break
            name_col = None
            # Pick a specific name column if present, else fall through to
            # last-column behaviour.
            for tag in ("THREAD", "ARGS", "NAME", "CMDLINE"):
                if tag in upper_cols:
                    name_col = upper_cols.index(tag)
                    break
            continue
        if pid_col is None or cpu_col is None:
            continue
        parts = line.split()
        if len(parts) <= max(pid_col, cpu_col):
            continue
        try:
            tid = int(parts[pid_col])
        except (ValueError, IndexError):
            continue
        try:
            cpu = float(parts[cpu_col])
        except (ValueError, IndexError):
            continue
        if name_col is not None and name_col < len(parts):
            name = parts[name_col]
        else:
            name = parts[-1] if parts else ""
        prev = threads.get(tid)
        if prev is None or cpu > prev["cpu_pct"]:
            threads[tid] = {"tid": tid, "name": name, "cpu_pct": cpu}

    return sorted(threads.values(), key=lambda t: t["cpu_pct"], reverse=True)


def run(
    adb: Adb,
    process: Process,
    alert: AlertEvent,
    incidents_dir: Path,
    *,
    top_samples: int = DEFAULT_TOP_SAMPLES,
    top_delay_sec: int = DEFAULT_TOP_DELAY_SEC,
    top_n: int = DEFAULT_TOP_N_THREADS,
) -> Optional[Dict]:
    """Capture thread CPU evidence; write three files; return metadata dict."""
    incidents_dir.mkdir(parents=True, exist_ok=True)
    triggered_iso = utc_now_iso()
    ts_safe = safe_ts(triggered_iso)
    proc_safe = safe_filename(process.name)
    # NOTE: don't use Path.with_suffix here — the base name contains dots
    # (timestamp and package), so with_suffix replaces the wrong segment.
    base_name = f"cpu_{ts_safe}_{proc_safe}_pid{process.pid}"
    raw_path = incidents_dir / f"{base_name}.txt"
    task_stat_path = incidents_dir / f"{base_name}.task_stat.txt"
    parsed_path = incidents_dir / f"{base_name}.json"

    top_cmd = f"top -H -p {process.pid} -b -n {top_samples} -d {top_delay_sec}"
    top_timeout = top_samples * (top_delay_sec + 2) + 5
    try:
        r = adb.shell(top_cmd, check=False, timeout=top_timeout)
    except AdbError as e:
        log.error("thread_cpu top failed for pid=%d: %s", process.pid, e)
        return None

    raw_path.write_text(r.stdout or "", encoding="utf-8")

    # Best-effort: /proc/<pid>/task/*/stat snapshot for offline reconstruction.
    try:
        rs = adb.shell(
            f"for f in /proc/{process.pid}/task/*/stat; do cat $f; done",
            check=False, timeout=5,
        )
        if rs.returncode == 0 and rs.stdout:
            task_stat_path.write_text(rs.stdout, encoding="utf-8")
    except AdbError:
        pass

    threads = parse_top_h(r.stdout or "")

    incident = {
        "type": "cpu_threshold",
        "process": process.name,
        "pid": process.pid,
        "triggered_at": triggered_iso,
        "threshold": {
            "metric": alert.metric,
            "value": alert.threshold_value,
            "sustain_sec": alert.sustain_sec,
            "cooldown_sec": alert.cooldown_sec,
        },
        "observed": {
            "value_at_trigger": alert.value_at_trigger,
            "duration_above_sec": alert.duration_above_sec,
            "peak": alert.peak,
        },
        "evidence": {
            "raw_file": raw_path.name,
            "task_stat_file": task_stat_path.name if task_stat_path.exists() else None,
            "top_threads": threads[:top_n],
            "top_threads_count": len(threads),
        },
    }
    parsed_path.write_text(json.dumps(incident, indent=2), encoding="utf-8")
    log.info("cpu dump written: %s (top thread=%s)",
             parsed_path.name,
             threads[0]["name"] if threads else "?")
    return incident
