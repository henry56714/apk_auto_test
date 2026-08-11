"""Aggregate report across device sub-reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from .atomic_io import atomic_write_json

AGGREGATE_FILENAME = "aggregate.json"
AGGREGATE_HTML_FILENAME = "aggregate.html"


def _load_report(path: Path) -> Dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def aggregate_reports(device_reports: List[Dict]) -> Dict:
    devices = []
    groups: Dict[str, Dict] = {}
    device_events: List[Dict] = []
    total_incidents = 0
    ok_devices = 0

    for report in device_reports:
        run = report.get("run", {}) or {}
        device = run.get("device", {}) or {}
        serial = device.get("serial", "?")
        has_report = bool(report and report.get("verdict"))
        status = "ok" if has_report else "missing"
        if has_report:
            ok_devices += 1
        devices.append(
            {
                "serial": serial,
                "status": status,
                "android_version": device.get("android_version", "?"),
                "sdk_int": device.get("sdk_int", 0),
                "verdict": report.get("verdict", "unknown"),
                "coverage_ratio": report.get("coverage_ratio", 0.0),
                "report_path": report.get("_report_path"),
                "device_events": report.get("device_events") or [],
            }
        )
        for event in report.get("device_events") or []:
            device_events.append({"device": serial, **event})
        total_incidents += len(report.get("incidents") or [])
        for group in report.get("issue_groups") or []:
            fp = group.get("fingerprint")
            if not fp:
                continue
            g = groups.setdefault(
                fp,
                {
                    "fingerprint": fp,
                    "type": group.get("type"),
                    "occurrence_count": 0,
                    "affected_devices": [],
                    "first_seen_at": group.get("first_seen_at"),
                    "last_seen_at": group.get("last_seen_at"),
                },
            )
            g["occurrence_count"] += group.get("occurrence_count", 0)
            if serial not in g["affected_devices"]:
                g["affected_devices"].append(serial)

    return {
        "devices": devices,
        "device_count": len(devices),
        "ok_device_count": ok_devices,
        "total_incidents": total_incidents,
        "aggregate_health": ("healthy" if ok_devices == len(devices) and devices else "degraded"),
        "device_events": device_events,
        "issue_groups": sorted(
            groups.values(),
            key=lambda g: g["last_seen_at"] or "",
            reverse=True,
        ),
    }


def render_aggregate_html(result: Dict) -> str:
    device_rows = "".join(
        f"<tr><td>{d['serial']}</td><td>{d['status']}</td>"
        f"<td>{d['verdict']}</td><td>{d['coverage_ratio']}</td></tr>"
        for d in result.get("devices", [])
    )
    group_rows = "".join(
        f"<tr><td>{g['fingerprint']}</td><td>{g['type']}</td>"
        f"<td>{g['occurrence_count']}</td>"
        f"<td>{','.join(g['affected_devices'])}</td></tr>"
        for g in result.get("issue_groups", [])
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>SAT aggregate</title></head><body>"
        f"<h1>Aggregate ({result.get('device_count')} devices)</h1>"
        f"<p>health: {result.get('aggregate_health')}</p>"
        "<h2>Devices</h2><table border='1'>"
        "<tr><th>serial</th><th>status</th><th>verdict</th><th>coverage</th></tr>"
        f"{device_rows}</table>"
        "<h2>Issue groups</h2><table border='1'>"
        "<tr><th>fingerprint</th><th>type</th><th>count</th><th>devices</th></tr>"
        f"{group_rows}</table></body></html>"
    )


def write_aggregate(result: Dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / AGGREGATE_FILENAME, result)
    (output_dir / AGGREGATE_HTML_FILENAME).write_text(
        render_aggregate_html(result),
        encoding="utf-8",
    )
    return output_dir / AGGREGATE_FILENAME
