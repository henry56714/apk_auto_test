"""ADB subprocess wrapper.

Design goals:
- Survive 24h runs with intermittent disconnects (retry + backoff).
- Bound concurrency so N collector threads don't overwhelm the adb server.
- Hard timeout per call so a hung shell can't block the whole pipeline.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_CONCURRENCY = 4


class AdbError(RuntimeError):
    pass


class AdbTimeout(AdbError):
    pass


class AdbNotFound(AdbError):
    pass


@dataclass
class AdbResult:
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float


class Adb:
    def __init__(
        self,
        serial: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        concurrency: int = DEFAULT_CONCURRENCY,
        adb_path: str = "adb",
    ) -> None:
        self.serial = serial
        self.timeout = timeout
        self.retries = retries
        self.adb_path = adb_path
        self._sem = threading.BoundedSemaphore(concurrency)

    def _base_cmd(self) -> List[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(
        self,
        args: Iterable[str],
        *,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
        check: bool = True,
    ) -> AdbResult:
        timeout_v = self.timeout if timeout is None else timeout
        retries_v = self.retries if retries is None else retries
        cmd = self._base_cmd() + list(args)

        last_exc: Optional[Exception] = None
        for attempt in range(retries_v + 1):
            with self._sem:
                start = time.monotonic()
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=timeout_v,
                    )
                except subprocess.TimeoutExpired:
                    last_exc = AdbTimeout(
                        f"adb timed out after {timeout_v}s: {' '.join(cmd)}"
                    )
                except FileNotFoundError as e:
                    raise AdbNotFound(f"adb not found at: {self.adb_path}") from e
                else:
                    result = AdbResult(
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        duration_sec=time.monotonic() - start,
                    )
                    if result.returncode == 0 or not check:
                        return result
                    err = (result.stderr or result.stdout or "").strip()
                    last_exc = AdbError(
                        f"adb failed (rc={result.returncode}): {err[:200]}"
                    )

            if attempt < retries_v:
                backoff = DEFAULT_BACKOFF_BASE * (2 ** attempt)
                log.warning(
                    "adb attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1, retries_v + 1, last_exc, backoff,
                )
                time.sleep(backoff)

        assert last_exc is not None
        raise last_exc

    def shell(self, command: str, **kwargs) -> AdbResult:
        return self.run(["shell", command], **kwargs)

    def pull(self, remote: str, local: str, **kwargs) -> AdbResult:
        return self.run(["pull", remote, local], **kwargs)

    def list_devices(self) -> List[str]:
        """Return list of `<serial>\\t<state>` lines, excluding header."""
        r = self.run(["devices"], retries=0)
        out: List[str] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            out.append(line)
        return out
