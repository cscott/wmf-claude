# Claude memory persistence under `sbx`

**Status: design only** (as of 2026-09-08). Nothing in §"What to build" is
implemented — no host-side store, no seeding step in `wmf_sbx_create`, no
extraction on `wmf-sbx-rm`. The stopgap in the meantime is
`sbx/reference/claude-memory-seed/`: a checked-in snapshot of the memory
files that a fresh sandbox's Claude should start from, copied in by hand
(see `sbx/NOTES.md` §0). That snapshot is a
single-scope, manually-refreshed approximation of the global scope
described below.

cananian asked how Claude Code's memory files (`~/.claude/projects/<key>/memory/`)
persist, or don't, under `sbx`, and whether that argues for per-sandbox
isolation, a shared store, or something in between.

## What's actually true, checked directly from inside this sandbox

`findmnt -a` / `/proc/mounts` inside `wmf-claude-sbx` show:

```
/      overlay  rw,lowerdir=/run/bundles/<hash>/mounts/1,upperdir=/run/bundles/<hash>/mounts/0/upper,...
```

with **no** bind mount anywhere under `/home/agent`. Every bind mount in
this sandbox is one of the explicit `--clone`/read-only workspace paths
under `/home/cananian/Projects/Wikimedia/...` or `/run/sandbox/source`.
`~/.claude` (including its `projects/<key>/memory/` subtree) lives entirely
on **this sandbox's own private overlay disk** — it is not shared with the
host filesystem, and not shared with any other sandbox, by construction.

Interesting detail: `WORKSPACE_DIR` and `PWD` are both the **host-looking**
path `/home/cananian/Projects/Wikimedia/wmf-claude`, even though this is a
`--clone`-mode workspace living on the VM's own overlay (per `sbx/NOTES.md`
§2 — `git clone --reference /run/sandbox/source ... $WORKSPACE_DIR`, with
`$WORKSPACE_DIR` set to the *original host path string*, not something like
`/workspace`). That's why Claude Code's project-keying directory here is
literally `-home-cananian-Projects-Wikimedia-wmf-claude` — the same key a
bare-host Claude Code invocation from that same directory would use. But
the key matching doesn't mean the storage is shared: it's the same string,
computed independently, over two different physical directories (the
host's real working tree vs. this VM's private overlay clone).

**This explains the difference from cananian's recollection of wmf-claude's
(nono-based) behavior**: on a bare host, or under a setup that bind-mounts
`~/.claude` from the host, `~/.claude/projects/<key>/memory/` is one real,
persistent directory, shared by every Claude Code invocation from that
working directory. Under `sbx`, unless something explicitly arranges
otherwise, each `sbx create`/`sbx run` gets a **fresh, empty** `~/.claude`
on that sandbox's own new VM/overlay — memory does not survive even a
second sandbox created against the identical workspace path, let alone a
different one.

## What does and doesn't persist, per the CLI/architecture docs

- `docs.docker.com/ai/sandboxes/architecture.md`: "You can stop and restart
  without recreating the VM, preserving installed packages and Docker
  images." So `sbx stop` followed by `sbx run --name X` (re-attaching to
  the *same* named sandbox) keeps the same VM disk — `~/.claude` **does**
  survive a stop/restart cycle of one sandbox.
- `sbx rm`: "Stops running sandboxes, removes their containers, cleans up
  any Git worktrees, and deletes sandbox state. This action cannot be
  undone." Everything, including `~/.claude`, is gone once a sandbox is
  removed.
- `sbx cp [flags] SRC DST` (`SANDBOX:PATH` on either side, not both): the
  documented, supported way to move files between a sandbox and the host,
  independent of whether the agent is mid-session. This is the mechanism
  for both seeding memory in and extracting it back out.
- `sbx exec SANDBOX COMMAND` also works against a stopped sandbox ("If the
  sandbox is stopped, it is started first"), so a merge/export script could
  run entirely inside the sandbox via `sbx exec` instead of round-tripping
  files through `sbx cp`, if that's ever more convenient.

So: **sandbox-to-sandbox isolation is already the default and needs no
extra work** — this addresses cananian's "memories for one project may be
wasted tokens on a task in a different sandbox" concern automatically,
simply by virtue of how `sbx` provisions each sandbox's root filesystem.
The open design question is only the opposite direction: how to
deliberately share memory *across* sandboxes that should share it (repeat
sandboxes for the same Gerrit project; global WMF-wide knowledge), without
turning that into concurrent-write shared mutable state.

## Proposed design (future work, not blocking Layer A)

Matches cananian's stated preference for an **explicit extract-and-merge
step**, not live sharing:

- **Host-side memory store**, outside any single sandbox, organized by
  scope:
  - `~/.config/wmf-sbx/memory/global/` — knowledge with no single-repo
    owner (e.g. "Gerrit's REST API is reachable unauthenticated and
    supports `?p=`/`?m=`", WMF policy quirks, `sbx` usage lessons).
  - `~/.config/wmf-sbx/memory/repos/<canonical-gerrit-path>/` — memory
    scoped to one repo (e.g. `mediawiki/extensions/Cite`), keyed by the
    same canonical path the name-resolution design
    (`sbx/DESIGN-repo-resolution.md`) already produces.
- **Seed at `sbx create` time**: the wrapper `sbx cp`s the union of the
  global store plus the matching repo-scoped store into the new sandbox's
  `~/.claude/projects/<key>/memory/` before first launch. A plain snapshot
  copy — no live mount, so no risk of two concurrent sandboxes for the same
  repo racing on the same files.
- **Extract-and-merge at teardown**, as an explicit step the wrapper offers
  (not something that happens silently on every `sbx stop`): `sbx cp` the
  sandbox's memory directory back out to a scratch location, diff it
  against the current host-side store per scope, and reconcile — new files
  get added outright; changed files get a review pass (a merge could be as
  simple as "show the diff and ask," or as involved as delegating
  conflicting-content reconciliation to an agent turn, since these are
  short semantic markdown files, not line-oriented data). This is also the
  natural point to notice a memory written while working on one repo is
  actually global (e.g. a Gerrit-API or `sbx`-behavior fact learned
  mid-task on Cite) and move it from that repo's scope to `global/`.
  Concurrent sandboxes for the same repo never conflict at the storage
  layer, because extraction only happens one sandbox at a time, at that
  sandbox's own teardown moment — the merge step is what absorbs any
  actual disagreement between what two sandboxes learned.

## Open follow-ups

- Not yet checked whether `sbx cp` works against a *stopped* sandbox
  without first calling `sbx run`/`sbx exec` to wake it (the `cp` docs
  don't say either way; `exec`'s docs explicitly auto-start on demand).
- Not yet decided whether extraction should be offered per-`sbx stop` too
  (a sandbox that's paused, not removed, might still be worth syncing) or
  strictly per-`sbx rm`.
- No decision yet on the merge mechanics themselves (manual diff-and-ask
  vs. an agent-mediated reconciliation pass) — flagged as a real design
  choice, not a detail to default silently.
