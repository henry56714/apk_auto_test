"""Observation fusion (spec 4.2 / S1-03).

Merges observations from different sources into *occurrences* so the same
physical failure is counted exactly once. Fusion key priority:

1. same fault marker (`fault_id`) or same source record ID;
2. device epoch + uid + pid + full device-time window;
3. process + crash fingerprint + restart/exit sequence;
4. low-confidence fallback: host observation time.

Occurrences keep `primary_source` + `supporting_sources` and an
`occurrence_count` so reports can show every source that saw the failure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .observations import Observation

log = logging.getLogger(__name__)

DEFAULT_DEVICE_TS_WINDOW_SEC = 10.0
DEFAULT_HOST_WINDOW_SEC = 5.0
OCCURRENCE_MAX_AGE_SEC = 600.0

# Full device timestamps: "YYYY-MM-DD HH:MM:SS[.fff]" or logcat "MM-DD HH:MM:SS.mmm".
_DEVICE_TS_FULL_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?")
_DEVICE_TS_SHORT_RE = re.compile(r"(\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d+))?")


def parse_device_ts_epoch(ts: Optional[str], year: Optional[int] = None) -> Optional[float]:
    """Parse a device timestamp into a (naive, device-local) epoch seconds.

    Unlike the old seconds-of-day helper this keeps the *full date*, so
    yesterday's and today's same-second records can never collide (T-L0-007).
    """
    if not ts:
        return None
    m = _DEVICE_TS_FULL_RE.search(ts)
    if m:
        date_part = f"{m.group(1)} {m.group(2)}"
        frac = m.group(3)
    else:
        m = _DEVICE_TS_SHORT_RE.search(ts)
        if not m:
            return None
        if year is None:
            year = 1970
        date_part = f"{year}-{m.group(1)}"
        frac = m.group(2)

    frac_s = float("0." + frac) if frac else 0.0
    try:
        import calendar
        from datetime import datetime

        dt = datetime.strptime(date_part, "%Y-%m-%d %H:%M:%S")
        return calendar.timegm(dt.utctimetuple()) + frac_s
    except ValueError:
        return None


def _device_time_close(a: Optional[float], b: Optional[float], window: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= window


@dataclass
class Occurrence:
    """A fused group of observations describing one physical failure."""

    key: str
    process: str
    pid: int
    type: str
    observations: List[Observation] = field(default_factory=list)
    primary_source: str = ""
    supporting_sources: List[str] = field(default_factory=list)
    occurrence_count: int = 1
    created_host_sec: float = 0.0
    device_event_time: Optional[str] = None
    fault_id: Optional[str] = None
    fingerprint: Optional[str] = None

    def add(self, obs: Observation) -> None:
        self.observations.append(obs)
        if self.primary_source != obs.source and obs.source not in self.supporting_sources:
            self.supporting_sources.append(obs.source)
        if not self.primary_source:
            self.primary_source = obs.source
        self.occurrence_count += 1

    @property
    def sources(self) -> List[str]:
        out = []
        if self.primary_source:
            out.append(self.primary_source)
        out.extend(self.supporting_sources)
        return out

    def to_dict(self) -> Dict:
        return {
            "key": self.key,
            "process": self.process,
            "pid": self.pid,
            "type": self.type,
            "primary_source": self.primary_source,
            "supporting_sources": list(self.supporting_sources),
            "occurrence_count": self.occurrence_count,
            "device_event_time": self.device_event_time,
            "fault_id": self.fault_id,
            "fingerprint": self.fingerprint,
            "observation_count": len(self.observations),
        }


def _default_fingerprint(obs: Observation) -> str:
    parts = [
        obs.type,
        obs.subtype or "",
        obs.extra.get("exception_class") or "",
        obs.extra.get("signal") or "",
    ]
    joined = "|".join(p for p in parts if p)
    return joined or f"{obs.process}:{obs.type}"


def fusion_key_for(
    obs: Observation,
    *,
    device_ts_window_sec: float = DEFAULT_DEVICE_TS_WINDOW_SEC,
    host_window_sec: float = DEFAULT_HOST_WINDOW_SEC,
    year: Optional[int] = None,
) -> Tuple[str, ...]:
    """Return the fusion key for an observation (priority order per spec).

    The fault-marker key still carries pid + a time bucket: repeated
    relaunches of the *same* fault (crash loop) land in different buckets and
    stay separate occurrences, while three sources reporting the same physical
    crash (same marker/pid/time) collapse to one.
    """
    window_sec = max(1.0, float(device_ts_window_sec))
    dev = parse_device_ts_epoch(obs.device_event_time, year)
    host_bucket = (
        int((obs.host_monotonic_sec or 0) // max(1.0, float(host_window_sec)))
        if obs.host_monotonic_sec is not None
        else 0
    )
    time_bucket = int(dev // window_sec) if dev is not None else host_bucket

    fp = obs.fingerprint or _default_fingerprint(obs)
    # 1. Fault marker (same action) + pid + time bucket + type. The type is
    # part of the key: a crash and its own process death share a fault id but
    # are different observations and must never collapse into one.
    if obs.fault_id:
        return ("fault", obs.fault_id, obs.pid or -1, time_bucket, obs.type, fp)
    # 2. Device epoch + uid + pid + process + type + device-time window +
    #    fingerprint. The fingerprint disambiguates two *different* crashes
    #    sharing a pid within the window (T-L0-006).
    if dev is not None and obs.pid:
        return (
            "device",
            obs.device_epoch,
            obs.uid or -1,
            obs.pid,
            obs.process,
            obs.type,
            int(dev // window_sec),
            fp,
        )
    # 3. process + pid + crash fingerprint (low-confidence fallback).
    return ("fingerprint", obs.device_epoch, obs.process, obs.type, fp, obs.pid or -1)


def _key_is_fingerprint(key: Tuple[str, ...]) -> bool:
    return bool(key) and key[0] == "fingerprint"


def _distinguishing_marker(obs: Observation) -> str:
    """Exception class / signal that identifies *which* crash this is.

    Subtypes like "crashed" are framework classifications, not signatures —
    they never distinguish one crash from another.
    """
    return obs.extra.get("exception_class") or obs.extra.get("signal") or ""


class FusionEngine:
    """Online fusion of observations into occurrences.

    `observe()` returns `(is_new, occurrence)`. Callers treat `is_new` as
    "create an incident"; duplicate observations attach to the existing
    occurrence (supporting source).
    """

    def __init__(
        self,
        *,
        device_ts_window_sec: float = DEFAULT_DEVICE_TS_WINDOW_SEC,
        host_window_sec: float = DEFAULT_HOST_WINDOW_SEC,
        max_age_sec: float = OCCURRENCE_MAX_AGE_SEC,
        year: Optional[int] = None,
    ) -> None:
        self.device_ts_window_sec = float(device_ts_window_sec)
        self.host_window_sec = float(host_window_sec)
        self._max_age = float(max_age_sec)
        self.year = year
        # key → Occurrence
        self._occurrences: Dict[Tuple[str, ...], Occurrence] = {}
        # base key (without fingerprint) → occurrences sharing the window
        self._base_index: Dict[Tuple[str, ...], List[Occurrence]] = {}

    def _gc(self, now_sec: float) -> None:
        if not self._occurrences:
            return
        stale = [
            key
            for key, occ in self._occurrences.items()
            if occ.created_host_sec and now_sec - occ.created_host_sec > self._max_age
        ]
        for key in stale:
            self._occurrences.pop(key, None)
        for base, occs in list(self._base_index.items()):
            kept = [
                o
                for o in occs
                if o.created_host_sec and now_sec - o.created_host_sec <= self._max_age
            ]
            if kept:
                self._base_index[base] = kept
            else:
                self._base_index.pop(base, None)

    def _compatible(self, occ: Occurrence, obs: Observation) -> bool:
        """Cross-source compatibility inside a shared time window.

        Two observations of the same physical crash may carry different
        fingerprints (logcat knows the exception class, ExitInfo only says
        "crashed"). They are compatible unless both sides name a *specific,
        different* crash signature.
        """
        markers = {_distinguishing_marker(o) for o in occ.observations if _distinguishing_marker(o)}
        mine = _distinguishing_marker(obs)
        if not markers or not mine:
            return True
        return mine in markers

    def observe(
        self,
        obs: Observation,
        now_sec: float,
    ) -> Tuple[bool, Occurrence]:
        """Fuse one observation. Returns `(is_new_occurrence, occurrence)`."""
        self._gc(now_sec)
        # 0. Exact replay check: the same source record delivered twice
        # (logcat reconnect tail replay) always fuses, whatever the time key.
        if obs.source_record_id:
            for existing in self._occurrences.values():
                if any(
                    o.source == obs.source and o.source_record_id == obs.source_record_id
                    for o in existing.observations
                ):
                    existing.add(obs)
                    return False, existing
        key = fusion_key_for(
            obs,
            device_ts_window_sec=self.device_ts_window_sec,
            host_window_sec=self.host_window_sec,
            year=self.year,
        )
        occ = self._occurrences.get(key)
        if occ is not None:
            occ.add(obs)
            return False, occ

        # Fault-tagged observations live in the fault key space; sources
        # without the marker (ExitInfo, DropBox) live in the device/fingerprint
        # space. Compute the no-marker key once and use it both for merge
        # attempts and for dual registration.
        untagged_key: Optional[Tuple[str, ...]] = None
        if key and key[0] == "fault":
            untagged = Observation(
                source=obs.source,
                source_record_id=obs.source_record_id,
                process=obs.process,
                pid=obs.pid,
                type=obs.type,
                subtype=obs.subtype,
                severity=obs.severity,
                expected=obs.expected,
                device_event_time=obs.device_event_time,
                host_monotonic_sec=obs.host_monotonic_sec,
                device_epoch=obs.device_epoch,
                uid=obs.uid,
                fingerprint=obs.fingerprint,
                extra=dict(obs.extra),
            )
            untagged_key = fusion_key_for(
                untagged,
                device_ts_window_sec=self.device_ts_window_sec,
                host_window_sec=self.host_window_sec,
                year=self.year,
            )
            alt_key = untagged_key
            alt = self._occurrences.get(alt_key)
            if alt is not None and self._compatible(alt, obs):
                alt.add(obs)
                return False, alt
            if alt_key and alt_key[0] in ("device",):
                for candidate in list(self._base_index.get(alt_key[:-1], ())):
                    if self._compatible(candidate, obs):
                        candidate.add(obs)
                        return False, candidate

        # Same time window but a different fingerprint: merge when the
        # signatures are compatible (e.g. one side lacks the exception class),
        # keep separate when both name different specific crashes.
        if key and key[0] in ("device", "fault"):
            base_key = key[:-1]
            for candidate in list(self._base_index.get(base_key, ())):
                if self._compatible(candidate, obs):
                    candidate.add(obs)
                    return False, candidate

        occ = Occurrence(
            key="|".join(str(p) for p in key),
            process=obs.process,
            pid=obs.pid,
            type=obs.type,
            primary_source=obs.source,
            created_host_sec=now_sec,
            device_event_time=obs.device_event_time,
            fault_id=obs.fault_id,
            fingerprint=obs.fingerprint or _default_fingerprint(obs),
        )
        occ.observations.append(obs)
        self._occurrences[key] = occ
        if key and key[0] in ("device", "fault"):
            self._base_index.setdefault(key[:-1], []).append(occ)
        # Fault-key occurrences must also be reachable via the untagged
        # device key so marker-less sources (ExitInfo/DropBox) can find them.
        if key and key[0] == "fault" and untagged_key is not None:
            if untagged_key[0] in ("device",):
                self._base_index.setdefault(untagged_key[:-1], []).append(occ)
        return True, occ

    def find_any_window_occurrence(self, obs: Observation) -> Optional[Occurrence]:
        """Any occurrence of the same process+pid within the device-time
        window, regardless of type. Used when the AM classified a death
        differently from logcat (e.g. `excessive_resource_usage` for a crash
        while cached): the record still corroborates the crash."""
        dev = parse_device_ts_epoch(obs.device_event_time, self.year)
        for occ in self._occurrences.values():
            if occ.process != obs.process or occ.pid != obs.pid or occ.pid == 0:
                continue
            occ_dev = parse_device_ts_epoch(occ.device_event_time, self.year)
            if dev is None or occ_dev is None:
                continue
            if abs(dev - occ_dev) <= self.device_ts_window_sec * 2:
                return occ
        return None

    def find_compatible(self, obs: Observation, type_candidates) -> Optional[Occurrence]:
        """Find an occurrence for the same pid/time window whose type is one
        of `type_candidates` and whose signature is compatible.

        Used for ExitInfo `crashed` records that cannot distinguish a Java
        from a native crash: they attach to whichever crash occurrence the
        window already holds instead of fabricating a second incident.
        """
        for candidate_type in type_candidates:
            probe = Observation(
                source=obs.source,
                source_record_id=obs.source_record_id,
                process=obs.process,
                pid=obs.pid,
                type=candidate_type,
                subtype=obs.subtype,
                severity=obs.severity,
                expected=obs.expected,
                device_event_time=obs.device_event_time,
                host_monotonic_sec=obs.host_monotonic_sec,
                device_epoch=obs.device_epoch,
                uid=obs.uid,
                fingerprint=obs.fingerprint,
                extra=dict(obs.extra),
            )
            key = fusion_key_for(
                probe,
                device_ts_window_sec=self.device_ts_window_sec,
                host_window_sec=self.host_window_sec,
                year=self.year,
            )
            occ = self._occurrences.get(key)
            if occ is not None and self._compatible(occ, obs):
                return occ
            if key and key[0] in ("device",):
                for candidate in list(self._base_index.get(key[:-1], ())):
                    if self._compatible(candidate, obs):
                        return candidate
        return None

    def occurrences(self) -> List[Occurrence]:
        return list(self._occurrences.values())

    def clear(self) -> None:
        self._occurrences.clear()
        self._base_index.clear()
