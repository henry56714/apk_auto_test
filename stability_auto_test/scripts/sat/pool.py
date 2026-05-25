"""Collector pool: 2 long-lived pipelines + dispatcher.

1. logcat thread  — reads `adb logcat` stream, parses lines into events
   (java_crash, native_crash, ANR, process_death via am_proc_died/am_kill),
   writes raw lines to the rotating log file, dispatches events.
2. watcher thread — discovers processes for the target package on a 5 s
   reconcile cadence; writes lifecycle rows (new/restart/gone) to the CSV
   but does NOT dispatch stability events (process_death is detected via the
   am_proc_died / am_kill entries in the logcat events buffer).

Dispatch path: event → Deduper → fire_dump(event)
fire_dump starts a worker thread (bounded by a semaphore) that runs the
appropriate dumper to write incident files. Per-type incident caps prevent
runaway disk usage.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from . import discovery
from .adb import Adb
from .collectors.logcat import LogcatStream
from .detection import (
    ALL_EVENT_TYPES,
    EVENT_ANR,
    EVENT_JAVA_CRASH,
    EVENT_NATIVE_CRASH,
    EVENT_PROCESS_DEATH,
    Deduper,
    LogcatLineParser,
    StabilityEvent,
)
from .discovery import Process
from .dumpers import anr as anr_dumper
from .dumpers import java_crash as java_crash_dumper
from .dumpers import native_crash as native_crash_dumper
from .dumpers import proc_death as proc_death_dumper
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
    pull_tombstone: bool = True
    pull_anr_trace: bool = True


@dataclass(frozen=True)
class CollectorsConfig:
    logcat_enabled: bool = True
    logcat_buffers: tuple = ("main", "system", "events", "crash")
    logcat_reconnect_backoff_sec: float = 2.0


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
        incidents_dir: Optional[Path] = None,
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
        self._incidents_dir = Path(incidents_dir) if incidents_dir else None
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

        self._deduper = Deduper(
            self._detection.dedup_window_sec,
            device_ts_window_sec=self._detection.device_ts_window_sec,
        )
        self._dispatch_lock = threading.Lock()
        self._dump_sem = threading.BoundedSemaphore(self._dumps_cfg.max_concurrent)
        self._event_counts: Dict[str, int] = {t: 0 for t in ALL_EVENT_TYPES}
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
                target=self._logcat_loop, daemon=True, name="logcat-collector",
            )
            self._logcat_thread.start()

        self._watcher_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="proc-watcher",
        )
        self._watcher_thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._global_stop.set()
        if self._logcat_stream is not None:
            self._logcat_stream.stop()
        for t in (self._logcat_thread, self._watcher_thread):
            if t is not None:
                t.join(timeout=join_timeout)

    # ------------------------------------------------------------------

    def current_processes(self) -> List[Process]:
        with self._procs_lock:
            return list(self._procs.values())

    def event_counts(self) -> Dict[str, int]:
        with self._event_counts_lock:
            return dict(self._event_counts)

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
        )

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
        try:
            self._logcat_stream = self._logcat_stream_factory()
        except Exception:
            log.exception("logcat stream factory failed; logcat pipeline disabled")
            return
        try:
            for line in self._logcat_stream.lines():
                if self._global_stop.is_set():
                    break
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
            # End-of-stream: flush any in-progress block.
            for event in parser.flush():
                self._dispatch(event)
        finally:
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
        self, event: str, process: Process, *, old_pid: int, gap_sec: float,
    ) -> None:
        if self._lifecycle_writer is None:
            return
        self._lifecycle_writer.write_row({
            "timestamp": self._now_iso(),
            "process_name": process.name,
            "event": event,
            "old_pid": old_pid,
            "new_pid": 0 if event == "gone" else process.pid,
            "gap_sec": round(gap_sec, 3),
        })

    # ── dispatcher ──

    def _dispatch(self, event: StabilityEvent) -> None:
        with self._dispatch_lock:
            if not self._deduper.observe(event, self._now_sec()):
                return
            with self._event_counts_lock:
                cap = self._dumps_cfg.max_incidents_per_type
                if self._event_counts.get(event.event_type, 0) >= cap:
                    log.warning("max incidents (%d) reached for %s; dropping",
                                cap, event.event_type)
                    return
                self._event_counts[event.event_type] = (
                    self._event_counts.get(event.event_type, 0) + 1
                )
            self._write_event_row(event)

        t = threading.Thread(
            target=self._run_dump,
            args=(event,),
            daemon=True,
            name=f"dump-{event.event_type}-{event.pid}",
        )
        t.start()

    def _write_event_row(self, event: StabilityEvent) -> None:
        if self._events_writer is None:
            return
        try:
            self._events_writer.write_row({
                "timestamp": event.triggered_at,
                "event_type": event.event_type,
                "process_name": event.process,
                "pid": event.pid,
                "severity": event.severity,
                "summary": event.summary[:500],
            })
        except Exception:
            log.exception("events writer failed")

    def _run_dump(self, event: StabilityEvent) -> None:
        if self._incidents_dir is None:
            return
        if not self._dump_sem.acquire(timeout=60.0):
            log.warning("dump semaphore wait timed out for %s/%s",
                        event.event_type, event.process)
            return
        try:
            if event.event_type == EVENT_JAVA_CRASH:
                self._java_crash_dump(self._adb, event, self._incidents_dir)
            elif event.event_type == EVENT_NATIVE_CRASH:
                self._native_crash_dump(
                    self._adb, event, self._incidents_dir,
                    pull_tombstone=self._dumps_cfg.pull_tombstone,
                )
            elif event.event_type == EVENT_ANR:
                self._anr_dump(
                    self._adb, event, self._incidents_dir,
                    pull_anr_trace=self._dumps_cfg.pull_anr_trace,
                )
            elif event.event_type == EVENT_PROCESS_DEATH:
                self._proc_death_dump(self._adb, event, self._incidents_dir)
        except Exception:
            log.exception("dumper failed for %s/%s",
                          event.event_type, event.process)
        finally:
            self._dump_sem.release()

    def _record_sample_failure(self, source: str) -> None:
        with self._event_counts_lock:
            self._sample_failures[source] = self._sample_failures.get(source, 0) + 1
