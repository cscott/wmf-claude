---
description: Run PHPUnit tests for MediaWiki core or an extension. Use when the user wants to verify that a change passes tests, run a specific test file, or run an extension's full test suite.
disable-model-invocation: false
argument-hint: "[path-or-extension]"
allowed-tools:
  - Bash(vendor/bin/phpunit *)
  - Bash(TMPDIR=* vendor/bin/phpunit *)
  - Bash(MW_SKIP_EXTERNAL_DEPENDENCIES=* vendor/bin/phpunit *)
  - Bash(TMPDIR=* MW_SKIP_EXTERNAL_DEPENDENCIES=* vendor/bin/phpunit *)
---

# Run PHPUnit Tests

Run PHPUnit tests for MediaWiki core or an extension from the project's MediaWiki root.

## Steps

1. **Locate the MediaWiki root.** Run from the project's MW root (where `vendor/bin/phpunit` lives). If you're not sure, `git rev-parse --show-toplevel` finds the repo root; it's typically the same as the MW root for core, or the parent of an extension's checkout.

2. **Parse `$ARGUMENTS` to determine the target:**
   - If it looks like an extension name (e.g. `CentralAuth`): `vendor/bin/phpunit extensions/{ext}/tests/phpunit`
   - If it's a path (e.g. `tests/phpunit/unit/includes/...` or `extensions/Foo/tests/...`): pass it directly
   - If it's a single test file: pass it directly
   - If `$ARGUMENTS` is empty: ask the user what to run

3. **Choose environment flags as needed:**
   - `MW_SKIP_EXTERNAL_DEPENDENCIES=1` — skips composer/external checks; useful for fast iteration on integration tests.
   - `TMPDIR=<writable-dir>` — only set this if the default tmp path isn't writable inside the sandbox or PHPUnit complains about it. Check the project's `CLAUDE.md` for a recommended value (some setups need it; many don't).

4. **Run the command.** Examples:
   ```bash
   # Unit test for a specific class
   vendor/bin/phpunit tests/phpunit/unit/includes/Foo/BarTest.php

   # All tests in an extension (integration-friendly env)
   MW_SKIP_EXTERNAL_DEPENDENCIES=1 vendor/bin/phpunit extensions/CentralAuth/tests/phpunit
   ```

5. **Report results.** Surface test counts, failures, and the first few error lines. Don't paraphrase — quote PHPUnit's actual output for the failing assertions.

## Notes

- Some test failures are caused by side effects from deferred updates leaking between tests. If a single test passes in isolation but fails as part of a suite, that's likely it.
- Run only the relevant tests first before broader suites — extension test suites can take many minutes.
- For containerized setups (MediaWiki-Docker, MWDD, vagrant), the test command typically runs through the container's exec wrapper. Check the project's `CLAUDE.md` for the actual invocation if `vendor/bin/phpunit` doesn't work directly.

## Input

`$ARGUMENTS`
