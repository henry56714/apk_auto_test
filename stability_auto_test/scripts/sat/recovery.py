"""`sat recover` — rebuild a report from the journal after an abnormal exit."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .journal import JOURNAL_FILENAME, read_journal
from .reporter import result as result_builder
from .runlock import RunLockError, check_recoverable, clear_stale_lock
from .utils import utc_now_iso

log = logging.getLogger(__name__)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def recover_report(output_dir: Path) -> Dict:
    """Rebuild `report.json` from the run journal and incident evidence."""
    output_dir = Path(output_dir)
    if not output_dir.exists():
        raise RunLockError(f"output directory does not exist: {output_dir}")
    check_recoverable(output_dir)
    clear_stale_lock(output_dir)

    journal_path = output_dir / JOURNAL_FILENAME
    records, warnings = read_journal(journal_path)
    if not records:
        raise RunLockError(f"no journal records to recover in {output_dir}")

    start_ts = end_ts = None
    package = "unknown"
    run_id = None
    for rec in records:
        ts = _parse_ts(rec.get("ts"))
        if ts is None:
            continue
        if start_ts is None or ts < start_ts:
            start_ts = ts
        if end_ts is None or ts > end_ts:
            end_ts = ts
        if package == "unknown" and rec.get("process"):
            package = rec["process"].split(":")[0]
        if run_id is None and rec.get("run_id"):
            run_id = rec["run_id"]

    now = datetime.now(timezone.utc)
    started_at = start_ts or now
    ended_at = end_ts or now

    device: Dict = {"serial": "?"}
    status_path = output_dir / "status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            device = {
                "serial": status.get("device", "?"),
                "android_version": status.get("android_version", "?"),
            }
        except (json.JSONDecodeError, OSError):
            pass

    recovered_at = utc_now_iso()
    # Abnormal exit means the observation window was incomplete — default to
    # inconclusive rather than fabricating a healthy verdict.
    collector_health = {
        "health": "inconclusive",
        "coverage_ratio": 0.0,
        "reasons": [
            "recovered after abnormal exit; observation window incomplete — "
            "no run-complete journal marker found"
        ],
    }
    result = result_builder.build(
        output_dir=output_dir,
        package=package,
        started_at=started_at,
        ended_at=ended_at,
        device=device,
        config_effective={"package": package, "recovered": True},
        run_id=run_id,
        exit_code=130,
        exit_reason="recovered_after_abnormal_exit",
        recovered=True,
        recovered_at=recovered_at,
        collector_health=collector_health,
    )
    result_builder.write(result, output_dir)
    log.info("recovered report written to %s", output_dir / result_builder.REPORT_FILENAME)
    return result
