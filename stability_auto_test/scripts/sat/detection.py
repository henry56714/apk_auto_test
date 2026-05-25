"""Stability-event detection from logcat / dropbox text.

Two pieces:

1. `LogcatLineParser` — line-by-line state machine that consumes logcat
   `threadtime` lines and yields `StabilityEvent`s. It accumulates the
   multi-line blocks for Java crash (AndroidRuntime), native crash (libc/DEBUG)
   and ANR (ActivityManager "ANR in"), then emits one event per block with
   summary fields (exception class / signal / reason) and top stack frames.

2. `Deduper` — small TTL set that suppresses repeated emissions of the same
   `(process, pid, event_type)` within a sliding window. Without it, the
   dropbox poller would re-emit each crash on every poll. Production semantics:
   first observation in window is emitted; duplicates within window are
   dropped. v1 keeps `dedup_count = 1` on each emitted event for simplicity.

Both pieces are pure (no IO, no threads). They're driven by callers in
`pool.py` who own all threading and file IO.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)


# ── Process-state code table ─────────────────────────────────────────────────
# am_proc_died event format: [user, pid, name, oom_adj, procState]
# parts[4] = procState at time of death (ActivityManager.PROCESS_STATE_* constant).
_PROC_STATE_LABELS: Dict[int, str] = {
    0:  "persistent",
    1:  "persistent-ui",
    2:  "top",
    3:  "bound-top",
    4:  "foreground-service",
    5:  "bound-foreground-service",
    6:  "important-foreground",
    7:  "important-background",
    8:  "transient-background",
    9:  "backup",
    10: "service",
    11: "receiver",
    12: "top-sleeping",
    13: "heavy-weight",
    14: "home",
    15: "last-activity",
    16: "cached-activity",
    17: "cached-activity-client",
    18: "cached-recent",
    19: "cached-empty",
    20: "nonexistent",
}


def _decode_proc_state(raw: str) -> str:
    """Return "label (N)" for numeric am_proc_died procState codes.

    am_kill emits a human-readable string (e.g. "remove task") for the same
    field position, so non-numeric values are returned as-is.
    """
    try:
        code = int(raw)
    except (ValueError, TypeError):
        return raw
    label = _PROC_STATE_LABELS.get(code)
    return f"{label} ({code})" if label else raw


# ── Event types ───────────────────────────────────────────────────────────────
EVENT_JAVA_CRASH = "java_crash"
EVENT_NATIVE_CRASH = "native_crash"
EVENT_ANR = "anr"
EVENT_PROCESS_DEATH = "process_death"

ALL_EVENT_TYPES = (
    EVENT_JAVA_CRASH, EVENT_NATIVE_CRASH, EVENT_ANR, EVENT_PROCESS_DEATH,
)

EVENT_SOURCE_LOGCAT = "logcat"
EVENT_SOURCE_DROPBOX = "dropbox"
EVENT_SOURCE_WATCHER = "watcher"


@dataclass
class StabilityEvent:
    event_type: str
    process: str
    pid: int
    triggered_at: str          # ISO UTC string from utils.utc_now_iso()
    severity: str = "fatal"
    summary: str = ""
    source: str = EVENT_SOURCE_LOGCAT
    # Type-specific evidence fields:
    exception_class: Optional[str] = None     # java_crash
    signal: Optional[str] = None              # native_crash
    fault_addr: Optional[str] = None          # native_crash
    reason: Optional[str] = None              # anr / process_death
    top_frames: List[str] = field(default_factory=list)
    raw_lines: List[str] = field(default_factory=list)
    # Original device-side timestamp from the logcat line (parser preserves it
    # verbatim; the canonical `triggered_at` uses host wall-clock observation).
    device_ts: Optional[str] = None


# ── logcat threadtime parser ──────────────────────────────────────────────────

# threadtime sample:
#   05-21 10:00:00.123  1234  5678 E AndroidRuntime: FATAL EXCEPTION: main
# Also supports `-v year` style where date is YYYY-MM-DD.
LOGCAT_LINE_RE = re.compile(
    r"^(?P<ts>(?:\d{4}-)?\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})"
    r"\s+(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<level>[VDIWEFA])\s+(?P<tag>[^:]+?)\s*:\s?(?P<msg>.*)$"
)

ANR_HEAD_RE = re.compile(r"^ANR in\s+(?P<proc>\S+?)(?:\s+\([^)]*\))?\s*$")
ANR_PID_RE = re.compile(r"^PID:\s*(?P<pid>\d+)\s*$")
ANR_REASON_RE = re.compile(r"^Reason:\s*(?P<reason>.*)$")

JAVA_PROCESS_RE = re.compile(r"^Process:\s*(?P<proc>\S+?)(?:,\s*PID:\s*(?P<pid>\d+))?\s*$")
JAVA_FRAME_RE = re.compile(r"^\s*at\s+(?P<frame>.+)$")
JAVA_EXC_RE = re.compile(r"^(?P<exc>[A-Za-z_][\w.$]*(?:Exception|Error|Throwable))(?::\s*(?P<msg>.*))?$")

# libc/DEBUG native crash lines:
LIBC_FATAL_RE = re.compile(
    r"Fatal signal\s+(?P<num>\d+)\s+\((?P<name>[A-Z]+)\)"
    r"(?:.*?fault addr\s+(?P<addr>\S+))?"
    r".*?pid\s+(?P<pid>\d+)\s+\((?P<proc>[^)]+)\)"
)
DEBUG_PID_RE = re.compile(r"^pid:\s*(?P<pid>\d+),\s*tid:\s*\d+(?:,\s*name:\s*\S+)?\s*>>>\s*(?P<proc>\S+)\s*<<<")
DEBUG_SIGNAL_RE = re.compile(r"^signal\s+(?P<num>\d+)\s+\((?P<name>[A-Z]+)\)(?:.*?fault addr\s+(?P<addr>\S+))?")
DEBUG_FRAME_RE = re.compile(r"^\s*#(?P<idx>\d+)\s+pc\s+\S+\s+(?P<rest>.+)$")

# Events-buffer tags carry comma-separated payloads inside brackets.
EVENTS_AM_RE = re.compile(r"^\[(?P<payload>.*)\]\s*$")
AM_PROC_DIED_TAG = "am_proc_died"
AM_KILL_TAG = "am_kill"
AM_ANR_TAG = "am_anr"


def _name_matches_package(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ":")


@dataclass
class _JavaCrashState:
    pid: int
    tid: int
    process: Optional[str] = None
    exception_class: Optional[str] = None
    summary: Optional[str] = None
    frames: List[str] = field(default_factory=list)
    raw: List[str] = field(default_factory=list)
    device_ts: Optional[str] = None


@dataclass
class _NativeCrashState:
    pid: Optional[int] = None
    process: Optional[str] = None
    signal: Optional[str] = None
    fault_addr: Optional[str] = None
    frames: List[str] = field(default_factory=list)
    raw: List[str] = field(default_factory=list)
    device_ts: Optional[str] = None


@dataclass
class _AnrState:
    process: Optional[str] = None
    pid: Optional[int] = None
    reason: Optional[str] = None
    raw: List[str] = field(default_factory=list)
    lines_since_anchor: int = 0
    device_ts: Optional[str] = None


# Hard cap on accumulator size so a runaway tag stream can't grow unbounded.
MAX_BLOCK_LINES = 400
# How many subsequent ActivityManager lines after `ANR in` we consider part of
# the same ANR record.
ANR_CONTEXT_LINES = 40


class LogcatLineParser:
    """State machine: feed lines one by one, get events as they complete.

    `package` limits emitted events to processes whose name equals `package`
    or starts with `package + ":"` (matches `pat/discovery.py` rules).
    `now_iso_fn` supplies the canonical observation timestamp (callable so
    tests can pin it).
    """

    def __init__(
        self,
        package: str,
        *,
        now_iso_fn,
        enable_java_crash: bool = True,
        enable_native_crash: bool = True,
        enable_anr: bool = True,
        enable_process_death: bool = True,
    ) -> None:
        self.package = package
        self._now_iso = now_iso_fn
        self.enable_java_crash = enable_java_crash
        self.enable_native_crash = enable_native_crash
        self.enable_anr = enable_anr
        self.enable_process_death = enable_process_death

        self._java: Optional[_JavaCrashState] = None
        self._native: Optional[_NativeCrashState] = None
        self._anr: Optional[_AnrState] = None

    # ------------------------------------------------------------------

    def feed_line(self, line: str) -> List[StabilityEvent]:
        """Process one logcat line. Returns 0+ completed events."""
        m = LOGCAT_LINE_RE.match(line.rstrip("\n"))
        if not m:
            # Unparseable line. If we're inside a block, treat it as the block
            # ending so we don't accumulate garbage forever.
            return self._flush_all_terminators()

        gd = m.groupdict()
        ts = gd["ts"]
        pid = int(gd["pid"])
        tid = int(gd["tid"])
        level = gd["level"]
        tag = gd["tag"].strip()
        msg = gd["msg"]

        emitted: List[StabilityEvent] = []

        # ── Java crash (AndroidRuntime, level E/F) ──
        if self.enable_java_crash and tag == "AndroidRuntime" and level in ("E", "F"):
            if msg.startswith("FATAL EXCEPTION"):
                # Starting a new java crash; flush any previous one.
                if self._java is not None:
                    ev = self._finalize_java()
                    if ev:
                        emitted.append(ev)
                self._java = _JavaCrashState(pid=pid, tid=tid, device_ts=ts)
                self._java.raw.append(line.rstrip("\n"))
                return emitted
            if self._java is not None and self._java.pid == pid:
                self._absorb_java_line(msg, line)
                return emitted
        else:
            # AndroidRuntime stream ended without a clean continuation.
            if self._java is not None and (tag != "AndroidRuntime" or self._java.pid != pid):
                ev = self._finalize_java()
                if ev:
                    emitted.append(ev)

        # ── Native crash (libc Fatal signal OR DEBUG block) ──
        if self.enable_native_crash:
            if tag == "libc" and "Fatal signal" in msg:
                ev = self._absorb_libc_fatal(msg, line, ts)
                if ev:
                    emitted.append(ev)
            elif tag == "DEBUG":
                ev = self._absorb_debug_line(msg, line, ts)
                if ev:
                    emitted.append(ev)
            elif self._native is not None and tag != "DEBUG":
                # DEBUG block ended.
                ev = self._finalize_native()
                if ev:
                    emitted.append(ev)

        # ── ANR (ActivityManager "ANR in" multi-line) ──
        if self.enable_anr:
            if tag == "ActivityManager" and msg.startswith("ANR in"):
                if self._anr is not None:
                    ev = self._finalize_anr()
                    if ev:
                        emitted.append(ev)
                self._anr = _AnrState(device_ts=ts)
                self._anr.raw.append(line.rstrip("\n"))
                m2 = ANR_HEAD_RE.match(msg)
                if m2:
                    self._anr.process = m2.group("proc")
                return emitted
            if self._anr is not None and tag == "ActivityManager":
                self._absorb_anr_line(msg, line)
                if self._anr.lines_since_anchor >= ANR_CONTEXT_LINES:
                    ev = self._finalize_anr()
                    if ev:
                        emitted.append(ev)
                return emitted
            if self._anr is not None and tag != "ActivityManager":
                ev = self._finalize_anr()
                if ev:
                    emitted.append(ev)

        # ── events buffer ANR / proc_died / kill ──
        if tag == AM_ANR_TAG and self.enable_anr:
            ev = self._make_event_from_events_buffer(EVENT_ANR, msg, ts)
            if ev:
                emitted.append(ev)
        elif tag in (AM_PROC_DIED_TAG, AM_KILL_TAG) and self.enable_process_death:
            ev = self._make_event_from_events_buffer(EVENT_PROCESS_DEATH, msg, ts)
            if ev:
                emitted.append(ev)

        return emitted

    # ------------------------------------------------------------------

    def flush(self) -> List[StabilityEvent]:
        """Force-close any open blocks (call at end-of-stream)."""
        return self._flush_all_terminators()

    def _flush_all_terminators(self) -> List[StabilityEvent]:
        out: List[StabilityEvent] = []
        ev = self._finalize_java()
        if ev:
            out.append(ev)
        ev = self._finalize_native()
        if ev:
            out.append(ev)
        ev = self._finalize_anr()
        if ev:
            out.append(ev)
        return out

    # ── Java crash internals ──

    def _absorb_java_line(self, msg: str, raw: str) -> None:
        assert self._java is not None
        st = self._java
        st.raw.append(raw.rstrip("\n"))
        if len(st.raw) > MAX_BLOCK_LINES:
            return
        m = JAVA_PROCESS_RE.match(msg)
        if m:
            st.process = m.group("proc")
            if m.group("pid"):
                try:
                    st.pid = int(m.group("pid"))
                except ValueError:
                    pass
            return
        # First exception-ish line after Process: → exception class + summary
        if st.exception_class is None:
            mexc = JAVA_EXC_RE.match(msg)
            if mexc:
                st.exception_class = mexc.group("exc")
                st.summary = msg.strip()
                return
        mfr = JAVA_FRAME_RE.match(msg)
        if mfr and len(st.frames) < 10:
            st.frames.append(mfr.group("frame").strip())

    def _finalize_java(self) -> Optional[StabilityEvent]:
        st = self._java
        self._java = None
        if st is None:
            return None
        if not st.process or not _name_matches_package(st.process, self.package):
            return None
        summary = st.summary or (st.exception_class or "java crash")
        return StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process=st.process,
            pid=st.pid,
            triggered_at=self._now_iso(),
            severity="fatal",
            summary=summary,
            source=EVENT_SOURCE_LOGCAT,
            exception_class=st.exception_class,
            top_frames=list(st.frames),
            raw_lines=list(st.raw),
            device_ts=st.device_ts,
        )

    # ── Native crash internals ──

    def _absorb_libc_fatal(self, msg: str, raw: str, ts: str) -> Optional[StabilityEvent]:
        m = LIBC_FATAL_RE.search(msg)
        if not m:
            return None
        proc = m.group("proc")
        if not _name_matches_package(proc, self.package):
            return None
        # libc one-liner is usually self-contained, but DEBUG block may follow
        # with backtrace. Open a native state to absorb subsequent DEBUG lines.
        if self._native is None:
            self._native = _NativeCrashState(device_ts=ts)
        st = self._native
        st.pid = int(m.group("pid"))
        st.process = proc
        st.signal = m.group("name")
        st.fault_addr = m.group("addr") or st.fault_addr
        st.raw.append(raw.rstrip("\n"))
        return None

    def _absorb_debug_line(self, msg: str, raw: str, ts: str) -> Optional[StabilityEvent]:
        if self._native is None:
            self._native = _NativeCrashState(device_ts=ts)
        st = self._native
        st.raw.append(raw.rstrip("\n"))
        if len(st.raw) > MAX_BLOCK_LINES:
            return None
        m_pid = DEBUG_PID_RE.match(msg)
        if m_pid:
            proc = m_pid.group("proc")
            if not _name_matches_package(proc, self.package):
                # Not our package; discard the state to avoid leaking other
                # crashes into our pipeline.
                self._native = None
                return None
            st.pid = int(m_pid.group("pid"))
            st.process = proc
            return None
        m_sig = DEBUG_SIGNAL_RE.match(msg)
        if m_sig and st.signal is None:
            st.signal = m_sig.group("name")
            st.fault_addr = m_sig.group("addr") or st.fault_addr
            return None
        m_fr = DEBUG_FRAME_RE.match(msg)
        if m_fr and len(st.frames) < 16:
            st.frames.append(f"#{m_fr.group('idx')} {m_fr.group('rest').strip()}")
        return None

    def _finalize_native(self) -> Optional[StabilityEvent]:
        st = self._native
        self._native = None
        if st is None or st.process is None or st.pid is None:
            return None
        if not _name_matches_package(st.process, self.package):
            return None
        summary = (
            f"native crash {st.signal or '?'}"
            + (f" @ {st.fault_addr}" if st.fault_addr else "")
        )
        return StabilityEvent(
            event_type=EVENT_NATIVE_CRASH,
            process=st.process,
            pid=st.pid,
            triggered_at=self._now_iso(),
            severity="fatal",
            summary=summary,
            source=EVENT_SOURCE_LOGCAT,
            signal=st.signal,
            fault_addr=st.fault_addr,
            top_frames=list(st.frames),
            raw_lines=list(st.raw),
            device_ts=st.device_ts,
        )

    # ── ANR internals ──

    def _absorb_anr_line(self, msg: str, raw: str) -> None:
        assert self._anr is not None
        st = self._anr
        st.raw.append(raw.rstrip("\n"))
        st.lines_since_anchor += 1
        if len(st.raw) > MAX_BLOCK_LINES:
            return
        m_pid = ANR_PID_RE.match(msg)
        if m_pid:
            try:
                st.pid = int(m_pid.group("pid"))
            except ValueError:
                pass
            return
        m_r = ANR_REASON_RE.match(msg)
        if m_r:
            st.reason = m_r.group("reason").strip()

    def _finalize_anr(self) -> Optional[StabilityEvent]:
        st = self._anr
        self._anr = None
        if st is None or not st.process:
            return None
        if not _name_matches_package(st.process, self.package):
            return None
        summary = f"ANR: {st.reason}" if st.reason else "ANR"
        return StabilityEvent(
            event_type=EVENT_ANR,
            process=st.process,
            pid=st.pid or 0,
            triggered_at=self._now_iso(),
            severity="error",
            summary=summary,
            source=EVENT_SOURCE_LOGCAT,
            reason=st.reason,
            raw_lines=list(st.raw),
            device_ts=st.device_ts,
        )

    # ── Events-buffer tags (am_anr / am_proc_died / am_kill) ──

    def _make_event_from_events_buffer(
        self, event_type: str, msg: str, ts: str,
    ) -> Optional[StabilityEvent]:
        m = EVENTS_AM_RE.match(msg.strip())
        if not m:
            return None
        parts = [p.strip() for p in m.group("payload").split(",")]
        # Common payloads:
        #   am_proc_died : [user,pid,name,...]
        #   am_kill      : [user,pid,name,oom_adj,reason]
        #   am_anr       : [user,pid,name,flags,reason]
        if len(parts) < 3:
            return None
        try:
            pid = int(parts[1])
        except ValueError:
            return None
        proc = parts[2]
        if not _name_matches_package(proc, self.package):
            return None
        reason: Optional[str] = None
        if event_type == EVENT_ANR and len(parts) >= 5:
            reason = parts[4]
        elif event_type == EVENT_PROCESS_DEATH and len(parts) >= 5:
            reason = _decode_proc_state(parts[4])
        return StabilityEvent(
            event_type=event_type,
            process=proc,
            pid=pid,
            triggered_at=self._now_iso(),
            severity="error" if event_type == EVENT_ANR else "warning",
            summary=f"{event_type}: {reason}" if reason else event_type,
            source=EVENT_SOURCE_LOGCAT,
            reason=reason,
            device_ts=ts,
            raw_lines=[],
        )


# ── Deduper ───────────────────────────────────────────────────────────────────

# Matches HH:MM:SS[.fraction] inside any timestamp string.
_DEVICE_TS_RE = re.compile(r'(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?')


def _parse_device_ts_sec(ts: Optional[str]) -> Optional[float]:
    """Extract seconds-of-day from a device timestamp string.

    Handles logcat format ("MM-DD HH:MM:SS.mmm") and dropbox format
    ("YYYY-MM-DD HH:MM:SS"). Returns None if unparseable.
    """
    if not ts:
        return None
    m = _DEVICE_TS_RE.search(ts)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    frac = float("0." + m.group(4)) if m.group(4) else 0.0
    return h * 3600 + mi * 60 + s + frac


class Deduper:
    """Suppress repeated ``(process, pid, event_type)`` observations.

    Dedup strategy (applied in priority order):

    1. **Device-time window** (primary) — if both the incoming event and the
       stored anchor have a ``device_ts``, compare the device-side timestamps.
       Events within ``device_ts_window_sec`` (default 10 s) of each other
       represent the same physical crash regardless of *when* the host observed
       them. This correctly merges logcat and dropbox reports of the same crash
       even when the dropbox poll arrives 30 s later in host time.

    2. **Host-time window** (fallback) — used when either event lacks a
       ``device_ts`` (e.g. watcher ``process_death`` events that are generated
       locally). Events observed within ``window_sec`` (default 5 s) are
       considered duplicates.

    Anchors are GC-ed after ``anchor_max_age_sec`` (default 300 s).
    """

    def __init__(
        self,
        window_sec: float = 5.0,
        device_ts_window_sec: float = 10.0,
        anchor_max_age_sec: float = 300.0,
    ) -> None:
        self.window_sec = float(window_sec)
        self.device_ts_window_sec = float(device_ts_window_sec)
        self._anchor_max_age = float(anchor_max_age_sec)
        # key → (host_time_sec, device_ts_sec_or_None)
        self._anchors: Dict[Tuple[str, int, str], Tuple[float, Optional[float]]] = {}

    def observe(self, event: StabilityEvent, now_sec: float) -> bool:
        # GC anchors older than max_age.
        cutoff = now_sec - self._anchor_max_age
        if self._anchors:
            stale = [k for k, (t, _) in self._anchors.items() if t < cutoff]
            for k in stale:
                self._anchors.pop(k, None)

        key = (event.process, event.pid, event.event_type)
        event_dev = _parse_device_ts_sec(event.device_ts)

        if key in self._anchors:
            anchor_host, anchor_dev = self._anchors[key]
            if event_dev is not None and anchor_dev is not None:
                # Primary: device-time dedup. Handles midnight rollover.
                delta = abs(event_dev - anchor_dev)
                if min(delta, 86400.0 - delta) <= self.device_ts_window_sec:
                    return False
            else:
                # Fallback: host-time dedup (no device_ts on one or both).
                if now_sec - anchor_host <= self.window_sec:
                    return False

        self._anchors[key] = (now_sec, event_dev)
        return True
