"""External workload: run an arbitrary command (Maestro / Appium / script)."""

from __future__ import annotations

import shlex
import subprocess
import time
from typing import List, Optional

from ..utils import utc_now_iso
from .base import Workload, WorkloadResult

SENSITIVE_MARKERS = ("TOKEN", "PASSWORD", "PASSWD", "SECRET", "KEY", "AUTH")


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

    def run(self) -> WorkloadResult:
        self._started = utc_now_iso()
        self._proc = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
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
                self._ended = utc_now_iso()
                return WorkloadResult(
                    status="interrupted", exit_code=130,
                    started_at=self._started, ended_at=self._ended,
                )
            time.sleep(0.1)
        else:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._ended = utc_now_iso()
            return WorkloadResult(
                status="failed", exit_code=124, message="external command timed out",
                started_at=self._started, ended_at=self._ended,
            )
        self._ended = utc_now_iso()
        status = "ok" if rc == 0 else "failed"
        return WorkloadResult(
            status=status, exit_code=rc or 0,
            started_at=self._started, ended_at=self._ended,
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
