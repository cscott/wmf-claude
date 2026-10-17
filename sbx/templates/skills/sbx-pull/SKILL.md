---
description: Take the engineer's hand-off from the host in a wmf-sbx sandbox. Runs `git sbx-pull`, reads what the engineer changed, and keeps working on the same branch. Use when the engineer types /sbx-pull, or says they handed the work back, edited or amended your branch, or want you to start from what is on their machine.
allowed-tools:
  - Bash(git sbx-pull*)
  - Bash(git -C * sbx-pull*)
  - Bash(git range-diff*)
  - Bash(git -C * range-diff*)
  - Bash(git log*)
  - Bash(git -C * log*)
  - Bash(git status*)
  - Bash(git -C * status*)
---

# Take a hand-off from the host

The engineer's checkout is the remote `local` (read-only). They hand work
to you by leaving it on a branch in their checkout; `git sbx-pull` takes
that branch into your clone. They take your work back with their own `git
sbx-pull`. Do this in each repo the engineer names (`git -C REPO ...`);
with none named, in the repo of the current work.

## Steps

1. `git status --short`. If the tree has changes, stop: tell the engineer,
   and ask whether to commit them first. Do not stash or discard them.
2. Run `git sbx-pull` with no argument (`git sbx-pull BRANCH` when the
   engineer names a branch). Then, by what it prints:
   - **`what changed: git range-diff OLD...NEW`**: run that command and read
     it. It shows what the engineer changed in your commits (an amend, a
     fix-up, a rebase). Say in a few lines what they changed, and keep it
     in mind: do not undo their edits.
   - **`new tip: SHA`** with no old tip: a branch new to this sandbox. Read
     `git log -p @{upstream}..HEAD`, or the last few commits, to learn
     what the work is.
   - **`name a branch for the work: git sbx-pull --as NAME`**: the engineer
     is on `master` (or `main`) with work-in-progress commits. Read them:
     run the `git log -p ...` command it printed. Choose a short branch name
     for the work, from the commit messages and the diff: lowercase words
     with hyphens, at most about five (`fix-ref-escaping`,
     `cite-backlink-labels`); not `master`, `main`, `REL*`, `wmf/*` or
     `review/*`. Run `git sbx-pull --as NAME`, and tell the engineer the
     name: it is the branch name they will see.
   - **`has commits that the host does not have`**: your work is not on
     the host yet, and taking the hand-off would leave it behind. Stop and
     tell the engineer to run `git sbx-pull` in their checkout first. Use
     `-f` only when the engineer says to drop your commits.
   - **`nothing that origin does not have`**: the engineer's branch has no
     work on it. Ask them which branch they meant.
   - Any other error: report it as printed. Do not work around it.

## After the hand-off

- Keep working on this branch, under this name. Do not make a new branch
  for the next step: the engineer finds your work by this name, and a new
  name at each hand-off leaves them to remember which branch is the
  latest. Only a hand-off from `master`/`main` gets a new branch (`--as`).
- Amend or add commits as the engineer asks. An amend keeps the commit's
  `Change-Id:` trailer: keep it.
- When you stop, commit on this branch and tell the engineer the branch
  and commit. They take it with `git sbx-pull` in their checkout.
