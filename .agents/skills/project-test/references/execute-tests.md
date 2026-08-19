# Execute project tests

Use this mode when the user asks to run, verify, or judge an already-maintained project test suite.

## Select a suite

Use the explicit suite or `feature:<name>` supplied by the user. If none is supplied, use `execution.default_target` from the plan. Do not infer a maintenance action from a code diff, and do not edit the collection when coverage looks incomplete.

Inspect available targets with:

```bash
python <skill-dir>/scripts/testctl.py --config <plan> list
```

Use `--dry-run` before costly dynamic or release suites. Supply runtime values with repeated `--var key=value`, for example device serial or APK path.

## Execute

```bash
python <skill-dir>/scripts/testctl.py --config <plan> run <target> \
  --var device=<adb-serial> \
  --var fault_apk=<apk-path>
```

The runner expands composite suites, checks requirements, executes argv without a shell, saves complete logs, and writes a structured report. It snapshots the working tree and protected test assets before and after execution.

Do not patch a failing test, change product code, update expected output, install missing system dependencies, or start a destructive device scenario unless the user has separately authorized that action. Environment preparation already declared by the selected suite is within the requested execution workflow.

## Judge

Use deterministic results as the floor:

- `PASS`: every required command passed, required suites did not skip, and snapshots stayed unchanged.
- `FAIL`: a command, assertion, timeout, cleanup, or hard gate failed, or a required device, APK, binary, SDK, variable, file, environment value, or capability was unavailable.
- `STALE`: code or protected test assets changed during execution.
- `TEST_GAP`: deterministic checks may pass, but AI finds that the maintained suite does not establish the requested behavior. Do not fix the gap in this mode.

When a requirement is missing, keep the exact details in `missing_requirements`, mark that suite and the overall run `FAIL`, skip its commands, and return the normal failure exit code. AI may downgrade `PASS` to `TEST_GAP` or `FAIL` with concrete evidence. AI must never upgrade `FAIL` or `STALE` to `PASS`.

## Execution result

Return a compact summary containing:

- target and expanded suites;
- code and test-plan/test-asset digests;
- static and dynamic results separately;
- passed, failed, skipped, and timed-out commands, plus suites that failed because requirements were unmet;
- device/API/ABI and APK identity when applicable;
- cleanup result;
- report/log location;
- overall verdict and residual risk.
