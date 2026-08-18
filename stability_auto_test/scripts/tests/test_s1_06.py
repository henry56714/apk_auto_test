"""Workload / matrix / unified stop signal (spec S1-06).

Covers T-L0-021 and T-L1-016 .. T-L1-022, T-L1-027, T-L1-028.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sat.cli as cli
from sat.api import StabilityConfig, StabilityTest
from sat.collectors.device_health import (
    DeviceHealthMonitor,
    DeviceSnapshot,
)

# ── T-L0-021: config validation is unified across all entry points ───────────


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"min_coverage_ratio": 1.5}, "min_coverage_ratio"),
        ({"min_coverage_ratio": -0.1}, "min_coverage_ratio"),
        ({"rescan_interval_sec": -1}, "rescan_interval_sec"),
        ({"dedup_window_sec": -5}, "dedup_window_sec"),
        ({"logcat_buffers": []}, "logcat_buffers"),
        ({"device_reboot_policy": "bogus"}, "device_reboot_policy"),
        ({"max_queue_size": 0}, "max_queue_size"),
        ({"evidence_sample_every_n": 0}, "evidence_sample_every_n"),
        ({"policy_min_uptime_ratio": 2.0}, "policy_min_uptime_ratio"),
        ({"policy_fail_on": ["java_crash", "not_a_type"]}, "not_a_type"),
        ({"dump_shutdown_timeout_sec": 0}, "dump_shutdown_timeout_sec"),
        ({"pre_context_sec": -1}, "pre_context_sec"),
    ],
)
def test_library_config_validation(tmp_path: Path, kwargs, fragment):
    with pytest.raises(ValueError) as excinfo:
        StabilityConfig(
            package="com.example.app",
            output_dir=tmp_path,
            **kwargs,
        )
    assert fragment in str(excinfo.value)


def test_cli_and_yaml_share_the_same_validation(tmp_path: Path):
    yaml = tmp_path / "bad.yaml"
    yaml.write_text(
        "package: com.example.app\nmin_coverage_ratio: 7\n",
        encoding="utf-8",
    )
    rc = cli.main(
        [
            "--config",
            str(yaml),
            "--output",
            str(tmp_path / "out"),
            "--duration",
            "1s",
        ]
    )
    assert rc == cli.EXIT_SETUP  # same error code for CLI and YAML inputs
    # Library error message carries the field name too.
    with pytest.raises(ValueError) as excinfo:
        cli.build_config(
            cli.build_parser().parse_args(
                ["--config", str(yaml), "--output", str(tmp_path / "out2")]
            ),
            yaml,
        )
    assert "min_coverage_ratio" in str(excinfo.value)


def test_invalid_config_never_touches_adb(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "Adb",
        lambda *a, **k: calls.append("adb") or MagicMock(),
    )
    rc = cli.main(
        [
            "--package",
            "com.example.app",
            "--output",
            str(tmp_path / "out"),
            "--min-coverage",
            "9",
            "--duration",
            "1s",
        ]
    )
    assert rc == cli.EXIT_SETUP
    assert calls == []


# ── T-L1-016: external workload with huge output cannot deadlock ─────────────


def test_external_workload_large_output_no_deadlock():
    from sat.workloads.external import ExternalWorkload

    cmd = "python -c 'import sys; [print(\"line%d\" % i) for i in range(200000)]'"
    workload = ExternalWorkload(cmd, timeout_sec=30.0)
    result = workload.run()
    assert result.status == "ok"
    assert workload.output_lines or workload.output_truncated
    assert workload.output_bytes <= 1024 * 1024 + 64 * 1024  # bounded


def test_external_workload_failure_carries_tail():
    from sat.workloads.external import ExternalWorkload

    workload = ExternalWorkload("sh -c 'echo boom; exit 7'", timeout_sec=30.0)
    result = workload.run()
    assert result.status == "failed"
    assert result.exit_code == 7
    assert "boom" in (result.message or "")


# ── T-L1-017: duration is the unified budget ─────────────────────────────────


def test_cli_duration_budget_covers_workload(monkeypatch):
    """Workload 1.5 s + duration 1 s must finish in ~1 s, not 2.5 s."""
    monkeypatch.setattr(cli, "StabilityTest", _FakeTestRecordingDuration)
    started = time.monotonic()
    rc = cli.main(
        [
            "--package",
            "com.example.app",
            "--output",
            "/tmp/x",
            "--duration",
            "1s",
            "--workload",
            "external",
            "--external-cmd",
            "sleep 1.5",
        ]
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"run took {elapsed:.1f}s; workload must not extend it"
    assert rc == cli.EXIT_OK


class _FakeTestRecordingDuration:
    _last_instance = None

    def __init__(self, cfg):
        _FakeTestRecordingDuration._last_instance = self
        self.cfg = cfg
        self._result = None
        self._stopped = False
        self.exit_calls = []
        self._bookmarks = MagicMock()
        self._exit_reason = "duration_elapsed"

    def start(self):
        pass

    def stop(self):
        self._stopped = True
        self._result = {
            "verdict": "stable",
            "policy": {"enabled": False, "passed": True},
            "run": {"run_id": "x"},
        }

    def wait(self, deadline):
        while time.time() < deadline:
            time.sleep(0.05)
        return None

    def rewrite_reports(self):
        pass

    def set_exit(self, code, reason):
        self.exit_calls.append((code, reason))
        self._exit_reason = reason


# ── T-L1-018: workload failure exits non-zero unless explicitly ignored ──────


def test_cli_workload_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(cli, "StabilityTest", _FakeTestRecordingDuration)
    rc = cli.main(
        [
            "--package",
            "com.example.app",
            "--output",
            "/tmp/x",
            "--duration",
            "1s",
            "--workload",
            "external",
            "--external-cmd",
            "sh -c 'exit 3'",
        ]
    )
    assert rc == cli.EXIT_GATE_FAILED  # workload_failed surfaces (IMP-08)
    fake = _FakeTestRecordingDuration._last_instance
    assert ("workload_failed") in [r for _, r in fake.exit_calls]


def test_cli_workload_failure_ignored_continues(monkeypatch):
    monkeypatch.setattr(cli, "StabilityTest", _FakeTestRecordingDuration)
    rc = cli.main(
        [
            "--package",
            "com.example.app",
            "--output",
            "/tmp/x",
            "--duration",
            "1s",
            "--workload",
            "external",
            "--external-cmd",
            "sh -c 'exit 3'",
            "--ignore-workload-failure",
        ]
    )
    assert rc == cli.EXIT_OK


# ── T-L1-020: unified stop event ─────────────────────────────────────────────


def test_stability_test_wait_returns_stop_reason():
    t = StabilityTest.__new__(StabilityTest)
    t.stop_event = threading.Event()
    t._fail_fast_event = threading.Event()

    # Deadline path.
    assert t.wait(time.time() + 0.05) is None
    # Fail-fast path.
    t._fail_fast_event.set()
    assert t.wait(time.time() + 5.0) == "fail_fast"
    # Dashboard/stop path.
    t2 = StabilityTest.__new__(StabilityTest)
    t2.stop_event = threading.Event()
    t2._fail_fast_event = threading.Event()
    t2.stop_event.set()
    assert t2.wait(time.time() + 5.0) == "stop_requested"


def test_pool_fail_fast_triggers_callback(tmp_path: Path):
    from sat.pool import CollectorPool, CollectorsConfig
    from sat.storage import (
        EVENTS_COLUMNS,
        EVENTS_SCHEMA_TAG,
        LIFECYCLE_COLUMNS,
        LIFECYCLE_SCHEMA_TAG,
        CsvStreamWriter,
    )

    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(
        tmp_path,
        "lifecycle",
        LIFECYCLE_COLUMNS,
        LIFECYCLE_SCHEMA_TAG,
    )
    fired = threading.Event()
    pool = CollectorPool(
        MagicMock(),
        "com.example.app",
        events_writer=ev,
        lifecycle_writer=life,
        collectors=CollectorsConfig(
            logcat_enabled=False,
            device_reboot_policy="fail-fast",
        ),
        discover_fn=lambda a, p: [],
        on_fail_fast=fired.set,
    )
    pool.start()
    pool._on_device_gap("offline")
    assert fired.is_set()
    pool.stop(join_timeout=1.0)
    ev.close()
    life.close()


# ── T-L1-021/022: matrix full config + failed workers stay visible ───────────


def test_matrix_mode_passes_full_config_and_no_forced_launch(monkeypatch, tmp_path: Path):
    captured = {}
    launches = []

    def fake_run_matrix(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cli, "run_matrix", fake_run_matrix)
    import sat.matrix as matrix_mod

    monkeypatch.setattr(
        matrix_mod,
        "launch_package_on",
        lambda device, pkg: launches.append(device),
    )

    cfg = StabilityConfig(
        package="com.example.app",
        output_dir=tmp_path / "matrix",
        min_coverage_ratio=0.9,
        redact=True,
        redaction_regexes=[r"canary-\d+"],
        policy_max_anr=3,
    )
    args = cli.build_parser().parse_args(
        [
            "--package",
            "com.example.app",
            "--output",
            str(tmp_path / "matrix"),
            "--devices",
            "d1,d2",
            "--matrix-parallel",
            "4",
        ]
    )
    rc = cli._run_matrix_mode(args, cfg)
    assert rc == cli.EXIT_OK
    assert captured["max_parallel"] == 4
    # Workers receive a serialized full config (policy/redaction included).
    extra = list(captured["extra_args"] or [])
    config_arg = extra[extra.index("--config") + 1]
    assert config_arg.endswith("matrix_worker_config.yaml")
    # YAML config contains the full policy/redaction config.
    import yaml as _yaml

    yaml_cfg = _yaml.safe_load(Path(tmp_path / "matrix" / "matrix_worker_config.yaml").read_text())
    assert yaml_cfg["min_coverage_ratio"] == 0.9
    assert yaml_cfg["policy_max_anr"] == 3
    assert yaml_cfg["redact"] is True
    # Monitor-only mode: no launch happened.
    assert launches == []


def test_aggregate_includes_failed_workers():
    from sat.aggregate import aggregate_reports

    good = {
        "_report_path": "/x/1/report.json",
        "run": {"device": {"serial": "d1", "sdk_int": 35}},
        "verdict": "stable",
        "coverage_ratio": 0.99,
        "incidents": [],
        "issue_groups": [],
        "device_events": [],
    }
    missing = {
        "_report_path": None,
        "run": {
            "device": {"serial": "d2", "sdk_int": 0},
            "exit_code": 2,
            "exit_reason": "worker crashed",
        },
        "verdict": "inconclusive",
        "incidents": [],
        "issue_groups": [],
        "device_events": [],
        "coverage_ratio": 0.0,
    }
    aggregate = aggregate_reports([good, missing])
    serials = [d["serial"] for d in aggregate["devices"]]
    assert set(serials) == {"d1", "d2"}
    assert aggregate["aggregate_health"] == "degraded"


# ── T-L1-027: offline → booting → ready state machine ────────────────────────


def test_device_state_machine_offline_booting_ready():
    snaps = [
        DeviceSnapshot(state="device", boot_id="b1", boot_completed=True),
        DeviceSnapshot(state="offline"),
        DeviceSnapshot(state="device", boot_id="b2", boot_completed=False),
        DeviceSnapshot(state="device", boot_id="b2", boot_completed=True),
        DeviceSnapshot(state="device", boot_id="b2", boot_completed=True),
    ]
    iter_snaps = iter(snaps)
    monitor = DeviceHealthMonitor(
        MagicMock(),
        interval_sec=0.01,
        query_fn=lambda: next(iter_snaps, snaps[-1]),
    )
    monitor.start()
    time.sleep(0.2)
    monitor.stop()
    events = monitor.events()
    kinds = [e.event_type for e in events]
    assert "offline" in kinds
    assert "recovered" in kinds
    # boot_id changed while online → reboot + epoch bump.
    assert monitor.pid_epoch >= 1


def test_boot_id_change_is_reboot():
    snaps = [
        DeviceSnapshot(state="device", boot_id="old", boot_completed=True),
        DeviceSnapshot(state="device", boot_id="new", boot_completed=True),
        DeviceSnapshot(state="device", boot_id="new", boot_completed=True),
    ]
    iter_snaps = iter(snaps)
    monitor = DeviceHealthMonitor(
        MagicMock(),
        interval_sec=0.01,
        query_fn=lambda: next(iter_snaps, snaps[-1]),
    )
    monitor.start()
    time.sleep(0.15)
    monitor.stop()
    assert any(e.event_type == "reboot" for e in monitor.events())
    assert monitor.pid_epoch == 1


# ── T-L1-028: uptime regression without boot_id still detects reboot ─────────


def test_uptime_regression_detects_reboot_without_boot_id():
    snaps = [
        DeviceSnapshot(state="device", boot_id="", boot_completed=True, uptime_sec=10000),
        DeviceSnapshot(state="device", boot_id="", boot_completed=True, uptime_sec=3),
        DeviceSnapshot(state="device", boot_id="", boot_completed=True, uptime_sec=4),
    ]
    iter_snaps = iter(snaps)
    monitor = DeviceHealthMonitor(
        MagicMock(),
        interval_sec=0.01,
        query_fn=lambda: next(iter_snaps, snaps[-1]),
    )
    monitor.start()
    time.sleep(0.15)
    monitor.stop()
    reboots = [e for e in monitor.events() if e.event_type == "reboot"]
    assert len(reboots) == 1
    assert "uptime regression" in reboots[0].detail
    assert monitor.pid_epoch == 1
