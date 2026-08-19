---
name: project-test
description: Maintain, audit, inventory, execute, and judge project-owned static and dynamic test suites. Use when asked to maintain a whole test collection, maintain tests for one feature or the current uncommitted working tree, list project tests, or run an existing named test suite. Do not use for writing product code or automatically coupling test maintenance to code edits.
---

# Project Test

Manage project test assets and execute them without coupling either workflow to product-code authoring.

## Choose exactly one mode

- **Maintain tests:** Read [references/maintain-tests.md](references/maintain-tests.md). This mode may edit only project-declared test assets. It is independently invoked and never starts merely because code changed.
- **Execute tests:** Read [references/execute-tests.md](references/execute-tests.md). This mode treats product code, tests, the plan, and this skill as read-only. It may write only logs and result artifacts outside those paths.
- **Inspect or validate:** Use `testctl.py validate`, `list`, `inventory`, or `scope` directly. Read [references/test-plan-schema.md](references/test-plan-schema.md) when plan fields or validation errors need interpretation.

If a request mixes maintenance and execution, complete them as two explicit phases. A maintenance self-check is evidence about the test asset itself, not acceptance of the product code. Start the later execution phase from a fresh code/test snapshot and report it separately.

## Discover the project plan

Use a user-specified plan when provided. Otherwise search upward from the working directory, then the repository, for `test-plan.yaml` or `test-plan.yml`. If multiple plans could apply, select the one whose `project.repo_root` contains the requested project; do not merge plans.

Resolve this skill directory from the loaded `SKILL.md`, then invoke:

```bash
python <skill-dir>/scripts/testctl.py --config <test-plan.yaml> <command>
```

## Invariants

1. Product-code authoring, test maintenance, and test execution are independent workflows.
2. Maintenance defaults to the current uncommitted working tree only when no whole-project, feature, or base scope was explicitly supplied. Include staged, unstaged, and untracked non-ignored files.
3. Execution never edits tests, fixtures, test apps, the test plan, this skill, or product code. Report a gap; do not repair it in execution mode.
4. Static and dynamic tests are first-class. Dynamic suites must declare environment requirements, observable assertions, timeouts, and cleanup ownership in the project plan or test implementation.
5. Deterministic assertions and exit codes are hard evidence. AI may downgrade a result to `STALE`, `TEST_GAP`, or `FAIL`, but must not reinterpret a hard failure as `PASS`.
6. A selected suite with a missing required device, APK, SDK, binary, environment variable, file, or runtime capability is `FAIL`. Preserve the unmet requirement as evidence and do not execute commands whose prerequisites are missing.
7. Keep full logs in artifacts and return a compact evidence summary with the exact suite, code snapshot, test-plan snapshot, counts, skips, failures, duration, and remaining risk.
