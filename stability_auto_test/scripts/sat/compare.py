"""Baseline comparison for stability regressions.

Matches reports by incident fingerprint. `new_regressions` and `worsened`
drive the optional CI gate; everything else is informational.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from .atomic_io import atomic_write_json
from .reporter.junit import write_junit

COMPARE_FILENAME = "compare.json"
COMPARE_HTML_FILENAME = "compare.html"


class CompareError(RuntimeError):
    pass


def load_report(path: Path) -> Dict:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise CompareError(f"unparseable report {path}: {e}") from e
    if not isinstance(data, dict) or "issue_groups" not in data:
        raise CompareError(f"report {path} has no issue_groups; not a SAT report")
    return data


def _check_compatible(a: Dict, b: Dict) -> None:
    va = str(a.get("schema_version", "?"))
    vb = str(b.get("schema_version", "?"))
    if va != vb:
        raise CompareError(
            f"incompatible schema versions: baseline={va} current={vb}"
        )


def _group_map(report: Dict) -> Dict[str, Dict]:
    return {g["fingerprint"]: g for g in (report.get("issue_groups") or [])}


def _affected_devices(group: Dict) -> List[str]:
    return list(group.get("affected_devices") or [])


def _severity(group: Dict) -> int:
    sev = group.get("type", "")
    return {"java_crash": 3, "native_crash": 3, "anr": 2,
            "process_death": 1}.get(sev, 0)


def compare_reports(baseline: Dict, current: Dict) -> Dict:
    _check_compatible(baseline, current)
    base = _group_map(baseline)
    cur = _group_map(current)
    fps = sorted(set(base) | set(cur))

    new_regressions: List[Dict] = []
    worsened: List[Dict] = []
    unchanged: List[Dict] = []
    improved: List[Dict] = []
    fixed: List[Dict] = []

    for fp in fps:
        b = base.get(fp)
        c = cur.get(fp)
        entry = {
            "fingerprint": fp,
            "type": (c or b).get("type"),
            "baseline_count": (b or {}).get("occurrence_count", 0),
            "current_count": (c or {}).get("occurrence_count", 0),
            "affected_devices": _affected_devices(c or b),
            "severity": _severity(c or b),
            "first_seen_at": (c or {}).get("first_seen_at"),
            "last_seen_at": (c or {}).get("last_seen_at"),
        }
        if b is None:
            new_regressions.append(entry)
        elif c is None:
            fixed.append(entry)
        elif c["occurrence_count"] > b["occurrence_count"]:
            worsened.append(entry)
        elif c["occurrence_count"] < b["occurrence_count"]:
            improved.append(entry)
        else:
            unchanged.append(entry)

    return {
        "baseline_schema_version": baseline.get("schema_version"),
        "current_schema_version": current.get("schema_version"),
        "new_regressions": new_regressions,
        "worsened": worsened,
        "unchanged": unchanged,
        "improved": improved,
        "fixed": fixed,
    }


def render_compare_html(result: Dict) -> str:
    def rows(key: str) -> str:
        items = result.get(key) or []
        return "".join(
            f"<tr><td>{i.get('fingerprint', '')}</td>"
            f"<td>{i.get('type', '')}</td>"
            f"<td>{i.get('baseline_count', 0)} → {i.get('current_count', 0)}</td>"
            f"<td>{','.join(i.get('affected_devices') or [])}</td></tr>"
            for i in items
        )

    sections = ""
    for key in ("new_regressions", "worsened", "unchanged", "improved", "fixed"):
        sections += (
            f"<h3>{key} ({len(result.get(key) or [])})</h3>"
            f"<table border='1'><tr><th>fingerprint</th><th>type</th>"
            f"<th>counts</th><th>devices</th></tr>{rows(key)}</table>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>SAT compare</title></head><body>"
        "<h1>stability_auto_test compare</h1>"
        f"<p>baseline {result.get('baseline_schema_version')} → "
        f"current {result.get('current_schema_version')}</p>{sections}"
        "</body></html>"
    )


def write_compare(result: Dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / COMPARE_FILENAME, result)
    (output_dir / COMPARE_HTML_FILENAME).write_text(
        render_compare_html(result), encoding="utf-8",
    )
    return output_dir / COMPARE_FILENAME


def write_compare_junit(result: Dict, path: Path) -> Path:
    """JUnit for compare: new/worsened are failures, others pass."""
    groups = []
    for status in ("new_regressions", "worsened"):
        for item in result.get(status) or []:
            groups.append({
                "fingerprint": item.get("fingerprint"),
                "type": item.get("type"),
            })
    for status in ("unchanged", "improved", "fixed"):
        for item in result.get(status) or []:
            groups.append({
                "fingerprint": item.get("fingerprint"),
                "type": item.get("type"),
            })
    policy_passed = not (result.get("new_regressions") or result.get("worsened"))
    report = {
        "verdict": "unstable" if not policy_passed else "stable",
        "policy": {
            "enabled": True,
            "passed": policy_passed,
            "rules": [{"rule": "compare", "pass": policy_passed}],
        },
        "issue_groups": groups,
        "incidents": [],
        "coverage_ratio": 1.0,
    }
    return write_junit(report, path)
