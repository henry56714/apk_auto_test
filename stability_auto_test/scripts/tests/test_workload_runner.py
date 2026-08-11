from __future__ import annotations

import json
import time
from pathlib import Path

from sat.bookmark import BookmarkWriter
from sat.workloads.base import Workload, WorkloadResult
from sat.workloads.runner import WorkloadRunner


class _FakeWorkload(Workload):
    name = "fake"

    def __init__(self, cleanup_sec: float = 0.0):
        super().__init__()
        self.prepared = False
        self.cleaned = False
        self.cleanup_sec = cleanup_sec

    def prepare(self):
        self.prepared = True

    def run(self) -> WorkloadResult:
        return WorkloadResult(status="ok", exit_code=0)

    def cleanup(self):
        if self.cleanup_sec:
            time.sleep(self.cleanup_sec)
        self.cleaned = True

    def manifest(self):
        return {"type": "fake", "seed": 42}


def test_runner_writes_manifest_and_bookmarks(tmp_path: Path):
    bm = BookmarkWriter(tmp_path)
    workload = _FakeWorkload()
    runner = WorkloadRunner(
        workload,
        bookmarks=bm,
        manifest_path=tmp_path / "workload_manifest.json",
    )
    result = runner.run()
    assert result.status == "ok"
    assert workload.prepared and workload.cleaned
    manifest = json.loads((tmp_path / "workload_manifest.json").read_text())
    assert manifest["seed"] == 42
    assert manifest["status"] == "ok"
    labels = [b["label"] for b in bm.read_all()]
    assert labels == ["workload_start", "workload_end"]


def test_cleanup_timeout_does_not_block(tmp_path: Path):
    bm = BookmarkWriter(tmp_path)
    workload = _FakeWorkload(cleanup_sec=30.0)
    runner = WorkloadRunner(
        workload,
        bookmarks=bm,
        manifest_path=tmp_path / "workload_manifest.json",
        cleanup_timeout_sec=0.2,
    )
    started = time.monotonic()
    runner.run()
    assert time.monotonic() - started < 5.0
    assert (tmp_path / "workload_manifest.json").exists()
