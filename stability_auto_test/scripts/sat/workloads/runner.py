"""Workload runner: bookmarks + manifest + cleanup with timeout."""

from __future__ import annotations

import threading
from pathlib import Path

from ..atomic_io import atomic_write_json
from ..bookmark import BookmarkWriter
from ..utils import utc_now_iso
from .base import Workload, WorkloadResult

MANIFEST_FILENAME = "workload_manifest.json"


class WorkloadRunner:
    def __init__(
        self,
        workload: Workload,
        *,
        bookmarks: BookmarkWriter,
        manifest_path: Path,
        cleanup_timeout_sec: float = 10.0,
    ) -> None:
        self.workload = workload
        self.bookmarks = bookmarks
        self.manifest_path = Path(manifest_path)
        self.cleanup_timeout_sec = float(cleanup_timeout_sec)

    def run(self) -> WorkloadResult:
        self.workload.prepare()
        self.bookmarks.append("workload_start", self.workload.manifest())
        result = self.workload.run()
        self.bookmarks.append("workload_end", {"status": result.status})
        self._cleanup()
        manifest = dict(self.workload.manifest())
        manifest.update({
            "started_at": result.started_at or utc_now_iso(),
            "ended_at": result.ended_at or utc_now_iso(),
            "status": result.status,
            "exit_code": result.exit_code,
        })
        atomic_write_json(self.manifest_path, manifest)
        return result

    def stop(self) -> None:
        self.workload.stop()

    def _cleanup(self) -> None:
        worker = threading.Thread(target=self.workload.cleanup, daemon=True)
        worker.start()
        worker.join(timeout=self.cleanup_timeout_sec)
