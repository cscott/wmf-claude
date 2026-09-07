# Actually adding (and removing) the host-side `sandbox-<name>` remotes

**Status: implemented.** `sbx/bin/wmf_sbx_state.py`,
`sbx/bin/wmf_sbx_remotes.py`, `sbx/bin/wmf_sbx_rm.py` +
`sbx/bin/wmf-sbx-rm`, and the `wmf_sbx_create.py` wiring (`--no-remotes`,
the opportunistic prune, dict-shaped `parallel_tree_remotes`) all landed
as described, with these deltas:

- `prune` lives in `wmf_sbx_remotes.prune_dead(live_names, ...)`, taking
  the live-sandbox set as an argument rather than looking it up, so
  `wmf_sbx_create` can call it without an import cycle (`wmf_sbx_rm`
  imports `wmf_sbx_create` for `existing_sandbox_names`, not the reverse).
- `unfetched_tips` deduplicates SHAs: `git ls-remote` reports `HEAD` and
  the branch it points at as two lines, and reporting one commit twice
  made the refusal message read as more lost work than there was.
- **Suspending gc rode along afterwards** (2026-09-08, cananian). Once
  the parallel-tree clones became `git clone --shared` (`NOTES.md` §30),
  each sandbox clone reads its objects out of the *host* repo through
  `objects/info/alternates`, and a `git gc` on the host can collect
  objects it still needs. The remote lifecycle is exactly the right hook:
  `sync_remotes` calls `suspend_gc(host_dir)` after it sets the ownership
  marker, `remove_remotes` calls `resume_gc(host_dir)` after each entry,
  and `resume_gc` restores only once `marked_remotes(host_dir)` is empty
  — so two sandboxes sharing a host repo can't un-protect each other. The
  previous values live in the repo's own config as
  `wmfSbx.savedGcAuto` / `wmfSbx.savedGcPruneExpire` (with an `(unset)`
  sentinel), keyed on a `wmfSbx.gcSuspended` marker for idempotence, all
  read and written with `--local` so an inherited `~/.gitconfig` value
  never gets pinned to one repo. See `NOTES.md` §31.3 for the three
  subtleties. **Known gap:** a repo we clone but fail to add a remote to
  (§4's "name-taken" skip) keeps gc enabled.
- Tests are in `sbx/bin/tests/test_wmf_sbx_{state,remotes,rm}.py` and
  drive **real git repositories** in tempdirs rather than faking
  `subprocess.run`, since §11's decisions rest on git's exit-code and
  config contracts (3 / 2 / marker-dropped-with-the-remote) — a fake would
  only re-assert my reading of them. `GitContractTests` pins those three
  directly.

`sbx/DESIGN-parallel-clone-tree.md` §3 gets the host to the point where the
sandbox's parallel clone tree is *fetchable*: `wmf_sbx_setup.py` starts a
read-only `git daemon` over the tree, `wmf_sbx_create.publish_daemon_port`
publishes its port, and `print_remote_add_reminder` prints one line per
repo:

```
git -C /home/cananian/Wikimedia/core remote add sandbox-mw-cite git://127.0.0.1:32779/Wikimedia/core
```

...and then stops, leaving the engineer to copy-paste N lines. This
document is the plan for running those commands ourselves, and — the part
that has to be settled *first* — for taking them back out again when the
sandbox goes away.

## 1. Why cleanup is the hard half

Three properties make "just run `git remote add`" unsafe on its own:

- **The remotes outlive the sandbox.** `sbx rm` destroys the sandbox and
  everything in it; the `sandbox-mw-cite` entries in a dozen host
  `.git/config` files survive untouched. Over a few months of sandboxes
  that is unbounded accumulation in repos the engineer uses daily.
- **Host ports get recycled.** §15.3's publish step deliberately lets
  `sbx ports --publish` pick an ephemeral host port, precisely so we do no
  collision bookkeeping. The flip side is that once sandbox `mw-cite` is
  removed, its port is free, and a *later, unrelated* sandbox can be
  assigned it (confirmed real by cananian, 2026-09-07 — not an inference
  from how ephemeral ports generally work). A stale `sandbox-mw-cite`
  remote then points at a live daemon serving somebody else's tree, and
  `git fetch sandbox-mw-cite` silently succeeds with the wrong objects.
  This is the strongest argument for reliable cleanup — a leaked remote is
  not merely untidy, it is actively wrong.
- **The URL is not stable for the sandbox's own lifetime either.** So the
  same machinery that adds a remote must also be able to *re-point* one.
  **CONFIRMED on the host 2026-09-08** (`sbx/NOTES.md` §34), and worse
  than this section originally assumed: the daemon itself now survives a
  restart (the kit's `setup.startup` command brings it back — §33.2), but
  sbx re-applies the publish with a **fresh ephemeral host port every
  time** (32783 → 32784 across one `sbx stop`/start). So a recorded port
  is stale from the first restart onward, and no code should read one
  except as a hint. `wmf-sbx-resume` and `wmf-sbx-rm`'s guard both call
  `wmf_sbx_create.refresh_host_port`, which looks the port up and hands
  fresh URLs back to `sync_remotes`.

Together those mean: the URL cannot be the identity of the remote, and we
need persistent per-sandbox state recording which host repos we touched.

## 2. Persistent state

One JSON file per sandbox, written atomically (tempfile + `os.replace`) at:

```
${XDG_STATE_HOME:-~/.local/state}/wmf-sbx/sandboxes/<name>.json
```

Per-sandbox files rather than one registry so that two concurrent
`wmf-sbx-create` runs never read-modify-write the same file. `<name>` is
validated against `^[A-Za-z0-9][A-Za-z0-9._-]*$` before use as a filename
(no separators, no `..`); `sbx` sandbox names are already this shape, and
an explicit `--name` that isn't gets rejected up front rather than
escaping the directory.

```json
{
  "schemaVersion": 1,
  "name": "mw-cite",
  "created": "2026-09-07T18:22:03Z",
  "daemonPort": 9977,
  "hostPort": 32779,
  "primaryDir": "/home/cananian/Wikimedia/core",
  "remotes": [
    {
      "hostDir": "/home/cananian/Wikimedia/core",
      "remote": "sandbox-mw-cite",
      "url": "git://127.0.0.1:32779/Wikimedia/core",
      "sandboxPath": "/home/agent/Wikimedia/core",
      "adopted": false
    }
  ],
  "skipped": [
    {"hostDir": "/home/cananian/Wikimedia/Parsoid", "reason": "readonly-bind"}
  ]
}
```

`skipped` is not needed for cleanup; it is there so `wmf-sbx-rm --dry-run`
and a future `wmf-sbx ls` can explain *why* a repo has no remote without
re-deriving it. `adopted: true` marks a remote that already existed and
that we re-pointed rather than created (§4).

`primaryDir` is the realpath'd host directory of the sandbox's *primary*
repo -- `sbx create`'s positional, not an extra. It exists so
`wmf-sbx-resume`/`-rm`/`-start`/`-exec` can take a filesystem-path
shortcut (`.`, `..`, an absolute path) in place of the sandbox's name:
`wmf_sbx.state.resolve_name_arg()` realpath's the argument and looks for
the one sandbox whose `primaryDir` matches, erroring out if none or more
than one does. A sandbox created before this field existed has no
`primaryDir` and is simply not reachable that way -- its plain name
still works.

`wmf-sbx-cp` uses the same lookup, but can't just call
`resolve_name_arg()` on its whole argument: `sbx cp`'s SRC/DST are
`SANDBOX:PATH`, not a bare name, so `wmf_sbx.cp.resolve_cp_arg()` splits
on the first `:`, resolves the NAME half alone when it looks path-shaped,
and reassembles it with the PATH half untouched.

This is the project's first persistent host state — §8's "Persistence"
requirement, arriving via §15's design instead of §8's outbox. Everything
`wmf-claude` has today is session-scoped and reaped on exit; this
deliberately is not.

## 3. Ownership marker: a git config key, not the URL

Cleanup must never delete a remote the engineer created or repurposed.
Matching on the recorded URL is the obvious check and is wrong: after a
`wmf-sbx-resume` re-publish the URL legitimately changes, so a
URL-mismatch would make us abandon our own remote and leak it forever.

Instead, when we add a remote we also set a marker in the *host repo's own*
config:

```
git -C <hostDir> config remote.sandbox-mw-cite.wmfSbxSandbox mw-cite
```

Cleanup removes a remote iff that key is present and equals the sandbox
name. That is a two-sided record — the state file says where to look, the
repo itself says "yes, this one is ours" — and it degrades safely in both
directions: a hand-edited or user-replaced remote loses the marker and is
left alone with a warning; a lost state file leaves markers behind that a
`--scan` fallback (§6) can still find. `git remote remove` drops the whole
`remote.<name>.*` section, marker included, so there is nothing extra to
clean.

## 4. `wmf-sbx-create`: adding the remotes

Replaces the `print_remote_add_reminder` call at the end of `main()`, after
`publish_daemon_port` returns a host port. New flag `--no-remotes` to opt
out (falls back to today's printed reminder), for engineers who would
rather manage their own config.

For each entry `parallel_tree_remotes()` produces:

1. **Skip `:ro` repos.** Their parallel path is a bind mount of the
   original (`wmf_sbx_setup.bind_into_parallel_tree`), so a remote there
   would point the host repo at *itself* over the loopback — pure
   confusion, no new objects ever. This is a behaviour change:
   `parallel_tree_remotes` today deliberately includes them, which was
   harmless when it only printed. It needs the `readonly_dirs` set passed
   in, and records them under `skipped`.
2. **Skip non-repos.** A raw-path extra that is a plain directory (`git -C
   DIR rev-parse --git-dir` fails) gets `skipped: not-a-git-repo`.
3. `git -C <hostDir> remote add --no-tags <remote> <url>`.
   **`--no-tags` is deliberate:** tags live in a single global namespace
   per repository, so a default fetch from a sandbox would import the
   sandbox's tags straight into the host repo's `refs/tags/*`, where they
   are indistinguishable from real ones. Branches land under
   `refs/remotes/sandbox-<name>/*` and are namespaced; tags are not.
4. On exit 3 (`error: remote <name> already exists`, verified), read
   `remote.<name>.wmfSbxSandbox`:
   - marker equals this sandbox name → stale entry from a same-named
     predecessor: `git remote set-url` to the new URL, record
     `adopted: true`;
   - marker absent or different → leave it alone, warn, record under
     `skipped` with reason `name-taken`. Do **not** silently steal a
     user-owned remote name.
5. `git config remote.<name>.wmfSbxSandbox <sandbox>` (§3).

Everything here is best-effort, exactly as `publish_daemon_port` already
is: the sandbox exists by this point and a config-lock contention or a
read-only repo is not a reason to fail the whole `create`. Warn, record
what succeeded, carry on. The state file is written **after** the loop,
listing only remotes actually added or adopted — so we can never try to
remove something we never created.

Ordering note: this runs after `sbx create` returns, which means the
`commands.install` step has completed and the daemon is up, so an optional
`git ls-remote` reachability probe against the first URL is available as a
cheap sanity check. Worth doing once, not per repo.

## 5. `wmf-sbx-rm`

New shim `sbx/bin/wmf-sbx-rm` + `sbx/bin/wmf_sbx_rm.py`, following the
existing `wmf-sbx-create`/`wmf_sbx_create.py` split. Like every other
entry point it shells out to `sbx/bin/wmf-sbx`, never to a bare `sbx`
(NOTES.md #8).

```
wmf-sbx-rm [--dry-run] [--keep-remotes] [--force] [--prune] NAME [NAME...]
```

**Order of operations.** Remove the sandbox first, remotes second — if
`sbx rm` fails we have not yet mutated a dozen host repos. But keying off
the exit code alone strands state when the engineer removed the sandbox
some other way, so the actual rule is: run `wmf-sbx rm NAME`, then
re-check `existing_sandbox_names()`. If `NAME` is gone — whatever the exit
code said — proceed to remote cleanup and delete the state file. If it is
still there, report the error and touch nothing.

That rule is doing real work, because the exit code carries almost no
information (confirmed on the host, 2026-09-07): removing a nonexistent
sandbox gives `Error: sandbox 'claude-Parsoid' not found (run 'sbx ls' to
see your sandboxes)` and **exit 1**, which is presumably also what any
other failure gives. `existing_sandbox_names()` is the source of truth.

**`sbx rm` prompts; let the prompt through.** It does *not* refuse on a
running sandbox — it asks `Remove sandbox 'NAME'? This cannot be undone.
(y/N):` and proceeds on `y` (confirmed on the host; the sandbox in that
transcript had just served an `exec`). So `wmf-sbx-rm` runs it with stdio
inherited rather than captured, and the irreversible confirmation comes
from the tool that actually does the destroying. That is deliberately the
same posture §13 item 3 takes toward `sbx template save`'s prompt — do not
script past it by piping `y`. `sbx rm --force` skips the prompt (confirmed
on the host, 2026-09-07), so `wmf-sbx-rm --force` passes it straight
through: one flag, meaning "I know what this destroys," disabling both our
guard (§ below) and sbx's confirmation. A separate `--yes` that skipped
only the prompt was considered and rejected — the guard is the stronger of
the two nets, so a flag that keeps the weaker one while removing the
stronger is a confusing thing to offer. Use the long `--force`; only that
spelling is verified, `-f` is not.

**Declining the prompt exits 0.** Answering `n` prints `Aborted.` and
returns **0** (confirmed on the host) — so the exit status is uninformative
in *both* directions: 1 can mean "already gone" and 0 can mean "the user
said no." This is the trap the §5 rule above exists to avoid, and it is a
genuine data-loss hazard rather than a cosmetic one: a `wmf-sbx-rm` that
believed exit 0 would strip every `sandbox-<name>` remote off the host
repos of a sandbox the engineer had just declined to delete, leaving it
running and unreachable. Only the post-hoc `existing_sandbox_names()`
check distinguishes the cases.

`--prune` needs none of this: it only handles sandboxes that are *already*
gone from `wmf-sbx ls`, so it never invokes `sbx rm` and never prompts.
Same for the opportunistic prune inside `wmf-sbx-create` — it cleans
host-side remotes and state only, and must never remove a sandbox
mid-create.

**Unfetched-commit guard.** §8's teardown requirement carries over, and
matters more here: under §15 the agent's commits live in the sandbox's own
overlay filesystem, and `sbx rm` is unrecoverable. So *before* removing,
for each recorded remote, `git ls-remote <url>` and check each returned tip
against the host repo (`git -C <hostDir> cat-file -e <sha>^{commit}`). Any
tip the host has never seen means unfetched work; refuse the whole
operation and name the repos, unless `--force`. If the daemon is
unreachable (sandbox stopped, or §15.4 has bitten), we cannot tell —
refuse by default too, with a distinct message pointing at
`wmf-sbx-resume`, since "can't check" must not read as "nothing to lose".
`--force` skips the guard entirely.

**Remote removal.** For each recorded remote: if `hostDir` is gone or is no
longer a git repo, skip silently (the repo was moved or deleted — that is
expected, not an error). Otherwise check the §3 marker and `git -C
<hostDir> remote remove <remote>`; a `No such remote` (exit 2, verified) is
also a silent success, because idempotence is the point. Then delete the
state file.

`--keep-remotes` removes the sandbox and leaves the remotes and the state
file, for the rare case of wanting to re-create under the same name.
`--dry-run` prints the full plan — sandbox, remotes, guard results — and
does nothing.

**`--prune`** (with no NAME) walks every state file, drops the ones whose
sandbox no longer exists, and cleans their remotes. This is the escape
hatch for the case that *will* happen: the engineer runs plain `sbx rm`
(or `sbx logout`, which stops everything) and never goes through
`wmf-sbx-rm` at all. `wmf-sbx-create` should call the same prune pass
opportunistically at startup — it already shells out for
`existing_sandbox_names()`, so the sandbox list is in hand for free, and
that is exactly when a recycled host port is about to become a hazard
(§1).

## 6. Later, not now

- ~~**`wmf-sbx-resume` integration.**~~ **DONE** 2026-09-08. It went in as
  `wmf_sbx_create.refresh_host_port(name, state)`: look the published port
  up (publishing again if there is none), rebuild the candidate URLs from
  each remote's recorded `sandboxPath`, and hand them to `sync_remotes`,
  which re-points anything carrying our marker — exactly the shape this
  section asked for, and the reason §3 keys ownership off a marker rather
  than the URL. `wmf-sbx-resume` calls it on every attach;
  `wmf-sbx-rm` calls it before the unfetched-work guard probes anything.
- **`--scan DIR` fallback** for a lost or corrupted state file: walk for
  `.git` directories and grep config for `wmfSbxSandbox` markers. Cheap to
  add, only worth it if state files actually go missing.
- **`wmf-sbx ls`**, from §8's requirement list: sandboxes, their repos, and
  unfetched commit counts — the guard in §5 already computes exactly that.

## 7. Files

| Path | Change |
| --- | --- |
| `sbx/bin/wmf_sbx_state.py` | new — state file path/validate/load/save/delete/list |
| `sbx/bin/wmf_sbx_remotes.py` | new — `sync_remotes`, `remove_remotes`, marker helpers |
| `sbx/bin/wmf_sbx_rm.py` | new — `wmf-sbx-rm` logic |
| `sbx/bin/wmf-sbx-rm` | new — shim, mirroring `wmf-sbx-create` |
| `sbx/bin/wmf_sbx_create.py` | `parallel_tree_remotes` takes `readonly_dirs`; `--no-remotes`; call `sync_remotes` + write state instead of `print_remote_add_reminder`; opportunistic prune |
| `sbx/bin/tests/test_wmf_sbx_state.py` | new |
| `sbx/bin/tests/test_wmf_sbx_remotes.py` | new |
| `sbx/bin/tests/test_wmf_sbx_rm.py` | new |

**Testability is unusually good here**, which is worth stating given how
much of §15 could only be verified on the host: the remote add/remove/marker
logic is plain git against throwaway repos in `tmp`, and the state layer is
plain JSON — both fully exercisable inside a sandbox with no `sbx` binary
present. Only the `wmf-sbx rm` call itself and the port lifecycle need
dependency injection (`run=subprocess.run`) and a host-side check, matching
how `existing_sandbox_names` and `publish_daemon_port` are already tested.

## 8. Open questions for the host

1. ~~Does `sbx rm NAME` on a running sandbox refuse, like `template save`
   does (§13)?~~ **No** (cananian, 2026-09-07): it prompts
   `Remove sandbox 'NAME'? This cannot be undone. (y/N):` and proceeds.
   Handled in §5 — inherit stdio, let the prompt reach the terminal.
2. ~~Exit code for a nonexistent sandbox?~~ **1**, with
   `Error: sandbox 'NAME' not found (run 'sbx ls' to see your sandboxes)`
   (cananian, 2026-09-07). Almost certainly the generic failure code, so
   §5's "re-check `existing_sandbox_names()`" rule stays as written rather
   than branching on it. **And declining the prompt exits 0** — so the
   status is ambiguous in both directions and must not be trusted at all.
3. ~~Is the port-recycling hazard real?~~ **Yes** (cananian, 2026-09-07).
   This is now a confirmed premise of §1, not an inference — reliable
   cleanup is a correctness requirement.
4. ~~Does `sbx rm` have a force / assume-yes flag?~~ **Yes, `--force`**
   (cananian, 2026-09-07): `wmf-sbx rm --force NAME` deletes with no
   prompt. `wmf-sbx-rm --force` passes it through (§5). Whether `-f` also
   works is untested — use the long spelling.
