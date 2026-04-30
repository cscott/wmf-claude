---
description: Drop a starter CLAUDE.md into the current repository. Use when an engineer wants to bootstrap a new WMF project with a baseline CLAUDE.md (and optionally MediaWiki conventions). Run from the project root.
argument-hint: "[--mediawiki]"
allowed-tools:
  - Bash(test:*)
  - Bash(git rev-parse:*)
  - Read
  - Write
---

# Initialize a project for wmf-claude

Drops a starter `CLAUDE.md` into the current repository so the engineer has a place to record project-specific conventions. If invoked with `--mediawiki`, also appends MediaWiki conventions (services, hooks, REST, autoloading, code style, schemas).

The plugin's templates live at `${CLAUDE_PLUGIN_ROOT}/templates/`.

## Steps

1. Confirm we're at a sensible location:
   - Run `git rev-parse --show-toplevel` to find the project root. If the command fails, tell the user this skill must be run from inside a git repository and stop.
   - `cd` mentally to that root for the rest of these steps.

2. Check for existing `./CLAUDE.md`:
   - Run `test -f ./CLAUDE.md`. If it exists, do NOT overwrite. Tell the user it already exists and ask whether they want it appended to (with the MW addendum if `--mediawiki` was passed) or whether to skip.

3. If `./CLAUDE.md` doesn't exist:
   - Read `${CLAUDE_PLUGIN_ROOT}/templates/CLAUDE.md`.
   - Write the contents to `./CLAUDE.md` verbatim.

4. If `$ARGUMENTS` contains `--mediawiki`:
   - Read `${CLAUDE_PLUGIN_ROOT}/templates/mediawiki/CLAUDE.md`.
   - Append the contents to `./CLAUDE.md` (after a blank line separator). Do NOT overwrite existing MediaWiki content if it's already present — check for the `## Available MediaWiki skills` heading first.

5. After the file is in place, summarize what was done and tell the engineer:
   - Edit `CLAUDE.md` to fill in the project-specific sections marked with HTML comment placeholders.
   - The file is the canonical place for project-specific conventions and is auto-loaded by Claude Code into context.

## Input

`$ARGUMENTS`
