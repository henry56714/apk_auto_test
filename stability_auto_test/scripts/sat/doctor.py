"""`sat doctor` — read-only environment / device capability self-check.

Doctor never modifies the device or the output directory; it only inspects and
reports. Capability problems (e.g. no root for /data/tombstones) are reported
as `unavailable`, not as doctor failures.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from .adb import Adb, AdbError
from .collectors.exit_info import exit_info_available
from .device import DeviceSetupError, get_device_info, is_package_installed, list_devices

log = logging.getLogger(__name__)


def _shell(adb: Adb, cmd: str) -> tuple:
    try:
        r = adb.shell(cmd, check=False, timeout=8.0)
        return r.returncode, r.stdout
    except AdbError as e:
        return 1, str(e)


def _check(name: str, status: str, detail: str = "") -> Dict:
    return {"name": name, "status": status, "detail": detail}


def _select_device(adb: Adb, device: Optional[str]) -> str:
    try:
        raw = list_devices(adb)
    except AdbError as e:
        raise DeviceSetupError(f"adb unavailable: {e}") from e
    if not raw:
        raise DeviceSetupError("no devices detected (run `adb devices`)")
    states = dict(raw)
    if device is not None:
        if device not in states:
            raise DeviceSetupError(f"device '{device}' not found; connected: {list(states)}")
        state = states[device]
        if state != "device":
            raise DeviceSetupError(f"device '{device}' is {state} (expected 'device')")
        return device
    online = [s for s, st in states.items() if st == "device"]
    if not online:
        detail = ", ".join(f"{s} ({st})" for s, st in states.items())
        raise DeviceSetupError(f"no online devices; connected but unavailable: {detail}")
    if len(online) > 1:
        raise DeviceSetupError(f"multiple devices online ({online}); pass --device")
    return online[0]


def run_doctor(
    adb: Adb,
    package: str,
    *,
    device: Optional[str] = None,
    output_dir: Optional[Path] = None,
) -> Dict:
    checks: List[Dict] = []

    try:
        adb.run(["version"], retries=0, timeout=3.0)
        checks.append(_check("adb", "ok", "adb executable reachable"))
    except AdbError as e:
        raise DeviceSetupError(f"adb not usable: {e}") from e

    chosen = _select_device(adb, device)
    if adb.serial is None:
        adb.serial = chosen
    checks.append(_check("device", "ok", f"serial={chosen} state=device"))

    info = get_device_info(adb, serial=chosen)
    checks.append(
        _check(
            "android_version",
            "ok",
            f"Android {info.android_version} (sdk {info.sdk_int}, {info.cpu_cores} cores)",
        )
    )

    rc, out = _shell(adb, "getprop ro.product.cpu.abi; getprop ro.product.cpu.abilist")
    abi_detail = " ".join(line.strip() for line in out.splitlines() if line.strip())
    checks.append(
        _check(
            "cpu_abi",
            "ok" if abi_detail else "unavailable",
            abi_detail or "no ABI reported",
        )
    )

    checks.append(
        _check(
            "exit_info",
            "ok" if exit_info_available(adb) else "unavailable",
            "dumpsys activity exit-info reachable"
            if exit_info_available(adb)
            else "dumpsys activity exit-info unavailable (pre-API-30 or restricted)",
        )
    )

    installed = is_package_installed(adb, package)
    checks.append(
        _check(
            "package_installed",
            "ok" if installed else "fail",
            f"package={package}" + ("" if installed else " (not installed)"),
        )
    )

    rc, out = _shell(adb, f"pidof {package}")
    running = rc == 0 and out.strip()
    checks.append(
        _check(
            "process_state",
            "running" if running else "not_running",
            out.strip() or "no process found",
        )
    )

    rc, out = _shell(adb, "logcat -d -b main -t 1")
    checks.append(
        _check(
            "logcat_buffer",
            "ok" if rc == 0 else "unavailable",
            out.strip()[:160],
        )
    )

    rc, out = _shell(adb, "dumpsys dropbox --print | head -n 5")
    checks.append(
        _check(
            "dropbox",
            "ok" if rc == 0 and out.strip() else "unavailable",
            out.strip()[:160] or "no dropbox content",
        )
    )

    rc, out = _shell(adb, "ls /data/tombstones/ 2>/dev/null | head -n 1")
    tomb_ok = rc == 0 and bool(out.strip())
    checks.append(
        _check(
            "tombstone_permission",
            "ok" if tomb_ok else "unavailable",
            out.strip() or "no access (likely non-root user build)",
        )
    )

    rc, out = _shell(adb, "ls /data/anr/ 2>/dev/null | head -n 1")
    anr_ok = rc == 0 and bool(out.strip())
    checks.append(
        _check(
            "anr_trace_permission",
            "ok" if anr_ok else "unavailable",
            out.strip() or "no access (likely non-root user build)",
        )
    )

    out_dir = Path(output_dir) if output_dir else Path("./reports")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".doctor-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        disk = shutil.disk_usage(out_dir)
        checks.append(
            _check(
                "output_dir",
                "ok",
                f"{out_dir} writable; free {disk.free // (1024 * 1024)} MiB",
            )
        )
    except OSError as e:
        checks.append(_check("output_dir", "fail", str(e)))

    symbolizers = []
    for tool in ("llvm-symbolizer", "retrace", "jadx"):
        path = shutil.which(tool)
        if path:
            symbolizers.append(f"{tool}={path}")
    checks.append(
        _check(
            "symbolization_tools",
            "ok" if symbolizers else "unavailable",
            ", ".join(symbolizers) or "no symbolizer found",
        )
    )

    return {
        "ok": True,
        "device": chosen,
        "android_version": info.android_version,
        "sdk_int": info.sdk_int,
        "package": package,
        "checks": checks,
    }
