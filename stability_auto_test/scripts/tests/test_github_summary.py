from __future__ import annotations

from pathlib import Path

from sat.reporter.github_summary import write_github_summary


def _result() -> dict:
    return {
        "verdict": "unstable",
        "incidents": [{"id": "i1"}],
        "coverage_ratio": 0.99,
        "policy": {"passed": False},
        "run": {"config_effective": {"output_dir": "/tmp/run1"}},
    }


def test_no_github_env_returns_false(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert write_github_summary(_result()) is False


def test_github_summary_written_when_env_set(tmp_path: Path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert write_github_summary(_result()) is True
    text = summary.read_text(encoding="utf-8")
    assert "## stability_auto_test" in text
    assert "verdict: `unstable`" in text
    assert "/tmp/run1" in text
