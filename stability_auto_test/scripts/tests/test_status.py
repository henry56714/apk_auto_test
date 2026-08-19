from __future__ import annotations

import json
from pathlib import Path

from sat.status import StatusWriter


def test_start_and_stop_publish_immediate_and_final_snapshots(tmp_path: Path):
    writer = StatusWriter(
        tmp_path,
        interval_sec=60,
        query_fn=lambda: {"run_id": "run-1", "incidents": 2},
    )

    writer.start()
    running = json.loads(writer.path.read_text(encoding="utf-8"))
    writer.stop()
    stopped = json.loads(writer.path.read_text(encoding="utf-8"))

    assert running["running"] is True
    assert running["run_id"] == "run-1"
    assert running["incidents"] == 2
    assert stopped["running"] is False
    assert stopped["elapsed_sec"] >= 0
    assert stopped["timestamp"]


def test_query_failure_still_writes_core_status(tmp_path: Path, caplog):
    def fail_query():
        raise RuntimeError("collector unavailable")

    writer = StatusWriter(tmp_path, query_fn=fail_query)
    writer._write_once(running=True)
    snapshot = json.loads(writer.path.read_text(encoding="utf-8"))

    assert snapshot["running"] is True
    assert snapshot["elapsed_sec"] == 0.0
    assert "status query_fn raised" in caplog.text
