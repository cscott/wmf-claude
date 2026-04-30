<!-- The content below is appended to a project's CLAUDE.md by `/wmf-claude:init-project --mediawiki`. -->

## Available MediaWiki skills

| Skill | Purpose |
|-------|---------|
| `/wmf-claude:run-tests [path-or-extension]` | Run PHPUnit tests |
| `/wmf-claude:test-coverage [extension]` | PHPUnit with code coverage |
| `/wmf-claude:lint` | Detect changed file types, run appropriate linters |
| `/wmf-claude:manual-test [feature or url-path]` | Browser testing via an external browser-automation tool (you supply the local wiki URL and credentials) |
| `/wmf-claude:compare-rebase [base-commit]` | Show what changed in each commit after a rebase |

## MediaWiki agents

| Agent | When it's used |
|-------|----------------|
| `mediawiki-dev` | Writes and modifies MediaWiki PHP/JS/CSS code |
| `mediawiki-explore` | Explores the codebase to find code paths and patterns |
| `test-writer` | Writes PHPUnit tests for core or extensions |

## General guidance

Always try to keep the delta of what you change as small as possible, and reuse existing utility methods and services wherever possible. If refactoring is needed as part of a feature, put it in a separate commit ahead of the feature commit.

## Local settings

Never modify `LocalSettings.php` directly. Use `LocalSettings.claude.php` for temporary config changes — load it conditionally at the end of `LocalSettings.php`.

## Extensions and skins

Extensions and skins live at `extensions/{extensionName}` and `skins/{skinName}` — each is its own git repository. When running git commands for them, use `git -C extensions/{extensionName}` instead of `cd extensions/{extensionName} && git ...`. The `-C` flag avoids compound `cd && git` commands that trigger unnecessary approval prompts.

## Running tests

Use `/wmf-claude:run-tests [path-or-extension]` to run PHPUnit tests. Use `/wmf-claude:test-coverage [extension]` to check code coverage.

Key test suites:

- `tests/phpunit/unit/` — Pure unit tests (no DB, no globals, no services). Base class: `MediaWikiUnitTestCase`. Calling `MediaWikiServices::getInstance()` will throw.
- `tests/phpunit/includes/` and `tests/phpunit/integration/` — Integration tests with DB/services. Base class: `MediaWikiIntegrationTestCase`.

Direct invocation (run from MediaWiki root, no `MW_INSTALL_PATH=` prefix, no specific php binary):

- Unit: `vendor/bin/phpunit <test-file>`
- Integration: `MW_SKIP_EXTERNAL_DEPENDENCIES=1 vendor/bin/phpunit --bootstrap tests/phpunit/bootstrap.php <test-file>`
- Coverage: `composer phpunit:coverage-edit -- extensions/{ext}` then run PHPUnit with coverage

As part of writing a patch, check that code coverage levels do not drop. Sometimes it is OK for the coverage levels to drop when there's no easy way to test the code.

## Linting

Use `/wmf-claude:lint` to automatically detect changed file types and run the appropriate linters. Available linters:

- **PHP**: `composer phpcs` (CodeSniffer), `composer lint` (syntax check), `composer phan` (static analysis), `composer fix` (autofix)
- **JS/CSS**: `npm run lint` (ESLint + stylelint via grunt)
- **JSON**: `jq` or `python -m json.tool`

For extensions, run phpcs from MW root: `vendor/bin/phpcs --standard=MediaWiki extensions/{ext}/path/to/file.php`

Never write custom scripts to fix formatting or whitespace. Always use existing project formatters (`composer fix`, `phpcbf`, `npm run lint:fix`, `jq`, etc.). The Read tool may display tabs as spaces — do not treat this as a problem that needs fixing.

Always run phpcs on changed PHP files before handing back. Don't leave lint to CI.

## Code style

- MediaWiki uses tabs, not spaces.
- Avoid short variable names like `$u`. Prefer `$centralAuthUser` or `$user`.
- Always use return types and parameter type hints when writing new code.
- PHP `use` statements must be sorted alphabetically.
- When you reference a class for the first time in a PHP file, add the corresponding `use` import at the top — don't rely on linters to catch it.
- Max line length is 120 characters.
- Method names must be **lowerCamelCase** — no underscores (e.g. `getUserInfo` not `get_userInfo`). See [Manual:Coding_conventions/PHP](https://www.mediawiki.org/wiki/Manual:Coding_conventions/PHP).
- Closures that don't reference `$this` must be declared `static` (e.g. `static function () { ... }`). The `MediaWiki.Usage.StaticClosure` sniff enforces this.
- Never vertically align `=`, `=>`, or trailing comments by padding with spaces. One space around `=` and `=>`, one space before `//`. Applies to PHP, JS, JSON, YAML, shell — every language. PHPCS will flag aligned tab-indented blocks.
- JS top-of-file description comments use `/**`, not `/*!`. The `/*!` "preserve through minification" syntax is non-standard in MediaWiki.

## Dependency injection and services

`MediaWikiServices` is the global service locator. Services are defined as factory closures in `includes/ServiceWiring.php` and are lazy-initialized singletons.

The `ServiceOptions` pattern: each service class declares `public const CONSTRUCTOR_OPTIONS = ['ConfigKey1', ...]` and receives a `ServiceOptions` instance that extracts only needed config keys. Always call `$options->assertRequiredOptions(self::CONSTRUCTOR_OPTIONS)` in constructors.

Services must NOT capture request-level state (no `RequestContext`, no current user). Extensions add services via `$wgServiceWiringFiles` and can replace core services via the `MediaWikiServices` hook.

## REST API

Entry point: `rest.php`. Routes defined in `includes/Rest/coreRoutes.json`. Handlers extend `Handler` or `SimpleHandler` (which unpacks path params into `run()` arguments). The `services` array in route JSON definitions injects services via `ObjectFactory` — constructor parameters must match the order listed.

## Hook system

Hooks use an interface-based, type-safe system. Core calls hooks via `HookRunner`. Extensions register handlers in `extension.json`:

```json
{
    "HookHandlers": {
        "main": { "class": "MyHookHandler", "services": ["ReadOnlyMode"] }
    },
    "Hooks": { "SomeHookName": "main" }
}
```

The handler class implements `SomeHookNameHook` with method `onSomeHookName(...)`. Returning `false` aborts the hook chain.

## Autoloading

`autoload.php` is a generated class map — do not edit manually. Regenerate with `php maintenance/run.php generateLocalAutoload` after adding new core classes. Extensions register classes via `AutoloadClasses` or `AutoloadNamespaces` (PSR-4) in `extension.json`.

## Code search

[codesearch.wmcloud.org](https://codesearch.wmcloud.org/search/) is excellent for finding examples across the MediaWiki ecosystem.

The frontend requires JavaScript, so for programmatic searches use the **backend Hound API**:
`https://codesearch-backend.wmcloud.org/search/api/v1/search?q={query}&repos=*`
Returns JSON with `Results` keyed by repo name, each containing `FileMatches`.

## Fetching from the web

Prefer `curl` (via Bash) over `WebFetch` for Wikimedia sites — WebFetch frequently gets 403'd. For non-Wikimedia sites, try WebFetch first and fall back to `curl` if it fails. Pipe curl JSON output to `jq`, not `python3 -m json.tool`.

## Gerrit and git

Never push patches to Gerrit. Just make commits on the feature branch for the relevant task.

Never commit on master/main, always switch to a feature branch.

The `gerrit` MCP server is available for interacting with Wikimedia Gerrit (gerrit.wikimedia.org). Use `/wmf-claude:review-patch [change-id]` for a full code review of a Gerrit change. When the user refers to a "patch", this means a Gerrit change.

Never post review comments on Gerrit — the user handles all replies.

When making changes to an existing patch, stage your changes (`git add`) but do NOT commit until the user has approved. For multi-patch stacks in Gerrit, use the same approach: patch 1 on branch-1 with staged changes, patch 2 on branch-2 with staged changes — all staged but uncommitted. This lets the user review exactly what will change before pushing to Gerrit.

When revising commits with an interactive rebase, use reflog to show what changed between commits, so the user can understand the changes before running `git review`. Always update the commit message after amending (don't use `--no-edit`).

**MANDATORY: Preserve `Change-Id:` when amending commits during interactive rebases.** Before amending any commit that has been pushed to Gerrit, fetch the original `Change-Id` using `mcp__gerrit__get_commit_message` and include it verbatim in the amended message. After the rebase, verify via `mcp__gerrit__get_commit_message` that the Change-Ids in the local commits still match Gerrit. Using the wrong Change-Id creates a new Gerrit change and breaks the patch stack — this is extremely costly to fix.

Use `git -c commit.gpgsign=false` when committing — the sandbox blocks `~/.gnupg`.

## Manual testing

Use `/wmf-claude:manual-test [feature or url-path]` to manually test features on your local wiki via an external browser-automation tool (the skill walks Claude through the workflow but expects you to have something like `agent-browser` or Playwright installed).

## Logs

Application logs live in `logs/` (relative to the repo root). After manual testing or when debugging a change, check the logs for non-visible errors (PHP warnings/exceptions, deprecation notices) and slow MySQL queries. Use `tail` or `grep` to scan recent log entries.

## Schemas

Look for `tables.json` as the canonical reference of a database schema.

### Notable completed schema migrations

- **Blocks**: The `ipblocks` table no longer exists. Blocks use the `block` and `block_target` tables, joined on `bt_id = bl_target`. The `block_target` table has `bt_address` for the IP/range and `bt_auto` for autoblocks.
- **Actor migration**: Most tables no longer store `user_id` directly — they reference the `actor` table via an actor ID column (e.g. `rev_actor`, `rc_actor`, `log_actor`). Join to `actor` (`actor_id`, `actor_user`, `actor_name`) to resolve user info.

### WMF analytics replicas

Use plain database names (`enwiki`, `metawiki`, `commonswiki`) — never the `_p` suffix.
