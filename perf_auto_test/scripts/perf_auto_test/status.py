"""Heartbeat writer — overwrites `status.json` every N seconds.

Parent processes / supervisors / dashboards poll this file to learn what the
test is doing right now without parsing the streaming CSVs. Kept deliberately
small (a handful of fields) so the write is cheap and the JSON is easy to
consume from shell scripts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from .utils import utc_now_iso

log = logging.getLogger(__name__)

STATUS_FILENAME = "status.json"


class StatusWriter:
    def __init__(
        self,
        output_dir: Path,
        *,
        interval_sec: float = 10.0,
        query_fn: Callable[[], Dict] = lambda: {},
    ) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.path = output_dir / STATUS_FILENAME
        self.interval_sec = interval_sec
        self._query_fn = query_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._started_at = time.time()
        self._stop.clear()
        # Write an initial snapshot immediately so consumers see *something*
        # before the first interval elapses.
        self._write_once(running=True)
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="status-writer",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        # Final snapshot with running=False so consumers know the run ended.
        self._write_once(running=False)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.interval_sec):
                break
            self._write_once(running=True)

    def _write_once(self, *, running: bool) -> None:
        elapsed = (time.time() - self._started_at) if self._started_at else 0.0
        try:
            extra = self._query_fn() or {}
        except Exception:
            log.exception("status query_fn raised; emitting empty extras")
            extra = {}
        snapshot = {
            "timestamp": utc_now_iso(),
            "running": running,
            "elapsed_sec": round(elapsed, 2),
            **extra,
        }
        with self._lock:
            try:
                self.path.write_text(json.dumps(snapshot, indent=2),
                                     encoding="utf-8")
            except OSError as e:
                log.warning("status write failed: %s", e)
