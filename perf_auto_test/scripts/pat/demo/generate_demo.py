#!/usr/bin/env python3
"""Generate a rich demo performance report for README screenshots.

Run from perf_auto_test/scripts/:
    python -m pat.demo.generate_demo
"""

from __future__ import annotations

import csv
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from pat.reporter.html import HTML_FILENAME, render  # noqa: E402

# ── reproducible RNG ─────────────────────────────────────────────────────────
rng = random.Random(42)

# ── scenario constants ────────────────────────────────────────────────────────
PACKAGE = "com.example.perftest"
APP_NAME = "PerfTest Demo App"
DEVICE_SERIAL = "HT7C61A00001"
ANDROID_VER = "14"
SDK_INT = 34
CPU_CORES = 8

CPU_THRESHOLD = 300.0  # %  (realistic for 8-core device)
MEM_THRESHOLD = 400.0  # MB PSS

T0 = datetime(2024, 3, 15, 9, 0, 0, tzinfo=timezone.utc)
DURATION = 900  # 15 min

PROCS = [
    {"name": PACKAGE, "pid": 8100},
    {"name": f"{PACKAGE}:render", "pid": 8150},
    {"name": f"{PACKAGE}:push", "pid": 8155},
]

CPU_INTERVAL = 1  # sec
MEM_INTERVAL = 5  # sec

OUTPUT_DIR = SCRIPTS_DIR / "reports" / "demo"


# ── timestamp helper ──────────────────────────────────────────────────────────
def ts(offset_sec: float) -> str:
    dt = T0 + timedelta(seconds=offset_sec)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


# ── signal generators ─────────────────────────────────────────────────────────
def _gauss(x: float, center: float, width: float) -> float:
    return math.exp(-0.5 * ((x - center) / width) ** 2)


def cpu_main(t: float) -> float:
    """Main process CPU%.  Two large spikes that breach the 300 % threshold."""
    base = 65 + 15 * math.sin(2 * math.pi * t / 120) + rng.gauss(0, 8)
    spike1 = 320 * _gauss(t, 308, 22)  # t≈298–340 s
    spike2 = 340 * _gauss(t, 662, 20)  # t≈640–690 s
    return max(0.0, base + spike1 + spike2)


def cpu_render(t: float) -> float:
    """:render process CPU%.  Gap around t=520 (crash+restart)."""
    if 518 < t < 526:
        return 0.0
    base = 45 + 10 * math.sin(2 * math.pi * t / 90 + 1.2) + rng.gauss(0, 6)
    spike1 = 85 * _gauss(t, 308, 18)
    spike2 = 95 * _gauss(t, 662, 16)
    return max(0.0, base + spike1 + spike2)


def cpu_push(t: float) -> float:
    return max(0.0, 8 + rng.gauss(0, 3))


def mem_main(t: float) -> float:
    """Main process PSS MB.  Gradual growth crossing the 400 MB threshold twice."""
    growth = 260 + 235 * (1 - math.exp(-t / 600))
    bump = 30 * _gauss(t, 480, 120)
    return max(50.0, growth + bump + rng.gauss(0, 6))


def mem_render(t: float) -> float:
    """:render PSS MB.  Drop and ramp after restart."""
    if 518 < t < 526:
        return 0.0
    if 526 <= t < 560:
        return max(0.0, 18 + (35 - 18) * (t - 526) / 34 + rng.gauss(0, 2))
    return max(0.0, 38 + 8 * math.sin(2 * math.pi * t / 300) + rng.gauss(0, 2))


def mem_push(t: float) -> float:
    return 0.0 if t > 870 else max(0.0, 22 + rng.gauss(0, 1.5))


# ── build CSV rows ────────────────────────────────────────────────────────────
def build_cpu_rows() -> list[dict]:
    rows = []
    for i in range(DURATION // CPU_INTERVAL + 1):
        t = i * CPU_INTERVAL
        t_str = ts(t)
        rows += [
            {
                "timestamp": t_str,
                "process_name": PROCS[0]["name"],
                "pid": PROCS[0]["pid"],
                "cpu_pct": round(cpu_main(t), 2),
            },
            {
                "timestamp": t_str,
                "process_name": PROCS[1]["name"],
                "pid": PROCS[1]["pid"],
                "cpu_pct": round(cpu_render(t), 2),
            },
            {
                "timestamp": t_str,
                "process_name": PROCS[2]["name"],
                "pid": PROCS[2]["pid"],
                "cpu_pct": round(cpu_push(t), 2),
            },
        ]
    return rows


def build_mem_rows() -> list[dict]:
    rows = []
    for i in range(DURATION // MEM_INTERVAL + 1):
        t = i * MEM_INTERVAL
        t_str = ts(t)

        def _row(name, pid, pss):
            if pss <= 0:
                return None
            return {
                "timestamp": t_str,
                "process_name": name,
                "pid": pid,
                "pss_mb": round(pss, 2),
                "java_heap_mb": round(pss * 0.32, 2),
                "native_heap_mb": round(pss * 0.41, 2),
                "graphics_mb": round(pss * 0.12, 2),
                "code_mb": round(pss * 0.10, 2),
                "stack_mb": round(pss * 0.02, 2),
            }

        for r in [
            _row(PROCS[0]["name"], PROCS[0]["pid"], mem_main(t)),
            _row(PROCS[1]["name"], PROCS[1]["pid"], mem_render(t)),
            _row(PROCS[2]["name"], PROCS[2]["pid"], mem_push(t)),
        ]:
            if r:
                rows.append(r)
    return rows


# ── statistics ────────────────────────────────────────────────────────────────
def stats(values: list[float]) -> dict:
    if not values:
        return {}
    n = len(values)
    sv = sorted(values)
    return {
        "mean": round(sum(values) / n, 2),
        "p50": round(sv[int(n * 0.50)], 2),
        "p90": round(sv[int(n * 0.90)], 2),
        "p95": round(sv[int(n * 0.95)], 2),
        "max": round(max(values), 2),
        "samples": n,
    }


# ── assemble report.json ──────────────────────────────────────────────────────
def build_report(cpu_rows: list[dict], mem_rows: list[dict]) -> dict:
    cpu_by_proc: dict[str, list[float]] = {}
    mem_by_proc: dict[str, list[float]] = {}
    for r in cpu_rows:
        cpu_by_proc.setdefault(r["process_name"], []).append(r["cpu_pct"])
    for r in mem_rows:
        mem_by_proc.setdefault(r["process_name"], []).append(r["pss_mb"])

    processes = []
    for p in PROCS:
        name = p["name"]
        is_push = name.endswith(":push")
        is_render = name.endswith(":render")
        processes.append(
            {
                "name": name,
                "first_seen_at": ts(0 if name == PACKAGE else (2 if is_render else 4)),
                "last_seen_at": ts(870 if is_push else DURATION),
                "uptime_ratio": 0.968 if is_push else (0.981 if is_render else 0.999),
                "restart_count": 1 if is_render else 0,
                "stats": {
                    "cpu_pct": stats(cpu_by_proc.get(name, [])),
                    "mem_pss_mb": stats(mem_by_proc.get(name, [])),
                },
                "alerts": {
                    "cpu": 2 if name == PACKAGE else 0,
                    "mem": 2 if name == PACKAGE else 0,
                },
                "sample_failures": {"cpu": 0, "mem": 0},
            }
        )

    cpu_csv = "cpu_2024-03-15_09.csv"
    mem_csv = "mem_2024-03-15_09.csv"
    lc_csv = "lifecycle_2024-03-15_09.csv"

    return {
        "schema_version": "1.0",
        "run": {
            "started_at": ts(0),
            "ended_at": ts(DURATION),
            "duration_sec": float(DURATION),
            "exit_code": 0,
            "exit_reason": "duration_elapsed",
            "device": {
                "serial": DEVICE_SERIAL,
                "android_version": ANDROID_VER,
                "sdk_int": SDK_INT,
                "cpu_cores": CPU_CORES,
            },
            "package": PACKAGE,
            "config_effective": {
                "package": PACKAGE,
                "output_dir": "reports/demo",
                "device": None,
                "wait_timeout_sec": 60.0,
                "cpu_interval_sec": float(CPU_INTERVAL),
                "mem_interval_sec": float(MEM_INTERVAL),
                "rescan_interval_sec": 5.0,
                "process_filter": None,
                "cpu_threshold_percent": CPU_THRESHOLD,
                "cpu_sustain_sec": 10.0,
                "cpu_cooldown_sec": 180.0,
                "mem_threshold_pss_mb": MEM_THRESHOLD,
                "mem_sustain_sec": 60.0,
                "mem_cooldown_sec": 300.0,
                "enable_heap_dumps": True,
                "max_cpu_dumps": 50,
                "max_heap_dumps": 20,
                "max_concurrent_dumps": 2,
                "emit_html": True,
                "status_interval_sec": 10.0,
            },
        },
        "data_files": {
            "cpu": [cpu_csv],
            "mem": [mem_csv],
            "lifecycle": [lc_csv],
        },
        "processes": processes,
        "incidents": [
            # ── CPU incident 001 ────────────────────────────────────────────
            {
                "id": "incident-001",
                "type": "cpu_threshold",
                "process": PACKAGE,
                "pid": 8100,
                "triggered_at": ts(298),
                "threshold": {
                    "metric": "cpu_pct",
                    "value": CPU_THRESHOLD,
                    "sustain_sec": 10.0,
                    "cooldown_sec": 180.0,
                },
                "observed": {"value_at_trigger": 312.4, "peak": 387.1, "duration_above_sec": 47.2},
                "evidence": {
                    "top_threads": [
                        {"tid": "8101", "name": "RenderThread", "cpu_pct": 148.3},
                        {"tid": "8102", "name": "GL Thread", "cpu_pct": 95.7},
                        {"tid": "8103", "name": "VideoDecoder", "cpu_pct": 72.4},
                        {"tid": "8104", "name": "AudioMixer", "cpu_pct": 38.2},
                        {"tid": "8105", "name": "main", "cpu_pct": 22.1},
                    ],
                    "top_threads_count": 18,
                    "raw_file": "cpu_incident-001_top.txt",
                    "task_stat_file": "cpu_incident-001_task.txt",
                },
                "_source_file": "cpu_incident-001.json",
            },
            # ── Mem incident 002 ────────────────────────────────────────────
            {
                "id": "incident-002",
                "type": "mem_threshold",
                "process": PACKAGE,
                "pid": 8100,
                "triggered_at": ts(448),
                "threshold": {
                    "metric": "mem_pss_mb",
                    "value": MEM_THRESHOLD,
                    "sustain_sec": 60.0,
                    "cooldown_sec": 300.0,
                },
                "observed": {"value_at_trigger": 414.8, "peak": 467.3, "duration_above_sec": 68.5},
                "evidence": {
                    "heap_status": "ok",
                    "fallback_reason": None,
                    "hprof_file": "heap_incident-002_pid8100.hprof",
                    "hprof_size_bytes": 68_157_440,
                    "meminfo_file": "heap_incident-002_pid8100.meminfo.txt",
                    "meminfo_parsed_file": "heap_incident-002_pid8100.meminfo.json",
                    "top_categories": [
                        {"name": "Native Heap", "pss_mb": 178.4},
                        {"name": "Java Heap", "pss_mb": 134.2},
                        {"name": "GL mtrack", "pss_mb": 54.7},
                        {"name": "Code", "pss_mb": 41.3},
                        {"name": "Stack", "pss_mb": 8.2},
                    ],
                },
                "_source_file": "heap_incident-002.json",
            },
            # ── CPU incident 003 ────────────────────────────────────────────
            {
                "id": "incident-003",
                "type": "cpu_threshold",
                "process": PACKAGE,
                "pid": 8100,
                "triggered_at": ts(655),
                "threshold": {
                    "metric": "cpu_pct",
                    "value": CPU_THRESHOLD,
                    "sustain_sec": 10.0,
                    "cooldown_sec": 180.0,
                },
                "observed": {"value_at_trigger": 326.8, "peak": 401.6, "duration_above_sec": 39.1},
                "evidence": {
                    "top_threads": [
                        {"tid": "8101", "name": "RenderThread", "cpu_pct": 162.5},
                        {"tid": "8102", "name": "GL Thread", "cpu_pct": 88.3},
                        {"tid": "8103", "name": "ImageProcessor", "cpu_pct": 81.7},
                        {"tid": "8104", "name": "CacheManager", "cpu_pct": 29.6},
                    ],
                    "top_threads_count": 21,
                    "raw_file": "cpu_incident-003_top.txt",
                    "task_stat_file": "cpu_incident-003_task.txt",
                },
                "_source_file": "cpu_incident-003.json",
            },
            # ── Mem incident 004 ────────────────────────────────────────────
            {
                "id": "incident-004",
                "type": "mem_threshold",
                "process": PACKAGE,
                "pid": 8100,
                "triggered_at": ts(803),
                "threshold": {
                    "metric": "mem_pss_mb",
                    "value": MEM_THRESHOLD,
                    "sustain_sec": 60.0,
                    "cooldown_sec": 300.0,
                },
                "observed": {"value_at_trigger": 422.5, "peak": 491.8, "duration_above_sec": 97.4},
                "evidence": {
                    "heap_status": "fallback",
                    "fallback_reason": "am dumpheap returned non-zero exit code",
                    "hprof_file": None,
                    "hprof_size_bytes": 0,
                    "meminfo_file": "heap_incident-004_pid8100.meminfo.txt",
                    "meminfo_parsed_file": "heap_incident-004_pid8100.meminfo.json",
                    "top_categories": [
                        {"name": "Native Heap", "pss_mb": 195.6},
                        {"name": "Java Heap", "pss_mb": 141.8},
                        {"name": "GL mtrack", "pss_mb": 61.2},
                        {"name": "Code", "pss_mb": 42.7},
                        {"name": "Stack", "pss_mb": 9.1},
                    ],
                },
                "_source_file": "heap_incident-004.json",
            },
        ],
        "lifecycle_events": [
            {
                "timestamp": ts(0.5),
                "process": PACKAGE,
                "event": "new",
                "old_pid": 0,
                "new_pid": 8100,
                "gap_sec": 0.0,
            },
            {
                "timestamp": ts(2.1),
                "process": f"{PACKAGE}:render",
                "event": "new",
                "old_pid": 0,
                "new_pid": 8150,
                "gap_sec": 0.0,
            },
            {
                "timestamp": ts(4.3),
                "process": f"{PACKAGE}:push",
                "event": "new",
                "old_pid": 0,
                "new_pid": 8155,
                "gap_sec": 0.0,
            },
            {
                "timestamp": ts(519.8),
                "process": f"{PACKAGE}:render",
                "event": "restart",
                "old_pid": 8150,
                "new_pid": 8235,
                "gap_sec": 1.4,
            },
            {
                "timestamp": ts(870.2),
                "process": f"{PACKAGE}:push",
                "event": "gone",
                "old_pid": 8155,
                "new_pid": 0,
                "gap_sec": 0.0,
            },
        ],
        "bookmarks": [
            {"timestamp": ts(62.0), "label": "Cold start complete"},
            {"timestamp": ts(242.0), "label": "Stress test: video streaming"},
            {"timestamp": ts(602.0), "label": "Stress test: image processing"},
        ],
    }


# ── write helpers ─────────────────────────────────────────────────────────────
def write_csv(path: Path, prefix: str, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# perf_auto_test/{prefix}/v1\n")
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    out = OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / "incidents").mkdir(exist_ok=True)

    cpu_rows = build_cpu_rows()
    mem_rows = build_mem_rows()

    # CSVs
    write_csv(
        out / "cpu_2024-03-15_09.csv",
        "cpu",
        ["timestamp", "process_name", "pid", "cpu_pct"],
        cpu_rows,
    )
    write_csv(
        out / "mem_2024-03-15_09.csv",
        "mem",
        [
            "timestamp",
            "process_name",
            "pid",
            "pss_mb",
            "java_heap_mb",
            "native_heap_mb",
            "graphics_mb",
            "code_mb",
            "stack_mb",
        ],
        mem_rows,
    )

    # Lifecycle CSV
    lc_path = out / "lifecycle_2024-03-15_09.csv"
    with open(lc_path, "w", newline="", encoding="utf-8") as f:
        f.write("# perf_auto_test/lifecycle/v1\n")
        w = csv.writer(f)
        w.writerow(["timestamp", "process_name", "pid", "event"])
        w.writerows(
            [
                [ts(0.5), PACKAGE, 8100, "new"],
                [ts(2.1), f"{PACKAGE}:render", 8150, "new"],
                [ts(4.3), f"{PACKAGE}:push", 8155, "new"],
                [ts(519.8), f"{PACKAGE}:render", 8235, "restart"],
                [ts(870.2), f"{PACKAGE}:push", 8155, "gone"],
            ]
        )

    # Placeholder evidence files (so file links in the report work)
    for name in [
        "cpu_incident-001_top.txt",
        "cpu_incident-001_task.txt",
        "cpu_incident-003_top.txt",
        "cpu_incident-003_task.txt",
        "heap_incident-002_pid8100.meminfo.txt",
        "heap_incident-004_pid8100.meminfo.txt",
    ]:
        (out / "incidents" / name).write_text(f"[demo placeholder: {name}]\n")

    # report.json
    report = build_report(cpu_rows, mem_rows)
    (out / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # report.html
    html = render(report, out)
    (out / HTML_FILENAME).write_text(html, encoding="utf-8")

    print(f"✓  Demo report → {out}")
    print(f"   HTML: {out / HTML_FILENAME}")


if __name__ == "__main__":
    main()
