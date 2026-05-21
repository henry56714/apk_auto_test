"""Device selection and pre-flight checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .adb import Adb, AdbError, AdbNotFound


class DeviceSetupError(RuntimeError):
    """Pre-flight check failed; not retriable."""


@dataclass
class DeviceInfo:
    serial: str
    android_version: str
    sdk_int: int
    cpu_cores: int


def list_devices(adb: Adb) -> List[Tuple[str, str]]:
    """Return list of (serial, state) for each device line in `adb devices`."""
    r = adb.run(["devices"], retries=0)
    out: List[Tuple[str, str]] = []
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s or s.lower().startswith("list of devices"):
            continue
        # Tab-separated: "<serial>\t<state>"
        parts = s.split("\t") if "\t" in s else s.split()
        if len(parts) >= 2:
            out.append((parts[0].strip(), parts[1].strip()))
    return out


def select_device(serial: Optional[str], devices: List[Tuple[str, str]]) -> str:
    online = [(s, st) for s, st in devices if st == "device"]
    if not online:
        raise DeviceSetupError("no online devices (run `adb devices`)")
    if serial is not None:
        for s, _ in online:
            if s == serial:
                return s
        raise DeviceSetupError(
            f"device '{serial}' not online; online devices: {[s for s, _ in online]}"
        )
    if len(online) == 1:
        return online[0][0]
    raise DeviceSetupError(
        f"{len(online)} devices online ({[s for s, _ in online]}); pass --device"
    )


def is_package_installed(adb: Adb, package: str) -> bool:
    try:
        r = adb.shell(f"pm list packages {package}", check=False, timeout=5.0)
    except AdbError:
        return False
    if r.returncode != 0:
        return False
    target = f"package:{package}"
    return any(line.strip() == target for line in r.stdout.splitlines())


def get_device_info(adb: Adb, serial: str = "") -> DeviceInfo:
    version = _getprop(adb, "ro.build.version.release")
    sdk_str = _getprop(adb, "ro.build.version.sdk")
    cores = _read_cpu_cores(adb)
    try:
        sdk_int = int(sdk_str)
    except ValueError:
        sdk_int = 0
    return DeviceInfo(
        serial=serial,
        android_version=version,
        sdk_int=sdk_int,
        cpu_cores=cores,
    )


def _getprop(adb: Adb, prop: str) -> str:
    try:
        r = adb.shell(f"getprop {prop}", check=False, timeout=3.0)
    except AdbError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


_NPROC_RE = re.compile(r"^\s*(\d+)\s*$")


def _read_cpu_cores(adb: Adb) -> int:
    """Return online CPU count. Prefer `nproc`; fall back to /proc/cpuinfo."""
    try:
        r = adb.shell("nproc", check=False, timeout=3.0)
    except AdbError:
        r = None
    if r and r.returncode == 0:
        m = _NPROC_RE.match(r.stdout.strip())
        if m:
            return int(m.group(1))
    try:
        r2 = adb.shell("cat /proc/cpuinfo", check=False, timeout=3.0)
    except AdbError:
        return 1
    if r2 and r2.returncode == 0:
        n = sum(1 for line in r2.stdout.splitlines() if line.startswith("processor"))
        if n > 0:
            return n
    return 1


def preflight(
    adb: Adb,
    *,
    serial: Optional[str],
    package: str,
) -> DeviceInfo:
    """Run all startup checks; raise DeviceSetupError with a clear message on failure.

    Returns the resolved DeviceInfo (with the chosen serial filled in).
    Caller still needs `wait_for_processes` separately, since the app may not
    be running yet at this point.
    """
    try:
        adb.run(["version"], retries=0, timeout=3.0)
    except AdbNotFound as e:
        raise DeviceSetupError(f"adb not found: {e}") from e
    except AdbError as e:
        raise DeviceSetupError(f"adb not usable: {e}") from e

    devices = list_devices(adb)
    chosen = select_device(serial, devices)
    # Re-bind adb to the chosen serial for subsequent calls if caller used auto-select.
    if adb.serial is None:
        adb.serial = chosen

    if not is_package_installed(adb, package):
        raise DeviceSetupError(
            f"package '{package}' not installed on device {chosen}"
        )

    return get_device_info(adb, serial=chosen)
