"""Local real-time dashboard (127.0.0.1 only, SSE updates)."""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional


class LiveServer:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        status_query: Callable[[], Dict] = lambda: {},
        stop_callback: Optional[Callable[[], None]] = None,
        bookmark_callback: Optional[Callable[[str], None]] = None,
        output_dir: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = port
        self._status_query = status_query
        self._stop_callback = stop_callback
        self._bookmark_callback = bookmark_callback
        self.output_dir = Path(output_dir) if output_dir else None
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._subscribers: set = set()
        self._lock = threading.Lock()

    @property
    def bound_port(self) -> Optional[int]:
        return self._server.server_address[1] if self._server else None

    def start(self) -> None:
        self._server = ThreadingHTTPServer(
            (self.host, self.port),
            self._handler_factory(),
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="live-dashboard",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def handle(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes = b"",
    ) -> tuple:
        """Pure request handling used by the HTTP handler and unit tests."""
        if method == "GET" and path == "/":
            html = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>SAT live</title></head><body><h1>SAT live</h1>"
                "<input id='label' value='manual-checkpoint'> "
                "<button id='bm' type='button'>bookmark</button> "
                "<button id='stop' type='button'>stop test</button>"
                "<pre id='status'>loading...</pre>"
                "<script>"
                "var es=new EventSource('/api/stream');"
                "es.onmessage=function(e){document.getElementById('status').textContent=e.data;};"
                "setInterval(function(){fetch('/api/status').then(r=>r.json()).then(d=>"
                "document.getElementById('status').textContent=JSON.stringify(d,null,2));},3000);"
                "document.getElementById('bm').onclick=function(){"
                "fetch('/api/bookmark',{method:'POST',headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({label:document.getElementById('label').value})});};"
                "document.getElementById('stop').onclick=function(){"
                "if(confirm('Stop this test?')){fetch('/api/stop',{method:'POST',"
                "headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})});}};"
                "</script></body></html>"
            )
            return 200, html.encode("utf-8"), "text/html; charset=utf-8"
        if method == "GET" and path == "/api/status":
            data = json.dumps(self._status_query() or {}, ensure_ascii=False)
            return 200, data.encode("utf-8"), "application/json; charset=utf-8"
        if method == "GET" and path == "/api/report" and self.output_dir:
            report = self.output_dir / "report.json"
            if report.exists():
                return 200, report.read_bytes(), "application/json"
            return 404, b"not found", "text/plain"
        if method == "POST" and path == "/api/stop":
            try:
                confirm = json.loads(
                    body.decode("utf-8", "replace") or "{}",
                ).get("confirm")
            except json.JSONDecodeError:
                confirm = None
            if confirm is not True:
                return (
                    400,
                    json.dumps(
                        {"ok": False, "error": "confirmation required"},
                    ).encode(),
                    "application/json",
                )
            if self._stop_callback is not None:
                threading.Thread(
                    target=self._stop_callback,
                    daemon=True,
                ).start()
            return 200, json.dumps({"ok": True}).encode(), "application/json"
        if method == "POST" and path == "/api/bookmark":
            try:
                label = json.loads(
                    body.decode("utf-8", "replace") or "{}",
                ).get("label")
            except json.JSONDecodeError:
                label = None
            if not label:
                return (
                    400,
                    json.dumps({"ok": False, "error": "label required"}).encode(),
                    "application/json",
                )
            if self._bookmark_callback is not None:
                self._bookmark_callback(str(label))
            return 200, json.dumps({"ok": True}).encode(), "application/json"
        return 404, b"not found", "text/plain"

    def _broadcast(self) -> None:
        """Push current status to all connected SSE subscribers."""
        payload = "data: " + json.dumps(self._status_query() or {}, ensure_ascii=False) + "\n\n"
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    def stream_events(self):
        """SSE generator; closing it removes the subscriber (no leak)."""
        q: queue.Queue = queue.Queue(maxsize=10)
        with self._lock:
            self._subscribers.add(q)
        try:
            yield ("data: " + json.dumps(self._status_query() or {}, ensure_ascii=False) + "\n\n")
            while True:
                try:
                    item = q.get(timeout=15)
                    yield item
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with self._lock:
                self._subscribers.discard(q)

    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/api/stream":
                    self._send_sse()
                else:
                    code, body, ctype = server.handle(self.path, method="GET")
                    self._send_bytes(body, ctype, code)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                code, resp, ctype = server.handle(
                    self.path,
                    method="POST",
                    body=body,
                )
                self._send_bytes(resp, ctype, code)

            def _send_sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    for item in server.stream_events():
                        self.wfile.write(item.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _send_bytes(self, body: bytes, ctype: str, code: int = 200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
