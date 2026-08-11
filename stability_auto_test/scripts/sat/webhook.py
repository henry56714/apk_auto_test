"""Generic webhook notifier with rate limiting and failure isolation."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Callable, Dict, List, Optional

from .utils import utc_now_iso

log = logging.getLogger(__name__)


class WebhookNotifier:
    def __init__(
        self,
        url: str,
        *,
        events: Optional[List[str]] = None,
        rate_limit_sec: float = 60.0,
        timeout: float = 5.0,
        send_fn: Optional[Callable[[str, bytes], bool]] = None,
    ) -> None:
        self.url = url
        self.events = set(events or [
            "on_first_fatal", "on_gate_failed", "on_device_offline",
            "on_run_complete",
        ])
        self.rate_limit_sec = float(rate_limit_sec)
        self.timeout = float(timeout)
        self._send = send_fn or self._default_send
        self._lock = threading.Lock()
        self._last_sent: Dict[str, float] = {}
        self._sent = 0
        self._failed = 0
        self._rate_limited = 0

    def _default_send(self, url: str, body: bytes) -> bool:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return 200 <= r.status < 300

    def notify(self, event_type: str, payload: Dict) -> bool:
        if event_type not in self.events:
            return False
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(event_type)
            if last is not None and now - last < self.rate_limit_sec:
                self._rate_limited += 1
                return False
            self._last_sent[event_type] = now
        body = {
            "event": event_type,
            "timestamp": utc_now_iso(),
            "summary": payload.get("summary", ""),
            "severity": payload.get("severity", "info"),
        }
        try:
            ok = self._send(self.url, json.dumps(body, ensure_ascii=False).encode())
        except Exception as e:  # noqa: BLE001 - never block collection
            log.warning("webhook %s failed: %s", event_type, e)
            ok = False
        with self._lock:
            if ok:
                self._sent += 1
            else:
                self._failed += 1
        return ok

    def stats(self) -> Dict:
        with self._lock:
            return {
                "sent": self._sent,
                "failed": self._failed,
                "rate_limited": self._rate_limited,
            }
