"""Collector pool: 2 long-lived pipelines + dispatcher.

1. logcat thread  — reads `adb logcat` stream, parses lines into events
   (java_crash, native_crash, ANR, process_death via am_proc_died/am_kill),
   writes raw lines to the rotating log file, dispatches events.
2. watcher thread — discovers processes for the target package on a 5 s
   reconcile cadence; writes lifecycle rows (new/restart/gone) to the CSV
   but does NOT dispatch stability events (process_death is detected via the
   am_proc_died / am_kill entries in the logcat events buffer).

Dispatch path: event → Deduper → fire_dump(event)
fire_dump submits a bounded task to a ThreadPoolExecutor. The pool tracks every
task through `queued -> running -> persisted|failed|timed_out` so `stop()`
can drain in-flight dumps before reports are generated. Per-type incident
caps prevent runaway disk usage.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from . import discovery
from .adb import Adb
from .analyzers.anr import analyze_anr_trace
from .analyzers.fingerprint import fingerprint_incident
from .analyzers.java_retrace import deobfuscate_stack
from .analyzers.native_symbolizer import symbolize_frames
from .backpressure import BackpressureController, EvidenceSampler
from .collectors.device_health import DeviceHealthMonitor
from .collectors.exit_info import latest_watermark, query_exit_info
from .collectors.logcat import LogcatStream
from .collectors.resource_risk import ResourceRiskDetector, ResourceRiskMonitor
from .context import LogcatContextBuffer, LogEntry, format_context_slice
from .detection import (
    ALL_EVENT_TYPES,
    EVENT_ANR,
    EVENT_JAVA_CRASH,
    EVENT_NATIVE_CRASH,
    EVENT_PROCESS_DEATH,
    LOGCAT_LINE_RE,
    Deduper,
    LogcatLineParser,
    StabilityEvent,
)
from .discovery import Process
from .dumpers import anr as anr_dumper
from .dumpers import base_name_for
from .dumpers import java_crash as java_crash_dumper
from .dumpers import native_crash as native_crash_dumper
from .dumpers import proc_death as proc_death_dumper
from .journal import (
    STATUS_DROPPED_BY_BACKPRESSURE,
    STATUS_DROPPED_BY_CAP,
    STATUS_FAILED,
    STATUS_PERSISTED,
    STATUS_TIMED_OUT,
    IncidentJournal,
)
from .quota import QuotaConfig, QuotaTracker
from .selfmon import SelfMonitor
from .storage import CsvStreamWriter, LogStreamWriter
from .utils import utc_now_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectionConfig:
    enable_java_crash: bool = True
    enable_native_crash: bool = True
    enable_anr: bool = True
    enable_process_death: bool = True
    # Host-time fallback window: used when device_ts is absent.
    dedup_window_sec: float = 5.0
    # Device-time window: dedup events from the same physical crash that arrive
    # via different logcat tags (e.g. libc + DEBUG for native crashes).
    device_ts_window_sec: float = 10.0


@dataclass(frozen=True)
class DumpsConfig:
    pre_context_sec: float = 30.0
    post_context_sec: float = 10.0
    max_incidents_per_type: int = 200
    max_concurrent: int = 2
    dump_shutdown_timeout_sec: float = 60.0
    context_retention_sec: Optional[float] = None
    context_buffer_max_lines: int = 5000
    context_buffer_max_bytes: int = 4 * 1024 * 1024
    max_disk_bytes: Optional[int] = None
    max_log_file_bytes: int = 512 * 1024 * 1024
    log_retention_hours: int = 24
    max_queue_size: int = 50
    evidence_sample_every_n: int = 5
    self_monitor_enabled: bool = True
    self_monitor_interval_sec: float = 60.0
    pull_tombstone: bool = True
    pull_anr_trace: bool = True


@dataclass(frozen=True)
class CollectorsConfig:
    logcat_enabled: bool = True
    logcat_buffers: tuple = ("main", "system", "events", "crash")
    logcat_reconnect_backoff_sec: float = 2.0
    device_health_interval_sec: float = 5.0
    device_reboot_policy: str = "wait-and-resume"
    resource_risk_enabled: bool = True
    resource_risk_interval_sec: float = 30.0
    resource_fd_growth_threshold: int = 200
    resource_thread_growth_threshold: int = 50


@dataclass(frozen=True)
class DiagnosisConfig:
    mapping_file: Optional[str] = None
    retrace_command: Optional[str] = None
    native_symbols_dir: Optional[str] = None
    llvm_symbolizer_path: Optional[str] = None


DUMP_TASK_STATES = ("queued", "running", "persisted", "failed", "timed_out")


@dataclass
class _DumpTask:
    event: Optional[StabilityEvent] = None
    anchor_sec: float = 0.0
    state: str = "queued"
    future: Optional[concurrent.futures.Future] = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    _terminal_written: bool = False


class CollectorPool:
    def __init__(
        self,
        adb: Adb,
        package: str,
        *,
        events_writer: CsvStreamWriter,
        lifecycle_writer: CsvStreamWriter,
        logcat_writer: Optional[LogStreamWriter] = None,
        rescan_interval_sec: float = 5.0,
        process_filter: Optional[Iterable[str]] = None,
        detection: Optional[DetectionConfig] = None,
        dumps: Optional[DumpsConfig] = None,
        collectors: Optional[CollectorsConfig] = None,
        diagnosis: Optional[DiagnosisConfig] = None,
        incidents_dir: Optional[Path] = None,
        journal: Optional[IncidentJournal] = None,
        run_id: Optional[str] = None,
        adb_path: str = "adb",
        # Test injection points (production passes none):
        discover_fn: Optional[Callable[[Adb, str], List[Process]]] = None,
        logcat_stream_factory: Optional[Callable[[], LogcatStream]] = None,
        java_crash_dump_fn: Optional[Callable] = None,
        native_crash_dump_fn: Optional[Callable] = None,
        anr_dump_fn: Optional[Callable] = None,
        proc_death_dump_fn: Optional[Callable] = None,
        now_iso_fn: Optional[Callable[[], str]] = None,
        now_sec_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._adb = adb
        self._package = package
        self._events_writer = events_writer
        self._lifecycle_writer = lifecycle_writer
        self._logcat_writer = logcat_writer
        self._rescan_interval = float(rescan_interval_sec)
        self._filter = self._normalize_filter(process_filter, package)
        self._detection = detection or DetectionConfig()
        self._dumps_cfg = dumps or DumpsConfig()
        self._collectors_cfg = collectors or CollectorsConfig()
        self._diagnosis = diagnosis or DiagnosisConfig()
        self._incidents_dir = Path(incidents_dir) if incidents_dir else None
        if journal is None and self._incidents_dir is not None:
            journal = IncidentJournal(self._incidents_dir.parent / "incident_journal.jsonl")
        self._journal = journal
        self._run_id = run_id
        self._adb_path = adb_path

        self._discover = discover_fn or discovery.discover
        self._logcat_stream_factory = logcat_stream_factory or self._default_logcat_factory
        self._java_crash_dump = java_crash_dump_fn or java_crash_dumper.run
        self._native_crash_dump = native_crash_dump_fn or native_crash_dumper.run
        self._anr_dump = anr_dump_fn or anr_dumper.run
        self._proc_death_dump = proc_death_dump_fn or proc_death_dumper.run
        self._now_iso = now_iso_fn or utc_now_iso
        self._now_sec = now_sec_fn or time.time

        self._procs: Dict[str, Process] = {}
        self._procs_lock = threading.RLock()
        self._gone_at: Dict[str, float] = {}

        self._global_stop = threading.Event()
        self._logcat_thread: Optional[threading.Thread] = None
        self._watcher_thread: Optional[threading.Thread] = None
        self._logcat_stream: Optional[LogcatStream] = None
        self._logcat_stats: Dict = {}
        self._parser: Optional[LogcatLineParser] = None
        self._exit_watermark: Optional[float] = None
        self._exit_records: List[Dict] = []
        self._device_monitor: Optional[DeviceHealthMonitor] = None
        self._resource_monitor: Optional[ResourceRiskMonitor] = None
        self._quota = QuotaTracker(
            self._incidents_dir.parent if self._incidents_dir else Path("."),
            QuotaConfig(
                max_disk_bytes=self._dumps_cfg.max_disk_bytes,
                max_log_file_bytes=self._dumps_cfg.max_log_file_bytes,
                log_retention_hours=self._dumps_cfg.log_retention_hours,
                max_queue_size=self._dumps_cfg.max_queue_size,
                evidence_sample_every_n=self._dumps_cfg.evidence_sample_every_n,
            ),
        )
        self._backpressure = BackpressureController(
            max_queue_size=self._dumps_cfg.max_queue_size,
        )
        self._sampler = EvidenceSampler(
            every_n=self._dumps_cfg.evidence_sample_every_n,
        )
        self._self_monitor: Optional[SelfMonitor] = None

        self._deduper = Deduper(
            self._detection.dedup_window_sec,
            device_ts_window_sec=self._detection.device_ts_window_sec,
        )
        context_retention = self._dumps_cfg.context_retention_sec or (
            self._dumps_cfg.pre_context_sec + self._dumps_cfg.post_context_sec + 60.0
        )
        self._context_buffer = LogcatContextBuffer(
            retention_sec=context_retention,
            max_entries=self._dumps_cfg.context_buffer_max_lines,
            max_bytes=self._dumps_cfg.context_buffer_max_bytes,
            clock=self._now_sec,
        )
        self._accepting = True
        self._dispatch_lock = threading.Lock()
        self._dump_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(self._dumps_cfg.max_concurrent)),
            thread_name_prefix="dump-",
        )
        self._task_lock = threading.Lock()
        self._tasks: List[_DumpTask] = []
        self._pending_dumps = 0
        self._queue_peak = 0
        self._event_counts: Dict[str, int] = {t: 0 for t in ALL_EVENT_TYPES}
        self._dropped_by_cap = 0
        self._sample_failures: Dict[str, int] = {"logcat": 0}
        self._event_counts_lock = threading.Lock()

    # ------------------------------------------------------------------

    def start(self, initial_processes: Iterable[Process] = ()) -> None:
        with self._procs_lock:
            for p in initial_processes:
                if self._passes_filter(p):
                    self._procs[p.name] = p
                    self._write_lifecycle("new", p, old_pid=0, gap_sec=0.0)

        if self._collectors_cfg.logcat_enabled:
            self._logcat_thread = threading.Thread(
                target=self._logcat_loop,
                daemon=True,
                name="logcat-collector",
            )
            self._logcat_thread.start()

        self._device_monitor = DeviceHealthMonitor(
            self._adb,
            interval_sec=self._collectors_cfg.device_health_interval_sec,
            on_gap_started=self._on_device_gap,
            on_recovered=self._on_device_recovered,
        )
        self._device_monitor.start()

        if self._collectors_cfg.resource_risk_enabled:
            self._resource_monitor = ResourceRiskMonitor(
                self._adb,
                self._package,
                interval_sec=self._collectors_cfg.resource_risk_interval_sec,
                detector=ResourceRiskDetector(
                    fd_growth_threshold=(self._collectors_cfg.resource_fd_growth_threshold),
                    thread_growth_threshold=(self._collectors_cfg.resource_thread_growth_threshold),
                ),
            )
            self._resource_monitor.start()

        if self._dumps_cfg.self_monitor_enabled:
            self._self_monitor = SelfMonitor(
                self._incidents_dir.parent if self._incidents_dir else Path("."),
                interval_sec=self._dumps_cfg.self_monitor_interval_sec,
                queue_depth_fn=self._backpressure.queued_count,
            )
            self._self_monitor.start()

        try:
            self._exit_watermark = latest_watermark(self._adb, self._package)
        except Exception:
            log.exception("exit-info watermark query failed; using no watermark")

        self._watcher_thread = threading.Thread(
            target=self._watch_loop,
            daemon=True,
            name="proc-watcher",
        )
        self._watcher_thread.start()

    def stop(
        self,
        join_timeout: float = 5.0,
        *,
        dump_shutdown_timeout_sec: Optional[float] = None,
    ) -> None:
        """Stop the pool in a fixed order and drain dump tasks.

        Order:
        1. stop accepting new events;
        2. stop + join the logcat thread, then flush the parser so an
           in-progress crash block is not silently lost;
        3. join the watcher thread;
        4. wait for dump tasks up to `dump_shutdown_timeout_sec` and mark
           anything still pending as `timed_out`;
        5. cancel queued work / shut the executor down.

        Writers and the final report are closed/built by the caller
        (`api.StabilityTest.stop()`), which runs after this returns.
        """
        self._accepting = False
        self._global_stop.set()
        if self._logcat_stream is not None:
            self._logcat_stream.stop()
        if self._logcat_thread is not None:
            self._logcat_thread.join(timeout=join_timeout)
        # Flush any in-progress parser block that arrived at the tail of the
        # stream; these are already-detected events, not new ones.
        if self._parser is not None:
            for event in self._parser.flush():
                self._dispatch_flushed(event)
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=join_timeout)
        if self._device_monitor is not None:
            self._device_monitor.stop()
        if self._resource_monitor is not None:
            self._resource_monitor.stop()
        if self._self_monitor is not None:
            self._self_monitor.stop()

        try:
            records = query_exit_info(
                self._adb,
                self._package,
                since_epoch=self._exit_watermark,
            )
            self._exit_records = [r.to_dict() for r in records]
        except Exception:
            log.exception("exit-info query failed at stop")

        # Drain pending dump tasks normally first.
        timeout = (
            self._dumps_cfg.dump_shutdown_timeout_sec
            if dump_shutdown_timeout_sec is None
            else dump_shutdown_timeout_sec
        )
        with self._task_lock:
            pending = [t for t in self._tasks if t.future is not None and not t.future.done()]
        if pending:
            deadline = time.monotonic() + max(0.0, float(timeout))
            for task in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    task.future.result(timeout=remaining)
                except BaseException:  # noqa: BLE001 - state already recorded by wrapper
                    pass

        # Signal cancellation for any tasks still not done.
        with self._task_lock:
            for task in self._tasks:
                if task.state in ("queued", "running") and (
                    task.future is None or not task.future.done()
                ):
                    task.cancelled.set()

        # Mark any still-unfinished tasks as timed_out (single terminal state).
        with self._task_lock:
            for task in self._tasks:
                if task.state in ("queued", "running") and (
                    task.future is None or not task.future.done()
                ):
                    task.state = "timed_out"
                    task._terminal_written = True
                    if self._journal is not None and task.event is not None:
                        try:
                            self._journal.terminal(
                                task.event.event_id or "",
                                STATUS_TIMED_OUT,
                                error_type="dump_shutdown_timeout",
                                error=("dump task did not finish within dump_shutdown_timeout_sec"),
                            )
                        except Exception:
                            log.exception("journal timed_out append failed")
        self._dump_executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """Close the incident journal file handle (flush + close)."""
        if self._journal is not None:
            try:
                self._journal.close()
            except Exception:
                log.exception("journal close failed")

    # ------------------------------------------------------------------

    def current_processes(self) -> List[Process]:
        with self._procs_lock:
            return list(self._procs.values())

    def event_counts(self) -> Dict[str, int]:
        with self._event_counts_lock:
            return dict(self._event_counts)

    def dump_task_states(self) -> Dict[str, int]:
        counts = {s: 0 for s in DUMP_TASK_STATES}
        with self._task_lock:
            for task in self._tasks:
                counts[task.state] = counts.get(task.state, 0) + 1
        return counts

    def dropped_by_cap_count(self) -> int:
        with self._event_counts_lock:
            return self._dropped_by_cap

    def dropped_by_backpressure_count(self) -> int:
        return self._backpressure.dropped_count()

    def collector_status(self) -> Dict:
        out: Dict = {}
        if self._logcat_stream is not None:
            self._logcat_stats = dict(self._logcat_stream.stats)
        if self._logcat_stats:
            out["logcat"] = dict(self._logcat_stats)
        return out

    def queue_backlog_peak(self) -> int:
        with self._task_lock:
            return self._queue_peak

    def exit_info_records(self) -> List[Dict]:
        return [dict(r) for r in self._exit_records]

    def device_events(self) -> List[Dict]:
        if self._device_monitor is None:
            return []
        return [e.to_dict() for e in self._device_monitor.events()]

    def resource_risk_events(self) -> List[Dict]:
        if self._resource_monitor is None:
            return []
        return self._resource_monitor.events()

    def self_resource_summary(self) -> Dict:
        if self._self_monitor is None:
            return {}
        return self._self_monitor.summary()

    def _on_device_gap(self, kind: str) -> None:
        log.warning("device gap started: %s", kind)
        if self._collectors_cfg.device_reboot_policy == "fail-fast":
            self._accepting = False
            self._global_stop.set()
        if self._logcat_stream is not None:
            self._logcat_stream.stop()

    def _on_device_recovered(self) -> None:
        if self._global_stop.is_set():
            return
        log.info("device recovered; clearing process state and restarting logcat")
        with self._procs_lock:
            self._procs.clear()
            self._gone_at.clear()
        if self._collectors_cfg.logcat_enabled:
            self._logcat_thread = threading.Thread(
                target=self._logcat_loop,
                daemon=True,
                name="logcat-collector",
            )
            self._logcat_thread.start()

    def sample_failures(self) -> Dict[str, int]:
        with self._event_counts_lock:
            return dict(self._sample_failures)

    # ── default factory ──

    def _default_logcat_factory(self) -> LogcatStream:
        return LogcatStream(
            serial=self._adb.serial,
            adb_path=self._adb_path,
            buffers=list(self._collectors_cfg.logcat_buffers),
            reconnect_backoff_sec=self._collectors_cfg.logcat_reconnect_backoff_sec,
            initial_device_ts=self._query_device_ts(),
        )

    def _query_device_ts(self) -> Optional[str]:
        """Return `MM-DD HH:MM:SS.mmm` for the initial logcat watermark."""
        try:
            r = self._adb.shell(
                "date +%m-%d_%H:%M:%S.000",
                check=False,
                timeout=3.0,
            )
        except Exception:
            return None
        if r.returncode != 0:
            return None
        ts = r.stdout.strip().replace("_", " ")
        return ts if ts else None

    # ── filter ──

    @staticmethod
    def _normalize_filter(filter_list, package: str):
        if not filter_list:
            return None
        out = set()
        for f in filter_list:
            f = (f or "").strip()
            if not f or f == "main":
                out.add(package)
            elif f.startswith(":"):
                out.add(package + f)
            else:
                out.add(f)
        return out

    def _passes_filter(self, p: Process) -> bool:
        return self._filter is None or p.name in self._filter

    # ── logcat pipeline ──

    def _logcat_loop(self) -> None:
        parser = LogcatLineParser(
            self._package,
            now_iso_fn=self._now_iso,
            enable_java_crash=self._detection.enable_java_crash,
            enable_native_crash=self._detection.enable_native_crash,
            enable_anr=self._detection.enable_anr,
            enable_process_death=self._detection.enable_process_death,
        )
        self._parser = parser
        try:
            self._logcat_stream = self._logcat_stream_factory()
        except Exception:
            log.exception("logcat stream factory failed; logcat pipeline disabled")
            return
        try:
            for line in self._logcat_stream.lines():
                if self._global_stop.is_set():
                    break
                self._append_context_entry(line)
                if self._logcat_writer is not None:
                    try:
                        self._logcat_writer.write_line(line)
                    except Exception:
                        log.exception("logcat writer failed")
                try:
                    events = parser.feed_line(line)
                except Exception:
                    self._record_sample_failure("logcat")
                    log.exception("logcat parser failed on line")
                    continue
                for event in events:
                    self._dispatch(event)
            try:
                self._quota.enforce_log_retention()
            except Exception:
                log.exception("log retention failed")
            # End-of-stream: flush any in-progress block.
            for event in parser.flush():
                self._dispatch(event)
        finally:
            if self._logcat_stream is not None:
                self._logcat_stats = dict(self._logcat_stream.stats)
            self._logcat_stream = None

    # ── watcher pipeline ──

    def _watch_loop(self) -> None:
        try:
            self._reconcile()
        except Exception:
            log.exception("watcher initial reconcile failed")
        while not self._global_stop.is_set():
            if self._global_stop.wait(self._rescan_interval):
                break
            try:
                self._reconcile()
            except Exception:
                log.exception("watcher reconcile failed")

    def _reconcile(self) -> None:
        try:
            live = self._discover(self._adb, self._package)
        except Exception:
            log.exception("discover failed during reconcile")
            return
        live = [p for p in live if self._passes_filter(p)]
        live_by_name: Dict[str, Process] = {p.name: p for p in live}

        with self._procs_lock:
            current_names = set(self._procs.keys())
            live_names = set(live_by_name.keys())

            for name in current_names - live_names:
                proc = self._procs.pop(name, None)
                if proc is None:
                    continue
                self._write_lifecycle("gone", proc, old_pid=proc.pid, gap_sec=0.0)
                self._gone_at[name] = self._now_sec()
                # process_death events are detected via am_proc_died / am_kill
                # in the logcat events buffer — no dispatch here.

            for name in live_names:
                proc = live_by_name[name]
                if name in self._procs:
                    if self._procs[name].pid != proc.pid:
                        old_pid = self._procs[name].pid
                        self._write_lifecycle("restart", proc, old_pid=old_pid, gap_sec=0.0)
                        self._procs[name] = proc
                else:
                    gap = 0.0
                    event = "new"
                    if name in self._gone_at:
                        gap = max(0.0, self._now_sec() - self._gone_at.pop(name))
                        event = "restart"
                    self._procs[name] = proc
                    self._write_lifecycle(event, proc, old_pid=0, gap_sec=gap)

    def _write_lifecycle(
        self,
        event: str,
        process: Process,
        *,
        old_pid: int,
        gap_sec: float,
    ) -> None:
        if self._lifecycle_writer is None:
            return
        self._lifecycle_writer.write_row(
            {
                "timestamp": self._now_iso(),
                "process_name": process.name,
                "event": event,
                "old_pid": old_pid,
                "new_pid": 0 if event == "gone" else process.pid,
                "gap_sec": round(gap_sec, 3),
            }
        )

    # ── dispatcher ──

    def _append_context_entry(self, line: str) -> None:
        m = LOGCAT_LINE_RE.match(line)
        if m:
            entry = LogEntry(
                host_ts=self._now_sec(),
                device_ts=m.group("ts"),
                pid=int(m.group("pid")),
                tid=int(m.group("tid")),
                tag=m.group("tag").strip(),
                level=m.group("level"),
                raw=line,
            )
        else:
            entry = LogEntry(
                host_ts=self._now_sec(),
                device_ts=None,
                pid=None,
                tid=None,
                tag="",
                level="",
                raw=line,
            )
        try:
            self._context_buffer.append(entry)
        except Exception:
            log.exception("context buffer append failed")

    def _dispatch(self, event: StabilityEvent) -> None:
        if not self._accepting:
            return
        self._dispatch_inner(event)

    def _dispatch_flushed(self, event: StabilityEvent) -> None:
        """Dispatch an event recovered from the parser during stop-flush."""
        self._dispatch_inner(event)

    def _dispatch_inner(self, event: StabilityEvent) -> None:
        event_id = str(uuid.uuid4())
        event.event_id = event_id
        event.run_id = self._run_id
        with self._dispatch_lock:
            if not self._deduper.observe(event, self._now_sec()):
                return
            with self._event_counts_lock:
                cap = self._dumps_cfg.max_incidents_per_type
                if self._event_counts.get(event.event_type, 0) >= cap:
                    log.warning(
                        "max incidents (%d) reached for %s; dropping", cap, event.event_type
                    )
                    self._dropped_by_cap += 1
                    self._journal_detected(event)
                    self._journal_terminal(
                        event_id,
                        STATUS_DROPPED_BY_CAP,
                        error_type="max_incidents_per_type",
                        error=f"incident cap {cap} reached for {event.event_type}",
                    )
                    return
                self._event_counts[event.event_type] = (
                    self._event_counts.get(event.event_type, 0) + 1
                )
            self._write_event_row(event)
        self._journal_detected(event)
        self._submit_dump(event)

    def _submit_dump(self, event: StabilityEvent) -> None:
        if not self._backpressure.try_acquire():
            self._journal_terminal(
                event.event_id or "",
                STATUS_DROPPED_BY_BACKPRESSURE,
                error_type="queue_full",
                error=f"dump queue full (max {self._backpressure.max_queue_size})",
            )
            return
        task = _DumpTask(event=event, anchor_sec=self._now_sec())
        with self._task_lock:
            self._tasks.append(task)
            self._pending_dumps += 1
            self._queue_peak = max(self._queue_peak, self._pending_dumps)

        def run() -> dict:
            with self._task_lock:
                # Guard: stop() may have already claimed a terminal state.
                if task._terminal_written:
                    return {}
                task.state = "running"
            try:
                # Check cooperative cancellation before starting work.
                if task.cancelled.is_set():
                    raise RuntimeError("dump cancelled before start")
                result = self._run_dump(event, task.anchor_sec)
            except BaseException as exc:
                # Single terminal state: only write if not already timed_out.
                with self._task_lock:
                    if not task._terminal_written:
                        task._terminal_written = True
                        task.state = "failed"
                    else:
                        # Another path (stop timeout) already claimed the terminal state.
                        return {}
                self._journal_terminal(
                    event.event_id or "",
                    STATUS_FAILED,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
                raise
            # Single terminal state: only write persisted if not already timed_out.
            with self._task_lock:
                if not task._terminal_written:
                    task._terminal_written = True
                    task.state = "persisted"
                else:
                    return result
            self._journal_terminal(event.event_id or "", STATUS_PERSISTED)
            return result

        def run_wrapper() -> dict:
            try:
                return run()
            finally:
                with self._task_lock:
                    self._pending_dumps -= 1
                self._backpressure.release()

        with self._task_lock:
            task.future = self._dump_executor.submit(run_wrapper)

    def _write_event_row(self, event: StabilityEvent) -> None:
        if self._events_writer is None:
            return
        try:
            self._events_writer.write_row(
                {
                    "timestamp": event.triggered_at,
                    "event_id": event.event_id or "",
                    "run_id": event.run_id or "",
                    "event_type": event.event_type,
                    "process_name": event.process,
                    "pid": event.pid,
                    "severity": event.severity,
                    "summary": event.summary[:500],
                }
            )
        except Exception:
            log.exception("events writer failed")

    def _journal_detected(self, event: StabilityEvent) -> None:
        if self._journal is None:
            return
        try:
            self._journal.detected(event.event_id or "", event)
        except Exception:
            log.exception("journal detected append failed")

    def _journal_terminal(
        self,
        event_id: str,
        status: str,
        *,
        error_type: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if self._journal is None:
            return
        try:
            self._journal.terminal(
                event_id,
                status,
                error_type=error_type,
                error=error,
            )
        except Exception:
            log.exception("journal terminal append failed (%s)", status)

    def _attach_context(
        self,
        event: StabilityEvent,
        anchor_sec: float,
    ) -> None:
        """Wait for the post-context window, then snapshot and write the slice."""
        pre_sec = self._dumps_cfg.pre_context_sec
        post_sec = self._dumps_cfg.post_context_sec
        deadline = anchor_sec + max(0.0, float(post_sec))
        now = self._now_sec()
        while now < deadline and not self._global_stop.is_set():
            self._global_stop.wait(min(0.1, max(0.0, deadline - now)))
            now = self._now_sec()

        slice_ = self._context_buffer.snapshot(
            anchor_sec,
            pre_sec=pre_sec,
            post_sec=post_sec,
            now_ts=now,
        )
        if self._global_stop.is_set() and now < deadline:
            slice_.post_missing_reason = "run_stopped_early"
        if slice_.dropped_by_cap_count > 0 and pre_sec > 0:
            slice_.pre_missing_reason = "buffer_overflow_dropped"

        event.context_meta = {
            "pre_context_sec": round(float(pre_sec), 3),
            "post_context_sec": round(float(post_sec), 3),
            "pre_context_sec_actual": slice_.pre_context_sec_actual,
            "post_context_sec_actual": slice_.post_context_sec_actual,
            "pre_context_missing_reason": slice_.pre_missing_reason,
            "post_context_missing_reason": slice_.post_missing_reason,
            "context_buffer_dropped_count": slice_.dropped_by_cap_count,
        }
        if self._incidents_dir is None:
            return
        if pre_sec <= 0 and post_sec <= 0:
            return
        if self._quota.hard_reached:
            event.context_meta["disk_quota_skipped"] = True
            return
        try:
            base = base_name_for(event)
            path = self._incidents_dir / f"{base}_context.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                format_context_slice(event.raw_lines, slice_),
                encoding="utf-8",
            )
            event.context_file = path.name
        except Exception:
            log.exception("context slice write failed")

    def _run_dump(self, event: StabilityEvent, anchor_sec: float) -> dict:
        self._attach_context(event, anchor_sec)
        if self._incidents_dir is None:
            return {}
        if event.event_type == EVENT_JAVA_CRASH:
            incident = self._java_crash_dump(self._adb, event, self._incidents_dir)
        elif event.event_type == EVENT_NATIVE_CRASH:
            incident = self._native_crash_dump(
                self._adb,
                event,
                self._incidents_dir,
                pull_tombstone=self._dumps_cfg.pull_tombstone,
            )
        elif event.event_type == EVENT_ANR:
            incident = self._anr_dump(
                self._adb,
                event,
                self._incidents_dir,
                pull_anr_trace=self._dumps_cfg.pull_anr_trace,
            )
        elif event.event_type == EVENT_PROCESS_DEATH:
            incident = self._proc_death_dump(self._adb, event, self._incidents_dir)
        else:
            raise ValueError(f"unknown event type: {event.event_type}")
        self._postprocess_incident(event, incident)
        return incident

    def _postprocess_incident(
        self,
        event: StabilityEvent,
        incident: dict,
    ) -> None:
        """Apply diagnosis analyzers and rewrite the incident JSON atomically."""
        evidence = incident.setdefault("evidence", {})
        if (
            event.event_type == EVENT_PROCESS_DEATH
            and self._incidents_dir is not None
            and (self._incidents_dir.parent / "workload_manifest.json").exists()
        ):
            evidence["workload_expected"] = True
        fingerprint = fingerprint_incident(incident)
        decision = self._sampler.decide(fingerprint)
        if decision == "occurrence_only":
            for key in ("context_file", "trace_file", "logcat_slice_file", "dropbox_file"):
                evidence.pop(key, None)
            evidence["sampled"] = True
            evidence["sample_reason"] = "occurrence_only"
        else:
            evidence["sampled"] = False

        if event.event_type == EVENT_JAVA_CRASH and self._diagnosis.mapping_file:
            result = deobfuscate_stack(
                evidence.get("top_frames", []),
                mapping_path=Path(self._diagnosis.mapping_file),
                retrace_command=self._diagnosis.retrace_command,
            )
            evidence["symbolication_status"] = result.status
            if result.error:
                evidence["symbolication_error"] = result.error
            if result.frames:
                evidence["deobfuscated_frames"] = result.frames
        elif event.event_type == EVENT_NATIVE_CRASH and (
            self._diagnosis.native_symbols_dir or self._diagnosis.llvm_symbolizer_path
        ):
            result = symbolize_frames(
                evidence.get("top_frames", []),
                symbols_dir=(
                    Path(self._diagnosis.native_symbols_dir)
                    if self._diagnosis.native_symbols_dir
                    else None
                ),
                llvm_symbolizer=self._diagnosis.llvm_symbolizer_path,
            )
            evidence["symbolication_status"] = result.status
            if result.error:
                evidence["symbolication_error"] = result.error
            if result.frames:
                evidence["symbolized_frames"] = result.frames
        elif event.event_type == EVENT_ANR:
            trace_lines: List[str] = list(event.raw_lines)
            trace_file = evidence.get("trace_file")
            if trace_file and (self._incidents_dir / trace_file).exists():
                trace_lines = (
                    (self._incidents_dir / trace_file)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
            evidence["diagnosis"] = analyze_anr_trace(trace_lines)

        if self._quota.hard_reached and event.event_type != EVENT_PROCESS_DEATH:
            evidence["disk_quota_skipped"] = True

        base = base_name_for(event)
        path = self._incidents_dir / f"{base}.json"
        if path.exists():
            from .dumpers import write_incident

            write_incident(path, incident)

    def _record_sample_failure(self, source: str) -> None:
        with self._event_counts_lock:
            self._sample_failures[source] = self._sample_failures.get(source, 0) + 1
