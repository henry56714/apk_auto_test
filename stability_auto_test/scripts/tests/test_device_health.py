from __future__ import annotations

import time
from types import SimpleNamespace

from sat.adb import AdbError
from sat.collectors.device_health import DeviceHealthMonitor, DeviceSnapshot


class _FakeAdb:
    def __init__(self, responses):
        self.responses = {key: list(value) for key, value in responses.items()}

    def shell(self, command, **kwargs):
        values = self.responses[command]
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(stdout=value)


def test_boot_id_change_forms_reboot_event():
    snaps = [
        DeviceSnapshot(boot_id="a", boot_completed=True),
        DeviceSnapshot(boot_id="b", boot_completed=False),
        DeviceSnapshot(boot_id="b", boot_completed=True),
        DeviceSnapshot(boot_id="b", boot_completed=True),
        DeviceSnapshot(boot_id="b", boot_completed=True),
    ]
    idx = {"n": 0}

    def query():
        s = snaps[min(idx["n"], len(snaps) - 1)]
        idx["n"] += 1
        return s

    monitor = DeviceHealthMonitor(
        None,  # type: ignore[arg-type]
        interval_sec=0.01,
        query_fn=query,
        now_fn=time.time,
    )
    monitor.start()
    time.sleep(0.15)
    monitor.stop()
    events = monitor.events()
    types = [e.event_type for e in events]
    assert "reboot" in types
    assert "recovered" in types
    assert monitor.pid_epoch >= 1


def test_adb_offline_gap_tracked():
    clock = {"t": 100.0}

    def now_fn():
        clock["t"] += 1.0
        return clock["t"]

    states = [
        DeviceSnapshot(boot_id="x", boot_completed=True, state="device"),
        DeviceSnapshot(boot_id="x", boot_completed=True, state="offline"),
        DeviceSnapshot(boot_id="x", boot_completed=True, state="offline"),
        DeviceSnapshot(boot_id="x", boot_completed=True, state="device"),
        DeviceSnapshot(boot_id="x", boot_completed=True, state="device"),
    ]
    idx = {"n": 0}

    def query():
        s = states[min(idx["n"], len(states) - 1)]
        idx["n"] += 1
        return s

    monitor = DeviceHealthMonitor(
        None,  # type: ignore[arg-type]
        interval_sec=0.01,
        query_fn=query,
        now_fn=now_fn,
    )
    monitor.start()
    time.sleep(0.1)
    monitor.stop()
    offline = [e for e in monitor.events() if e.event_type == "offline"]
    assert offline
    assert offline[0].ended_at is not None
    assert offline[0].ended_at - offline[0].started_at >= 1.0


def test_default_query_reads_kernel_boot_id_and_device_state():
    adb = _FakeAdb(
        {
            "cat /proc/sys/kernel/random/boot_id": ["boot-a\n"],
            "getprop sys.boot_completed": ["1\n"],
            "cat /proc/uptime": ["123.5 22.0\n"],
        }
    )
    snapshot = DeviceHealthMonitor(adb)._default_query()
    assert snapshot == DeviceSnapshot(
        boot_id="boot-a",
        boot_completed=True,
        uptime_sec=123.5,
        state="device",
    )


def test_default_query_falls_back_to_boot_property_and_bad_uptime_zero():
    adb = _FakeAdb(
        {
            "cat /proc/sys/kernel/random/boot_id": [""],
            "getprop ro.boot.boot_id": ["legacy-boot\n"],
            "getprop sys.boot_completed": ["0\n"],
            "cat /proc/uptime": ["unavailable idle\n"],
        }
    )
    snapshot = DeviceHealthMonitor(adb)._default_query()
    assert snapshot.boot_id == "legacy-boot"
    assert snapshot.boot_completed is False
    assert snapshot.uptime_sec == 0.0
    assert snapshot.state == "device"


def test_default_query_maps_adb_or_empty_uptime_to_offline():
    adb = _FakeAdb(
        {
            "cat /proc/sys/kernel/random/boot_id": [AdbError("offline")],
        }
    )
    assert DeviceHealthMonitor(adb)._default_query().state == "offline"

    empty_uptime = _FakeAdb(
        {
            "cat /proc/sys/kernel/random/boot_id": ["boot-a"],
            "getprop sys.boot_completed": ["1"],
            "cat /proc/uptime": [""],
        }
    )
    assert DeviceHealthMonitor(empty_uptime)._default_query().state == "offline"


def test_gap_callbacks_are_idempotent_and_event_snapshots_are_copies():
    started = []
    recovered = []
    monitor = DeviceHealthMonitor(
        None,  # type: ignore[arg-type]
        on_gap_started=started.append,
        on_recovered=lambda: recovered.append(True),
    )
    monitor._begin_gap("offline", 10.0)
    monitor._begin_gap("reboot", 11.0)
    monitor._end_gap(12.0, "recovered")
    monitor._end_gap(13.0, "ignored")

    events = monitor.events()
    assert started == ["offline"]
    assert recovered == [True]
    assert [event.event_type for event in events] == ["offline", "recovered"]
    assert events[0].to_dict() == {
        "event_type": "offline",
        "started_at": 10.0,
        "ended_at": 12.0,
        "detail": "recovered",
    }
    events[0].detail = "mutated"
    assert monitor.events()[0].detail == "recovered"


def test_uptime_regression_without_boot_id_is_inferred_as_reboot():
    snapshots = iter(
        [
            DeviceSnapshot(boot_id="", uptime_sec=500.0),
            DeviceSnapshot(boot_id="", uptime_sec=5.0),
            DeviceSnapshot(boot_id="", uptime_sec=5.0),
        ]
    )
    last = DeviceSnapshot(boot_id="", uptime_sec=5.0)
    monitor = DeviceHealthMonitor(
        None,  # type: ignore[arg-type]
        interval_sec=0.005,
        query_fn=lambda: next(snapshots, last),
    )
    monitor.start()
    time.sleep(0.05)
    monitor.stop()

    reboot = [event for event in monitor.events() if event.event_type == "reboot"]
    assert len(reboot) == 1
    assert "uptime regression 500s -> 5s" in reboot[0].detail
    assert monitor.pid_epoch == 1
