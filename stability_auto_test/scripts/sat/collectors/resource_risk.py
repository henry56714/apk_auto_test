"""Low-frequency resource-exhaustion pre-warning (FD/thread/RSS).

Spec S2-05 / IMP-11 semantics:

- every metric sample carries `value + source + capability + error`; a
  permission denial is `unavailable` with the error recorded — it is NEVER
  silently written as a real 0;
- baselines are keyed by (process, pid, process start time) so PID reuse
  after a restart starts a fresh baseline (process epoch);
- a single sample may raise *multiple* risks (FD and thread growing together
  are not first-match lost).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..adb import Adb, AdbError

log = logging.getLogger(__name__)

CAPABILITY_OK = "ok"
CAPABILITY_UNAVAILABLE = "unavailable"


@dataclass
class ResourceSample:
    pid: int
    ts: float
    fd_count: Optional[int] = None
    thread_count: Optional[int] = None
    rss_kb: Optional[int] = None
    binder_failures: int = 0
    process_start_time: Optional[str] = None
    # metric → error string; a metric listed here was *not* sampled
    # successfully and must never be treated as 0.
    errors: Dict[str, str] = field(default_factory=dict)

    def capability(self, metric: str) -> str:
        value = getattr(self, metric, None)
        if value is not None:
            return CAPABILITY_OK
        if metric in self.errors:
            return CAPABILITY_UNAVAILABLE
        return CAPABILITY_UNAVAILABLE

    def to_dict(self) -> Dict:
        return {
            "pid": self.pid,
            "ts": self.ts,
            "fd_count": self.fd_count,
            "thread_count": self.thread_count,
            "rss_kb": self.rss_kb,
            "binder_failures": self.binder_failures,
            "process_start_time": self.process_start_time,
            "errors": dict(self.errors),
            "capabilities": {
                metric: self.capability(metric) for metric in ("fd_count", "thread_count", "rss_kb")
            },
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
        rss_growth_threshold_kb: int = 300 * 1024,
        recovery_ratio: float = 0.5,
    ) -> None:
        self.fd_threshold = int(fd_growth_threshold)
        self.thread_threshold = int(thread_growth_threshold)
        self.rss_threshold = int(rss_growth_threshold_kb)
        self.recovery_ratio = float(recovery_ratio)
        # (pid, metric, process_start_time) → baseline
        self._baselines: Dict[tuple, float] = {}
        self._armed: Dict[tuple, bool] = {}

    @staticmethod
    def _key(sample: ResourceSample, metric: str) -> tuple:
        return (sample.pid, metric, sample.process_start_time or "?")

    def _observe_metric(
        self,
        sample: ResourceSample,
        metric: str,
        threshold: float,
    ) -> Optional[RiskEvent]:
        value = getattr(sample, metric, None)
        if value is None:
            return None  # unavailable ≠ 0 (T-L1-023)
        key = self._key(sample, metric)
        if key not in self._baselines:
            self._baselines[key] = float(value)
            self._armed[key] = False
            return None
        baseline = self._baselines[key]
        growth = value - baseline
        if self._armed.get(key):
            if value <= baseline + threshold * self.recovery_ratio:
                self._armed[key] = False
            return None
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

    def observe_all(self, sample: ResourceSample) -> List[RiskEvent]:
        """Return ALL risks raised by this sample (no first-match loss)."""
        out: List[RiskEvent] = []
        for metric, threshold in (
            ("fd_count", self.fd_threshold),
            ("thread_count", self.thread_threshold),
            ("rss_kb", self.rss_threshold),
        ):
            event = self._observe_metric(sample, metric, threshold)
            if event is not None:
                out.append(event)
        return out

    def observe(self, sample: ResourceSample) -> Optional[RiskEvent]:
        """First-risk shortcut (backward-compatible API)."""
        events = self.observe_all(sample)
        return events[0] if events else None


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
            r = self.adb.shell(f"pidof {self.package}", check=False, timeout=5.0)
        except AdbError:
            return []
        if r.returncode != 0:
            return []
        out = []
        for pid_s in r.stdout.split():
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            sample = ResourceSample(pid=pid, ts=self._now())
            # Process start time (process epoch for baselines).
            sample.process_start_time = self._read_proc_field(pid, "stat", index=22)
            # FD count — probe permission FIRST so a denial is `unavailable`,
            # never a fake 0 (T-L1-023).
            probe = self.adb.shell(
                f"ls /proc/{pid}/fd >/dev/null 2>&1; echo $?",
                check=False,
                timeout=5.0,
            )
            if probe.returncode != 0 or probe.stdout.strip() != "0":
                sample.errors["fd_count"] = "cannot list /proc/%d/fd: rc=%s out=%r" % (
                    pid,
                    probe.returncode,
                    probe.stdout.strip(),
                )
            else:
                sample.fd_count = self._read_count(f"/proc/{pid}/fd")
                if sample.fd_count is None:
                    sample.errors["fd_count"] = "fd listing failed"
            threads = self._read_count(f"/proc/{pid}/task")
            if threads is None:
                sample.errors["thread_count"] = "task listing failed"
            else:
                sample.thread_count = threads
            # RSS from /proc/<pid>/status VmRSS (kB).
            rss = self._read_vmrss(pid)
            if rss is None:
                sample.errors["rss_kb"] = "VmRSS not readable"
            else:
                sample.rss_kb = rss
            out.append(sample)
        return out

    def _read_count(self, path: str) -> Optional[int]:
        try:
            r = self.adb.shell(f"ls {path} 2>/dev/null | wc -l", check=False, timeout=5.0)
        except AdbError:
            return None
        if r.returncode != 0:
            return None
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None

    def _read_proc_field(self, pid: int, path: str, *, index: int) -> Optional[str]:
        try:
            r = self.adb.shell(f"cat /proc/{pid}/{path}", check=False, timeout=5.0)
        except AdbError:
            return None
        if r.returncode != 0:
            return None
        text = r.stdout.strip()
        if path == "stat":
            # comm is parenthesised and may contain spaces: split after ")".
            rest = text.rsplit(")", 1)
            if len(rest) != 2:
                return None
            fields = rest[1].split()
            # Field 22 (starttime) is index 20 after the leading pid+comm.
            if len(fields) > 20:
                return fields[20]
            return None
        fields = text.split()
        if len(fields) > index:
            return fields[index]
        return None

    def _read_vmrss(self, pid: int) -> Optional[int]:
        try:
            r = self.adb.shell(f"cat /proc/{pid}/status", check=False, timeout=5.0)
        except AdbError:
            return None
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    return int(line.split()[1])
                except (IndexError, ValueError):
                    return None
        return None

    def start(self) -> None:
        import queue

        self._external_samples: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="resource-risk",
        )
        self._thread.start()

    def submit_external_sample(self, sample: ResourceSample) -> None:
        """Accept an app self-reported sample (SAT_RESOURCE_SAMPLE marker)."""
        queue_ = getattr(self, "_external_samples", None)
        if queue_ is None:
            return
        queue_.put_nowait(sample)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _drain_external(self) -> List[ResourceSample]:
        queue_ = getattr(self, "_external_samples", None)
        if queue_ is None:
            return []
        out: List[ResourceSample] = []
        while True:
            try:
                out.append(queue_.get_nowait())
            except Exception:
                break
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                samples = self._sample_fn()
            except Exception:
                self.status = "capability_unavailable"
                samples = []
            samples = list(samples) + self._drain_external()
            with self._lock:
                self._samples.extend(s.to_dict() for s in samples)
                for s in samples:
                    for event in self._detector.observe_all(s):
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
