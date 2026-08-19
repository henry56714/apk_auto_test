from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

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
from sat.evidence.trace_matcher import match_trace


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


def _res(rc: int, stdout: str):
    return MagicMock(returncode=rc, stdout=stdout)


def _fake_trace_adb(files, pull_body: str = ""):
    """files: list of (name, size, date, time, header)."""
    adb = MagicMock()
    ls_lines = []
    headers = {}
    for name, size, date, time_s, header in files:
        ls_lines.append(f"-rw-r----- 1 root root {size} {date} {time_s} {name}")
        headers[name] = header

    def shell(cmd, **kw):
        if "ls -ln" in cmd:
            return _res(0, "\n".join(ls_lines) + "\n")
        if "head -n 20" in cmd:
            for name, header in headers.items():
                if name in cmd:
                    return _res(0, header)
            return _res(0, "")
        return _res(0, "")

    def pull(remote, local, **kw):
        Path(local).write_text(pull_body or headers.get(remote.rsplit("/", 1)[-1], ""))
        return MagicMock(returncode=0)

    adb.shell.side_effect = shell
    adb.pull.side_effect = pull
    return adb


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
    assert "no_confident_match" in inc["evidence"]["fallback_reason"]
    assert inc["evidence"]["evidence_match_confidence"] == "none"


def test_native_crash_pulls_tombstone_when_available(tmp_path: Path):
    adb = _fake_trace_adb(
        [
            (
                "tombstone_00",
                1234,
                "2026-05-21",
                "10:00:00",
                "pid: 1234, tid: 1234, name: main >>> com.example.app <<<\n",
            )
        ],
        pull_body="pid: 1234, tid: 1234, name: main >>> com.example.app <<<\n",
    )
    inc = native_dumper.run(
        adb,
        _event(event_type=EVENT_NATIVE_CRASH, triggered_at="2026-05-21 10:00:00.000"),
        tmp_path,
    )
    assert inc["evidence"]["trace_file"] is not None
    assert "com.example.app" in (tmp_path / inc["evidence"]["trace_file"]).read_text()
    assert inc["evidence"]["fallback_reason"] is None
    assert inc["evidence"]["evidence_match_confidence"] == "high"
    assert inc["evidence"]["trace_verified"] is True


def test_native_crash_pull_disabled(tmp_path: Path):
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=0, stdout="")
    inc = native_dumper.run(
        adb,
        _event(event_type=EVENT_NATIVE_CRASH),
        tmp_path,
        pull_tombstone=False,
    )
    assert inc["evidence"]["trace_file"] is None
    assert "disabled" in inc["evidence"]["fallback_reason"]


def test_anr_dumper_pull_failure(tmp_path: Path):
    adb = _fake_trace_adb(
        [
            (
                "anr_2026-05-21-100000-1",
                5000,
                "2026-05-21",
                "10:00:00",
                "----- pid 1234 at 2026-05-21 10:00:00 -----\nCmd line: com.example.app\n",
            )
        ]
    )
    adb.pull.side_effect = AdbError("permission denied")
    inc = anr_dumper.run(adb, _event(event_type=EVENT_ANR), tmp_path)
    assert inc["evidence"]["trace_file"] is None
    assert "ANR trace pull failed" in inc["evidence"]["fallback_reason"]

    # The candidate scan may be inconclusive even though DropBox gives the
    # exact framework trace path. In that case the exact path must win.
    trace_body = (
        "----- pid 1234 at 2026-05-21 10:00:00 -----\n"
        "Cmd line: com.example.app\n"
        '"main" prio=5 tid=1 Runnable\n'
        "  at com.example.app.Main.run(Main.java:1)\n"
    )
    exact_adb = _fake_trace_adb([], pull_body=trace_body)
    fetcher = MagicMock()
    fetcher.fetch.return_value = [
        "Process: com.example.app",
        "Data File: /data/anr/anr_2026-05-21-10-00-00-000",
        trace_body,
    ]
    recovered = anr_dumper.run(
        exact_adb,
        _event(event_type=EVENT_ANR),
        tmp_path / "dropbox-recovery",
        fetcher=fetcher,
    )
    assert recovered["evidence"]["trace_file"] is not None
    assert recovered["evidence"]["fallback_reason"] is None
    assert recovered["evidence"]["trace_verified"] is True
    assert "dropbox_data_file" in recovered["evidence"]["evidence_match_reasons"]
    assert exact_adb.pull.call_args.args[0] == "/data/anr/anr_2026-05-21-10-00-00-000"


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


def test_trace_matcher_prefers_target_pid_over_latest_other_package():
    adb = _fake_trace_adb(
        [
            (
                "tombstone_01",
                200,
                "2026-05-21",
                "10:00:05",
                "pid: 9999, tid: 9999, name: main >>> com.example.other <<<\n",
            ),
            (
                "tombstone_00",
                100,
                "2026-05-21",
                "09:59:58",
                "pid: 1234, tid: 1234, name: main >>> com.example.app <<<\n",
            ),
        ]
    )
    event = _event(
        event_type=EVENT_NATIVE_CRASH,
        triggered_at="2026-05-21 10:00:00.000",
    )
    result = match_trace(adb, event, "/data/tombstones/")
    assert result.bound
    assert result.candidate.name == "tombstone_00"
    assert result.candidate.pid == 1234
    assert "pid_match" in result.reasons
    assert "process_match" in result.reasons


def test_trace_matcher_rejects_pid_match_with_distant_time():
    adb = _fake_trace_adb(
        [
            (
                "tombstone_00",
                100,
                "2026-05-21",
                "08:00:00",
                "pid: 1234, tid: 1234, name: main >>> com.example.app <<<\n",
            )
        ]
    )
    event = _event(
        event_type=EVENT_NATIVE_CRASH,
        triggered_at="2026-05-21 10:00:00.000",
    )
    result = match_trace(adb, event, "/data/tombstones/")
    assert not result.bound
    assert result.score < result.threshold
    assert "no_confident_match" in result.reasons
    assert result.confidence == "low"


def test_trace_matcher_matches_multiple_child_processes():
    adb = _fake_trace_adb(
        [
            (
                "tombstone_a",
                100,
                "2026-05-21",
                "10:00:00",
                "pid: 2001, tid: 2001, name: r1 >>> com.example.app:remote <<<\n",
            ),
            (
                "tombstone_b",
                100,
                "2026-05-21",
                "10:00:01",
                "pid: 2002, tid: 2002, name: r2 >>> com.example.app:push <<<\n",
            ),
        ]
    )
    ev_a = _event(
        event_type=EVENT_NATIVE_CRASH,
        process="com.example.app:remote",
        pid=2001,
        triggered_at="2026-05-21 10:00:00.000",
    )
    ev_b = _event(
        event_type=EVENT_NATIVE_CRASH,
        process="com.example.app:push",
        pid=2002,
        triggered_at="2026-05-21 10:00:01.000",
    )
    assert match_trace(adb, ev_a, "/data/tombstones/").candidate.name == "tombstone_a"
    assert match_trace(adb, ev_b, "/data/tombstones/").candidate.name == "tombstone_b"


def test_trace_matcher_permission_denied_does_not_raise(tmp_path: Path):
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=1, stdout="")
    inc = native_dumper.run(
        adb,
        _event(event_type=EVENT_NATIVE_CRASH),
        tmp_path,
    )
    assert inc["evidence"]["trace_file"] is None
    assert inc["evidence"]["evidence_match_confidence"] == "none"
    assert "no_confident_match" in inc["evidence"]["fallback_reason"]


def test_trace_matcher_low_confidence_not_confirmed(tmp_path: Path):
    adb = _fake_trace_adb(
        [
            (
                "tombstone_00",
                100,
                "2026-05-21",
                "06:00:00",
                "pid: 1234, tid: 1234, name: main >>> com.example.app <<<\n",
            )
        ]
    )
    inc = native_dumper.run(
        adb,
        _event(event_type=EVENT_NATIVE_CRASH, triggered_at="2026-05-21 10:00:00.000"),
        tmp_path,
    )
    assert inc["evidence"]["trace_file"] is None
    assert inc["evidence"]["evidence_match_confidence"] == "low"
    assert inc["evidence"]["trace_verified"] is False
    assert "no_confident_match" in inc["evidence"]["fallback_reason"]
