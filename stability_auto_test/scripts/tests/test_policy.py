from __future__ import annotations

from pathlib import Path

from sat.cli import build_config, build_parser
from sat.policy import PolicyConfig, evaluate_policy


def _incident(inc_type: str, reason: str = "") -> dict:
    return {
        "type": inc_type,
        "process": "com.example.app",
        "pid": 1,
        "triggered_at": "2026-05-21 10:00:00.000",
        "evidence": {"reason": reason},
    }


def test_crash_fails_gate_and_lists_rules():
    incidents = [_incident("java_crash")]
    result = evaluate_policy(incidents, [], 1.0, PolicyConfig())
    assert result["passed"] is False
    assert any(r["rule"] == "fail_on" and r["pass"] is False for r in result["rules"])
    assert len(result["rules"]) >= 5


def test_normal_background_recycle_passes_gate():
    incidents = [_incident("process_death", reason="cached")]
    result = evaluate_policy(incidents, [], 1.0, PolicyConfig())
    death_rule = next(r for r in result["rules"] if r["rule"] == "max_process_death")
    assert death_rule["actual"] == 0
    assert death_rule["pass"] is True
    assert result["passed"] is True


def test_low_coverage_fails_gate():
    result = evaluate_policy([], [], 0.8, PolicyConfig())
    cov_rule = next(r for r in result["rules"] if r["rule"] == "min_coverage_ratio")
    assert cov_rule["pass"] is False
    assert result["passed"] is False


def test_multiple_failures_all_listed():
    incidents = [_incident("anr"), _incident("java_crash")]
    result = evaluate_policy(incidents, [], 0.5, PolicyConfig())
    failed = [r["rule"] for r in result["rules"] if r["pass"] is False]
    assert "fail_on" in failed
    assert "max_anr" in failed
    assert "min_coverage_ratio" in failed


def test_cli_overrides_yaml_policy(tmp_path: Path):
    yaml = tmp_path / "policy.yaml"
    yaml.write_text(
        "package: com.example.app\n"
        "policy:\n"
        "  fail_on: [anr]\n"
        "  max_anr: 5\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args([
        "--config", str(yaml),
        "--output", str(tmp_path / "out"),
        "--fail-on", "java_crash,native_crash",
        "--max-anr", "2",
        "--ci",
    ])
    cfg = build_config(args, yaml)
    assert cfg.policy_fail_on == ["java_crash", "native_crash"]
    assert cfg.policy_max_anr == 2
    assert cfg.ci_mode is True


def test_workload_expected_exit_passes_gate():
    incident = _incident("process_death", reason="am_kill")
    incident["evidence"]["workload_expected"] = True
    result = evaluate_policy([incident], [], 1.0, PolicyConfig())
    death_rule = next(r for r in result["rules"] if r["rule"] == "max_process_death")
    assert death_rule["pass"] is True
    assert result["passed"] is True
