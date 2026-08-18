from __future__ import annotations

from pathlib import Path

import pytest
from sat import cli
from sat.cli import _parse_duration, build_config, build_parser


def test_parse_duration_units():
    assert _parse_duration("30s") == 30
    assert _parse_duration("5m") == 300
    assert _parse_duration("1h") == 3600
    assert _parse_duration("2d") == 86400 * 2


def test_parse_duration_bad():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_duration("abc")


def test_build_config_requires_package(tmp_path: Path):
    args = build_parser().parse_args([])
    with pytest.raises(ValueError):
        build_config(args, None)


def test_yaml_config_applies(tmp_path: Path):
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(
        """
package: com.example.app
detection:
  enable_anr: false
  dedup_window_sec: 12
""",
        encoding="utf-8",
    )
    args = build_parser().parse_args(["--output", str(tmp_path / "o")])
    cfg = build_config(args, yaml)
    assert cfg.package == "com.example.app"
    assert cfg.enable_anr is False
    assert cfg.dedup_window_sec == 12


def test_cli_overrides_yaml(tmp_path: Path):
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(
        "package: com.example.app\ndetection:\n  dedup_window_sec: 12\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "--output",
            str(tmp_path / "o"),
            "--dedup-window",
            "3",
        ]
    )
    cfg = build_config(args, yaml)
    assert cfg.dedup_window_sec == 3


def test_cli_main_returns_setup_on_missing_package(tmp_path: Path):
    # Without any args, --package is missing → EXIT_SETUP (2)
    rc = cli.main([])
    assert rc == cli.EXIT_SETUP


class _FakeStabilityTest:
    def __init__(self, cfg):
        _FakeStabilityTest._last_instance = self
        self.cfg = cfg
        self._result = None
        self._stopped = False
        self.exit_calls = []
        self._bookmarks = None
        self._exit_reason = "duration_elapsed"

    def start(self):
        pass

    def stop(self):
        self._stopped = True
        self._result = self._make_result()

    def wait(self, deadline):
        import time as _time

        while _time.time() < deadline:
            _time.sleep(0.05)
        return None

    def rewrite_reports(self):
        pass

    def set_exit(self, code, reason):
        self.exit_calls.append((code, reason))

    def _make_result(self):
        verdict = "unstable" if self.cfg.package == "com.example.crash" else "stable"
        return {
            "verdict": verdict,
            "policy": {
                "enabled": self.cfg.ci_mode,
                "passed": self.cfg.package != "com.example.crash",
            },
        }


def test_cli_default_mode_returns_zero_on_crash(monkeypatch):
    monkeypatch.setattr(cli, "StabilityTest", _FakeStabilityTest)
    rc = cli.main(
        [
            "--package",
            "com.example.crash",
            "--duration",
            "1s",
            "--output",
            "/tmp/x",
        ]
    )
    assert rc == cli.EXIT_OK


def test_cli_ci_mode_returns_gate_failed_on_crash(monkeypatch):
    monkeypatch.setattr(cli, "StabilityTest", _FakeStabilityTest)
    rc = cli.main(
        [
            "--package",
            "com.example.crash",
            "--ci",
            "--duration",
            "1s",
            "--output",
            "/tmp/x",
        ]
    )
    assert rc == cli.EXIT_GATE_FAILED


def test_cli_ci_mode_returns_zero_when_clean(monkeypatch):
    monkeypatch.setattr(cli, "StabilityTest", _FakeStabilityTest)
    rc = cli.main(
        [
            "--package",
            "com.example.app",
            "--ci",
            "--duration",
            "1s",
            "--output",
            "/tmp/x",
        ]
    )
    assert rc == cli.EXIT_OK


def test_cli_inconclusive_returns_exit_4(monkeypatch):
    class Inconclusive(_FakeStabilityTest):
        def _make_result(self):
            return {
                "verdict": "inconclusive",
                "policy": {"enabled": True, "passed": True},
            }

    monkeypatch.setattr(cli, "StabilityTest", Inconclusive)
    rc = cli.main(
        [
            "--package",
            "com.example.app",
            "--ci",
            "--duration",
            "1s",
            "--output",
            "/tmp/x",
        ]
    )
    assert rc == cli.EXIT_INCONCLUSIVE


def test_cli_interrupt_records_exit_130_in_report(monkeypatch):
    monkeypatch.setattr(cli, "StabilityTest", _FakeStabilityTest)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupt)
    rc = cli.main(
        [
            "--package",
            "com.example.app",
            "--duration",
            "1s",
            "--output",
            "/tmp/x",
        ]
    )
    assert rc == cli.EXIT_SIGINT
    assert any(
        code == cli.EXIT_SIGINT and reason == "interrupted"
        for code, reason in cli.StabilityTest._last_instance.exit_calls
    )
