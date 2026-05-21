"""Library API integration example — runs against a real connected device.

NOT a unit test (uses real adb). Run manually:

    cd tools/perf_auto_test
    python tests/integration/lib_api_example.py com.android.settings

The script:
  1. opens a PerfTest context for the package
  2. drops a bookmark, sleeps 30 seconds, drops another bookmark
  3. asserts the report.json matches schema, bookmarks were recorded
  4. prints a brief summary

This mirrors how a parent test framework would embed perf monitoring around a
scenario it's driving with UI automation or some other stress tool.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import jsonschema

from perf_auto_test import PerfConfig, PerfTest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "report.schema.json"


def main(package: str, output_dir: Path) -> int:
    cfg = PerfConfig(
        package=package,
        output_dir=output_dir,
        wait_timeout_sec=30,
        cpu_interval_sec=1.0,
        mem_interval_sec=3.0,
        rescan_interval_sec=5.0,
        # Aggressive thresholds so a short run produces something:
        cpu_threshold_percent=20.0,
        cpu_sustain_sec=3.0,
        cpu_cooldown_sec=30.0,
        mem_threshold_pss_mb=100.0,
        mem_sustain_sec=5.0,
        mem_cooldown_sec=30.0,
        status_interval_sec=5.0,
        emit_junit=True,
    )

    print(f"[lib_api_example] starting; output={output_dir}", file=sys.stderr)
    with PerfTest(cfg) as t:
        t.bookmark("scenario_setup_done")
        print("[lib_api_example] sleeping 15s (phase 1)", file=sys.stderr)
        time.sleep(15)
        t.bookmark("phase_1_end", metadata={"notes": "first scenario complete"})
        print("[lib_api_example] sleeping 15s (phase 2)", file=sys.stderr)
        time.sleep(15)
        t.bookmark("phase_2_end")

    result = t.result
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(result, schema)
    print(f"[lib_api_example] schema OK", file=sys.stderr)

    bookmarks = result["bookmarks"]
    assert len(bookmarks) == 3, f"expected 3 bookmarks, got {len(bookmarks)}"

    print()
    print("=== Summary ===")
    print(f"  Package: {result['run']['package']}")
    print(f"  Exit:    {result['run']['exit_code']} ({result['run']['exit_reason']})")
    print(f"  Duration: {result['run']['duration_sec']:.0f}s")
    print(f"  Processes: {len(result['processes'])}")
    for p in result["processes"]:
        cpu = p["stats"]["cpu_pct"]
        mem = p["stats"]["mem_pss_mb"]
        cpu_str = f"mean={cpu['mean']:.1f} p95={cpu['p95']:.1f} max={cpu['max']:.1f}" if cpu else "(no samples)"
        mem_str = f"mean={mem['mean']:.1f} max={mem['max']:.1f}" if mem else "(no samples)"
        print(f"    - {p['name']}:")
        print(f"        CPU: {cpu_str}")
        print(f"        Mem: {mem_str}")
        print(f"        alerts: cpu={p['alerts']['cpu']} mem={p['alerts']['mem']}")
    print(f"  Incidents: {len(result['incidents'])}")
    print(f"  Bookmarks: {[b['label'] for b in bookmarks]}")
    print()
    print(f"Reports in: {output_dir}")
    return result["run"]["exit_code"]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python lib_api_example.py <package> [output_dir]", file=sys.stderr)
        sys.exit(2)
    pkg = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("./reports/lib_api_example")
    sys.exit(main(pkg, out))
