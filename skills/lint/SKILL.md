---
description: Detect changed file types in the working tree and run the appropriate linters. Use when the user wants to lint their changes before committing.
disable-model-invocation: false
allowed-tools:
  - Bash(composer *)
  - Bash(npm run lint*)
  - Bash(git diff --name-only*)
  - Bash(vendor/bin/phpcs *)
  - Bash(vendor/bin/phpcbf *)
---

# Run Linters on Changed Files

Detect changed files and run the linters that match.

## Steps

1. List changed files: `git diff --name-only HEAD` plus `git diff --name-only --cached` for staged changes.
2. Decide what to run based on file extensions:
   - `.php` files → `composer phpcs` and `composer phan` (or `vendor/bin/phpcs --standard=MediaWiki <files>` if composer scripts aren't available in this project)
   - `.js`, `.ts`, `.css`, `.less`, `.vue` files → `npm run lint` (if `package.json` defines a `lint` script)
   - JSON / YAML — no MW-standard linter; spot-check with `jq` / `yamllint` if available
3. Run the relevant linters and report results. If a tool isn't installed in the project, say so rather than guessing.

## Linter details (typical MW-ecosystem invocations)

| Tool | Command | What it does |
|------|---------|--------------|
| PHPCS | `composer phpcs` | PHP CodeSniffer for MediaWiki style |
| Phan | `composer phan` | Static analysis for PHP |
| PHPCBF | `composer fix` | Auto-fix PHP style (run only if asked) |
| ESLint + stylelint | `npm run lint` | JS/CSS linting (via grunt in most MW repos) |

If the project's `composer.json` or `package.json` defines different script names (e.g. `lint:php`, `lint:js`), use those instead. Check the project's `CLAUDE.md` for any project-specific overrides.

## Notes

- If `$ARGUMENTS` is given, scope the linting (e.g. a specific extension directory).
- If no files are changed, tell the user instead of running anything.
- Don't write custom scripts to fix formatting or whitespace — always defer to the project's existing fixers.

## Input

`$ARGUMENTS`
