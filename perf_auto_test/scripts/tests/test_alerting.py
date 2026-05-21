"""Threshold state machine tests."""

from __future__ import annotations

import pytest

from perf_auto_test.alerting import State, ThresholdConfig, ThresholdTracker


def _cfg(value: float = 80.0, sustain: float = 60.0, cooldown: float = 300.0) -> ThresholdConfig:
    return ThresholdConfig(metric="cpu_pct", value=value,
                           sustain_sec=sustain, cooldown_sec=cooldown)


class TestNoFire:
    def test_steady_below_threshold_never_fires(self):
        t = ThresholdTracker(_cfg())
        for i in range(100):
            assert t.feed(float(i), 50.0) is None
        assert t.state is State.OK

    def test_brief_spike_does_not_fire(self):
        """A breach that returns to OK before sustain elapses must NOT fire."""
        t = ThresholdTracker(_cfg(sustain=60))
        assert t.feed(0.0, 90.0) is None    # enter RISING at t=0
        assert t.state is State.RISING
        assert t.feed(30.0, 90.0) is None   # still under sustain, RISING
        assert t.feed(31.0, 50.0) is None   # back to OK
        assert t.state is State.OK


class TestFire:
    def test_sustained_breach_fires_once(self):
        t = ThresholdTracker(_cfg(value=80, sustain=60, cooldown=300))
        assert t.feed(0.0, 90.0) is None     # RISING starts
        assert t.feed(30.0, 95.0) is None    # not yet
        assert t.feed(59.9, 95.0) is None    # almost
        e = t.feed(60.0, 95.0)               # exactly sustain — fire
        assert e is not None
        assert e.metric == "cpu_pct"
        assert e.value_at_trigger == 95.0
        assert e.duration_above_sec == pytest.approx(60.0)
        assert e.peak == 95.0
        assert e.threshold_value == 80
        assert t.state is State.COOLDOWN

    def test_peak_tracked_during_rising(self):
        t = ThresholdTracker(_cfg(value=80, sustain=10))
        t.feed(0.0, 90.0)
        t.feed(5.0, 98.0)        # peak
        t.feed(8.0, 85.0)
        e = t.feed(10.0, 82.0)
        assert e is not None
        assert e.peak == 98.0
        assert e.value_at_trigger == 82.0


class TestCooldown:
    def test_breach_during_cooldown_does_not_fire(self):
        t = ThresholdTracker(_cfg(value=80, sustain=10, cooldown=100))
        t.feed(0.0, 90.0)
        e1 = t.feed(10.0, 95.0)
        assert e1 is not None
        # Now in COOLDOWN until t=110.
        assert t.feed(20.0, 100.0) is None
        assert t.feed(50.0, 99.0) is None
        assert t.feed(109.9, 99.0) is None
        assert t.state is State.COOLDOWN

    def test_cooldown_expiry_returns_to_ok(self):
        t = ThresholdTracker(_cfg(value=80, sustain=10, cooldown=100))
        t.feed(0.0, 90.0)
        t.feed(10.0, 95.0)
        # After cooldown_until=110, a value <= threshold should land in OK.
        assert t.feed(115.0, 50.0) is None
        assert t.state is State.OK

    def test_can_fire_again_after_cooldown(self):
        t = ThresholdTracker(_cfg(value=80, sustain=10, cooldown=100))
        t.feed(0.0, 90.0)
        e1 = t.feed(10.0, 95.0)
        assert e1 is not None
        # Cooldown expires at 110, then start a new RISING.
        t.feed(115.0, 90.0)             # OK -> RISING
        e2 = t.feed(125.0, 95.0)         # 10s sustain elapsed
        assert e2 is not None
        assert e2.triggered_at == 125.0


class TestEdgeCases:
    def test_exactly_at_threshold_is_not_over(self):
        """Threshold semantics: value > threshold (strict)."""
        t = ThresholdTracker(_cfg(value=80))
        # value == threshold should NOT enter RISING.
        for i in range(200):
            assert t.feed(float(i), 80.0) is None
        assert t.state is State.OK

    def test_mem_metric_works_the_same(self):
        """Tracker is value-agnostic; mem_pss_mb works identically."""
        t = ThresholdTracker(ThresholdConfig("mem_pss_mb", 500.0, 30.0, 60.0))
        t.feed(0.0, 600.0)
        e = t.feed(30.0, 700.0)
        assert e is not None
        assert e.metric == "mem_pss_mb"

    def test_reset_clears_state(self):
        t = ThresholdTracker(_cfg())
        t.feed(0.0, 90.0)
        assert t.state is State.RISING
        t.reset()
        assert t.state is State.OK
