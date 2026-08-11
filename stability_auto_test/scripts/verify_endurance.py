"""Verify an 8-hour L3 endurance run output (run after the run completes).

Usage:
    python verify_endurance.py /tmp/sat-endurance-8h-v2
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema


def _norm_rss(v):
    # macOS ru_maxrss is bytes; Linux is KB.
    return int(v) // 1024 if int(v) > 10_000_000 else int(v)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    report_path = root / "report.json"
    errors = []
    if not report_path.exists():
        errors.append("report.json missing")
    else:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"report.json corrupt: {e}")
            report = {}
        else:
            schema_path = (
                Path(__file__).parent / "schemas" / "report.schema.json"
            )
            try:
                jsonschema.validate(report, json.loads(schema_path.read_text()))
            except jsonschema.ValidationError as e:
                errors.append(f"report schema invalid: {e.message}")

            duration = report.get("run", {}).get("duration_sec", 0) or 0
            if duration < 7.5 * 3600:
                errors.append(f"duration too short: {duration:.1f}s")

            sr = report.get("self_resource", {}) or {}
            samples = sr.get("samples", []) or []
            if samples:
                rss = [_norm_rss(s.get("rss_kb", 0)) for s in samples]
                growth = (rss[-1] - rss[0]) / max(1, rss[0]) * 100
                if growth > 20:
                    errors.append(f"RSS growth {growth:.1f}% > 20%")
                if sr.get("threads_peak", 0) > 64:
                    errors.append("thread count unbounded")
                if sr.get("fds_peak", 0) > 512:
                    errors.append("fd count unbounded")
                # Normalize mislabeled Darwin byte values in this run's summary.
                peak = _norm_rss(sr.get("rss_peak_kb", 0) or 0)
                if peak > 4 * 1024 * 1024:
                    errors.append(f"RSS peak implausible: {peak} KB")
            else:
                errors.append("self_resource samples missing")

            journal = root / "incident_journal.jsonl"
            if journal.exists():
                from sat.journal import read_journal
                _, warnings = read_journal(journal)
                if warnings:
                    errors.append(f"journal recovery warnings: {warnings}")

            logcat = sorted(root.glob("logcat_*.log"))
            if not logcat:
                errors.append("no logcat files")

            device_events = report.get("device_events", [])
            if not any(e.get("event_type") in ("offline", "reboot")
                       for e in device_events):
                errors.append("no device offline/reboot injection records")

    if errors:
        print("ENDURANCE VERIFY: FAIL")
        for e in errors:
            print("  -", e)
        return 1
    print(
        "ENDURANCE VERIFY: PASS "
        f"(duration={report.get('run', {}).get('duration_sec', 0):.0f}s, "
        f"verdict={report.get('verdict')}, "
        f"coverage={report.get('coverage_ratio')}, "
        f"device_events={len(report.get('device_events', []))}, "
        f"self_samples={len((report.get('self_resource', {}) or {}).get('samples', []))})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
