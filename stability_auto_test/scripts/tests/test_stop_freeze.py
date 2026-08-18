"""Cancellable, frozen evidence tasks (spec S1-02 / IMP-02).

Tests T-L1-001 .. T-L1-004: `stop()` must drain normal dumpers, time out
blocked ones, keep exactly one terminal state per event, and leave the output
directory frozen (no late writes after stop returns).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from sat.detection import EVENT_JAVA_CRASH, StabilityEvent
from sat.pool import (
    CollectorPool,
    CollectorsConfig,
    DumpsConfig,
)
from sat.storage import (
    EVENTS_COLUMNS,
    EVENTS_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    CsvStreamWriter,
)

PACKAGE = "com.example.app"


def _writers(tmp_path: Path):
    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG)
    return ev, life


def _pool(tmp_path: Path, *, dumper, dumps: DumpsConfig):
    ev_w, life_w = _writers(tmp_path)
    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=tmp_path / "incidents",
        rescan_interval_sec=10.0,
        collectors=CollectorsConfig(logcat_enabled=False, resource_risk_enabled=False),
        discover_fn=lambda adb, pkg: [],
        java_crash_dump_fn=dumper,
        dumps=dumps,
    )
    return pool, ev_w, life_w


def _dispatch_crash(pool: CollectorPool, pid: int = 4) -> None:
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process=PACKAGE,
            pid=pid,
            triggered_at="2026-08-13 10:00:00.000",
            summary="boom",
        )
    )


def _dir_snapshot(root: Path) -> dict:
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return snap


# ── T-L1-001: normal delayed dumper, stop waits and report is complete ───────


def test_stop_waits_for_delayed_dumper_and_freezes(tmp_path: Path):
    started = threading.Event()

    def delayed_dumper(adb, ev, d, staging_dir=None):
        started.set()
        time.sleep(1.0)
        target = Path(staging_dir) if staging_dir else Path(d)
        (target / "incident.json").write_text(
            json.dumps({"type": ev.event_type, "process": ev.process}),
            encoding="utf-8",
        )
        return {"type": ev.event_type, "process": ev.process}

    pool, ev_w, life_w = _pool(
        tmp_path,
        dumper=delayed_dumper,
        dumps=DumpsConfig(dump_shutdown_timeout_sec=5.0, post_context_sec=0.0),
    )
    pool.start()
    _dispatch_crash(pool)
    assert started.wait(2.0)
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=5.0)
    states = pool.dump_task_states()
    journal_text = (
        (tmp_path / "incident_journal.jsonl").read_text()
        if (tmp_path / "incident_journal.jsonl").exists()
        else "<no journal>"
    )
    assert states["persisted"] == 1, f"states={states} journal={journal_text}"
    assert states["timed_out"] == 0
    # Published evidence is visible in the incident dir.
    incident_files = list((tmp_path / "incidents").glob("*.json"))
    assert len(incident_files) == 1
    # After stop, the output dir must not change.
    before = _dir_snapshot(tmp_path)
    time.sleep(0.3)
    assert _dir_snapshot(tmp_path) == before
    ev_w.close()
    life_w.close()


# ── T-L1-002: permanently blocked dumper, stop returns in bound, no publish ──


def test_blocked_dumper_times_out_staging_not_published(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()

    def blocked_dumper(adb, ev, d, staging_dir=None):
        entered.set()
        # Write into staging, then block far beyond the stop deadline: the
        # staged file must never appear in the incident dir.
        staging_dir = Path(staging_dir)
        (staging_dir / "late_evidence.txt").write_text("secret", encoding="utf-8")
        release.wait(10.0)
        return {"type": ev.event_type, "process": ev.process}

    pool, ev_w, life_w = _pool(
        tmp_path,
        dumper=blocked_dumper,
        dumps=DumpsConfig(dump_shutdown_timeout_sec=0.3, post_context_sec=0.0),
    )
    pool.start()
    _dispatch_crash(pool)
    assert entered.wait(2.0)
    started_stop = time.monotonic()
    pool.stop(join_timeout=0.2, dump_shutdown_timeout_sec=0.3)
    elapsed = time.monotonic() - started_stop
    assert elapsed < 2.0, "stop() must return within the shutdown bound"
    states = pool.dump_task_states()
    assert states["timed_out"] == 1
    assert states["persisted"] == 0
    # Nothing published.
    assert not list((tmp_path / "incidents").glob("*.json"))
    assert not list((tmp_path / "incidents").glob("late_evidence*"))
    # Journal records the single timed_out terminal state.
    journal = (tmp_path / "incident_journal.jsonl").read_text()
    assert journal.count('"status": "timed_out"') == 1
    release.set()
    ev_w.close()
    life_w.close()


# ── T-L1-003: stop vs completion race, exactly one terminal state ────────────


def test_stop_completion_race_single_terminal_state(tmp_path: Path):
    for i in range(50):
        barrier = threading.Barrier(2)

        def racing_dumper(adb, ev, d):
            barrier.wait(timeout=5.0)
            return {"type": ev.event_type, "process": ev.process}

        pool, ev_w, life_w = _pool(
            tmp_path,
            dumper=racing_dumper,
            dumps=DumpsConfig(dump_shutdown_timeout_sec=1.0, post_context_sec=0.0),
        )
        pool.start()
        _dispatch_crash(pool, pid=100 + i)
        # Release both stop() and the dumper at the same instant.
        stopper = threading.Thread(
            target=lambda: pool.stop(
                join_timeout=0.5,
                dump_shutdown_timeout_sec=1.0,
            )
        )
        stopper.start()
        barrier.wait(timeout=5.0)
        stopper.join(timeout=5.0)
        states = pool.dump_task_states()
        terminal = sum(v for k, v in states.items() if k in ("persisted", "failed", "timed_out"))
        assert terminal == 1, f"iteration {i}: states={states}"
        # Journal: exactly one terminal record for the event.
        journal = (tmp_path / "incident_journal.jsonl").read_text()
        assert journal.count('"status": "detected"') == 1
        terminal_statuses = [
            s
            for s in (
                "persisted",
                "failed",
                "timed_out",
                "dropped_by_cap",
                "dropped_by_backpressure",
            )
            if f'"status": "{s}"' in journal
        ]
        assert len(terminal_statuses) == 1, f"iteration {i}: {terminal_statuses}"
        ev_w.close()
        life_w.close()
        tmp_path2 = tmp_path / f"iter-{i}"
        tmp_path2.mkdir()
        tmp_path = tmp_path2


# ── T-L1-004: late worker write after freeze is rejected ─────────────────────


def test_late_worker_cannot_write_after_freeze(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()

    def slow_dumper(adb, ev, d, staging_dir=None):
        entered.set()
        release.wait(10.0)
        return {"type": ev.event_type, "process": ev.process}

    pool, ev_w, life_w = _pool(
        tmp_path,
        dumper=slow_dumper,
        dumps=DumpsConfig(dump_shutdown_timeout_sec=0.2, post_context_sec=0.0),
    )
    pool.start()
    _dispatch_crash(pool)
    assert entered.wait(2.0)
    pool.stop(join_timeout=0.2, dump_shutdown_timeout_sec=0.2)
    assert pool.dump_task_states()["timed_out"] == 1
    before = _dir_snapshot(tmp_path)
    # Release the worker *after* the freeze; it may write to staging only.
    release.set()
    time.sleep(0.4)
    after = _dir_snapshot(tmp_path)
    # No published incident appeared (late publish rejected).
    assert not list((tmp_path / "incidents").glob("*.json"))
    # The incident dir contents are frozen.
    incidents_before = {k: v for k, v in before.items() if k.startswith("incidents/")}
    incidents_after = {k: v for k, v in after.items() if k.startswith("incidents/")}
    assert incidents_before == incidents_after
    ev_w.close()
    life_w.close()


# ── published evidence is byte-identical to staged content ───────────────────


def test_published_file_matches_staged_content(tmp_path: Path):
    def writing_dumper(adb, ev, d, staging_dir=None):
        (Path(staging_dir) / "probe.txt").write_text("payload-42", encoding="utf-8")
        return {"type": ev.event_type, "process": ev.process}

    pool, ev_w, life_w = _pool(
        tmp_path,
        dumper=writing_dumper,
        dumps=DumpsConfig(dump_shutdown_timeout_sec=5.0, post_context_sec=0.0),
    )
    pool.start()
    _dispatch_crash(pool)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (tmp_path / "incidents" / "probe.txt").exists():
            break
        time.sleep(0.05)
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=5.0)
    probe = tmp_path / "incidents" / "probe.txt"
    assert probe.read_text() == "payload-42"
    ev_w.close()
    life_w.close()
