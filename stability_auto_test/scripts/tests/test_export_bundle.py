from __future__ import annotations

import json
import zipfile
from pathlib import Path


def test_redacted_export_excludes_raw_sensitive_data(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(
        json.dumps({"run": {"summary": "token=abc123"}}), encoding="utf-8",
    )
    (run / "logcat_2026-08-10_10.log").write_text(
        "user 13800138000\n", encoding="utf-8",
    )
    (run / "incident_journal.jsonl").write_text(
        '{"summary": "alice@example.com"}\n', encoding="utf-8",
    )
    (run / ".sat-run.lock").write_text("{}", encoding="utf-8")

    from sat.redaction import export_bundle
    target = export_bundle(run, tmp_path / "share.zip", redacted=True)
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        assert ".sat-run.lock" not in names
        joined = "\n".join(zf.read(n).decode("utf-8", "replace") for n in names)
    assert "abc123" not in joined
    assert "13800138000" not in joined
    assert "alice@example.com" not in joined


def test_export_bundle_requires_redaction_flag_for_raw(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text('{"run": {}}', encoding="utf-8")
    from sat.redaction import export_bundle
    target = export_bundle(run, tmp_path / "raw.zip", redacted=False)
    assert target.exists()
