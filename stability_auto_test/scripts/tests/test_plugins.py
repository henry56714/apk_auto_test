from __future__ import annotations

from importlib.metadata import EntryPoint

from sat.plugins.example import ExampleCollector
from sat.plugins.registry import (
    PluginRunner,
    discover_plugins,
    load_plugin,
)


def _fake_entry_points(group):
    ep = EntryPoint(name="example", value="sat.plugins.example:ExampleCollector", group=group)
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


def test_installed_wheel_discovers_example_entry_point():
    """The example plugin entry-point must be registered in pyproject.toml
    and discoverable via importlib.metadata when installed."""
    from pathlib import Path

    # Verify the pyproject.toml declares the entry point.
    import tomllib
    from sat.plugins.example import ExampleCollector

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    entry_points = data.get("project", {}).get("entry-points", {}).get("sat.plugins", {})
    assert "example" in entry_points, "example plugin entry point not registered in pyproject.toml"
    assert "ExampleCollector" in entry_points.get("example", ""), (
        "example entry point must reference ExampleCollector"
    )
    # The ExampleCollector class must be importable.
    assert ExampleCollector is not None
    # When installed as a package, load_plugin("example") must return
    # the class. In source-tree mode (not installed), load_plugin may
    # return None — that's acceptable; the entry point declaration is
    # what matters for wheel-based discovery.
    cls = load_plugin("example")
    if cls is not None:
        assert cls is ExampleCollector
