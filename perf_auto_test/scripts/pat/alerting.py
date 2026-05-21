"""Per-process threshold state machine.

Lifecycle:

    OK ──value>threshold──> RISING ──sustained for sustain_sec──> ALERTING(fire)
     ▲                       │                                      │
     │ value≤threshold        │                                      ▼
     └───────────────────────┘                                  COOLDOWN
                                                                   │ cooldown_sec
     ▲─────────────────────────────────────────────────────────────┘

- RISING → OK if value drops below threshold before sustain elapses (single
  spikes do not fire).
- ALERTING is transient: tracker fires once and moves straight to COOLDOWN.
- COOLDOWN absorbs all breaches; on expiry, returns to OK (a fresh breach
  must start the sequence over).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class State(str, Enum):
    OK = "OK"
    RISING = "RISING"
    COOLDOWN = "COOLDOWN"


@dataclass(frozen=True)
class ThresholdConfig:
    metric: str        # e.g. "cpu_pct" or "mem_pss_mb"
    value: float       # the trip value
    sustain_sec: float
    cooldown_sec: float


@dataclass
class AlertEvent:
    metric: str
    triggered_at: float
    value_at_trigger: float
    duration_above_sec: float
    peak: float
    threshold_value: float
    sustain_sec: float
    cooldown_sec: float


class ThresholdTracker:
    def __init__(self, cfg: ThresholdConfig) -> None:
        self.cfg = cfg
        self._state: State = State.OK
        self._rising_since: Optional[float] = None
        self._peak: float = 0.0
        self._cooldown_until: float = 0.0

    @property
    def state(self) -> State:
        return self._state

    def feed(self, ts: float, value: float) -> Optional[AlertEvent]:
        # Cooldown expiry takes priority — we may transition mid-feed.
        if self._state is State.COOLDOWN and ts >= self._cooldown_until:
            self._state = State.OK
            self._peak = 0.0
            self._rising_since = None

        over = value > self.cfg.value

        if self._state is State.COOLDOWN:
            # Track peak for visibility, but never fire while cooling down.
            if over and value > self._peak:
                self._peak = value
            return None

        if self._state is State.OK:
            if not over:
                return None
            # Enter RISING and fall through so a sustain_sec=0 config can
            # fire on this same call.
            self._state = State.RISING
            self._rising_since = ts
            self._peak = value

        # RISING
        if not over:
            self._state = State.OK
            self._rising_since = None
            self._peak = 0.0
            return None

        if value > self._peak:
            self._peak = value
        rising_since = self._rising_since if self._rising_since is not None else ts
        duration = ts - rising_since
        if duration >= self.cfg.sustain_sec:
            event = AlertEvent(
                metric=self.cfg.metric,
                triggered_at=ts,
                value_at_trigger=value,
                duration_above_sec=duration,
                peak=self._peak,
                threshold_value=self.cfg.value,
                sustain_sec=self.cfg.sustain_sec,
                cooldown_sec=self.cfg.cooldown_sec,
            )
            self._state = State.COOLDOWN
            self._cooldown_until = ts + self.cfg.cooldown_sec
            self._rising_since = None
            self._peak = 0.0
            return event
        return None

    def reset(self) -> None:
        self._state = State.OK
        self._rising_since = None
        self._peak = 0.0
        self._cooldown_until = 0.0
