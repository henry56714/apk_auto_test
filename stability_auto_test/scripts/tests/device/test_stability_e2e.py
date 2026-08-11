"""L2 end-to-end stability tests on a real device.

Run:
    python -m pytest tests/device -m stability_e2e -q \
        --device "$SAT_DEVICE" --package "$SAT_FAULT_PACKAGE"
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.device import faults

SCRIPTS = Path(__file__).resolve().parent.parent.parent


def _adb(device: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["adb", "-s", device, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _launch(device: str, package: str) -> None:
    # Always force-stop first so we get a clean launch.
    _adb(device, "shell", "am", "force-stop", package)
    time.sleep(0.5)
    # Find launcher activity from dumpsys first, then start it directly.
    info = _adb(device, "shell", "dumpsys", "package", package)
    activity = None
    for line in info.stdout.splitlines():
        if package + "/" in line and "filter" in line:
            activity = line.strip().split()[1]
            break
    if activity:
        _adb(device, "shell", "am", "start", "-n", activity)
    else:
        _adb(
            device,
            "shell",
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
    time.sleep(2)
    # Verify process started.
    for _ in range(5):
        pid = _adb(device, "shell", "pidof", package).stdout.strip()
        if pid:
            break
        time.sleep(1)


def _start_monitor(device: str, package: str, out: Path, duration: int = 20, extra=None):
    cmd = [
        sys.executable,
        "-m",
        "sat",
        "--package",
        package,
        "--device",
        device,
        "--duration",
        f"{duration}s",
        "--min-coverage",
        "0.8",
        "--output",
        str(out),
    ]
    cmd += list(extra or [])
    return subprocess.Popen(
        cmd,
        cwd=SCRIPTS,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait(proc, timeout: float = 90) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("sat run did not finish in time")


def _report(out: Path) -> dict:
    return json.loads((out / "report.json").read_text(encoding="utf-8"))


@pytest.mark.stability_e2e
def test_java_crash_detected_and_ci_gate_fails(
    device_serial,
    target_package,
    tmp_path,
):
    out = tmp_path / "java-crash"
    _launch(device_serial, target_package)
    proc = _start_monitor(
        device_serial,
        target_package,
        out,
        duration=20,
        extra=["--ci"],
    )
    time.sleep(3)
    ok, err = faults.java_crash(device_serial, target_package)
    assert ok, err
    rc = _wait(proc)

    report = _report(out)
    types = [i["type"] for i in report["incidents"]]
    assert "java_crash" in types
    assert report["issue_groups"], "expected at least one issue group"
    assert report["issue_groups"][0]["occurrence_count"] >= 1
    assert rc == 1, "CI gate must fail on a java crash"


@pytest.mark.stability_e2e
def test_normal_force_stop_is_not_a_crash(
    device_serial,
    target_package,
    tmp_path,
):
    out = tmp_path / "normal-exit"
    _launch(device_serial, target_package)
    proc = _start_monitor(device_serial, target_package, out, duration=15)
    time.sleep(3)
    ok, err = faults.normal_exit(device_serial, target_package)
    assert ok, err
    _wait(proc)

    report = _report(out)
    types = [i["type"] for i in report["incidents"]]
    assert "java_crash" not in types
    assert "native_crash" not in types
    assert "anr" not in types
    assert report["exit_info"], "exit-info records expected on Android 11+"


@pytest.mark.stability_e2e
def test_native_sigsegv_detected_when_permitted(
    device_serial,
    target_package,
    tmp_path,
):
    _launch(device_serial, target_package)
    ok, reason = faults.native_sigsegv(device_serial, target_package)
    if not ok:
        pytest.skip(reason)

    # Re-launch (SIGSEGV killed the process) and start monitor BEFORE crash.
    _launch(device_serial, target_package)
    out = tmp_path / "native-crash"
    proc = _start_monitor(device_serial, target_package, out, duration=15)
    time.sleep(3)
    ok2, reason2 = faults.native_sigsegv(device_serial, target_package)
    assert ok2, reason2
    _wait(proc)
    report = _report(out)
    types = [i["type"] for i in report["incidents"]]
    assert "native_crash" in types


@pytest.mark.stability_e2e
def test_anr_injection_capability_reported(device_serial, target_package, tmp_path):
    ok, reason = faults.anr(device_serial, target_package)
    if not ok:
        pytest.skip(reason)

    # ANR injection available — start monitor, trigger ANR, verify detection.
    out = tmp_path / "anr-run"
    proc = _start_monitor(device_serial, "com.anr.test", out, duration=25)
    time.sleep(3)
    ok2, reason2 = faults.trigger_anr(device_serial)
    assert ok2, reason2
    _wait(proc)
    report = _report(out)
    types = [i["type"] for i in report["incidents"]]
    assert "anr" in types, f"Expected ANR incident, got types={types}"


@pytest.mark.stability_e2e
def test_junit_counts_match_report(
    device_serial,
    target_package,
    tmp_path,
):
    out = tmp_path / "junit-run"
    _launch(device_serial, target_package)
    cmd = [
        sys.executable,
        "-m",
        "sat",
        "--package",
        target_package,
        "--device",
        device_serial,
        "--duration",
        "12s",
        "--min-coverage",
        "0.8",
        "--ci",
        "--output",
        str(out),
        "--junit",
        str(tmp_path / "junit.xml"),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=SCRIPTS,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(3)
    ok, err = faults.java_crash(device_serial, target_package)
    assert ok, err
    _wait(proc)
    report = _report(out)
    root = ET.parse(tmp_path / "junit.xml").getroot()
    assert int(root.attrib["tests"]) >= len(report["issue_groups"])
    assert int(root.attrib["failures"]) == sum(
        1 for i in report["issue_groups"] if report["policy"]["passed"] is False
    )
