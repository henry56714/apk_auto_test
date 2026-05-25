"""Bookmark writer: line-oriented JSON appender.

Two write paths share one file (`bookmarks.jsonl`):
- Python library API: `PerfTest.bookmark(label, metadata)` calls `append()`
- External process (e.g. a shell-driven test harness): writes a JSON line
  directly with `echo '{...}' >> bookmarks.jsonl`

At end of run, the reporter calls `read_all()` to fold both sources into the
report's bookmarks[] array.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .utils import utc_now_iso

log = logging.getLogger(__name__)

BOOKMARKS_FILENAME = "bookmarks.jsonl"


class BookmarkWriter:
    def __init__(self, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.path = output_dir / BOOKMARKS_FILENAME
        self._lock = threading.Lock()
        # Touch on init so external tailers don't have to handle "file not yet
        # created". An empty file is a valid JSONL document.
        if not self.path.exists():
            self.path.touch()

    def append(self, label: str, metadata: Optional[Dict] = None) -> Dict:
        entry = {
            "timestamp": utc_now_iso(),
            "label": str(label),
            "metadata": dict(metadata) if metadata else {},
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        return entry

    def read_all(self) -> List[Dict]:
        """Return all entries from the file (library + external appends)."""
        if not self.path.exists():
            return []
        out: List[Dict] = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("could not read bookmarks file: %s", e)
            return []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                entry = json.loads(s)
            except json.JSONDecodeError:
                log.warning("skipping malformed bookmark line: %r", s[:120])
                continue
            if isinstance(entry, dict) and "label" in entry and "timestamp" in entry:
                out.append(entry)
        return out
