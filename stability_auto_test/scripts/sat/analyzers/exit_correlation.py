"""Correlate ApplicationExitInfo records with incidents.

The same physical crash can surface in logcat, DropBox and `dumpsys activity
exit-info`. We match on PID + process + time window and mark the incident as
correlated instead of creating a second occurrence. Normal background recycles
stay in the `exit_info` list and are not turned into incidents.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..detection import _name_matches_package

log = logging.getLogger(__name__)

CORRELATION_WINDOW_SEC = 30.0


def _ts_epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def correlate_exit_info(
    incidents: List[Dict],
    exit_records: List[Dict],
    *,
    window_sec: float = CORRELATION_WINDOW_SEC,
) -> List[Dict]:
    """Annotate exit records with the incident they belong to (if any).

    Incidents are mutated with `evidence.exit_info_*` markers. The annotated
    exit records are returned for the report's `exit_info` section.
    """
    for rec in exit_records:
        rec_ts = rec.get("timestamp_epoch")
        if rec_ts is None:
            rec_ts = _ts_epoch(rec.get("timestamp"))
        matched: Optional[Dict] = None
        base_proc = (rec.get("process") or "").split(":")[0]
        for inc in incidents:
            inc_ts = _ts_epoch(inc.get("triggered_at"))
            if rec.get("pid") and inc.get("pid") != rec["pid"]:
                continue
            if base_proc and not _name_matches_package(
                inc.get("process", ""), base_proc,
            ):
                continue
            if rec_ts is not None and inc_ts is not None:
                if abs(rec_ts - inc_ts) > window_sec:
                    continue
            matched = inc
            break
        if matched is not None:
            rec["correlated_incident_id"] = matched.get("id")
            evidence = matched.setdefault("evidence", {})
            evidence["exit_info_correlated"] = True
            if "exit_info_reason" not in evidence:
                evidence["exit_info_reason"] = rec.get("exit_reason")
        else:
            rec["correlated_incident_id"] = None
    return exit_records
