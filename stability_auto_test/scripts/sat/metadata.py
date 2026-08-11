"""App / build metadata for run records and trend aggregation."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Dict

from .adb import Adb, AdbError

log = logging.getLogger(__name__)


def _getprop(adb: Adb, prop: str) -> str:
    try:
        r = adb.shell(f"getprop {prop}", check=False, timeout=3.0)
    except AdbError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def collect_app_metadata(adb: Adb, package: str) -> Dict:
    version_name = ""
    version_code = ""
    try:
        r = adb.shell(f"dumpsys package {package}", check=False, timeout=10.0)
        if r.returncode == 0:
            m = re.search(r"versionName=(\S+)", r.stdout)
            if m:
                version_name = m.group(1)
            m = re.search(r"versionCode=(\d+)", r.stdout)
            if m:
                version_code = m.group(1)
    except AdbError:
        pass
    return {
        "app_version_name": version_name,
        "app_version_code": version_code,
        "build_id": _getprop(adb, "ro.build.id"),
        "git_sha": _git_sha(),
    }


def _git_sha() -> str:
    env = os.environ.get("SAT_GIT_SHA")
    if env:
        return env
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""
