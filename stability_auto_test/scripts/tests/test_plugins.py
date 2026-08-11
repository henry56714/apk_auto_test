from __future__ import annotations

from importlib.metadata import EntryPoint

from sat.plugins.example import ExampleCollector
from sat.plugins.registry import (
    PluginRunner,
    discover_plugins,
    load_plugin,
)


def _fake_entry_points(group):
    ep = EntryPoint(name="example", value="sat.plugins.example:ExampleCollector",
                    group=group)
    return [ep]


def test_discovery_disabled_by_default(monkeypatch):
    monkeypatch.setattr(
        "sat.plugins.registry.importlib.metadata.entry_points",
        _fake_entry_points,
    )
    assert discover_plugins(enabled=False) == []
    assert discover_plugins(enabled=True) == ["example"]
    cls = load_plugin("example")
    assert cls is ExampleCollector


def test_example_plugin_collects_namespaced_output():
    runner = PluginRunner()
    plugin = ExampleCollector()
    outputs = list(runner.call("example", plugin.collect, None) or [])
    assert outputs == [{"plugin": "example", "observation": 1}]
    assert runner.namespace("example", {"a": 1}) == {"plugins.example": {"a": 1}}
    assert runner.namespace("other", {"a": 1}) != runner.namespace("example", {"a": 1})


def test_no_installed_plugins_yields_empty():
    assert discover_plugins() == []
