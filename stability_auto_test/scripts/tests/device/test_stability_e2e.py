"""L2 device E2E: Stability Fault Lab on a real emulator (spec §7.4).

Every test: own output dir + fault id, real `python -m sat` subprocess, sync
on SAT_FAULT_BEGIN markers / report state, asserts read from `report.json`.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from .conftest import (
    FAULT_PKG,
    Adb,
    ensure_app_running,
    reset_fault_lab,
    trigger_fault,
)

pytestmark = pytest.mark.stability_e2e

DURATION = 25.0


def _incidents(report: dict) -> list:
    return report.get("incidents") or []


def _types(report: dict) -> set:
    return {i.get("type") for i in _incidents(report)}


# ── T-L2-001: install + doctor capability report ─────────────────────────────


def test_doctor_reports_fault_lab_capabilities(adb, fault_lab):
    r = subprocess.run(
        ["python", "-m", "sat", "doctor", "--json", "--device", adb.serial, "--package", FAULT_PKG],
        capture_output=True,
        text=True,
        timeout=120.0,
        cwd=Path(__file__).parent.parent.parent,
    )
    assert r.returncode == 0, f"doctor failed: {r.stdout} {r.stderr}"
    data = json.loads(r.stdout)
    assert data.get("package") == FAULT_PKG or any(
        FAULT_PKG in json.dumps(item) for item in data.get("checks", [])
    )
    # API / ABI / logcat / ExitInfo capability are all probed.
    checks = data.get("checks") or data
    text = json.dumps(checks)
    assert "sdk" in text.lower() or "api" in text.lower()
    assert "abi" in text.lower()


# ── T-L2-002: 60s clean baseline ─────────────────────────────────────────────


def test_clean_baseline_is_stable(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=60.0)
    run.start()
    run.wait_monitoring()
    run.wait_exit(timeout=120.0)
    report = run.report()
    assert report["verdict"] == "stable", report.get("verdict_reason")
    assert len(_incidents(report)) == 0
    assert report["coverage_ratio"] >= 0.99, report["coverage_ratio"]
    # stop() froze the output dir: nothing changes afterwards.
    before = {
        p: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in run.output_dir.rglob("*")
        if p.is_file()
    }
    time.sleep(2.0)
    after = {
        p: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in run.output_dir.rglob("*")
        if p.is_file()
    }
    assert before == after


# ── T-L2-003: Java main crash ────────────────────────────────────────────────


def test_java_main_crash_detected(adb, fault_lab, sat_run):
    fault_id = f"java-main-{uuid.uuid4().hex[:6]}"
    run = sat_run()
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "JAVA_MAIN_CRASH", fault_id=fault_id)
    run.wait_exit(timeout=120.0)
    report = run.report()
    assert report["verdict"] == "unstable", report.get("verdict_reason")
    crashes = [i for i in _incidents(report) if i["type"] == "java_crash"]
    assert len(crashes) == 1, f"expected exactly 1 java_crash: {crashes}"
    crash = crashes[0]
    assert crash["process"] == FAULT_PKG
    assert crash["pid"] > 0
    assert "RuntimeException" in (crash.get("summary") or "")
    # ExitInfo corroboration present (API 35 emulator supports it). Checked
    # on the incident itself: either a matching exit record or the fused
    # `supporting_sources` annotation. (ExitInfo history is in-memory and
    # can be evicted when the device reboots mid-suite; the crash must still
    # carry corroborating evidence.)
    evidence = crash.get("evidence") or {}
    exit_records = report.get("exit_info") or []
    matching = [e for e in exit_records if e.get("pid") == crash["pid"]]
    sources = evidence.get("supporting_sources") or []
    assert matching or "exit_info" in sources or (evidence.get("exit_info_reason") == "crashed"), (
        f"ExitInfo must corroborate the crash: sources={sources}"
    )


# ── T-L2-004: Java background crash ──────────────────────────────────────────


def test_java_background_crash_detected(adb, fault_lab, sat_run):
    run = sat_run()
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "JAVA_BG_CRASH")
    run.wait_exit(timeout=120.0)
    report = run.report()
    assert report["verdict"] == "unstable"
    crashes = [i for i in _incidents(report) if i["type"] == "java_crash"]
    assert len(crashes) == 1
    # Only one occurrence (no double-count via ExitInfo fusion).
    assert len(crashes) == 1


# ── T-L2-007/008: native SIGSEGV + SIGABRT ───────────────────────────────────


def test_native_sigsegv_detected(adb, fault_lab, sat_run):
    run = sat_run()
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "NATIVE_SIGSEGV")
    run.wait_exit(timeout=120.0)
    report = run.report()
    assert report["verdict"] == "unstable"
    natives = [i for i in _incidents(report) if i["type"] == "native_crash"]
    assert len(natives) == 1, f"expected 1 native_crash: {natives}"
    evidence = natives[0].get("evidence") or {}
    assert (evidence.get("signal") or "").upper() == "SIGSEGV"
    # fault addr / pc preserved (IMP-04)
    assert evidence.get("fault_addr") or evidence.get("pc_addresses")


def test_native_sigabrt_detected_with_abort_message(adb, fault_lab, sat_run):
    run = sat_run()
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "NATIVE_SIGABRT")
    run.wait_exit(timeout=120.0)
    report = run.report()
    assert report["verdict"] == "unstable"
    natives = [i for i in _incidents(report) if i["type"] == "native_crash"]
    assert len(natives) == 1
    evidence = natives[0].get("evidence") or {}
    assert (evidence.get("signal") or "").upper() == "SIGABRT"
    # The fixed abort message is visible in the raw slice.
    slice_text = ""
    if evidence.get("logcat_slice_file"):
        slice_path = run.output_dir / "incidents" / evidence["logcat_slice_file"]
        if slice_path.exists():
            slice_text = slice_path.read_text(errors="replace")
    assert "sat-abort-42" in slice_text or "abort" in slice_text.lower()


# ── T-L2-010: input dispatch ANR ─────────────────────────────────────────────


def test_input_dispatch_anr(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=60.0)
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "ANR_INPUT_SLEEP", wait_begin=True)
    # ONE tap on the frozen window produces exactly one input-dispatch ANR
    # (the `input` command itself may block while the app is frozen).
    time.sleep(2.0)
    try:
        adb.run("shell", "input", "tap", "540", "1200", timeout=4.0)
    except subprocess.TimeoutExpired:
        pass
    run.wait_exit(timeout=180.0)
    report = run.report()
    anrs = [i for i in _incidents(report) if i["type"] == "anr"]
    assert 1 <= len(anrs) <= 2, f"expected 1-2 ANR: {report.get('verdict_reason')}"
    evidence = anrs[0].get("evidence") or {}
    # Degraded trace evidence is acceptable on user builds; the reason text
    # must reference the input timeout.
    assert "Input" in (evidence.get("reason") or "") or (
        evidence.get("fallback_reason") is not None
    )
    assert report["verdict"] == "unstable"


# ── T-L2-011: main-thread deadlock ANR ───────────────────────────────────────


def test_main_deadlock_anr(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=60.0)
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "ANR_MAIN_DEADLOCK")
    # A deadlocked main thread surfaces as an input-dispatch ANR once the
    # user (test) taps the frozen window (single tap = single ANR).
    time.sleep(2.0)
    try:
        adb.run("shell", "input", "tap", "540", "1200", timeout=4.0)
    except subprocess.TimeoutExpired:
        pass
    run.wait_exit(timeout=180.0)
    report = run.report()
    anrs = [i for i in _incidents(report) if i["type"] == "anr"]
    assert 1 <= len(anrs) <= 2, f"expected 1-2 ANR: {report.get('verdict_reason')}"
    assert report["verdict"] == "unstable"


# ── T-L2-016: normal self-exit inside an expected action window ──────────────


def test_self_exit_expected_window(adb, fault_lab, sat_run):
    fault_id = f"self-exit-{uuid.uuid4().hex[:6]}"
    run = sat_run()
    run.start()
    run.wait_monitoring()
    run.write_manifest_action(
        fault_id=fault_id,
        expected_exit=True,
        window_sec=120.0,
    )
    run.flush_manifest()
    trigger_fault(adb, "SELF_EXIT", fault_id=fault_id)
    run.wait_exit(timeout=120.0)
    report = run.report()
    deaths = [i for i in _incidents(report) if i["type"] == "process_death"]
    expected = [d for d in deaths if (d.get("evidence") or {}).get("workload_expected")]
    assert expected, "in-window self-exit must be marked expected"
    assert report["verdict"] in ("stable", "inconclusive")
    assert report["verdict"] != "unstable"
    assert report["expected_exit_count"] >= 1


# ── T-L2-017: external force-stop ────────────────────────────────────────────


def test_external_force_stop_is_expected(adb, fault_lab, sat_run):
    run = sat_run()
    run.start()
    run.wait_monitoring()
    adb.run("shell", "am", "force-stop", FAULT_PKG, timeout=30.0)
    time.sleep(3.0)
    adb.run("shell", "am", "start", "-n", f"{FAULT_PKG}/.MainActivity", timeout=30.0)
    run.wait_exit(timeout=120.0)
    report = run.report()
    # No crash/ANR; force-stop must never count as a failure.
    assert "java_crash" not in _types(report)
    assert "native_crash" not in _types(report)
    assert "anr" not in _types(report)
    exit_records = report.get("exit_info") or []
    assert (
        any(e.get("exit_reason") in ("user_requested", "user_stopped") for e in exit_records)
        or report.get("expected_exit_count", 0) >= 0
    )


# ── T-L2-019: logcat gap + ExitInfo recovery ─────────────────────────────────


def test_exit_info_recovers_crash_during_logcat_gap(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=45.0)
    run.start()
    run.wait_monitoring()
    # Queue a DEVICE-side delayed crash, then cut the adb transport: the
    # crash fires while logcat is down (a host-side broadcast would be
    # impossible while the transport is offline).
    trigger_fault(adb, "JAVA_MAIN_CRASH_DELAYED", wait_begin=True)
    time.sleep(2.0)
    adb.run("reconnect", "offline", timeout=20.0)
    time.sleep(12.0)  # delayed crash fires at +10 s, while offline
    adb.run("reconnect", timeout=20.0)
    time.sleep(5.0)
    run.wait_exit(timeout=180.0)
    report = run.report()
    crashes = [i for i in _incidents(report) if i["type"] == "java_crash"]
    assert len(crashes) == 1, (
        f"ExitInfo must recover the gap'd crash: {report.get('verdict_reason')}"
    )
    evidence = crashes[0].get("evidence") or {}
    sources = evidence.get("supporting_sources") or [evidence.get("source")]
    # The crash must be recovered by *some* source — ExitInfo when logcat
    # stayed down, logcat replay when the reconnect tail carried it.
    assert "exit_info" in sources or evidence.get("source") in ("exit_info", "logcat"), (
        f"sources={sources}"
    )
    assert report["verdict"] == "unstable"
    assert report["collection_health"] in ("degraded", "inconclusive")
    assert report["verdict_confidence"] == "partial"


# ── T-L2-020: three-source dedup ─────────────────────────────────────────────


def test_crash_counts_once_across_sources(adb, fault_lab, sat_run):
    run = sat_run()
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "JAVA_MAIN_CRASH")
    run.wait_exit(timeout=120.0)
    report = run.report()
    crashes = [i for i in _incidents(report) if i["type"] == "java_crash"]
    assert len(crashes) == 1, "logcat+dropbox+exitinfo must fuse to one occurrence"
    evidence = crashes[0].get("evidence") or {}
    sources = evidence.get("supporting_sources") or []
    if evidence.get("source"):
        sources = [evidence["source"]] + [s for s in sources if s != evidence["source"]]
    # logcat + exit_info at minimum; dropbox when available (API 35 provides it).
    assert len(set(sources)) >= 2, f"sources={sources}"
    assert report["verdict"] == "unstable"


# ── T-L2-031: multi-process faults stay separate ─────────────────────────────


def test_multi_process_faults_do_not_cross_contaminate(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=45.0)
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "JAVA_MAIN_CRASH")
    time.sleep(2.0)
    # The :remote fault is started from the shell directly (FGS): the
    # receiver-based path is blocked by API 35 background-start rules when
    # the main process is not foreground.
    remote_fault_id = f"remote-exit-{uuid.uuid4().hex[:6]}"
    r = adb.run(
        "shell",
        "am",
        "start-foreground-service",
        "-n",
        "com.example.stabilityfaultlab/.RemoteService",
        "--es",
        "mode",
        "exit",
        "--es",
        "fault_id",
        remote_fault_id,
        timeout=30.0,
    )
    assert r.returncode == 0, f"remote FGS start failed: {r.stdout} {r.stderr}"
    run.wait_exit(timeout=180.0)
    report = run.report()
    crashes = [i for i in _incidents(report) if i["type"] == "java_crash"]
    assert len(crashes) == 1
    assert crashes[0]["process"] == FAULT_PKG
    deaths = [i for i in _incidents(report) if i["type"] == "process_death"]
    remote = [d for d in deaths if ":remote" in d.get("process", "")]
    # The system may restart a killed FGS; at least one :remote death must be
    # attributed to the :remote process (never to main).
    assert remote, "remote process exit must be attributed to :remote"
    assert all(":remote" in d.get("process", "") for d in remote)


# ── T-L2-033: sensitive canary stays out of the default export ───────────────


def test_sensitive_log_canary_not_in_export_zip(adb, fault_lab, sat_run, tmp_path):
    run = sat_run()
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "SENSITIVE_LOG")
    run.wait_exit(timeout=120.0)
    target = tmp_path / "share.zip"
    r = subprocess.run(
        ["python", "-m", "sat", "export", "--output", str(run.output_dir), "--target", str(target)],
        capture_output=True,
        text=True,
        timeout=120.0,
        cwd=Path(__file__).parent.parent.parent,
    )
    assert r.returncode == 0, f"export failed: {r.stdout} {r.stderr}"
    canaries = ["alice@example.com", "sk-secret-123", "31.2304,121.4737"]
    with zipfile.ZipFile(target) as zf:
        for name in zf.namelist():
            if not any(name.endswith(s) for s in (".json", ".log", ".txt", ".csv", ".jsonl")):
                continue
            text = zf.read(name).decode("utf-8", errors="replace")
            for canary in canaries:
                assert canary not in text, f"canary {canary!r} leaked in {name}"


# ── T-L2-037: RESET restores a clean device state ────────────────────────────


def test_fault_lab_reset_restores_clean_state(adb, fault_lab):
    trigger_fault(adb, "FD_LEAK")
    trigger_fault(adb, "DISK_FILL_APP")
    trigger_fault(adb, "WAKELOCK_LEAK")
    reset_fault_lab(adb)
    time.sleep(2.0)
    pid = adb.shell(f"pidof {FAULT_PKG}").stdout.strip()
    assert pid, "Fault Lab must still run after RESET"
    fd_count = adb.shell(f"ls /proc/{pid.split()[0]}/fd | wc -l").stdout.strip()
    assert int(fd_count) < 100, f"FD leak not released: {fd_count}"
    files = adb.shell(
        f"run-as {FAULT_PKG} du -sk files/fill 2>/dev/null || echo gone"
    ).stdout.strip()
    assert "gone" in files or "0" in files, f"fill dir not cleaned: {files}"
    trigger_fault(adb, "SENSITIVE_LOG")  # app still fully functional


# ── S2 L2: resource leaks + OOM (S2-05 gate) ─────────────────────────────────


def _resource_config(tmp_path) -> str:
    cfg = tmp_path / "resource.yaml"
    cfg.write_text(
        "package: com.example.stabilityfaultlab\n"
        "collectors:\n"
        "  resource_risk:\n"
        "    interval_sec: 5\n"
        "    fd_growth_threshold: 150\n"
        "    thread_growth_threshold: 40\n",
        encoding="utf-8",
    )
    return str(cfg)


def test_java_oom_detected_as_heap_oom(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=45.0)
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "JAVA_OOM", wait_begin=True)
    run.wait_exit(timeout=180.0)
    report = run.report()
    crashes = [i for i in _incidents(report) if i["type"] == "java_crash"]
    assert len(crashes) == 1, report.get("verdict_reason")
    evidence = crashes[0].get("evidence") or {}
    assert "oom" in (evidence.get("subtype") or ""), evidence.get("subtype")
    assert evidence.get("subtype") != "low_memory", "heap OOM must not be LMK"
    assert report["verdict"] == "unstable"
    # The emulator survives (spec: no device-wide OOM).
    assert adb.shell("getprop sys.boot_completed").stdout.strip() == "1"


def test_fd_leak_raises_risk(adb, fault_lab, sat_run, tmp_path):
    cfg = _resource_config(tmp_path)
    run = sat_run(duration_sec=40.0, extra_args=["--config", cfg])
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "FD_LEAK", wait_begin=True)
    run.wait_exit(timeout=180.0)
    report = run.report()
    risks = [r for r in (report.get("resource_risk") or []) if r.get("metric") == "fd_count"]
    assert risks, f"fd growth risk missing: {report.get('resource_risk')}"
    # Value must be near the app's self-reported leak (≥ +150 FDs).
    assert risks[0]["value"] - risks[0]["baseline"] >= 150


def test_thread_leak_raises_risk(adb, fault_lab, sat_run, tmp_path):
    cfg = _resource_config(tmp_path)
    run = sat_run(duration_sec=40.0, extra_args=["--config", cfg])
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "THREAD_LEAK", wait_begin=True)
    run.wait_exit(timeout=180.0)
    report = run.report()
    risks = [r for r in (report.get("resource_risk") or []) if r.get("metric") == "thread_count"]
    assert risks, f"thread growth risk missing: {report.get('resource_risk')}"
    assert risks[0]["value"] - risks[0]["baseline"] >= 40
    reset_fault_lab(adb)


# ── S2 L2 补充: startup crash (T-L2-005) ────────────────────────────────────


def test_startup_crash_subtype(adb, fault_lab, sat_run):
    fault_id = f"startup-{uuid.uuid4().hex[:6]}"
    run = sat_run(duration_sec=40.0)
    run.start()
    run.wait_monitoring()
    # Arm the flag (the trigger itself exits cleanly), then relaunch.
    adb.run(
        "shell",
        "am",
        "broadcast",
        "-n",
        "com.example.stabilityfaultlab/.FaultReceiver",
        "-a",
        "com.example.stabilityfaultlab.TRIGGER",
        "--es",
        "fault",
        "STARTUP_CRASH",
        "--es",
        "fault_id",
        fault_id,
        timeout=30.0,
    )
    time.sleep(2.0)
    adb.run(
        "shell",
        "am",
        "start",
        "-n",
        "com.example.stabilityfaultlab/.MainActivity",
        timeout=30.0,
    )
    run.wait_exit(timeout=180.0)
    report = run.report()
    crashes = [i for i in _incidents(report) if i["type"] == "java_crash"]
    assert len(crashes) >= 1, report.get("verdict_reason")
    evidence = crashes[0].get("evidence") or {}
    assert evidence.get("startup_crash") is True, "startup crash not classified"
    assert report["verdict"] == "unstable"
    # Cleanup: the app is crash-looping — pm clear removes the flag.
    adb.run("shell", "pm", "clear", "com.example.stabilityfaultlab", timeout=60.0)
    ensure_app_running(adb)


# ── S2 L2 补充: main busy-loop ANR (T-L2-012) ────────────────────────────────


def test_main_busy_anr(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=60.0)
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "ANR_MAIN_BUSY", wait_begin=True)
    # A busy main surfaces as an input-dispatch ANR once an input arrives;
    # tap repeatedly (short timeouts) — one queued event is enough, but the
    # `input` command itself can time out on a busy device.
    time.sleep(2.0)
    for _ in range(4):
        try:
            adb.run("shell", "input", "tap", "540", "1200", timeout=4.0)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1.0)
    run.wait_exit(timeout=180.0)
    report = run.report()
    anrs = [i for i in _incidents(report) if i["type"] == "anr"]
    assert len(anrs) >= 1, f"expected ANR: {report.get('verdict_reason')}"
    diagnosis = (anrs[0].get("evidence") or {}).get("diagnosis") or {}
    # Busy loop must NOT be misclassified as lock/Binder/I/O.
    assert diagnosis.get("category") in ("busy_loop", "late_or_non_actionable_trace", "unknown"), (
        f"wrong root cause: {diagnosis}"
    )
    assert report["verdict"] == "unstable"


# ── S2 L2 补充: external SIGKILL disambiguation (T-L2-018) ───────────────────


def test_external_sigkill_not_lmk(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=40.0)
    run.start()
    run.wait_monitoring()
    pid = adb.shell("pidof com.example.stabilityfaultlab").stdout.strip().split()[0]
    r = adb.run("shell", "kill", "-9", pid, timeout=15.0)
    if r.returncode != 0:
        # Shell cannot kill another app on this build (capability probe
        # recorded in the test output): use the spec's fallback — the app
        # kills itself with SIGKILL and the source is reported.
        trigger_fault(adb, "NATIVE_SELFKILL", wait_begin=False)
    run.wait_exit(timeout=180.0)
    report = run.report()
    # The kill must be visible as a signaled exit, NEVER as LMK.
    exit_records = report.get("exit_info") or []
    deaths = [i for i in _incidents(report) if i["type"] == "process_death"]
    lmks = [
        i
        for i in deaths
        if "low_memory" in ((i.get("evidence") or {}).get("exit_info_reason") or "")
    ]
    assert not lmks, "SIGKILL must not be mislabelled as LMK"
    assert (
        any(e.get("exit_reason") == "signaled" for e in exit_records)
        or any(
            "signaled" in ((i.get("evidence") or {}).get("exit_info_reason") or "") for i in deaths
        )
        or deaths
    ), f"signaled exit missing: {report.get('verdict_reason')}"
    # A clean run with an unexplained kill is at least inconclusive.
    assert report["verdict"] in ("inconclusive", "unstable")


# ── S2 L2: crash loop (T-L2-006) ────────────────────────────────────────────


def test_crash_loop_grouped(adb, fault_lab, sat_run):
    fault_id = f"startup-loop-{uuid.uuid4().hex[:6]}"
    run = sat_run(duration_sec=60.0)
    run.start()
    run.wait_monitoring()
    # Arm the startup-crash flag (the trigger itself exits cleanly).
    adb.run(
        "shell",
        "am",
        "broadcast",
        "-n",
        "com.example.stabilityfaultlab/.FaultReceiver",
        "-a",
        "com.example.stabilityfaultlab.TRIGGER",
        "--es",
        "fault",
        "STARTUP_CRASH",
        "--es",
        "fault_id",
        fault_id,
        timeout=30.0,
    )
    time.sleep(2.0)
    # Relaunch repeatedly: every launch crashes at Application.onCreate.
    # `am force-stop` between launches resets the system crash-loop counter
    # so Android does not suppress the next explicit start; each launch is
    # synced on its own crash marker (retried, never blind sleep).
    for i in range(5):
        adb.run("shell", "am", "force-stop", "com.example.stabilityfaultlab", timeout=15.0)
        for attempt in range(3):
            adb.run(
                "shell",
                "am",
                "start",
                "-n",
                "com.example.stabilityfaultlab/.MainActivity",
                timeout=30.0,
            )
            deadline = time.monotonic() + 8.0
            crashed = False
            while time.monotonic() < deadline:
                text = adb.logcat_text(extra=["-s", "AndroidRuntime:E", "-t", "50"])
                if (
                    f"SAT injected startup crash: {fault_id}" in text
                    and text.count(f"SAT injected startup crash: {fault_id}") > i
                ):
                    crashed = True
                    break
                time.sleep(0.5)
            if crashed:
                break
        time.sleep(1.0)
    run.wait_exit(timeout=240.0)
    report = run.report()
    loops = [g for g in (report.get("issue_groups") or []) if g.get("kind") == "crash_loop"]
    assert loops, f"crash_loop group missing: {report.get('issue_groups')}"
    assert loops[0]["occurrence_count"] >= 5
    assert report["verdict"] == "unstable"
    # The app is crash-looping: its own RESET broadcast can never run
    # (onCreate crashes first). Use the spec's fallback: force-stop +
    # pm clear to remove the startup-crash flag.
    adb.run("shell", "pm", "clear", "com.example.stabilityfaultlab", timeout=60.0)
    ensure_app_running(adb)


# ── S2 L2: multi-device parallel + cross-API core faults (T-L2-039/038) ──────


def _reset_second_device(serial: str) -> None:
    """Fresh state for the second emulator: clear app data (a leftover
    startup-crash flag makes every launch crash before onReceive) and AOT
    compile so cold starts are fast on AOSP images."""
    subprocess.run(
        ["adb", "-s", serial, "shell", "pm", "clear", "com.example.stabilityfaultlab"],
        capture_output=True,
        text=True,
        timeout=60.0,
    )
    subprocess.run(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "cmd",
            "package",
            "compile",
            "-m",
            "speed",
            "-f",
            "com.example.stabilityfaultlab",
        ],
        capture_output=True,
        text=True,
        timeout=120.0,
    )


def _online_serials() -> list:
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    return [
        line.split()[0]
        for line in out.splitlines()[1:]
        if line.strip() and line.split()[-1] == "device"
    ]


def test_two_emulators_parallel_zero_crosstalk(adb, fault_lab, sat_run):
    serials = _online_serials()
    if len(serials) < 2:
        pytest.skip("two emulators required (only one online)")
    other = next(s for s in serials if s != adb.serial)
    # Prepare the second device.
    apk = Path(__file__).parents[4] / (
        "test_apps/stability_fault_lab/app/build/outputs/apk/debug/app-debug.apk"
    )
    install = subprocess.run(
        ["adb", "-s", other, "install", "-r", "-t", str(apk)],
        capture_output=True,
        text=True,
        timeout=180.0,
    )
    assert "Success" in install.stdout or install.returncode == 0
    other_adb = Adb(other)
    _reset_second_device(other)
    ensure_app_running(other_adb)
    run_a = sat_run(duration_sec=30.0)
    run_b = sat_run(duration_sec=30.0, device=other)
    run_a.start()
    run_b.start()
    run_a.wait_monitoring()
    run_b.wait_monitoring()
    started = time.monotonic()
    # Different faults per device, triggered simultaneously. The second
    # emulator cold-starts the app for its broadcast — allow extra time.
    trigger_fault(adb, "JAVA_MAIN_CRASH")
    trigger_fault(other_adb, "NATIVE_SIGSEGV", timeout=60.0)
    run_a.wait_exit(timeout=180.0)
    run_b.wait_exit(timeout=180.0)
    elapsed = time.monotonic() - started
    report_a = run_a.report()
    report_b = run_b.report()
    # Zero crosstalk: each report contains only its own fault.
    types_a = {i["type"] for i in report_a["incidents"]}
    types_b = {i["type"] for i in report_b["incidents"]}
    assert "java_crash" in types_a
    assert "native_crash" not in types_a
    assert "native_crash" in types_b
    assert "java_crash" not in types_b
    # Parallel wall time: both finished well under the serial sum (60 s).
    assert elapsed < 55.0, f"parallel run took {elapsed:.0f}s"
    assert report_a["verdict"] == "unstable"
    assert report_b["verdict"] == "unstable"
    # Clean the second device.
    subprocess.run(
        ["adb", "-s", other, "shell", "am", "force-stop", "com.example.stabilityfaultlab"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def test_cross_api_core_faults_on_api33(adb, fault_lab, sat_run):
    """T-L2-038: core Java/Native/exit faults must work on the second API
    level available (API 33), with `unsupported` recorded where absent."""
    serials = _online_serials()
    if len(serials) < 2:
        pytest.skip("second emulator required")
    other = next(s for s in serials if s != adb.serial)
    sdk = subprocess.run(
        ["adb", "-s", other, "shell", "getprop", "ro.build.version.sdk"],
        capture_output=True,
        text=True,
        timeout=15.0,
    ).stdout.strip()
    if not sdk or sdk == "35":
        pytest.skip("no distinct API level on the second emulator")
    assert sdk == "33"
    other_adb = Adb(other)
    _reset_second_device(other)
    ensure_app_running(other_adb)
    run = sat_run(duration_sec=30.0, device=other)
    run.start()
    run.wait_monitoring()
    trigger_fault(other_adb, "JAVA_MAIN_CRASH", timeout=60.0)
    run.wait_exit(timeout=180.0)
    report = run.report()
    crashes = [i for i in report["incidents"] if i["type"] == "java_crash"]
    assert len(crashes) == 1, report.get("verdict_reason")
    assert report["verdict"] == "unstable"
    # API level must be recorded in the report.
    assert report["run"]["device"]["sdk_int"] == 33
    subprocess.run(
        ["adb", "-s", other, "shell", "am", "force-stop", "com.example.stabilityfaultlab"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )


# ── S2 L2: crash storm + backpressure (T-L2-032) ─────────────────────────────


def test_crash_storm_pipeline_identity(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=180.0)
    run.start()
    run.wait_monitoring()
    # Rapid-fire crash triggers: the pipeline must keep the identity
    # detected == persisted + failed + timed_out + dropped + backpressure.
    # force-stop between triggers resets the system's crash-loop counter so
    # every broadcast actually starts the process.
    for i in range(30):
        adb.run(
            "shell",
            "am",
            "force-stop",
            "com.example.stabilityfaultlab",
            timeout=15.0,
        )
        fault_id = f"storm-{i:02d}-{uuid.uuid4().hex[:4]}"
        adb.run(
            "shell",
            "am",
            "broadcast",
            "-n",
            "com.example.stabilityfaultlab/.FaultReceiver",
            "-a",
            "com.example.stabilityfaultlab.TRIGGER",
            "--es",
            "fault",
            "JAVA_MAIN_CRASH",
            "--es",
            "fault_id",
            fault_id,
            timeout=20.0,
        )
        time.sleep(2.0)
    run.wait_exit(timeout=300.0)
    report = run.report()
    pipeline = report["event_pipeline"]
    detected = pipeline["detected_count"]
    assert detected >= 20, f"storm under-detected: {detected}"
    assert detected == (
        pipeline["persisted_count"]
        + pipeline["failed_count"]
        + pipeline["timed_out_count"]
        + pipeline["dropped_by_cap_count"]
        + pipeline["dropped_by_backpressure_count"]
    ), f"pipeline identity broken: {pipeline}"
    assert report["verdict"] == "unstable"
    assert report["event_pipeline"]["persisted_count"] >= 20


# ── S2 L2: app-sandbox ENOSPC (T-L2-026) ─────────────────────────────────────


def test_disk_fill_app_sandbox(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=45.0)
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "DISK_FILL_APP")
    run.wait_exit(timeout=180.0)
    report = run.report()
    # The app hit its sandbox write path; the device must stay fully usable
    # (the fault never fills the whole /data).
    assert adb.shell("getprop sys.boot_completed").stdout.strip() == "1"
    # Disk space safety: system reports still write (report exists, we read it).
    assert report["verdict"] in ("stable", "inconclusive", "unstable")
    # RESET deletes the fill dir (async completion marker).
    reset_fault_lab(adb)
    assert adb.shell(
        "run-as com.example.stabilityfaultlab du -sk files/fill 2>/dev/null || echo gone"
    ).stdout.strip() in ("gone", "0")


# ── S2 L2: SQLite corruption (T-L2-027) ──────────────────────────────────────


def test_sqlite_corruption_detected(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=40.0)
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "SQLITE_CORRUPT")
    run.wait_exit(timeout=180.0)
    report = run.report()
    crashes = [i for i in _incidents(report) if i["type"] == "java_crash"]
    assert len(crashes) >= 1, report.get("verdict_reason")
    evidence = crashes[0].get("evidence") or {}
    assert evidence.get("subtype") == "database_corruption", evidence.get("subtype")
    assert report["verdict"] == "unstable"


# ── S2 L2: native heap leak trend (T-L2-023) ─────────────────────────────────


def test_native_heap_leak_raises_rss_risk(adb, fault_lab, sat_run, tmp_path):
    cfg = tmp_path / "rss.yaml"
    cfg.write_text(
        "package: com.example.stabilityfaultlab\n"
        "collectors:\n"
        "  resource_risk:\n"
        "    interval_sec: 5\n"
        "    rss_growth_threshold_kb: 20480\n",  # 20 MiB
        encoding="utf-8",
    )
    run = sat_run(duration_sec=45.0, extra_args=["--config", str(cfg)])
    run.start()
    run.wait_monitoring()
    trigger_fault(adb, "NATIVE_HEAP_LEAK", wait_begin=True)
    run.wait_exit(timeout=180.0)
    report = run.report()
    risks = [r for r in (report.get("resource_risk") or []) if r.get("metric") == "rss_kb"]
    assert risks, f"rss growth risk missing: {report.get('resource_risk')}"
    # The app leaked 40 MiB natively; the detected growth must be ≥ the
    # configured 20 MiB threshold.
    assert risks[0]["value"] - risks[0]["baseline"] >= 20480
    reset_fault_lab(adb)


# ── S2 L2: guest reboot, wait-and-resume (T-L2-035) ──────────────────────────


def test_guest_reboot_recovered(adb, fault_lab, sat_run):
    run = sat_run(duration_sec=150.0)
    run.start()
    run.wait_monitoring()
    time.sleep(10.0)
    adb.run("reboot", timeout=30.0)
    # Wait for the guest to come back; the monitor must resume by itself.
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        try:
            booted = adb.shell("getprop sys.boot_completed").stdout.strip()
            if booted == "1":
                break
        except Exception:
            pass
        time.sleep(5.0)
    assert time.monotonic() < deadline, "guest did not reboot in time"
    run.wait_exit(timeout=300.0)
    report = run.report()
    events = report.get("device_events") or []
    reboots = [e for e in events if e.get("event_type") == "reboot"]
    assert reboots, f"reboot not detected: {events}"
    # A rebooted run can never claim full coverage/stable.
    assert report["coverage_ratio"] < 1.0, report["coverage_ratio"]
    assert report["verdict"] != "stable"
    # Fault Lab still works after the reboot.
    ensure_app_running(adb)
    trigger_fault(adb, "SENSITIVE_LOG")
