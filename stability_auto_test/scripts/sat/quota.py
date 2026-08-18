"""Disk quota and log-retention guards for long runs (spec S1-05 / IMP-12).

Quotas are split into three orthogonal budgets:

- `min_free_bytes`  — keep at least this much free on the output filesystem;
- `max_run_bytes`   — cap the run's own output directory size;
- `max_file_bytes`  — per-file size cap (writers rotate on it);

plus a periodic retention window for old logcat files. Every deletion is
audited (count + bytes) so long-run disk behaviour is explainable.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)


@dataclass
class QuotaConfig:
    min_free_bytes: Optional[int] = None
    max_run_bytes: Optional[int] = None
    max_file_bytes: int = 512 * 1024 * 1024
    log_retention_hours: int = 24
    max_queue_size: int = 50
    evidence_sample_every_n: int = 5
    # Deprecated alias: kept so older call sites/tests keep working.
    max_disk_bytes: Optional[int] = None

    @property
    def effective_min_free_bytes(self) -> Optional[int]:
        return self.min_free_bytes if self.min_free_bytes is not None else self.max_disk_bytes


class QuotaTracker:
    def __init__(
        self,
        output_dir: Path,
        config: QuotaConfig,
        *,
        now_sec_fn: Callable[[], float] = time.time,
        disk_usage_fn: Callable = shutil.disk_usage,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.config = config
        self._now = now_sec_fn
        self._disk_usage = disk_usage_fn
        self._soft_warned = False
        # Deletion audit (IMP-12: retention must leave a trace).
        self.audit: List[dict] = []

    def run_bytes(self) -> int:
        total = 0
        try:
            for p in self.output_dir.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        except OSError:
            pass
        return total

    def disk_state(self) -> dict:
        try:
            usage = self._disk_usage(self.output_dir)
            free = int(usage.free)
        except OSError:
            free = None
        if free is None:
            return {"free_bytes": None, "hard_reached": False, "soft_warning": False}

        min_free = self.config.effective_min_free_bytes
        hard_free = min_free is not None and free < min_free
        soft_free = min_free is not None and free < min_free * 1.2
        run_bytes = self.run_bytes()
        hard_run = self.config.max_run_bytes is not None and run_bytes >= self.config.max_run_bytes
        soft_run = (
            self.config.max_run_bytes is not None and run_bytes >= self.config.max_run_bytes * 0.9
        )
        hard = hard_free or hard_run
        soft = soft_free or soft_run or hard
        if soft and not self._soft_warned:
            log.warning(
                "disk quota warning: free=%s run_bytes=%d",
                free,
                run_bytes,
            )
            self._soft_warned = True
        return {
            "free_bytes": free,
            "run_bytes": run_bytes,
            "hard_reached": hard,
            "soft_warning": soft,
        }

    @property
    def hard_reached(self) -> bool:
        return bool(self.disk_state()["hard_reached"])

    def enforce_log_retention(self) -> int:
        """Delete logcat files older than the retention window; audited."""
        if self.config.log_retention_hours <= 0:
            return 0
        cutoff = self._now() - self.config.log_retention_hours * 3600
        removed = 0
        for path in self.output_dir.glob("logcat_*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    self.audit.append(
                        {
                            "path": path.name,
                            "reason": "retention",
                            "bytes": size,
                            "at": self._now(),
                        }
                    )
            except OSError:
                continue
        if removed:
            log.info("log retention removed %d file(s)", removed)
        return removed
