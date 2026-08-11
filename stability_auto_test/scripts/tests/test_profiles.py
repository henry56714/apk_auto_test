from __future__ import annotations

import json
from pathlib import Path

from sat import cli
from sat.cli import build_config, build_parser


def test_profile_defaults_applied(tmp_path: Path):
    args = build_parser().parse_args([
        "--package", "com.example.app",
        "--output", str(tmp_path / "out"),
        "--profile", "smoke",
    ])
    cfg = build_config(args, None)
    assert cfg.profile_name == "smoke"
    assert cfg.pre_context_sec == 10
    assert cfg.min_coverage_ratio == 0.9
    assert cfg.config_sources["pre_context_sec"] == "profile"


def test_yaml_and_cli_override_profile(tmp_path: Path):
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(
        "package: com.example.app\n"
        "profile: soak\n"
        "dumps:\n  pre_context_sec: 100\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args([
        "--config", str(yaml),
        "--output", str(tmp_path / "out"),
        "--dedup-window", "3",
    ])
    cfg = build_config(args, yaml)
    assert cfg.profile_name == "soak"
    assert cfg.pre_context_sec == 100
    assert cfg.config_sources["pre_context_sec"] == "yaml"
    assert cfg.dedup_window_sec == 3
    assert cfg.config_sources["dedup_window_sec"] == "cli"


def test_print_effective_config(tmp_path: Path, capsys):
    rc = cli.main([
        "--package", "com.example.app",
        "--output", str(tmp_path / "out"),
        "--profile", "smoke",
        "--print-effective-config",
    ])
    assert rc == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["profile_name"] == "smoke"
    assert data["config_sources"]["min_coverage_ratio"] == "profile"
    assert data["pre_context_sec"] == 10
