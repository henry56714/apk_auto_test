"""Java stack deobfuscation (ProGuard/R8 mapping files).

Uses the configured `retrace` command when available; otherwise falls back to a
built-in mapping parser. Original frames are always preserved; the symbolized
result and a status are returned separately.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class RetraceResult:
    frames: List[str]
    status: str  # ok | partial | fallback | unavailable
    error: Optional[str] = None


_CLASS_LINE_RE = re.compile(r"^(?P<orig>[\w.$]+)\s*->\s*(?P<obf>[\w.$]+):\s*$")
# Standard ProGuard/R8 member lines:
#   <ret_type> <orig>(<args>) [:<start>:<end>] -> <obf>
# Examples:
#   void onResume() -> a
#   void onResume():10:20 -> a
#   java.lang.String getName(java.lang.String) -> a
_MEMBER_LINE_RE = re.compile(
    r"^\s+"
    r"(?P<ret>[\w.<>\[\]]+)\s+"
    r"(?P<orig>[\w$<>]+)\((?P<args>[^)]*)\)"
    r"(?::\d+(?::\d+)?)?\s*->\s*"
    r"(?P<obf>[\w$]+)"
)


def parse_mapping(mapping_text: str) -> Dict:
    """Return {obfuscated_class: original_class, (obf_class, obf_method): orig_method}.

    Accepts standard ProGuard/R8 format where ``original -> obfuscated``.
    """
    classes: Dict[str, str] = {}
    members: Dict[tuple, str] = {}
    cur_orig: Optional[str] = None
    cur_obf: Optional[str] = None
    for raw in mapping_text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        m = _CLASS_LINE_RE.match(line)
        if m:
            cur_orig = m.group("orig")
            cur_obf = m.group("obf")
            classes[cur_obf] = cur_orig
            continue
        if cur_obf is None:
            continue
        m = _MEMBER_LINE_RE.match(line)
        if m:
            members[(cur_obf, m.group("obf"))] = m.group("orig")
    return {"classes": classes, "members": members}


def load_mapping(path: Path) -> Dict:
    return parse_mapping(Path(path).read_text(encoding="utf-8", errors="replace"))


def _deobfuscate_frame(frame: str, mapping: Dict) -> str:
    classes = mapping.get("classes", {})
    members = mapping.get("members", {})
    # "at com.a.a.a(X.java:1)" → class com.a.a, method a
    m = re.match(
        r"^(?P<prefix>at\s+)?(?P<cls>[\w.$]+)\.(?P<method>[\w$]+)"
        r"(?:\((?P<args>[^)]*)\))?(?P<rest>.*)$",
        frame.strip(),
    )
    if not m:
        return frame
    cls = m.group("cls")
    method = m.group("method")
    orig_cls = classes.get(cls, cls)
    orig_method = members.get((cls, method), method)
    prefix = m.group("prefix") or ""
    args = m.group("args") or ""
    rest = m.group("rest") or ""
    suffix = f"({args}){rest}" if m.group("args") is not None else rest
    return f"{prefix}{orig_cls}.{orig_method}{suffix}"


def _run_retrace_tool(
    cmd: List[str],
    mapping_path: Path,
    frames: List[str],
) -> Optional[List[str]]:
    try:
        proc = subprocess.run(
            [*cmd, str(mapping_path)],
            input="\n".join(frames) + "\n",
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("retrace tool failed: %s", e)
        return None
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def deobfuscate_stack(
    frames: List[str],
    *,
    mapping_path: Optional[Path] = None,
    retrace_command: Optional[str] = None,
) -> RetraceResult:
    if not frames:
        return RetraceResult([], "unavailable", "no frames")
    if mapping_path is None or not Path(mapping_path).exists():
        return RetraceResult(list(frames), "unavailable", "no mapping file")

    if retrace_command:
        tool_out = _run_retrace_tool(
            retrace_command.split(),
            Path(mapping_path),
            frames,
        )
        if tool_out is not None and len(tool_out) == len(frames):
            return RetraceResult(tool_out, "ok")
        log.warning("retrace tool produced unusable output; using built-in parser")

    try:
        mapping = load_mapping(mapping_path)
    except OSError as e:
        return RetraceResult(list(frames), "unavailable", str(e))
    out = [_deobfuscate_frame(f, mapping) for f in frames]
    changed = any(a != b for a, b in zip(frames, out))
    if not changed:
        # No frame was deobfuscated — the mapping had no effect.
        status = "unavailable"
        if retrace_command:
            status = "fallback"
        return RetraceResult(list(frames), status, "no frames matched mapping")
    status = "ok"
    if retrace_command:
        status = "fallback"
    return RetraceResult(out, status)
