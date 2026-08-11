from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from sat.api import StabilityConfig, StabilityTest
from sat.detection import EVENT_JAVA_CRASH, StabilityEvent
from sat.device import DeviceInfo
from sat.discovery import Process
from sat.webhook import WebhookNotifier


def test_same_event_rate_limited_but_different_events_sent():
    payloads = []

    def send(url, body):
        payloads.append(json.loads(body))
        return True

    notifier = WebhookNotifier(
        "http://example.invalid/hook",
        rate_limit_sec=60.0,
        send_fn=send,
    )
    assert notifier.notify("on_first_fatal", {"summary": "a"}) is True
    assert notifier.notify("on_first_fatal", {"summary": "b"}) is False
    assert notifier.notify("on_run_complete", {"summary": "c"}) is True
    assert notifier.stats()["sent"] == 2
    assert notifier.stats()["rate_limited"] == 1


def test_api_report_includes_notifications(tmp_path: Path, monkeypatch):
    calls = []

    class FakeNotifier:
        def __init__(self, *a, **kw):
            pass

        def notify(self, event_type, payload):
            calls.append(event_type)
            return True

        def stats(self):
            return {"sent": len(calls), "failed": 0, "rate_limited": 0}

    monkeypatch.setattr("sat.api.WebhookNotifier", FakeNotifier)
    monkeypatch.setattr(
        "sat.api.preflight",
        lambda adb, *, serial, package: DeviceInfo(
            serial="test-serial", android_version="14", sdk_int=34, cpu_cores=4,
        ),
    )
    monkeypatch.setattr(
        "sat.api.wait_for_processes",
        lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)],
    )
    cfg = StabilityConfig(
        package="com.example.app",
        output_dir=tmp_path / "out",
        wait_timeout_sec=1.0,
        rescan_interval_sec=10.0,
        logcat_enabled=False,
        emit_html=False,
        status_interval_sec=10.0,
        webhook_url="http://example.invalid/hook",
    )
    t = StabilityTest(cfg, adb=MagicMock(),
                      discover_fn=lambda adb, pkg: [Process(pid=1234, name=pkg)])
    t.start()
    t.stop()
    assert "on_run_complete" in calls
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["notifications"]["sent"] >= 1


def test_webhook_summaries_redacted_when_enabled(tmp_path: Path, monkeypatch):
    payloads = []

    class FakeNotifier:
        def __init__(self, *a, **kw):
            pass

        def notify(self, event_type, payload):
            if event_type == "on_first_fatal":
                payloads.append(payload)
            return True

        def stats(self):
            return {"sent": len(payloads), "failed": 0, "rate_limited": 0}

    monkeypatch.setattr("sat.api.WebhookNotifier", FakeNotifier)
    monkeypatch.setattr(
        "sat.api.preflight",
        lambda adb, *, serial, package: DeviceInfo(
            serial="test-serial", android_version="14", sdk_int=34, cpu_cores=4,
        ),
    )
    monkeypatch.setattr(
        "sat.api.wait_for_processes",
        lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)],
    )
    cfg = StabilityConfig(
        package="com.example.app",
        output_dir=tmp_path / "out",
        wait_timeout_sec=1.0,
        rescan_interval_sec=10.0,
        logcat_enabled=False,
        emit_html=False,
        status_interval_sec=10.0,
        webhook_url="http://example.invalid/hook",
        redact=True,
        redaction_regexes=["abc123"],
    )
    t = StabilityTest(cfg, adb=MagicMock(),
                      discover_fn=lambda adb, pkg: [Process(pid=1234, name=pkg)])
    t.start()
    t._pool._dispatch(StabilityEvent(
        event_type=EVENT_JAVA_CRASH,
        process="com.example.app",
        pid=1234,
        triggered_at="2026-05-21 10:00:00.000",
        summary="token=abc123 boom",
        raw_lines=["05-21 10:00:00.000  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main"],
    ))
    t.stop()
    assert payloads
    assert "abc123" not in payloads[0]["summary"]
