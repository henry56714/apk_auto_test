from __future__ import annotations

import threading

import pytest
from sat.tasks import TaskCancelled, TaskContext, task_context_for


def test_context_without_deadline_has_infinite_budget():
    context = TaskContext(deadline=None, cancelled=threading.Event(), now_fn=lambda: 99.0)
    assert context.remaining() == float("inf")
    assert context.expired() is False
    assert context.timeout_for(30) == 30.0
    assert context.shell_timeout(object(), 12.5) == 12.5


def test_context_clamps_remaining_and_step_timeout():
    context = TaskContext(deadline=110.0, cancelled=threading.Event(), now_fn=lambda: 100.0)
    assert context.remaining() == 10.0
    assert context.timeout_for(30) == 10.0
    assert context.timeout_for(2) == 2.0


def test_context_uses_small_positive_timeout_at_deadline():
    context = TaskContext(deadline=99.0, cancelled=threading.Event(), now_fn=lambda: 100.0)
    assert context.remaining() == 0.0
    assert context.expired() is True
    assert context.timeout_for(30) == 0.05


def test_check_reports_dispatcher_cancellation_before_deadline():
    cancelled = threading.Event()
    cancelled.set()
    context = TaskContext(deadline=0.0, cancelled=cancelled, now_fn=lambda: 1.0)
    with pytest.raises(TaskCancelled, match="dispatcher"):
        context.check()


def test_check_reports_expired_deadline():
    context = TaskContext(deadline=1.0, cancelled=threading.Event(), now_fn=lambda: 2.0)
    with pytest.raises(TaskCancelled, match="deadline"):
        context.check()


def test_factory_preserves_shared_event_and_clock():
    cancelled = threading.Event()

    def now_fn():
        return 4.0

    context = task_context_for(deadline=9.0, cancelled=cancelled, now_fn=now_fn)
    assert context.deadline == 9.0
    assert context.cancelled is cancelled
    assert context.now_fn is now_fn
    context.check()
