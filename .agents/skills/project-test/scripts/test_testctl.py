#!/usr/bin/env python3
"""Regression tests for the deterministic project-test helper."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import testctl  # noqa: E402


class PatternTests(unittest.TestCase):
    def test_recursive_glob_matches_direct_and_nested_files(self) -> None:
        patterns = ["tests/**/*.py"]
        self.assertTrue(testctl._matches("tests/test_api.py", patterns))
        self.assertTrue(testctl._matches("tests/device/test_api.py", patterns))
        self.assertFalse(testctl._matches("src/test_api.py", patterns))


class ScopeDigestTests(unittest.TestCase):
    def test_digest_includes_change_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "same.txt").write_text("same", encoding="utf-8")
            modified = testctl._scope_digest(
                repo, [{"status": "M", "path": "same.txt"}]
            )
            added = testctl._scope_digest(
                repo, [{"status": "A", "path": "same.txt"}]
            )
            self.assertNotEqual(modified, added)


class ExecutionSnapshotTests(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _create_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "test@example.invalid")
        self._git(repo, "config", "user.name", "project-test")
        protected = repo / "tests" / "protected.txt"
        protected.parent.mkdir()
        protected.write_text("before", encoding="utf-8")
        self._git(repo, "add", "tests/protected.txt")
        self._git(repo, "commit", "-q", "-m", "fixture")
        return repo

    def test_protected_asset_mutation_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._create_repo(root)

            plan = {
                "schema_version": 1,
                "project": {"id": "fixture"},
                "test_assets": {"protected_paths": ["tests/**"]},
                "maintenance": {"editable_paths": ["tests/**"]},
                "requirements": {},
                "inventory": [],
                "features": {},
                "suites": {
                    "mutate": {
                        "kind": "static",
                        "level": "L0",
                        "commands": [
                            {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; "
                                    "Path('tests/protected.txt').write_text('after')",
                                ]
                            }
                        ],
                    }
                },
            }
            plan_path = root / "test-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            report_dir = root / "report"
            with contextlib.redirect_stdout(io.StringIO()):
                result = testctl.command_run(
                    plan,
                    plan_path,
                    repo,
                    "mutate",
                    [],
                    False,
                    str(report_dir),
                )

            self.assertEqual(testctl.EXIT_STALE, result)
            report = json.loads(
                (report_dir / "result.json").read_text(encoding="utf-8")
            )
            self.assertEqual("STALE", report["verdict"])
            self.assertIn(
                "protected test assets changed during execution",
                report["stale_reasons"],
            )

    def test_missing_requirement_is_fail_and_command_does_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._create_repo(root)
            plan = {
                "schema_version": 1,
                "project": {"id": "fixture"},
                "test_assets": {"protected_paths": ["tests/**"]},
                "maintenance": {"editable_paths": ["tests/**"]},
                "requirements": {
                    "device": {"kind": "variable", "name": "device"}
                },
                "inventory": [],
                "features": {},
                "suites": {
                    "needs-device": {
                        "kind": "dynamic",
                        "level": "L2",
                        "requires": ["device"],
                        "commands": [
                            {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; "
                                    "Path('command-ran').write_text('yes')",
                                ]
                            }
                        ],
                    }
                },
            }
            plan_path = root / "test-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            report_dir = root / "report"
            with contextlib.redirect_stdout(io.StringIO()):
                result = testctl.command_run(
                    plan,
                    plan_path,
                    repo,
                    "needs-device",
                    [],
                    False,
                    str(report_dir),
                )

            self.assertEqual(testctl.EXIT_FAIL, result)
            self.assertFalse((repo / "command-ran").exists())
            report = json.loads(
                (report_dir / "result.json").read_text(encoding="utf-8")
            )
            self.assertEqual("FAIL", report["verdict"])
            suite = report["suites"][0]
            self.assertEqual("FAIL", suite["status"])
            self.assertEqual("UNMET_REQUIREMENT", suite["failure_type"])
            self.assertEqual(
                ["device: runtime variable missing: device"],
                suite["missing_requirements"],
            )


if __name__ == "__main__":
    unittest.main()
