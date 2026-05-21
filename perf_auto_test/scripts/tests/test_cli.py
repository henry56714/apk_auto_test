"""CLI tests: arg parsing, fail-on, exit-code mapping (no real device)."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from perf_auto_test import cli


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
# evaluate_fail_on
# ---------------------------------------------------------------------------
def _fake_result(cpu_alerts=0, mem_alerts=0, restarts=0) -> dict:
    return {
        "processes": [
            {"name": "com.foo", "alerts": {"cpu": cpu_alerts, "mem": mem_alerts},
             "restart_count": restarts},
        ],
    }


class TestEvaluateFailOn:
    def test_none_spec_returns_none(self):
        assert cli.evaluate_fail_on(None, _fake_result()) is None
        assert cli.evaluate_fail_on("", _fake_result()) is None

    def test_alerts_gte_triggers(self):
        assert cli.evaluate_fail_on("alerts>=1", _fake_result(cpu_alerts=1)) is not None

    def test_alerts_gte_no_trigger(self):
        assert cli.evaluate_fail_on("alerts>=1", _fake_result()) is None

    def test_cpu_alerts_only(self):
        assert cli.evaluate_fail_on("cpu_alerts>=1", _fake_result(mem_alerts=5)) is None
        assert cli.evaluate_fail_on("cpu_alerts>=1", _fake_result(cpu_alerts=1)) is not None

    def test_restarts(self):
        assert cli.evaluate_fail_on("restarts>=2", _fake_result(restarts=1)) is None
        assert cli.evaluate_fail_on("restarts>=2", _fake_result(restarts=2)) is not None

    def test_or_semantics(self):
        # Either condition triggers.
        spec = "alerts>=1,restarts>=2"
        assert cli.evaluate_fail_on(spec, _fake_result()) is None
        assert cli.evaluate_fail_on(spec, _fake_result(cpu_alerts=1)) is not None
        assert cli.evaluate_fail_on(spec, _fake_result(restarts=2)) is not None

    def test_other_operators(self):
        assert cli.evaluate_fail_on("alerts==0", _fake_result()) is not None
        assert cli.evaluate_fail_on("alerts>0", _fake_result()) is None
        assert cli.evaluate_fail_on("alerts<5", _fake_result(cpu_alerts=10)) is None

    def test_bad_expression_raises(self):
        with pytest.raises(ValueError, match="bad --fail-on"):
            cli.evaluate_fail_on("garbage", _fake_result())

    def test_unknown_counter_raises(self):
        with pytest.raises(ValueError, match="unknown counter"):
            cli.evaluate_fail_on("foo>=1", _fake_result())

    def test_trigger_message_useful(self):
        msg = cli.evaluate_fail_on("alerts>=1", _fake_result(cpu_alerts=3))
        assert "alerts=3" in msg
        assert ">=" in msg


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
            "--no-html", "--emit-junit", "--quiet", "--log-json",
        ])
        assert ns.no_html is True
        assert ns.emit_junit is True
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
            no_html=False, emit_junit=False,
            fail_on=None, quiet=False, verbose=False, log_json=False, config=None,
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

    def test_missing_output_raises(self):
        with pytest.raises(ValueError, match="output"):
            cli.build_config(self._args(output=None), yaml_path=None)

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
        from perf_auto_test.device import DeviceSetupError

        with patch("perf_auto_test.cli.PerfTest") as MockPT:
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
        with patch("perf_auto_test.cli.PerfTest") as MockPT:
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
        with patch("perf_auto_test.cli.PerfTest") as MockPT:
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

    def test_fail_on_triggers_returns_1(self, tmp_path):
        with patch("perf_auto_test.cli.PerfTest") as MockPT:
            inst = MockPT.return_value
            inst.start.return_value = None
            inst.stop.return_value = None
            inst._stopped = True
            inst._result = {
                "processes": [{"name": "com.foo",
                               "alerts": {"cpu": 1, "mem": 0},
                               "restart_count": 0}],
                "run": {},
            }
            inst.result = inst._result
            rc = cli.main([
                "--package", "com.foo", "--output", str(tmp_path),
                "--duration", "1s", "--fail-on", "alerts>=1", "--quiet",
            ])
        assert rc == cli.EXIT_FAIL_ON
        # set_exit and rewrite_reports should have been called.
        inst.set_exit.assert_called_once()
        inst.rewrite_reports.assert_called_once()

    def test_fail_on_not_triggered_returns_0(self, tmp_path):
        with patch("perf_auto_test.cli.PerfTest") as MockPT:
            inst = MockPT.return_value
            inst.start.return_value = None
            inst.stop.return_value = None
            inst._stopped = True
            inst._result = {
                "processes": [{"name": "com.foo",
                               "alerts": {"cpu": 0, "mem": 0},
                               "restart_count": 0}],
                "run": {},
            }
            inst.result = inst._result
            rc = cli.main([
                "--package", "com.foo", "--output", str(tmp_path),
                "--duration", "1s", "--fail-on", "alerts>=1", "--quiet",
            ])
        assert rc == cli.EXIT_OK
        inst.set_exit.assert_not_called()

    def test_bad_fail_on_returns_2(self, tmp_path):
        with patch("perf_auto_test.cli.PerfTest") as MockPT:
            inst = MockPT.return_value
            inst.start.return_value = None
            inst.stop.return_value = None
            inst._stopped = True
            inst._result = {"processes": [], "run": {}}
            inst.result = inst._result
            rc = cli.main([
                "--package", "com.foo", "--output", str(tmp_path),
                "--duration", "1s", "--fail-on", "garbage_expr", "--quiet",
            ])
        assert rc == cli.EXIT_SETUP

    def test_missing_required_arg_returns_2(self, tmp_path):
        # Missing --output should be caught by build_config → ValueError → EXIT_SETUP
        rc = cli.main(["--package", "com.foo", "--duration", "1s", "--quiet"])
        assert rc == cli.EXIT_SETUP
