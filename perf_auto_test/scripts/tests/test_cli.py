"""CLI tests: arg parsing, exit-code mapping (no real device)."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from pat import cli


# ---------------------------------------------------------------------------
# Duration & list parsing
# ---------------------------------------------------------------------------
class TestParseDuration:
    @pytest.mark.parametrize("s,expected", [
        ("30s", 30.0), ("5m", 300.0), ("1h", 3600.0), ("24h", 86400.0),
        ("2d", 172800.0), ("0s", 0.0), ("60", 60.0),
    ])
    def test_valid(self, s, expected):
        assert cli._parse_duration(s) == expected

    @pytest.mark.parametrize("s", ["abc", "1y", "-5m", "5.5m", ""])
    def test_invalid(self, s):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._parse_duration(s)


class TestParseCsvList:
    def test_none(self):
        assert cli._parse_csv_list(None) is None

    def test_empty_str(self):
        assert cli._parse_csv_list("") is None

    def test_single(self):
        assert cli._parse_csv_list(":remote") == [":remote"]

    def test_multiple(self):
        assert cli._parse_csv_list(":remote, :push,main") == [":remote", ":push", "main"]


# ---------------------------------------------------------------------------
# build_parser smoke
# ---------------------------------------------------------------------------
class TestBuildParser:
    def test_help_does_not_crash(self):
        p = cli.build_parser()
        # parse_args(['--help']) raises SystemExit; we just want the parser to build.
        assert p is not None

    def test_minimum_args(self):
        ns = cli.build_parser().parse_args(["--package", "com.foo", "--output", "/tmp/x"])
        assert ns.package == "com.foo"
        assert ns.output == "/tmp/x"
        assert ns.duration == 300.0

    def test_threshold_flags(self):
        ns = cli.build_parser().parse_args([
            "--package", "com.foo", "--output", "/tmp/x",
            "--cpu-threshold-percent", "70",
            "--mem-threshold-pss-mb", "300",
        ])
        assert ns.cpu_threshold_percent == 70.0
        assert ns.mem_threshold_pss_mb == 300.0

    def test_headless_flags(self):
        ns = cli.build_parser().parse_args([
            "--package", "com.foo", "--output", "/tmp/x",
            "--no-html", "--quiet", "--log-json",
        ])
        assert ns.no_html is True
        assert ns.quiet is True
        assert ns.log_json is True


# ---------------------------------------------------------------------------
# build_config (flat + YAML merging)
# ---------------------------------------------------------------------------
class TestBuildConfig:
    def _args(self, **overrides):
        defaults = dict(
            package="com.foo", output="/tmp/x", duration=300.0, device=None,
            wait_timeout=None, cpu_interval=None, mem_interval=None,
            rescan_interval=None, processes=None,
            cpu_threshold_percent=None, cpu_sustain_sec=None, cpu_cooldown_sec=None,
            mem_threshold_pss_mb=None, mem_sustain_sec=None, mem_cooldown_sec=None,
            no_heap_dumps=False, status_interval=None,
            no_html=False,
            quiet=False, verbose=False, log_json=False, config=None,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_minimal_cli_only(self):
        cfg = cli.build_config(self._args(), yaml_path=None)
        assert cfg.package == "com.foo"
        assert str(cfg.output_dir) == "/tmp/x"

    def test_missing_package_raises(self):
        with pytest.raises(ValueError, match="package"):
            cli.build_config(self._args(package=None), yaml_path=None)

    def test_missing_output_auto_generated(self):
        cfg = cli.build_config(self._args(output=None), yaml_path=None)
        assert "foo" in str(cfg.output_dir)
        assert "reports" in str(cfg.output_dir)

    def test_yaml_provides_defaults(self, tmp_path):
        yaml_path = tmp_path / "c.yaml"
        yaml_path.write_text(
            "package: com.from.yaml\n"
            "thresholds:\n"
            "  cpu:\n"
            "    percent: 90\n"
            "    sustain_sec: 30\n"
            "discovery:\n"
            "  wait_timeout_sec: 120\n",
            encoding="utf-8",
        )
        cfg = cli.build_config(
            self._args(package=None, output="/tmp/y"),
            yaml_path=yaml_path,
        )
        assert cfg.package == "com.from.yaml"
        assert cfg.cpu_threshold_percent == 90
        assert cfg.cpu_sustain_sec == 30
        assert cfg.wait_timeout_sec == 120

    def test_cli_overrides_yaml(self, tmp_path):
        yaml_path = tmp_path / "c.yaml"
        yaml_path.write_text(
            "package: com.from.yaml\n"
            "thresholds:\n  cpu:\n    percent: 50\n",
            encoding="utf-8",
        )
        cfg = cli.build_config(
            self._args(package="com.from.cli", output="/tmp/y",
                       cpu_threshold_percent=85.0),
            yaml_path=yaml_path,
        )
        assert cfg.package == "com.from.cli"
        assert cfg.cpu_threshold_percent == 85.0

    def test_processes_filter_parsed(self):
        cfg = cli.build_config(self._args(processes=":remote,:push"), yaml_path=None)
        assert cfg.process_filter == [":remote", ":push"]


# ---------------------------------------------------------------------------
# Exit-code mapping via main() — mock PerfTest
# ---------------------------------------------------------------------------
class TestMainExitCodes:
    def test_setup_failure_returns_2(self, tmp_path):
        from pat.device import DeviceSetupError

        with patch("pat.cli.PerfTest") as MockPT:
            inst = MockPT.return_value
            inst.start.side_effect = DeviceSetupError("package not installed")
            inst._stopped = False
            inst._result = None
            rc = cli.main([
                "--package", "com.foo", "--output", str(tmp_path),
                "--duration", "1s", "--quiet",
            ])
        assert rc == cli.EXIT_SETUP

    def test_wait_timeout_returns_3(self, tmp_path):
        with patch("pat.cli.PerfTest") as MockPT:
            inst = MockPT.return_value
            inst.start.side_effect = TimeoutError("waited 60s")
            inst._stopped = False
            inst._result = None
            rc = cli.main([
                "--package", "com.foo", "--output", str(tmp_path),
                "--duration", "1s", "--quiet",
            ])
        assert rc == cli.EXIT_WAIT_TIMEOUT

    def test_clean_run_returns_0(self, tmp_path):
        with patch("pat.cli.PerfTest") as MockPT:
            inst = MockPT.return_value
            inst.start.return_value = None
            inst.stop.return_value = None
            inst._stopped = True
            inst._result = {"processes": [], "run": {}}
            inst.result = inst._result
            rc = cli.main([
                "--package", "com.foo", "--output", str(tmp_path),
                "--duration", "1s", "--quiet",
            ])
        assert rc == cli.EXIT_OK

    def test_missing_required_arg_returns_2(self, tmp_path):
        # Missing --output should be caught by build_config → ValueError → EXIT_SETUP
        rc = cli.main(["--package", "com.foo", "--duration", "1s", "--quiet"])
        assert rc == cli.EXIT_SETUP
