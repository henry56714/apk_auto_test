"""Device-level health monitoring (reboot / ADB gaps / boot progress)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ..adb import Adb, AdbError

log = logging.getLogger(__name__)


@dataclass
class DeviceEvent:
    event_type: str
    started_at: float
    ended_at: Optional[float] = None
    detail: str = ""

    def to_dict(self) -> Dict:
        return {
            "event_type": self.event_type,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "detail": self.detail,
        }


@dataclass
class DeviceSnapshot:
    boot_id: str = ""
    boot_completed: bool = True
    uptime_sec: float = 0.0
    state: str = "device"


class DeviceHealthMonitor:
    def __init__(
        self,
        adb: Adb,
        *,
        interval_sec: float = 5.0,
        now_fn: Callable[[], float] = time.time,
        query_fn: Optional[Callable[[], DeviceSnapshot]] = None,
        on_gap_started: Optional[Callable[[str], None]] = None,
        on_recovered: Optional[Callable[[], None]] = None,
    ) -> None:
        self.adb = adb
        self.interval_sec = float(interval_sec)
        self._now = now_fn
        self._query = query_fn or self._default_query
        self._on_gap_started = on_gap_started
        self._on_recovered = on_recovered
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._events: List[DeviceEvent] = []
        self._lock = threading.Lock()
        self._last: Optional[DeviceSnapshot] = None
        self._active_gap: Optional[DeviceEvent] = None
        self.pid_epoch = 0

    def _default_query(self) -> DeviceSnapshot:
        try:
            boot_id = self.adb.shell(
                "getprop ro.boot.boot_id", check=False, timeout=3.0,
            ).stdout.strip()
            boot_completed = self.adb.shell(
                "getprop sys.boot_completed", check=False, timeout=3.0,
            ).stdout.strip() == "1"
            uptime = self.adb.shell(
                "cat /proc/uptime", check=False, timeout=3.0,
            ).stdout.split()[0]
            state = "device"
            try:
                uptime_sec = float(uptime)
            except (ValueError, IndexError):
                uptime_sec = 0.0
        except (AdbError, IndexError, ValueError):
            return DeviceSnapshot(state="offline")
        return DeviceSnapshot(
            boot_id=boot_id, boot_completed=boot_completed,
            uptime_sec=uptime_sec, state=state,
        )

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="device-health",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def events(self) -> List[DeviceEvent]:
        with self._lock:
            return [DeviceEvent(**e.__dict__) for e in self._events]

    def _record(self, event: DeviceEvent) -> None:
        with self._lock:
            self._events.append(event)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._now()
            snap = self._query()
            last = self._last
            self._last = snap

            if last is not None:
                if last.state in ("device", "") and snap.state != "device":
                    self._begin_gap(snap.state, now)
                elif last.state != "device" and snap.state == "device":
                    self._end_gap(now)
                elif last.state == "device" and snap.state == "device" \
                        and last.boot_id and snap.boot_id \
                        and last.boot_id != snap.boot_id:
                    self._begin_gap("reboot", now)
                    self._end_gap(now, detail=f"boot_id {last.boot_id} -> {snap.boot_id}")
                    self.pid_epoch += 1

            if self._stop.wait(self.interval_sec):
                break

    def _begin_gap(self, kind: str, now: float) -> None:
        if self._active_gap is not None:
            return
        self._active_gap = DeviceEvent(
            event_type=kind, started_at=now, detail="started",
        )
        self._record(self._active_gap)
        if self._on_gap_started is not None:
            self._on_gap_started(kind)

    def _end_gap(self, now: float, detail: str = "recovered") -> None:
        if self._active_gap is None:
            return
        self._active_gap.ended_at = now
        self._active_gap.detail = detail
        self._record(DeviceEvent(
            event_type="recovered", started_at=now, detail=detail,
        ))
        self._active_gap = None
        if self._on_recovered is not None:
            self._on_recovered()
