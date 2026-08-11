from __future__ import annotations

import sys
import threading
import time
from shlex import join as shlex_join
from shlex import split as shlex_split

from sat.workloads.external import ExternalWorkload, _filter_env


def test_external_command_success():
    w = ExternalWorkload(f"{sys.executable} -c 'print(1)'")
    result = w.run()
    assert result.status == "ok"
    assert result.exit_code == 0


def test_external_command_failure():
    w = ExternalWorkload(f"{sys.executable} -c 'import sys; sys.exit(3)'")
    result = w.run()
    assert result.status == "failed"
    assert result.exit_code == 3


def test_stop_interrupts_external_workload():
    w = ExternalWorkload(f"{sys.executable} -c 'import time; time.sleep(30)'")
    result_holder = {}

    def target():
        result_holder["r"] = w.run()

    t = threading.Thread(target=target)
    t.start()
    time.sleep(0.5)
    w.stop()
    t.join(timeout=10)
    assert not t.is_alive()
    assert result_holder["r"].status == "interrupted"
    w.cleanup()


def test_sensitive_env_filtered():
    args = ["maestro", "test", "API_TOKEN=abc123", "flow=ok"]
    filtered = _filter_env(args)
    assert "API_TOKEN=***" in filtered
    assert "abc123" not in filtered
    assert "flow=ok" in filtered


def test_shlex_roundtrip_preserves_compound_command():
    argv = ["adb", "-s", "X", "shell",
            "monkey -p com.example.app -c android.intent.category.LAUNCHER 1; "
            "sleep 1; am crash com.example.app"]
    assert shlex_split(shlex_join(argv)) == argv
