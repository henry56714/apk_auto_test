"""Disk quota and log-retention guards for long runs."""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class QuotaConfig:
    max_disk_bytes: Optional[int] = None
    max_log_file_bytes: int = 512 * 1024 * 1024
    log_retention_hours: int = 24
    max_queue_size: int = 50
    evidence_sample_every_n: int = 5


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

    def disk_state(self) -> dict:
        try:
            usage = self._disk_usage(self.output_dir)
            free = int(usage.free)
        except OSError:
            return {"free_bytes": None, "hard_reached": False, "soft_warning": False}
        hard = (
            self.config.max_disk_bytes is not None
            and free < self.config.max_disk_bytes
        )
        soft = hard or (
            self.config.max_disk_bytes is not None
            and free < self.config.max_disk_bytes * 1.2
        )
        if soft and not self._soft_warned:
            log.warning("disk soft quota warning: %d bytes free", free)
            self._soft_warned = True
        return {
            "free_bytes": free,
            "hard_reached": hard,
            "soft_warning": soft,
        }

    @property
    def hard_reached(self) -> bool:
        return bool(self.disk_state()["hard_reached"])

    def enforce_log_retention(self) -> int:
        """Delete logcat files older than the retention window; return count."""
        if self.config.log_retention_hours <= 0:
            return 0
        cutoff = self._now() - self.config.log_retention_hours * 3600
        removed = 0
        for path in self.output_dir.glob("logcat_*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
