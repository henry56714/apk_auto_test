from __future__ import annotations

from pathlib import Path

from sat.context import LogcatContextBuffer, LogEntry, format_context_slice


def _entry(host_ts: float, raw: str = "", **kw) -> LogEntry:
    return LogEntry(
        host_ts=host_ts,
        device_ts=kw.get("device_ts", f"05-21 10:00:00.{int(host_ts % 1000):03d}"),
        pid=kw.get("pid", 1234),
        tid=kw.get("tid", 1234),
        tag=kw.get("tag", "App"),
        level=kw.get("level", "I"),
        raw=raw or f"line-{host_ts}",
    )


def test_snapshot_respects_time_window():
    buf = LogcatContextBuffer(
        retention_sec=1000.0,
        max_entries=10000,
        max_bytes=10 * 1024 * 1024,
        clock=lambda: 110.0,
    )
    # Outside the window (before pre-start).
    for ts in (40.0, 50.0, 60.0, 69.0):
        buf.append(_entry(ts))
    # 20 pre-event lines.
    for ts in (80.0, 81.0, 82.0, 83.0, 84.0, 85.0, 86.0, 87.0, 88.0, 89.0,
               90.0, 91.0, 92.0, 93.0, 94.0, 95.0, 96.0, 97.0, 98.0, 99.0):
        buf.append(_entry(ts))
    # 10 post-event lines.
    for ts in (101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0):
        buf.append(_entry(ts))

    slice_ = buf.snapshot(100.0, pre_sec=30.0, post_sec=10.0, now_ts=110.0)
    assert len(slice_.pre_entries) == 20
    assert len(slice_.post_entries) == 10
    assert slice_.pre_entries[0].host_ts == 80.0
    assert slice_.post_entries[-1].host_ts == 110.0
    assert all(e.host_ts >= 70.0 for e in slice_.pre_entries)


def test_buffer_bounded_by_line_count():
    buf = LogcatContextBuffer(
        retention_sec=100000.0,
        max_entries=100,
        max_bytes=10 * 1024 * 1024,
        clock=lambda: 1.0,
    )
    for i in range(500):
        buf.append(_entry(float(i)))
    stats = buf.stats()
    assert stats["entries"] == 100
    assert stats["dropped_by_cap"] == 400


def test_buffer_bounded_by_bytes():
    buf = LogcatContextBuffer(
        retention_sec=100000.0,
        max_entries=10000,
        max_bytes=500,
        clock=lambda: 1.0,
    )
    for i in range(50):
        buf.append(_entry(float(i), raw="x" * 200))
    stats = buf.stats()
    assert stats["dropped_by_cap"] > 0
    assert stats["entries"] <= 2


def test_zero_pre_and_post_context():
    buf = LogcatContextBuffer(
        retention_sec=1000.0,
        max_entries=1000,
        max_bytes=1024 * 1024,
        clock=lambda: 120.0,
    )
    for ts in (95.0, 100.0, 105.0, 110.0, 115.0, 120.0):
        buf.append(_entry(ts))
    slice_ = buf.snapshot(110.0, pre_sec=0.0, post_sec=0.0, now_ts=120.0)
    assert slice_.pre_entries == []
    assert slice_.post_entries == []
    assert slice_.pre_context_sec_actual == 0.0
    assert slice_.post_context_sec_actual == 0.0


def test_overlapping_slices_do_not_overwrite_each_other(tmp_path: Path):
    buf = LogcatContextBuffer(
        retention_sec=1000.0,
        max_entries=1000,
        max_bytes=1024 * 1024,
        clock=lambda: 50.0,
    )
    for ts in range(10, 50):
        buf.append(_entry(float(ts), raw=f"shared-{ts}"))

    first = buf.snapshot(30.0, pre_sec=10.0, post_sec=5.0, now_ts=50.0)
    second = buf.snapshot(40.0, pre_sec=5.0, post_sec=5.0, now_ts=50.0)
    p1 = tmp_path / "a_context.txt"
    p2 = tmp_path / "b_context.txt"
    p1.write_text(format_context_slice(["event-a"], first), encoding="utf-8")
    p2.write_text(format_context_slice(["event-b"], second), encoding="utf-8")

    t1 = p1.read_text(encoding="utf-8")
    t2 = p2.read_text(encoding="utf-8")
    assert "shared-25" in t1
    assert "shared-45" in t2
    assert "event-a" in t1 and "event-b" in t2
    assert t1 != t2
