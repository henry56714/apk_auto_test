# Maintain project tests

Use this mode only when the user explicitly asks to create, update, audit, or organize the project test collection. Do not modify product code and do not turn maintenance validation into product acceptance.

## Select the maintenance scope

Apply this precedence:

1. Whole project: `scope --all`.
2. Named feature: `scope --feature <name>`.
3. Explicit comparison base: `scope --base <git-ref>`.
4. No explicit scope: `scope`, which snapshots `HEAD` to the current working tree plus untracked non-ignored files.

Run the scope command before editing. Treat its `input_digest`, file list, and classification as immutable input for the current maintenance pass. Files created by this pass do not recursively enlarge that input.

If the default scope is empty, return `NO_CHANGES`. Do not silently expand to the whole project.

## Maintain the collection

1. Read the selected behavior, relevant product specifications, existing tests, fixtures, test applications, and the project plan.
2. Classify existing test changes as assets to review, not as product behavior that recursively requires more tests.
3. Follow dependencies far enough to cover the changed or named behavior, while avoiding unrelated whole-project review unless `--all` was selected.
4. Maintain both applicable categories:
   - Static: lint, unit tests, pure logic, fake-host/ADB integration, schemas, configuration, packaging, and code analysis.
   - Dynamic: build/install/launch, Java/Kotlin/JNI interactions, ADB actions, process behavior, structured observations, device health, and cleanup.
5. Dynamic scenarios must have arrange, act, observe, assert, cleanup, timeout, and environment requirements. Prefer external observable behavior over implementation-only assertions.
6. Update suite membership, feature mappings, expected inventory counts, and requirement metadata when test assets change.
7. Do not weaken or delete an existing assertion merely to accommodate current product behavior. When a requirement changed, record the old requirement, new requirement, and why the test change is legitimate.
8. Use `validate` and `inventory --check`. Run only the focused checks needed to prove new or changed test assets are collectable and behave deterministically. Label those runs `MAINTENANCE_SELF_CHECK`.

## Editable boundary

The plan's `maintenance.editable_paths` is the allowlist for this mode. If a necessary test change lies outside it, report the path and request an explicit scope decision. Never edit a product file as a convenience for making a test pass.

## Maintenance result

Report:

- scope mode, base/ref/feature, and input digest;
- subject changes and pre-existing test changes;
- tests, fixtures, test apps, and plan entries added/updated/removed;
- static and dynamic coverage affected;
- inventory before/after;
- maintenance self-checks and their limitations;
- unresolved gaps or environment needs;
- an explicit reminder that product acceptance has not run.
