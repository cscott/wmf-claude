---
description: Draft a commit message for the currently staged changes following Wikimedia conventions (component subject, Why/What body, Assisted-by trailer, Bug/Change-Id trailers). Use when the user wants to commit staged work and have the message follow Gerrit/MediaWiki format.
disable-model-invocation: false
allowed-tools:
  - Bash(git diff --cached*)
  - Bash(git log*)
  - mcp__phabricator__phabricator_get_task
  - mcp__gerrit__get_commit_message
---

# Draft a Commit Message

Generate a commit message for the currently staged changes, following Wikimedia conventions (Gerrit `Change-Id`, `Bug:` Phab reference, `Assisted-by:` for AI assistance).

## Steps

1. Run `git diff --cached` to see what's staged
2. Run `git log --oneline -5` to see recent commit style for this repo
3. **MANDATORY:** if there's a candidate `Bug: TXXXXX` (from branch name, prior commits, or context), call `mcp__phabricator__phabricator_get_task` to verify the task title actually matches the change. Do NOT trust the branch name — task IDs in branch names are frequently wrong. If you can't verify, leave the line as `Bug: TXXXXX` for the user to fill in.
4. If amending an existing commit that has been pushed to Gerrit, fetch the original `Change-Id` via `mcp__gerrit__get_commit_message` and include it verbatim. Never let the commit-msg hook generate a new one.
5. Draft a commit message following the format below.

## Format

```
component: Short subject line

Why:

- Reason for the change

What:

- What was changed

Assisted-by: Claude Opus 4.7
Bug: TXXXXX
```

## Rules

- The subject line should be concise and describe the "what" at a high level. Prefix with the component name (e.g. `MyExtension: …`, `wmf-config: …`).
- `Why:` explains the motivation — why is this change needed?
- `What:` lists specific changes made.
- Always put a blank line after `Why:` and after `What:`.
- Every item under `Why:`/`What:` must start with `- ` (even if there's only one item).
- Never end bullet points with a period.
- Wrap all body lines at 72 characters maximum (subject can be up to 80).
- `Assisted-by:` uses Linux-kernel style: just the model name, no email angle brackets, no `(1M context)` or other parentheticals (e.g. `Assisted-by: Claude Opus 4.7`). Goes directly above `Bug:`.
- Never use `Co-Authored-By:` — Wikimedia convention is `Assisted-by:` for AI assistance.
- `Bug:` goes directly above `Change-Id:` (which the Gerrit commit-msg hook adds automatically).
- **Verifying the `Bug:` reference is mandatory** — see step 3 above. Do not infer the task ID from the branch name without calling `mcp__phabricator__phabricator_get_task`.
- After an amend, the message should describe the current state vs. base — not the iteration history of how it got there.
- Output ONLY the commit message — no code fences, no commentary.

## Input

$ARGUMENTS
