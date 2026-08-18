"""Resource risk S2 semantics (T-L1-023 .. T-L1-025)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sat.collectors.resource_risk import (
    CAPABILITY_UNAVAILABLE,
    ResourceRiskDetector,
    ResourceRiskMonitor,
    ResourceSample,
)

# ── T-L1-023: permission denial is `unavailable`, never a fake 0 ─────────────


def test_permission_denied_fd_is_unavailable_not_zero():
    adb = MagicMock()

    def shell(cmd, check=False, timeout=5.0):
        if cmd.startswith("pidof"):
            return MagicMock(returncode=0, stdout="1234")
        if ">/dev/null 2>&1; echo $?" in cmd:
            return MagicMock(returncode=0, stdout="1")  # ls denied
        if "wc -l" in cmd:
            return MagicMock(returncode=0, stdout="0")
        if "/proc/1234/stat" in cmd:
            return MagicMock(
                returncode=0,
                stdout="1234 (proc) S 1 1 1 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0 12345 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0",
            )
        if "/proc/1234/status" in cmd:
            return MagicMock(returncode=0, stdout="VmRSS:\t  123456 kB\n")
        return MagicMock(returncode=0, stdout="")

    adb.shell.side_effect = shell
    monitor = ResourceRiskMonitor(adb, "com.example.app")
    samples = monitor._default_sample()
    assert len(samples) == 1
    sample = samples[0]
    # FD denied → None value + error + unavailable capability.
    assert sample.fd_count is None
    assert "fd_count" in sample.errors
    assert sample.capability("fd_count") == CAPABILITY_UNAVAILABLE
    d = sample.to_dict()
    assert d["fd_count"] is None
    assert d["capabilities"]["fd_count"] == CAPABILITY_UNAVAILABLE
    # Never written as 0 anywhere.
    assert d["fd_count"] != 0


# ── T-L1-024: FD + thread both over threshold → both reported ────────────────


def test_two_metrics_raise_two_risks():
    detector = ResourceRiskDetector(
        fd_growth_threshold=100,
        thread_growth_threshold=20,
    )
    first = ResourceSample(
        pid=1,
        ts=0.0,
        fd_count=10,
        thread_count=5,
        process_start_time="123",
    )
    assert detector.observe_all(first) == []
    second = ResourceSample(
        pid=1,
        ts=1.0,
        fd_count=200,
        thread_count=80,
        process_start_time="123",
    )
    events = detector.observe_all(second)
    metrics = {e.metric for e in events}
    assert metrics == {"fd_count", "thread_count"}, "no first-match loss"


# ── T-L1-025: PID reuse with a new process epoch resets the baseline ─────────


def test_pid_reuse_new_epoch_resets_baseline():
    detector = ResourceRiskDetector(fd_growth_threshold=100)
    old_proc = ResourceSample(
        pid=7,
        ts=0.0,
        fd_count=10,
        process_start_time="aaaa",
    )
    assert detector.observe(old_proc) is None
    # Same pid, new process epoch: baseline must restart.
    new_proc = ResourceSample(
        pid=7,
        ts=1.0,
        fd_count=200,
        process_start_time="bbbb",
    )
    assert detector.observe_all(new_proc) == [], "old baseline must not leak"
    new_proc2 = ResourceSample(
        pid=7,
        ts=2.0,
        fd_count=350,
        process_start_time="bbbb",
    )
    events = detector.observe_all(new_proc2)
    assert len(events) == 1
    assert events[0].baseline == 200  # baseline from the new epoch


# ── app self-reported samples feed the detector (SAT_RESOURCE_SAMPLE) ────────

def test_pool_consumes_self_reported_resource_samples(tmp_path: Path):
    import time as time_mod
    from unittest.mock import MagicMock

    from sat.pool import CollectorPool, CollectorsConfig, DumpsConfig
    from sat.storage import (
        EVENTS_COLUMNS,
        EVENTS_SCHEMA_TAG,
        LIFECYCLE_COLUMNS,
        LIFECYCLE_SCHEMA_TAG,
        CsvStreamWriter,
    )

    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(
        tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG,
    )
    pool = CollectorPool(
        MagicMock(),
        "com.example.app",
        events_writer=ev,
        lifecycle_writer=life,
        incidents_dir=tmp_path / "incidents",
        collectors=CollectorsConfig(
            logcat_enabled=False,
            resource_risk_enabled=True,
            resource_risk_interval_sec=0.1,
            resource_fd_growth_threshold=100,
            resource_thread_growth_threshold=20,
        ),
        discover_fn=lambda a, p: [],
        dumps=DumpsConfig(post_context_sec=0.0),
    )
    pool.start()
    try:
        # Baseline self-report (pre-leak).
        pool._record_fault_marker(
            "08-13 10:00:00.100  1234  1234 I SAT: SAT_RESOURCE_SAMPLE id=f1 fd_count=40 thread_count=10"
        )
        # Post-leak self-report.
        pool._record_fault_marker(
            "08-13 10:00:05.100  1234  1234 I SAT: SAT_RESOURCE_SAMPLE id=f1 fd_count=250 thread_count=80"
        )
        deadline = time_mod.monotonic() + 5.0
        while time_mod.monotonic() < deadline:
            events = pool.resource_risk_events()
            if len(events) >= 2:
                break
            time_mod.sleep(0.1)
        pool.stop(join_timeout=1.0)
    finally:
        ev.close()
        life.close()
    metrics = {e["metric"] for e in pool.resource_risk_events()}
    assert metrics == {"fd_count", "thread_count"}, pool.resource_risk_events()
