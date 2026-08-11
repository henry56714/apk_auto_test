from __future__ import annotations

from sat.analyzers.exit_correlation import correlate_exit_info


def _incident(pid=1234, ts="2026-05-21 10:00:00.000"):
    return {
        "id": "incident-001",
        "type": "java_crash",
        "process": "com.example.app",
        "pid": pid,
        "triggered_at": ts,
        "evidence": {},
    }


def _exit_record(pid=1234, ts="2026-05-21T10:00:01.000", reason="crashed"):
    return {
        "pid": pid,
        "process": "com.example.app",
        "timestamp": ts,
        "exit_reason": reason,
        "source": "exit_info",
        "confidence": "high",
        "expected": False,
        "category": "crash",
        "is_stability_failure": True,
    }


def test_same_crash_from_multiple_sources_forms_one_occurrence():
    incidents = [_incident()]
    records = [_exit_record(), _exit_record(), _exit_record(reason="signaled")]
    annotated = correlate_exit_info(incidents, records)
    assert len(incidents) == 1
    assert all(r["correlated_incident_id"] == "incident-001" for r in annotated)
    assert incidents[0]["evidence"]["exit_info_correlated"] is True
    assert incidents[0]["evidence"]["exit_info_reason"] == "crashed"


def test_normal_recycle_is_not_turned_into_incident():
    incidents = []
    records = [{
        "pid": 777,
        "process": "com.example.app",
        "timestamp": "2026-05-21T10:00:00.000",
        "exit_reason": "normal_recycle",
        "source": "exit_info",
        "confidence": "high",
        "expected": True,
        "category": "process_exit",
        "is_stability_failure": False,
    }]
    annotated = correlate_exit_info(incidents, records)
    assert incidents == []
    assert annotated[0]["correlated_incident_id"] is None


def test_time_distant_records_do_not_correlate():
    incidents = [_incident()]
    records = [_exit_record(ts="2026-05-21T11:00:00.000")]
    annotated = correlate_exit_info(incidents, records)
    assert annotated[0]["correlated_incident_id"] is None
    assert "exit_info_correlated" not in incidents[0]["evidence"]


def test_pid_mismatch_does_not_correlate():
    incidents = [_incident(pid=1234)]
    records = [_exit_record(pid=9999)]
    annotated = correlate_exit_info(incidents, records)
    assert annotated[0]["correlated_incident_id"] is None
