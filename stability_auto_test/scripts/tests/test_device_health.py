from __future__ import annotations

import time

from sat.collectors.device_health import DeviceHealthMonitor, DeviceSnapshot


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
