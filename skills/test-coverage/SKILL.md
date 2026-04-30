---
description: Run PHPUnit with code coverage for an extension or core component. Use when the user wants to know coverage levels or check that a patch hasn't dropped coverage.
disable-model-invocation: false
argument-hint: "[extension-name]"
allowed-tools:
  - Bash(composer *)
  - Bash(vendor/bin/phpunit *)
  - Bash(TMPDIR=* vendor/bin/phpunit *)
  - Bash(MW_SKIP_EXTERNAL_DEPENDENCIES=* vendor/bin/phpunit *)
  - Bash(TMPDIR=* MW_SKIP_EXTERNAL_DEPENDENCIES=* vendor/bin/phpunit *)
---

# Check Test Coverage

Run PHPUnit with code coverage for an extension or core component.

## Steps

1. **Configure coverage scope.** Run `composer phpunit:coverage-edit -- extensions/{ext}` to update `phpunit.xml` so only files under the target extension are instrumented. This makes the coverage run dramatically faster than instrumenting all of MediaWiki.
2. **Run PHPUnit with `--coverage-text`.** Typical invocation:
   ```bash
   vendor/bin/phpunit --coverage-text extensions/{ext}/tests/phpunit
   ```
   Prefix with `TMPDIR=<writable-dir>` only if the default tmp path doesn't work inside the sandbox (check `CLAUDE.md`).
3. **Report the summary.** Line coverage, method coverage, class coverage. Quote the relevant chunk of PHPUnit's output rather than paraphrasing.

## Notes

- The `composer phpunit:coverage-edit` step modifies `phpunit.xml` in place; remind the user not to commit those changes.
- Coverage levels should not drop as part of a patch — flag if they do.
- Sometimes a coverage drop is expected (e.g. removing dead code that had tests; new code that's hard to test, with rationale in the patch).

## Input

`$ARGUMENTS`
