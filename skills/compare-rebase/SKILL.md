---
description: Show what changed in each commit after an interactive rebase by reflog-walking the previous commit shas and writing per-commit diffs to files. Use when the user has just finished a rebase and wants to review the deltas before pushing.
argument-hint: "[base-commit]"
allowed-tools:
  - Bash(git reflog *)
  - Bash(git log *)
  - Bash(git diff *)
  - Bash(git merge-base *)
  - Bash(git rev-parse *)
  - Bash(git symbolic-ref *)
  - Bash(mkdir *)
  - Write
---

# Compare Rebase

Show what changed in each commit after a rebase, writing per-commit diffs to files for review in an editor.

## Steps

1. **Determine the base commit:**
   - If `$ARGUMENTS` is given, use it as the base (commit hash, branch name, or `HEAD~N`).
   - Otherwise find the merge-base with the project's primary branch. Detect the primary branch with `git symbolic-ref refs/remotes/origin/HEAD` (typically yields `origin/main` or `origin/master`); fall back to `origin/master` if that fails.

2. **List the current commits above the base:**
   ```bash
   git log --oneline --reverse <base>..HEAD
   ```

3. **Search the reflog for previous versions of these commits.** Use the reflog to find entries where the branch was updated by `rebase` and match commits by their subject line (first line of commit message):
   ```bash
   git reflog --format="%H %gs: %s"
   ```
   Look for `rebase (pick)` or `rebase (fixup)` entries whose subjects match the current commits.

4. **For each commit in the current stack:**
   - If a previous version was found, run `git diff <old-hash> <new-hash>` and write the output to a `.patch` file.
   - If no previous version exists (new commit), note that it's new.

5. **Write all diff output** to a per-branch directory under the user's tmp dir:
   ```
   ${TMPDIR:-/tmp}/wmf-claude-diffs/<branch-name>/
   ```
   Use filenames like `01-<short-subject>.patch` (numbered by commit order). If there are multiple commits, also create an `all.patch` combining everything.

6. **Tell the user the file path(s)** so they can open them in their editor. Do NOT summarize the diff contents — the user will read the files directly.

## Notes

- This skill assumes the engineer has done a rebase recently enough that the previous commit shas are still in the reflog (default expiry is 90 days for reachable, 30 days for unreachable refs).
- For very large diffs, the `all.patch` file may be unwieldy; the per-commit files are the primary deliverable.

## Input

`$ARGUMENTS`
