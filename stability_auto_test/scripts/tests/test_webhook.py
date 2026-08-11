from __future__ import annotations

from sat.webhook import WebhookNotifier


def _recording_sender(payloads, fail=False):
    def send(url, body):
        import json
        payloads.append(json.loads(body))
        if fail:
            raise RuntimeError("webhook down")
        return True
    return send


def test_notify_sends_template_payload():
    payloads = []
    notifier = WebhookNotifier(
        "http://example.invalid/hook",
        send_fn=_recording_sender(payloads),
    )
    ok = notifier.notify("on_first_fatal", {
        "summary": "java crash", "severity": "fatal",
    })
    assert ok is True
    assert payloads[0]["event"] == "on_first_fatal"
    assert payloads[0]["summary"] == "java crash"
    assert notifier.stats()["sent"] == 1


def test_failure_is_recorded_not_raised():
    notifier = WebhookNotifier(
        "http://example.invalid/hook",
        send_fn=_recording_sender([], fail=True),
    )
    ok = notifier.notify("on_run_complete", {"summary": "x"})
    assert ok is False
    assert notifier.stats()["failed"] == 1


def test_unregistered_event_ignored():
    notifier = WebhookNotifier("http://example.invalid/hook")
    assert notifier.notify("on_something_else", {"summary": "x"}) is False
