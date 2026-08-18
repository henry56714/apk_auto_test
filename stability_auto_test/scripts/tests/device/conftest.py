"""Device test fixtures: Fault Lab install + real `python -m sat` runs.

Spec §7.1: every L2 case uses its own output dir, run id and fault id; sync on
`SAT_FAULT_BEGIN` markers, process state, ExitInfo or report state — never on
fixed sleeps alone. All assertions read the authoritative `report.json`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pytest

FAULT_PKG = "com.example.stabilityfaultlab"
FAULT_RECEIVER = f"{FAULT_PKG}/.FaultReceiver"
TRIGGER_ACTION = f"{FAULT_PKG}.TRIGGER"
RESET_ACTION = f"{FAULT_PKG}.RESET"


def pytest_addoption(parser):
    parser.addoption(
        "--device",
        action="store",
        default=None,
        help="ADB serial for device tests",
    )
    parser.addoption(
        "--fault-apk",
        action="store",
        default=None,
        help="Path to the Stability Fault Lab debug APK",
    )


@dataclass
class Adb:
    serial: str

    def run(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["adb", "-s", self.serial, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def shell(self, cmd: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
        return self.run("shell", cmd, timeout=timeout)

    def logcat_text(self, extra: List[str] = (), timeout: float = 30.0) -> str:
        r = self.run("logcat", "-d", *extra, timeout=timeout)
        return r.stdout or ""


@pytest.fixture(scope="session")
def adb(request) -> Adb:
    serial = request.config.getoption("--device")
    if not serial:
        pytest.skip("--device is required for device tests")
    probe = subprocess.run(["adb", "-s", serial, "get-state"], capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.strip() != "device":
        pytest.skip(f"device {serial} not online: {probe.stdout.strip()}")
    return Adb(serial)


@pytest.fixture(scope="session")
def fault_lab(adb: Adb, request) -> None:
    """Install + launch the Fault Lab APK once per session; RESET at start."""
    apk = request.config.getoption("--fault-apk")
    if not apk or not Path(apk).exists():
        pytest.skip("--fault-apk is required for device tests")
    r = adb.run("install", "-r", "-t", str(Path(apk).resolve()), timeout=180.0)
    assert "Success" in r.stdout or r.returncode == 0, f"install failed: {r.stdout} {r.stderr}"
    # Fresh app data every session: a leftover startup-crash flag (or a
    # busy-looping main thread from a previous session) would make RESET
    # impossible. pm clear + force-stop are the spec §7.1.2 fallbacks.
    adb.run("shell", "am", "force-stop", FAULT_PKG, timeout=30.0)
    adb.run("shell", "pm", "clear", FAULT_PKG, timeout=60.0)
    ensure_app_running(adb)
    reset_fault_lab(adb)
    yield
    reset_fault_lab(adb)
    adb.run("shell", "am", "force-stop", FAULT_PKG)


@pytest.fixture(autouse=True)
def _app_running_between_tests(adb: Adb):
    """Every test starts from a clean app state: force-stop + fresh launch
    (a previous test may leave the main thread blocked / crash-looping)."""
    adb.run("shell", "am", "force-stop", FAULT_PKG, timeout=30.0)
    ensure_app_running(adb)
    yield


def ensure_app_running(adb: Adb) -> None:
    """Start MainActivity when the Fault Lab is not running.

    Robust against a wedged system_server right after `install -r` (package
    update processing): never blocks a single `am start -W` forever, always
    falls back to polling `pidof` + the READY marker.
    """
    for _attempt in range(3):
        if adb.shell(f"pidof {FAULT_PKG}").stdout.strip().split():
            return
        # Wait until the package manager finished processing the update.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            pm = adb.shell(f"pm path {FAULT_PKG}").stdout.strip()
            if "package:" in pm:
                break
            time.sleep(0.5)
        adb.run("logcat", "-c", timeout=15.0)  # fresh marker evidence
        try:
            adb.run(
                "shell", "am", "start", "-W", "-n",
                f"{FAULT_PKG}/.MainActivity",
                timeout=20.0,
            )
        except subprocess.TimeoutExpired:
            pass  # the launch may still have succeeded; verify below
        # Sync on process presence, then on the READY marker.
        deadline = time.monotonic() + 45.0
        running = False
        while time.monotonic() < deadline:
            if adb.shell(f"pidof {FAULT_PKG}").stdout.strip().split():
                running = True
                break
            time.sleep(0.5)
        if running:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                text = adb.logcat_text(extra=["-s", "SAT:I", "-t", "200"])
                if f"SAT_FAULT_READY process={FAULT_PKG}" in text:
                    return
                time.sleep(0.5)
            return  # process up; READY marker best-effort
    raise RuntimeError("Fault Lab app failed to reach READY")


def adb_clear_logcat(adb: Adb) -> None:
    adb.run("logcat", "-c", timeout=15.0)


def trigger_fault(
    adb: Adb,
    fault: str,
    *,
    fault_id: Optional[str] = None,
    wait_begin: bool = True,
    timeout: float = 30.0,
) -> str:
    """Broadcast a fault and wait for its SAT_FAULT_BEGIN marker."""
    fault_id = fault_id or f"{fault.lower()}-{uuid.uuid4().hex[:8]}"
    r = adb.run(
        "shell",
        "am",
        "broadcast",
        "-n",
        FAULT_RECEIVER,
        "-a",
        TRIGGER_ACTION,
        "--es",
        "fault",
        fault,
        "--es",
        "fault_id",
        fault_id,
        timeout=90.0,
    )
    assert r.returncode == 0, f"broadcast failed: {r.stdout} {r.stderr}"
    if wait_begin:
        marker = wait_for_marker(adb, fault_id, timeout=timeout)
        assert marker is not None, f"SAT_FAULT_BEGIN for {fault_id} not seen in logcat"
    return fault_id


def reset_fault_lab(adb: Adb) -> None:
    """Send RESET and wait for the cleanup-completion marker (async teardown)."""
    adb.run(
        "shell",
        "am",
        "broadcast",
        "-n",
        FAULT_RECEIVER,
        "-a",
        RESET_ACTION,
        timeout=90.0,
    )
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        text = adb.logcat_text(extra=["-s", "SAT:V", "-t", "100"])
        if "SAT_FAULT_END id=RESET" in text:
            return
        time.sleep(0.5)
    # Fallback: deletion may still be running; the next test's prechecks
    # tolerate it (never a blind pass).


def wait_for_marker(adb: Adb, fault_id: str, *, timeout: float = 30.0) -> Optional[str]:
    """Poll logcat until `SAT_FAULT_BEGIN id=<fault_id>` appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = adb.logcat_text(extra=["-s", "SAT:V", "-t", "300"])
        for line in text.splitlines():
            if f"SAT_FAULT_BEGIN id={fault_id}" in line:
                return line
        time.sleep(0.5)
    return None


def wait_for(
    condition,
    *,
    timeout: float = 60.0,
    interval: float = 0.5,
    description: str = "condition",
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {description}")


@dataclass
class SatRun:
    """One real `python -m sat` subprocess run against the device."""

    adb: Adb
    output_dir: Path
    package: str
    duration_sec: float
    extra_args: List[str] = field(default_factory=list)
    proc: Optional[subprocess.Popen] = None
    manifest_actions: List[dict] = field(default_factory=list)

    def _cmd(self) -> List[str]:
        return [
            sys.executable,
            "-m",
            "sat",
            "--package",
            self.package,
            "--device",
            self.adb.serial,
            "--duration",
            f"{self.duration_sec:.0f}s",
            "--output",
            str(self.output_dir),
        ] + list(self.extra_args)

    def start(self) -> "SatRun":
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stdout_path = self.output_dir.parent / f"sat-{self.output_dir.name}.out"
        self.stdout_fh = open(self.stdout_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            self._cmd(),
            stdout=self.stdout_fh,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return self

    def wait_monitoring(self, timeout: float = 60.0) -> None:
        """Wait until the run's collector status shows logcat collecting."""

        def collecting() -> bool:
            status_path = self.output_dir / "status.json"
            if not status_path.exists():
                return False
            try:
                status = json.loads(status_path.read_text())
            except (OSError, json.JSONDecodeError):
                return False
            collectors = status.get("collectors") or {}
            logcat = collectors.get("logcat") or {}
            return logcat.get("lines_read", 0) > 0

        wait_for(collecting, timeout=timeout, description="logcat collecting")

    def write_manifest_action(
        self,
        *,
        fault_id: str,
        expected_exit: bool,
        window_sec: float = 120.0,
        started_at: Optional[str] = None,
    ) -> None:
        self.manifest_actions.append(
            {
                "id": f"action-{fault_id}",
                "fault_id": fault_id,
                "expected_exit": expected_exit,
                "window_sec": window_sec,
                "started_at": started_at or time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            }
        )

    def flush_manifest(self) -> None:
        manifest = {
            "type": "device_test",
            "status": "ok",
            "actions": list(self.manifest_actions),
        }
        (self.output_dir / "workload_manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

    def wait_exit(self, timeout: float = 300.0) -> int:
        assert self.proc is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                return rc
            time.sleep(0.5)
        raise TimeoutError("sat process did not exit in time")

    def report(self) -> dict:
        path = self.output_dir / "report.json"
        if not path.exists():
            tail = ""
            if getattr(self, "stdout_path", None) and self.stdout_path.exists():
                tail = "\n".join(self.stdout_path.read_text(encoding="utf-8").splitlines()[-20:])
            raise AssertionError(
                f"report.json missing in {self.output_dir}\n--- sat output ---\n{tail}"
            )
        report = json.loads(path.read_text(encoding="utf-8"))
        # Schema validation (spec §7.1.4).
        import jsonschema

        schema = json.loads(
            (Path(__file__).parent.parent.parent / "schemas" / "report.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.validate(report, schema)
        return report

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def __enter__(self) -> "SatRun":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


@pytest.fixture
def sat_run(adb: Adb, tmp_path: Path):
    """Factory for short monitor runs (each test gets its own dir/id).

    `device` may be a different serial (multi-device matrix tests).
    """
    runs: List[SatRun] = []

    def factory(
        duration_sec: float = 30.0,
        *,
        package: str = FAULT_PKG,
        extra_args: Optional[List[str]] = None,
        device: Optional[str] = None,
    ) -> SatRun:
        run = SatRun(
            adb=Adb(device) if device else adb,
            output_dir=tmp_path / f"run-{uuid.uuid4().hex[:8]}",
            package=package,
            duration_sec=duration_sec,
            extra_args=list(extra_args or []),
        )
        runs.append(run)
        return run

    yield factory
    for run in runs:
        run.stop()
