"""Logcat long-lived stream collector.

Spawns `adb logcat -v threadtime -b <buffers>` as a subprocess and yields
lines as they arrive. On disconnect (process exit, pipe broken), backs off and
reconnects, supplying `-T '<last_device_ts>'` so we don't re-replay history.

The collector is `subprocess`-based (not adb-class-based) because `adb logcat`
without `-d` runs forever and Adb.run() is bounded by a per-call timeout. We
mirror Adb's serial handling here so multi-device runs work.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import Callable, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)

DEFAULT_BUFFERS: Sequence[str] = ("main", "system", "events", "crash")
RECONNECT_BACKOFF_BASE_SEC = 2.0
RECONNECT_BACKOFF_MAX_SEC = 30.0

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
        popen_fn: Optional[Callable] = None,
    ) -> None:
        self.serial = serial
        self.adb_path = adb_path
        self.buffers = list(buffers)
        self.reconnect_backoff = reconnect_backoff_sec
        self._popen = popen_fn or subprocess.Popen

        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._last_device_ts: Optional[str] = None
        self._reconnects: int = 0
        self._lines_read: int = 0
        self._read_failures: int = 0

    @property
    def stats(self) -> dict:
        return {
            "lines_read": self._lines_read,
            "reconnects": self._reconnects,
            "read_failures": self._read_failures,
            "last_device_ts": self._last_device_ts,
        }

    def stop(self) -> None:
        self._stop.set()
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
        if self._last_device_ts is not None:
            # logcat -T '<ts>' resumes from the given device-side timestamp.
            cmd += ["-T", self._last_device_ts]
        return cmd

    def lines(self) -> Iterable[str]:
        """Yield logcat lines forever until stop()."""
        backoff = self.reconnect_backoff
        while not self._stop.is_set():
            cmd = self._build_cmd()
            log.info("starting logcat: %s", " ".join(cmd))
            try:
                self._proc = self._popen(
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
                return
            except Exception:
                log.exception("logcat spawn failed; backing off")
                self._sleep_backoff(backoff)
                backoff = min(RECONNECT_BACKOFF_MAX_SEC, backoff * 2)
                continue

            assert self._proc is not None
            stdout = self._proc.stdout
            if stdout is None:
                self._kill_proc()
                self._sleep_backoff(backoff)
                continue

            backoff = self.reconnect_backoff  # successful spawn → reset backoff
            try:
                for line in stdout:
                    if self._stop.is_set():
                        break
                    self._lines_read += 1
                    ts = _extract_device_ts(line)
                    if ts is not None:
                        self._last_device_ts = ts
                    yield line.rstrip("\n")
            except Exception:
                self._read_failures += 1
                log.exception("error reading logcat; will reconnect")
            finally:
                self._kill_proc()

            if self._stop.is_set():
                return
            self._reconnects += 1
            log.warning("logcat ended; reconnecting in %.1fs (n=%d)", backoff, self._reconnects)
            self._sleep_backoff(backoff)
            backoff = min(RECONNECT_BACKOFF_MAX_SEC, backoff * 2)

    def _sleep_backoff(self, sec: float) -> None:
        self._stop.wait(sec)
