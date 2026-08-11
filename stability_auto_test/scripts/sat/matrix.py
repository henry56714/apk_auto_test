"""Multi-device parallel matrix runner.

Each device gets its own output sub-directory and its own `python -m sat`
process so ADB serial state can never cross-contaminate another worker.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional


@dataclass
class MatrixResult:
    device: str
    output_dir: Path
    returncode: int
    timed_out: bool = False
    error: Optional[str] = None


def _sanitize(serial: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in serial)


def launch_package_on(device: str, package: str) -> None:
    """Best-effort launch so monitor-only workers find the target process."""
    probe = subprocess.run(
        ["adb", "-s", device, "shell",
         f"cmd package resolve-activity --brief -c "
         f"android.intent.category.LAUNCHER {package}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    activity = None
    if probe.returncode == 0:
        for line in probe.stdout.splitlines():
            if line.strip().startswith(package) and "/" in line:
                activity = line.strip()
                break
    if activity:
        subprocess.run(
            ["adb", "-s", device, "shell", "am", "start", "-n", activity],
            capture_output=True,
            text=True,
            timeout=15,
        )
    else:
        subprocess.run(
            ["adb", "-s", device, "shell", "monkey", "-p", package,
             "-c", "android.intent.category.LAUNCHER", "1"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    time.sleep(2)


def run_matrix(
    *,
    package: str,
    devices: List[str],
    output_root: Path,
    duration_sec: int,
    max_parallel: int = 2,
    device_timeout_sec: float = 3600.0,
    extra_args: Optional[List[str]] = None,
    run_one: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> List[MatrixResult]:
    """Run one independent process per device."""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    results: List[MatrixResult] = []
    lock = threading.Lock()
    sem = threading.BoundedSemaphore(max(1, max_parallel))

    def one(device: str) -> None:
        sub = output_root / f"device_{_sanitize(device)}"
        with sem:
            try:
                if run_one is not None:
                    proc = run_one(device=device, output_dir=sub)
                    rc, timed_out, err = proc.returncode, False, None
                else:
                    cmd = [
                        sys.executable, "-m", "sat",
                        "--package", package,
                        "--device", device,
                        "--duration", f"{duration_sec}s",
                        "--output", str(sub),
                    ] + list(extra_args or [])
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=device_timeout_sec,
                    )
                    rc, timed_out, err = proc.returncode, False, None
            except subprocess.TimeoutExpired:
                rc, timed_out, err = 124, True, "device timeout"
            except Exception as e:  # noqa: BLE001 - keep matrix going
                rc, timed_out, err = 2, False, str(e)
            with lock:
                results.append(MatrixResult(
                    device=device, output_dir=sub,
                    returncode=rc, timed_out=timed_out, error=err,
                ))

    threads = [threading.Thread(target=one, args=(d,)) for d in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=device_timeout_sec + 60)
    return results
