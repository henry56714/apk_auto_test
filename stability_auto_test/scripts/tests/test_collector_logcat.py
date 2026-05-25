from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

from sat.collectors.logcat import LogcatStream, _extract_device_ts


def test_extract_device_ts_threadtime_default():
    assert _extract_device_ts(
        "05-21 10:00:00.123  1234  5678 E AndroidRuntime: x"
    ) == "05-21 10:00:00.123"


def test_extract_device_ts_with_year():
    assert _extract_device_ts(
        "2026-05-21 10:00:00.123  1234  5678 E X: x"
    ) == "2026-05-21 10:00:00.123"


def test_extract_device_ts_unparseable():
    assert _extract_device_ts("not a logcat line") is None


def _make_stream_with_lines(lines):
    """Create a LogcatStream whose subprocess yields `lines` once then EOFs."""
    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO("\n".join(lines) + "\n")
    fake_proc.stderr = io.StringIO("")
    fake_proc.terminate = MagicMock()
    fake_proc.wait = MagicMock(return_value=0)
    fake_proc.kill = MagicMock()
    popen_calls = {"count": 0}

    def popen(cmd, **kwargs):
        popen_calls["count"] += 1
        if popen_calls["count"] == 1:
            return fake_proc
        # Subsequent calls: stop the stream so iteration ends.
        stream._stop.set()
        eof = MagicMock()
        eof.stdout = io.StringIO("")
        eof.terminate = MagicMock()
        eof.wait = MagicMock(return_value=0)
        eof.kill = MagicMock()
        return eof

    stream = LogcatStream(serial=None, buffers=["main"], popen_fn=popen,
                          reconnect_backoff_sec=0.0)
    return stream, popen_calls


def test_logcat_stream_yields_lines_and_advances_last_ts():
    lines = [
        "05-21 10:00:00.123  1 1 I tag: hello",
        "05-21 10:00:00.456  2 2 I tag: world",
    ]
    stream, _calls = _make_stream_with_lines(lines)
    out = list(stream.lines())
    assert out == lines
    assert stream._last_device_ts == "05-21 10:00:00.456"
    assert stream._lines_read == 2


def test_logcat_stream_resume_arg_when_reconnecting():
    stream, _ = _make_stream_with_lines([
        "05-21 10:00:00.001  1 1 I tag: x",
    ])
    stream._last_device_ts = "05-21 10:00:00.001"
    cmd = stream._build_cmd()
    # `-T '<ts>'` must be present after the buffer args.
    assert "-T" in cmd
    assert cmd[cmd.index("-T") + 1] == "05-21 10:00:00.001"
