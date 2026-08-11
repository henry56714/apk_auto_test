"""Stable plugin interfaces.

- Collector: produces observations (no event semantics).
- Analyzer: consumes observations/incidents and emits or enriches events.
- EvidenceProvider: collects evidence for an incident.
- Reporter: consumes the unified result model.

Plugin outputs must be namespaced dicts (``plugins.<plugin_name>.<field>``) so
two plugins can never collide.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable


class Collector(ABC):
    name: str = "collector"

    @abstractmethod
    def collect(self, adb) -> Iterable[Dict]:
        """Yield observation dicts."""


class Analyzer(ABC):
    name: str = "analyzer"

    @abstractmethod
    def analyze(self, observations: Iterable[Dict]) -> Iterable[Dict]:
        """Yield or enrich event dicts."""


class EvidenceProvider(ABC):
    name: str = "evidence"

    @abstractmethod
    def provide(self, incident: Dict) -> Dict:
        """Return namespaced evidence for an incident."""


class Reporter(ABC):
    name: str = "reporter"

    @abstractmethod
    def render(self, result: Dict) -> Any:
        """Consume the unified result model."""
