"""Privacy redaction for logs, reports and share bundles."""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Pattern

from .atomic_io import atomic_write_json

POLICY_VERSION = "1.0"

DEFAULT_PATTERNS: List[Pattern] = [
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # email
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # CN mobile
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b[=:]\s*\S+"),
    re.compile(r"[-+]?\d{1,2}\.\d{3,},\s*[-+]?\d{1,3}\.\d{3,}"),  # lat/lng
]


@dataclass
class Redactor:
    patterns: List[Pattern] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    version: str = POLICY_VERSION

    @classmethod
    def from_config(cls, regexes: Optional[List[str]] = None) -> "Redactor":
        patterns = list(DEFAULT_PATTERNS)
        for raw in regexes or []:
            try:
                patterns.append(re.compile(raw))
            except re.error as e:
                raise ValueError(f"invalid redaction regex {raw!r}: {e}") from e
        return cls(patterns=patterns)

    def redact(self, text: str) -> tuple:
        hits = 0
        for pattern in self.patterns:
            text, n = pattern.subn("[REDACTED]", text)
            hits += n
        return text, hits

    def redact_value(self, value):
        if isinstance(value, str):
            return self.redact(value)[0]
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        return value

    def redact_dict(self, data: Dict) -> Dict:
        out = self.redact_value(data)
        assert isinstance(out, dict)
        return out


def redact_output_dir(output_dir: Path, redactor: Redactor) -> Dict:
    """Redact report/incidents/logcat/status in place; returns redaction stats."""
    output_dir = Path(output_dir)
    hits = 0
    report_path = output_dir / "report.json"
    result: Dict = {}
    if report_path.exists():
        result = redactor.redact_dict(json.loads(report_path.read_text()))
        atomic_write_json(report_path, result)

    for inc in (output_dir / "incidents").glob("*.json"):
        data = redactor.redact_dict(json.loads(inc.read_text()))
        atomic_write_json(inc, data)

    for log in output_dir.glob("logcat_*.log"):
        lines = []
        for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
            redacted, n = redactor.redact(raw)
            hits += n
            lines.append(redacted)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Also redact CSV files (events, lifecycle) which may contain raw process
    # names, summaries, etc.
    for csv_file in output_dir.glob("*.csv"):
        lines = []
        for raw in csv_file.read_text(encoding="utf-8", errors="replace").splitlines():
            redacted, n = redactor.redact(raw)
            hits += n
            lines.append(redacted)
        csv_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Redact context files inside incidents/
    for ctx_file in (output_dir / "incidents").glob("*_context.txt"):
        lines = []
        for raw in ctx_file.read_text(encoding="utf-8", errors="replace").splitlines():
            redacted, n = redactor.redact(raw)
            hits += n
            lines.append(redacted)
        ctx_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    status_path = output_dir / "status.json"
    if status_path.exists():
        atomic_write_json(
            status_path,
            redactor.redact_dict(json.loads(status_path.read_text())),
        )

    result["redaction"] = {
        "policy_version": redactor.version,
        "hits": hits,
    }
    atomic_write_json(report_path, result)
    from .reporter import html as html_renderer

    html_renderer.write(result, output_dir)
    return result["redaction"]


def export_bundle(
    output_dir: Path,
    target: Path,
    *,
    redacted: bool = True,
) -> Path:
    output_dir = Path(output_dir)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(target).parent / f".export-{target.name}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    for item in output_dir.iterdir():
        if item.name in (".sat-run.lock",) or item.suffix == ".tmp":
            continue
        dest = staging / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    if redacted:
        redact_output_dir(staging, Redactor())
        # raw originals never enter the bundle
        (staging / "incident_journal.jsonl").write_text(
            "# redacted bundle: journal omitted (contains raw log lines)\n",
            encoding="utf-8",
        )
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in staging.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(staging))
    shutil.rmtree(staging)
    return target
