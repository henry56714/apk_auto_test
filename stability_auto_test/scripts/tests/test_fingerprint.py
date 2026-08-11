from __future__ import annotations

from sat.analyzers.fingerprint import fingerprint_incident, group_incidents


def _incident(**kw):
    base = {
        "type": "java_crash",
        "process": "com.example.app",
        "pid": 1234,
        "triggered_at": "2026-05-21 10:00:00.000",
        "summary": "java.lang.RuntimeException: boom",
        "evidence": {
            "exception_class": "java.lang.RuntimeException",
            "top_frames": [
                "at com.example.Main.onResume(MainActivity.java:42)",
                "at com.example.App.dispatch(App.java:10)",
            ],
        },
    }
    base.update(kw)
    return base


def test_java_crash_same_bug_different_line_numbers_same_fingerprint():
    a = _incident(evidence={
        "exception_class": "java.lang.RuntimeException",
        "top_frames": [
            "at com.example.Main.onResume(MainActivity.java:42)",
            "at com.example.App.dispatch(App.java:10)",
        ],
    })
    b = _incident(evidence={
        "exception_class": "java.lang.RuntimeException",
        "top_frames": [
            "at com.example.Main.onResume(MainActivity.java:99)",
            "at com.example.App.dispatch(App.java:123)",
        ],
    })
    assert fingerprint_incident(a) == fingerprint_incident(b)


def test_java_crash_different_entry_points_different_fingerprint():
    a = _incident(evidence={
        "exception_class": "java.lang.NullPointerException",
        "top_frames": [
            "at com.example.Main.onResume(MainActivity.java:42)",
        ],
    })
    b = _incident(evidence={
        "exception_class": "java.lang.NullPointerException",
        "top_frames": [
            "at com.example.Service.onBind(Service.java:7)",
        ],
    })
    assert fingerprint_incident(a) != fingerprint_incident(b)


def test_native_aslr_address_change_same_fingerprint():
    a = _incident(type="native_crash", evidence={
        "signal": "SIGSEGV",
        "top_frames": [
            "#00 pc 0x00000000003a1c84  /data/app/.../lib/arm64/libmap.so (TileCache::get+0x48)",
        ],
    })
    b = _incident(type="native_crash", evidence={
        "signal": "SIGSEGV",
        "top_frames": [
            "#00 pc 0x00000000007f1234  /data/app/.../lib/arm64/libmap.so (TileCache::get+0x48)",
        ],
    })
    assert fingerprint_incident(a) == fingerprint_incident(b)
    c = _incident(type="native_crash", evidence={
        "signal": "SIGSEGV",
        "top_frames": [
            "#00 pc 0x00000000003a1c84  /data/app/.../lib/arm64/libother.so (Foo::bar+0x10)",
        ],
    })
    assert fingerprint_incident(a) != fingerprint_incident(c)


def test_anr_and_process_death_fingerprints():
    anr_a = _incident(type="anr", summary="ANR: input dispatching timed out", evidence={
        "reason": "input dispatching timed out",
        "top_frames": [
            "at com.example.Main.handleInput(Main.java:5)",
        ],
    })
    anr_b = _incident(type="anr", summary="ANR: input dispatching timed out", evidence={
        "reason": "input dispatching timed out",
        "top_frames": [
            "at com.example.Main.handleInput(Main.java:9)",
        ],
    })
    assert fingerprint_incident(anr_a) == fingerprint_incident(anr_b)

    pd_a = _incident(type="process_death", evidence={"reason": "cached"})
    pd_b = _incident(type="process_death", evidence={"reason": "anr"})
    assert fingerprint_incident(pd_a) != fingerprint_incident(pd_b)


def test_grouping_counts_real_repeated_occurrences():
    incidents = [_incident(id=f"incident-{i:03d}", triggered_at=f"2026-05-21 10:00:{i:02d}.000")
                 for i in range(10)]
    incidents.append(_incident(
        id="incident-999",
        evidence={
            "exception_class": "java.lang.IllegalStateException",
            "top_frames": ["at com.example.Other.run(Other.java:1)"],
        },
    ))
    groups = group_incidents(incidents)
    counts = sorted(g["occurrence_count"] for g in groups)
    assert counts == [1, 10]
    big = next(g for g in groups if g["occurrence_count"] == 10)
    assert len(big["occurrence_ids"]) == 10
    assert big["representative_incident_id"] == "incident-000"
