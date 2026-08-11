from __future__ import annotations

from pathlib import Path

import pytest
from sat import cli
from sat.cli import build_config, build_parser
from sat.config import validate_config


def test_unknown_yaml_field_rejected_with_hint():
    errors = validate_config({"mystery": 1})
    assert any(
        "unknown field 'mystery'" in e and "allowed" in e for e in errors
    )


def test_unknown_yaml_field_fails_cli_with_setup_code(tmp_path: Path, capsys):
    yaml = tmp_path / "bad.yaml"
    yaml.write_text("package: com.example.app\nmystery: 1\n", encoding="utf-8")
    rc = cli.main(["--config", str(yaml)])
    assert rc == cli.EXIT_SETUP
    assert "mystery" in capsys.readouterr().err


def test_negative_dedup_window_rejected(tmp_path: Path):
    yaml = tmp_path / "bad.yaml"
    yaml.write_text(
        "package: com.example.app\ndetection:\n  dedup_window_sec: -1\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(["--config", str(yaml)])
    with pytest.raises(ValueError, match="dedup_window_sec"):
        build_config(args, yaml)


def test_empty_logcat_buffers_rejected(tmp_path: Path):
    yaml = tmp_path / "bad.yaml"
    yaml.write_text(
        "package: com.example.app\n"
        "collectors:\n  logcat:\n    buffers: []\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(["--config", str(yaml)])
    with pytest.raises(ValueError, match="buffers"):
        build_config(args, yaml)


def test_negative_incident_cap_rejected(tmp_path: Path):
    yaml = tmp_path / "bad.yaml"
    yaml.write_text(
        "package: com.example.app\n"
        "dumps:\n  max_incidents_per_type: -1\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(["--config", str(yaml)])
    with pytest.raises(ValueError, match="max_incidents_per_type"):
        build_config(args, yaml)


def test_lenient_mode_ignores_unknown_fields(tmp_path: Path):
    yaml = tmp_path / "ok.yaml"
    yaml.write_text(
        "package: com.example.app\nmystery: 1\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args([
        "--config", str(yaml), "--config-lenient",
        "--output", str(tmp_path / "out"),
    ])
    cfg = build_config(args, yaml)
    assert cfg.package == "com.example.app"


def test_valid_config_passes(tmp_path: Path):
    yaml = tmp_path / "ok.yaml"
    yaml.write_text(
        "package: com.example.app\n"
        "detection:\n  dedup_window_sec: 5\n"
        "dumps:\n  max_incidents_per_type: 10\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args([
        "--config", str(yaml), "--output", str(tmp_path / "out"),
    ])
    assert build_config(args, yaml).package == "com.example.app"
