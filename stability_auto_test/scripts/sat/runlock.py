"""Run-directory lock with stale-lock detection for `sat recover`."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .atomic_io import atomic_write_json
from .utils import utc_now_iso

LOCK_FILENAME = ".sat-run.lock"


class RunLockError(RuntimeError):
    pass


@dataclass
class RunLockInfo:
    run_id: str
    pid: int
    device: Optional[str]
    started_at: str

    @property
    def alive(self) -> bool:
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        return True


def read_lock(output_dir: Path) -> Optional[RunLockInfo]:
    path = Path(output_dir) / LOCK_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return RunLockInfo(run_id="?", pid=0, device=None, started_at="?")
    return RunLockInfo(
        run_id=str(data.get("run_id", "?")),
        pid=int(data.get("pid", 0)),
        device=data.get("device"),
        started_at=str(data.get("started_at", "?")),
    )


class RunLock:
    def __init__(
        self,
        output_dir: Path,
        *,
        run_id: str,
        device: Optional[str],
    ) -> None:
        self.output_dir = Path(output_dir)
        self.path = self.output_dir / LOCK_FILENAME
        self.run_id = run_id
        self.device = device
        self._held = False

    def acquire(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        existing = read_lock(self.output_dir)
        if existing is not None and existing.alive and existing.pid != os.getpid():
            raise RunLockError(
                f"output directory {self.output_dir} is locked by run "
                f"{existing.run_id} (pid {existing.pid}); remove "
                f"{self.path.name} only for recovery scenarios"
            )
        payload = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "device": self.device,
            "started_at": utc_now_iso(),
        }
        atomic_write_json(self.path, payload)
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        self._held = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def check_recoverable(output_dir: Path) -> None:
    """Raise RunLockError when the directory is locked by a live process."""
    lock = read_lock(output_dir)
    if lock is not None and lock.alive and lock.pid != os.getpid():
        raise RunLockError(
            f"cannot recover {output_dir}: run {lock.run_id} is still active "
            f"(pid {lock.pid})"
        )


def clear_stale_lock(output_dir: Path) -> bool:
    """Remove a stale lock; returns True when a stale lock was removed."""
    lock = read_lock(output_dir)
    if lock is None or lock.alive:
        return False
    try:
        (Path(output_dir) / LOCK_FILENAME).unlink(missing_ok=True)
        return True
    except OSError:
        return False
