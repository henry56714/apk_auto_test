"""Unified observation model (spec 4.1 / S1-03).

Every detection source (logcat, ExitInfo, dropbox, lifecycle, watcher, ...)
emits an `Observation` — a raw fact — *before* any Incident exists. The fusion
layer (`sat/fusion.py`) merges observations into occurrences/incidents, so a
single physical failure seen by three sources is counted exactly once while
every source stays traceable (`primary_source` / `supporting_sources`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Sources.
SOURCE_LOGCAT = "logcat"
SOURCE_EXIT_INFO = "exit_info"
SOURCE_DROPBOX = "dropbox"
SOURCE_LIFECYCLE = "lifecycle"
SOURCE_WATCHER = "watcher"
SOURCE_DEVICE_HEALTH = "device_health"
SOURCE_RESOURCE = "resource"

# Types.
TYPE_JAVA_CRASH = "java_crash"
TYPE_NATIVE_CRASH = "native_crash"
TYPE_ANR = "anr"
TYPE_PROCESS_EXIT = "process_exit"

# Severities.
SEVERITY_FATAL = "fatal"
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# Confidences.
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

ALL_SOURCES = (
    SOURCE_LOGCAT,
    SOURCE_EXIT_INFO,
    SOURCE_DROPBOX,
    SOURCE_LIFECYCLE,
    SOURCE_WATCHER,
    SOURCE_DEVICE_HEALTH,
    SOURCE_RESOURCE,
)


@dataclass
class Observation:
    """One raw stability fact from one source."""

    source: str
    process: str
    pid: int
    type: str
    severity: str = SEVERITY_ERROR
    package: str = ""
    subtype: Optional[str] = None
    confidence: str = CONFIDENCE_HIGH
    expected: bool = False
    device_event_time: Optional[str] = None
    host_received_at: Optional[str] = None
    host_monotonic_sec: Optional[float] = None
    device_epoch: int = 1
    uid: Optional[int] = None
    source_record_id: Optional[str] = None
    fault_id: Optional[str] = None
    fingerprint: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def key_fields(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "source": self.source,
            "source_record_id": self.source_record_id,
            "device_epoch": self.device_epoch,
            "device_event_time": self.device_event_time,
            "host_received_at": self.host_received_at,
            "host_monotonic_sec": self.host_monotonic_sec,
            "package": self.package,
            "process": self.process,
            "pid": self.pid,
            "uid": self.uid,
            "type": self.type,
            "subtype": self.subtype,
            "severity": self.severity,
            "expected": self.expected,
            "confidence": self.confidence,
            "fault_id": self.fault_id,
            "fingerprint": self.fingerprint,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {**self.key_fields(), **(self.extra or {})}


def observation_from_event(
    event,
    *,
    device_epoch: int,
    now_iso: str,
    now_sec: float,
    run_id: Optional[str] = None,
) -> Observation:
    """Convert a logcat-derived `StabilityEvent` into an Observation."""
    from .detection import (
        EVENT_ANR,
        EVENT_JAVA_CRASH,
        EVENT_NATIVE_CRASH,
        EVENT_PROCESS_DEATH,
    )

    event_type = event.event_type
    if event_type == EVENT_PROCESS_DEATH:
        obs_type = TYPE_PROCESS_EXIT
    else:
        obs_type = event_type
    severity = event.severity if event_type in (EVENT_ANR,) else SEVERITY_FATAL
    return Observation(
        observation_id=getattr(event, "observation_id", "") or str(uuid.uuid4()),
        source=event.source or SOURCE_LOGCAT,
        source_record_id=event.event_id or None,
        process=event.process,
        pid=event.pid,
        type=obs_type,
        subtype=(
            EVENT_JAVA_CRASH
            if event_type == EVENT_JAVA_CRASH
            else EVENT_NATIVE_CRASH
            if event_type == EVENT_NATIVE_CRASH
            else EVENT_ANR
            if event_type == EVENT_ANR
            else None
        ),
        severity=severity,
        expected=False,
        device_event_time=event.device_ts,
        host_received_at=now_iso,
        host_monotonic_sec=now_sec,
        device_epoch=device_epoch,
        fault_id=getattr(event, "fault_id", None),
        extra={
            "exception_class": getattr(event, "exception_class", None),
            "signal": getattr(event, "signal", None),
            "fault_addr": getattr(event, "fault_addr", None),
            "reason": getattr(event, "reason", None),
            "summary": getattr(event, "summary", ""),
        },
    )
