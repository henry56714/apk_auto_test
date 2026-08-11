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
    _adb(device, "shell", "monkey", "-p", package,
         "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(2)


def _start_monitor(device: str, package: str, out: Path, duration: int = 20, extra=None):
    cmd = [
        sys.executable, "-m", "sat",
        "--package", package,
        "--device", device,
        "--duration", f"{duration}s",
        "--min-coverage", "0.8",
        "--output", str(out),
    ]
    cmd += list(extra or [])
    return subprocess.Popen(
        cmd, cwd=SCRIPTS, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
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
    device_serial, target_package, tmp_path,
):
    out = tmp_path / "java-crash"
    _launch(device_serial, target_package)
    proc = _start_monitor(
        device_serial, target_package, out, duration=20, extra=["--ci"],
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
    device_serial, target_package, tmp_path,
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
    device_serial, target_package, tmp_path,
):
    _launch(device_serial, target_package)
    ok, reason = faults.native_sigsegv(device_serial, target_package)
    if not ok:
        pytest.skip(reason)

    out = tmp_path / "native-crash"
    proc = _start_monitor(device_serial, target_package, out, duration=15)
    _wait(proc)
    report = _report(out)
    types = [i["type"] for i in report["incidents"]]
    assert "native_crash" in types


@pytest.mark.stability_e2e
def test_anr_injection_capability_reported(device_serial, target_package):
    ok, reason = faults.anr(device_serial, target_package)
    if ok:
        pytest.skip("ANR injection supported on this build; covered by L1")
    pytest.skip(reason)


@pytest.mark.stability_e2e
def test_junit_counts_match_report(
    device_serial, target_package, tmp_path,
):
    out = tmp_path / "junit-run"
    _launch(device_serial, target_package)
    cmd = [
        sys.executable, "-m", "sat",
        "--package", target_package,
        "--device", device_serial,
        "--duration", "12s",
        "--min-coverage", "0.8",
        "--ci",
        "--output", str(out),
        "--junit", str(tmp_path / "junit.xml"),
    ]
    proc = subprocess.Popen(
        cmd, cwd=SCRIPTS, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    _wait(proc)
    report = _report(out)
    root = ET.parse(tmp_path / "junit.xml").getroot()
    assert int(root.attrib["tests"]) == len(report["issue_groups"])
    assert int(root.attrib["failures"]) == sum(
        1 for i in report["issue_groups"] if report["policy"]["passed"] is False
    )
