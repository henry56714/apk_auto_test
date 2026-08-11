"""Run replay: deterministic reproduction of a previous run."""

from __future__ import annotations

import time as _time
from pathlib import Path
from shlex import join as shlex_join
from typing import Dict, Optional

import yaml

from .adb import Adb
from .api import StabilityConfig, StabilityTest
from .workloads.external import _filter_env

REPLAY_FILENAME = "replay.yaml"
REPLAY_VERSION = 1


def write_replay_manifest(
    config: StabilityConfig,
    *,
    workload_manifest: Optional[Dict],
    output_dir: Path,
    run_id: Optional[str] = None,
) -> Path:
    if workload_manifest and workload_manifest.get("type") == "external":
        workload_manifest = dict(workload_manifest)
        workload_manifest["command_template"] = _filter_env(
            list(workload_manifest.get("command_template") or [])
        )
    data = {
        "replay_version": REPLAY_VERSION,
        "package": config.package,
        "device": config.device,
        "original_run_id": run_id,
        "workload": workload_manifest,
        "config": {
            "dedup_window_sec": config.dedup_window_sec,
            "pre_context_sec": config.pre_context_sec,
            "post_context_sec": config.post_context_sec,
            "logcat_buffers": list(config.logcat_buffers),
            "min_coverage_ratio": config.min_coverage_ratio,
            "policy_fail_on": list(config.policy_fail_on),
        },
    }
    path = Path(output_dir) / REPLAY_FILENAME
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def read_replay_manifest(path: Path) -> Dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or data.get("replay_version") != REPLAY_VERSION:
        raise ValueError(f"unsupported replay manifest: {path}")
    return data


def config_from_manifest(data: Dict, output_dir: Path) -> StabilityConfig:
    cfg_data = data.get("config") or {}
    return StabilityConfig(
        package=str(data["package"]),
        device=data.get("device"),
        output_dir=Path(output_dir),
        dedup_window_sec=float(cfg_data.get("dedup_window_sec", 5.0)),
        pre_context_sec=float(cfg_data.get("pre_context_sec", 30.0)),
        post_context_sec=float(cfg_data.get("post_context_sec", 10.0)),
        logcat_buffers=list(cfg_data.get("logcat_buffers", ["main", "system", "events", "crash"])),
        min_coverage_ratio=float(cfg_data.get("min_coverage_ratio", 0.99)),
        policy_fail_on=list(cfg_data.get("policy_fail_on", ["java_crash", "native_crash", "anr"])),
        replay_of_run_id=data.get("original_run_id"),
    )


def _workload_from_manifest(data: Dict, stab: StabilityTest):
    workload = data.get("workload") or {}
    wtype = workload.get("type")
    if wtype is None:
        return None
    if wtype == "launch":
        from .workloads.launch import LaunchWorkload
        return LaunchWorkload(stab._adb, stab.config.package,
                              activity=workload.get("activity"))
    if wtype == "monkey":
        from .workloads.monkey import MonkeyWorkload
        return MonkeyWorkload(
            stab._adb, stab.config.package,
            seed=int(workload.get("seed", 0)),
            event_count=int(workload.get("event_count", 1000)),
            throttle_ms=int(workload.get("throttle_ms", 50)),
        )
    if wtype == "external":
        from .workloads.external import ExternalWorkload
        cmd = shlex_join(workload.get("command_template", []))
        return ExternalWorkload(cmd, timeout_sec=float(workload.get("timeout_sec", 300)))
    return None


def run_replay(
    manifest_path: Path,
    output_dir: Path,
    *,
    adb=None,
    discover_fn=None,
    duration_sec: float = 60.0,
) -> StabilityTest:
    data = read_replay_manifest(manifest_path)
    cfg = config_from_manifest(data, output_dir)
    adb = adb or Adb(serial=cfg.device)
    _ensure_running(adb, cfg.package)
    stab = StabilityTest(cfg, adb=adb, discover_fn=discover_fn)
    stab.start()
    workload = _workload_from_manifest(data, stab)
    if workload is not None:
        stab.run_workload(workload)
    deadline = _time.time() + duration_sec
    while _time.time() < deadline:
        _time.sleep(0.5)
    stab.stop()
    return stab


def _ensure_running(adb: Adb, package: str) -> None:
    """Launch the target before monitoring so replay can reproduce crashes."""
    try:
        r = adb.shell(f"pidof {package}", check=False, timeout=5.0)
        if r.returncode == 0 and r.stdout.strip():
            return
        adb.shell(
            f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
            check=False,
            timeout=30.0,
        )
        _time.sleep(2)
    except Exception:
        return


def is_reproduced(original: Dict, replay: Dict) -> bool:
    """True when at least one fingerprint from the original also appears."""
    original_fps = {g.get("fingerprint") for g in (original.get("issue_groups") or [])}
    replay_fps = {g.get("fingerprint") for g in (replay.get("issue_groups") or [])}
    return bool(original_fps & replay_fps)
