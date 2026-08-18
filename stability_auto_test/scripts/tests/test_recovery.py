from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest
from sat.journal import STATUS_DETECTED, STATUS_PERSISTED
from sat.recovery import recover_report
from sat.runlock import LOCK_FILENAME, RunLock, RunLockError

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "report.schema.json"


def _write_journal(output_dir: Path, run_id: str = "run-abc") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "incident_journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "journal_version": 1,
                        "ts": "2026-05-21 10:00:00.000",
                        "event_id": "e1",
                        "run_id": run_id,
                        "status": STATUS_DETECTED,
                        "event_type": "java_crash",
                        "process": "com.example.app",
                        "pid": 1234,
                        "triggered_at": "2026-05-21 10:00:00.000",
                        "severity": "fatal",
                        "summary": "boom",
                    }
                ),
                json.dumps(
                    {
                        "journal_version": 1,
                        "ts": "2026-05-21 10:00:01.000",
                        "event_id": "e1",
                        "run_id": run_id,
                        "status": STATUS_PERSISTED,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_run_lock_rejects_second_instance(tmp_path: Path):
    proc = subprocess.Popen(["sleep", "30"])
    try:
        lock = RunLock(
            tmp_path,
            run_id="run-1",
            device="serial-1",
        )
        # Write an active lock owned by another live process.
        lock.acquire()
        lock.release()
        (tmp_path / LOCK_FILENAME).write_text(
            json.dumps(
                {
                    "run_id": "run-other",
                    "pid": proc.pid,
                    "device": "x",
                    "started_at": "2026-05-21 10:00:00",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(RunLockError):
            RunLock(
                tmp_path,
                run_id="run-2",
                device="serial-2",
            ).acquire()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_recover_blocks_on_active_lock(tmp_path: Path):
    proc = subprocess.Popen(["sleep", "30"])
    try:
        _write_journal(tmp_path)
        (tmp_path / LOCK_FILENAME).write_text(
            json.dumps(
                {
                    "run_id": "run-live",
                    "pid": proc.pid,
                    "device": "x",
                    "started_at": "2026-05-21 10:00:00",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(RunLockError):
            recover_report(tmp_path)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_recover_removes_stale_lock_and_rebuilds_report(tmp_path: Path):
    _write_journal(tmp_path)
    (tmp_path / LOCK_FILENAME).write_text(
        json.dumps(
            {
                "run_id": "run-dead",
                "pid": 99999999,
                "device": "x",
                "started_at": "2026-05-21 10:00:00",
            }
        ),
        encoding="utf-8",
    )

    result = recover_report(tmp_path)
    assert not (tmp_path / LOCK_FILENAME).exists()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["run"]["recovered"] is True
    assert report["run"]["recovered_at"]
    assert report["run"]["run_id"] == "run-abc"
    assert len(report["incidents"]) == 1
    assert report["incidents"][0]["event_id"] == "e1"
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(report, schema)
    assert result["run"]["exit_reason"] == "recovered_after_abnormal_exit"


def test_recover_without_journal_fails(tmp_path: Path):
    tmp_path.mkdir(exist_ok=True)
    with pytest.raises(RunLockError):
        recover_report(tmp_path)


def test_abnormal_recovery_is_inconclusive_without_run_complete(tmp_path: Path):
    """Recovered reports without a run-complete marker: coverage is
    inconclusive, but an already-observed fatal crash must still surface as
    `unstable` (spec IMP-01) with partial confidence."""
    _write_journal(tmp_path)
    result = recover_report(tmp_path)
    assert result["collection_health"] == "inconclusive"
    assert result["coverage_ratio"] == 0.0
    assert result["verdict"] == "unstable"
    assert result["verdict_confidence"] == "partial"
    assert any("confirmed failure" in r for r in result["verdict_reason"])
