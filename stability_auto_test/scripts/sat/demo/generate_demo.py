#!/usr/bin/env python3
"""Generate a rich demo stability report for README screenshots.

Run from stability_auto_test/scripts/:
    python -m sat.demo.generate_demo
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from sat.reporter.html import write  # noqa: E402

# ── scenario constants ────────────────────────────────────────────────────────
PACKAGE = "com.example.navapp"
DEVICE_SERIAL = "HT9A12B00042"
ANDROID_VER = "14"
SDK_INT = 34
CPU_CORES = 8

T0 = datetime(2024, 6, 20, 14, 0, 0, tzinfo=timezone.utc)
DURATION = 5400  # 90 min

PROCS = [
    {"name": PACKAGE, "pid": 5100},
    {"name": f"{PACKAGE}:locationservice", "pid": 5140},
    {"name": f"{PACKAGE}:push", "pid": 5145},
]

OUTPUT_DIR = SCRIPTS_DIR / "reports" / "demo"


def ts(offset_sec: float) -> str:
    dt = T0 + timedelta(seconds=offset_sec)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def device_ts(offset_sec: float) -> str:
    dt = T0 + timedelta(seconds=offset_sec)
    return dt.strftime("%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


# ── incidents ─────────────────────────────────────────────────────────────────
INCIDENTS = [
    # ── java_crash #001 ──────────────────────────────────────────────────────
    {
        "id": "incident-001",
        "type": "java_crash",
        "process": PACKAGE,
        "pid": 5100,
        "triggered_at": ts(720),
        "severity": "fatal",
        "summary": "java_crash: java.lang.NullPointerException in MapRenderer",
        "evidence": {
            "logcat_slice_file": "java_crash_t720_pid5100.txt",
            "trace_file": None,
            "exception_class": "java.lang.NullPointerException",
            "signal": None,
            "fault_addr": None,
            "reason": None,
            "top_frames": [
                "com.example.navapp.render.MapRenderer.drawTile(MapRenderer.java:412)",
                "com.example.navapp.render.MapRenderer.renderFrame(MapRenderer.java:287)",
                "com.example.navapp.render.RenderThread.run(RenderThread.java:94)",
                "android.os.Handler.handleCallback(Handler.java:959)",
                "android.os.Handler.dispatchMessage(Handler.java:100)",
                "android.os.Looper.loopOnce(Looper.java:232)",
                "android.os.Looper.loop(Looper.java:317)",
                "android.app.ActivityThread.main(ActivityThread.java:8705)",
            ],
            "source": "logcat",
            "dedup_count": 1,
            "fallback_reason": None,
            "device_ts": device_ts(720),
        },
        "_source_file": "java_crash_t720_pid5100.json",
    },
    # ── native_crash #002 ────────────────────────────────────────────────────
    {
        "id": "incident-002",
        "type": "native_crash",
        "process": PACKAGE,
        "pid": 5178,
        "triggered_at": ts(1680),
        "severity": "fatal",
        "summary": "native_crash: SIGSEGV in libmapcore.so",
        "evidence": {
            "logcat_slice_file": "native_crash_t1680_pid5178.txt",
            "trace_file": "native_crash_t1680_pid5178.tombstone",
            "exception_class": None,
            "signal": "SIGSEGV",
            "fault_addr": "0x0000000000000018",
            "reason": "Segmentation fault",
            "top_frames": [
                "#00 pc 0x00000000003a1c84  /data/app/com.example.navapp/lib/arm64/libmapcore.so (TileCache::get+0x48)",
                "#01 pc 0x00000000003b2210  /data/app/com.example.navapp/lib/arm64/libmapcore.so (RasterLayer::render+0x1a4)",
                "#02 pc 0x00000000003c0098  /data/app/com.example.navapp/lib/arm64/libmapcore.so (MapView::drawFrame+0x88)",
                "#03 pc 0x00000000000b1420  /system/lib64/libandroid.so (ANativeWindow_setBuffersGeometry+0x2c0)",
            ],
            "source": "logcat",
            "dedup_count": 1,
            "fallback_reason": None,
            "device_ts": device_ts(1680),
        },
        "_source_file": "native_crash_t1680_pid5178.json",
    },
    # ── anr #003 ─────────────────────────────────────────────────────────────
    {
        "id": "incident-003",
        "type": "anr",
        "process": PACKAGE,
        "pid": 5231,
        "triggered_at": ts(2700),
        "severity": "error",
        "summary": "anr: Input dispatching timed out — main thread blocked on GPS fix",
        "evidence": {
            "logcat_slice_file": "anr_t2700_pid5231.txt",
            "trace_file": None,
            "exception_class": None,
            "signal": None,
            "fault_addr": None,
            "reason": "Input dispatching timed out (Application does not have a focused window)",
            "top_frames": [],
            "source": "logcat",
            "dedup_count": 1,
            "fallback_reason": "ANR trace pull failed: /data/anr not accessible on user build",
            "device_ts": device_ts(2700),
        },
        "_source_file": "anr_t2700_pid5231.json",
    },
    # ── process_death #004 ───────────────────────────────────────────────────
    {
        "id": "incident-004",
        "type": "process_death",
        "process": f"{PACKAGE}:push",
        "pid": 5145,
        "triggered_at": ts(3120),
        "severity": "warning",
        "summary": "process_death: cached-empty (19) — OOM killer reclaimed :push",
        "evidence": {
            "logcat_slice_file": None,
            "trace_file": None,
            "exception_class": None,
            "signal": None,
            "fault_addr": None,
            "reason": "cached-empty (19)",
            "top_frames": [],
            "source": "logcat",
            "dedup_count": 1,
            "fallback_reason": None,
            "device_ts": device_ts(3120),
        },
        "_source_file": "process_death_t3120_pid5145.json",
    },
    # ── java_crash #005 ──────────────────────────────────────────────────────
    {
        "id": "incident-005",
        "type": "java_crash",
        "process": PACKAGE,
        "pid": 5231,
        "triggered_at": ts(4020),
        "severity": "fatal",
        "summary": "java_crash: java.lang.OutOfMemoryError in BitmapFactory",
        "evidence": {
            "logcat_slice_file": "java_crash_t4020_pid5231.txt",
            "trace_file": None,
            "exception_class": "java.lang.OutOfMemoryError",
            "signal": None,
            "fault_addr": None,
            "reason": "Failed to allocate a 12582912 byte allocation",
            "top_frames": [
                "android.graphics.BitmapFactory.nativeDecodeAsset(Native Method)",
                "android.graphics.BitmapFactory.decodeStream(BitmapFactory.java:709)",
                "com.example.navapp.map.TileLoader.loadBitmap(TileLoader.java:203)",
                "com.example.navapp.map.TileLoader.fetchTile(TileLoader.java:148)",
                "com.example.navapp.map.TileCache.get(TileCache.java:89)",
                "com.example.navapp.render.MapRenderer.drawTile(MapRenderer.java:398)",
            ],
            "source": "dropbox",
            "dedup_count": 1,
            "fallback_reason": None,
            "device_ts": device_ts(4020),
        },
        "_source_file": "java_crash_t4020_pid5231.json",
    },
    # ── anr #006 ─────────────────────────────────────────────────────────────
    {
        "id": "incident-006",
        "type": "anr",
        "process": PACKAGE,
        "pid": 5289,
        "triggered_at": ts(4680),
        "severity": "error",
        "summary": "anr: Broadcast of Intent timed out — RouteCalculationService unresponsive",
        "evidence": {
            "logcat_slice_file": "anr_t4680_pid5289.txt",
            "trace_file": "anr_t4680_pid5289.trace",
            "exception_class": None,
            "signal": None,
            "fault_addr": None,
            "reason": "Broadcast of Intent { act=com.example.navapp.ROUTE_UPDATE } timed out",
            "top_frames": [],
            "source": "logcat",
            "dedup_count": 2,
            "fallback_reason": None,
            "device_ts": device_ts(4680),
        },
        "_source_file": "anr_t4680_pid5289.json",
    },
]

# ── lifecycle events ──────────────────────────────────────────────────────────
LIFECYCLE = [
    {
        "timestamp": ts(1.0),
        "process": PACKAGE,
        "event": "new",
        "old_pid": 0,
        "new_pid": 5100,
        "gap_sec": 0.0,
    },
    {
        "timestamp": ts(2.2),
        "process": f"{PACKAGE}:locationservice",
        "event": "new",
        "old_pid": 0,
        "new_pid": 5140,
        "gap_sec": 0.0,
    },
    {
        "timestamp": ts(4.5),
        "process": f"{PACKAGE}:push",
        "event": "new",
        "old_pid": 0,
        "new_pid": 5145,
        "gap_sec": 0.0,
    },
    {
        "timestamp": ts(725.3),
        "process": PACKAGE,
        "event": "restart",
        "old_pid": 5100,
        "new_pid": 5178,
        "gap_sec": 5.2,
    },
    {
        "timestamp": ts(1688.7),
        "process": PACKAGE,
        "event": "restart",
        "old_pid": 5178,
        "new_pid": 5231,
        "gap_sec": 8.3,
    },
    {
        "timestamp": ts(3122.0),
        "process": f"{PACKAGE}:push",
        "event": "gone",
        "old_pid": 5145,
        "new_pid": 0,
        "gap_sec": 0.0,
    },
    {
        "timestamp": ts(4027.4),
        "process": PACKAGE,
        "event": "restart",
        "old_pid": 5231,
        "new_pid": 5289,
        "gap_sec": 7.1,
    },
]

BOOKMARKS = [
    {"timestamp": ts(120.0), "label": "App started, navigating home"},
    {"timestamp": ts(600.0), "label": "Map rendering stress test"},
    {"timestamp": ts(3000.0), "label": "Route planning test"},
]


# ── processes ─────────────────────────────────────────────────────────────────
def build_processes() -> list:
    return [
        {
            "name": PACKAGE,
            "first_seen_at": ts(1.0),
            "last_seen_at": ts(DURATION),
            "uptime_ratio": 0.987,
            "restart_count": 3,
            "events": {"java_crash": 2, "native_crash": 1, "anr": 2, "process_death": 0},
            "sample_failures": {"logcat": 0, "dropbox": 0},
        },
        {
            "name": f"{PACKAGE}:locationservice",
            "first_seen_at": ts(2.2),
            "last_seen_at": ts(DURATION),
            "uptime_ratio": 1.0,
            "restart_count": 0,
            "events": {"java_crash": 0, "native_crash": 0, "anr": 0, "process_death": 0},
            "sample_failures": {"logcat": 0, "dropbox": 0},
        },
        {
            "name": f"{PACKAGE}:push",
            "first_seen_at": ts(4.5),
            "last_seen_at": ts(3122.0),
            "uptime_ratio": 0.578,
            "restart_count": 0,
            "events": {"java_crash": 0, "native_crash": 0, "anr": 0, "process_death": 1},
            "sample_failures": {"logcat": 0, "dropbox": 0},
        },
    ]


# ── full report dict ──────────────────────────────────────────────────────────
def build_report() -> dict:
    ev_csv = "events_2024-06-20_14.csv"
    lc_csv = "lifecycle_2024-06-20_14.csv"
    log_file = "logcat_2024-06-20_14.log"

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
                "rescan_interval_sec": 5.0,
                "process_filter": None,
                "logcat_enabled": True,
                "logcat_buffers": ["main", "system", "events", "crash"],
                "logcat_reconnect_backoff_sec": 2.0,
                "enable_java_crash": True,
                "enable_native_crash": True,
                "enable_anr": True,
                "enable_process_death": True,
                "dedup_window_sec": 5.0,
                "pre_context_sec": 30.0,
                "post_context_sec": 10.0,
                "max_incidents_per_type": 200,
                "max_concurrent_dumps": 2,
                "pull_tombstone": True,
                "pull_anr_trace": True,
                "emit_html": True,
                "status_interval_sec": 10.0,
            },
        },
        "processes": build_processes(),
        "incidents": INCIDENTS,
        "lifecycle_events": LIFECYCLE,
        "bookmarks": BOOKMARKS,
        "data_files": {
            "events": [ev_csv],
            "lifecycle": [lc_csv],
            "logcat": [log_file],
        },
    }


# ── write helpers ─────────────────────────────────────────────────────────────
def write_events_csv(out: Path) -> None:
    path = out / "events_2024-06-20_14.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("# stability_auto_test/events/v1\n")
        w = csv.writer(f)
        w.writerow(["timestamp", "event_type", "process_name", "pid", "severity", "summary"])
        for inc in INCIDENTS:
            w.writerow(
                [
                    inc["triggered_at"],
                    inc["type"],
                    inc["process"],
                    inc["pid"],
                    inc["severity"],
                    inc["summary"],
                ]
            )


def write_lifecycle_csv(out: Path) -> None:
    path = out / "lifecycle_2024-06-20_14.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("# stability_auto_test/lifecycle/v1\n")
        w = csv.writer(f)
        w.writerow(["timestamp", "process_name", "event", "old_pid", "new_pid", "gap_sec"])
        for lc in LIFECYCLE:
            w.writerow(
                [
                    lc["timestamp"],
                    lc["process"],
                    lc["event"],
                    lc["old_pid"],
                    lc["new_pid"],
                    lc["gap_sec"],
                ]
            )


def main() -> None:
    out = OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / "incidents").mkdir(exist_ok=True)

    write_events_csv(out)
    write_lifecycle_csv(out)

    # Placeholder logcat log
    (out / "logcat_2024-06-20_14.log").write_text("[demo logcat placeholder]\n")

    # Placeholder incident evidence files
    for inc in INCIDENTS:
        ev = inc["evidence"]
        for fname in [ev.get("logcat_slice_file"), ev.get("trace_file")]:
            if fname:
                (out / "incidents" / fname).write_text(f"[demo placeholder: {fname}]\n")
        (out / "incidents" / inc["_source_file"]).write_text(
            json.dumps(inc, indent=2, ensure_ascii=False)
        )

    report = build_report()
    (out / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    written = write(report, out)
    print(f"✓  Demo report → {out}")
    print(f"   HTML: {written}")


if __name__ == "__main__":
    main()
