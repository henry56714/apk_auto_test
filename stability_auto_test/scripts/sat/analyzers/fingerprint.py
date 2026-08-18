"""Stable incident fingerprints for issue grouping.

Volatile fields (PID, absolute native addresses, Java line numbers) are
normalized away so the same bug seen across runs (or after ASLR) clusters
together, while genuinely different business entry points stay separate.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List

_JAVA_FRAME_RE = re.compile(
    r"^(?:at\s+)?(?P<frame>[A-Za-z_][\w.$]*(?:\[[\w.]+\])?"
    r"(?:\.[A-Za-z_][\w$]*)?)\((?P<file>[^)]*)\)"
)
_NATIVE_FRAME_RE = re.compile(
    r"#\d+\s+pc\s+\S+\s+(?P<module>\S+\.so(?:\.[\w.]+)?)"
    r"(?:\s+\((?P<symbol>[^+]+)(?:\+0x[0-9a-fA-F]+)?\))?"
)
_ANR_FRAME_RE = re.compile(
    r"^\s*at\s+(?P<frame>[A-Za-z_][\w.$]*(?:\.[A-Za-z_][\w$]*)?)"
    r"(?:\((?P<file>[^)]*)\))?"
)


def _normalize_java_frame(frame: str) -> str:
    m = _JAVA_FRAME_RE.match(frame.strip())
    if m:
        return m.group("frame")
    # Fallback: strip line numbers like `X.java:123` → `X.java`
    return re.sub(r"\.java:\d+", ".java", frame.strip())


def _normalize_native_frame(frame: str) -> str:
    m = _NATIVE_FRAME_RE.match(frame.strip())
    if m:
        symbol = (m.group("symbol") or "").strip()
        return f"{m.group('module')}::{symbol}" if symbol else m.group("module")
    # Remove absolute addresses while keeping module-ish tokens.
    return re.sub(r"0x[0-9a-fA-F]+", "0xADDR", frame.strip())


def _normalize_anr_frame(frame: str) -> str:
    m = _ANR_FRAME_RE.match(frame.strip())
    return m.group("frame") if m else re.sub(r":\d+\)", ")", frame.strip())


def _hash(parts: List[str]) -> str:
    payload = "|".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _top_frames(evidence: Dict, key: str = "top_frames", limit: int = 5) -> List[str]:
    frames = [f for f in (evidence.get(key) or []) if isinstance(f, str)]
    return frames[:limit]


def fingerprint_incident(incident: Dict) -> str:
    """Return a stable fingerprint for an incident dict."""
    inc_type = incident.get("type", "unknown")
    evidence = incident.get("evidence") or {}
    process = (incident.get("process") or "").split(":")[0]

    if inc_type == "java_crash":
        exc = evidence.get("exception_class") or ""
        if not exc:
            m = re.search(
                r"([A-Za-z_][\w.$]*(?:Exception|Error|Throwable))", incident.get("summary", "")
            )
            exc = m.group(1) if m else "unknown"
        frames = [_normalize_java_frame(f) for f in _top_frames(evidence)]
        return _hash(["java", exc, *frames])

    if inc_type == "native_crash":
        signal = evidence.get("signal") or "?"
        frames = [_normalize_native_frame(f) for f in _top_frames(evidence)]
        return _hash(["native", signal, *frames])

    if inc_type == "anr":
        reason = evidence.get("reason") or incident.get("summary", "")
        frames = [_normalize_anr_frame(f) for f in _top_frames(evidence)]
        return _hash(["anr", reason, *frames])

    if inc_type == "process_death":
        reason = evidence.get("reason") or incident.get("summary", "")
        return _hash(["process_death", process, reason])

    return _hash([inc_type, process, incident.get("summary", "")])


def group_incidents(incidents: List[Dict]) -> List[Dict]:
    """Group incidents into issue groups by fingerprint."""
    groups: Dict[str, Dict] = {}
    for inc in incidents:
        fp = fingerprint_incident(inc)
        g = groups.setdefault(
            fp,
            {
                "fingerprint": fp,
                "type": inc.get("type"),
                "occurrence_count": 0,
                "first_seen_at": None,
                "last_seen_at": None,
                "affected_processes": [],
                "representative_incident_id": None,
                "occurrence_ids": [],
            },
        )
        g["occurrence_count"] += 1
        ts = inc.get("triggered_at", "")
        if g["first_seen_at"] is None or ts < g["first_seen_at"]:
            g["first_seen_at"] = ts
        if g["last_seen_at"] is None or ts > g["last_seen_at"]:
            g["last_seen_at"] = ts
        proc = inc.get("process")
        if proc and proc not in g["affected_processes"]:
            g["affected_processes"].append(proc)
        if g["representative_incident_id"] is None:
            g["representative_incident_id"] = inc.get("id")
        g["occurrence_ids"].append(inc.get("id"))

    out = list(groups.values())
    out.sort(key=lambda g: g["last_seen_at"] or "", reverse=True)
    for g in out:
        g["affected_processes"].sort()
        # S2-01: repeated crashes of the same fingerprint form a crash loop
        # (≥3 occurrences, or ≥2 startup crashes).
        if g["type"] in ("java_crash", "native_crash") and (
            g["occurrence_count"] >= 3
            or (
                g["occurrence_count"] >= 2
                and any(
                    (i.get("evidence") or {}).get("startup_crash")
                    for i in incidents
                    if i.get("id") in g["occurrence_ids"]
                )
            )
        ):
            g["kind"] = "crash_loop"
        else:
            g["kind"] = "occurrence_group"
    return out
