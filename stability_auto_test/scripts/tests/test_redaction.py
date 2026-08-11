from __future__ import annotations

import json
from pathlib import Path

import pytest
from sat.analyzers.fingerprint import fingerprint_incident
from sat.redaction import Redactor, redact_output_dir


def test_builtin_patterns_redact_sensitive_values():
    redactor = Redactor()
    text = "contact alice@example.com 13800138000 token=abc123 31.2304,121.4737"
    out, hits = redactor.redact(text)
    assert "alice@example.com" not in out
    assert "13800138000" not in out
    assert "abc123" not in out
    assert "31.2304,121.4737" not in out
    assert hits >= 4


def test_invalid_regex_rejected():
    with pytest.raises(ValueError, match="invalid redaction regex"):
        Redactor.from_config(["(unclosed"])


def test_redact_output_dir_removes_raw_values(tmp_path: Path):
    (tmp_path / "report.json").write_text(
        json.dumps({"run": {"summary": "token=abc123"}, "incidents": []}),
        encoding="utf-8",
    )
    (tmp_path / "incidents").mkdir()
    (tmp_path / "incidents" / "i.json").write_text(
        json.dumps({"summary": "call 13800138000"}),
        encoding="utf-8",
    )
    (tmp_path / "logcat_2026-08-10_10.log").write_text(
        "# tag\nuser alice@example.com\n", encoding="utf-8",
    )

    stats = redact_output_dir(tmp_path, Redactor())
    assert stats["hits"] > 0
    report = (tmp_path / "report.json").read_text()
    incident = (tmp_path / "incidents" / "i.json").read_text()
    log = (tmp_path / "logcat_2026-08-10_10.log").read_text()
    assert "abc123" not in report
    assert "13800138000" not in incident
    assert "alice@example.com" not in log
    assert "[REDACTED]" in report


def test_fingerprint_stable_across_redaction():
    raw = {
        "type": "java_crash",
        "process": "com.example.app",
        "pid": 1,
        "triggered_at": "2026-05-21 10:00:00.000",
        "summary": "token=abc123 java.lang.RuntimeException: boom",
        "evidence": {
            "exception_class": "java.lang.RuntimeException",
            "top_frames": ["at com.example.Main.run(Main.java:1)"],
        },
    }
    redacted = Redactor().redact_dict(raw)
    assert fingerprint_incident(raw) == fingerprint_incident(redacted)
