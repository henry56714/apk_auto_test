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
    r"^(?P<prefix>#\d+\s+pc\s+)(?P<addr>(?:0x)?[0-9a-fA-F]+)"
    r"\s+(?P<module>\S+\.so(?:\.[\w.]+)?)(?:\s+\((?P<symbol>.*)\))?\s*$"
)


def _read_build_id(so_path: Path) -> Optional[str]:
    """Best-effort build-id: `llvm-readelf -n` when available, else the
    `<so>.build-id` sidecar written by the Fault Lab build."""
    sidecar = Path(str(so_path) + ".build-id")
    if sidecar.exists():
        try:
            return sidecar.read_text(encoding="utf-8").strip().lower()
        except OSError:
            pass
    for tool in ("llvm-readelf", "readelf"):
        try:
            proc = subprocess.run(
                [tool, "-n", str(so_path)],
                capture_output=True,
                text=True,
                timeout=15.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            if "Build ID" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    return parts[-1].strip().lower()
    return None


def _find_so(
    symbols_dir: Path,
    module_name: str,
    *,
    abi: Optional[str] = None,
    build_id: Optional[str] = None,
) -> Optional[Path]:
    """Match a symbol file by module name + ABI + build ID (IMP-04 / T-L0-012).

    With several same-named `.so` files (arm64/x86 copies), only the one whose
    ABI and/or build ID match is selected; no evidence → no match.
    """
    if not symbols_dir.exists():
        return None
    candidates = list(symbols_dir.rglob(module_name))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    scored: List[tuple] = []
    for cand in candidates:
        score = 0
        if abi and abi in str(cand):
            score += 2
        if build_id:
            bid = _read_build_id(cand)
            if bid and bid == build_id.lower():
                score += 10
        scored.append((score, cand))
    scored.sort(key=lambda item: -item[0])
    return scored[0][1] if scored[0][0] > 0 else None


def _run_symbolizer(
    tool: str,
    obj_path: Path,
    address: str,
) -> Optional[str]:
    try:
        proc = subprocess.run(
            [
                tool,
                "--obj",
                str(obj_path),
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
    abi: Optional[str] = None,
    build_id: Optional[str] = None,
) -> SymbolizeResult:
    if not frames:
        return SymbolizeResult([], "unavailable", "no frames")
    if symbols_dir is None or not Path(symbols_dir).exists():
        return SymbolizeResult(list(frames), "unavailable", "no symbols dir")
    if not llvm_symbolizer:
        return SymbolizeResult(list(frames), "unavailable", "no llvm-symbolizer")

    out: List[str] = []
    failures = 0
    changed = 0
    for frame in frames:
        m = _FRAME_RE.match(frame.strip())
        if not m or m.group("symbol"):
            out.append(frame)
            failures += 1
            continue
        so_path = _find_so(
            Path(symbols_dir),
            Path(m.group("module")).name,
            abi=abi,
            build_id=build_id,
        )
        if so_path is None:
            failures += 1
            out.append(frame)
            continue
        # Normalize address: ensure 0x prefix and strip leading zeros.
        addr = m.group("addr")
        if addr.startswith("0x") or addr.startswith("0X"):
            hex_part = addr[2:]
        else:
            hex_part = addr
        addr = "0x" + (hex_part.lstrip("0") or "0")
        symbol = _run_symbolizer(llvm_symbolizer, so_path, addr)
        if symbol is None:
            failures += 1
            out.append(frame)
        else:
            changed += 1
            out.append(f"{m.group('prefix')}{m.group('addr')} {m.group('module')} ({symbol})")
    if changed == 0:
        # No frame was actually symbolized — do not claim success.
        status = "unavailable"
    elif failures == 0:
        status = "ok"
    elif changed > 0:
        status = "partial"
    else:
        status = "unavailable"
    return SymbolizeResult(out, status)
