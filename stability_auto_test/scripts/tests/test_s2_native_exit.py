"""S2: native symbol ABI/build-id matching (T-L0-012) and exit taxonomy
(T-L0-028)."""

from __future__ import annotations

from pathlib import Path

from sat.analyzers.native_symbolizer import _find_so, symbolize_frames
from sat.collectors.exit_info import (
    ExitInfoRecord,
    parse_exit_info_text,
)

# ── T-L0-012: same-named .so in two ABIs — only the matching one is used ─────


def test_find_so_matches_abi_directory(tmp_path: Path):
    arm = tmp_path / "arm64-v8a"
    x86 = tmp_path / "x86_64"
    arm.mkdir()
    x86.mkdir()
    (arm / "libfaultlab.so").write_bytes(b"arm")
    (x86 / "libfaultlab.so").write_bytes(b"x86")

    chosen = _find_so(tmp_path, "libfaultlab.so", abi="arm64-v8a")
    assert chosen is not None
    assert "arm64-v8a" in str(chosen)

    chosen_x86 = _find_so(tmp_path, "libfaultlab.so", abi="x86_64")
    assert "x86_64" in str(chosen_x86)


def test_find_so_matches_build_id_sidecar(tmp_path: Path):
    arm = tmp_path / "arm64-v8a"
    x86 = tmp_path / "x86_64"
    arm.mkdir()
    x86.mkdir()
    (arm / "libfaultlab.so").write_bytes(b"arm")
    (x86 / "libfaultlab.so").write_bytes(b"x86")
    (arm / "libfaultlab.so.build-id").write_text("aaaa1111")
    (x86 / "libfaultlab.so.build-id").write_text("bbbb2222")

    chosen = _find_so(tmp_path, "libfaultlab.so", build_id="bbbb2222")
    assert chosen is not None
    assert "x86_64" in str(chosen), "build id must win over name order"


def test_find_so_no_match_without_evidence(tmp_path: Path):
    arm = tmp_path / "arm64-v8a"
    x86 = tmp_path / "x86_64"
    arm.mkdir()
    x86.mkdir()
    (arm / "libfaultlab.so").write_bytes(b"arm")
    (x86 / "libfaultlab.so").write_bytes(b"x86")
    # No ABI, no build id: the ambiguous pick must NOT silently take the first.
    assert _find_so(tmp_path, "libfaultlab.so") is None


def test_symbolize_without_matching_so_is_unavailable(tmp_path: Path):
    frames = ["#00 pc 00001234  /data/app/libmissing.so"]
    result = symbolize_frames(
        frames,
        symbols_dir=tmp_path,
        llvm_symbolizer="llvm-symbolizer",
        abi="arm64-v8a",
        build_id="ffff",
    )
    assert result.status == "unavailable"
    assert result.frames == frames  # raw frames preserved


# ── T-L0-028: exit taxonomy expected/failure/unknown ─────────────────────────


def _rec(reason: str, expected: bool = False) -> ExitInfoRecord:
    return ExitInfoRecord(
        pid=1,
        process="com.example.app",
        timestamp="2026-08-13T10:00:00",
        exit_reason=reason,
        expected=expected,
    )


def test_exit_taxonomy():
    from sat.pool import CollectorPool

    assert CollectorPool._exit_taxonomy(_rec("user_requested", expected=True)) == "expected"
    assert CollectorPool._exit_taxonomy(_rec("exit_self", expected=True)) == "expected"
    assert CollectorPool._exit_taxonomy(_rec("crashed")) == "failure"
    assert CollectorPool._exit_taxonomy(_rec("anr")) == "failure"
    assert CollectorPool._exit_taxonomy(_rec("low_memory")) == "failure"
    assert CollectorPool._exit_taxonomy(_rec("signaled")) == "failure"
    assert CollectorPool._exit_taxonomy(_rec("other")) == "unknown"


def test_unknown_exit_reason_never_mislabeled_expected():
    rec = parse_exit_info_text(
        "Package: com.example.app\n"
        "  Process: com.example.app (pid 9)\n"
        "  Timestamp: 2026-08-13T10:00:00.000\n"
        "  Reason: FUTURE_ANDROID_REASON_42\n"
    )[0]
    assert rec.expected is False
    assert rec.category == "unknown"
    assert rec.raw_reason == "FUTURE_ANDROID_REASON_42"
