"""Low-frequency resource-exhaustion pre-warning (FD/thread/memory)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ..adb import Adb, AdbError

log = logging.getLogger(__name__)


@dataclass
class ResourceSample:
    pid: int
    ts: float
    fd_count: Optional[int] = None
    thread_count: Optional[int] = None
    rss_kb: Optional[int] = None
    binder_failures: int = 0

    def to_dict(self) -> Dict:
        return {
            "pid": self.pid,
            "ts": self.ts,
            "fd_count": self.fd_count,
            "thread_count": self.thread_count,
            "rss_kb": self.rss_kb,
            "binder_failures": self.binder_failures,
        }


@dataclass
class RiskEvent:
    pid: int
    ts: float
    metric: str
    value: float
    baseline: float
    message: str

    def to_dict(self) -> Dict:
        return {
            "pid": self.pid,
            "ts": self.ts,
            "metric": self.metric,
            "value": self.value,
            "baseline": self.baseline,
            "message": self.message,
        }


class ResourceRiskDetector:
    """Hysteresis detector: one event per sustained growth episode."""

    def __init__(
        self,
        *,
        fd_growth_threshold: int = 200,
        thread_growth_threshold: int = 50,
        recovery_ratio: float = 0.5,
    ) -> None:
        self.fd_threshold = int(fd_growth_threshold)
        self.thread_threshold = int(thread_growth_threshold)
        self.recovery_ratio = float(recovery_ratio)
        self._baselines: Dict[str, float] = {}
        self._armed: Dict[str, bool] = {}

    def observe(self, sample: ResourceSample) -> Optional[RiskEvent]:
        for metric, value, threshold in (
            ("fd_count", sample.fd_count, self.fd_threshold),
            ("thread_count", sample.thread_count, self.thread_threshold),
        ):
            if value is None:
                continue
            key = f"{sample.pid}:{metric}"
            if key not in self._baselines:
                self._baselines[key] = float(value)
                self._armed[key] = False
                continue
            baseline = self._baselines[key]
            growth = value - baseline
            if self._armed.get(key):
                if value <= baseline + threshold * self.recovery_ratio:
                    self._armed[key] = False
                continue
            if growth >= threshold:
                self._armed[key] = True
                return RiskEvent(
                    pid=sample.pid,
                    ts=sample.ts,
                    metric=metric,
                    value=float(value),
                    baseline=baseline,
                    message=f"{metric} grew from {baseline:.0f} to {value:.0f}",
                )
        return None


class ResourceRiskMonitor:
    def __init__(
        self,
        adb: Adb,
        package: str,
        *,
        interval_sec: float = 30.0,
        detector: Optional[ResourceRiskDetector] = None,
        sample_fn: Optional[Callable[[], List[ResourceSample]]] = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.adb = adb
        self.package = package
        self.interval_sec = float(interval_sec)
        self._detector = detector or ResourceRiskDetector()
        self._sample_fn = sample_fn or self._default_sample
        self._now = now_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._samples: List[Dict] = []
        self._events: List[RiskEvent] = []
        self._lock = threading.Lock()
        self.status = "ok"

    def _default_sample(self) -> List[ResourceSample]:
        try:
            r = self.adb.shell(
                f"pidof {self.package}", check=False, timeout=5.0,
            )
        except AdbError:
            return []
        if r.returncode != 0:
            return []
        out = []
        for pid in r.stdout.split():
            fd = self._read_int(f"/proc/{pid}/fd", "ls")
            threads = self._read_int(f"/proc/{pid}/task", "ls")
            out.append(ResourceSample(
                pid=int(pid),
                ts=self._now(),
                fd_count=fd,
                thread_count=threads,
            ))
        return out

    def _read_int(self, path: str, kind: str) -> Optional[int]:
        try:
            if kind == "ls":
                r = self.adb.shell(
                    f"ls {path} 2>/dev/null | wc -l", check=False, timeout=5.0,
                )
            else:
                return None
        except AdbError:
            return None
        if r.returncode != 0:
            return None
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="resource-risk",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                samples = self._sample_fn()
            except Exception:
                self.status = "capability_unavailable"
                samples = []
            with self._lock:
                self._samples.extend(s.to_dict() for s in samples)
                for s in samples:
                    event = self._detector.observe(s)
                    if event is not None:
                        self._events.append(event)
            if self._stop.wait(self.interval_sec):
                break

    def events(self) -> List[Dict]:
        with self._lock:
            return [e.to_dict() for e in self._events]

    def samples(self) -> List[Dict]:
        with self._lock:
            return list(self._samples)


def correlate_resource_risk(
    incidents: List[Dict],
    risk_events: List[Dict],
    *,
    window_sec: float = 60.0,
) -> None:
    """Attach a nearby risk event to a crash/ANR incident as supporting evidence."""
    for event in risk_events:
        for inc in incidents:
            if inc.get("pid") != event.get("pid"):
                continue
            try:
                from datetime import datetime
                inc_dt = datetime.fromisoformat(
                    (inc.get("triggered_at") or "").replace("Z", "+00:00")
                )
                if inc_dt.tzinfo is None:
                    from datetime import timezone
                    inc_dt = inc_dt.replace(tzinfo=timezone.utc)
                inc_ts = inc_dt.timestamp()
            except (ValueError, TypeError):
                continue
            if abs(inc_ts - float(event["ts"])) <= window_sec:
                evidence = inc.setdefault("evidence", {})
                evidence["resource_risk"] = event
                break
