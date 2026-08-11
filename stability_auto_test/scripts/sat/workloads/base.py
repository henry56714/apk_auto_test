"""Workload plugin interface."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class WorkloadResult:
    status: str = "ok"  # ok | failed | interrupted
    exit_code: int = 0
    message: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


class Workload(ABC):
    """Stable plugin interface for built-in / external workloads."""

    name: str = "base"

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._started: Optional[str] = None
        self._ended: Optional[str] = None

    def prepare(self) -> None:
        """Optional pre-run setup (e.g. launch)."""

    @abstractmethod
    def run(self) -> WorkloadResult:
        """Run the workload and return its result."""

    def stop(self) -> None:
        self._stop.set()

    def cleanup(self) -> None:
        """Best-effort teardown; must not block forever."""

    def manifest(self) -> Dict:
        return {"type": self.name}
