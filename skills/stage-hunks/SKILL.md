---
description: Stage selected hunks, or split changes into atomic commits, without `git add -p` (which cannot run here — the Bash tool has no TTY on stdin). Uses a non-destructive git diff → edit patch → git apply --cached round-trip. Use when a change needs splitting into separate commits, when only part of a file should be staged, or when an unpushed commit is too big.
allowed-tools:
  - Bash(git diff *)
  - Bash(git status *)
  - Bash(git apply *)
  - Bash(git add *)
  - Bash(git commit *)
  - Bash(git log *)
  - Bash(git show *)
  - Bash(git rev-parse *)
  - Bash(git symbolic-ref *)
  - Bash(git reset --soft *)
  - Bash(mkdir *)
  - Read
  - Write
---

# Stage Hunks

Stage a chosen subset of the working tree so commits can be atomic, without any
interactive git command.

`git add -p`, `git add -i`, and `git checkout -p` cannot work in this environment: the
Bash tool runs commands with no TTY on stdin, so their hunk loops read EOF and abort
having staged nothing. This is a harness property, not a sandbox policy — there is no
setting that changes it.

## Rule 0 — never destroy the working tree

Do **not** run `git reset --hard`, `git checkout -- <path>`, `git restore`,
`git clean`, or `git stash drop` in order to reorganise commits, and never re-type
changes from memory to rebuild them. That discards reviewed, tested work and can
silently reconstruct it differently from what was verified.

Bare `git reset` (mixed) and `git reset --soft` are safe — they move HEAD and the index
only, never the working tree — and are the only resets this skill uses.

## Steps

### 1. Snapshot first, unconditionally

Before touching the index, make the work recoverable:

```bash
git status --short                       # note any ?? untracked files
git add -N <each-untracked-path>         # intent-to-add: see Notes — do this first
D="${TMPDIR:-/tmp}/wmf-claude-staging/$(git rev-parse --abbrev-ref HEAD)"
mkdir -p "$D"
git diff --binary          > "$D/00-all-unstaged.patch"
git diff --cached --binary > "$D/00-all-staged.patch"
git status --short         > "$D/00-status.txt"
```

Tell the user where `$D` is. If anything goes wrong later, these patches rebuild the
original state.

### 2. Take the cheap route when it applies

If the split is one-commit-per-file, just `git add <paths>` for the first group and go
to step 5. Do not build patch files for a split that `git add` already expresses.
Likewise, a **new** file only ever belongs wholly to one commit — stage it with
`git add <path>`, never via a patch.

### 3. Build a patch containing only the wanted hunks

```bash
git diff -U1 --binary -- <paths> > "$D/work.patch"
```

`-U1` keeps context narrow so changes a few lines apart stay in separate hunks; more
context merges them into one. Read `work.patch`, then write a new file containing, for
each file you want to touch:

```
diff --git a/PATH b/PATH
--- a/PATH
+++ b/PATH
@@ ... @@          <- only the hunks you want, copied verbatim
```

Omit files with no wanted hunks entirely. The `index` line is optional and can be
dropped.

### 4. Dry-run, then apply to the index only

```bash
git apply --cached --check "$D/commit1.patch"   # always check first
git apply --cached         "$D/commit1.patch"
```

`--cached` writes the index and nothing else, so the working tree still holds every
change at every point in this process. Nothing is at risk.

If you edited lines *inside* a hunk rather than dropping whole hunks, the `@@` line
counts no longer match and git rejects the patch as `corrupt patch`. Add `--recount` so
git recomputes them:

```bash
git apply --cached --recount "$D/commit1.patch"
```

### 5. Verify both sides, then commit

```bash
git diff --cached    # must be exactly this commit's content
git diff             # must be exactly the remainder
```

Together these must account for everything in the step 1 snapshot. If they do, commit
(use `/wmf-claude:write-commit-msg` for the message), then repeat from step 3 for the
next commit. After the last commit, `git diff` and `git diff --cached` should both be
empty.

### 6. If the index ends up wrong

Run bare `git reset` — index only, working tree untouched — and restart from step 3.
The snapshot from step 1 is still on disk.

## Splitting an already-made commit

Only for commits **not yet pushed**. `git reset --soft HEAD~1` moves HEAD back and
leaves everything staged, working tree intact; bare `git reset HEAD~1` also unstages.
Then continue from step 3. Never `--hard`.

For a commit already pushed to Gerrit, preserve its `Change-Id` — fetch it with
`mcp__gerrit__get_commit_message` before rewriting anything.

## When to hand it back to the engineer

If the split needs human judgement — an ambiguous hunk, or intertwined changes where
you cannot tell which commit a line belongs to — stop and ask. Suggest they run
`git add -p` themselves in a separate terminal in the same checkout and tell you when
they are done. Do not guess at the split.

## Notes

- **`git diff` omits untracked files.** A file listed `??` by `git status` appears in no
  patch at all, so a snapshot taken before `git add -N` silently misses it. Run
  `git add -N <path>` on every untracked path first. Verified: intent-to-add entries do
  **not** leak into commits as empty files — the file is only committed once its content
  is really staged, so leaving the `-N` marker in place is safe.
- **`--binary`** is needed for binary files; without it the patch records only
  "Binary files differ" and will not apply.
- **`-U0`** splits hunks even more finely than `-U1`, but such patches then need
  `git apply --unidiff-zero`. Prefer `-U1`.
- **Never pipe canned answers into `git add -p`** (e.g. `printf 'y\nn\n' | git add -p`).
  It appears to work, but the answers are matched to hunks by position, so it stages the
  wrong content whenever the diff shifts.

## Input

`$ARGUMENTS`
