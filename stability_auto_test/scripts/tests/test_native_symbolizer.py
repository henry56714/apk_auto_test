from __future__ import annotations

import stat
from pathlib import Path

from sat.analyzers.native_symbolizer import symbolize_frames


def _make_fake_symbolizer(tmp_path: Path) -> Path:
    script = tmp_path / "llvm-symbolizer"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "obj = args[args.index('--obj') + 1]\n"
        "addr = args[-1]\n"
        "if 'libmap.so' in obj and addr == '0x3a1c84':\n"
        "    print('TileCache::get')\n"
        "    print('/src/tile.cpp:42')\n"
        "else:\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_symbolize_frames_with_fixture(tmp_path: Path):
    symbols = tmp_path / "symbols" / "arm64"
    symbols.mkdir(parents=True)
    (symbols / "libmap.so").write_text("ELF", encoding="utf-8")
    tool = _make_fake_symbolizer(tmp_path)
    frames = [
        "#00 pc 0x3a1c84  /data/app/com.example/lib/arm64/libmap.so",
    ]
    result = symbolize_frames(
        frames,
        symbols_dir=symbols,
        llvm_symbolizer=str(tool),
    )
    assert result.status == "ok"
    assert "TileCache::get" in result.frames[0]
    assert "tile.cpp:42" in result.frames[0]
    assert "0x3a1c84" in result.frames[0]


def test_missing_symbolizer_preserves_original(tmp_path: Path):
    symbols = tmp_path / "symbols"
    symbols.mkdir()
    frames = ["#00 pc 0x3a1c84  /data/app/com.example/lib/arm64/libmap.so"]
    result = symbolize_frames(
        frames,
        symbols_dir=symbols,
        llvm_symbolizer="/nonexistent/llvm-symbolizer",
    )
    assert result.status == "unavailable"
    assert result.frames == frames


def test_android_tombstone_frame_without_0x(tmp_path: Path):
    """Real Android tombstone frames may lack the 0x prefix on addresses."""
    symbols = tmp_path / "symbols" / "arm64"
    symbols.mkdir(parents=True)
    (symbols / "libmap.so").write_text("ELF", encoding="utf-8")
    tool = _make_fake_symbolizer(tmp_path)
    # Frame WITHOUT 0x prefix (as seen in real tombstone output).
    frames = [
        "#00 pc 003a1c84  /data/app/com.example/lib/arm64/libmap.so",
    ]
    result = symbolize_frames(
        frames,
        symbols_dir=symbols,
        llvm_symbolizer=str(tool),
    )
    # Must still work — address is normalized to 0x prefix.
    assert result.status == "ok"
    assert "TileCache::get" in result.frames[0]


def test_unchanged_frames_are_not_reported_ok(tmp_path: Path):
    """When no frames are symbolised, status must NOT be 'ok'."""
    symbols = tmp_path / "symbols" / "arm64"
    symbols.mkdir(parents=True)
    (symbols / "libother.so").write_text("ELF", encoding="utf-8")
    tool = _make_fake_symbolizer(tmp_path)
    frames = [
        "#00 pc 0xdeadbeef  /data/app/com.example/lib/arm64/libother.so",
        "#01 pc 0xcafebabe  /data/app/com.example/lib/arm64/libother.so",
    ]
    result = symbolize_frames(
        frames,
        symbols_dir=symbols,
        llvm_symbolizer=str(tool),
    )
    # The fake symbolizer only handles libmap.so with 0x3a1c84.
    # These frames cannot be symbolised; status must not be "ok".
    assert result.status != "ok"
    assert result.status in ("unavailable", "partial")
    # Frames must be unchanged.
    assert result.frames == frames


def test_missing_so_preserves_original_and_marks_unavailable(tmp_path: Path):
    symbols = tmp_path / "symbols"
    symbols.mkdir()
    frames = ["#00 pc 0x3a1c84  /data/app/com.example/lib/arm64/libother.so"]
    result = symbolize_frames(
        frames,
        symbols_dir=symbols,
        llvm_symbolizer=str(_make_fake_symbolizer(tmp_path)),
    )
    assert result.status == "unavailable"
    assert result.frames == frames
