"""Launch workload: start the target activity (or launcher activity)."""

from __future__ import annotations

import re
from typing import Optional

from ..adb import Adb, AdbError
from ..utils import utc_now_iso
from .base import Workload, WorkloadResult


def resolve_launcher_activity(adb: Adb, package: str) -> Optional[str]:
    try:
        r = adb.shell(
            f"cmd package resolve-activity --brief -c "
            f"android.intent.category.LAUNCHER {package}",
            check=False,
            timeout=8.0,
        )
    except AdbError:
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        m = re.search(r"(\S+/\S+)", line)
        if m and m.group(1).startswith(package):
            return m.group(1)
    return None


class LaunchWorkload(Workload):
    name = "launch"

    def __init__(self, adb: Adb, package: str, activity: Optional[str] = None):
        super().__init__()
        self.adb = adb
        self.package = package
        self.activity = activity

    def run(self) -> WorkloadResult:
        self._started = utc_now_iso()
        target = self.activity or resolve_launcher_activity(self.adb, self.package)
        if not target:
            # Fallback: monkey launcher intent.
            self.adb.shell(
                f"monkey -p {self.package} -c android.intent.category.LAUNCHER 1",
                check=False,
                timeout=30.0,
            )
        else:
            self.adb.shell(
                f"am start -W -n {target}",
                check=False,
                timeout=30.0,
            )
        self._ended = utc_now_iso()
        return WorkloadResult(
            status="ok", exit_code=0,
            started_at=self._started, ended_at=self._ended,
        )

    def manifest(self) -> dict:
        return {
            "type": self.name,
            "package": self.package,
            "activity": self.activity,
        }
