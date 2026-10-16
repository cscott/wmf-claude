# Test plan: `git sbx-pull`, end to end, with MultiTitle

This plan closes the open items in `NOTES.md` for the commit flow:

- the new image, with `git-sbx-pull` and the `/sbx-pull` user skill;
- the host command;
- `git review-check` and `git safe-reset NAME/BRANCH`;
- tab completion, including its fetch;
- the hand-off in both directions, and each safety stop;
- `wmf-sbx rm` removing the records.

It also does the Gerrit user-agent check in a new sandbox (NOTES, "Not yet
RAN in a new sandbox").

Do the steps in order: each step starts from the state that the step before
it left. Each step has commands for your host shell (**Host**), a prompt to
type into the sandbox's Claude Code session (**Agent**), and what you should
see (**Expect**). Copy the prompts exactly. When a result is different, stop
and send me the output.

Nothing in this plan uploads to Gerrit. **Do not run `git review`** in
MultiTitle during the test.

Names used below:

- `$MT`: your MultiTitle checkout on the host.
- `sbx-multititle`: the sandbox, and its remote in `$MT`.
- `SHORT`: a 12-character sha in the output. Yours will be different.

---

## 0. Prepare the host

**Host:**

```bash
cd /path/to/wmf-claude
git fetch dev
git switch lima-port && git merge --ff-only dev/lima-port

wmf-sbx resolve MultiTitle        # prints the project and its directory
cd <that directory> && export MT=$PWD
echo "${SANDBOX_NAME-unset}"      # must print "unset"; the host mode depends on it
git status --short                # must be empty
git switch master
git fetch origin && git merge --ff-only origin/master
wmf-sbx ls-remotes                # must print nothing
git branch --list 'mt-*'          # must print nothing
```

**Expect:** each command does what its comment says. If MultiTitle's
`master` has your own commits, stop and tell me: later steps reset
`master` to `origin/master`.

## 1. Make the sandbox (new image)

**Host:**

```bash
cd "$MT"
wmf-sbx create MultiTitle
```

**Expect:**

- `create` builds a new golden image, because the helpers changed.
- No warning that `git fetch origin` failed and fell back to `local`.
- Claude Code starts in `sbx-multititle`.
- `wmf-sbx ls-remotes` (in a second host shell, in `$MT`) prints `sbx-multititle`.

**Agent:**

> Run these and show me the output, changing nothing: `command -v git-sbx-pull`, `ls ~/.claude/skills/sbx-pull/`, `git fetch origin`, `git safe-reset origin`, `git status -sb`.

**Expect:**

- `/usr/local/bin/git-sbx-pull`
- `SKILL.md`
- `git fetch origin` with no 403
- `git safe-reset origin` ending on `origin/master`, with no "Could not reach Gerrit"

Then type `/` in the session: `/sbx-pull` must be in the command list.

**Closes:** the new image, the user skill, and Gerrit's user agent in a new sandbox.

## 2. Sandbox → host: a new branch, and completion with its fetch

**Agent:**

> Make a branch with `git switch -c mt-readme --track origin/master`. Add the sentence "Test line 1 for git sbx-pull." at the end of README.md and commit it with the subject "README: Test line 1". Do nothing else.

**Host:** the remote `sbx-multititle` has not been fetched yet, so only the
completion's fetch can find `mt-readme`. Open a **new shell** first, so that
it loads the new completion file:

```bash
cd "$MT"
git branch -r | grep sbx-multititle   # nothing yet
git sbx-pull <TAB><TAB>
```

**Expect:**

- After a short wait (the fetch): `mt-readme` and `sbx-multititle/mt-readme`.
- A second `<TAB><TAB>` straight after is instant.
- `ls .git/wmf-sbx-completion/` prints `sbx-multititle`.
- No `[1] 12345` job lines over the prompt.

**Host:** also check `--onto` completion:

```bash
git sbx-pull --onto=REL<TAB>
```

**Expect:** `REL1_46` and `REL1_47`.

**Host:** clear the line, then:

```bash
git sbx-pull
git status -sb | head -1
git config --get-regexp '^branch\.mt-readme\.wmfsbx'
git review-check
```

**Expect:**

- `git sbx-pull` with no argument takes the only new branch, and prints:
  ```
  mt-readme: new branch at SHORT (sbx-multititle/mt-readme)
  mt-readme: upstream origin/master (...)
  ```
- `## mt-readme...origin/master [ahead 1]`
- Three keys:
  - `branch.mt-readme.wmfsbx sbx-multititle`
  - `branch.mt-readme.wmfsbxbranch mt-readme`
  - `branch.mt-readme.wmfsbxsha <the tip>`
- `HEAD is in a sandbox (sbx-multititle/mt-readme)`

**Closes:** the completion fetch, a new branch with an upstream from the
sandbox branch's upstream, and `review-check` for agent refs.

## 3. Sandbox → host: the next pull needs no argument

**Agent:**

> On mt-readme, add the sentence "Test line 2." at the end of README.md and commit it with the subject "README: Test line 2". Do nothing else.

**Host:**

```bash
git sbx-pull
```

**Expect:**
`mt-readme: SHORT1 -> SHORT2 (sbx-multititle/mt-readme); the old tip is mt-readme@{1}`.

**Closes:** taking the current branch's record, and the move with `reset --keep`.

## 4. `git safe-reset NAME/BRANCH` hands over to `git sbx-pull`

**Agent:**

> On mt-readme, add the sentence "Test line 3." at the end of README.md and commit it with the subject "README: Test line 3". Do nothing else.

**Host:**

```bash
git safe-reset sbx-multititle/mt-readme
```

**Expect:**

- First: `sbx-multititle is a sandbox; running `git sbx-pull sbx-multititle/mt-readme` (use that command next time).`
- Then the `SHORT2 -> SHORT3` line from `git sbx-pull`.

## 5. Host → sandbox: a hand-off on a named branch

**Host:** amend the agent's last commit, as in a review:

```bash
git commit --amend -m "README: Test line 3 (amended on the host)"
```

**Agent:**

> /sbx-pull

**Expect:**

- The agent runs `git sbx-pull`, which prints:
  ```
  mt-readme: at SHORT (the host's mt-readme)
  old tip: <sha>
  new tip: <sha>
  what changed: git range-diff ...
  ```
- The agent runs the range-diff and says that only the commit message changed.
- It stays on `mt-readme`, with upstream `local/mt-readme`.
- It does not make a new branch.

**Host:**

```bash
git sbx-pull
git branch -r --contains HEAD
git review-check
```

**Expect:**

- `mt-readme: at SHORT (sbx-multititle/mt-readme) already`
- `sbx-multititle-seen/mt-readme` in the list
- `HEAD is in a sandbox (...)`

**Closes:** the guest's `git sbx-pull`, the `/sbx-pull` skill, and the
seen refs.

## 6. Both safety stops, and `-f`

Here both sides change `mt-readme` at the same time.

**Agent:**

> On mt-readme, add the sentence "Test line 4." at the end of README.md and commit it with the subject "README: Test line 4". Do nothing else.

**Host:** do not pull. Amend again:

```bash
git commit --amend -m "README: Test line 3 (amended twice)"
```

**Agent:**

> /sbx-pull

**Expect:**

- The sandbox's `git sbx-pull` stops with: `mt-readme (SHORT) has commits that the host does not have; ask the engineer to run `git sbx-pull` on the host first ...`
- The agent tells you this and does **not** use `-f`.

**Host:**

```bash
git sbx-pull
```

**Expect:** it stops with:
`mt-readme (SHORT) has commits that sbx-multititle/mt-readme does not have and are saved nowhere else; -f to move mt-readme anyway (the old tip stays in its reflog)`.

**Host:**

```bash
git sbx-pull -f
git log -1 --format=%s mt-readme@{1}
git log -1 --format=%s
```

**Expect:**

- The `-> ` line.
- `README: Test line 3 (amended twice)`: your amend is in the reflog.
- `README: Test line 4`

**Agent:**

> /sbx-pull

**Expect:** `git sbx-pull` takes the host's `mt-readme`, which is now the
agent's own tip: there is no old tip/new tip pair, and nothing to read.

## 7. The host does not leave unsaved work

**Host:**

```bash
git switch -c mt-scratch master
git commit -q --allow-empty -m "scratch: not saved anywhere"
git sbx-pull mt-readme
```

**Expect:** it stops with:
`mt-scratch (SHORT) is not on Gerrit, origin or a sandbox; upload it (git review), or -f to leave it anyway`.
(Its Gerrit query is a GET.)

**Host:**

```bash
git switch mt-readme
git branch -D mt-scratch
```

## 8. Host → sandbox: a hand-off from master (`--as`)

**Host:**

```bash
git switch master
echo "Hand-off test from master." >> README.md
git commit -qam "README: Hand-off test from master"
```

**Agent:**

> /sbx-pull

**Expect:**

- The sandbox's `git sbx-pull` stops with: `the host is on master, with 1 commit(s) that origin does not have; name a branch for the work: git sbx-pull --as NAME ...`
- The agent reads the commit and runs `git sbx-pull --as NAME`, with a name from the commit, such as `readme-handoff-test`.
- The new branch has upstream `local/master`.
- Its old branch `mt-readme` is saved, because the host has it, so there is no stop.

Write down NAME.

**Agent:**

> On this branch, add the sentence "Test line 5." at the end of README.md and commit it with the subject "README: Test line 5". Do nothing else.

**Host:**

```bash
git sbx-pull
git status -sb | head -1
```

**Expect:**

- `git sbx-pull` with no argument takes NAME, because it is the only new branch.
  `mt-readme` is not new: its tip is the recorded one. `git sbx-pull` prints:
  ```
  NAME: new branch at SHORT (sbx-multititle/NAME)
  NAME: upstream origin/master (...)
  ```
  The sandbox's upstream `local/master` becomes your `master`'s upstream.
- Leaving `master` does not stop: its hand-off commit is in the sandbox's NAME.
- `## NAME...origin/master [ahead 2]`

**Host:** put `master` back:

```bash
git switch master
git safe-reset origin
```

**Expect:**

- `safe-reset` does not refuse, because HEAD is in a sandbox.
- `master` is at `origin/master` again.

## 9. A release branch, and `--onto`

**Agent:**

> Make a branch with `git switch --no-track -c mt-rel origin/REL1_46`. Add the sentence "Test line REL." at the end of README.md and commit it with the subject "README: Test line REL". Do nothing else.

**Host:**

```bash
git sbx-pull mt-rel
git sbx-pull mt-rel --onto REL1_47
git sbx-pull mt-rel --onto REL1_46
git status -sb | head -1
```

**Expect:**

1. `mt-rel: new branch at ...` and `mt-rel: upstream origin/REL1_46 (...)`. The sandbox branch has no upstream, so this is the guess. If it says `origin/master` or `origin/REL1_47`, send me the output.
2. `at SHORT ... already`, then `mt-rel: upstream origin/REL1_47 (--onto)`
3. The same line, back to `origin/REL1_46`.
4. `## mt-rel...origin/REL1_46 [ahead 1]`

## 10. `wmf-sbx rm` removes the records

**Host:**

```bash
git switch master
wmf-sbx rm sbx-multititle        # everything was pulled, so it must not refuse
git remote | grep sbx-multititle                      # nothing
git config --get-regexp '^(branch|remote)\..*\.wmfsbx'   # nothing
                     # (wmfsbx.* keys are the image snapshot's: they stay)
git for-each-ref refs/remotes/sbx-multititle-seen/    # nothing
git branch --list 'mt-*' NAME                         # the branches stay
```

**Host:** clean up (`NAME` from step 8):

```bash
git branch -D mt-readme mt-rel NAME
rm -rf .git/wmf-sbx-completion
```

---

## Not in this plan

- **`git sbx-pull BRANCH` with more than one sandbox.** It must stop with "more than one sandbox has BRANCH; name one". It needs two MultiTitle sandboxes; only the unit tests check it.
- **Leaving a branch that is at its recorded tip, after the sandbox branch has
  moved on.** The unit tests check it; here such a tip is always saved for
  another reason too.
- **zsh, and macOS bash 3.2.** The completion does not support them (NOTES, "Open").

## Report

Send me the step numbers that passed, and the output of each step that did
not. I will record them in `NOTES.md` as RAN.
