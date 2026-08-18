"""Golden-corpus privacy (T-NF-010): fixtures only carry synthetic canaries."""

from __future__ import annotations

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).parent

# Synthetic domains used across fixtures; anything else is a leak.
_ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "test.local",
    "localhost",
)

_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")

_SERIAL_RE = re.compile(r"\b(?:emulator-\d{4}|R\d{2}[A-Z]{2}\d{6}|[A-Z]{3}\d{5,})\b")


def test_fixtures_contain_only_synthetic_emails():
    for path in TESTS_ROOT.rglob("*"):
        if path.is_dir() or "__pycache__" in str(path):
            continue
        if path.suffix not in (".py", ".json", ".jsonl", ".txt", ".yaml", ".md", ".csv"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _EMAIL_RE.finditer(text):
            domain = match.group(1).lower()
            assert domain in _ALLOWED_EMAIL_DOMAINS, (
                f"non-synthetic email {match.group(0)!r} in {path}"
            )


def test_fixtures_have_no_real_device_serials():
    for path in TESTS_ROOT.rglob("*"):
        if path.is_dir() or "__pycache__" in str(path):
            continue
        if path.suffix not in (".py", ".json", ".jsonl", ".txt", ".yaml", ".md"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _SERIAL_RE.finditer(text):
            token = match.group(0)
            if token.startswith("emulator-"):
                continue  # synthetic test serial
            assert False, f"real-looking device serial {token!r} in {path}"
