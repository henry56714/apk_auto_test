"""Local report index + trend aggregation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from .atomic_io import atomic_write_json

INDEX_FILENAME = ".sat-index.json"


def _load_report(path: Path) -> Dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def scan_reports(root: Path) -> Dict:
    root = Path(root)
    entries = []
    errors = []
    for report_path in sorted(root.rglob("report.json")):
        report = _load_report(report_path)
        if not report:
            errors.append(str(report_path))
            continue
        run = report.get("run", {}) or {}
        device = run.get("device", {}) or {}
        groups = report.get("issue_groups") or []
        entries.append({
            "path": str(report_path),
            "run_id": run.get("run_id"),
            "started_at": run.get("started_at"),
            "package": run.get("package"),
            "device": device.get("serial"),
            "android_version": device.get("android_version"),
            "app_version_name": run.get("app_version_name"),
            "app_version_code": run.get("app_version_code"),
            "build_id": run.get("build_id"),
            "git_sha": run.get("git_sha"),
            "verdict": report.get("verdict"),
            "coverage_ratio": report.get("coverage_ratio"),
            "incident_count": len(report.get("incidents") or []),
            "issue_groups": [
                {
                    "fingerprint": g.get("fingerprint"),
                    "type": g.get("type"),
                    "occurrence_count": g.get("occurrence_count", 0),
                }
                for g in groups
            ],
        })
    return {
        "root": str(root),
        "run_count": len(entries),
        "errors": errors,
        "runs": entries,
    }


def write_index(root: Path, data: Dict) -> Path:
    path = Path(root) / INDEX_FILENAME
    return atomic_write_json(path, data)


def load_index(root: Path) -> Dict:
    path = Path(root) / INDEX_FILENAME
    if not path.exists():
        return {}
    return _load_report(path)


def trend(data: Dict, *, by: str = "fingerprint") -> Dict:
    rows: Dict[str, Dict] = {}
    seen_fps: set = set()
    new_by_run: List[Dict] = []
    runs = sorted(
        data.get("runs", []),
        key=lambda r: r.get("started_at") or "",
    )
    for run in runs:
        device = run.get("device") or "?"
        version = run.get("android_version") or "?"
        app_version = run.get("app_version_name") or "?"
        run_new_fps = []
        for group in run.get("issue_groups", []):
            key = group.get(by) or group.get("fingerprint") or "unknown"
            row = rows.setdefault(key, {
                "key": key,
                "occurrence_count": 0,
                "affected_devices": set(),
                "android_versions": set(),
                "app_versions": set(),
                "coverage_samples": [],
                "first_seen_at": None,
                "last_seen_at": None,
            })
            row["occurrence_count"] += group.get("occurrence_count", 0)
            row["affected_devices"].add(device)
            row["android_versions"].add(version)
            if app_version != "?":
                row["app_versions"].add(app_version)
            if run.get("coverage_ratio") is not None:
                row["coverage_samples"].append(float(run["coverage_ratio"]))
            fp = group.get("fingerprint")
            if fp and fp not in seen_fps:
                run_new_fps.append(fp)
            if group.get("first_seen_at"):
                row["first_seen_at"] = min(
                    row["first_seen_at"] or group["first_seen_at"],
                    group["first_seen_at"],
                )
            if group.get("last_seen_at"):
                row["last_seen_at"] = max(
                    row["last_seen_at"] or group["last_seen_at"],
                    group["last_seen_at"],
                )
        for fp in run_new_fps:
            seen_fps.add(fp)
        if run_new_fps:
            new_by_run.append({
                "started_at": run.get("started_at"),
                "new_fingerprint_count": len(run_new_fps),
                "fingerprints": run_new_fps,
            })
    out = []
    for row in rows.values():
        row["affected_devices"] = sorted(row["affected_devices"])
        row["android_versions"] = sorted(row["android_versions"])
        row["app_versions"] = sorted(row["app_versions"])
        row["avg_coverage"] = (
            round(sum(row["coverage_samples"]) / len(row["coverage_samples"]), 3)
            if row["coverage_samples"] else None
        )
        out.append(row)
    out.sort(key=lambda r: r["occurrence_count"], reverse=True)
    return {"by": by, "rows": out, "new_regressions_by_run": new_by_run}


def render_trend_html(result: Dict) -> str:
    rows = "".join(
        f"<tr><td>{r['key']}</td><td>{r['occurrence_count']}</td>"
        f"<td>{','.join(r['affected_devices'])}</td>"
        f"<td>{','.join(r['android_versions'])}</td>"
        f"<td>{','.join(r['app_versions'])}</td>"
        f"<td>{r.get('avg_coverage')}</td></tr>"
        for r in result.get("rows", [])
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>SAT trend</title></head><body>"
        f"<h1>Trend by {result.get('by')}</h1>"
        "<table border='1'><tr><th>key</th><th>count</th>"
        "<th>devices</th><th>android</th><th>app versions</th>"
        "<th>avg coverage</th></tr>"
        f"{rows}</table></body></html>"
    )


def write_trend(result: Dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "trend.json", result)
    (output_dir / "trend.html").write_text(
        render_trend_html(result), encoding="utf-8",
    )
    return output_dir / "trend.json"
