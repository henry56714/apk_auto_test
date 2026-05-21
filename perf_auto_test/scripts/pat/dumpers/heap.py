"""Heap dumper for memory threshold breaches.

Two layers:
1. Always capture `dumpsys meminfo <pid> -d` as both .txt (human) and .json
   (parsed top categories). This works on any APK on any device.
2. Try `am dumpheap <pid> /data/local/tmp/<…>.hprof` on top. This requires the
   app to be debuggable OR the device to be rooted. On failure (return code
   nonzero, file not produced, or zero size) we fall back gracefully and
   record the reason in the incident metadata.

Files in `incidents_dir` for one incident:
  - heap_<ts>_<process>_pid<pid>.meminfo.txt
  - heap_<ts>_<process>_pid<pid>.meminfo.json
  - heap_<ts>_<process>_pid<pid>.hprof          (if ok)
  - heap_<ts>_<process>_pid<pid>.json           (incident metadata, AI source)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from ..adb import Adb, AdbError
from ..alerting import AlertEvent
from ..collectors.memory import parse_meminfo
from ..discovery import Process
from ..utils import safe_filename, safe_ts, utc_now_iso

log = logging.getLogger(__name__)

HEAP_STABLE_MAX_WAIT_SEC = 30.0
HEAP_STABLE_POLL_SEC = 1.0
HEAP_PULL_TIMEOUT_SEC = 120.0


def _capture_meminfo_detailed(
    adb: Adb,
    pid: int,
    txt_path: Path,
    json_path: Path,
) -> Optional[Dict]:
    """Run `dumpsys meminfo <pid> -d` and write both raw text and parsed JSON.
    Returns the parsed dict or None on failure."""
    try:
        r = adb.shell(f"dumpsys meminfo {pid} -d", check=False, timeout=15.0)
    except AdbError as e:
        log.warning("dumpsys meminfo -d failed for pid=%d: %s", pid, e)
        return None
    if r.returncode != 0 or not r.stdout:
        log.warning("dumpsys meminfo -d rc=%d for pid=%d", r.returncode, pid)
        return None
    txt_path.write_text(r.stdout, encoding="utf-8")

    sample = parse_meminfo(r.stdout, pid=pid)
    if sample is None:
        log.warning("could not parse meminfo for pid=%d", pid)
        return None

    parsed = {
        "captured_at": utc_now_iso(),
        "pid": pid,
        "total_pss_mb": sample.total_pss_mb,
        "top_categories": [
            {"name": "Java Heap", "pss_mb": sample.java_heap_pss_mb},
            {"name": "Native Heap", "pss_mb": sample.native_heap_pss_mb},
            {"name": "Graphics", "pss_mb": sample.graphics_pss_mb},
            {"name": "Code", "pss_mb": sample.code_pss_mb},
            {"name": "Stack", "pss_mb": sample.stack_pss_mb},
        ],
    }
    # Sort descending by pss_mb so the reporter can show "what dominates".
    parsed["top_categories"].sort(key=lambda c: c["pss_mb"], reverse=True)
    json_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    return parsed


def _wait_for_stable_file(
    adb: Adb,
    remote_path: str,
    *,
    max_wait_sec: float = HEAP_STABLE_MAX_WAIT_SEC,
    poll_sec: float = HEAP_STABLE_POLL_SEC,
) -> int:
    """Poll the remote file's size until it's the same two ticks in a row.
    Returns the final size, or 0 on timeout / file absent."""
    deadline = time.monotonic() + max_wait_sec
    last_size = -1
    while time.monotonic() < deadline:
        try:
            r = adb.shell(
                f"wc -c < {remote_path} 2>/dev/null", check=False, timeout=3.0,
            )
        except AdbError:
            r = None
        if r and r.returncode == 0:
            try:
                size = int(r.stdout.strip())
            except ValueError:
                size = -1
            if size > 0 and size == last_size:
                return size
            last_size = size
        time.sleep(poll_sec)
    return last_size if last_size > 0 else 0


def run(
    adb: Adb,
    process: Process,
    alert: AlertEvent,
    incidents_dir: Path,
    *,
    enable_heap: bool = True,
) -> Optional[Dict]:
    """Capture memory evidence; write up to four files; return metadata dict."""
    incidents_dir.mkdir(parents=True, exist_ok=True)
    triggered_iso = utc_now_iso()
    ts_safe = safe_ts(triggered_iso)
    proc_safe = safe_filename(process.name)
    base_name = f"heap_{ts_safe}_{proc_safe}_pid{process.pid}"

    meminfo_txt = incidents_dir / f"{base_name}.meminfo.txt"
    meminfo_json = incidents_dir / f"{base_name}.meminfo.json"
    incident_json = incidents_dir / f"{base_name}.json"
    hprof_local = incidents_dir / f"{base_name}.hprof"

    meminfo_parsed = _capture_meminfo_detailed(adb, process.pid, meminfo_txt, meminfo_json)
    top_categories = meminfo_parsed["top_categories"] if meminfo_parsed else []

    heap_status = "skipped"
    fallback_reason: Optional[str] = None
    hprof_size_bytes = 0

    if not enable_heap:
        fallback_reason = "heap dumps disabled by config"
    else:
        device_path = f"/data/local/tmp/{base_name}.hprof"
        # Pre-clean any leftover from an aborted previous attempt.
        try:
            adb.shell(f"rm -f {device_path}", check=False, timeout=3.0)
        except AdbError:
            pass

        rc = None
        stderr = ""
        try:
            r = adb.shell(
                f"am dumpheap {process.pid} {device_path}",
                check=False, timeout=20.0,
            )
            rc = r.returncode
            stderr = (r.stderr or "").strip()
        except AdbError as e:
            fallback_reason = f"am dumpheap exception: {e}"

        if fallback_reason is None and rc != 0:
            fallback_reason = (
                f"am dumpheap rc={rc}"
                + (f": {stderr[:100]}" if stderr else "")
            )

        if fallback_reason is None:
            size = _wait_for_stable_file(adb, device_path)
            if size <= 0:
                fallback_reason = "hprof file not produced within 30s (likely non-debuggable app)"
            else:
                try:
                    adb.pull(device_path, str(hprof_local),
                             check=True, timeout=HEAP_PULL_TIMEOUT_SEC)
                    if hprof_local.exists():
                        hprof_size_bytes = hprof_local.stat().st_size
                        heap_status = "ok"
                except AdbError as e:
                    fallback_reason = f"adb pull failed: {e}"

            try:
                adb.shell(f"rm -f {device_path}", check=False, timeout=3.0)
            except AdbError:
                pass

        if heap_status != "ok" and fallback_reason is not None:
            heap_status = "fallback"

    incident = {
        "type": "mem_threshold",
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
            "heap_status": heap_status,
            "fallback_reason": fallback_reason,
            "hprof_file": hprof_local.name if heap_status == "ok" else None,
            "hprof_size_bytes": hprof_size_bytes,
            "meminfo_file": meminfo_txt.name if meminfo_txt.exists() else None,
            "meminfo_parsed_file": meminfo_json.name if meminfo_json.exists() else None,
            "top_categories": top_categories,
        },
    }
    incident_json.write_text(json.dumps(incident, indent=2), encoding="utf-8")
    log.info("heap dump written: %s (status=%s)", incident_json.name, heap_status)
    return incident
