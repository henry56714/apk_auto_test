"""ANR dumper.

Writes raw logcat slice + structured incident JSON.
Best-effort: tries to pull the latest ANR trace from `/data/anr/` (root-only
on user builds). On failure, records `fallback_reason` and continues.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from ..adb import Adb, AdbError
from ..detection import StabilityEvent
from ..evidence.trace_matcher import match_trace, verify_local_trace
from . import (
    base_name_for,
    build_incident_dict,
    fetch_and_write_dropbox,
    write_incident,
    write_raw_slice,
)

log = logging.getLogger(__name__)


def run(
    adb: Adb,
    event: StabilityEvent,
    incidents_dir: Path,
    *,
    pull_anr_trace: bool = True,
    ctx=None,
    staging_dir: Optional[Path] = None,
    fetcher=None,
) -> Dict:
    target = staging_dir or incidents_dir
    target.mkdir(parents=True, exist_ok=True)
    base = base_name_for(event)
    slice_path = target / f"{base}.txt"
    trace_path = target / f"{base}.trace"
    json_path = target / f"{base}.json"

    slice_name = write_raw_slice(slice_path, event)
    trace_name: Optional[str] = None
    fallback: Optional[str] = None
    match_info: Dict = {
        "evidence_match_confidence": "none",
        "evidence_match_reasons": [],
        "trace_verified": False,
    }

    if pull_anr_trace:
        if ctx is not None:
            ctx.check()
        match = match_trace(
            adb, event, "/data/anr/", timeout=(ctx.timeout_for(30.0) if ctx is not None else 30.0)
        )
        match_info["evidence_match_confidence"] = match.confidence
        match_info["evidence_match_reasons"] = list(match.reasons)
        if not match.bound:
            fallback = "no_confident_match"
        else:
            remote = match.candidate.path
            try:
                if ctx is not None:
                    ctx.check()
                adb.pull(
                    remote,
                    str(trace_path),
                    check=True,
                    timeout=ctx.timeout_for(30.0) if ctx is not None else 30.0,
                )
                if trace_path.exists() and trace_path.stat().st_size > 0:
                    ok, reason = verify_local_trace(trace_path, event)
                    match_info["trace_verified"] = ok
                    if not ok:
                        # Verification failed: the pulled file must NOT be
                        # treated as evidence. Quarantine it and drop the
                        # reference (IMP-05 / T-L0-013).
                        match_info["trace_verify_reason"] = reason
                        match_info["evidence_match_confidence"] = "low"
                        quarantine = target / f"{trace_path.name}.unverified"
                        try:
                            trace_path.rename(quarantine)
                        except OSError:
                            try:
                                trace_path.unlink()
                            except OSError:
                                pass
                        fallback = "verification_failed"
                    else:
                        trace_name = trace_path.name
                else:
                    fallback = "ANR trace pull produced empty file"
            except AdbError as e:
                fallback = f"ANR trace pull failed: {e}"
    else:
        fallback = "ANR trace pull disabled by config"

    dropbox_name = fetch_and_write_dropbox(adb, event, target, base, ctx=ctx, fetcher=fetcher)
    incident = build_incident_dict(
        event,
        logcat_slice_file=slice_name,
        trace_file=trace_name,
        fallback_reason=fallback,
        dropbox_file=dropbox_name,
        extra_evidence=match_info,
    )
    write_incident(json_path, incident)
    log.info(
        "anr incident written: %s (trace=%s, dropbox=%s)", json_path.name, trace_name, dropbox_name
    )
    return incident
