"""Bounded dump queue + evidence sampling strategy."""

from __future__ import annotations

import threading
from typing import Dict


class BackpressureController:
    def __init__(self, max_queue_size: int = 50) -> None:
        self.max_queue_size = max(1, int(max_queue_size))
        self._lock = threading.Lock()
        self._queued = 0
        self._dropped = 0

    def try_acquire(self) -> bool:
        with self._lock:
            if self._queued >= self.max_queue_size:
                self._dropped += 1
                return False
            self._queued += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._queued = max(0, self._queued - 1)

    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def queued_count(self) -> int:
        with self._lock:
            return self._queued


class EvidenceSampler:
    """`first_full_then_every_n`: full evidence for the first occurrence,
    occurrence-only for repeats, full again every N."""

    def __init__(self, every_n: int = 5) -> None:
        self.every_n = max(1, int(every_n))
        self._counts: Dict[str, int] = {}

    def decide(self, fingerprint: str) -> str:
        n = self._counts.get(fingerprint, 0) + 1
        self._counts[fingerprint] = n
        if n == 1 or n % self.every_n == 0:
            return "full"
        return "occurrence_only"
