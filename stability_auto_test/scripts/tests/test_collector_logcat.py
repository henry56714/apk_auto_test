from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest
from sat.collectors.logcat import LogcatStream, _extract_device_ts
from sat.health import compute_collector_health


def test_extract_device_ts_threadtime_default():
    assert (
        _extract_device_ts("05-21 10:00:00.123  1234  5678 E AndroidRuntime: x")
        == "05-21 10:00:00.123"
    )


def test_extract_device_ts_with_year():
    assert (
        _extract_device_ts("2026-05-21 10:00:00.123  1234  5678 E X: x")
        == "2026-05-21 10:00:00.123"
    )


def test_extract_device_ts_unparseable():
    assert _extract_device_ts("not a logcat line") is None


def _make_stream_with_lines(lines):
    """Create a LogcatStream whose subprocess yields `lines` once then EOFs."""
    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO("\n".join(lines) + "\n")
    fake_proc.stderr = io.StringIO("")
    fake_proc.terminate = MagicMock()
    fake_proc.wait = MagicMock(return_value=0)
    fake_proc.kill = MagicMock()
    popen_calls = {"count": 0}

    def popen(cmd, **kwargs):
        popen_calls["count"] += 1
        if popen_calls["count"] == 1:
            return fake_proc
        # Subsequent calls: stop the stream so iteration ends.
        stream._stop.set()
        eof = MagicMock()
        eof.stdout = io.StringIO("")
        eof.terminate = MagicMock()
        eof.wait = MagicMock(return_value=0)
        eof.kill = MagicMock()
        return eof

    stream = LogcatStream(serial=None, buffers=["main"], popen_fn=popen, reconnect_backoff_sec=0.0)
    return stream, popen_calls


def test_logcat_stream_yields_lines_and_advances_last_ts():
    lines = [
        "05-21 10:00:00.123  1 1 I tag: hello",
        "05-21 10:00:00.456  2 2 I tag: world",
    ]
    stream, _calls = _make_stream_with_lines(lines)
    out = list(stream.lines())
    assert out == lines
    assert stream._last_device_ts == "05-21 10:00:00.456"
    assert stream._lines_read == 2


def test_logcat_stream_resume_arg_when_reconnecting():
    stream, _ = _make_stream_with_lines(
        [
            "05-21 10:00:00.001  1 1 I tag: x",
        ]
    )
    stream._last_device_ts = "05-21 10:00:00.001"
    cmd = stream._build_cmd()
    # `-T '<ts>'` must be present after the buffer args.
    assert "-T" in cmd
    assert cmd[cmd.index("-T") + 1] == "05-21 10:00:00.001"


def test_logcat_stream_stats_track_up_intervals_and_gaps():
    fake_proc1 = MagicMock()
    fake_proc1.stdout = io.StringIO(
        "05-21 10:00:00.100  1 1 I tag: one\n05-21 10:00:00.200  1 1 I tag: two\n"
    )
    fake_proc1.stderr = io.StringIO("")
    fake_proc1.terminate = MagicMock()
    fake_proc1.wait = MagicMock(return_value=0)
    fake_proc1.kill = MagicMock()

    fake_proc2 = MagicMock()
    fake_proc2.stdout = io.StringIO("")
    fake_proc2.stderr = io.StringIO("")
    fake_proc2.terminate = MagicMock()
    fake_proc2.wait = MagicMock(return_value=0)
    fake_proc2.kill = MagicMock()

    state = {"n": 0}

    def popen(cmd, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return fake_proc1
        stream._stop.set()
        return fake_proc2

    clock = {"now": 100.0}

    def now_fn():
        clock["now"] += 0.25
        return clock["now"]

    stream = LogcatStream(
        serial=None,
        buffers=["main"],
        reconnect_backoff_sec=0.0,
        popen_fn=popen,
        now_fn=now_fn,
    )
    list(stream.lines())
    stream.stop()
    stats = stream.stats

    assert stats["reconnects"] >= 1
    # IMP-06: only connections that produced at least one line count as
    # collecting — the second (empty) connection must be a gap, not "up".
    assert len(stats["up_intervals"]) == 1
    assert len(stats["gap_intervals"]) >= 1
    assert stats["up_intervals"][0][1] <= stats["gap_intervals"][0][0]
    assert stats["started_at"] is not None
    assert stats["ended_at"] is not None


def test_coverage_health_healthy_full_run():
    health = compute_collector_health(
        logcat_stats={
            "up_intervals": [(100.0, 199.0)],
            "reconnects": 0,
        },
        planned_sec=100.0,
        min_coverage_ratio=0.99,
    )
    assert health.coverage_ratio >= 0.99
    assert health.health == "healthy"


def test_coverage_health_degraded_with_twenty_percent_gap():
    health = compute_collector_health(
        logcat_stats={
            "up_intervals": [(100.0, 150.0), (170.0, 200.0)],
            "reconnects": 1,
        },
        planned_sec=100.0,
        min_coverage_ratio=0.99,
    )
    assert health.coverage_ratio == pytest.approx(0.8, abs=0.01)
    assert health.health == "degraded"


def test_coverage_health_inconclusive_when_never_collected():
    health = compute_collector_health(
        logcat_stats={"up_intervals": []},
        planned_sec=100.0,
    )
    assert health.health == "inconclusive"
