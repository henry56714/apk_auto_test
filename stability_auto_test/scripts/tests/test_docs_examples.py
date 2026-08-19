from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import yaml
from sat.config import validate_config

SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT = SCRIPTS.parent
ROOT = PROJECT.parent


def _python_blocks(md: str):
    return re.findall(r"```python\n(.*?)```", md, re.S)


def _sat_python_blocks():
    blocks = []
    for path in (PROJECT / "README.md", ROOT / "README.md", ROOT / "README.zh.md"):
        blocks.extend(
            b for b in _python_blocks(path.read_text(encoding="utf-8"))
            if "from sat" in b
        )
    return blocks


def test_readme_python_api_examples_run():
    blocks = _sat_python_blocks()
    assert blocks
    sys.path.insert(0, str(SCRIPTS))
    try:
        for block in blocks:
            exec(compile(block, "<readme-example>", "exec"), {})
    finally:
        sys.path.remove(str(SCRIPTS))


def test_readme_parameters_match_cli_help():
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    params_section = readme.split("## CLI 参数", 1)[1].split("## Python 库 API", 1)[0]
    help_run = subprocess.run(
        [sys.executable, "-m", "sat", "--help"],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        check=True,
    )
    help_text = help_run.stdout
    for sub in ("doctor", "recover"):
        sub_run = subprocess.run(
            [sys.executable, "-m", "sat", sub, "--help"],
            cwd=SCRIPTS,
            capture_output=True,
            text=True,
            check=True,
        )
        help_text += sub_run.stdout
    readme_flags = set(re.findall(r"(?<![A-Za-z])--[a-z][a-z-]+", params_section))
    help_flags = set(re.findall(r"--[a-z][a-z-]+", help_text))
    missing = sorted(f for f in readme_flags if f not in help_flags)
    assert not missing, f"README flags missing from --help: {missing}"


def test_config_example_is_valid():
    data = yaml.safe_load((SCRIPTS / "config.example.yaml").read_text(encoding="utf-8"))
    assert validate_config(data) == []


def test_example_ci_workflow_parses():
    workflow = ROOT / ".github" / "workflows" / "stability-smoke.yml"
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    assert data["name"] == "stability-smoke"
    jobs = data["jobs"]["smoke"]["steps"]
    assert any("upload-artifact" in s.get("uses", "") for s in jobs)


def test_generated_html_has_no_cdn_dependency(tmp_path: Path):
    from sat.reporter import html as html_renderer
    result = {
        "schema_version": "1.15",
        "run": {
            "package": "com.example.app",
            "started_at": "2026-05-21 10:00:00.000",
            "ended_at": "2026-05-21 10:05:00.000",
            "duration_sec": 300.0,
            "exit_code": 0,
            "exit_reason": "duration_elapsed",
            "device": {"serial": "x", "android_version": "14"},
        },
            "processes": [],
            "incidents": [],
            "issue_groups": [],
            "exit_info": [],
            "device_events": [],
            "resource_risk": [],
            "self_resource": {},
        "event_pipeline": {
            "detected_count": 0, "persisted_count": 0, "failed_count": 0,
            "timed_out_count": 0, "dropped_by_cap_count": 0,
            "dropped_by_backpressure_count": 0,
        },
        "collection_health": "healthy",
        "coverage_ratio": 1.0,
        "verdict": "stable",
            "collectors": {},
            "policy": {"enabled": False, "passed": True, "rules": []},
            "recovery_warnings": [],
        "lifecycle_events": [],
        "bookmarks": [],
        "data_files": {"events": [], "lifecycle": [], "logcat": [], "journal": []},
    }
    path = html_renderer.write(result, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "cdn.plot.ly" not in text
    assert "https://cdn" not in text
    assert "Plotly.newPlot" in text


def test_demo_report_offline_and_interactive(tmp_path: Path):
    out = tmp_path / "demo"
    from sat.demo import generate_demo
    generate_demo.OUTPUT_DIR = out
    generate_demo.main()
    html_text = (out / "report.html").read_text(encoding="utf-8")
    assert "cdn.plot.ly" not in html_text
    assert "filter" in html_text.lower()
    assert "Plotly.newPlot" in html_text


def test_wheel_contains_resources_and_excludes_tests(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
         "--no-build-isolation", "--wheel-dir", str(dist)],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        check=True,
    )
    wheel = next(dist.glob("stability_auto_test-*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        joined = "\n".join(names)
        assert "tests/" not in joined
        assert "sat/demo/" not in joined
        assert any("schemas/report.schema.json" in n for n in names)
        metadata_name = next(n for n in names if n.endswith("METADATA"))
        metadata = zf.read(metadata_name).decode("utf-8")
        assert "License" in metadata
        assert "Repository" in metadata
        assert "stability" in metadata.lower()
        assert "README" in metadata or "Description" in metadata
