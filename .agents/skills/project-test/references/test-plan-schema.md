# Test plan schema v1

`test-plan.yaml` is project-owned. The skill and runner are generic.

## Required top-level fields

```yaml
schema_version: 1
project:
  id: example
  repo_root: ..
suites: {}
```

`project.repo_root` is resolved relative to the plan file.

## Test assets and maintenance

```yaml
test_assets:
  protected_paths:
    - path/to/tests/**/*.py
    - path/to/test-plan.yaml

maintenance:
  editable_paths:
    - path/to/tests/**
```

Protected paths are hashed before and after execution. Editable paths are the maintenance-mode allowlist.

## Requirements

```yaml
requirements:
  adb:
    kind: binary
    value: adb
  device:
    kind: variable
    name: device
  fault_apk:
    kind: path_variable
    name: fault_apk
  android_home:
    kind: environment
    name: ANDROID_HOME
  schema_file:
    kind: path
    value: project/schema.json
```

Supported kinds are `binary`, `variable`, `path_variable`, `environment`, and `path`.
If any requirement of a selected suite is unavailable, that suite and the overall run are `FAIL`; its commands are not executed and the report retains the exact `missing_requirements` evidence.

## Suites

A leaf suite contains commands:

```yaml
suites:
  lint:
    kind: static
    level: L0
    requires: []
    commands:
      - cwd: project
        argv: ["{python}", -m, ruff, check, src, tests]
        timeout_seconds: 120
        fail_on_skip: false
```

`kind` is `static` or `dynamic`. Commands are argv arrays and never shell strings. Available substitutions include `{python}`, `{repo_root}`, `{plan_dir}`, plan defaults from `execution.variables`, and `--var key=value` overrides.

A composite suite contains only `includes`:

```yaml
  baseline:
    kind: composite
    includes: [lint, core-unit]
```

## Features

```yaml
features:
  anr:
    description: ANR capture and analysis
    paths:
      - project/src/anr/**
      - project/tests/test_anr.py
    suites: [host-anr, device-anr]
```

Paths define feature-scoped maintenance. Suites define `run feature:anr`.

## Inventory

```yaml
inventory:
  - id: host-pytest
    cwd: project
    argv: ["{python}", -m, pytest, --collect-only, -q, tests]
    parser: pytest_collect
    expected_count: 100
    explicit_suite: device-full
    timeout_seconds: 120
```

`inventory --check` fails when actual and expected counts differ. When `explicit_suite` is set, every collected node ID must appear exactly once in that suite's expanded command selectors; unassigned, unknown, and duplicate selectors fail the check. Supported parsers are `pytest_collect` and `nonempty_lines`.

## Execution defaults

```yaml
execution:
  default_target: baseline
  variables:
    fault_apk: path/to/default.apk
```
