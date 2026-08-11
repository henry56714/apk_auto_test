from __future__ import annotations

import time

from sat.collectors.resource_risk import (
    ResourceRiskDetector,
    ResourceRiskMonitor,
    ResourceSample,
)


def _sample(pid: int, fd: int, threads: int = 10, ts: float = 0.0):
    return ResourceSample(pid=pid, ts=ts, fd_count=fd, thread_count=threads)


def test_hysteresis_triggers_once_and_rearms():
    detector = ResourceRiskDetector(fd_growth_threshold=200)
    events = []
    for fd in (100, 100, 300, 320, 320, 150, 400):
        ev = detector.observe(_sample(1, fd, ts=float(fd)))
        if ev:
            events.append(ev)
    assert len(events) == 2
    assert events[0].metric == "fd_count"
    assert events[0].value == 300
    assert events[1].value == 400


def test_monitor_capability_unavailable_is_not_parser_failure():
    def boom():
        raise RuntimeError("no permission")

    monitor = ResourceRiskMonitor(
        None,  # type: ignore[arg-type]
        "com.example.app",
        interval_sec=0.01,
        sample_fn=boom,
    )
    monitor.start()
    time.sleep(0.05)
    monitor.stop()
    assert monitor.status == "capability_unavailable"
    assert monitor.events() == []


def test_detector_overhead_is_small():
    detector = ResourceRiskDetector()
    start = time.monotonic()
    for i in range(10000):
        detector.observe(_sample(1, 100 + (i % 3), ts=float(i)))
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
