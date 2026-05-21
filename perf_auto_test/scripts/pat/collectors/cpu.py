"""CPU% collector using /proc/<pid>/stat + /proc/stat.

Algorithm (single-core-normalized, matches `top`):
    cpu% = Δ(utime + stime) / Δ(total_jiffies) * num_cores * 100

A process fully using one core for the sample interval reports 100%; a
process saturating all N cores reports 100*N%.

Both files are read in one adb shell invocation to keep per-sample overhead
low at 1Hz × N processes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

from ..adb import Adb, AdbError

_COMBINED_CMD_TMPL = "cat /proc/{pid}/stat 2>/dev/null && echo ---SEP--- && cat /proc/stat"


@dataclass
class CpuSample:
    timestamp: float
    pid: int
    utime: int
    stime: int
    total_jiffies: int


def parse_proc_pid_stat(text: str) -> Optional[Tuple[int, int]]:
    """Return (utime, stime) in clock ticks from /proc/<pid>/stat content, or None.

    The `comm` field is in parens and may itself contain spaces and parens
    (e.g. ":remote" sub-processes). Find the LAST `)` to anchor field parsing.
    """
    s = text.strip()
    rparen = s.rfind(")")
    if rparen < 0:
        return None
    after = s[rparen + 1:].split()
    # After comm: state(0), ppid(1), ... utime(11), stime(12) ...
    if len(after) < 13:
        return None
    try:
        return int(after[11]), int(after[12])
    except ValueError:
        return None


def parse_proc_stat(text: str) -> Optional[int]:
    """Sum the aggregate `cpu  user nice system idle iowait irq softirq ...`
    line from /proc/stat → total CPU jiffies across all cores."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            try:
                return sum(int(x) for x in parts[1:])
            except ValueError:
                return None
    return None


def parse_combined(text: str) -> Optional[Tuple[Tuple[int, int], int]]:
    """Split the combined `<pid stat>---SEP---<proc stat>` blob and parse both."""
    if "---SEP---" not in text:
        return None
    head, tail = text.split("---SEP---", 1)
    proc = parse_proc_pid_stat(head)
    sys_total = parse_proc_stat(tail)
    if proc is None or sys_total is None:
        return None
    return proc, sys_total


def sample(adb: Adb, pid: int) -> Optional[CpuSample]:
    """One snapshot from device. Returns None if process is gone or output malformed."""
    try:
        r = adb.shell(_COMBINED_CMD_TMPL.format(pid=pid), check=False, timeout=3.0)
    except AdbError:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    parsed = parse_combined(r.stdout)
    if parsed is None:
        return None
    (utime, stime), total = parsed
    return CpuSample(
        timestamp=time.time(),
        pid=pid,
        utime=utime,
        stime=stime,
        total_jiffies=total,
    )


class CpuPercentCalculator:
    """Stateful: holds the previous CpuSample, returns % on each new sample.

    Returns None for the first sample (no delta yet) and whenever the delta
    can't be trusted (negative — e.g. counter wrap or pid reused — or zero
    system delta).
    """

    def __init__(self, cores: int) -> None:
        if cores < 1:
            cores = 1
        self.cores = cores
        self._prev: Optional[CpuSample] = None

    def update(self, s: CpuSample) -> Optional[float]:
        prev = self._prev
        self._prev = s
        if prev is None or prev.pid != s.pid:
            return None
        d_proc = (s.utime + s.stime) - (prev.utime + prev.stime)
        d_total = s.total_jiffies - prev.total_jiffies
        if d_total <= 0 or d_proc < 0:
            return None
        return d_proc / d_total * self.cores * 100.0

    def reset(self) -> None:
        self._prev = None
