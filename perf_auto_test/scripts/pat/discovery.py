"""Discover processes for a given package name on a device.

Process naming on Android:
- main:     <package>
- sub:      <package>:<suffix>          e.g. com.foo:remote
- chromium: <package>:sandboxed_process<N>
- isolated: <package>:isolated_process<N>

Why cmdline verification matters: /proc/[pid]/comm (the source of `ps NAME`) is
truncated to 15 chars. Long package names collide with their `:` sub-processes
under that truncation, so we always verify candidates against
/proc/[pid]/cmdline before reporting.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import List, Tuple

from .adb import Adb, AdbError

COMM_MAX = 15  # kernel TASK_COMM_LEN - 1

_PS_ROW = re.compile(r"^\s*(\d+)\s+(\S.*?)\s*$")
_DUMPSYS_PROC = re.compile(r"(\d+):([A-Za-z][\w.]*(?::[\w.]+)?)")


@dataclass
class Process:
    pid: int
    name: str
    started_at: float = field(default_factory=time.time)


def _name_matches_package(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ":")


def _name_could_be_truncated_match(name: str, package: str) -> bool:
    """True if `name` (length <= COMM_MAX) could be a truncated prefix of
    `package` or `package:<anything>`."""
    if len(name) < COMM_MAX:
        return False
    pkg_with_colon = package + ":"
    return package.startswith(name) or pkg_with_colon.startswith(name)


def parse_ps_output(text: str, package: str) -> List[Tuple[int, str]]:
    """Parse `ps -A -o PID,NAME` output → candidate (pid, ps_name) tuples.

    Includes both exact matches and possible truncations (length == COMM_MAX
    and `name` is a prefix of `package` or `package:`); caller must verify
    candidates via cmdline.
    """
    out: List[Tuple[int, str]] = []
    seen: set = set()
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        up = s.upper()
        if up.startswith("PID") and "NAME" in up:
            continue
        m = _PS_ROW.match(line)
        if not m:
            continue
        pid = int(m.group(1))
        name = m.group(2).strip()
        if pid in seen:
            continue
        if _name_matches_package(name, package) or _name_could_be_truncated_match(name, package):
            out.append((pid, name))
            seen.add(pid)
    return out


def parse_ps_old_output(text: str, package: str) -> List[Tuple[int, str]]:
    """Parse old-style `ps` output (Android <8).

    Header e.g.: USER  PID  PPID  VSIZE  RSS  WCHAN  PC  NAME
    Where NAME is the last column (may contain a leading status char like 'S').
    """
    lines = text.splitlines()
    if not lines:
        return []
    header_cols = lines[0].split()
    if "PID" not in header_cols:
        return []
    pid_idx = header_cols.index("PID")
    # NAME is typically last column in old toolbox `ps` output.
    out: List[Tuple[int, str]] = []
    seen: set = set()
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) <= pid_idx:
            continue
        try:
            pid = int(parts[pid_idx])
        except ValueError:
            continue
        name = parts[-1]
        if pid in seen:
            continue
        if _name_matches_package(name, package) or _name_could_be_truncated_match(name, package):
            out.append((pid, name))
            seen.add(pid)
    return out


def parse_dumpsys_processes(text: str, package: str) -> List[Tuple[int, str]]:
    """Fallback parser for `dumpsys activity processes`.

    Matches `PID:process_name` tokens; dumpsys preserves full names so no
    cmdline verification is needed.
    """
    out: List[Tuple[int, str]] = []
    seen: set = set()
    for line in text.splitlines():
        for m in _DUMPSYS_PROC.finditer(line):
            pid = int(m.group(1))
            name = m.group(2)
            if pid in seen:
                continue
            if _name_matches_package(name, package):
                out.append((pid, name))
                seen.add(pid)
    return out


def read_cmdline(adb: Adb, pid: int) -> str:
    """Return the first nul-separated token of /proc/<pid>/cmdline (the
    canonical process name), or empty string if unreadable / process gone."""
    try:
        r = adb.shell(f"cat /proc/{pid}/cmdline 2>/dev/null", check=False, timeout=2.0)
    except AdbError:
        return ""
    if r.returncode != 0 or not r.stdout:
        return ""
    raw = r.stdout
    return raw.split("\x00", 1)[0].strip()


def discover(adb: Adb, package: str) -> List[Process]:
    """Return all live processes for `package`, with full (un-truncated) names."""
    candidates = _gather_candidates(adb, package)

    verified: List[Process] = []
    seen_pids: set = set()
    for pid, ps_name in candidates:
        if pid in seen_pids:
            continue
        full = read_cmdline(adb, pid)
        if full and _name_matches_package(full, package):
            verified.append(Process(pid=pid, name=full))
            seen_pids.add(pid)
            continue
        # cmdline unreadable: trust ps if not at truncation boundary
        if not full and len(ps_name) < COMM_MAX and _name_matches_package(ps_name, package):
            verified.append(Process(pid=pid, name=ps_name))
            seen_pids.add(pid)
    return verified


def _gather_candidates(adb: Adb, package: str) -> List[Tuple[int, str]]:
    try:
        r = adb.shell("ps -A -o PID,NAME", check=False, timeout=5.0)
    except AdbError:
        r = None
    if r and r.returncode == 0 and r.stdout.strip():
        cands = parse_ps_output(r.stdout, package)
        if cands:
            return cands

    try:
        r2 = adb.shell("ps", check=False, timeout=5.0)
    except AdbError:
        r2 = None
    if r2 and r2.returncode == 0 and r2.stdout.strip():
        cands = parse_ps_old_output(r2.stdout, package)
        if cands:
            return cands

    try:
        r3 = adb.shell("dumpsys activity processes", check=False, timeout=10.0)
    except AdbError:
        r3 = None
    if r3 and r3.returncode == 0:
        return parse_dumpsys_processes(r3.stdout, package)

    return []


def wait_for_processes(
    adb: Adb,
    package: str,
    *,
    timeout_sec: float = 60.0,
    poll_interval_sec: float = 1.0,
) -> List[Process]:
    """Poll `discover` until at least one process appears, or timeout."""
    deadline = time.monotonic() + timeout_sec
    while True:
        procs = discover(adb, package)
        if procs:
            return procs
        if time.monotonic() >= deadline:
            return []
        time.sleep(poll_interval_sec)
