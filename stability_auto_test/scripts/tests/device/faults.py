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
    """ANR injection requires a debuggable/eng build; covered by L1 fixtures."""
    return False, "ANR injection unavailable on user build (use L1 fixtures)"


def low_memory(device: str, package: str) -> Tuple[bool, str]:
    return False, "low-memory injection unavailable on user build"
