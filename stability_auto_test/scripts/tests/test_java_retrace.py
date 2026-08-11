from __future__ import annotations

from pathlib import Path

from sat.analyzers.java_retrace import deobfuscate_stack, parse_mapping

_MAPPING = """com.a.a -> com.example.Main:
    void a() -> onResume
    void b(int) -> onDestroy
com.b.b -> com.example.Util:
    java.lang.String c() -> name
"""


def test_mapping_parser_and_deobfuscation(tmp_path: Path):
    mapping = parse_mapping(_MAPPING)
    assert mapping["classes"]["com.a.a"] == "com.example.Main"
    assert mapping["members"][("com.a.a", "a")] == "onResume"

    path = tmp_path / "mapping.txt"
    path.write_text(_MAPPING, encoding="utf-8")
    frames = [
        "at com.a.a.a(X.java:1)",
        "at com.a.a.b(X.java:2)",
    ]
    result = deobfuscate_stack(frames, mapping_path=path)
    assert result.frames == [
        "at com.example.Main.onResume(X.java:1)",
        "at com.example.Main.onDestroy(X.java:2)",
    ]
    assert result.status == "ok"


def test_missing_tool_falls_back_to_builtin(tmp_path: Path):
    path = tmp_path / "mapping.txt"
    path.write_text(_MAPPING, encoding="utf-8")
    result = deobfuscate_stack(
        ["at com.a.a.a(X.java:1)"],
        mapping_path=path,
        retrace_command="/nonexistent/retrace",
    )
    assert result.frames == ["at com.example.Main.onResume(X.java:1)"]
    assert result.status == "fallback"


def test_no_mapping_file_preserves_original(tmp_path: Path):
    frames = ["at com.a.a.a(X.java:1)"]
    result = deobfuscate_stack(frames, mapping_path=tmp_path / "missing.txt")
    assert result.frames == frames
    assert result.status == "unavailable"
