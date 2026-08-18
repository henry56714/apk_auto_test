"""Observation fusion semantics (spec S1-03 / T-L0-005 .. T-L0-008)."""

from __future__ import annotations

from sat.fusion import FusionEngine, parse_device_ts_epoch
from sat.observations import (
    SOURCE_DROPBOX,
    SOURCE_EXIT_INFO,
    SOURCE_LOGCAT,
    TYPE_JAVA_CRASH,
    Observation,
)


def _obs(
    source: str,
    pid: int,
    device_ts: str,
    *,
    epoch: int = 1,
    fingerprint: str = "",
    exception_class: str = "RuntimeException",
) -> Observation:
    fp = fingerprint or f"java_crash|{exception_class}|"
    return Observation(
        source=source,
        source_record_id=f"{source}-rec-{pid}-{device_ts}",
        process="com.example.app",
        pid=pid,
        type=TYPE_JAVA_CRASH,
        subtype="runtime_exception",
        severity="fatal",
        device_event_time=device_ts,
        host_received_at="2026-08-13T10:00:01+00:00",
        host_monotonic_sec=100.0,
        device_epoch=epoch,
        fingerprint=fp,
        extra={"exception_class": exception_class},
    )


# ── T-L0-005: one crash seen by three sources counts once ────────────────────


def test_three_sources_one_occurrence():
    eng = FusionEngine()
    a = _obs(SOURCE_LOGCAT, 1234, "2026-08-13 10:00:00.100")
    b = _obs(SOURCE_DROPBOX, 1234, "2026-08-13 10:00:00.400")
    c = _obs(SOURCE_EXIT_INFO, 1234, "2026-08-13 10:00:00.900")
    for i, obs in enumerate([a, b, c]):
        is_new, occ = eng.observe(obs, now_sec=100.0 + i)
        assert is_new if i == 0 else not is_new
    occurrences = eng.occurrences()
    assert len(occurrences) == 1
    occ = occurrences[0]
    assert occ.primary_source == SOURCE_LOGCAT
    assert set(occ.supporting_sources) == {SOURCE_DROPBOX, SOURCE_EXIT_INFO}
    assert set(occ.sources) == {SOURCE_LOGCAT, SOURCE_DROPBOX, SOURCE_EXIT_INFO}
    assert len(occ.observations) == 3


def test_same_fault_marker_fuses_cross_source():
    eng = FusionEngine()
    a = _obs(SOURCE_LOGCAT, 1, "2026-08-13 10:00:00.100")
    a.fault_id = "java-main-001"
    b = _obs(SOURCE_EXIT_INFO, 1, "2026-08-13 10:00:00.800")
    b.fault_id = "java-main-001"
    assert eng.observe(a, now_sec=50.0)[0] is True
    assert eng.observe(b, now_sec=51.0)[0] is False
    assert len(eng.occurrences()) == 1


def test_same_fault_marker_different_time_stays_separate():
    """Crash loop: same fault id, relaunched much later => separate occurrences."""
    eng = FusionEngine()
    a = _obs(SOURCE_LOGCAT, 1, "2026-08-13 10:00:00.100")
    a.fault_id = "startup-crash"
    b = _obs(SOURCE_LOGCAT, 2, "2026-08-13 10:01:00.100")
    b.fault_id = "startup-crash"
    assert eng.observe(a, now_sec=50.0)[0] is True
    assert eng.observe(b, now_sec=120.0)[0] is True
    assert len(eng.occurrences()) == 2


# ── T-L0-006: two different crashes, same pid/process, close in time ─────────


def test_two_different_crashes_same_pid_not_merged():
    eng = FusionEngine()
    a = _obs(SOURCE_LOGCAT, 42, "2026-08-13 10:00:00.100")
    b = _obs(
        SOURCE_LOGCAT,
        42,
        "2026-08-13 10:00:05.100",
        exception_class="NullPointerException",
    )
    assert eng.observe(a, now_sec=100.0)[0] is True
    assert eng.observe(b, now_sec=105.0)[0] is True
    assert len(eng.occurrences()) == 2


# ── T-L0-007: midnight crossover with full dates ──────────────────────────────


def test_midnight_crossover_not_merged_with_previous_day():
    eng = FusionEngine()
    yesterday = _obs(SOURCE_LOGCAT, 7, "2026-08-12 23:59:59.800")
    today = _obs(SOURCE_LOGCAT, 7, "2026-08-13 00:00:01.000")
    # ~1.2 s apart on the device clock, but on different days: the full-date
    # bucket must keep them separate (seconds-of-day math would merge them).
    assert eng.observe(yesterday, now_sec=100.0)[0] is True
    assert eng.observe(today, now_sec=101.0)[0] is True
    assert len(eng.occurrences()) == 2


def test_parse_device_ts_full_date_keeps_date():
    a = parse_device_ts_epoch("2026-08-13 00:00:01.000")
    b = parse_device_ts_epoch("2026-08-12 23:59:59.800")
    assert a is not None and b is not None
    assert abs(a - b - 1.2) < 0.01


def test_parse_device_ts_logcat_short_format_uses_year():
    a = parse_device_ts_epoch("08-13 00:00:01.000", year=2026)
    b = parse_device_ts_epoch("08-12 23:59:59.800", year=2026)
    assert abs(a - b - 1.2) < 0.01


# ── T-L0-008: reboot + PID reuse stays separate ───────────────────────────────


def test_reboot_pid_reuse_not_fused():
    eng = FusionEngine()
    before = _obs(SOURCE_LOGCAT, 555, "2026-08-13 10:00:00.100", epoch=1)
    after = _obs(SOURCE_EXIT_INFO, 555, "2026-08-13 10:05:00.100", epoch=2)
    assert eng.observe(before, now_sec=100.0)[0] is True
    assert eng.observe(after, now_sec=400.0)[0] is True
    assert len(eng.occurrences()) == 2


def test_same_epoch_same_pid_near_time_fuses():
    eng = FusionEngine()
    a = _obs(SOURCE_LOGCAT, 555, "2026-08-13 10:00:00.100", epoch=1)
    b = _obs(SOURCE_DROPBOX, 555, "2026-08-13 10:00:02.100", epoch=1)
    assert eng.observe(a, now_sec=100.0)[0] is True
    assert eng.observe(b, now_sec=102.0)[0] is False
