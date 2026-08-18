"""Cancellable evidence-task context (spec S1-02 / IMP-02).

Dumpers run on worker threads that cannot be force-killed from Python. This
module gives every evidence task a shared *deadline* and a cancellation event
so each ADB step can:

- derive its timeout from the task's *remaining* budget instead of restarting
  a fresh 30 s per step;
- check for cancellation between steps and abort cooperatively;
- write evidence into a staging directory that is only published (atomically
  moved into the final output dir) when the task completes in time.

`stop()` can therefore return with the output directory frozen: late workers
keep writing to staging only, and their files are never published.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


class TaskCancelled(Exception):
    """Raised when a task checks cancellation after `stop()` claimed it."""


@dataclass
class TaskContext:
    """Shared deadline + cancellation state for one dump task.

    `deadline` is a monotonic timestamp (seconds). `cancelled` is the
    threading.Event owned by the dispatcher's task record. `now_fn` is
    injectable for tests.
    """

    deadline: Optional[float]
    cancelled: threading.Event
    now_fn: Callable[[], float] = time.monotonic

    def remaining(self) -> float:
        if self.deadline is None:
            return float("inf")
        return max(0.0, float(self.deadline) - self.now_fn())

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        """Abort cooperatively when cancelled or out of time."""
        if self.cancelled.is_set():
            raise TaskCancelled("task cancelled by dispatcher")
        if self.expired():
            raise TaskCancelled("task deadline exceeded")

    def timeout_for(self, step_timeout: float) -> float:
        """Timeout to pass an ADB step: min(step budget, remaining deadline)."""
        remaining = self.remaining()
        if remaining == float("inf"):
            return float(step_timeout)
        return min(float(step_timeout), max(0.05, remaining))

    def shell_timeout(self, adb, step_timeout: float) -> float:
        """ADB-step timeout, honouring a per-call floor.

        `adb` is accepted for call-site readability but unused here; the value
        is derived purely from the task deadline so a hung step can never
        exceed the shared budget.
        """
        del adb
        return self.timeout_for(step_timeout)


def task_context_for(
    *,
    deadline: Optional[float],
    cancelled: threading.Event,
    now_fn: Callable[[], float] = time.monotonic,
) -> TaskContext:
    return TaskContext(deadline=deadline, cancelled=cancelled, now_fn=now_fn)
