"""Collector pool: per-process workers + dynamic re-discovery + alert/dump triggering.

A background watcher periodically calls `discovery.discover(adb, package)`
and reconciles the set of monitored processes. Each worker's sample feeds a
per-process threshold tracker (one for CPU, one for Mem). On state transition
to ALERTING the pool launches a dump worker that writes evidence files into
`incidents/` and a structured incident JSON (the AI/reporter source).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from . import discovery
from .adb import Adb
from .alerting import AlertEvent, ThresholdConfig, ThresholdTracker
from .collectors import cpu as cpu_mod
from .collectors import memory as mem_mod
from .collectors.cpu import CpuPercentCalculator
from .discovery import Process
from .dumpers import heap as heap_dumper
from .dumpers import thread_cpu as thread_cpu_dumper
from .storage import CsvStreamWriter
from .utils import utc_now_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThresholdsBundle:
    cpu: ThresholdConfig
    mem: ThresholdConfig


@dataclass(frozen=True)
class DumpsConfig:
    enable_heap: bool = True
    max_cpu_dumps: int = 50
    max_heap_dumps: int = 20
    max_concurrent: int = 2


def default_thresholds(
    *,
    cpu_percent: float = 80.0,
    cpu_sustain_sec: float = 60.0,
    cpu_cooldown_sec: float = 300.0,
    mem_pss_mb: float = 500.0,
    mem_sustain_sec: float = 120.0,
    mem_cooldown_sec: float = 600.0,
) -> ThresholdsBundle:
    return ThresholdsBundle(
        cpu=ThresholdConfig("cpu_pct", cpu_percent, cpu_sustain_sec, cpu_cooldown_sec),
        mem=ThresholdConfig("mem_pss_mb", mem_pss_mb, mem_sustain_sec, mem_cooldown_sec),
    )


@dataclass
class _Worker:
    process: Process
    stop: threading.Event
    cpu_thread: threading.Thread
    mem_thread: threading.Thread
    cpu_tracker: ThresholdTracker
    mem_tracker: ThresholdTracker


def _normalize_filter(
    filter_list: Optional[Iterable[str]], package: str,
) -> Optional[Set[str]]:
    if not filter_list:
        return None
    out: Set[str] = set()
    for f in filter_list:
        f = (f or "").strip()
        if not f or f == "main":
            out.add(package)
        elif f.startswith(":"):
            out.add(package + f)
        else:
            out.add(f)
    return out


class CollectorPool:
    def __init__(
        self,
        adb: Adb,
        package: str,
        *,
        cpu_cores: int,
        cpu_writer: CsvStreamWriter,
        mem_writer: CsvStreamWriter,
        lifecycle_writer: Optional[CsvStreamWriter] = None,
        cpu_interval_sec: float = 1.0,
        mem_interval_sec: float = 5.0,
        rescan_interval_sec: float = 5.0,
        process_filter: Optional[Iterable[str]] = None,
        thresholds: Optional[ThresholdsBundle] = None,
        dumps: Optional[DumpsConfig] = None,
        incidents_dir: Optional[Path] = None,
        discover_fn: Optional[Callable[[Adb, str], List[Process]]] = None,
        cpu_dump_fn: Optional[Callable] = None,
        heap_dump_fn: Optional[Callable] = None,
    ) -> None:
        self._adb = adb
        self._package = package
        self._cores = cpu_cores
        self._cpu_writer = cpu_writer
        self._mem_writer = mem_writer
        self._lifecycle_writer = lifecycle_writer
        self._cpu_interval = cpu_interval_sec
        self._mem_interval = mem_interval_sec
        self._rescan_interval = rescan_interval_sec
        self._filter = _normalize_filter(process_filter, package)
        self._discover = discover_fn or discovery.discover
        self._thresholds = thresholds or default_thresholds()
        self._dumps_cfg = dumps or DumpsConfig()
        self._incidents_dir = Path(incidents_dir) if incidents_dir else None
        self._cpu_dump_fn = cpu_dump_fn or thread_cpu_dumper.run
        self._heap_dump_fn = heap_dump_fn or heap_dumper.run

        self._workers: Dict[str, _Worker] = {}
        self._workers_lock = threading.RLock()
        self._gone_at: Dict[str, float] = {}
        self._global_stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None

        self._dump_sem = threading.BoundedSemaphore(self._dumps_cfg.max_concurrent)
        self._dump_counts: Dict[str, int] = {"cpu": 0, "mem": 0}
        self._dump_lock = threading.Lock()

        self._sample_failures: Dict[str, Dict[str, int]] = {}
        self._sample_failure_lock = threading.Lock()

    def start(self, initial_processes: Iterable[Process] = ()) -> None:
        for p in initial_processes:
            if not self._passes_filter(p):
                continue
            with self._workers_lock:
                if p.name not in self._workers:
                    self._add_worker(p, lifecycle_event="new", old_pid=0, gap_sec=0.0)
        self._watcher = threading.Thread(
            target=self._watch_loop, daemon=True, name="pool-watcher",
        )
        self._watcher.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._global_stop.set()
        with self._workers_lock:
            workers = list(self._workers.values())
            for w in workers:
                w.stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=join_timeout)
        for w in workers:
            w.cpu_thread.join(timeout=join_timeout)
            w.mem_thread.join(timeout=join_timeout)

    def current_processes(self) -> List[Process]:
        with self._workers_lock:
            return [w.process for w in self._workers.values()]

    def dump_counts(self) -> Dict[str, int]:
        with self._dump_lock:
            return dict(self._dump_counts)

    def sample_failures(self) -> Dict[str, Dict[str, int]]:
        """Cumulative None-result counts per process per metric kind.

        A failure means the adb call timed out / returned an error / produced
        unparseable output. Useful for distinguishing 'no data because process
        is gone' (lifecycle gone) from 'no data because sampling is broken'.
        """
        with self._sample_failure_lock:
            return {k: dict(v) for k, v in self._sample_failures.items()}

    def _record_sample_failure(self, name: str, kind: str) -> None:
        with self._sample_failure_lock:
            d = self._sample_failures.setdefault(name, {"cpu": 0, "mem": 0})
            d[kind] += 1

    def _passes_filter(self, p: Process) -> bool:
        return self._filter is None or p.name in self._filter

    # -------- watcher loop --------

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

        with self._workers_lock:
            current_names = set(self._workers.keys())
            live_names = set(live_by_name.keys())

            for name in current_names - live_names:
                self._remove_worker(name, lifecycle_event="gone")
                self._gone_at[name] = time.time()

            for name in live_names:
                proc = live_by_name[name]
                if name in self._workers:
                    if self._workers[name].process.pid != proc.pid:
                        old_pid = self._workers[name].process.pid
                        self._remove_worker(name, lifecycle_event=None)
                        self._add_worker(
                            proc, lifecycle_event="restart",
                            old_pid=old_pid, gap_sec=0.0,
                        )
                else:
                    gap = 0.0
                    event = "new"
                    if name in self._gone_at:
                        gap = max(0.0, time.time() - self._gone_at.pop(name))
                        event = "restart"
                    self._add_worker(proc, lifecycle_event=event,
                                     old_pid=0, gap_sec=gap)

    # -------- worker management (caller holds _workers_lock) --------

    def _add_worker(
        self,
        process: Process,
        *,
        lifecycle_event: str,
        old_pid: int,
        gap_sec: float,
    ) -> None:
        stop = threading.Event()
        cpu_tracker = ThresholdTracker(self._thresholds.cpu)
        mem_tracker = ThresholdTracker(self._thresholds.mem)
        cpu_thread = threading.Thread(
            target=self._cpu_loop, args=(process, stop, cpu_tracker),
            daemon=True, name=f"cpu-{process.name}-{process.pid}",
        )
        mem_thread = threading.Thread(
            target=self._mem_loop, args=(process, stop, mem_tracker),
            daemon=True, name=f"mem-{process.name}-{process.pid}",
        )
        worker = _Worker(
            process=process, stop=stop,
            cpu_thread=cpu_thread, mem_thread=mem_thread,
            cpu_tracker=cpu_tracker, mem_tracker=mem_tracker,
        )
        self._workers[process.name] = worker
        cpu_thread.start()
        mem_thread.start()
        self._write_lifecycle(lifecycle_event, process,
                              old_pid=old_pid, gap_sec=gap_sec)
        log.info("worker added: %s pid=%d (event=%s)",
                 process.name, process.pid, lifecycle_event)

    def _remove_worker(
        self, name: str, *, lifecycle_event: Optional[str],
    ) -> None:
        w = self._workers.pop(name, None)
        if w is None:
            return
        w.stop.set()
        if lifecycle_event is not None:
            self._write_lifecycle(lifecycle_event, w.process,
                                  old_pid=w.process.pid, gap_sec=0.0)
        log.info("worker removed: %s pid=%d (event=%s)",
                 w.process.name, w.process.pid, lifecycle_event)

    def _write_lifecycle(
        self, event: str, process: Process, *, old_pid: int, gap_sec: float,
    ) -> None:
        if self._lifecycle_writer is None:
            return
        self._lifecycle_writer.write_row({
            "timestamp": utc_now_iso(),
            "process_name": process.name,
            "event": event,
            "old_pid": old_pid,
            "new_pid": 0 if event == "gone" else process.pid,
            "gap_sec": round(gap_sec, 3),
        })

    # -------- worker loops --------

    def _cpu_loop(
        self, process: Process, stop: threading.Event, tracker: ThresholdTracker,
    ) -> None:
        calc = CpuPercentCalculator(self._cores)
        while not stop.is_set() and not self._global_stop.is_set():
            try:
                s = cpu_mod.sample(self._adb, process.pid)
            except Exception:
                log.exception("cpu sample failed for pid=%d", process.pid)
                s = None
            if s is None:
                self._record_sample_failure(process.name, "cpu")
            else:
                pct = calc.update(s)
                if pct is not None:
                    ts = time.time()
                    self._cpu_writer.write_row({
                        "timestamp": utc_now_iso(),
                        "process_name": process.name,
                        "pid": process.pid,
                        "cpu_pct": round(pct, 2),
                    })
                    event = tracker.feed(ts, pct)
                    if event is not None:
                        self._fire_dump("cpu", process, event)
            if stop.wait(self._cpu_interval):
                break

    def _mem_loop(
        self, process: Process, stop: threading.Event, tracker: ThresholdTracker,
    ) -> None:
        while not stop.is_set() and not self._global_stop.is_set():
            try:
                s = mem_mod.sample(self._adb, process.pid)
            except Exception:
                log.exception("mem sample failed for pid=%d", process.pid)
                s = None
            if s is None:
                self._record_sample_failure(process.name, "mem")
            else:
                ts = time.time()
                self._mem_writer.write_row({
                    "timestamp": utc_now_iso(),
                    "process_name": process.name,
                    "pid": process.pid,
                    "pss_mb": round(s.total_pss_mb, 2),
                    "java_heap_mb": round(s.java_heap_pss_mb, 2),
                    "native_heap_mb": round(s.native_heap_pss_mb, 2),
                    "graphics_mb": round(s.graphics_pss_mb, 2),
                    "code_mb": round(s.code_pss_mb, 2),
                    "stack_mb": round(s.stack_pss_mb, 2),
                })
                event = tracker.feed(ts, s.total_pss_mb)
                if event is not None:
                    self._fire_dump("mem", process, event)
            if stop.wait(self._mem_interval):
                break

    # -------- dump triggering --------

    def _fire_dump(
        self, kind: str, process: Process, alert: AlertEvent,
    ) -> None:
        if self._incidents_dir is None:
            log.warning("alert fired but no incidents_dir configured; skipping dump")
            return
        cap = (self._dumps_cfg.max_cpu_dumps if kind == "cpu"
               else self._dumps_cfg.max_heap_dumps)
        with self._dump_lock:
            if self._dump_counts[kind] >= cap:
                log.warning("max %s dumps (%d) reached; skipping", kind, cap)
                return
            self._dump_counts[kind] += 1
        t = threading.Thread(
            target=self._run_dump,
            args=(kind, process, alert),
            daemon=True,
            name=f"dump-{kind}-{process.pid}",
        )
        t.start()

    def _run_dump(self, kind: str, process: Process, alert: AlertEvent) -> None:
        if not self._dump_sem.acquire(timeout=60.0):
            log.warning("dump semaphore wait timed out for %s/%s", kind, process.name)
            return
        try:
            if kind == "cpu":
                self._cpu_dump_fn(self._adb, process, alert, self._incidents_dir)
            else:
                self._heap_dump_fn(
                    self._adb, process, alert, self._incidents_dir,
                    enable_heap=self._dumps_cfg.enable_heap,
                )
        except Exception:
            log.exception("dump failed for %s/%s", kind, process.name)
        finally:
            self._dump_sem.release()
