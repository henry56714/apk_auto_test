"""Render a human/AI-friendly Markdown summary from a built result.

Structure is **fixed** — same sections in the same order regardless of run
content — so AI agents can slice it deterministically. Empty sections render
as "(none)" rather than disappearing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

SUMMARY_FILENAME = "summary.md"


def _opt(v) -> str:
    return "—" if v is None else str(v)


def _opt_pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.2f}"


def _stats_row(label: str, stats: Optional[dict]) -> str:
    if stats is None:
        return f"- **{label}**: (no samples)"
    return (
        f"- **{label}**: mean={stats['mean']:.2f} / p50={stats['p50']:.2f} / "
        f"p90={stats['p90']:.2f} / p95={stats['p95']:.2f} / max={stats['max']:.2f} "
        f"(n={stats['samples']})"
    )


def _table(headers: List[str], rows: Iterable[List[str]]) -> str:
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    rendered_rows = list(rows)
    if not rendered_rows:
        lines.append("| " + " | ".join("(none)" for _ in headers) + " |")
    else:
        for row in rendered_rows:
            lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render(result: dict) -> str:
    run = result["run"]
    dev = run.get("device", {})

    md: List[str] = []

    md.append(f"# Perf Report — `{run['package']}` on `{dev.get('serial', '?')}`")
    md.append("")

    # Run section
    md.append("## Run")
    md.append("")
    md.append(f"- **Schema version**: {result['schema_version']}")
    md.append(f"- **Started**: {run['started_at']}")
    md.append(f"- **Ended**:   {run['ended_at']}")
    md.append(f"- **Duration**: {run['duration_sec']:.0f}s")
    md.append(f"- **Exit code**: {run['exit_code']} ({run.get('exit_reason', '?')})")
    md.append(f"- **Device**: serial=`{dev.get('serial', '?')}` "
              f"Android={dev.get('android_version', '?')} "
              f"SDK={dev.get('sdk_int', '?')} cores={dev.get('cpu_cores', '?')}")
    md.append("")

    # Discovered processes
    md.append("## Discovered Processes")
    md.append("")
    proc_rows = []
    for p in result["processes"]:
        proc_rows.append([
            f"`{p['name']}`",
            _opt(p.get("first_seen_at")),
            _opt(p.get("last_seen_at")),
            f"{p['uptime_ratio']:.3f}",
            str(p["restart_count"]),
        ])
    md.append(_table(
        ["process", "first_seen", "last_seen", "uptime_ratio", "restarts"],
        proc_rows,
    ))
    md.append("")

    # Stats per process
    md.append("## Stats per Process")
    md.append("")
    if not result["processes"]:
        md.append("(no processes)")
        md.append("")
    for p in result["processes"]:
        md.append(f"### `{p['name']}`")
        md.append("")
        md.append(_stats_row("CPU%", p["stats"]["cpu_pct"]))
        md.append(_stats_row("Mem PSS (MB)", p["stats"]["mem_pss_mb"]))
        md.append(f"- **Alerts**: cpu={p['alerts']['cpu']}, mem={p['alerts']['mem']}")
        md.append("")

    # Incidents
    md.append("## Incidents")
    md.append("")
    inc_rows = []
    for i in result["incidents"]:
        ev = i.get("evidence", {})
        if i["type"] == "cpu_threshold":
            top = ev.get("top_threads") or []
            evidence_summary = (
                f"Top: {top[0]['name']} @ {top[0]['cpu_pct']:.1f}%"
                if top else "—"
            )
        else:
            cats = ev.get("top_categories") or []
            heap_status = ev.get("heap_status", "?")
            evidence_summary = (
                f"heap={heap_status}; top: {cats[0]['name']} @ {cats[0]['pss_mb']:.1f}MB"
                if cats else f"heap={heap_status}"
            )
        inc_rows.append([
            i["id"],
            i["type"].replace("_threshold", ""),
            f"`{i['process']}`",
            f"{i['observed']['value_at_trigger']:.1f}",
            f"{i['observed']['peak']:.1f}",
            f"{i['threshold']['value']:.0f} / {i['threshold']['sustain_sec']:.0f}s",
            evidence_summary,
        ])
    md.append(_table(
        ["id", "type", "process", "value@trigger", "peak", "thr/sustain", "evidence"],
        inc_rows,
    ))
    md.append("")

    # Per-incident details (helps AI cite specifics)
    if result["incidents"]:
        md.append("### Incident details")
        md.append("")
        for i in result["incidents"]:
            md.append(f"#### {i['id']} — `{i['process']}` ({i['type']})")
            md.append("")
            md.append(f"- Triggered: {i['triggered_at']}")
            md.append(f"- Threshold: {i['threshold']['value']} "
                      f"({i['threshold']['metric']}), sustained {i['observed']['duration_above_sec']:.0f}s")
            md.append(f"- Peak: {i['observed']['peak']:.2f}")
            ev = i.get("evidence", {})
            if i["type"] == "cpu_threshold":
                tops = ev.get("top_threads") or []
                if tops:
                    md.append("- Top threads:")
                    for t in tops[:5]:
                        md.append(f"  - `{t['name']}` (tid={t['tid']}): {t['cpu_pct']:.2f}%")
                if ev.get("raw_file"):
                    md.append(f"- Raw: `incidents/{ev['raw_file']}`")
            else:
                md.append(f"- Heap status: **{ev.get('heap_status', '?')}**")
                if ev.get("fallback_reason"):
                    md.append(f"- Fallback reason: {ev['fallback_reason']}")
                cats = ev.get("top_categories") or []
                if cats:
                    md.append("- Top memory categories:")
                    for c in cats[:5]:
                        md.append(f"  - {c['name']}: {c['pss_mb']:.2f} MB")
                if ev.get("meminfo_file"):
                    md.append(f"- meminfo -d: `incidents/{ev['meminfo_file']}`")
                if ev.get("hprof_file"):
                    md.append(f"- hprof: `incidents/{ev['hprof_file']}` "
                              f"({ev.get('hprof_size_bytes', 0)} bytes)")
            md.append("")

    # Lifecycle
    md.append("## Lifecycle Events")
    md.append("")
    life_rows = []
    for e in result["lifecycle_events"]:
        life_rows.append([
            e["timestamp"], f"`{e['process']}`", e["event"],
            str(e.get("old_pid", 0)), str(e.get("new_pid", 0)),
            f"{e.get('gap_sec', 0):.2f}",
        ])
    md.append(_table(
        ["timestamp", "process", "event", "old_pid", "new_pid", "gap_sec"],
        life_rows,
    ))
    md.append("")

    # Bookmarks
    md.append("## Bookmarks")
    md.append("")
    bm_rows = [[b["timestamp"], b["label"]] for b in result["bookmarks"]]
    md.append(_table(["timestamp", "label"], bm_rows))
    md.append("")

    # Files
    md.append("## Files")
    md.append("")
    md.append("- `report.json` — authoritative structured result")
    md.append("- `summary.md` — this file")
    md.append("- `report.html` — interactive charts")
    for kind in ("cpu", "mem", "lifecycle"):
        for f in result["data_files"].get(kind, []):
            md.append(f"- `{f}` — {kind} time series")
    if result["incidents"]:
        md.append("- `incidents/` — raw + parsed evidence per alert")
    md.append("")

    return "\n".join(md)


def write(result: dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / SUMMARY_FILENAME
    path.write_text(render(result), encoding="utf-8")
    return path
