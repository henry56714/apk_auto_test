"""External workload: run an arbitrary command (Maestro / Appium / script)."""

from __future__ import annotations

import collections
import shlex
import subprocess
import threading
import time
from typing import List, Optional

from ..utils import utc_now_iso
from .base import Workload, WorkloadResult

SENSITIVE_MARKERS = ("TOKEN", "PASSWORD", "PASSWD", "SECRET", "KEY", "AUTH")

# Bounded capture: multi-MiB output must never deadlock the pipe (IMP-08).
MAX_CAPTURED_LINES = 5000
MAX_CAPTURED_BYTES = 1024 * 1024


def _filter_env(args: List[str]) -> List[str]:
    out = []
    for i, arg in enumerate(args):
        if "=" in arg:
            key = arg.split("=", 1)[0].upper()
            if any(m in key for m in SENSITIVE_MARKERS):
                out.append(f"{arg.split('=', 1)[0]}=***")
                continue
        out.append(arg)
    return out


class ExternalWorkload(Workload):
    name = "external"

    def __init__(self, command: str, *, timeout_sec: float = 300.0):
        super().__init__()
        self.argv = shlex.split(command)
        self.timeout_sec = float(timeout_sec)
        self._proc: Optional[subprocess.Popen] = None
        self.output_lines: collections.deque = collections.deque(
            maxlen=MAX_CAPTURED_LINES,
        )
        self.output_bytes = 0
        self.output_truncated = False

    def _drain_output(self, proc: subprocess.Popen) -> None:
        """Consume stdout continuously so the pipe can never fill."""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self.output_bytes + len(line) > MAX_CAPTURED_BYTES:
                    self.output_truncated = True
                    continue
                self.output_bytes += len(line)
                self.output_lines.append(line.rstrip("\n"))
        except Exception:
            pass

    def run(self) -> WorkloadResult:
        self._started = utc_now_iso()
        self._proc = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        drainer = threading.Thread(
            target=self._drain_output,
            args=(self._proc,),
            daemon=True,
            name="workload-output-drain",
        )
        drainer.start()
        deadline = time.monotonic() + self.timeout_sec
        while time.monotonic() < deadline:
            rc = self._proc.poll()
            if rc is not None:
                break
            if self._stop.is_set():
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                drainer.join(timeout=1.0)
                self._ended = utc_now_iso()
                return WorkloadResult(
                    status="interrupted",
                    exit_code=130,
                    started_at=self._started,
                    ended_at=self._ended,
                )
            time.sleep(0.1)
        else:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            drainer.join(timeout=1.0)
            self._ended = utc_now_iso()
            return WorkloadResult(
                status="failed",
                exit_code=124,
                message="external command timed out",
                started_at=self._started,
                ended_at=self._ended,
            )
        drainer.join(timeout=2.0)
        self._ended = utc_now_iso()
        status = "ok" if rc == 0 else "failed"
        tail = "\n".join(list(self.output_lines)[-5:])
        return WorkloadResult(
            status=status,
            exit_code=rc or 0,
            started_at=self._started,
            ended_at=self._ended,
            message=(tail[:400] if rc != 0 else ""),
        )

    def stop(self) -> None:
        self._stop.set()

    def cleanup(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def manifest(self) -> dict:
        return {
            "type": self.name,
            "command_template": _filter_env(list(self.argv)),
            "timeout_sec": self.timeout_sec,
        }
