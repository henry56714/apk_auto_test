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

    # Raw logcat slices + dropbox bodies inside incidents/ (IMP-07: these were
    # previously left unredacted).
    for raw_file in (output_dir / "incidents").glob("*.txt"):
        lines = []
        for raw in raw_file.read_text(encoding="utf-8", errors="replace").splitlines():
            redacted, n = redactor.redact(raw)
            hits += n
            lines.append(redacted)
        raw_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Incident journal (JSONL) — redact line by line.
    journal_path = output_dir / "incident_journal.jsonl"
    if journal_path.exists():
        lines = []
        for raw in journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
            redacted, n = redactor.redact(raw)
            hits += n
            lines.append(redacted)
        journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Bookmark / workload manifests.
    for extra in ("bookmarks.jsonl", "workload_manifest.json", "replay.yaml"):
        extra_path = output_dir / extra
        if extra_path.exists():
            lines = []
            for raw in extra_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines():
                redacted, n = redactor.redact(raw)
                hits += n
                lines.append(redacted)
            extra_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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


# Files copied verbatim into a *raw* export, and the only files allowed into a
# redacted export (allowlist, spec S1-05 / IMP-07). Binary evidence
# (tombstones, traces) and the journal never enter a redacted bundle.
REDACTED_ALLOWLIST_GLOBS = (
    "report.json",
    "junit.xml",
    "summary.md",
    "status.json",
    "*.csv",
    "logcat_*.log",
    "bookmarks*",
    "workload_manifest.json",
    "replay.yaml",
    "self_resource.jsonl",
    "incidents/*.json",
    "incidents/*_context.txt",
    "incidents/*.txt",
    "incidents/*_dropbox.txt",
)

TEXT_SUFFIXES = (
    ".json",
    ".jsonl",
    ".csv",
    ".log",
    ".txt",
    ".md",
    ".yaml",
    ".yml",
)

REDACTION_MANIFEST_NAME = "redaction_manifest.json"


def _matches_allowlist(rel: Path) -> bool:
    rel_str = str(rel)
    for pattern in REDACTED_ALLOWLIST_GLOBS:
        if Path(rel_str).match(pattern):
            return True
    return False


def _redact_text_file(path: Path, redactor: Redactor) -> int:
    hits = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        out_lines = []
        for raw in fh:
            redacted, n = redactor.redact(raw)
            hits += n
            out_lines.append(redacted)
    path.write_text("".join(out_lines), encoding="utf-8")
    return hits


def _scan_tree_for_secrets(root: Path, patterns: List[Pattern]) -> int:
    """Count remaining pattern hits across every text file in the tree."""
    total = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in patterns:
            total += len(pattern.findall(text))
    return total


def export_bundle(
    output_dir: Path,
    target: Path,
    *,
    redacted: bool = True,
    raw: bool = False,
    acknowledge_sensitive: bool = False,
    extra_regexes: Optional[List[str]] = None,
) -> Path:
    """Export a run directory as a zip bundle.

    - Default is a *redacted* bundle built from an allowlist; unknown or
      binary files never enter it.
    - Raw exports must be explicitly acknowledged (`raw=True` +
      `acknowledge_sensitive=True`) and copy the directory verbatim.
    - After a redacted export the bundle is scanned with the full pattern set
      (defaults + `extra_regexes`); any canary hit deletes the zip and raises,
      so sensitive data can never ship by accident (T-L0-023).
    """
    output_dir = Path(output_dir)
    target = Path(target)
    if raw and not acknowledge_sensitive:
        raise ValueError(
            "raw export requires --acknowledge-sensitive: the bundle will "
            "contain unredacted evidence"
        )

    redactor = Redactor.from_config(extra_regexes)
    scan_patterns = list(redactor.patterns)

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(target).parent / f".export-{target.name}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    total_hits = 0
    if raw:
        # Explicit raw export: verbatim copy, no allowlist, no scan.
        for item in output_dir.iterdir():
            if item.name in (".sat-run.lock",) or item.suffix == ".tmp":
                continue
            dest = staging / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
    else:
        for item in output_dir.iterdir():
            if item.name in (".sat-run.lock",) or item.suffix == ".tmp":
                continue
            rel = Path(item.name)
            if item.is_dir():
                for sub in item.rglob("*"):
                    if sub.is_file() and _matches_allowlist(sub.relative_to(output_dir)):
                        dest = staging / sub.relative_to(output_dir)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(sub, dest)
            elif _matches_allowlist(rel):
                shutil.copy2(item, staging / rel)
        # Redact every text file in the staged tree (streamed per line).
        for p in sorted(staging.rglob("*")):
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
                total_hits += _redact_text_file(p, redactor)
        # Journal replaced with an explicit digest stub — it contains raw log
        # lines and is never part of a shareable bundle.
        (staging / "incident_journal.jsonl").write_text(
            "# redacted bundle: journal omitted (contains raw log lines)\n",
            encoding="utf-8",
        )
        (staging / REDACTION_MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "policy_version": redactor.version,
                    "hits": total_hits,
                    "allowlisted_only": True,
                    "binary_evidence_excluded": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # Canary scan (redacted bundles only): any hit destroys the product.
    if not raw:
        remaining = _scan_tree_for_secrets(staging, scan_patterns)
        if remaining > 0:
            shutil.rmtree(staging)
            if target.exists():
                target.unlink()
            raise ValueError(
                f"redaction failed: {remaining} sensitive pattern hit(s) "
                "remain in the bundle; export aborted and artifacts removed"
            )

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(staging.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(staging))
    shutil.rmtree(staging)
    return target
