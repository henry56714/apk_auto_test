"""Tool-self resource monitor (RSS / threads / fds / queue depth)."""

from __future__ import annotations

import json
import logging
import os
import resource
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

SELF_RESOURCE_FILENAME = "self_resource.jsonl"


class SelfMonitor:
    def __init__(
        self,
        output_dir: Path,
        *,
        interval_sec: float = 60.0,
        queue_depth_fn: Callable[[], int] = lambda: 0,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.interval_sec = float(interval_sec)
        self._queue_depth_fn = queue_depth_fn
        self._now = now_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._samples: List[Dict] = []
        self._lock = threading.Lock()
        self._path = self.output_dir / SELF_RESOURCE_FILENAME

    def _sample(self) -> Dict:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS reports ru_maxrss in bytes; Linux in KB.
        rss_kb = usage.ru_maxrss
        if sys.platform == "darwin":
            rss_kb = rss_kb // 1024
        try:
            if os.path.isdir("/proc/self/fd"):
                fd_count = len(os.listdir("/proc/self/fd"))
            else:
                fd_count = len(os.listdir("/dev/fd"))
        except OSError:
            fd_count = -1
        return {
            "ts": self._now(),
            "rss_kb": int(rss_kb),
            "threads": threading.active_count(),
            "fds": fd_count,
            "queue_depth": self._queue_depth_fn(),
        }

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="self-monitor",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self._sample()
                with self._lock:
                    self._samples.append(sample)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
            except Exception:
                log.exception("self-monitor sample failed")
            if self._stop.wait(self.interval_sec):
                break

    def summary(self) -> Dict:
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return {
                "samples": [],
                "rss_growth_kb": 0,
                "rss_peak_kb": 0,
                "threads_peak": 0,
                "fds_peak": 0,
                "queue_peak": 0,
            }
        rss = [s.get("rss_kb", 0) for s in samples]
        return {
            "samples": samples,
            "rss_growth_kb": max(0, rss[-1] - rss[0]),
            "rss_peak_kb": max(rss),
            "threads_peak": max(s.get("threads", 0) for s in samples),
            "fds_peak": max(s.get("fds", 0) for s in samples),
            "queue_peak": max(s.get("queue_depth", 0) for s in samples),
        }
