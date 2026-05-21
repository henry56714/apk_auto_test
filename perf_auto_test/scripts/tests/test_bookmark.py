"""Bookmark writer tests."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from pat.bookmark import BookmarkWriter


class TestBookmarkWriter:
    def test_append_creates_file(self, tmp_path: Path):
        bw = BookmarkWriter(tmp_path)
        bw.append("phase_1")
        assert (tmp_path / "bookmarks.jsonl").exists()

    def test_read_all_returns_appended(self, tmp_path: Path):
        bw = BookmarkWriter(tmp_path)
        bw.append("a")
        bw.append("b", metadata={"k": "v"})
        entries = bw.read_all()
        labels = [e["label"] for e in entries]
        assert labels == ["a", "b"]
        assert entries[1]["metadata"] == {"k": "v"}

    def test_external_append_visible(self, tmp_path: Path):
        """A parent test harness can write JSON lines directly to the file."""
        bw = BookmarkWriter(tmp_path)
        bw.append("from_library")
        # Simulate parent process appending.
        path = tmp_path / "bookmarks.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-05-15T10:01:00Z",
                                "label": "from_outside", "metadata": {}}) + "\n")
        labels = [e["label"] for e in bw.read_all()]
        assert labels == ["from_library", "from_outside"]

    def test_malformed_lines_skipped(self, tmp_path: Path):
        path = tmp_path / "bookmarks.jsonl"
        path.write_text(
            "this is not json\n"
            + json.dumps({"timestamp": "t", "label": "ok"}) + "\n"
            + "{ malformed json\n"
            + json.dumps({"timestamp": "t2", "label": "also_ok"}) + "\n",
            encoding="utf-8",
        )
        bw = BookmarkWriter(tmp_path)
        labels = [e["label"] for e in bw.read_all()]
        assert labels == ["ok", "also_ok"]

    def test_read_all_missing_file_returns_empty(self, tmp_path: Path):
        bw = BookmarkWriter(tmp_path)
        # No appends; file doesn't exist
        assert bw.read_all() == []

    def test_concurrent_appends_serialized(self, tmp_path: Path):
        bw = BookmarkWriter(tmp_path)

        def worker(start: int):
            for i in range(start, start + 50):
                bw.append(f"label_{i}")

        threads = [threading.Thread(target=worker, args=(i * 50,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries = bw.read_all()
        assert len(entries) == 200
        # All labels distinct
        labels = {e["label"] for e in entries}
        assert len(labels) == 200
