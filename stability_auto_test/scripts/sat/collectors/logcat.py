"""Logcat long-lived stream collector.

Spawns `adb logcat -v threadtime -b <buffers>` as a subprocess and yields
lines as they arrive. On disconnect (process exit, pipe broken), backs off and
reconnects, supplying `-T '<last_device_ts>'` so we don't re-replay history.

Reliability semantics (spec S1-04 / IMP-06):

- stderr is drained by a bounded background reader (an ADB that keeps printing
  errors can never fill the stderr pipe and deadlock the collector);
- a connection only counts as *collecting* after its first parsed line — a
  process that stays alive without producing output contributes no coverage;
- a heartbeat timeout marks silent connections stale, kills them and
  reconnects, so "alive but mute" logcat can never be reported as coverage.

The collector is `subprocess`-based (not adb-class-based) because `adb logcat`
without `-d` runs forever and Adb.run() is bounded by a per-call timeout. We
mirror Adb's serial handling here so multi-device runs work.
"""

from __future__ import annotations

import collections
import logging
import queue
import re
import subprocess
import threading
import time
from typing import Callable, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)

DEFAULT_BUFFERS: Sequence[str] = ("main", "system", "events", "crash")
RECONNECT_BACKOFF_BASE_SEC = 2.0
RECONNECT_BACKOFF_MAX_SEC = 30.0
STALE_HEARTBEAT_SEC = 60.0
STDERR_BUFFER_LINES = 200
STDOUT_QUEUE_LINES = 500

_TS_PREFIX_RE = re.compile(r"^(?P<ts>(?:\d{4}-)?\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})")


def _extract_device_ts(line: str) -> Optional[str]:
    m = _TS_PREFIX_RE.match(line)
    return m.group("ts") if m else None


class LogcatStream:
    def __init__(
        self,
        *,
        serial: Optional[str],
        adb_path: str = "adb",
        buffers: Sequence[str] = DEFAULT_BUFFERS,
        reconnect_backoff_sec: float = RECONNECT_BACKOFF_BASE_SEC,
        stale_sec: float = STALE_HEARTBEAT_SEC,
        popen_fn: Optional[Callable] = None,
        now_fn: Callable[[], float] = time.time,
        initial_device_ts: Optional[str] = None,
    ) -> None:
        self.serial = serial
        self.adb_path = adb_path
        self.buffers = list(buffers)
        self.reconnect_backoff = reconnect_backoff_sec
        self.stale_sec = float(stale_sec)
        self._popen = popen_fn or subprocess.Popen
        self._now = now_fn
        self._initial_device_ts = initial_device_ts

        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._last_device_ts: Optional[str] = None
        self._reconnects: int = 0
        self._lines_read: int = 0
        self._read_failures: int = 0
        self._stale_events: int = 0
        self._started_at: Optional[float] = None
        self._ended_at: Optional[float] = None
        # Collecting starts at the first parsed line, not at Popen success.
        self._conn_started_at: Optional[float] = None
        self._conn_spawned_at: Optional[float] = None
        self._gap_started_at: Optional[float] = None
        self._up_intervals: List[tuple] = []
        self._gap_intervals: List[tuple] = []
        self._last_success_host_ts: Optional[float] = None
        self._first_line_delays: List[float] = []
        self._stderr_lines: collections.deque = collections.deque(
            maxlen=STDERR_BUFFER_LINES,
        )
        self._stderr_bytes = 0
        self._backlog_peak = 0

    @property
    def stats(self) -> dict:
        return {
            "lines_read": self._lines_read,
            "reconnects": self._reconnects,
            "read_failures": self._read_failures,
            "stale_events": self._stale_events,
            "stderr_lines": len(self._stderr_lines),
            "stderr_bytes": self._stderr_bytes,
            "last_success_host_ts": self._last_success_host_ts,
            "last_device_ts": self._last_device_ts,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "up_intervals": list(self._up_intervals),
            "gap_intervals": list(self._gap_intervals),
            "first_line_delays": list(self._first_line_delays),
            "backlog_peak": self._backlog_peak,
        }

    def stop(self) -> None:
        self._stop.set()
        now = self._now()
        if self._conn_started_at is not None:
            self._up_intervals.append((self._conn_started_at, now))
            self._conn_started_at = None
        if self._gap_started_at is not None:
            self._gap_intervals.append((self._gap_started_at, now))
            self._gap_started_at = None
        self._ended_at = now
        self._kill_proc()

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._proc = None

    def _build_cmd(self) -> List[str]:
        cmd: List[str] = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += ["logcat", "-v", "threadtime"]
        for b in self.buffers:
            cmd += ["-b", b]
        resume_ts = self._last_device_ts or self._initial_device_ts
        if resume_ts is not None:
            # logcat -T '<ts>' resumes from the given device-side timestamp.
            cmd += ["-T", resume_ts]
        return cmd

    def _spawn(self, cmd: List[str]) -> Optional[subprocess.Popen]:
        try:
            proc = self._popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            log.error("adb not found at: %s", self.adb_path)
            return None
        except Exception:
            log.exception("logcat spawn failed; backing off")
            return None
        self._proc = proc
        return proc

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Bounded stderr drainer: consumes the pipe so it can never fill."""
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                if self._stop.is_set():
                    break
                self._stderr_lines.append(line.rstrip("\n"))
                self._stderr_bytes = min(
                    64 * 1024 * 1024,
                    self._stderr_bytes + len(line),
                )
        except Exception:
            pass

    def lines(self) -> Iterable[str]:
        """Yield logcat lines forever until stop().

        A connection only counts as collecting once its first line arrived;
        silent connections are killed after `stale_sec` and reconnected.
        """
        backoff = self.reconnect_backoff
        while not self._stop.is_set():
            cmd = self._build_cmd()
            log.info("starting logcat: %s", " ".join(cmd))
            proc = self._spawn(cmd)
            if proc is None:
                if self._stop.is_set():
                    return
                self._sleep_backoff(backoff)
                backoff = min(RECONNECT_BACKOFF_MAX_SEC, backoff * 2)
                continue
            if proc.stdout is None or proc.stderr is None:
                self._kill_proc()
                self._sleep_backoff(backoff)
                continue

            backoff = self.reconnect_backoff  # successful spawn → reset backoff
            if self._started_at is None:
                self._started_at = self._now()
            if self._gap_started_at is not None:
                self._gap_intervals.append((self._gap_started_at, self._now()))
                self._gap_started_at = None
            self._conn_spawned_at = self._now()
            self._conn_started_at = None  # collecting begins at first line

            q: queue.Queue = queue.Queue(maxsize=STDOUT_QUEUE_LINES)
            read_failed = threading.Event()
            stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(proc,),
                daemon=True,
                name="logcat-stderr-drain",
            )
            stderr_thread.start()

            def _read_stdout() -> None:
                try:
                    for line in proc.stdout:
                        if self._stop.is_set():
                            break
                        q.put(line.rstrip("\n"))
                except Exception:
                    read_failed.set()

            reader = threading.Thread(
                target=_read_stdout,
                daemon=True,
                name="logcat-stdout-reader",
            )
            reader.start()

            stale = False
            try:
                while not self._stop.is_set():
                    wait_sec = min(0.5, max(0.05, self.stale_sec))
                    try:
                        line = q.get(timeout=wait_sec)
                    except queue.Empty:
                        line = None
                    if line is None:
                        if read_failed.is_set() or not reader.is_alive():
                            # Connection died; drain any buffered tail.
                            try:
                                line = q.get_nowait()
                            except queue.Empty:
                                break
                        if line is not None:
                            pass  # tail line to process below
                        elif proc.poll() is not None:
                            try:
                                line = q.get_nowait()
                            except queue.Empty:
                                break
                        elif self._last_success_host_ts is None:
                            # No first line yet: stale if spawn is old.
                            if (
                                self._conn_spawned_at is not None
                                and self._now() - self._conn_spawned_at >= self.stale_sec
                            ):
                                self._stale_events += 1
                                log.warning(
                                    "logcat produced no first line for %.1fs; treating as stale",
                                    self.stale_sec,
                                )
                                stale = True
                                self._kill_proc()
                                break
                            continue
                        elif self._now() - self._last_success_host_ts >= self.stale_sec:
                            # Process alive but silent: stale connection.
                            self._stale_events += 1
                            log.warning(
                                "logcat silent for %.1fs; treating as stale",
                                self.stale_sec,
                            )
                            stale = True
                            self._kill_proc()
                            break
                        else:
                            continue
                    q.task_done()
                    if q.qsize() > self._backlog_peak:
                        self._backlog_peak = q.qsize()
                    self._lines_read += 1
                    ts = _extract_device_ts(line)
                    if ts is not None:
                        self._last_device_ts = ts
                    if self._conn_started_at is None:
                        # First line: this connection is now collecting.
                        self._conn_started_at = self._now()
                        if self._conn_spawned_at is not None:
                            self._first_line_delays.append(
                                self._conn_started_at - self._conn_spawned_at
                            )
                    self._last_success_host_ts = self._now()
                    yield line
            except Exception:
                self._read_failures += 1
                log.exception("error reading logcat; will reconnect")
            finally:
                self._kill_proc()
                reader.join(timeout=1.0)
                stderr_thread.join(timeout=1.0)

            now = self._now()
            if self._conn_started_at is not None:
                self._up_intervals.append((self._conn_started_at, now))
                self._conn_started_at = None
            # A connection that never produced a line contributed no coverage:
            # record the whole spawn window as a gap.
            if not self._stop.is_set():
                self._gap_started_at = now

            if self._stop.is_set() and not stale:
                return
            self._reconnects += 1
            log.warning(
                "logcat ended (stale=%s); reconnecting in %.1fs (n=%d)",
                stale,
                backoff,
                self._reconnects,
            )
            self._sleep_backoff(backoff)
            backoff = min(RECONNECT_BACKOFF_MAX_SEC, backoff * 2)

    def _sleep_backoff(self, sec: float) -> None:
        self._stop.wait(sec)
