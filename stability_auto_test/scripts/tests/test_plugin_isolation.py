from __future__ import annotations

import time

from sat.plugins.registry import PluginRunner


def test_plugin_exception_isolated_in_health():
    runner = PluginRunner()

    def boom(*args):
        raise RuntimeError("plugin exploded")

    assert runner.call("bad", boom) is None
    assert runner.health["bad"] == "failed"


def test_plugin_timeout_isolated():
    runner = PluginRunner()

    def slow(*args):
        time.sleep(30)

    started = time.monotonic()
    assert runner.call("slow", slow, timeout_sec=0.2) is None
    assert time.monotonic() - started < 3.0
    assert runner.health["slow"] == "timed_out"


def test_plugin_outputs_namespaced_no_collision():
    runner = PluginRunner()
    a = runner.namespace("plugin_a", {"x": 1})
    b = runner.namespace("plugin_b", {"x": 1})
    assert set(a) != set(b)
    assert "plugins.plugin_a" in a and "plugins.plugin_b" in b
