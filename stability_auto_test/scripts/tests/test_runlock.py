from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sat.runlock import RunLock, RunLockError


def test_two_processes_race_only_one_acquires_lock(tmp_path: Path):
    """Three independent processes racing for the same lock: only one wins.

    Uses a file-based barrier: each subprocess writes a ready marker, then
    all wait until all markers exist before attempting the O_EXCL lock
    creation at the same time.
    """
    scripts_dir = str(Path(__file__).parent.parent)
    # Build the subprocess script via .format() to get brace escaping right.
    script = (
        "import sys, json, time, os\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, {scripts_dir!r})\n"
        "from sat.runlock import RunLock, RunLockError\n"
        "output_dir = Path(sys.argv[1])\n"
        "idx = sys.argv[2]\n"
        "rdy = output_dir / ('.ready.' + idx)\n"
        "rdy.write_text('ok')\n"
        "deadline = time.time() + 20.0\n"
        "while time.time() < deadline:\n"
        "    ready = list(output_dir.glob('.ready.*'))\n"
        "    if len(ready) >= 3:\n"
        "        break\n"
        "    time.sleep(0.02)\n"
        "else:\n"
        "    print(json.dumps({{'idx': idx, 'result': 'timeout'}}))\n"
        "    sys.exit(1)\n"
        "try:\n"
        "    lock = RunLock(output_dir, run_id='run-' + idx, device='test-device')\n"
        "    lock.acquire()\n"
        "    print(json.dumps({{'idx': idx, 'result': 'acquired'}}))\n"
        "    time.sleep(2.0)\n"
        "    lock.release()\n"
        "except RunLockError:\n"
        "    print(json.dumps({{'idx': idx, 'result': 'rejected'}}))\n"
    ).format(scripts_dir=scripts_dir)

    import json

    procs = []
    for i in range(3):
        p = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(i)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append(p)

    results = []
    for p in procs:
        out, err = p.communicate(timeout=30)
        if out.strip():
            results.append(json.loads(out.strip()))

    acquired = [r for r in results if r["result"] == "acquired"]
    rejected = [r for r in results if r["result"] == "rejected"]

    assert len(acquired) == 1, (
        f"expected 1 winner, got {acquired}; rejected={rejected}; all={results}"
    )
    assert len(rejected) == 2, f"expected 2 rejections, got {rejected}; all={results}"


def test_run_lock_rejects_second_instance_same_process(tmp_path: Path):
    """Within the same process, a second acquire must fail."""
    lock = RunLock(tmp_path, run_id="run-1", device="d1")
    lock.acquire()
    try:
        lock2 = RunLock(tmp_path, run_id="run-2", device="d2")
        with pytest.raises(RunLockError):
            lock2.acquire()
    finally:
        lock.release()
