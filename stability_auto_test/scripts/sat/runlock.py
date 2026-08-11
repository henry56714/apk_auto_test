"""Run-directory lock with stale-lock detection for `sat recover`."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
        import errno
        import tempfile

        # Write the payload to a temp file first, fsync, then atomically
        # hard-link it to the lock path.  os.link fails with EEXIST when the
        # lock already exists, making the acquire atomic without a window
        # where the lock file is visible but empty.
        payload = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "device": self.device,
            "started_at": utc_now_iso(),
        }
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(self.output_dir),
            prefix=".sat-run-lock.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp_name, self.path)
            except OSError as e:
                if e.errno == errno.EEXIST:
                    existing = read_lock(self.output_dir)
                    if existing is not None and existing.alive and existing.pid != os.getpid():
                        raise RunLockError(
                            f"output directory {self.output_dir} is locked by "
                            f"run {existing.run_id} (pid {existing.pid}); "
                            f"remove {self.path.name} only for recovery "
                            f"scenarios"
                        ) from e
                    # Stale lock: remove and retry once.
                    clear_stale_lock(self.output_dir)
                    try:
                        os.link(tmp_name, self.path)
                    except OSError as e2:
                        raise RunLockError(
                            f"cannot acquire lock on {self.output_dir}: {e2}"
                        ) from e2
                else:
                    raise RunLockError(f"cannot acquire lock on {self.output_dir}: {e}") from e
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
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
            f"cannot recover {output_dir}: run {lock.run_id} is still active (pid {lock.pid})"
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
