from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sat.adb import AdbError
from sat.detection import (
    EVENT_ANR,
    EVENT_JAVA_CRASH,
    EVENT_NATIVE_CRASH,
    EVENT_PROCESS_DEATH,
    StabilityEvent,
)
from sat.dumpers import anr as anr_dumper
from sat.dumpers import java_crash as java_dumper
from sat.dumpers import native_crash as native_dumper
from sat.dumpers import proc_death as proc_death_dumper


def _event(event_type=EVENT_JAVA_CRASH, **kw) -> StabilityEvent:
    base = dict(
        event_type=event_type,
        process="com.example.app",
        pid=1234,
        triggered_at="2026-05-21 10:00:00.000",
        severity="fatal",
        summary="x",
        raw_lines=["raw 1", "raw 2"],
    )
    base.update(kw)
    return StabilityEvent(**base)


def test_java_crash_writes_slice_and_json(tmp_path: Path):
    incident = java_dumper.run(MagicMock(), _event(), tmp_path)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert any(f.endswith(".txt") for f in files)
    assert any(f.endswith(".json") for f in files)
    assert incident["type"] == EVENT_JAVA_CRASH
    assert incident["evidence"]["logcat_slice_file"].endswith(".txt")
    # The JSON on disk matches
    json_file = next(tmp_path.glob("*.json"))
    on_disk = json.loads(json_file.read_text())
    assert on_disk["process"] == "com.example.app"


def test_native_crash_falls_back_without_tombstone(tmp_path: Path):
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=1, stdout="")
    inc = native_dumper.run(adb, _event(event_type=EVENT_NATIVE_CRASH), tmp_path)
    assert inc["evidence"]["trace_file"] is None
    assert "no accessible tombstone" in inc["evidence"]["fallback_reason"]


def test_native_crash_pulls_tombstone_when_available(tmp_path: Path):
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=0, stdout="tombstone_00\n")

    def fake_pull(remote, local, **kw):
        Path(local).write_text("FAKE TOMBSTONE")
        return MagicMock(returncode=0)

    adb.pull.side_effect = fake_pull
    inc = native_dumper.run(adb, _event(event_type=EVENT_NATIVE_CRASH), tmp_path)
    assert inc["evidence"]["trace_file"] is not None
    assert (tmp_path / inc["evidence"]["trace_file"]).read_text() == "FAKE TOMBSTONE"
    assert inc["evidence"]["fallback_reason"] is None


def test_native_crash_pull_disabled(tmp_path: Path):
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=0, stdout="")
    inc = native_dumper.run(
        adb, _event(event_type=EVENT_NATIVE_CRASH), tmp_path,
        pull_tombstone=False,
    )
    assert inc["evidence"]["trace_file"] is None
    assert "disabled" in inc["evidence"]["fallback_reason"]


def test_anr_dumper_pull_failure(tmp_path: Path):
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=0, stdout="anr_2026-05-21-100000-1\n")
    adb.pull.side_effect = AdbError("permission denied")
    inc = anr_dumper.run(adb, _event(event_type=EVENT_ANR), tmp_path)
    assert inc["evidence"]["trace_file"] is None
    assert "ANR trace pull failed" in inc["evidence"]["fallback_reason"]


def test_proc_death_writes_minimal_incident(tmp_path: Path):
    inc = proc_death_dumper.run(
        MagicMock(),
        _event(event_type=EVENT_PROCESS_DEATH, raw_lines=[]),
        tmp_path,
    )
    files = list(tmp_path.iterdir())
    assert any(f.suffix == ".json" for f in files)
    # No raw_lines → no .txt file
    assert not any(f.suffix == ".txt" for f in files)
    assert inc["type"] == EVENT_PROCESS_DEATH
