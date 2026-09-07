# Let the primary workspace be read-only — our draft feature request

**Status: FILED, independently, as
[`docker/sbx-releases#586`](https://github.com/docker/sbx-releases/issues/586)**
— *"Feature request: allow read-only primary workspace for sandbox
hardening"*, cscott, 2026-09-14 21:25Z, open, no comments yet. cananian
wrote and filed their own version rather than this one. Design context
and the alternative we shelved in favour of asking: `NOTES.md`
§82.5–§82.6; the gap it closes is `SECURITY.md` §3.

**This page is now follow-up material, not a report.** #586 is shorter
and carries two arguments this draft does not: sandbox-to-sandbox
isolation for two agents working the same repo on different tasks, and
the open-source framing (read exposure is acceptable, unexpected writes
are not). It also cites
[#430](https://github.com/docker/sbx-releases/issues/430) ("Consider
adding 'remote' workflow" — no local mount at all, clone from the
remote), which this draft missed.

Five things in the body below are **not** in #586, in the order they are
worth adding as a comment if the thread needs one — §82.6 has the same
list with the reasoning:

1. **The scratch-primary workaround, pre-empted.** The likely first
   reply to #586 is "pass an empty directory as the primary and mount
   your repo `:ro` as an extra" — which works today, and which we
   rejected because the primary workspace's *path* is the agent's cwd
   and the agent cannot change it (§82.5). Highest value by a distance:
   without it the issue can be closed as already-possible.
2. **The two-path measurement** (`claude A:ro B:ro` fails identically),
   which shows the check is on position 0 alone rather than on the set,
   and so localises the fix to one validation.
3. **"Most of it already exists"**: `:ro` enforced below the namespace
   since 0.39.0 (#556, cscott's own), no-workspace sandboxes since
   0.42.0.
4. **Cheaper paths to yes**: a warning instead of an error, an explicit
   `--allow-readonly-workspace`, or `:ro` in position 0 read as the
   opt-in.
5. **Why `--clone` is not already the answer** for a sandbox that clones
   every workspace itself.

**A correction to this page's own search advice.** The bullet below used
to say a body-text search might find an existing request where our
title-only sweep did not. It would have: #430 has neither `workspace`
nor `read-only` in its title. cananian found it; we did not. Keep the
advice, and note it has now been paid for once.

**Re-check against whatever version is current** before adding anything:
the body below is written against **0.42.1** (the §82.3 measurement) and
0.43.0-rc3's release notes (§81), which change nothing here.

Everything below the line is the body, as drafted and not filed.

---

**Title:** Allow the primary workspace to be read-only (`:ro` on the first positional), for sandboxes that provide their own writable copies

### Summary

`sbx create` rejects `:ro` on the first positional workspace:

```console
$ sbx create --name probe-allro claude ~/src/project-a:ro ~/src/project-b:ro
ERROR: primary workspace must be read/write (remove ':ro' or ':readonly')
```

The rule is positional, not set-based — the command above names two
workspaces, neither of which the agent needs to write, and it is still
refused because the *first* one is `:ro`.

I would like `sbx create AGENT A:ro B:ro …` to be accepted, producing a
sandbox in which no host directory is writable.

### Why

We run agents against a set of source repositories that must not be
modified in place. The agent gets writable **clones** instead, made
inside the sandbox on the container's own filesystem, and the host
checkouts are mounted `:ro` purely as reference — the human reviews the
agent's commits and fetches them out over the sandbox's git daemon.

Every repository in that set is `:ro` by design. One of them
nevertheless has to be mounted read/write, because it is the one that
landed in position 0, and that single mount is the only writable path to
the user's real work in an otherwise closed sandbox. It is also the
repository the agent spends all its time in, which is the worst one to
leave writable.

### Why the existing workarounds do not cover it

- **`--clone`** solves it for the first workspace only, and we already do
  our own cloning for *every* workspace; adopting `--clone` for the
  primary alone would mean two different clone mechanisms with two
  different layouts inside one sandbox.
- **Creating with no workspace at all** — the 0.42.0 feature ("Sandboxes
  can now be created without a workspace bind mount by omitting the path
  in `sbx create`") — drops the `:ro` reference mounts too, which are the
  thing the agent reads.
- **A throwaway empty directory in position 0**, with the real
  repositories as `:ro` extras, does work today. We designed it and
  decided against it, for a reason that is worth passing on: `sbx` starts
  the agent in the primary workspace's path, and a coding agent's
  behaviour depends on that path being the project's real path. Claude
  Code in particular keys its per-project state (`~/.claude.json`'s
  `projects` map, and the on-disk project directory under
  `~/.claude/projects/`) off the working directory, and the agent cannot
  change its own working directory — so a scratch primary means every
  session, and every restart after an idle stop, begins in a directory
  whose name is meaningless. Mounting the real tree over the scratch path
  fixes the contents but not the name.

### What I think the change is

Most of it looks like it already exists:

- read-only extra workspaces are genuinely enforced below the sandbox's
  mount namespace — `sudo mount -o remount,rw` inside the sandbox no
  longer grants writes. That was [#556](https://github.com/docker/sbx-releases/issues/556),
  fixed by 0.39.0;
- a sandbox with **no** writable host workspace is already a supported
  state, as of 0.42.0's "without a workspace bind mount".

So the request is not "implement read-only mounting" or "support a
sandbox with nothing writable" — both exist. It is to stop rejecting
`:ro` in the first positional, and to treat the result the way a
no-workspace sandbox is already treated, while still starting the agent
in that first workspace's path.

If the validation exists to protect people from an agent that cannot
write anywhere and fails confusingly, a warning would serve as well as
an error, or an explicit opt-in flag (`--allow-readonly-workspace`, or
`:ro` on position 0 being read as the opt-in itself).

### Environment

- sbx v0.42.1, Linux host (Ubuntu 24.04, kernel 7.0).
- Reproduced with two `:ro` positionals as shown above; the same error
  appears for a single `:ro` positional.
