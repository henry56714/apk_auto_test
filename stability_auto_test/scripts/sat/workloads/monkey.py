"""Monkey workload with deterministic seed."""

from __future__ import annotations

from ..adb import Adb, AdbError
from ..utils import utc_now_iso
from .base import Workload, WorkloadResult


class MonkeyWorkload(Workload):
    name = "monkey"

    def __init__(
        self,
        adb: Adb,
        package: str,
        *,
        seed: int = 0,
        event_count: int = 1000,
        throttle_ms: int = 50,
    ):
        super().__init__()
        self.adb = adb
        self.package = package
        self.seed = int(seed)
        self.event_count = int(event_count)
        self.throttle_ms = int(throttle_ms)

    def _cmd(self) -> str:
        return (
            f"monkey -p {self.package} -s {self.seed} "
            f"--throttle {self.throttle_ms} -v {self.event_count}"
        )

    def run(self) -> WorkloadResult:
        self._started = utc_now_iso()
        try:
            r = self.adb.shell(self._cmd(), check=False, timeout=1800.0)
            rc = r.returncode
        except AdbError as e:
            rc = 1
            self._ended = utc_now_iso()
            return WorkloadResult(
                status="failed", exit_code=rc, message=str(e),
                started_at=self._started, ended_at=self._ended,
            )
        self._ended = utc_now_iso()
        status = "ok" if rc == 0 else "failed"
        return WorkloadResult(
            status=status, exit_code=rc,
            started_at=self._started, ended_at=self._ended,
        )

    def manifest(self) -> dict:
        return {
            "type": self.name,
            "package": self.package,
            "seed": self.seed,
            "event_count": self.event_count,
            "throttle_ms": self.throttle_ms,
            "command": self._cmd(),
        }
