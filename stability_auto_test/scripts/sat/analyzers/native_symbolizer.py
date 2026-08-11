"""Native stack symbolization with llvm-symbolizer."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


@dataclass
class SymbolizeResult:
    frames: List[str]
    status: str  # ok | partial | unavailable
    error: Optional[str] = None


_FRAME_RE = re.compile(
    r"^(?P<prefix>#\d+\s+pc\s+)(?P<addr>0x[0-9a-fA-F]+)"
    r"\s+(?P<module>\S+\.so(?:\.[\w.]+)?)(?:\s+\((?P<symbol>.*)\))?\s*$"
)


def _find_so(symbols_dir: Path, module_name: str) -> Optional[Path]:
    if not symbols_dir.exists():
        return None
    candidates = list(symbols_dir.rglob(module_name))
    return candidates[0] if candidates else None


def _run_symbolizer(
    tool: str,
    obj_path: Path,
    address: str,
) -> Optional[str]:
    try:
        proc = subprocess.run(
            [
                tool,
                "--obj", str(obj_path),
                "--functions",
                "--demangle",
                address,
            ],
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("llvm-symbolizer failed: %s", e)
        return None
    if proc.returncode != 0:
        return None
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    return " ".join(lines[:2])


def symbolize_frames(
    frames: List[str],
    *,
    symbols_dir: Optional[Path] = None,
    llvm_symbolizer: Optional[str] = None,
) -> SymbolizeResult:
    if not frames:
        return SymbolizeResult([], "unavailable", "no frames")
    if symbols_dir is None or not Path(symbols_dir).exists():
        return SymbolizeResult(list(frames), "unavailable", "no symbols dir")
    if not llvm_symbolizer:
        return SymbolizeResult(list(frames), "unavailable", "no llvm-symbolizer")

    out: List[str] = []
    failures = 0
    for frame in frames:
        m = _FRAME_RE.match(frame.strip())
        if not m or m.group("symbol"):
            out.append(frame)
            continue
        so_path = _find_so(Path(symbols_dir), Path(m.group("module")).name)
        if so_path is None:
            failures += 1
            out.append(frame)
            continue
        symbol = _run_symbolizer(llvm_symbolizer, so_path, m.group("addr"))
        if symbol is None:
            failures += 1
            out.append(frame)
        else:
            out.append(
                f"{m.group('prefix')}{m.group('addr')} {m.group('module')} "
                f"({symbol})"
            )
    if failures == 0:
        status = "ok"
    elif failures < len(frames):
        status = "partial"
    else:
        status = "unavailable"
    return SymbolizeResult(out, status)
