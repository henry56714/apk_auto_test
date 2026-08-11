"""Device fault-injection fixtures (capability-probed, user-build safe).

Provides deterministic injections that work on user builds plus explicit
capability reports for injections that need root / eng builds (native SIGSEGV,
ANR, low-memory). Nothing here ever hangs the whole device.
"""

from __future__ import annotations

import subprocess
from typing import Tuple


def _adb(device: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["adb", "-s", device, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def java_crash(device: str, package: str) -> Tuple[bool, str]:
    r = _adb(device, "shell", "am", "crash", package)
    return r.returncode == 0, r.stderr.strip() or r.stdout.strip()


def normal_exit(device: str, package: str) -> Tuple[bool, str]:
    r = _adb(device, "shell", "am", "force-stop", package)
    return r.returncode == 0, r.stderr.strip() or r.stdout.strip()


def native_sigsegv(device: str, package: str) -> Tuple[bool, str]:
    """Best-effort SIGSEGV; shell may lack permission on user builds."""
    pid = _adb(device, "shell", "pidof", package).stdout.strip()
    if not pid:
        return False, "target process not running"
    r = _adb(device, "shell", "kill", "-11", pid)
    if r.returncode != 0:
        return False, f"kill not permitted: {r.stderr.strip()}"
    return True, f"sent SIGSEGV to pid {pid}"


def anr(device: str, package: str) -> Tuple[bool, str]:
    """Check if ANR injection via com.anr.test is available."""
    r = _adb(device, "shell", "pm", "list", "packages", "com.anr.test")
    if "com.anr.test" not in r.stdout:
        return False, "com.anr.test not installed (build with Android SDK)"
    return True, "com.anr.test available for ANR injection"


def trigger_anr(device: str) -> Tuple[bool, str]:
    """Actually trigger ANR: launch com.anr.test + send touch events."""
    import time as _time

    _adb(device, "shell", "am", "force-stop", "com.anr.test")
    _time.sleep(0.5)
    _adb(device, "shell", "am", "start", "-n", "com.anr.test/.AnrActivity")
    _time.sleep(2.0)
    for _ in range(8):
        _adb(device, "shell", "input", "tap", "540", "1200")
        _time.sleep(0.25)
    _time.sleep(5)
    r = _adb(device, "shell", "ls", "-t", "/data/anr/")
    if r.returncode == 0 and "anr_" in r.stdout:
        return True, "ANR triggered via com.anr.test"
    return False, "ANR trigger produced no trace"


def low_memory(device: str, package: str) -> Tuple[bool, str]:
    return False, "low-memory injection unavailable on user build"
