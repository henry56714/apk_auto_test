from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

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
        "package: com.example.app\n"
        "detection:\n"
        "  dedup_window_sec: 12\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args([
        "--output", str(tmp_path / "o"),
        "--dedup-window", "3",
    ])
    cfg = build_config(args, yaml)
    assert cfg.dedup_window_sec == 3


def test_cli_main_returns_setup_on_missing_package(tmp_path: Path):
    # Without any args, --package is missing → EXIT_SETUP (2)
    rc = cli.main([])
    assert rc == cli.EXIT_SETUP
