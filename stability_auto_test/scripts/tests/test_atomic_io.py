from __future__ import annotations

import json
from pathlib import Path

import pytest
from sat.atomic_io import atomic_write_json


def test_atomic_write_replaces_old_json(tmp_path: Path):
    path = tmp_path / "report.json"
    path.write_text('{"old": true}', encoding="utf-8")
    atomic_write_json(path, {"new": True, "n": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True, "n": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_failure_keeps_old_json(tmp_path: Path):
    path = tmp_path / "report.json"
    path.write_text('{"old": true, "valid": true}', encoding="utf-8")

    def fail_fsync(fd):
        raise OSError("disk exploded")

    with pytest.raises(OSError):
        atomic_write_json(path, {"new": True}, fsync_fn=fail_fsync)

    # The previous JSON is untouched and still parseable.
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "old": True, "valid": True,
    }
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "nested" / "dir" / "incident.json"
    atomic_write_json(path, {"x": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 1}
