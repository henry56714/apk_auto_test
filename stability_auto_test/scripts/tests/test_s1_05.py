"""Safe export and real quotas (spec S1-05).

Covers T-L0-023 .. T-L0-025 and T-L1-011 .. T-L1-014.
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from sat.cli import build_parser
from sat.redaction import (
    REDACTION_MANIFEST_NAME,
    Redactor,
    export_bundle,
)
from sat.storage import LogStreamWriter

# ── T-L0-023: redacted bundle scans clean for every canary type ──────────────


def _fixture_output_dir(tmp_path: Path) -> Path:
    out = tmp_path / "run"
    (out / "incidents").mkdir(parents=True)
    secrets = {
        "report.json": '{"summary": "email user@example.com", "ok": true}',
        "incidents/inc-1.json": '{"evidence": {"token": "api_key=sk-123456"}}',
        "incidents/inc-1.txt": "raw slice with secret1234567890 and user@example.com",
        "incidents/inc-1_dropbox.txt": "dropbox body user@example.com",
        "incidents/inc-1.tombstone": "binary-ish tombstone with user@example.com",
        "incident_journal.jsonl": '{"status": "detected", "summary": "user@example.com"}',
        "events_2026-08-13_10.csv": "# h\nt,user@example.com\n",
        "logcat_2026-08-13_10.log": "# h\nline with user@example.com\n",
        "bookmarks.jsonl": '{"label": "x", "meta": "user@example.com"}\n',
        "workload_manifest.json": '{"actions": [{"id": "a", "note": "user@example.com"}]}',
        "status.json": '{"status": "running", "note": "user@example.com"}',
        "unknown_file.weird": "not allowed in redacted bundle",
        "binary_proto.pb": "binary with user@example.com",
    }
    for name, content in secrets.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return out


def test_redacted_bundle_scans_zero_and_excludes_unknown(tmp_path: Path):
    out = _fixture_output_dir(tmp_path)
    target = tmp_path / "bundle.zip"
    export_bundle(out, target, redacted=True)

    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        assert REDACTION_MANIFEST_NAME in names
        # Binary/unknown files never enter the redacted bundle.
        assert not any("unknown_file" in n for n in names)
        assert not any(n.endswith(".pb") for n in names)
        assert not any(n.endswith(".tombstone") for n in names)
        # Every text file inside is canary-free.
        redactor = Redactor()
        for name in names:
            if name.endswith((".zip",)):
                continue
            if not any(
                name.endswith(s)
                for s in (".json", ".csv", ".log", ".txt", ".jsonl", ".md", ".yaml")
            ):
                continue
            text = zf.read(name).decode("utf-8", errors="replace")
            _, hits = redactor.redact(text)
            assert hits == 0, f"canary remained in {name}"
        manifest = json.loads(zf.read(REDACTION_MANIFEST_NAME))
        assert manifest["hits"] > 0
        assert manifest["allowlisted_only"] is True


def test_redacted_bundle_canary_failure_deletes_zip(tmp_path: Path):
    out = _fixture_output_dir(tmp_path)
    target = tmp_path / "bundle.zip"
    # A split-across-lines canary: per-line redaction cannot catch it, so the
    # whole-file canary scan must — and must destroy the product.
    split_file = out / "incidents" / "inc-1.txt"
    split_file.write_text(
        split_file.read_text() + "\nsplit secret api_key=\nsk-123456\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        export_bundle(
            out,
            target,
            redacted=True,
            extra_regexes=[r"api_key=\s*\S+"],
        )
    assert not target.exists(), "failed export must delete the zip"


# ── T-L0-024: default export is redacted; raw needs acknowledgement ──────────


def test_export_defaults_to_redacted(tmp_path: Path):
    out = _fixture_output_dir(tmp_path)
    target = tmp_path / "bundle.zip"
    parser = build_parser()
    args = parser.parse_args(["export", "--output", str(out), "--target", str(target)])
    assert not getattr(args, "raw", False)


def test_raw_without_acknowledgement_exits_2_and_no_zip(tmp_path: Path, capsys):
    from sat.cli import EXIT_SETUP

    out = _fixture_output_dir(tmp_path)
    target = tmp_path / "bundle.zip"
    # The CLI branch: raw without acknowledgement exits 2 without a zip.
    from sat.cli import main

    rc = main(["export", "--output", str(out), "--target", str(target), "--raw"])
    assert rc == EXIT_SETUP
    assert not target.exists()


def test_raw_with_acknowledgement_exports(tmp_path: Path):
    out = _fixture_output_dir(tmp_path)
    target = tmp_path / "bundle.zip"
    export_bundle(
        out,
        target,
        redacted=False,
        raw=True,
        acknowledge_sensitive=True,
    )
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        assert any(n.endswith(".tombstone") for n in names)
        assert any("unknown_file" in n for n in names)


def test_export_bundle_raw_without_ack_raises(tmp_path: Path):
    out = _fixture_output_dir(tmp_path)
    with pytest.raises(ValueError):
        export_bundle(out, tmp_path / "b.zip", raw=True, acknowledge_sensitive=False)


# ── T-L0-025: path traversal and malicious names ─────────────────────────────


def test_export_does_not_follow_symlinks_outside_output(tmp_path: Path):
    out = tmp_path / "run"
    (out / "incidents").mkdir(parents=True)
    (out / "report.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("user@example.com", encoding="utf-8")
    (out / "link_to_outside").symlink_to(outside)

    target = tmp_path / "bundle.zip"
    export_bundle(out, target, redacted=True)
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
    assert not any("outside" in n for n in names)
    assert not any("link_to_outside" in n for n in names)


def test_export_zip_members_have_no_traversal(tmp_path: Path):
    out = _fixture_output_dir(tmp_path)
    (out / ".._evil").mkdir(exist_ok=True)
    (out / ".._evil" / "x.json").write_text('{"a": 1}', encoding="utf-8")
    target = tmp_path / "bundle.zip"
    export_bundle(out, target, redacted=True)
    with zipfile.ZipFile(target) as zf:
        for name in zf.namelist():
            assert not name.startswith(".."), f"traversal member: {name}"
            assert not name.startswith("/"), f"absolute member: {name}"


# ── T-L1-012: logcat file size rotation ──────────────────────────────────────


def test_logcat_writer_rotates_on_size(tmp_path: Path):
    writer = LogStreamWriter(tmp_path, max_file_bytes=200)
    big_line = "x" * 90 + "\n"
    for _ in range(10):
        writer.write_line(big_line)
    writer.close()
    files = writer.files()
    assert len(files) >= 4, "expected size rotation to create multiple files"
    for f in files:
        assert f.stat().st_size <= 200 + 100, "file exceeds limit + one line"
        assert f.read_text().startswith("# "), "schema header required"


# ── T-L1-013: retention during long run (fake clock) ─────────────────────────


def test_retention_deletes_old_files_and_audits(tmp_path: Path):
    from sat.quota import QuotaConfig, QuotaTracker

    now = time.time()
    old = tmp_path / "logcat_2026-08-09_00.log"
    new = tmp_path / "logcat_2026-08-10_00.log"
    old.write_text("old" * 100, encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (now - 2 * 3600, now - 2 * 3600))
    os.utime(new, (now - 100, now - 100))
    tracker = QuotaTracker(
        tmp_path,
        QuotaConfig(log_retention_hours=1),
        now_sec_fn=lambda: now,
    )
    removed = tracker.enforce_log_retention()
    assert removed == 1
    assert not old.exists()
    assert new.exists()
    assert len(tracker.audit) == 1
    assert tracker.audit[0]["reason"] == "retention"
    assert tracker.audit[0]["bytes"] > 0


# ── T-L1-011: fatal with low disk still journals and reports ─────────────────


def test_low_disk_fatal_still_journaled(tmp_path: Path):
    import time as time_mod
    from unittest.mock import MagicMock

    from sat.pool import CollectorPool, CollectorsConfig, DumpsConfig
    from sat.storage import (
        EVENTS_COLUMNS,
        EVENTS_SCHEMA_TAG,
        LIFECYCLE_COLUMNS,
        LIFECYCLE_SCHEMA_TAG,
        CsvStreamWriter,
    )

    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(
        tmp_path,
        "lifecycle",
        LIFECYCLE_COLUMNS,
        LIFECYCLE_SCHEMA_TAG,
    )

    def dumper(adb, event, d):
        return {"type": event.event_type, "process": event.process}

    pool = CollectorPool(
        MagicMock(),
        "com.example.app",
        events_writer=ev,
        lifecycle_writer=life,
        incidents_dir=tmp_path / "incidents",
        collectors=CollectorsConfig(logcat_enabled=False, resource_risk_enabled=False),
        discover_fn=lambda a, p: [],
        java_crash_dump_fn=dumper,
        dumps=DumpsConfig(
            min_free_bytes=10**12,  # pretend the disk is far below quota
            post_context_sec=0.0,
        ),
    )
    pool.start()
    from sat.detection import EVENT_JAVA_CRASH, StabilityEvent

    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process="com.example.app",
            pid=1,
            triggered_at="2026-08-13 10:00:00.000",
            summary="boom",
        )
    )
    deadline = time_mod.monotonic() + 5.0
    while time_mod.monotonic() < deadline:
        if pool.dump_task_states()["persisted"] >= 1:
            break
        time_mod.sleep(0.05)
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=5.0)
    ev.close()
    life.close()
    journal = (tmp_path / "incident_journal.jsonl").read_text()
    assert '"status": "detected"' in journal
    assert '"status": "persisted"' in journal
