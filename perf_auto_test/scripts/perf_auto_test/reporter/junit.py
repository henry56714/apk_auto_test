"""Render a JUnit-style XML report so CI dashboards can show pass/fail.

One <testcase> per (process, metric) pair. A non-zero alert count for that
pair turns into a <failure> with summary text; otherwise the testcase passes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

JUNIT_FILENAME = "report.junit.xml"


def render(result: dict) -> str:
    run = result.get("run", {})
    package = run.get("package", "unknown")
    suite_name = f"perf_auto_test.{package}"

    testcases: list = []
    failure_count = 0
    error_count = 0

    # Add a setup/run-level testcase that reports the exit_reason.
    if int(run.get("exit_code", 0)) >= 2:
        error_count += 1
        testcases.append({
            "name": "perf_auto_test.setup",
            "classname": suite_name,
            "error": run.get("exit_reason", "setup_failed"),
        })

    for proc in result.get("processes", []):
        for metric in ("cpu", "mem"):
            alerts = proc.get("alerts", {}).get(metric, 0)
            tc = {
                "name": f"{proc['name']}.{metric}_threshold",
                "classname": suite_name,
            }
            if alerts > 0:
                failure_count += 1
                related = [i for i in result.get("incidents", [])
                           if i.get("process") == proc["name"]
                           and i.get("type") == f"{metric}_threshold"]
                peaks = [i.get("observed", {}).get("peak", 0) for i in related]
                peak = max(peaks) if peaks else 0.0
                tc["failure"] = (
                    f"{alerts} {metric} threshold alert(s) fired; peak={peak:.2f}"
                )
            testcases.append(tc)

    suite = ET.Element("testsuite", {
        "name": suite_name,
        "tests": str(len(testcases)),
        "failures": str(failure_count),
        "errors": str(error_count),
        "time": str(run.get("duration_sec", 0)),
    })
    for tc in testcases:
        attrs = {"name": tc["name"], "classname": tc["classname"]}
        node = ET.SubElement(suite, "testcase", attrs)
        if "failure" in tc:
            f = ET.SubElement(node, "failure", {"message": tc["failure"]})
            f.text = tc["failure"]
        elif "error" in tc:
            e = ET.SubElement(node, "error", {"message": tc["error"]})
            e.text = tc["error"]

    suites = ET.Element("testsuites")
    suites.append(suite)
    ET.indent(suites, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suites, encoding="unicode")


def write(result: dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / JUNIT_FILENAME
    path.write_text(render(result), encoding="utf-8")
    return path
