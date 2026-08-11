"""In-repo example plugin (must be explicitly enabled)."""

from __future__ import annotations

from .interfaces import Collector


class ExampleCollector(Collector):
    name = "example"

    def collect(self, adb):
        yield {"plugin": "example", "observation": 1}
