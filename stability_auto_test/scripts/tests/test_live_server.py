from __future__ import annotations

import json
import queue
import time

from sat import live as live_module
from sat.live import LiveServer


def test_server_defaults_to_localhost_only():
    server = LiveServer()
    assert server.host == "127.0.0.1"
    assert server._server is None


def test_status_endpoint_matches_status_model():
    server = LiveServer(status_query=lambda: {"run_id": "r1", "processes": []})
    code, body, ctype = server.handle("/api/status")
    assert code == 200
    assert "application/json" in ctype
    assert json.loads(body)["run_id"] == "r1"


def test_stop_endpoint_requires_confirmation():
    calls = []
    server = LiveServer(stop_callback=lambda: calls.append(1))
    code, body, _ = server.handle(
        "/api/stop",
        method="POST",
        body=b'{"confirm": false}',
    )
    assert code == 400
    code, body, _ = server.handle(
        "/api/stop",
        method="POST",
        body=b'{"confirm": true}',
    )
    assert code == 200
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls == [1]


def test_sse_subscriber_removed_on_disconnect():
    server = LiveServer(status_query=lambda: {"run_id": "r1"})
    gen = server.stream_events()
    first = next(gen)
    assert first.startswith("data:")
    assert len(server._subscribers) == 1
    gen.close()
    assert len(server._subscribers) == 0


def test_report_endpoint_serves_current_report(tmp_path):
    (tmp_path / "report.json").write_text('{"run": {}}', encoding="utf-8")
    server = LiveServer(output_dir=tmp_path)
    code, body, _ = server.handle("/api/report")
    assert code == 200
    assert json.loads(body) == {"run": {}}


def test_bookmark_endpoint_calls_back():
    labels = []
    server = LiveServer(bookmark_callback=lambda label: labels.append(label))
    code, body, _ = server.handle(
        "/api/bookmark",
        method="POST",
        body=b'{"label": "checkpoint-1"}',
    )
    assert code == 200
    assert labels == ["checkpoint-1"]
    code, _, _ = server.handle("/api/bookmark", method="POST", body=b"{}")
    assert code == 400


def test_dashboard_page_has_bookmark_and_stop_controls():
    server = LiveServer()
    code, body, _ = server.handle("/")
    text = body.decode("utf-8")
    assert "bookmark" in text
    assert "stop test" in text


def test_status_change_is_broadcast_to_sse_subscriber():
    """When status changes, all SSE subscribers must receive the update."""
    status_state = {"run_id": "initial", "count": 0}

    def query():
        return dict(status_state)

    server = LiveServer(status_query=query)
    gen = server.stream_events()
    # First message is the initial status.
    first = next(gen)
    assert "initial" in first

    # Change the status and broadcast.
    status_state["run_id"] = "updated"
    status_state["count"] = 1
    server._broadcast()

    # The subscriber should receive the updated status.
    second = next(gen)
    assert "updated" in second
    gen.close()


def test_malformed_mutation_requests_and_unknown_routes_are_rejected(tmp_path):
    server = LiveServer(output_dir=tmp_path)
    assert server.handle("/api/stop", method="POST", body=b"{")[0] == 400
    assert server.handle("/api/bookmark", method="POST", body=b"{")[0] == 400
    assert server.handle("/api/report")[0] == 404
    assert server.handle("/missing")[0] == 404


def test_broadcast_drops_update_for_slow_subscriber_without_blocking():
    server = LiveServer(status_query=lambda: {"state": "new"})
    subscriber = queue.Queue(maxsize=1)
    subscriber.put_nowait("old")
    server._subscribers.add(subscriber)

    server._broadcast()

    assert subscriber.get_nowait() == "old"


def test_sse_emits_keepalive_after_idle_timeout(monkeypatch):
    server = LiveServer()
    stream = server.stream_events()
    next(stream)
    subscriber = next(iter(server._subscribers))
    monkeypatch.setattr(
        subscriber,
        "get",
        lambda timeout: (_ for _ in ()).throw(queue.Empty),
    )
    assert next(stream) == ": keepalive\n\n"
    stream.close()


def test_start_and_stop_manage_http_server_lifecycle(monkeypatch):
    instances = []

    class FakeHttpServer:
        def __init__(self, address, handler):
            self.server_address = (address[0], 43210)
            self.handler = handler
            self.shutdown_called = False
            self.close_called = False
            instances.append(self)

        def serve_forever(self):
            return None

        def shutdown(self):
            self.shutdown_called = True

        def server_close(self):
            self.close_called = True

    monkeypatch.setattr(live_module, "ThreadingHTTPServer", FakeHttpServer)
    server = LiveServer()
    assert server.bound_port is None

    server.start()
    assert server.bound_port == 43210
    server.stop()

    assert instances[0].shutdown_called is True
    assert instances[0].close_called is True
    assert server.bound_port is None
