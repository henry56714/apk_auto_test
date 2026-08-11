from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from sat.api import StabilityConfig
from sat.device import DeviceInfo
from sat.discovery import Process
from sat.replay import (
    config_from_manifest,
    is_reproduced,
    read_replay_manifest,
    run_replay,
    write_replay_manifest,
)


def _cfg(tmp_path: Path) -> StabilityConfig:
    return StabilityConfig(
        package="com.example.app",
        output_dir=tmp_path / "out",
        dedup_window_sec=7.0,
        pre_context_sec=20.0,
        post_context_sec=5.0,
        min_coverage_ratio=0.95,
        policy_fail_on=["java_crash", "anr"],
    )


def test_manifest_round_trip_preserves_seed_and_config(tmp_path: Path):
    cfg = _cfg(tmp_path)
    workload = {
        "type": "monkey",
        "package": "com.example.app",
        "seed": 99,
        "event_count": 500,
        "throttle_ms": 30,
        "command": "monkey -p com.example.app -s 99 ...",
    }
    manifest = write_replay_manifest(
        cfg, workload_manifest=workload, output_dir=tmp_path, run_id="run-orig",
    )
    data = read_replay_manifest(manifest)
    assert data["workload"]["seed"] == 99
    assert data["config"]["dedup_window_sec"] == 7.0
    assert data["original_run_id"] == "run-orig"

    replay_cfg = config_from_manifest(data, tmp_path / "replay")
    assert replay_cfg.package == "com.example.app"
    assert replay_cfg.dedup_window_sec == 7.0
    assert replay_cfg.policy_fail_on == ["java_crash", "anr"]
    assert replay_cfg.replay_of_run_id == "run-orig"
    assert replay_cfg.output_dir != cfg.output_dir


def test_replay_manifest_filters_sensitive_env(tmp_path: Path):
    cfg = _cfg(tmp_path)
    workload = {
        "type": "external",
        "command_template": ["maestro", "test", "API_TOKEN=abc", "flow.yaml"],
        "timeout_sec": 60.0,
    }
    write_replay_manifest(cfg, workload_manifest=workload, output_dir=tmp_path)
    data = read_replay_manifest(tmp_path / "replay.yaml")
    joined = json.dumps(data)
    assert "abc" not in joined
    assert "API_TOKEN=***" in joined


def test_run_replay_records_replay_of_run_id(tmp_path: Path, monkeypatch):
    from sat.replay import REPLAY_FILENAME

    monkeypatch.setattr(
        "sat.api.preflight",
        lambda adb, *, serial, package: DeviceInfo(
            serial="test-serial", android_version="14", sdk_int=34, cpu_cores=4,
        ),
    )
    monkeypatch.setattr("sat.api.wait_for_processes",
                        lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)])

    cfg = _cfg(tmp_path)
    write_replay_manifest(
        cfg, workload_manifest=None,
        output_dir=tmp_path, run_id="run-orig",
    )
    adb = MagicMock()
    adb.serial = "test-serial"
    stab = run_replay(
        tmp_path / REPLAY_FILENAME,
        tmp_path / "replay-out",
        adb=adb,
        discover_fn=lambda adb, pkg: [Process(pid=1234, name=pkg)],
        duration_sec=0,
    )
    assert stab.result["run"]["config_effective"]["replay_of_run_id"] == "run-orig"


def test_is_reproduced_shared_fingerprint():
    original = {"issue_groups": [{"fingerprint": "abc"}, {"fingerprint": "def"}]}
    replay = {"issue_groups": [{"fingerprint": "abc"}, {"fingerprint": "xyz"}]}
    assert is_reproduced(original, replay) is True
    assert is_reproduced(original, {"issue_groups": [{"fingerprint": "xyz"}]}) is False
