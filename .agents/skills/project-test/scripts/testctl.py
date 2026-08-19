#!/usr/bin/env python3
"""Deterministic helper for project-test skill plans.

The helper deliberately does not edit test assets. Maintenance uses scope
to freeze its input; execution uses run to enforce read-only snapshots.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_STALE = 4


class TestCtlError(RuntimeError):
    pass


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise TestCtlError(
                "PyYAML is required for YAML plans; install PyYAML or use JSON"
            ) from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise TestCtlError("test plan root must be a mapping")
    return data


def load_plan(plan_path: str) -> tuple[dict[str, Any], Path, Path]:
    path = Path(plan_path).expanduser().resolve()
    if not path.is_file():
        raise TestCtlError(f"test plan not found: {path}")
    plan = _load_yaml_or_json(path)
    project = plan.get("project") or {}
    repo_root = (path.parent / str(project.get("repo_root", "."))).resolve()
    if not (repo_root / ".git").exists():
        raise TestCtlError(f"project.repo_root is not a Git repository: {repo_root}")
    return plan, path, repo_root


def _command_spec(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        return {"argv": raw}
    if isinstance(raw, dict):
        return raw
    raise TestCtlError("command must be an argv list or a mapping")


def validate_plan(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if plan.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    project = plan.get("project")
    if not isinstance(project, dict) or not project.get("id"):
        issues.append("project.id is required")

    suites = plan.get("suites")
    if not isinstance(suites, dict) or not suites:
        issues.append("suites must be a non-empty mapping")
        suites = {}
    requirements = plan.get("requirements") or {}
    if not isinstance(requirements, dict):
        issues.append("requirements must be a mapping")
        requirements = {}

    for name, suite in suites.items():
        if not isinstance(suite, dict):
            issues.append(f"suite {name!r} must be a mapping")
            continue
        kind = suite.get("kind")
        if kind not in {"static", "dynamic", "composite"}:
            issues.append(f"suite {name!r} has invalid kind {kind!r}")
        if kind == "composite":
            includes = suite.get("includes")
            if not isinstance(includes, list) or not includes:
                issues.append(f"composite suite {name!r} needs includes")
            else:
                for child in includes:
                    if child not in suites:
                        issues.append(f"suite {name!r} includes unknown suite {child!r}")
            if suite.get("commands"):
                issues.append(f"composite suite {name!r} must not define commands")
        else:
            commands = suite.get("commands")
            if not isinstance(commands, list) or not commands:
                issues.append(f"leaf suite {name!r} needs commands")
            else:
                for index, raw in enumerate(commands):
                    try:
                        command = _command_spec(raw)
                    except TestCtlError as exc:
                        issues.append(f"suite {name!r} command {index}: {exc}")
                        continue
                    argv = command.get("argv")
                    if not isinstance(argv, list) or not argv or not all(
                        isinstance(item, (str, int, float)) for item in argv
                    ):
                        issues.append(
                            f"suite {name!r} command {index} needs a non-empty argv list"
                        )
            for requirement in suite.get("requires") or []:
                if requirement not in requirements:
                    issues.append(
                        f"suite {name!r} references unknown requirement {requirement!r}"
                    )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            issues.append(f"suite include cycle detected at {name!r}")
            return
        if name in visited or name not in suites:
            return
        visiting.add(name)
        suite = suites[name]
        if isinstance(suite, dict):
            for child in suite.get("includes") or []:
                visit(str(child))
        visiting.remove(name)
        visited.add(name)

    for suite_name in suites:
        visit(str(suite_name))

    features = plan.get("features") or {}
    if not isinstance(features, dict):
        issues.append("features must be a mapping")
    else:
        for name, feature in features.items():
            if not isinstance(feature, dict):
                issues.append(f"feature {name!r} must be a mapping")
                continue
            for suite_name in feature.get("suites") or []:
                if suite_name not in suites:
                    issues.append(
                        f"feature {name!r} references unknown suite {suite_name!r}"
                    )

    for name, requirement in requirements.items():
        if not isinstance(requirement, dict):
            issues.append(f"requirement {name!r} must be a mapping")
            continue
        if requirement.get("kind") not in {
            "binary",
            "variable",
            "path_variable",
            "environment",
            "path",
        }:
            issues.append(f"requirement {name!r} has unsupported kind")

    inventory = plan.get("inventory") or []
    if not isinstance(inventory, list):
        issues.append("inventory must be a list")
    else:
        seen_inventory: set[str] = set()
        for index, item in enumerate(inventory):
            if not isinstance(item, dict):
                issues.append(f"inventory item {index} must be a mapping")
                continue
            item_id = item.get("id")
            if not item_id:
                issues.append(f"inventory item {index} needs id")
            elif item_id in seen_inventory:
                issues.append(f"duplicate inventory id {item_id!r}")
            else:
                seen_inventory.add(str(item_id))
            if not isinstance(item.get("argv"), list):
                issues.append(f"inventory {item_id!r} needs argv")
            if item.get("parser") not in {"pytest_collect", "nonempty_lines"}:
                issues.append(f"inventory {item_id!r} has unsupported parser")
            explicit_suite = item.get("explicit_suite")
            if explicit_suite and explicit_suite not in suites:
                issues.append(
                    f"inventory {item_id!r} references unknown explicit_suite "
                    f"{explicit_suite!r}"
                )

    protected = ((plan.get("test_assets") or {}).get("protected_paths") or [])
    editable = ((plan.get("maintenance") or {}).get("editable_paths") or [])
    if not isinstance(protected, list) or not protected:
        issues.append("test_assets.protected_paths must be a non-empty list")
    if not isinstance(editable, list) or not editable:
        issues.append("maintenance.editable_paths must be a non-empty list")
    return issues


def _git(repo: Path, args: list[str], *, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=text, check=False
    )
    if result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise TestCtlError(f"git {' '.join(args)} failed: {stderr.strip()}")
    return result.stdout


def _untracked(repo: Path) -> list[str]:
    raw = _git(repo, ["ls-files", "--others", "--exclude-standard", "-z"])
    assert isinstance(raw, bytes)
    return [
        part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part
    ]


def _tracked(repo: Path) -> list[str]:
    raw = _git(repo, ["ls-files", "-z"])
    assert isinstance(raw, bytes)
    return [
        part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part
    ]


def _changed(repo: Path, base: str) -> list[dict[str, str]]:
    raw = _git(repo, ["diff", "--name-status", "-z", base, "--"])
    assert isinstance(raw, bytes)
    tokens = [
        part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part
    ]
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        kind = status[:1]
        if kind in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise TestCtlError("unexpected truncated git rename/copy output")
            old_path, path = tokens[index], tokens[index + 1]
            index += 2
            changes.append({"status": status, "old_path": old_path, "path": path})
        else:
            if index >= len(tokens):
                raise TestCtlError("unexpected truncated git diff output")
            changes.append({"status": status, "path": tokens[index]})
            index += 1
    known = {item["path"] for item in changes}
    for path in _untracked(repo):
        if path not in known:
            changes.append({"status": "?", "path": path})
    return changes


def _matches(path: str, patterns: list[str]) -> bool:
    normalized = path.replace(os.sep, "/")
    for pattern in patterns:
        variants = {pattern}
        candidate = pattern
        while "/**/" in candidate:
            candidate = candidate.replace("/**/", "/", 1)
            variants.add(candidate)
        if any(fnmatch.fnmatchcase(normalized, variant) for variant in variants):
            return True
    return False


def _digest_files(repo: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(set(paths)):
        digest.update(relative.encode("utf-8", "surrogateescape"))
        path = repo / relative
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _scope_digest(repo: Path, changes: list[dict[str, str]]) -> str:
    """Hash both Git change metadata and the selected working-tree content."""
    digest = hashlib.sha256()
    digest.update(
        json.dumps(changes, sort_keys=True, separators=(",", ":")).encode(
            "utf-8", "surrogateescape"
        )
    )
    paths: set[str] = set()
    for change in changes:
        for key in ("path", "old_path"):
            if change.get(key):
                paths.add(change[key])
    digest.update(_digest_files(repo, sorted(paths)).encode("ascii"))
    return digest.hexdigest()


def _expand_patterns(repo: Path, patterns: list[str]) -> list[str]:
    candidates = set(_tracked(repo) + _untracked(repo))
    return sorted(path for path in candidates if _matches(path, patterns))


def _working_tree_digest(repo: Path) -> str:
    diff = _git(repo, ["diff", "--binary", "HEAD", "--"])
    assert isinstance(diff, bytes)
    digest = hashlib.sha256(diff)
    for path in sorted(_untracked(repo)):
        digest.update(path.encode("utf-8", "surrogateescape"))
        candidate = repo / path
        if candidate.is_file():
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _variables(plan: dict[str, Any], repo: Path, plan_path: Path) -> dict[str, str]:
    result = {
        "python": sys.executable,
        "repo_root": str(repo),
        "plan_dir": str(plan_path.parent),
    }
    defaults = ((plan.get("execution") or {}).get("variables") or {})
    result.update({str(key): str(value) for key, value in defaults.items()})
    return result


class _StrictFormat(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise TestCtlError(f"missing command variable: {key}")


def _expand(value: Any, variables: dict[str, str]) -> str:
    return str(value).format_map(_StrictFormat(variables))


def _resolve_path(repo: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo / path


def _requirement_problem(
    definition: dict[str, Any], variables: dict[str, str], repo: Path
) -> str | None:
    kind = definition.get("kind")
    name = str(definition.get("name") or definition.get("value") or "")
    if kind == "binary":
        return None if name and shutil.which(name) else f"binary not found: {name}"
    if kind == "environment":
        return (
            None
            if name and os.environ.get(name)
            else f"environment variable missing: {name}"
        )
    if kind == "variable":
        return None if name and variables.get(name) else f"runtime variable missing: {name}"
    if kind == "path_variable":
        value = variables.get(name)
        if not value:
            return f"runtime path variable missing: {name}"
        path = _resolve_path(repo, value)
        return None if path.exists() else f"runtime path does not exist: {path}"
    if kind == "path":
        path = _resolve_path(repo, str(definition.get("value") or ""))
        return None if path.exists() else f"required path does not exist: {path}"
    return f"unsupported requirement kind: {kind}"


def resolve_target(plan: dict[str, Any], target: str) -> list[str]:
    suites = plan.get("suites") or {}
    if target.startswith("feature:"):
        feature_name = target.split(":", 1)[1]
        feature = (plan.get("features") or {}).get(feature_name)
        if not feature:
            raise TestCtlError(f"unknown feature: {feature_name}")
        roots = list(feature.get("suites") or [])
    else:
        if target not in suites:
            raise TestCtlError(f"unknown suite: {target}")
        roots = [target]

    resolved: list[str] = []

    def add(name: str, stack: tuple[str, ...] = ()) -> None:
        if name in stack:
            raise TestCtlError(f"suite include cycle: {' -> '.join((*stack, name))}")
        suite = suites[name]
        if suite.get("kind") == "composite":
            for child in suite.get("includes") or []:
                add(str(child), (*stack, name))
        elif name not in resolved:
            resolved.append(name)

    for root in roots:
        add(str(root))
    return resolved


def command_validate(plan: dict[str, Any]) -> int:
    issues = validate_plan(plan)
    print(
        json.dumps(
            {"status": "PASS" if not issues else "FAIL", "issues": issues}, indent=2
        )
    )
    return 0 if not issues else EXIT_CONFIG


def command_list(plan: dict[str, Any]) -> int:
    suites = {
        name: {
            "kind": suite.get("kind"),
            "level": suite.get("level"),
            "description": suite.get("description", ""),
            "includes": suite.get("includes", []),
        }
        for name, suite in (plan.get("suites") or {}).items()
    }
    features = {
        name: {
            "description": feature.get("description", ""),
            "suites": feature.get("suites", []),
        }
        for name, feature in (plan.get("features") or {}).items()
    }
    print(json.dumps({"suites": suites, "features": features}, indent=2))
    return 0


def command_scope(
    plan: dict[str, Any],
    repo: Path,
    *,
    all_files: bool,
    feature: str | None,
    base: str | None,
) -> int:
    protected = [
        str(item)
        for item in ((plan.get("test_assets") or {}).get("protected_paths") or [])
    ]
    editable = [
        str(item)
        for item in ((plan.get("maintenance") or {}).get("editable_paths") or [])
    ]
    mode: str
    changes: list[dict[str, str]]
    if all_files:
        mode = "all"
        changes = [
            {"status": "tracked", "path": path}
            for path in sorted(set(_tracked(repo) + _untracked(repo)))
        ]
    elif feature:
        mode = "feature"
        definition = (plan.get("features") or {}).get(feature)
        if not definition:
            raise TestCtlError(f"unknown feature: {feature}")
        patterns = [str(item) for item in definition.get("paths") or []]
        if not patterns:
            raise TestCtlError(f"feature {feature!r} has no maintenance paths")
        changes = [
            {"status": "selected", "path": path}
            for path in _expand_patterns(repo, patterns)
        ]
    else:
        mode = "base" if base else "working-tree"
        changes = _changed(repo, base or "HEAD")

    test_changes = [
        item for item in changes if _matches(item["path"], protected)
    ]
    subject_changes = [
        item for item in changes if not _matches(item["path"], protected)
    ]
    result = {
        "status": "NO_CHANGES" if not changes else "READY",
        "mode": mode,
        "base": base or ("HEAD" if mode == "working-tree" else None),
        "feature": feature,
        "input_digest": _scope_digest(repo, changes),
        "maintenance_editable_paths": editable,
        "changes": changes,
        "subject_changes": subject_changes,
        "existing_test_changes": test_changes,
    }
    print(json.dumps(result, indent=2))
    return 0


def _run_process(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: float
) -> tuple[int, str, float, bool]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output, time.monotonic() - started, False
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode()
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode()
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return 124, stdout + stderr, time.monotonic() - started, True


def _parse_inventory(parser: str, output: str) -> tuple[int, list[str]]:
    if parser == "pytest_collect":
        matches = re.findall(r"(\d+)\s+tests?\s+collected", output)
        if not matches:
            raise TestCtlError("could not find pytest collected count")
        nodeids = [
            line.strip()
            for line in output.splitlines()
            if "::" in line and not line.startswith((" ", "="))
        ]
        return int(matches[-1]), nodeids
    if parser == "nonempty_lines":
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return len(lines), lines
    raise TestCtlError(f"unsupported inventory parser: {parser}")


def _explicit_nodeids(plan: dict[str, Any], suite_name: str) -> list[str]:
    nodeids: list[str] = []
    for leaf_name in resolve_target(plan, suite_name):
        suite = plan["suites"][leaf_name]
        for raw_command in suite.get("commands") or []:
            command = _command_spec(raw_command)
            nodeids.extend(str(value) for value in command["argv"] if "::" in str(value))
    return nodeids


def command_inventory(
    plan: dict[str, Any], plan_path: Path, repo: Path, *, check: bool
) -> int:
    variables = _variables(plan, repo, plan_path)
    records: list[dict[str, Any]] = []
    failed = False
    for item in plan.get("inventory") or []:
        argv = [_expand(value, variables) for value in item["argv"]]
        cwd = _resolve_path(repo, _expand(item.get("cwd", "."), variables))
        timeout = float(item.get("timeout_seconds", 120))
        returncode, output, duration, timed_out = _run_process(
            argv, cwd, os.environ.copy(), timeout
        )
        record: dict[str, Any] = {
            "id": item["id"],
            "returncode": returncode,
            "duration_seconds": round(duration, 3),
            "timed_out": timed_out,
        }
        if returncode == 0 and not timed_out:
            try:
                actual, nodeids = _parse_inventory(str(item["parser"]), output)
                expected = item.get("expected_count")
                record.update(
                    {"actual_count": actual, "expected_count": expected}
                )
                if check and expected is not None and actual != int(expected):
                    record["status"] = "DRIFT"
                    failed = True
                else:
                    record["status"] = "PASS"
                explicit_suite = item.get("explicit_suite")
                if explicit_suite:
                    selected = _explicit_nodeids(plan, str(explicit_suite))
                    counts = Counter(selected)
                    collected_set = set(nodeids)
                    selected_set = set(selected)
                    unassigned = sorted(collected_set - selected_set)
                    unknown = sorted(selected_set - collected_set)
                    duplicates = sorted(
                        nodeid for nodeid, count in counts.items() if count > 1
                    )
                    record["explicit_suite"] = explicit_suite
                    record["mapping"] = {
                        "selected_count": len(selected),
                        "unassigned": unassigned,
                        "unknown": unknown,
                        "duplicates": duplicates,
                    }
                    if check and (unassigned or unknown or duplicates):
                        record["status"] = "DRIFT"
                        failed = True
            except TestCtlError as exc:
                record.update({"status": "FAIL", "error": str(exc)})
                failed = True
        else:
            record.update(
                {
                    "status": "FAIL",
                    "output_tail": "\n".join(output.splitlines()[-20:]),
                }
            )
            failed = True
        records.append(record)
    total = sum(int(item.get("actual_count", 0)) for item in records)
    print(
        json.dumps(
            {
                "status": "FAIL" if failed else "PASS",
                "total": total,
                "inventory": records,
            },
            indent=2,
        )
    )
    return EXIT_FAIL if failed else 0


def command_run(
    plan: dict[str, Any],
    plan_path: Path,
    repo: Path,
    target: str,
    runtime_vars: list[str],
    dry_run: bool,
    report_dir: str | None,
) -> int:
    issues = validate_plan(plan)
    if issues:
        raise TestCtlError("invalid test plan: " + "; ".join(issues))
    variables = _variables(plan, repo, plan_path)
    for raw in runtime_vars:
        if "=" not in raw:
            raise TestCtlError(f"--var must be key=value: {raw}")
        key, value = raw.split("=", 1)
        if not key:
            raise TestCtlError("--var key must not be empty")
        variables[key] = value

    for definition in (plan.get("requirements") or {}).values():
        if definition.get("kind") != "path_variable":
            continue
        name = str(definition.get("name") or "")
        if name and variables.get(name):
            variables[name] = str(_resolve_path(repo, variables[name]).resolve())

    selected = resolve_target(plan, target)
    project_id = str((plan.get("project") or {}).get("id", "project"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = (
        Path(report_dir).expanduser().resolve()
        if report_dir
        else Path(tempfile.gettempdir()) / "project-test" / project_id / stamp
    )
    root.mkdir(parents=True, exist_ok=True)

    protected_patterns = [
        str(item)
        for item in ((plan.get("test_assets") or {}).get("protected_paths") or [])
    ]
    protected_paths = _expand_patterns(repo, protected_patterns)
    code_before = _working_tree_digest(repo)
    tests_before = _digest_files(repo, protected_paths)
    plan_digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()

    definitions = plan.get("requirements") or {}
    records: list[dict[str, Any]] = []
    has_failure = False
    command_number = 0

    for suite_name in selected:
        suite = plan["suites"][suite_name]
        missing: list[str] = []
        for requirement_name in suite.get("requires") or []:
            problem = _requirement_problem(
                definitions[requirement_name], variables, repo
            )
            if problem:
                missing.append(f"{requirement_name}: {problem}")
        suite_record: dict[str, Any] = {
            "suite": suite_name,
            "kind": suite.get("kind"),
            "level": suite.get("level"),
            "status": "PLANNED" if dry_run else "PASS",
            "missing_requirements": missing,
            "commands": [],
        }
        if missing:
            suite_record["status"] = "FAIL"
            suite_record["failure_type"] = "UNMET_REQUIREMENT"
            has_failure = True
            records.append(suite_record)
            continue

        for raw_command in suite.get("commands") or []:
            command_number += 1
            command = _command_spec(raw_command)
            argv = [_expand(value, variables) for value in command["argv"]]
            cwd = _resolve_path(
                repo, _expand(command.get("cwd", "."), variables)
            )
            timeout = float(
                command.get(
                    "timeout_seconds", suite.get("timeout_seconds", 300)
                )
            )
            env = os.environ.copy()
            for key, value in (command.get("env") or {}).items():
                env[str(key)] = _expand(value, variables)
            log_path = root / f"{command_number:02d}-{suite_name}.log"
            command_record: dict[str, Any] = {
                "argv": argv,
                "cwd": str(cwd),
                "timeout_seconds": timeout,
                "log": str(log_path),
            }
            if dry_run:
                command_record["status"] = "PLANNED"
                suite_record["commands"].append(command_record)
                continue
            returncode, output, duration, timed_out = _run_process(
                argv, cwd, env, timeout
            )
            log_path.write_text(output, encoding="utf-8")
            skip_matches = [
                int(value) for value in re.findall(r"(\d+)\s+skipped", output)
            ]
            skipped = sum(skip_matches)
            expected = [
                int(value) for value in command.get("expected_exit_codes", [0])
            ]
            failed_skip = bool(
                command.get("fail_on_skip", False) and skipped
            )
            passed = returncode in expected and not timed_out and not failed_skip
            command_record.update(
                {
                    "status": "PASS" if passed else "FAIL",
                    "returncode": returncode,
                    "duration_seconds": round(duration, 3),
                    "timed_out": timed_out,
                    "skipped": skipped,
                    "output_tail": (
                        "\n".join(output.splitlines()[-20:]) if not passed else ""
                    ),
                }
            )
            suite_record["commands"].append(command_record)
            if not passed:
                suite_record["status"] = "FAIL"
                has_failure = True
                break
        records.append(suite_record)

    code_after = _working_tree_digest(repo)
    protected_after_paths = _expand_patterns(repo, protected_patterns)
    tests_after = _digest_files(repo, protected_after_paths)
    stale_reasons: list[str] = []
    if code_before != code_after:
        stale_reasons.append("working tree changed during execution")
    if tests_before != tests_after or protected_paths != protected_after_paths:
        stale_reasons.append("protected test assets changed during execution")

    if dry_run:
        verdict = "PLANNED"
    elif stale_reasons:
        verdict = "STALE"
    elif has_failure:
        verdict = "FAIL"
    else:
        verdict = "PASS"

    report = {
        "schema_version": 1,
        "project": project_id,
        "target": target,
        "expanded_suites": selected,
        "verdict": verdict,
        "dry_run": dry_run,
        "started_snapshot": {
            "working_tree_digest": code_before,
            "test_asset_digest": tests_before,
            "test_plan_digest": plan_digest,
        },
        "finished_snapshot": {
            "working_tree_digest": code_after,
            "test_asset_digest": tests_after,
        },
        "stale_reasons": stale_reasons,
        "suites": records,
        "artifact_dir": str(root),
    }
    report_path = root / "result.json"
    report_path.write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "verdict": verdict,
                "target": target,
                "expanded_suites": selected,
                "artifact_dir": str(root),
                "report": str(report_path),
                "stale_reasons": stale_reasons,
            },
            indent=2,
        )
    )
    return {
        "PASS": 0,
        "PLANNED": 0,
        "FAIL": EXIT_FAIL,
        "STALE": EXIT_STALE,
    }[verdict]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain and execute project test plans"
    )
    parser.add_argument(
        "--config", required=True, help="Path to test-plan.yaml or JSON"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the test plan")
    subparsers.add_parser("list", help="List suites and features")

    scope = subparsers.add_parser("scope", help="Freeze a maintenance scope")
    selection = scope.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", dest="all_files")
    selection.add_argument("--feature")
    selection.add_argument("--base")

    inventory = subparsers.add_parser(
        "inventory", help="Collect test inventory"
    )
    inventory.add_argument(
        "--check", action="store_true", help="Fail on expected-count drift"
    )

    run = subparsers.add_parser("run", help="Execute a suite or feature")
    run.add_argument("target", nargs="?", help="Suite id or feature:<name>")
    run.add_argument(
        "--var", action="append", default=[], help="Runtime key=value"
    )
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--report-dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan, plan_path, repo = load_plan(args.config)
        if args.command == "validate":
            return command_validate(plan)
        if args.command == "list":
            return command_list(plan)
        if args.command == "scope":
            return command_scope(
                plan,
                repo,
                all_files=args.all_files,
                feature=args.feature,
                base=args.base,
            )
        if args.command == "inventory":
            return command_inventory(
                plan, plan_path, repo, check=args.check
            )
        if args.command == "run":
            target = args.target or str(
                (plan.get("execution") or {}).get("default_target") or ""
            )
            if not target:
                raise TestCtlError(
                    "no target supplied and execution.default_target is unset"
                )
            return command_run(
                plan,
                plan_path,
                repo,
                target,
                args.var,
                args.dry_run,
                args.report_dir,
            )
        raise TestCtlError(f"unknown command: {args.command}")
    except (
        TestCtlError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps({"status": "ERROR", "error": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
