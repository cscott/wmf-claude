# NOTES.md — Phase 0 findings

## 0. Orientation for a fresh Claude instance picking this up

cananian is replacing `wmf-claude`'s nono-based sandboxing with a launcher
built on Docker's `sbx` ("Docker Sandboxes"). Goals: (a) a low-friction
single command to spin up a clean multi-repo MediaWiki dev sandbox, (b)
carry over `wmf-claude`'s MCP servers/skills, (c) track security posture
vs. nono, (d) make it usable by other WMF engineers, not just cananian's
own directory layout. Staged as **Layer A** (single-command clean-checkout
launcher) → **Layer B** (multiple push-back-capable clones per sandbox) →
**Layer C** (MediaWiki dependency-aware provisioning). **All three are
built, and all three have been run end to end in a real sandbox** (§30,
§32, §37). §1–13 are the earlier Phase-0 exploratory findings that
motivated the design; §14 onward and "Still to do" (end of file) are the
live status; §§30–46 are the real-sandbox measurements.

### Where things stand

`wmf-sbx-create Cite` (or any repo) produces a working wiki plus a live
MCP path to Phabricator and Gerrit (§73). The plugin ships in the
generated kit as a marketplace (Route A, §64); the MCP servers run **on
the host** behind `wmf-sbx-mcp-proxy` (Route 1, §65/§72/§73), which is
why no credential is ever inside the sandbox.

`sbx/bin/` (thin shims; the code is `sbx/src/wmf_sbx/*.py`):

| file | what it is |
|---|---|
| `wmf-sbx` | the mandatory wrapper (strips `SSH_AUTH_SOCK`, blocks `--cloud`; §8) |
| `wmf-sbx-create` | Layer A: resolve → clone → kit → `sbx create` → publish port → host remotes → read back the setup log |
| `wmf-sbx-setup` (inside the sandbox) | the in-sandbox half: parallel tree, `layout.json`, git daemon, MediaWiki setup, `--restore`, `--verify [--wait=N]` |
| `wmf-sbx-resume` | attach to an existing sandbox; waits for the layout |
| `wmf-sbx-run` | `--name NAME` dispatches to `wmf-sbx-resume`; otherwise errors out pointing at `wmf-sbx create` |
| `wmf-sbx-start` | restore a sandbox's mounts/remotes with no agent attached |
| `wmf-sbx-exec` | `wmf-sbx-start`'s restore, then a one-off command |
| `wmf-sbx-rm` | remove a sandbox and tear down its host remotes |
| `wmf-sbx-resolve` | repo-name → canonical path → local directory |
| `wmf-sbx-ls-remotes` | list the host-side `sandbox-*` remotes |
| `git-safe-reset`, `git-review-check` | git repo helpers for host and remote |

`sbx/helpers/` (scripts that run *inside* a sandbox, so deliberately not
on the host's `PATH`; the kit copies them in):

| file | what it is |
|---|---|
| `wmf-sbx-mcp-proxy` | the in-sandbox stdio shim for the host's MCP gateway (Route 1) |
| `wmf-sbx-gateway-tools` | inspect/diagnose the gateway's flat tool namespace (§62) |

Docs: this file (the findings log — numbered, dated, attributed),
`SECURITY.md` (the sbx
threat model — the **repo-root** `SECURITY.md` is about the *nono*
backend and its claims do not transfer), eleven `DESIGN-*.md` files, and
`reference/` (the upstream-bug write-ups in `reference/upstream/`, plus
the persistent-memory seed in `reference/claude-memory-seed/`).

**If the dev sandbox is about to be destroyed and recreated** (an sbx
upgrade, a host reinstall, …), write a sibling `sbx/RESUME.md` first — the
session state this file doesn't carry: what's in flight, what to
re-measure after whatever forced the recreate, plus the exact
`wmf-sbx-create` line from "Recreating this dev sandbox" below. Fold it
back into this file (and persistent memory)
once the recreate is done and measured, then delete it. That's happened
three times: the §8 SSH-forwarding recreate (written, folded in, and
removed 2026-09-06), the 0.39.0 → 0.42.1 upgrade (written 2026-09-08; the
re-measurement is §47; folded back in and removed 2026-09-11), and the
0.42.1 → 0.43.0-rc3 upgrade (written 2026-09-14; the re-measurement is
§83–§84; folded back in and removed 2026-09-15). One more, for the
v0.43.0 upgrade (written 2026-09-18, §96; removed 2026-09-19, when the
next check was deferred to the next sbx release). No `sbx/RESUME.md` in
the tree just means nothing is in flight right now — not that the
pattern was abandoned.

### Restoring persistent memory into a freshly created sandbox

A fresh sandbox gets a fresh, empty `~/.claude` — none of the prior
instance's persistent memory files carry over (this is expected; see
`sbx/DESIGN-memory-persistence.md`). A snapshot of that memory as of
2026-09-14 is committed at `sbx/reference/claude-memory-seed/` — nine
memory files plus the `MEMORY.md` index. To restore it, from inside the
new sandbox:

```bash
mkdir -p ~/.claude/projects/-home-cananian-Projects-Wikimedia-wmf-claude/memory
cp sbx/reference/claude-memory-seed/*.md \
   ~/.claude/projects/-home-cananian-Projects-Wikimedia-wmf-claude/memory/
```

(Skip `README.md` if you'd rather not carry that explanatory note into
the live memory directory — it's not a real memory file, just context for
whoever's restoring.) Then re-read `MEMORY.md` as usual; it points at the
other files. If cananian asks you to update memory during a session, do it
in the live `~/.claude/...` location as normal — the seed directory in the
repo is a point-in-time copy, not something to keep syncing live. If a
future session makes further changes worth carrying forward, refresh the
seed directory the same way this one was created: copy the current
`~/.claude/projects/<key>/memory/*.md` files over and commit.

### Recreating this dev sandbox

This is the *dev* sandbox — the one for working on `wmf-claude` itself, not
a MediaWiki dev sandbox — so it wants `--no-deps`. Run on the **host**,
after `cd ~/Projects/Wikimedia/wmf-claude`:

```bash
sbx/bin/wmf-sbx-create --name wmf-claude-sbx --no-deps \
  ~/Projects/Wikimedia/wmf-claude \
  ~/Projects/Wikimedia/core:ro \
  ~/Projects/Wikimedia/Skins:ro \
  ~/Projects/Wikimedia/Extensions:ro \
  ~/Projects/Wikimedia/Parsoid:ro \
  ~/Projects/Wikimedia/mediawiki-config:ro \
  ~/Projects/Wikimedia/integration-config:ro \
  ~/Projects/Wikimedia/docs.docker.com:ro
```

Trimmed from the original hand-written `sbx create` line below (per
cananian): `~/bin`, `mediawiki-core-clean`, and `mediawiki-kit` are all
dropped — `mediawiki-kit` in particular is now *generated*
(`wmf_sbx_kit.py`), so mounting the hand-maintained one would be exactly
the thing this replaced.

What each piece buys you:

- **`wmf-claude` is the primary and is not `:ro`**, so it becomes a
  writable clone in the parallel tree, aliased over
  `~/Projects/Wikimedia/wmf-claude` inside the sandbox — the starting cwd
  *is* the writable clone, nothing has to `cd` (§39). The host mirror is
  moved aside to `~/.sbx-originals/` and remounted read-only.
- **Everything else is `:ro`**, which under §15's default means a plain
  read-only bind mount of the host directory rather than a clone —
  reference material, not things to edit.
- **`core:ro` deliberately skips the whole MediaWiki setup** —
  `mediawiki_setup()` is gated on core being present *and writable*, so a
  read-only core means no `composer update`, no npm, no wiki install.
  That's the point in a dev sandbox: minutes saved, nothing to go wrong in
  a phase this sandbox doesn't use.
- **`--no-deps`** stops the dependency walk from pulling in implicit
  core/Vector — without it the walk fails outright here (`No config rule
  matches 'gerrit:mediawiki/skins/Vector'`) unless a `repos.yaml` rule
  covers it.
- **No `--no-remotes`**: the host-side `sandbox-wmf-claude-sbx` remote is
  how cananian reviews the work, so it stays on.
- **No `--no-mcp` either.** MCP is on by default and works end to end
  (§73): the create registers the Phabricator and Gerrit servers on the
  host and wires `mcp-gateway` into the sandbox. It needs node ≥ 20 on
  the host, which `wmf-sbx-create` preflights (§68).
- **`--skills=off` is added automatically** if the installed binary has
  it (`supported_skills_flag`, §82.1). You do not type it; you *verify*
  it — see §83's mount-absence measurement for what that looks like.

`--dry-run` first if anything looks off — it prints the resolution plan,
the generated kit YAML, and the exact `sbx create` line without touching
disk. Afterwards, `wmf-sbx-resume wmf-claude-sbx` is the way back in; a
bare `sbx run --name` does not wait for the layout.

### Notes on working in this repo

- cananian rebases away intermediate exploratory commits before anything
  goes upstream — frequent small commits during design/research are fine.
- PyYAML is the only runtime dependency (config loading only). Not
  preinstalled in a dev sandbox; `pip install --user` fails with
  "externally-managed-environment" — use
  `pip install --break-system-packages --target=<scratch-dir> pyyaml` and
  `PYTHONPATH=<scratch-dir>` to run the full test suite including the
  YAML-dependent cases. (cananian note: strongly prefer installing a
  package from apt rather than `pip install` where possible; you have
  `sudo` in this sandbox.)
- **Never commit `responses*.txt` from the repo root.** They are
  cananian's pasted host-command transcripts, untracked on purpose —
  gitignored at the repo root (`/responses*.txt`) so an `add -A` can't
  catch them by accident. They will not survive the next sandbox
  recreate; anything load-bearing from one gets distilled into a `NOTES.md`
  section, with excerpts, at the time.

### Durable non-goals

Carried forward from the project's original design, still in force:

- Do not fork `skills/` or `agents/`. If a skill does not work under
  `sbx`, fix the environment (as with the `mwdocker` shim) or propose an
  upstream change. A forked skill tree defeats the purpose of the
  project.
- Do not build a second sandbox backend for macOS versus Linux — `sbx`
  runs on both.
- Do not modify `profiles/wmf-engineer.json`. It is the nono backend's
  source of truth; this project reads it (`wiki_family_domains()`) but
  never edits it.
- Do not weaken the supply-chain checks in `bin/wmf-claude-build`
  (`npm ci --ignore-scripts`, `--require-hashes` for pip, the
  one-top-level-package invariant on `chrome-devtools-mcp`).

Recorded from *inside* the `wmf-claude-sbx` sandbox this plan itself was drafted
in, launched on the host with:

```bash
sbx create --name wmf-claude-sbx --kit $HOME/Wikimedia/mediawiki-kit \
  --clone claude `realpath $HOME/Wikimedia/wmf-claude` \
  `realpath $HOME/Wikimedia/core`:ro \
  `realpath $HOME/Wikimedia/mediawiki-core-clean`:ro \
  `realpath $HOME/Wikimedia/Skins`:ro \
  `realpath $HOME/Wikimedia/Extensions`:ro \
  `realpath $HOME/Wikimedia/Parsoid`:ro \
  `realpath $HOME/Wikimedia/mediawiki-config`:ro
```

Items below were originally numbered against the implementation plan's §5
(since folded into other documents and removed). Findings marked
**[verified in-sandbox]** were checked directly by the agent from inside this
session — no host round-trip needed. Findings marked **[human]** still need
the host `sbx` CLI / docs.

## 1. Absolute host paths — CONFIRMED **[verified in-sandbox]**

Every non-primary `--clone ...:ro` argument appears inside the sandbox at
its **exact resolved host path**, mounted `virtiofs`, `ro,relatime`:

```
/home/cananian/Projects/Wikimedia/core                 (ro)
/home/cananian/Projects/Wikimedia/mediawiki-core-clean (ro)
/home/cananian/Projects/Wikimedia/Skins                (ro)
/home/cananian/Projects/Wikimedia/Extensions           (ro)
/home/cananian/Projects/Wikimedia/Parsoid              (ro)
/home/cananian/Projects/Wikimedia/mediawiki-config     (ro)
```

This is why the launch command wraps every path in `` `realpath ...` ``: on
this host, `~/Wikimedia` is a **symlink** to `~/Projects/Wikimedia`, and `sbx`
does not resolve symlinks in bind-mount source paths — it mounts what you
literally pass. `realpath` collapses the symlink before `sbx` ever sees it, so
the mount lands at the same absolute path on both sides. Any wrapper CLI we
write **must** `realpath` every `--clone` argument itself, or silently produce
broken mounts on any host that uses this kind of convenience symlink.

Confirms the Phase 3 premise: `git clone --reference <that-path> <that-path>
<dest>` recorded inside the sandbox will resolve identically on the host. No
`--dissociate` needed.

## 2. RW vs RO mounts, flag syntax — CONFIRMED (corrected from first pass)

`docs.docker.com/ai/sandboxes/usage.md` (the plain `.md` document endpoint —
see §11 — got past the JS-shell that blocked the first pass) gives the real
grammar. Correcting an earlier misreading of the launch command:

- `--clone` is a **boolean flag**, not a flag that takes a group name. It
  puts the sandbox's **first** workspace into "clone mode." The word right
  after it (`claude`, in our launch command) is the **agent to run**, a
  normal positional argument (`sbx run claude`, `sbx run --clone claude`),
  not a name for the clone group.
- "Clone mode" (`--clone`, one workspace only): `sbx` bind-mounts the host
  repo read-only at `/run/sandbox/source`, then runs
  `git clone --reference /run/sandbox/source /run/sandbox/source
  $WORKSPACE_DIR` onto the VM's own overlay filesystem (read-write). On the
  **host** side, this adds a live `git-daemon` remote to the source repo:
  `sandbox-wmf-claude-sbx → git://127.0.0.1:<ephemeral-port>/wmf-claude`.
- "Direct mode" (the default, no `--clone`): the workspace is a read-write
  bind mount of the host tree — "edits the host working tree in place,"
  "changes in either direction are instant with no sync process." No git
  indirection at all.
- Every **extra** workspace path after the first (`sbx run claude
  ~/project-a ~/shared-libs:ro ~/docs:ro`) is a **plain bind mount**,
  independent of whatever mode the first workspace uses: read-write by
  default, read-only with a trailing `:ro`. There is no way to put an extra
  workspace into clone mode — only the first/primary workspace can be a
  clone. This is the load-bearing confirmation for Phase 3: `sbx` has no
  built-in notion of more than one push-back-capable workspace per sandbox.
  Anything beyond the first is either a live shared mount (direct, risky —
  edits land on the host tree immediately, dirty state and all) or a dead
  end (`:ro`, no way back out except manual copy).

This also means: if a wrapper ever mounts a secondary repo *without* `:ro`,
it is not "read-write clone" — it is **the exact same file, live, no
buffering** — the sandbox and the host share one inode. Worth calling out
explicitly in any wrapper's `--help`, since it is easy to reach for by
habit and very different from what "clone mode" gives the primary
workspace.

## 3. `sbx policy` granularity — CONFIRMED **[human docs]**

Closed via cananian's `wget -r -p -np -x -k` mirror of
`docs.docker.com/reference/cli/sbx/` (its `index.html` files are
server-rendered and readable; the `.md` sibling files at this depth are
empty stubs — worth remembering for any future doc fetch attempt).

`sbx policy allow network [--sandbox SANDBOX] RESOURCES` — confirmed
**domain-only**: "hostnames, domains, or IP addresses... exact domains
(`example.com`), wildcard subdomains (`*.example.com`), and optional port
suffixes (`example.com:443`)." `**` allows all hosts. No path/method
granularity exists at all — matches the plan's default assumption exactly.

Also newly confirmed:
- `sbx create`/`sbx run --deny-network HOST` — a **per-sandbox, additive-only**
  deny rule at creation time ("can only narrow, never widen, egress" —
  explicitly called out as safe under centralized governance).
- `sbx policy ls [SANDBOX] [--source local|org|kit] [--decision allow|deny]
  [--type all|network|filesystem] [--wide] [--json]` — policies have a
  **source** dimension (`local`, `org`, `kit`), meaning the kit's own
  `network.allowedDomains` (`mediawiki-kit-spec.yaml`) shows up as a
  first-class `kit`-sourced policy alongside `local` and `org` ones, not a
  side-channel outside `sbx`'s policy model as §9 guessed. There is also a
  **`filesystem`** policy type, distinct from `network` — not explored at
  all yet, worth a follow-up since the plan hasn't considered filesystem
  policy as a lever.
- `sbx policy log [SANDBOX] [--type all|network|filesystem] [--limit N]
  [--json]` — `filesystem` logs are explicitly called out as "not supported
  yet," so today this only tells you about network egress.

## 4. Custom template images (`sbx run -t` / `sbx template`) — CONFIRMED **[human docs]**

- `sbx template save SANDBOX TAG [-o FILE]` snapshots a **stopped** sandbox's
  container into the local image store (optionally exported to a
  shareable tar); `sbx template load FILE` imports that tar on another
  host. `sbx run -t TAG AGENT [WORKSPACE]` then boots a new sandbox from
  that saved image instead of the default agent-specific one.
  **CORRECTION (2026-09-06, verified against a real `sbx` binary):** this
  originally said "running sandbox" — wrong. `template save` on a running
  sandbox produces an *interactive* `Sandbox ... is running and must be
  stopped before saving. Stop it now? (y/N):` prompt with no flag to skip
  it; the sandbox must actually be stopped first (`sbx stop SANDBOX`, which
  has no such prompt) to save non-interactively. Also confirmed: a saved
  template is a pure base filesystem image — it carries whatever's on disk
  inside the container (including, notably, the entire `--clone` workspace's
  content, not just what `commands.install` did — see the multi-clone design
  discussion), but **not** environment variables, network policy, or
  bind-mounted content, all of which are create-time-only and must be
  respecified (`-e`, `sbx policy allow network --sandbox`, and the mount
  flags respectively) when creating from the template. Full writeup with the
  test methodology: `sbx/DESIGN-template-caching.md` §6 items 1, 3, 7.
- `sbx kit add SANDBOX REFERENCE` (experimental) can append a kit to an
  **already-running** sandbox — the container is recreated with the kit
  layered on, preserving kit-owned volumes and the workspace (bind mount or
  `--clone` working tree). Requires the sandbox to have been created with a
  "recreate-aware" label; older sandboxes are refused with a clear error.
  `sbx kit pull REFERENCE` pulls a kit artifact from an OCI registry to a
  local file.
- Net effect for Phase 2: a permanent "MediaWiki base" could be built once
  as an `sbx template`, referenced by `-t`, with `mediawiki-kit-spec.yaml`-style
  kits layered per-sandbox on top via `--kit` at creation (or `sbx kit add`
  after the fact) — matching the plan's template-vs-kit split, now backed
  by real command syntax instead of a guess.

## 5. Host-side socket/TCP reachability — CONFIRMED, and a bigger finding than expected **[verified in-sandbox]**

**This works today, unannounced, for SSH agent forwarding**, which answers
Phase 4's transport question but also raises a compliance question (see §8):

- `SSH_AUTH_SOCK=/run/ssh-agent.sock` inside the sandbox, backed by
  `SSH_AUTH_SOCK_GATEWAY=gateway.docker.internal:3129`.
- `ssh-add -l` through that socket lists the **host user's real keys**
  (`id_wmf`, `id_github`, plus personal `skiffserv` keys) — i.e. this is a
  live forwarded agent, not a sandboxed/ephemeral one.
- `ssh -T git@github.com` authenticates successfully as the host user.
- `ssh -T -p 29418 <user>@gerrit.wikimedia.org` (the actual `git-review`
  port) completes a TCP connection and SSH handshake, then fails at
  `Permission denied (publickey)` — i.e. **the network path to Gerrit's SSH
  port is open**; only key material stopped it, not policy.
- `ssh -p 22 gerrit.wikimedia.org` times out — that port is genuinely
  unreachable (unrelated to the 29418 result).

So: (a) Phase 4's premise ("can a process reach a host-side socket?") is
proven true, via the exact gateway-proxy pattern (`gateway.docker.internal:PORT`
→ an in-sandbox Unix socket) that a future host-side MCP server transport
could reuse. (b) The root README's "`git review` can't run inside a session,
push from a normal shell" premise is **only half true**: it's an
authentication gap today, not a network one. If the right key were loaded in
the forwarded agent, `git review` might work directly from inside `sbx`.

## 6. `wmf-claude-build` with no `nono` on PATH — CONFIRMED (partial)

`which nono` → not found; `nono` is absent from `PATH` in this sandbox. This
validates the Phase 1 Seam 3 premise that nothing *needs* nono to be present.
Not yet run: actually executing `bin/wmf-claude-build` end-to-end here to
confirm it completes (still TODO, low-risk, doable in-sandbox next).

## 7. Exhaustive `allow_domain` list from `profiles/wmf-engineer.json` — TODO

Not yet extracted. Straightforward next step, no blockers.

## 8. NEW — SSH agent forwarding is on by default: a compliance problem, not just a design input

Not a Phase-0 question originally, but the finding in §5 needs to be treated
as a **security finding**, not just a capability confirmation:

- WMF's `Secure_use_guidelines` (`sbx/reference/Secure_use_guidelines.wt`)
  states plainly: sandboxes "must not be granted access to staff accounts
  and/or elevated privileges (e.g. on-wiki rights, ... phab/gerrit accounts,
  **SSH access**)." The same page later cites `docker sbx` itself, favorably,
  as a tool that "can interact with your filesystem as a git remote" —
  written without apparent awareness that its default agent-forwarding
  behavior conflicts with the SSH-access line two paragraphs up.
- The user (cananian) independently confirmed this is *not* an intentional
  choice on their part: "I'm actually quite surprised that ssh forwarding
  works: my personal preference is that all keys are held outside the
  sandbox."
- `docs.docker.com/ai/sandboxes/security/` (fetched) describes credential
  handling only for HTTP: "the host-side proxy injects authentication
  headers into outbound HTTP requests. The raw credential values never enter
  the VM." **It does not mention SSH agent forwarding at all** — this may be
  an underdocumented default rather than a deliberate, reviewable feature.
- `docs.docker.com/ai/sandboxes/configuration/` mentions a `.sbxenv.yaml`
  "Environment Files" section and a "Credentials" section as nav entries,
  which is the most likely place such forwarding is toggled — content not
  reachable via WebFetch (JS-rendered), so this needs the host CLI/docs
  directly: **does `sbx create` have a flag or `.sbxenv.yaml` key to disable
  SSH agent forwarding, and what is it?**

**Resolved, partially**: `docs.docker.com/ai/sandboxes/configuration/credentials.md`
(the `.md` endpoint got past the JS shell that blocked the plain URL) confirms
this is a **documented, intentional default**, not an undocumented leak:

> "If your host has an SSH agent running and `SSH_AUTH_SOCK` is set, Docker
> Sandboxes forwards the agent into the sandbox." Private key material never
> leaves the host; the sandbox can only ask the host agent to sign challenges.

No disable flag or `.sbxenv.yaml` key for this is documented anywhere in the
`configuration/`, `usage/`, or `security/` pages fetched so far. The
forwarding is conditioned purely on whether `SSH_AUTH_SOCK` is set in the
shell that invokes `sbx create`/`sbx run` — there is no separate "sandbox
opts out" control. **Proposed mitigation, untested**: have any `wmf-sbx`
wrapper invoke `sbx` with `SSH_AUTH_SOCK` explicitly unset, e.g.
`env -u SSH_AUTH_SOCK sbx create ...`, so the forwarding precondition is
never true regardless of the operator's normal shell state. This can't be
verified from inside the current sandbox — `sbx` isn't invokable from
inside its own sandbox — so it needs a host-side test: run
`env -u SSH_AUTH_SOCK sbx create --name test-nossh ...`, then from inside
that sandbox check `echo $SSH_AUTH_SOCK` (expect empty) and `ssh-add -l`
(expect "Could not open a connection to your authentication agent" rather
than the host's real key list).

Recommend this become a standing item in `sbx/SECURITY.md` (§7.4 of the
plan) regardless of how Phase 0 item 3/4 resolve: state explicitly whether
agent forwarding is on by default, whether/how it can be turned off, and
until it can be, flag it as a **known deviation from WMF secure-use policy**
that any `wmf-sbx` wrapper must either close or surface loudly to the user
per-invocation.

**Update 2026-09-05 — the per-invocation `SSH_AUTH_SOCK` strip in
`wmf-sbx-create` was tested for real on the host and is NOT sufficient on
its own.** cananian ran the §14 launcher without `--dry-run`; the
resulting sandbox still had a live forwarded agent. Upstream confirms this
is a known, still-open bug, not a misunderstanding of the docs:
[docker/sbx-releases#305](https://github.com/docker/sbx-releases/issues/305)
(dups [#115](https://github.com/docker/sbx-releases/issues/115),
[#121](https://github.com/docker/sbx-releases/issues/121)) — a Docker
maintainer (`kiview`) said on 2026-07-03 forwarding "will be made
opt-in," but as of this writing all three issues are still open and no
opt-in flag exists. Root cause, as best understood: forwarding is not
scoped per `sbx` invocation but is sticky to whatever environment the
long-running `sbx` daemon holds — and that daemon can be started
implicitly by *any* `sbx` subcommand, not just `sbx create`. Stripping
`SSH_AUTH_SOCK` only around the one `sbx create` call `wmf-sbx-create`
makes does nothing if the daemon was already running (or gets started
later by some other bare `sbx` invocation) with the socket set.

The only reliable recovery cananian found was to kill and restart the
daemon with the socket guaranteed absent throughout:
`sbx daemon stop` (which unfortunately kills every running sandbox
without letting it save state — a real cost, not a free operation), then
every subsequent `sbx` command run as `SSH_AUTH_SOCK= sbx ...` (or
equivalently `env -u SSH_AUTH_SOCK`) so the daemon and every client
invocation forever afterward stay clean.

**Fix landed**: `sbx/bin/wmf-sbx` is now the single required entry point
for every `sbx` invocation, not just `sbx create`. It unsets
`SSH_AUTH_SOCK` and `exec`s the real `sbx` binary, with a self-recursion
guard in case a `sbx=wmf-sbx` alias resolves back to itself on `PATH`.
`wmf_sbx_create.py`'s `build_sbx_command` now calls this wrapper (its
sibling in `sbx/bin/`) instead of `sbx` directly; the old `env.pop(...)`
before `execvpe` is kept only as defense in depth. **Any documentation or
tooling that tells a user to run `sbx` must say `wmf-sbx` instead** —
including one-off manual commands, since a single stray bare `sbx`
invocation (even just `sbx daemon status` or `sbx policy ls`) can retaint
a clean daemon if run with `SSH_AUTH_SOCK` set. Users should add `alias
sbx=wmf-sbx` (pointing `PATH` at `sbx/bin/`) to their shell rc as a
habit-safety-net; docs should say so in bold. Prior art for this pattern:
[AlexanderMattTurner/agent-glovebox@b4ee4de](https://github.com/AlexanderMattTurner/agent-glovebox/commit/b4ee4de348c7f13089df009dc57d2fe90f5ed969),
which does the same env-strip-and-exec wrapper for the same upstream bug.
Still TODO: re-verify end-to-end against a real `sbx` daemon that was
started clean and has only ever been driven through `wmf-sbx` (the test
above already proved the wrapper itself strips the var correctly against
a fake `sbx`; what's unverified is the daemon staying clean over a full
session of real use).

**Update 2026-09-05 — end-to-end re-verification PASSED.** cananian
reported doing "a number of operations all using wmf-sbx, including a
complete session" (a prior Claude instance's full working session) before
this sandbox (`wmf-claude-sbx`) was created — i.e. exactly the sustained,
`wmf-sbx`-only usage pattern the still-open item called for. Checked from
inside the resulting sandbox:

```
$ echo "SSH_AUTH_SOCK: [${SSH_AUTH_SOCK}]"
SSH_AUTH_SOCK: []
$ ssh-add -l; echo "exit code: $?"
Could not open a connection to your authentication agent.
exit code: 2
```

Also confirmed no leftover trace anywhere else forwarding could hide: no
`ssh-agent` process running, no `IdentityAgent` set for any host (`ssh -G
github.com`), no `~/.ssh/config`, and no SSH-related exports in
`/etc/sandbox-persistent.sh`. The daemon was not retainted across the
prior full session's worth of `wmf-sbx`-only commands. This closes the
last open item from the 2026-09-05 fix — the `wmf-sbx` wrapper is
confirmed sufficient in real sustained use, not just for a single
`create` call.

## 9. Other findings not in the original Phase 0 list

- **Docker Engine is native, not brokered.** `docker version` shows a full
  client+server (v29.7.2) already running inside the sandbox with no visible
  broker process. This directly confirms §7.3's premise that
  `bin/launch-docker-broker` (547 lines of privileged host-side Python) is
  unnecessary under `sbx` — `mwdocker` can be a thin `docker compose exec`
  shim.
- **`sudo` is passwordless** inside the sandbox (uid 1000 `agent`, groups
  `sudo`, `docker`). Confirms the "disposable root" premise in §1.
- **OS is Ubuntu 26.04** ("Resolute Raccoon") in this VM, not Debian trixie —
  worth deciding explicitly in Phase 2 whether to match this or use
  `docker-registry.wikimedia.org/trixie` as the plan currently prefers; this
  VM's base image is presumably `mediawiki-kit`'s base, not something we
  chose.
- **`spec.yaml` (`sbx/reference/mediawiki-kit-spec.yaml`) is the "kit"
  referenced by `--kit $HOME/Wikimedia/mediawiki-kit`.** It is a `mixin`
  (schemaVersion 1) that layers onto whatever base `sbx` uses, and is the
  actual source of today's `apt-get install` list (php + extensions,
  composer, `git-review`, `php-wikidiff2`, imagemagick/librsvg2, `nano`),
  the `network.allowedDomains` list (all WMF wiki-family domains plus plain
  `github.com`), and MediaWiki-specific env vars (`MW_INSTALL_PATH`,
  `PARSOID`, `MW_VENDOR_REPO`, `MW_CORE_REPO`, `GERRITUA`). Two things worth
  flagging to the user:
  - `MW_INSTALL_PATH` points at `mediawiki-core-clean`, not `core` — i.e.
    the actual "run this to serve/test" MediaWiki checkout is the clean one,
    while `core` (also mounted) is presumably the dev checkout under active
    patching. `MW_INSTALL_PATHX` (note the `X`) points at `core` and looks
    like a disabled/parked variable.
  - `MW_VENDOR_REPO` points at `.../mediawiki-vendor`, which is **not** one
    of the `--clone` targets in the launch command — either an oversight, or
    vendor is expected to be installed via composer inside the sandbox
    rather than mounted from host.
  - This kit's `network.allowedDomains` is a **second, independent**
    network-policy surface, distinct from both nono's
    `profiles/wmf-engineer.json` and whatever `sbx`'s own default policy is.
    Any policy-generation work (plan §7.2) needs to account for the fact
    that under `sbx`, network policy may be composed from the kit *and* the
    sandbox's own policy, not from a single source of truth.
- **`claudebox`** (gitlab.wikimedia.org/daniel/claudebox, fetched via
  WebFetch) is a plain Docker-container wrapper (not a VM/hypervisor
  sandbox): project dir mounted as workdir, a persistent `~/.claudebox`
  profile dir mounted as `$HOME` for credential/config persistence across
  runs, and a host-side `tools/sorun-listen` unix-socket listener that lets
  the container run arbitrary host shell commands — exactly the pattern
  root `SECURITY.md` already warns against emulating ("take the broker's
  discipline, not `sorun`'s" — the design principle behind
  `bin/launch-docker-broker`).
  Nothing here suggests a technique worth borrowing over what `sbx` already
  provides; it's a lighter-weight, less-isolated alternative aimed at a
  different tradeoff (container, not microVM).

## 10. Gerrit project-name resolution — feasibility CONFIRMED **[verified in-sandbox]**

Checked whether `gerrit.wikimedia.org`'s REST API can support the
"fuzzy-match a bare name like `Cite` against the final `/`-separated path
component" design cananian proposed, without needing any WMF auth:

- `https://gerrit.wikimedia.org/r/projects/?d` is reachable **unauthenticated**
  from inside the sandbox (this domain is in `mediawiki-kit-spec.yaml`'s
  `allowedDomains` list). Response is prefixed with a `)]}'` XSSI-guard line
  that must be stripped before JSON parsing. A bare fetch returns ~500
  entries (`3d2png`, `AhoCorasick`, ...) — almost certainly a default
  page-size cap, not the true project count; pagination (`?S=`/`?n=`) not
  yet explored.
- `?m=Cite` (Gerrit's own substring match) reproduces **exactly** the
  ambiguity cananian predicted: `mediawiki/extensions/Cite` (ACTIVE),
  `mediawiki/extensions/CiteDrawer` (ACTIVE),
  `mediawiki/extensions/CiteThisPage` (ACTIVE), and
  `mediawiki/extensions/SemanticCite` (READ_ONLY) — because `m=` matches
  the substring anywhere in the full path, not just the final component.
  This confirms Gerrit's own substring search is **not** sufficient on its
  own and any wrapper needs to do its own final-path-component filtering
  over the fetched list (i.e.: fetch candidates via `?p=mediawiki/` or
  similar prefix, then in application code keep only entries whose
  `name.split('/').pop() === query`).
- `?p=<exact-path>` (exact prefix match) correctly returns exactly one
  project for each of `mediawiki/core`, `mediawiki/services/parsoid`, and
  `mediawiki/skins/MinervaNeue` — confirming that typing a full canonical
  Gerrit path (cananian's disambiguation escape hatch) resolves
  deterministically. Response objects include `name`, `parent` (namespace
  ACL parent, e.g. `mediawiki/services/parsoid` → parent
  `mediawiki/services`), `state` (`ACTIVE`/`READ_ONLY`), and `web_links`.

Net: no blocker to building the resolver as designed. Remaining
implementation questions (not blocked, just undecided): whether to fetch
the full project list once and cache it locally per-sandbox-creation, or
query narrower prefixes (`?p=mediawiki/extensions/`,
`?p=mediawiki/skins/`, `?p=mediawiki/services/`) per lookup; and how to
merge results across those three prefixes plus bare `mediawiki/core` for a
single final-path-component search.

## 12. `sbx mcp` — relevant to goal (b), importing wmf-claude's MCP servers

`sbx mcp add <name> (--url <url> | --command <cmd>)` registers an MCP
server (by remote URL, MCP community-registry URL, server-manifest URL, or
Docker Hardened Image ref) for use with `sbx create/run --static-mcp`;
`sbx mcp load` attaches an already-registered server to a running sandbox.
Has an SSRF guard rejecting private/RFC1918/loopback/link-local/cloud-metadata
hosts by default (with a `--skip-ssrf-check` escape hatch for legitimate
internal-network servers). This is the natural path for wiring
`wmf-claude`'s `mcp-phabricator`/`gerrit-mcp-server` submodules into an
`sbx`-based sandbox — not yet attempted, flagged here as the concrete
starting point when goal (b) work begins.

## 13. Meta: how to fetch docs.docker.com content when WebFetch can't

Two tiers found so far, both worth trying before giving up on a page:

1. Append `.md` to the URL (e.g. `docs.docker.com/ai/sandboxes.md`) —
   works for most top-level pages, fails (empty stub) for deep CLI
   reference pages like `reference/cli/sbx/create.md`.
2. Ask cananian to run `wget -r -p -np -x -k <url>` on the host and mount
   the result in — this pulls the real server-rendered `index.html` for
   every linked page (confirmed non-empty even where tier 1's `.md`
   sibling is a stub), at the cost of a lot of navigation-chrome noise
   that has to be stripped out programmatically (see the `python3 -c
   "...re.sub(r'<[^>]+>', ...)..."` extraction used for §3/§4/§12 above).

Ask for this whenever CLI flags might have changed and need re-verifying,
not just for pages that were unreachable the first time.

## 14. Layer A launcher (`sbx/bin/wmf-sbx-create`) — implemented

Resolves each repo argument via `wmf_sbx_resolve.py` (§10, and see
`sbx/DESIGN-repo-resolution.md`), `git clone`s any that don't have a local
checkout yet (plain clone from Gerrit/GitLab's HTTP(S) URL, so it lands on
the upstream default branch with no carried-over WIP — this is what "clean
checkout" means here; an *existing* checkout is used as-is, on whatever
branch it's already on), `realpath`s every directory per item 1 above, then
runs (via `subprocess.run`, not `execvpe` — see the auto-run bullet below
for why) `sbx create --name NAME [--kit KIT] --clone claude
PRIMARY_DIR [EXTRA_DIR:ro ...]`. First positional is the read-write clone
workspace; the rest are read-only, exactly matching the hand-written
invocation recorded at the top of this file.

- **SSH mitigation lands here**: `SSH_AUTH_SOCK` is popped from the child
  environment before the run, per the §8 proposal — this is the wrapper
  that mitigation was waiting on. Still needs the host-side verification
  from §8's checklist item, now unblocked.
- **Auto-runs the new sandbox on success**: `sbx create`'s own stdout ends
  with a reminder to run `sbx run NAME` — typing that literally (bypassing
  the `wmf-sbx` wrapper) is exactly the daemon-retainting mistake §8
  warns about, so a bare `sbx create ...` output can't be allowed to be
  the last thing the launcher shows. `wmf-sbx-create` now runs `sbx
  create` via `subprocess.run` instead of `execvpe`, which leaves the
  launcher in a position to react to the result: on success it prints its
  own reminder pointing at `wmf-sbx run NAME` (never bare `sbx`), then
  `execvpe`s into exactly that unless `--no-run` was passed, in which
  case it just prints the reminder and exits 0. A non-zero `sbx create`
  exit skips both the reminder and the run and propagates the exit code.
- **`--kit` resolution order**: explicit `--kit` flag, else a `kit:` key in
  `repos.yaml` (same file as the directory-mapping rules — no separate
  config file), else a MediaWiki kit is generated on the fly — see §15.
- **`--name` default**: derived from the primary repo's canonical path's
  final segment (`mw-<slug>`, lowercased, non-alnum runs collapsed to `-`).
- **`--dry-run`**: prints the resolution plan (each repo's canonical path,
  local directory, exists-vs-needs-clone) and the final `sbx create`
  command line, without cloning or execing anything — this is how the
  30-plus unit tests in `sbx/bin/tests/test_wmf_sbx_create.py` exercise the
  full flow without a real `sbx` binary or real WMF checkouts.
- Not yet done: actually invoking this against a real `sbx` install (no
  `sbx` binary available inside a dev sandbox to test against — the
  `--dry-run` output was checked by hand against the recorded manual
  invocation instead).
- **Raw filesystem path pass-through (commit `185e6e0`)**: the original
  manual invocation at the top of this file mounts three paths that don't
  fit the "one canonical repo → one local directory" model at all —
  `Skins/` and `Extensions/` are container directories holding many
  individual checkouts, and `mediawiki-core-clean/` is a hand-maintained
  second raw copy of `core`, not something `wmf_sbx_resolve.py` could ever
  map a canonical path to without colliding with the real `core` rule. Any
  `primary`/`extra` argument that starts with `/`, `~`, `./`, or `../` (a
  shape no real Gerrit/GitLab project path ever has) now skips resolution
  entirely: `os.path.realpath(os.path.expanduser(spec))`, verified to be
  an existing directory, done. This is also how cananian's requested
  "exception directory" mechanism falls out for free — no extra flag
  needed, any raw path works as a wide-access add-on to an otherwise
  project-scoped sandbox.
- **Nested-mount guard (`find_nested_mount_conflict`, same commit)**:
  cananian hit an `sbx` limitation firsthand trying to use
  `~/Wikimedia/wmf-claude` as the `--clone` work directory while also
  bind-mounting all of `~/Wikimedia` read-only — `sbx` refuses to put the
  work directory inside another bind mount (even though a bind-mount over
  a bind-mount isn't inherently unsound). `wmf-sbx-create` now checks
  every resolved directory (primary + extras) pairwise for an
  ancestor/descendant relationship and errors out before building the
  `sbx create` command, rather than handing `sbx` an invocation it will
  reject.
- **Verified equivalent invocation for reproducing this exact sandbox**
  (dry-run tested against this sandbox's real, live mount points):
  ```bash
  sbx/bin/wmf-sbx-create --name wmf-claude-sbx --kit ~/Wikimedia/mediawiki-kit \
    ~/Wikimedia/wmf-claude \
    ~/Wikimedia/core \
    ~/Wikimedia/mediawiki-core-clean \
    ~/Wikimedia/Skins \
    ~/Wikimedia/Extensions \
    ~/Wikimedia/Parsoid \
    ~/Wikimedia/mediawiki-config \
    ~/Wikimedia/integration-config \
    ~/Wikimedia/mediawiki-kit \
    ~/Wikimedia/docs.docker.com \
    ~/bin
  ```
  Every argument here is a raw path (all nine extras plus the primary), so
  this needs no `~/.config/wmf-sbx/repos.yaml` at all — `wmf-claude` could
  alternatively be given as `gitlab:repos/product-safety-and-integrity/wmf-claude`
  to exercise the resolver/auto-clone path, but raw paths are the more
  literal, config-independent reproduction of the hand-written original.
  Run this **on the host**, from a wmf-claude checkout — `--kit` is a
  host-side path consumed by `sbx create` at creation time, not something
  visible from inside a running sandbox.

## 15. Making `--kit` optional: generated MediaWiki kit — implemented

Full design in `sbx/DESIGN-kit-generation.md`; implemented in
`sbx/bin/wmf_sbx_kit.py`, wired into `wmf_sbx_create.main()` (§14).

The prompt for this was cananian's own `~/Wikimedia/mediawiki-kit/spec.yaml`
(the kit used in the §14 "verified equivalent invocation" example above) —
hand-maintained, hardcoding a personal disk layout, a stale
`MW_INSTALL_PATHX` workaround, and personal preferences (`GERRITUA`, a
`nano` install) inline with genuinely-shared MediaWiki dependencies. Every
other engineer following the Layer A launcher would otherwise need to
hand-write their own copy of this file before `wmf-sbx-create` worked at
all.

Split into three additive layers, all in `wmf_sbx_kit.py`:

1. **Upstreamed shared defaults** — `BASE_PACKAGES` (the apt packages every
   MediaWiki dev sandbox needs), `EXTRA_DOMAINS` (`github.com`, for
   Composer), and `REPO_ENVIRONMENT_VARS` (canonical Gerrit path → env var
   name(s): `gerrit:mediawiki/core` → `MW_INSTALL_PATH`, `MW_CORE_REPO`;
   `gerrit:mediawiki/services/parsoid` → `PARSOID`;
   `gerrit:mediawiki/vendor` → `MW_VENDOR_REPO`). No `MW_INSTALL_PATHX` —
   per cananian, `MW_INSTALL_PATH` and `MW_CORE_REPO` both just point at
   wherever `mediawiki/core` itself was cloned or mounted for this
   invocation, confirmed safe by the §1 finding that both `--clone` and
   `:ro`-mounted repos land inside the sandbox at the exact same absolute
   path as on the host — no path translation needed to wire these up.
2. **Network domains sourced dynamically from `profiles/wmf-engineer.json`**
   — `wiki_family_domains()` reads that file's `network.allow_domain` and
   keeps the plain-string entries matching `*.<label>.org` where `<label>`
   contains `wik` as a substring (covers `wiktionary`, which doesn't start
   with `wiki`). This closes real drift: `profiles/wmf-engineer.json`
   already had `*.wikinews.org`, which the old hand-copied kit spec.yaml
   was missing. Falls back to a small static list (logged to stderr) if
   the profile can't be read.
3. **Per-user extras from `~/.config/wmf-sbx/repos.yaml`** — two new keys,
   `extra_environment` and `extra_packages`, defaulted to empty by
   `wmf_sbx_resolve.load_config` alongside the existing `rules` and
   `extra_dependencies` (§10/Phase-0-item-list below). This is where
   `GERRITUA`, `CLAUDE_CODE_DISABLE_1M_CONTEXT`, and a `nano` install move
   to — see `sbx/reference/example-repos.yaml` for the documented shape.

**Identifying repos passed as raw paths**: cananian's own invocations (and
the §14 "verified equivalent invocation" example) pass `mediawiki/core`
etc. as raw filesystem paths (§14's raw-path pass-through), which have no
canonical Gerrit path attached on their own. `wmf_sbx_resolve.exact_rule_canonicals(rules)`
reverse-maps every *exact* (non-wildcard) rule in the config — `realpath(expanded
path) → canonical` — with no Gerrit/GitLab lookup or `git` shell-out needed,
since an exact rule's `match` already *is* its canonical path.
`wmf_sbx_create.canonicals_for_kit` looks up a raw-path argument's resolved
directory in this map before building the kit spec, so once a user's
`repos.yaml` has an exact rule for `mediawiki/core`, passing it by literal
path still wires up `MW_CORE_REPO`.

**Generation flow in `main()`**: `kit = args.kit or config.get("kit")` is
unchanged as the explicit-override path. When still `None`: on `--dry-run`,
the generated spec is printed to stderr as a YAML preview and nothing is
written to disk (the final `sbx create ...` preview line has no `--kit`
flag); on a real run, the spec is written to a fresh
`tempfile.mkdtemp(prefix="wmf-sbx-kit-")` directory used as `--kit`, removed
immediately after the `sbx create` subprocess returns (`--kit` is only read
at creation time — same one-shot-at-creation principle as this repo's
top-level `CLAUDE.md` "Gotchas" entry on the nono profile).

Not done: no `--no-kit` opt-out flag (not asked for — an empty
user-supplied `--kit` directory already covers it); no second built-in kit
template for non-MediaWiki work (the config's `kit:` override remains the
escape hatch). `sbx/reference/mediawiki-kit-spec.yaml` (the old
hand-maintained, personal-path snapshot) is left in place as a historical
record, not deleted or treated as a template.

82 unit tests across `test_wmf_sbx_kit.py` (new), plus additions to
`test_wmf_sbx_resolve.py` (`exact_rule_canonicals`) and
`test_wmf_sbx_create.py` (`canonicals_for_kit`, the `main()` generation
branch — dry-run preview, real-run temp-dir write/cleanup, explicit `--kit`
bypass) all pass (`python3 -m unittest discover -s sbx/bin/tests -v`, with
`python3-yaml` apt-installed).

## 16. Excluding archived Gerrit projects from bare-name resolution — implemented

Found while cananian was building their personal `repos.yaml` against real
`fgrep 'project=' Wikimedia/*/.gitreview` output (§15's per-user config):
`gerrit:mediawiki/extensions/Parsoid` is a defunct extension, marked
`[ARCHIVED]` in its Gerrit project description, but it shares a final path
segment ("Parsoid") with the live `gerrit:mediawiki/services/parsoid`, so a
bare-name search for `Parsoid` could resolve to the wrong (dead) project.

WMF's Gerrit convention marks retired repos with an `[ARCHIVED]` tag in the
project *description*, not via Gerrit's own `state` field (`ACTIVE`/
`READ_ONLY`/`HIDDEN` — see §10).

**First attempt (superseded, see the update below): exclude by default.**
`gerrit_search` took `include_archived=False` by default; when false it
appended `&d` to the `?m=` request (Gerrit's REST API wants project
descriptions as a bare presence flag, not `d=<value>`, and normally omits
descriptions for performance — §10 already used `?d` for exactly this
reason) and dropped any candidate whose description contained `[ARCHIVED]`
before the final-path-segment filter in `resolve_canonical_path` ever saw
it, with `--include-archived` on the CLI as an opt-out.

**Regression found (2026-09-06):** cananian hit this directly —
`gerrit:analytics/asana-stats` has no live namesake, so once its sole exact
match got excluded, `wmf-sbx-resolve asana-stats` failed outright
(`No Gerrit project found matching 'asana-stats'`) instead of the previous,
correct `needs_clone` result. Excluding-by-default conflated two different
situations: Parsoid's problem was a live rival with the same final segment
picking the *wrong* (dead) project, not that the archived project itself
should be unreachable by name. Confirmed live: `curl -s
"https://gerrit.wikimedia.org/r/projects/?m=asana-stats&d"` really does
return `"description":"[ARCHIVED] ..."` for `analytics/asana-stats` — it's
genuinely archived, it's just that "archived" and "unresolvable" shouldn't
have been the same thing.

**Revised design: warn, never exclude.** `gerrit_search(substring)` now
always requests descriptions and returns `(names, archived)` — every
matching project, plus the subset that's `[ARCHIVED]` — instead of
filtering anything out. `resolve_canonical_path` takes an optional `warn`
callback; if the chosen exact match is in `archived`, it calls
`warn(f"warning: {canonical} is marked [ARCHIVED] in Gerrit.")` but still
returns that canonical either way. `resolve()` defaults `warn` to printing
to stderr. The warning only fires on the bare-name search path (piggybacking
on the search request already made, so this costs no extra network
round-trip) — an already-qualified `gerrit:...` input skips the search
entirely, same as before, so it stays silent and network-free. The
`--include-archived` CLI flag and `include_archived` parameters were
removed entirely — nothing is ever excluded now, so there was nothing left
for the flag to opt out of.

`wmf-sbx-create` inherits the warn-not-exclude behavior for free (it calls
`wmf_sbx_resolve.resolve()`).

`test_wmf_sbx_resolve.py`: `GerritSearchTests` (renamed from
`GerritSearchArchivedTests`) covers the new `(names, archived)` return
shape; `ResolveCanonicalPathTests` covers a solely-archived bare name still
resolving-with-a-warning, a non-archived match staying silent, and `warn`
being optional (defaults to `None`, safely skipped).

**Follow-up (2026-09-06): prefer non-archived on a case-insensitive
conflict, and treat the actual Parsoid case as a live test.** Once
resolvable again, `Parsoid` (capital P) still surfaced two more bugs
cananian caught by testing against their real directory layout:

1. The final-segment match in `resolve_canonical_path` was case-sensitive,
   so bare `Parsoid` only ever matched
   `gerrit:mediawiki/extensions/Parsoid` (archived) -- the live
   `gerrit:mediawiki/services/parsoid` (lowercase "parsoid") was never
   even a candidate, so the warn-not-exclude fix from above just resolved
   the archived project silently-except-for-a-warning, not what cananian
   wanted. The match is now case-insensitive (`.casefold()`), and among
   the case-insensitive candidates a non-archived one is always preferred
   over an archived one -- only when *every* case-insensitive match is
   archived (asana-stats) does an archived project actually get chosen (see
   above). No `--include-archived`-equivalent is needed for the opposite
   case (deliberately wanting the archived one instead) -- passing the
   full `gerrit:mediawiki/extensions/Parsoid` path bypasses search
   entirely, same as any other already-qualified canonical.

2. `resolve_directory`'s gitreview-mismatch fallback (see the
   `.gitreview` verification entry below) had a real bug of its own:
   cananian's `~/Wikimedia/Extensions/Parsoid` is a symlink to
   `~/Wikimedia/Parsoid` (the live service checkout), so *both* candidate
   paths for `gerrit:mediawiki/extensions/Parsoid` existed but neither's
   `.gitreview` matched -- and the code fell through to reporting
   `needs_clone=True` at the most-specific candidate's path, which was
   already occupied by the wrong checkout. "needs_clone" is supposed to
   mean "safe to git clone here"; a directory that exists with the wrong
   content is not that. `resolve_directory` now raises `ResolutionError`
   instead whenever the final fallback candidate's path exists (regardless
   of *why* none of the candidates verified) -- a plain missing directory
   still needs_clones normally. See the now-corrected
   `test_all_candidates_mismatched_but_none_exist_falls_through_to_new_clone`
   (renamed -- it used to assert the buggy needs_clone behavior) and the
   new `test_most_specific_candidate_exists_but_mismatched_is_an_error`.

99 unit tests total, all passing.

## 17. Verifying an existing directory's `.gitreview` before trusting it — implemented

Belt-and-suspenders addition cananian asked for on top of §16:
`repos.yaml` rules are hand-maintained and directories get renamed,
repurposed, or go stale, so "a directory exists at the rule's expanded
path" isn't proof it's actually the right checkout. `resolve_directory`
now cross-checks: for a `gerrit:` canonical whose candidate directory
exists, if that directory has a `.gitreview` file, its `project=` line
(trailing `.git` stripped) must equal the canonical's path, or the
candidate is treated exactly like a missing directory -- fall through to
the next (less-specific) rule, same as the existing "falls back to looser
existing directory" behavior from §10/`ResolveDirectoryTests`. If every
existing candidate fails verification, resolution falls all the way
through to `needs_clone=True` at the most specific rule, same as if
nothing existed on disk at all. A directory with no `.gitreview` (or a
`gitlab:` canonical, which `.gitreview` doesn't apply to) is trusted as
before -- there's nothing to cross-check.

New standalone `gitreview_project(path)` helper (reads `<path>/.gitreview`,
returns the `project=` value or `None`) and a `gitreview_project=` keyword
on `resolve_directory`, injectable for testing the same way `exists=` is.

9 new unit tests: `GitreviewProjectTests` for the helper itself (reads the
real line, strips `.git`, missing file, missing line), plus 5 more in
`ResolveDirectoryTests` covering match/mismatch/missing-file/gitlab-is-
exempt/all-candidates-mismatched. 96 unit tests total, all passing.

## 18. Reverse lookup: `wmf-sbx-resolve --path PATH` — implemented

`wmf_sbx_create.canonicals_for_kit` already did a limited version of this
for `wmf-sbx-create`'s raw-path arguments (see `is_raw_path`): given a
local directory, identify which known repo it is, via
`exact_rule_canonicals`'s reverse map of *exact* (non-wildcard) rules
only. cananian asked for this generalized and exposed as its own CLI mode
on `wmf-sbx-resolve`, printing the same `canonical:`/`path:`/`rule:`
output as forward resolution.

New `reverse_resolve(path, rules, exists=, gitreview_project=)` in
`wmf_sbx_resolve.py`:

- If `path` has a `.gitreview` with a `project=` line, that's authoritative
  -- `repos.yaml` rules are hand-maintained and can drift (see §17), so the
  checked-out project wins over what a rule would have predicted for this
  location. The reported `rule` is whichever configured rule's own
  expansion (via the existing `matching_rules`/`expand_path`, so this
  works for `**`/`{name}` wildcard rules too, not just exact ones) produces
  this exact directory; if none does, `rule` is `None` and the CLI prints
  `<no rule>` -- the `.gitreview` project is still trusted, repos.yaml just
  doesn't know how to reproduce this particular directory.
- Without a `.gitreview` (not a Gerrit checkout, or nothing cloned there
  yet), falls back to `exact_rule_canonicals` only. A wildcard rule
  genuinely can't be inverted from a bare directory alone -- e.g.
  `gerrit:**/{name}` -> `~/Wikimedia/{name}` loses which Gerrit namespace
  `{name}` came from, so many different canonicals collapse onto the same
  local path template. An exact rule has only one possible canonical by
  construction, so that direction is safe without a `.gitreview` to
  disambiguate.
- Raises `ResolutionError` if `path` doesn't exist -- this is a lookup
  *of* an existing checkout, not a clone-target computation, so there's no
  `needs_clone` concept here (the CLI always reports `(exists)`).

This is a strict superset of what `canonicals_for_kit` needed, so it now
delegates to `reverse_resolve` (with `exists=lambda p: True`, since
raw-path arguments are already existence-checked upstream by
`resolve_repo`) instead of calling `exact_rule_canonicals` itself --
meaning `wmf-sbx-create` also benefits: a raw-path argument that's an
existing Gerrit checkout now gets identified via its `.gitreview` even
when it sits at a location only a wildcard rule would produce, not just
an exact-rule location as before.

`main()` gained a `--path PATH` flag, mutually exclusive with the
positional `NAME` (exactly one of the two is required). Same `canonical:`/
`path:`/`rule:` text output and `--json` shape as forward resolution,
`needs_clone` always `false`.

7 new unit tests in `ReverseResolveTests`: `.gitreview` + matching wildcard
rule, `.gitreview` with no rule producing that path (`<no rule>`),
`.gitreview` overriding what the directory's location would otherwise
suggest (the real Parsoid-symlink case from §16, reused here), no-
`.gitreview` exact-rule fallback, no-`.gitreview` wildcard-only match
staying unidentified, missing directory raising, and `~` expansion. 106
unit tests total, all passing.

## 19. Optional `--name` with collision prompting — implemented

`wmf-sbx-create`'s `--name` was already optional at the argparse level,
falling back to `default_sandbox_name` (`mw-<slug>`, derived from the
primary repo's canonical path) — that part of the old to-do item was
already done. What was missing: nothing checked whether
that derived name was already in use by an existing sandbox before handing
it to `sbx create --name`.

Confirmed via the docs.docker.com mirror (`reference/cli/sbx/ls/`,
`reference/cli/sbx/create/`) that `sbx create`'s own `--name` default is
`<agent>-<workdir>` (different scheme, kept as-is for us since
`default_sandbox_name` already matches what cananian asked for: "name
sandboxes after the primary working repo"), and that `sbx ls -q` lists
just sandbox names, one per line, regardless of status (a *stopped*
sandbox still occupies its name — see `wmf-sbx-ls` in the repo root for a
real example: `PageAssessments`, `claude-offset`, etc. are all stopped but
still listed).

Two new functions in `wmf_sbx_create.py`:

- `existing_sandbox_names(run=subprocess.run)` — runs `wmf-sbx ls -q` (via
  the `WMF_SBX` wrapper constant, never bare `sbx` — see §8) and returns
  the set of names. Best-effort: if the binary is missing or the command
  fails for any reason, returns an empty set rather than blocking sandbox
  creation on our own ability to check — `sbx create` itself remains the
  final authority on whether a name is actually free.
- `unique_sandbox_name(name, existing, prompt=input)` — if `name` collides,
  computes the first free `name-2`, `name-3`, ... suggestion and prompts
  the user, offering that suggestion as the default (empty response). If
  the user instead types a name that *also* collides, it loops and
  re-prompts with a fresh suggestion, rather than handing a colliding name
  to `sbx create`.

`main()` only runs the *derived default* name through this check
(`args.name or unique_sandbox_name(default_sandbox_name(name_source),
existing_sandbox_names())`) — an explicit `--name` is a deliberate user
choice, and `sbx create` already reports a clear error if that one turns
out to be taken, so re-prompting there would just be surprising.

13 new unit tests: `ExistingSandboxNamesTests` (parses `-q` output, ignores
blank lines, empty set on command failure or missing binary, confirms the
exact `wmf-sbx ls -q` invocation), `UniqueSandboxNameTests` (no-collision
short-circuits without prompting, empty response accepts the suggestion,
suggestion skips already-taken suffixes, explicit response used as-is,
re-prompts if the typed name also collides, response is stripped), and
`MainNameCollisionTests` (mocks `existing_sandbox_names`/
`unique_sandbox_name` to confirm `main()` wires them together on the
default-name path and skips them entirely when `--name` is given
explicitly). 119 unit tests total, all passing. Manually verified via
`--dry-run` in this sandbox (no `wmf-sbx`/`sbx` on `PATH` here) that the
missing-binary fallback is silent and the default name is used unchanged.

## 20. `sbx stop` + `sbx run`/`sbx exec` on the *same* sandbox — CONFIRMED **[human docs + cananian's own observed usage]**

Distinct from §4 (templates): this is restarting the identical container
after `sbx stop`, not booting a fresh sandbox from a saved image.

- `sbx stop SANDBOX [SANDBOX...]`: "Stopped sandboxes retain their state and
  can be restarted with `sbx run`" (docs, verbatim). `sbx exec SANDBOX
  COMMAND...`: "If the sandbox is stopped, it is started first" — so `exec`
  on a stopped sandbox is itself a restart, not just a running-sandbox-only
  operation.
- **Persists across the stop/restart cycle** (it's the same container, same
  writable layer — ordinary Docker stop/start semantics, not a rebuild):
  the entire container filesystem, including anything written after
  creation — a `--clone` workspace's commits/edits (`--clone` on `sbx run`
  re-attach is documented as "no-op when re-attaching to an existing
  clone-mode sandbox," i.e. it doesn't re-clone), any scratch clone a kit's
  `commands.install` made into a non-primary directory (relevant to the
  multi-clone/dependency design — a scratch clone only needs to be created
  once, at initial `sbx create`, and then survives every subsequent
  stop/restart for that sandbox's whole lifetime), packages `commands.install`
  installed, and env vars/network domains that were in effect when the
  sandbox was originally created. Bind mounts also persist in the sense that
  matters: they're host directory references re-attached at start, so
  whatever's currently on the host at that path is what you see — this
  matches cananian's own observed experience.
- **Does NOT persist** — must be respecified on every `sbx run`/`sbx exec`:
  the agent CLI invocation arguments (everything after `--`). This matches
  cananian's own observed workaround: `sbx run <name> -- --continue
  --dangerously-skip-permissions` on every re-attach, because the agent
  process itself is started fresh each time, not part of the container's
  persisted state — only its filesystem effects (e.g. Claude's own session
  transcript/state files, if written under the workspace or home directory)
  survive, not the invocation that launched it.
- `-e`/`--env` on `sbx run` is documented as dual-purpose: "Applies to the
  agent session, so it takes effect on a re-attach too; also baked into the
  sandbox when this run creates it" — i.e. passing `-e` on a *re-attach* run
  affects that one session/process, distinct from (and not necessarily the
  same as) permanently rewriting the container's baked-in environment for
  every future exec. Not yet empirically distinguished; worth a real test
  only if some future design needs env vars to change permanently mid-life
  rather than just for one attached session.
- Relevant to both `sbx/DESIGN-template-caching.md` and
  `sbx/DESIGN-parallel-clone-tree.md`: a sandbox's `--kit`
  provisioning and any scratch clones only ever need to happen once, at
  `sbx create` time — they're durable for that sandbox's entire lifetime
  across any number of stop/restart cycles, not something to redo per
  session.

## 21. Three cananian-reported bugs fixed 2026-09-07

- **`wmf-sbx-create . ...` failed name resolution.** `is_raw_path()` only
  matched paths starting with `/`, `~`, `./`, `../` -- a bare `.` or `..`
  (no trailing separator) fell through to Gerrit-name resolution and
  errored (`No Gerrit project found matching '.'`). Fixed: `is_raw_path`
  now also matches the literal strings `.` and `..`.
- **`/home/agent/Projects/...` parallel tree was root-owned.** The
  `commands.install` step (`wmf-sbx-setup`, hence `clone_into_parallel_tree`
  in `wmf_sbx_setup.py`) runs as root, so both the `git clone --reference`
  destination and any ancestor directories `os.makedirs` created along the
  way landed owned by root -- `git` then refuses to touch them ("detected
  dubious ownership") for the `agent` user who actually works there. Fixed:
  after a successful clone, `sudo chown -R agent:agent` the shallowest
  directory this script actually created (a new `shallowest_missing_ancestor`
  helper walks back up from the clone destination to find it, so the chown
  doesn't reach into anything pre-existing/unrelated).
- **Claude's session started in the read-only host-mirrored original, not
  the writable `/home/agent` clone.** Confirmed (`sbx/DESIGN-memory-persistence.md`)
  that `sbx` sets Claude's actual starting `PWD`/`WORKSPACE_DIR` to the
  literal host path passed as the primary positional to `sbx create` --
  this is not overridable via `-e WORKSPACE_DIR=...`/`-e CLAUDE_PROJECT_DIR=...`
  (cananian's own personal `extra_environment` override had no effect on
  the actual cwd, only -- maybe -- on project-memory keying). `/cd` is a
  Claude-Code-level notion, not an OS `chdir`, so only Claude itself,
  instructed to do so, can issue it -- and the wmf-claude plugin's own
  `SessionStart` hook isn't wired into the generated kit at all yet (no
  marketplace/plugin install step in `wmf_sbx_kit.py`), so that hook can't
  be the trigger today. What *does* already fire with no extra wiring:
  Claude Code loads a user-level `~/.claude/CLAUDE.md` automatically (this
  session's own context included it unprompted). Fixed by having
  `wmf_sbx_kit.write_kit_dir` also drop `files/home/.claude/CLAUDE.md`
  (`wmf_sbx_kit.HOME_CLAUDE_MD`) with computable instructions: derive
  `/home/agent/<relative-path-under-host-home>` from the current working
  directory and `/cd` there immediately if it exists. Host-home-agnostic by
  construction (no baked-in username), so it works for any WMF engineer's
  layout, not just cananian's.

Live-reproduced and fixed directly in a running sandbox created before
this session's `sbx` upgrade (0.31.0 -> 0.39.0) -- see the two open reports
below, which involve the *upgraded* `sbx` and couldn't be reproduced from
inside this (older) sandbox, since `sbx` itself is a host-only binary.

Fixed:

- **git-daemon-remote fetches went from working to `fatal: Could not read
  from remote repository`, reproduced 2026-09-07.** Confirmed host-side
  (`git remote update` failing against `git://127.0.0.1:<published-port>/...`
  despite `wmf-sbx ports --json` showing the forward correctly registered)
  and reproduced locally inside the sandbox. Root cause, confirmed (a first
  pass at this diagnosis, based on `/proc/<pid>/fd` appearing to show no
  listening socket, wrongly concluded the daemon process itself had gone
  stale -- that `/proc` evidence turned out to be a red herring: a second,
  independently-confirmed-working daemon showed the identical fd listing,
  so this sandbox's `/proc/<pid>/fd` doesn't reveal socket fds at all,
  working or not; a second Claude session reviewing the transcript flagged
  CVE-2022-24765 as the likelier mechanism, which a targeted repro then
  confirmed): `wmf-sbx-setup` runs as root, so the `git daemon` it starts,
  and every `upload-pack` it forks per connection, inherited root's euid.
  The moment a served repo's ownership stopped matching root -- e.g. right
  after `clone_into_parallel_tree`'s own chown to `agent` (this session's
  other fix, above) -- every fetch against that repo started tripping
  git's `CVE-2022-24765` "detected dubious ownership" check inside the
  forked child, which is indistinguishable to the client from a dead
  server (`fatal: Could not read from remote repository`) while the parent
  daemon process itself stays alive and otherwise healthy throughout
  (confirmed directly: `sudo env -i ... git daemon --verbose
  --log-destination=stderr ...` as root against the agent-owned tree logs
  exactly `fatal: detected dubious ownership in repository at
  '.../.git'` per connection, while `[NNN] Ready to rumble` keeps accepting
  new ones). Fixed, two parts, both needed (`start_daemon()`):
  1. Run as `agent`, not root -- least-privilege for a read-only,
     fetch-only daemon, and it makes euid match the owner of every
     git-*cloned* repo in the parallel tree.
  2. `-c safe.directory='*'` on top of that -- `agent`'s euid only
     matches *cloned* repos; a `:ro` opt-out repo's parallel path is a
     bind mount (`bind_into_parallel_tree`), never chowned (that would
     corrupt the pristine original), so it keeps whatever ownership the
     host-mirrored original has inside the sandbox -- `agent` only by
     coincidence when the host user's uid happens to be 1000 (confirmed:
     that coincidence is exactly why this sandbox's own `:ro` repos, had
     it had any, wouldn't have shown the bug even after fix #1 alone).
     Scoped to just this `git` invocation (and, via
     `GIT_CONFIG_PARAMETERS`, its forked children) rather than a global
     gitconfig change -- an acceptable blanket trust here since this
     daemon only ever serves read-only fetches out of the sandbox's own,
     otherwise-inaccessible-from-outside tree.
  Remediated live in the affected sandbox (killed the wedged-per-connection
  daemon, restarted as `agent` with `safe.directory='*'` on the same port)
  so cananian's pending fetch could go through immediately.

## 22. `:ro` must reach `sbx create`, not just the in-sandbox remount

cananian reported that `:ro`-suffixed repos "came up read-write, with git
remotes for all of them." Two separate things there, and the first read is
a red herring worth recording, because it wasted a round:

- The **remotes** part is expected regardless of `:ro`
  (`parallel_tree_remotes`'s docstring: the daemon serves the whole
  parallel tree, read-only bind mounts included, by design).
- The **read-write** part looked at first like a non-reproduction: a
  `touch` sweep across all nine `:ro` directories in the running sandbox,
  originals and parallel-tree copies alike, reported "Read-only file
  system" for every one of them. That check was too shallow. It only
  proved the *current* mount flags, which `wmf-sbx-setup`'s remount had
  set -- not whether anything stops the sandbox from changing them back.

The real problem, which cananian identified from the create-time
transcript: `split_ro_suffix` stripped `:ro` and never passed it to
`sbx create`, so every directory was created read-write and only
*remounted* read-only afterward, from inside. The sandboxed agent has
passwordless sudo, so that is trivially reversible:

```
$ sudo mount -o remount,rw /home/cananian/Projects/Wikimedia/docs.docker.com
$ touch /home/cananian/Projects/Wikimedia/docs.docker.com/.sbx-ro-test   # succeeds
```

Confirmed live in this sandbox -- a write landed in a real host file that
the engineer had explicitly marked `:ro`. Upstream describes the same
thing (docker/sbx-releases#556): *"This makes the `:ro` mounts only an
accident guard against ordinary tool writes -- not a barrier against the
sandboxed process itself."*

The distinction that makes the fix work: a mount `sbx` created read-only
**at create time** is backed read-only on the host side of the virtiofs
export, below the layer the sandbox's `mount` can reach. Confirmed here
against `/etc/hosts` (a create-time-`ro` virtiofs mount -- this sandbox
has no create-time-`ro` *workspace* mount to test, precisely because of
the bug being fixed):

```
$ sudo mount -o remount,rw /etc/hosts      # reports success
$ grep ' /etc/hosts ' /proc/mounts
bind-4a666ea3a3b04a24 /etc/hosts virtiofs rw,relatime 0 0   # even *says* rw
$ sudo touch /etc/hosts
touch: setting times of '/etc/hosts': Read-only file system  # still blocked
```

versus one of our own remounted originals, where the identical sequence
lets the write through. This sandbox has `SANDBOX_NAME`/`SANDBOX_ID` set
(0.39.0-era variables, per that release's notes), so this is current
0.39.0 behaviour, not stale 0.31.0 behaviour.

Note this contradicts a 2026-09-06 residual-risk note in
`sbx/IMPLEMENTATION-PLAN.md` §15.2, which read as though `sbx`'s own `:ro`
extras had been shown escapable too. Whether that was 0.31.0 behaviour
since fixed upstream, or a test mis-attributed to an sbx `:ro` mount when
it actually hit one of our own remounted ones, is unresolved -- but it is
not how 0.39.0 behaves, and that note has been corrected, since the whole
fix depends on the difference.

Fixed: `build_sbx_command` now re-appends `:ro` to every read-only extra
on the `sbx create` command line. Both layers are kept, deliberately:
create-time `:ro` is the enforcing one; `wmf-sbx-setup`'s remount stays as
the accident guard, and is still what picks bind-mount-instead-of-clone
for the parallel tree.

Not fixable the same way: **the primary workspace**. `sbx create` mounts
it read-write unconditionally, so `:ro` on the primary can only ever be
the escapable in-sandbox remount. `wmf-sbx-create` now warns and points
the user at passing it as an extra instead.

**Testing lesson worth keeping**: to test a read-only mount, don't just
`touch` it. Try to escalate first (`sudo mount -o remount,rw` then write),
because the sandbox itself can, and the pre-escalation `touch` result
looks identical either way.

### Resolved 2026-09-07: create-time `:ro` for *every* extra

The follow-up question -- whether *non*-`:ro` extras should get create-time
`:ro` too -- was put to cananian and answered yes. The hole was identical
for them: for a default (no-suffix) extra, `setup_repo` clones it into the
parallel tree and then remounts the original read-only, and that remount is
escapable exactly as above, so the "original is locked, work in the private
clone" half of the §15 design was an accident guard as well.

`build_sbx_command` now appends `:ro` to every extra unconditionally. This
is safe for the clone path: `git clone --reference ORIG ORIG DEST` only
reads ORIG. It reduces the user-facing `:ro` suffix to purely "bind-mount
instead of clone" in the parallel tree -- it no longer decides whether the
original is read-only, because now they all are.

The primary positional is the one exception, and it is sbx's limitation
rather than our choice: `sbx create --name foo claude <primary>:ro` fails
with `ERROR: primary workspace must be read/write (remove ':ro' or
':readonly')`. `wmf-sbx-create` warns when the user suffixes the primary,
since we can only remount it read-only from inside, which is escapable.

cananian also settled the 0.31-vs-0.39 ambiguity flagged above and in
IMPLEMENTATION-PLAN §15.2: **the reversible read-only mount was a 0.31 bug,
fixed somewhere between 0.31 and 0.39.** So the 2026-09-06 note that
recorded sbx's own `:ro` as escapable was accurate when written against
0.31.0 and is simply obsolete now -- not a misobservation.

## 23. MediaWiki setup-step findings (2026-09-07, verified against `~/Wikimedia/core`)

Gathered while writing `sbx/DESIGN-setup-steps.md`; recorded here because
each contradicts a reasonable assumption, and three of them changed the
design.

- **`composer mw-install:sqlite` enables no extensions.**
  `includes/Installer/CliInstaller.php:124-152`: skins fall through to an
  `else` branch that runs `findExtensions('skins')` unconditionally, but
  extensions are only detected when `--extensions=` or `--with-extensions`
  is passed — and core's script passes neither. Without a fix the
  dependency walk and the symlinks produce a `LocalSettings.php` that
  loads none of them. Fix (cananian, confirmed): append the flag rather
  than replace the script — `composer mw-install:sqlite -- --with-extensions`.
  Composer appends extra args to the script's last command, and
  `MaintenanceParameters::loadWithArgv`
  (`maintenance/includes/MaintenanceParameters.php:366-391`) accepts a
  `--foo` option anywhere in argv, including after the positional
  `MediaWiki Admin` — only a literal bare `--` ends option parsing, and
  composer eats its own `--` before PHP sees it.
- **`.env` is read by `docker compose`, by nothing else.** Core's own
  harnesses read `process.env` directly and throw if unset —
  `tests/selenium/wdio-mediawiki/wdio-defaults.conf.js:23`,
  `Gruntfile.js:9`. `dotenv` is still in `package.json` devDependencies
  but nothing in core requires it any more, and `api-testing` uses
  `.api-testing.config.json`. So writing `.env` in a sandbox that isn't
  running MediaWiki-Docker is inert unless the same values also go into
  the kit's `environment.variables`.
- **A parallel clone's `origin` is the read-only host original**, because
  `clone_into_parallel_tree` clones from the literal host path. `git
  safe-reset` inside the sandbox therefore resets to the *host's* local
  `master` (a clone's remote-tracking refs mirror the source's local
  branches), offline and with no SSH agent needed. Good default; just not
  the Gerrit semantics the script's name implies on the host.
- **`git-safe-reset` calls `git-review-check`**, not `git-check-reviews`
  — `~/bin` has a live `git-review-check` and a stale
  `git-check-reviews~` backup. Both scripts must land on `PATH` in the
  sandbox: `git-safe-reset` so `git safe-reset` resolves as a git
  subcommand, `git-review-check` because the first calls it by bare name.
- **Lockfile tracking differs between the two ecosystems in core:**
  `composer.lock` is gitignored (so `composer update` leaves a clean
  tree), `package-lock.json` is tracked (so `npm install` can dirty it,
  which then makes a later `git safe-reset` refuse — hence `npm ci`).
  `.env`, `cache/`, and `LocalSettings.php` are all gitignored.
- **No `.gitmodules`** in core, Translate, Cite, Parsoid, or Vector, so
  `git-safe-reset`'s trailing `git submodule update` — which would need
  the SSH agent the sandbox deliberately lacks — is a no-op in practice.
  Still must not be treated as fatal for the rare repo that has one.

## 24. sbx's own default network policy — CONFIRMED **[cananian, host, 2026-09-07]**

`wmf-sbx policy ls wmf-claude-sbx --type network --wide` finally answers
the "whatever `sbx`'s own default policy is" unknown §9 has been carrying,
and it changes the picture: **the kit is not the main network policy, it
is a small addition to a large permissive default.**

Six rows, five of them `source: local` with `APPLIES TO: all` — sbx's
shipped local defaults, not org policy — plus one `source: kit` row for
this sandbox:

| Rule | What it allows (abbreviated) |
| --- | --- |
| `default-ai-services` | `api.anthropic.com`, `claude.com`, `**.openai.com`, `**.chatgpt.com`, `**.cursor.sh`, `**.factory.ai`, `api.perplexity.ai`, `gemini.google.com`, `generativelanguage.googleapis.com`, … |
| `default-package-managers` | ~60 entries: `**.packagist.org`, `packagist.org`, `registry.npmjs.org`, `npmjs.org`, `nodejs.org`, `nodesource.com`, `pypi.org`, `files.pythonhosted.org`, `crates.io`, `rubygems.org`, `maven.org`, `proxy.golang.org`, … |
| `default-code-and-containers` | `github.com`, `**.github.com`, `**.githubusercontent.com`, `**.gitlab.com`, `bitbucket.org`, `docker.io`, `ghcr.io`, `quay.io`, `**.gcr.io`, `ppa.launchpad.net`, `sourceforge.net`, … |
| `default-cloud-infrastructure` | `**.amazonaws.com`, `**.googleapis.com`, `**.gstatic.com`, `**.visualstudio.com`, `dl.google.com`, `unpkg.com`, `jsdelivr.net`, `vercel.com`, `supabase.com`, … |
| `default-os-packages` | `**.debian.org`, `archive.ubuntu.com` (:80 and :443), `security.ubuntu.com`, `dl-cdn.alpinelinux.org`, `packagecloud.io`, … |
| `kit:wmf-claude-sbx` | our wiki-family list + `github.com`, plus Anthropic endpoints sbx injects (`api.anthropic.com`, `bridge.claudeusercontent.com`, `mcp-proxy.anthropic.com`, `platform.claude.com`, `downloads.claude.ai`) |

Verified live, not just read off the policy table:

```
$ wmf-sbx exec wmf-claude-sbx curl -sSI https://repo.packagist.org/packages.json
HTTP/1.0 200 Connection established

HTTP/2 200
...
$ wmf-sbx exec wmf-claude-sbx curl -sSI https://registry.npmjs.org/npm
HTTP/1.0 200 Connection established

HTTP/2 200
```

Note `repo.packagist.org` is reached via the `**.packagist.org` wildcard,
and `registry.npmjs.org` by its exact entry — both work.

The `HTTP/1.0 200 Connection established` preamble is itself a finding:
egress leaves the sandbox through an **HTTP `CONNECT` proxy**, which is
how the allowlist is enforced for TLS (SNI/host at CONNECT time, no MITM
of the payload). That matches the proxy behaviour §7 hypothesised and
means the enforcement point is a hostname allowlist, not a packet filter
— no path or method granularity, exactly as §3 said.

What this settles:

- **`composer update` and `npm ci` work with no kit change** — the
  registries are in `default-package-managers`, and both are confirmed
  reachable from inside a real sandbox. Was the blocker for
  `DESIGN-setup-steps.md` §3/§5; now resolved (§7.1 of that doc).
- **`EXTRA_DOMAINS`' `github.com` is redundant** with
  `default-code-and-containers`. Harmless; leave it, since it documents an
  actual dependency rather than relying on a default.
- **The kit's `allowedDomains` is not a restriction, it is an addition.**
  Any mental model that treats `mediawiki-kit-spec.yaml`'s list as "the
  network policy" is wrong by roughly two orders of magnitude — the wiki
  family and `github.com` sit on top of a default that already reaches
  every major package registry, `**.amazonaws.com`, and several AI
  vendors' APIs. This belongs in
  the sbx threat model as a first-class fact (tracked in `SECURITY.md`
  §10's "Open follow-ups"): **the interesting lever is
  `--deny-network`, which narrows, not `allowedDomains`, which widens.**
- The local rows are `source: local`, so they are per-machine and could
  differ for another engineer or under an org policy. Kit-declaring the
  registries we actually depend on is therefore still worth doing as
  documentation — it can never widen past the local/org layers.
- Format note: local rules carry explicit `:443` (and `:80` for the
  Ubuntu mirrors); the kit's entries do not, and use single-`*`
  wildcards where the defaults use `**`. Both forms are accepted.

## 25. What the sandbox base image actually contains — MEASURED **[in-sandbox, 2026-09-07]**

Measured from inside a live `wmf-claude-sbx` sandbox rather than inferred,
which is the point: a sandbox generated by `wmf-sbx-create` can inspect
its *own* image, so image-content questions never need a host round-trip.
(Image questions only. `sbx` itself is host-only — there is no `sbx`
binary in here — so sandbox *lifecycle* behaviour still can't be tested
from inside; see #8.)

- **Ubuntu 26.04 LTS**, apt sources at `archive.ubuntu.com resolute/*`.
- **node `v22.22.1`** and **npm `9.2.0`** at `/usr/bin/{node,npm,nodejs}`,
  from `universe` — already at their apt candidate versions, so
  `apt-get install nodejs` is a no-op and `BASE_PACKAGES` needs no entry.
  Node 22 is what MediaWiki CI targets. The odd-looking npm 9 / node 22
  pairing (upstream node 22 bundles npm 10) is just Ubuntu unbundling npm;
  npm 9 handles core's `lockfileVersion: 3` natively, so `npm ci` is fine.
- **PHP 8.5.4**, Composer 2.9.5, with `sqlite3`, `pdo_sqlite`, `intl`,
  `mbstring`, `xml`, and `apcu` all loaded. Against core's
  `"php": ">=8.3.0"` and `PHPVersionCheck.php`'s `$minimumVersion =
  '8.3.0'` — a floor with no ceiling — 8.5 is fine. MW is 1.47.0-alpha.
- `BASE_PACKAGES` landed: `git-review`, `convert`, `rsvg-convert` all on
  `PATH`.

Together with §24 this unblocks `DESIGN-setup-steps.md` entirely: both
things that design expected to need kit changes turned out to be already
provided, one by sbx's default network policy and one by the base image.

## 26. Host remotes and `wmf-sbx-rm` — implemented, host-unverified

`sbx/DESIGN-host-remotes.md` is implemented (2026-09-07):
`wmf_sbx_state.py` (per-sandbox JSON under
`${XDG_STATE_HOME:-~/.local/state}/wmf-sbx/sandboxes/`),
`wmf_sbx_remotes.py` (`sync_remotes` / `remove_remotes` / `unfetched_tips`
/ `prune_dead`), `wmf_sbx_rm.py` + the `wmf-sbx-rm` shim, and the
`wmf_sbx_create.py` wiring. 236 unit tests pass; the git-facing ones drive
real repositories in tempdirs.

**What the tests cannot cover.** Every claim about `sbx` itself is
second-hand — encoded from cananian's host transcripts, not exercised:

- `sbx rm` prompts rather than refusing on a running sandbox, and
  **declining exits 0** while removing a nonexistent sandbox exits 1. The
  code never reads that exit status; it re-checks `wmf-sbx ls -q`
  instead, and `test_declining_the_prompt_leaves_everything_alone` pins
  the consequence. But the premise is untested against a real binary.
- `--force` is the flag that skips the prompt (long spelling only; `-f`
  was never confirmed, so `remove_sandbox` doesn't emit it).
- Host port recycling — the reason cleanup is a correctness requirement
  rather than tidiness — is cananian's observation, 2026-09-07.

**The end-to-end loop has never been run**: `wmf-sbx-create` → commit in
the sandbox → `git fetch sandbox-<name>` on the host → `wmf-sbx-rm`. That
needs a host, because there is no `sbx` binary in here (§8). The most
valuable single check is the middle step: that a real fetch over
`git://127.0.0.1:<published>/<relative-path>` actually returns the
sandbox's commits, since the URL shape is assembled from
`wmf_sbx_setup.parallel_path` and the published port and has only ever
been *printed*.

## 27. The dependency walk — implemented (2026-09-07)

`sbx/bin/wmf_sbx_deps.py` implements §2–§6 of
`sbx/DESIGN-dependency-walk.md`; `wmf_sbx_create.expand_dependencies()`
consumes it, so `wmf-sbx-create gerrit:mediawiki/extensions/Translate`
now plans four repos instead of one. `--no-deps` skips the walk entirely,
`--no-dev` / `--no-suggests` narrow it. The §7 in-sandbox symlink half
landed the same day — see §28.

Three things worth keeping next to the code, all of them the reason a
piece of it looks the way it does:

- **Manifests come from gitiles when a repo isn't cloned yet**, so the
  closure is known before anything is cloned and `--dry-run` shows the
  real plan rather than the part of it that happens to be on disk
  already. A local checkout always wins (no network, and it's the tree
  that gets mounted); a checkout that exists but has no manifest is a
  leaf, *not* a reason to go to the network.
- **Wikimedia's edge rate-limits manifest fetches** — MEASURED
  **[in-sandbox, 2026-09-07]**. A burst of `?format=TEXT` fetches earns
  `HTTP 429` from Varnish (not gitiles) with `retry-after: 60`. Measured
  live: after tripping it, a Translate walk retried once, waited the full
  60s, and completed correctly (`core`, `UniversalLanguageSelector`,
  `Vector`). Hence `GITILES_ATTEMPTS`/`RETRY_AFTER_MAX` and the warning
  printed before each wait, so a minute of apparent hanging is explained.
- **"Couldn't ask" is not "has no dependencies."** A rate-limited or
  offline walk otherwise produces a plausible-looking closure that is
  quietly missing repos, which resurfaces much later as inexplicable
  failures inside the sandbox. `fetch_manifest` returns the `UNREACHABLE`
  sentinel rather than `None`, `make_manifest_for` collects those
  canonicals, and `wmf-sbx-create` lists them and **refuses to create**
  (`--dry-run` still prints the plan, marked possibly-short).

Unit tests are hermetic: `patch_offline()` in
`tests/test_wmf_sbx_create.py` stubs `fetch_manifest` for every `main()`
test. That's not just hygiene — before it was added the suite really did
fetch from gerrit, and once the 429 retry landed it took 120s instead of
0.1s.

**Still guesswork**: dependency key → Gerrit project. The key is a
credits name and we need a project path. It holds across all 298 local
manifests with zero counterexamples, but keys that cannot be a path
(`Abuse Filter`) are refused rather than guessed at, a fetched manifest
whose `name` disagrees with the key that pulled it in gets a warning, and
`dependency_overrides:` in `repos.yaml` is the escape hatch.

## 28. The symlinks, and the JSON plan file (2026-09-07)

§7 of `sbx/DESIGN-dependency-walk.md` is now
`wmf_sbx_setup.link_into_core()`: for each non-core repo it creates
`<core clone>/{extensions,skins}/<link name>` pointing at the repo's
**parallel** path, then `sudo chown -h agent:agent`s the link. It skips
with a printed reason when there is no core in the sandbox, or when core
came in as a `:ro` bind mount of the host's tree (unwritable, and writing
there would be wrong anyway). It replaces a stale symlink, but never
touches a real directory that's already sitting at the destination —
that's either a real checkout or a name collision, and clobbering it
would destroy work.

The part that wasn't in the design: **the decision of what to link where
is made on the host.** `wmf_sbx_create.link_plan()` computes
`{host_dir: (link_name, section)}` and the kit ships it to the sandbox as
a JSON file. Two things forced that, and either alone is sufficient:

- **The setup script can't recompute it.** Link names come from the
  canonical (`gerrit:mediawiki/extensions/Cite` → `Cite`) or, outside the
  `extensions`/`skins` trees, from the manifest's `name` field
  (`mediawiki/services/parsoid` → `extensions/Parsoid`). Canonicals exist
  only host-side, and the manifests the script would have to read live in
  clones it hasn't made yet at the time the plan is built.
- **Positional argv can't carry it.** `link_name` can contain spaces, and
  the closure routinely runs to a dozen repos.

So `wmf-sbx-plan.json` (`DESIGN-setup-steps.md` §1) landed here, one step
early — `wmf_sbx_kit.build_plan()` writes it, `load_plan()` reads it. The
old `HOST_HOME PORT REPO[:ro] ...` argv still works: `plan_from_argv()`
normalises it into the same dict, so there is exactly one code path
downstream. `write_kit_dir` refuses to write a kit whose install command
names the plan file without a plan attached — that failure would
otherwise appear only inside the sandbox, as a setup run that clones
nothing.

`--dry-run` prints the plan JSON next to the kit spec, which is the
cheapest way to check the link names for a given argument list without
building anything.

## 29. The rest of the setup chain (2026-09-07)

`sbx/DESIGN-setup-steps.md` is implemented: `wmf_sbx_setup.mediawiki_setup()`
now runs `git safe-reset --force` in every non-primary clone, `composer
update` in every clone that has a `composer.json` (**including** the
primary), the symlinks, `.env`, `npm ci`, and `composer mw-install:sqlite
-- --with-extensions`. `sbx/bin/git-safe-reset` and
`sbx/bin/git-review-check` ship in the kit's `files/home/bin/` and get
installed onto `PATH` first. Unit-tested only — no real sandbox has run
it end to end yet.

Four things that are load-bearing and not obvious from the code:

- **The daemon starts *before* the MediaWiki phase now**, not after.
  Composer and npm are minutes; the git daemon is what the host reviews
  through, and nothing below it depends on it. This is the change that
  makes a long `sbx create` inspectable while it runs.
- **`--force` on `git safe-reset` is the correct semantics, not a
  workaround.** `git-safe-reset` runs `git-review-check`, which refuses if
  HEAD isn't uploaded to Gerrit. That guard protects unpushed work — of
  which a clone made seconds ago from a read-only original has none, so
  without `--force` any unpushed commit the host happened to have in a
  *dependency* would abort setup for nothing. The dirty-tree guard stays
  live; it can only fire on a re-run, which is exactly when it should.
- **`.env` and the kit environment come from the same parser.** `.env` is
  read by `docker compose` and nothing else — core's `Gruntfile.js` and
  `wdio-defaults.conf.js` read `process.env.MW_SERVER`/`MW_SCRIPT_PATH`
  directly — so the file alone would do nothing here. The values that
  matter go into the kit's `environment.variables`, and both sides call
  `parse_install_params()` against core's own `composer.json`
  (`mw-install:sqlite`), which is the authority. Verified against the real
  script fetched from gitiles: change the script's `--server`, and both
  follow. This is the one place anything imports `wmf_sbx_setup` (it can't
  import anything itself — it ships as a single standalone file).
- **The whole chain is gated on a writable core**, the same precondition
  the symlinks have. Without one there's nothing to link into and no wiki
  to install, so resetting and composer-updating a dozen dependencies
  would be minutes of work for nothing.

Failure policy, per the design: the per-repo steps (safe-reset, composer
update) warn and continue — one broken extension shouldn't cost the whole
sandbox, and it can be fixed from inside — while `.env`/npm/install are
fatal, because a sandbox without a working wiki is the thing it was
created for. Warnings are also summarised at the end, since `sbx create`'s
output is long and composer's is longer.

`EXTRA_DOMAINS` now names `packagist.org` and `registry.npmjs.org`
alongside `github.com`. All three are redundant against sbx's own
`default-package-managers` / `default-code-and-containers` local policies
(§24), but those are `source: local` — a machine with a narrower set would
silently lose the ability to install dependencies, and a kit list can only
narrow, never widen, so declaring them states a requirement without
granting anything.

## 30. First real sandbox: `wmf-sbx-create Translate` — MEASURED **[cananian, host + in-sandbox, 2026-09-08]**

Everything in §27–§29 had only ever been unit-tested. This is what it
does against a real `sbx create`. Sandbox `mw-translate`, closure
`Translate → core + UniversalLanguageSelector + Vector`, host home
`/home/cananian`, repos under `~/Projects/Wikimedia/`.

**Worked, first try, no changes needed:**

- The whole chain ran to completion in **64.9s** — apt (64.8s, a separate
  step) plus clone × 4, safe-reset, `composer update` × 3, symlinks,
  `.env`, `npm ci`, and `composer mw-install:sqlite -- --with-extensions`.
  That closes the "how long does the closure actually take" question far
  more cheaply than expected. (Translate's closure is small; a Wikibase-
  sized one is still unmeasured.)
- **The plan file round-trips.** `wmf-sbx-plan.json` in the sandbox had
  exactly the four repos, the right canonicals, and
  `linkName`/`linkDir` on the three non-core ones.
- **`--with-extensions` does what §7 of the dependency-walk design assumed:**
  `LocalSettings.php` came out with `wfLoadSkin( 'Vector' )`,
  `wfLoadExtension( 'Translate' )` and
  `wfLoadExtension( 'UniversalLanguageSelector' )`. `Special:Version`
  lists all three.
- **The wiki serves.** `composer serve` → `curl
  'http://localhost:4000/index.php?title=Special:Version'` → 200.
- **`sudo -u agent -H` is fine in the image.** Nothing under the parallel
  tree was root-owned except the symlinks (below) — the clones, their
  checkouts, `vendor/`, `node_modules/` and `.env` are all `agent:agent`.
- **Every clone came out clean** (`git status --porcelain` empty in all
  four) after composer/npm.
- **The daemon comes up and the host can fetch through it.** `git fetch
  sandbox-mw-translate` worked from both `~/Projects/Wikimedia/Extensions/Translate`
  and `~/Projects/Wikimedia/core`; `wmf-sbx-rm --dry-run` listed exactly
  the four remotes and the state file. The process is `git-daemon`, not
  `git daemon` — `pgrep -af 'git daemon'` finds nothing.
- **The helpers landed**: `/usr/local/bin/git-safe-reset` and
  `git-review-check`, mode 755.
- **The read-only originals hold, verified by attempting the bypass.**
  `touch` fails; `sudo mount -o remount,rw` *reports success*;
  `/proc/mounts` then even says `rw` for that path; `touch` still fails
  with `Read-only file system`. Exactly the §22 / docker/sbx-releases#556
  behaviour, now confirmed on our own mounts rather than inferred.

**Three bugs, all fixed 2026-09-08:**

1. **`MW_INSTALL_PATH` pointed at the host mirror, not the clone.**
   `build_kit_spec` set every `REPO_ENVIRONMENT_VARS` variable to the
   *host* path it resolved. sbx mounts the host repo at that same literal
   path inside the sandbox, so the variable resolved to something real —
   the read-only mirror. `php maintenance/run.php version` therefore read
   the **host's** `LocalSettings.php`, which `wfLoadSkin( 'MinervaNeue' )`s
   a skin that was never mounted, and died on a missing `skin.json`.
   Fixed: the variables now go through `parallel_path()` (repos outside
   `hostHome` keep their literal path, since that's genuinely all they
   have).
2. **`sudo chown -h` left every symlink root-owned.** The links in
   `core/extensions/` and `core/skins/` came out `root root`, even though
   `link_into_core` shelled out to `sudo chown -h agent:agent` and every
   *other* `sudo chown` in the script (the clone's `-R`, `.env`'s) worked.
   Cause not established — the create-time stderr wasn't kept, and a
   warning would have been printed. Cosmetic in practice (traversal uses
   the target's permissions, and the containing directory is
   agent-owned, so the agent can still replace a link), but fixed the way
   that removes the whole question: `os.lchown` directly. The script is
   already root; no subprocess, no `PATH`, no sudo, no `chown`
   implementation differences, and a real exception on failure.
3. **`--reference` copied the object store instead of borrowing it.**
   `core/.git` was **685 MB** in the sandbox. `git clone --reference X X
   dest` writes `objects/info/alternates` *and* runs git's local-clone
   object copy — hardlinks when it can, and it cannot here: the original
   is a virtiofs mount from the host, the parallel tree is the
   container's writable layer. Measured directly: `--reference` leaves a
   full-size `.git/objects`, `--shared` leaves ~1 KB, and both write the
   same alternates line. Switched to `--shared`. Verified that a
   `--shared` clone still serves correctly over `git://`, which is how the
   host fetches.

   `--shared`'s documented danger — the source pruning objects the clone
   still needs — **is** live here, contrary to what this note first
   claimed. The read-only remount only stops the *sandbox* writing; the
   engineer's own shell can still `git gc` the original and collect
   objects the clone reads through the alternate. Handled on the host
   side instead: `wmf_sbx_remotes.suspend_gc` sets `gc.auto=0` /
   `gc.pruneExpire=never` in every repo it registers a `sandbox-<name>`
   remote in, stashing the previous values in that repo's own config
   under `wmfSbx.saved*`, and `resume_gc` puts them back once the last
   sandbox remote is gone (§31). Known gap: a repo we clone but fail to
   add a remote to — the "name-taken" skip — keeps gc enabled.

**Two open questions this raised, neither a bug:**

- **`MW_SCRIPT_PATH` is the empty string, not `/w`.** That's correct
  *here* and shouldn't be "fixed": `/w` is the docker-compose layout,
  where core is mounted at `/var/www/html/w`. We serve with `composer
  serve`, whose docroot **is** the core directory, so `index.php` is at
  the root. The value isn't hardcoded either way — it's parsed out of
  core's own `mw-install:sqlite` script (`--scriptpath=`), so it tracks
  whatever core does. Confirmed live: `index.php?title=Special:Version`
  returns 200.
- **`git safe-reset` in a dependency clone resets to the *host's* master,
  not Gerrit's.** The clone's only remote is `origin` → the read-only
  host mirror, so the "upstream" it resets to is whatever the engineer
  has locally. That's arguably the right default (the sandbox mirrors the
  host's world, and it costs no network), and it still does the job it
  was added for — a dependency checked out on a WIP branch on the host
  lands on `master` in the sandbox. But it is *not* "reset to upstream",
  and the observed core clone sat on cananian's local WIP commit
  `9e1ce684a48 [WIP] API: add action=parsoidoffsets` rather than Gerrit's
  master. **Settled in §31**: the remotes get swapped so `origin` means
  Gerrit and `local` means the host mirror.

**One real wart, unfixed:** the agent starts in the read-only host mirror
and can't get itself out. `HOME_CLAUDE_MD` tells Claude to run `/cd
/home/agent/<rel>` first thing; in the real session Claude read it,
correctly worked out the writable clone's path, and then reported that
"there's no `/cd` tool available in this session" — because there isn't.
`/cd` is a slash command the *user* types; a model can't invoke one. So
the instruction as written is unachievable, and cananian had to type
`/cd` by hand. Options, none implemented:

- have `wmf-sbx-create` print the exact `/cd` line to paste after
  attaching (cheap, honest, still manual);
- reword `HOME_CLAUDE_MD` to tell Claude to *ask* the user to run it, and
  to prefix its own commands with `cd <clone> && ` meanwhile (cheap,
  removes the false claim of a capability);
- bind-mount each clone *over* its host-mirror path, so the starting cwd
  simply is the writable clone and no `/cd` is needed at all. Cleanest
  for the user, and it hides the host's real files entirely — but it
  shadows the very path `--shared`'s alternates now point at, so the
  originals would have to be bind-mounted somewhere stable first. A
  §15-level change, not a tweak.

## 31. Remotes that mean what they say, and gc that stays out of the way [2026-09-08]

Three follow-ups from §30, all of them cananian's calls.

### 31.1 `mount --move` works in-sandbox — but won't survive a restart on its own [MEASURED, in-sandbox]

Tested in this dev sandbox against `/home/agent/Projects/Wikimedia/docs.docker.com`, a read-only virtiofs mount like the ones `sbx` creates:

| step | result |
| --- | --- |
| `sudo mount --move <mount> ~/.sbx-originals/<rel>` | exit 0 |
| the vacated path afterwards | an empty `root root` directory in the overlay; after `chown agent:agent`, writing a file there works |
| the mount at its new location | still read-only (`touch` → "Read-only file system") |
| `mount --move` back | exit 0, contents intact |

So the idea from §30's third bullet — move the sbx mounts aside and build
the clone tree where they used to be, so the starting cwd *is* the
writable clone — is mechanically available.

**It will not survive `sbx stop` / `sbx run`.** `mount --move` mutates the
running container's mount namespace; stop/start is docker stop/start,
which rebuilds the namespace from the container config, where the bind
mounts are recorded against their original paths (§20). After a restart
the sbx mount reappears at its original path and *shadows* the clone
built underneath — the clone isn't destroyed, it just becomes
unreachable, which is a worse failure than not doing it at all. Making
this work needs the move re-run at every container start. The mechanism
exists — `sbx create` prints "register 3 startup command(s), run on every
container start" for the `claude` kit — but the kit-spec key for it is
**unconfirmed**; check `~/Projects/Wikimedia/mediawiki-kit` before
building on this.

### 31.2 `origin` now means Gerrit; the host mirror is `local`

`git clone` names what it cloned from `origin`, which here is the host's
read-only checkout — so `git safe-reset` reset a dependency to whatever
branch the engineer's own checkout happened to be sitting on (§30). The
names now get swapped in `wmf_sbx_setup.configure_remotes`:

- `git remote rename origin local` — the host mirror keeps its usefulness
  (fast, offline, and it has whatever the engineer has that upstream
  doesn't) under a name that says what it is;
- `git remote add origin https://gerrit.wikimedia.org/r/<project>` — the
  URL is built host-side by `wmf_sbx_create.clone_url` (the same mapping
  `--dry-run` clones with) and carried into the sandbox as the plan's new
  per-repo `upstreamUrl`, because the setup script can't import it;
- `git fetch origin` — cheap despite appearances: the alternates line
  already makes every object the host had reachable, so this transfers
  only what upstream has moved on by.

`git_safe_reset` then names its remote explicitly rather than relying on
`git-safe-reset`'s own `origin` default, because which remote is right
depends on whether the swap completed. It falls back to `local` (with a
warning) on an unknown forge or a failed fetch, and repos with no
canonical keep the mirror as `origin` — inventing a `local` that means
something different from repo to repo would be worse than the wart.

https, not `ssh://`: the sandbox deliberately has no SSH agent (§8), so
anonymous http is the only fetch that can work. Nothing pushes from
inside the sandbox anyway; the host fetches from the sandbox's daemon.

### 31.3 gc suspension in the host repo

See the correction in §30's third bug. `sync_remotes` calls `suspend_gc`
right after it sets the ownership marker; `remove_remotes` calls
`resume_gc` after each entry, which restores only once no marked remote
is left in that repo (so two sandboxes sharing one host repo don't
un-protect each other). Details worth keeping:

- **`--local` on every read and write.** Without it we'd read a value
  inherited from `~/.gitconfig` and then write that inherited value into
  the repo on restore, quietly pinning a global setting to one repo.
- **An explicit `(unset)` sentinel**, because "the key was unset before
  we touched it" (unset it again) and "we have no record of this key"
  (leave it alone) must not restore the same way. The second case is what
  a half-finished suspend leaves behind, and treating it as the first
  would destroy a setting that was the engineer's all along.
- **Idempotence via a `wmfSbx.gcSuspended` marker, not by comparing
  values** — otherwise the second sandbox stashes the first one's
  `gc.auto=0` as if it were the engineer's own.

## 32. Second real sandbox: `wmf-sbx-create Translate Parsoid` — MEASURED **[cananian, host + in-sandbox, 2026-09-08]**

The run that checked §31's work. Five repos (Translate and Parsoid named,
core + ULS + Vector discovered), 77.8s in the `wmf-sbx-setup` install step
on top of 65.3s of `apt-get`.

**What §30's fixes and §31's work got right.** `MW_INSTALL_PATH` points at
`/home/agent/Projects/Wikimedia/core` and `php maintenance/run.php Version`
runs with no override (1.47.0-alpha); all four symlinks are `agent agent`;
`core/.git` is 15 MB with an alternates line into the host's object store
(`--reference`'s 685 MB copy is gone); every clone shows `local` = host
mirror and `origin` = Gerrit over https; core and Vector are both reset to
`origin/master` and away from the host's own commit; and the gc suspension
lands exactly as designed — `gc.auto=0`, `gc.pruneExpire=never`, two
`(unset)` sentinels and `wmfsbx.gcsuspended=true` in all five host repos
while the sandbox is up, and *nothing* left in any of them after
`wmf-sbx-rm`. That closes the "confirm the gc suspension end to end" item.

### 32.1 Parsoid was silently not reset — the remote swap's own regression

Parsoid's clone was still on `pfragment-claude-stack`, the host's WIP
branch. Reproduced in a scratch tree, root cause confirmed: the host's
HEAD was a topic branch, so `git clone` created no local `master`; §31.2
then added a second remote that also has a `master`; and `git checkout
master` — `git-safe-reset`'s first command — refuses to guess between
them:

```
hint: ... consider setting checkout.defaultRemote=origin in your config.
fatal: 'master' matched multiple (2) remote tracking branches
```

exit 128. core and Vector escaped it only because their host HEAD was
already `master`, so the local branch existed and there was nothing to
DWIM. **This is a regression §31.2 introduced**: with one remote the
checkout was never ambiguous. Fixed by taking git's own advice —
`configure_remotes` now also sets `checkout.defaultRemote` to whichever
remote safe-reset will use. Verified in the same scratch tree: the
checkout then succeeds and tracks `origin/master`.

It failed **silently**, which is the more interesting half. The setup
script did append a warning, but `sbx create` collapses the whole install
step to one line — `✓ python3 /home/agent/wmf-sbx-setup ... (77.8s)` — so
nothing the script printed reached the terminal. Anything we want an
engineer to actually see has to survive that collapse; still open.

### 32.2 Which clones keep the host's branch (cananian's call)

> "I think you're right: we shouldn't reset to origin anything which is
> mentioned on the command-line. I might change my mind about this,
> though, so maybe add a `--reset-all` option to reset everything
> including the primary. If I'm listing a bunch of directories on the
> command line I might prefer to `--reset-all` and then just adjust the
> few I'm continuing work-in-progress on."

Implemented: per-repo `"requested"` in the plan (from the paths
`expand_dependencies` did *not* discover), `"resetAll"` from
`wmf-sbx-create --reset-all`, read by
`wmf_sbx_setup.repos_to_leave_alone`. Mechanics in
`DESIGN-setup-steps.md` §8.2.

### 32.3 `sbx run NAME` is deprecated in 0.39

`sbx run mw-translate` still works but warns: "use `sbx run --name
mw-translate` instead" — the positional is the workspace path now.
`wmf-sbx-create`'s reconnect message emits the `--name` form.

### 32.4 `wmf-sbx-rm`'s daemon guard fired — not a regression

`wmf-sbx-rm mw-translate` refused with `errno=Connection refused` against
all five repos, which looks like a regression from §31's work but isn't:
the git daemon is started by the kit's `commands.install`, so it dies with
the container and never comes back (§15.4, and the error message says so).
It was reachable in Group 3 and gone by Group 9 because `sbx exec` **starts
a stopped sandbox** ("Sandbox mw-translate started successfully" precedes
every Group 4–8 result) — each exec had been reviving a container whose
one-shot install step never re-ran. `--force` completed cleanly.

This is the concrete case for moving the daemon to a kit *startup* command
— the thing that does run on every container start (§31.1).

### 32.5 kit-spec v2, and `sbx kit`

- `sbx kit validate ~/Projects/Wikimedia/mediawiki-kit/` → `VALID`, with
  `WARN: deprecated field "network.allowedDomains": use
  'permissions.network.allow' instead (kit-spec v2)`. `wmf_sbx_kit.py`
  still emits the v1 key.
- `validate` wants a directory or a ZIP, not `spec.yaml` ("zip: not a
  valid zip file").
- `sbx kit inspect` takes a reference — local dir, ZIP, OCI, or
  `git+https://...` — **not** a bare kit name, so there is no way to dump
  the built-in `claude` kit's spec and read the startup-command key off
  it.
- `sbx kit --help` names what a kit can carry: "credentials, network
  policies, environment variables, **startup commands**, and files".

## 33. kit-spec v2 and the startup daemon — implemented 2026-09-08

Source: the freshly-mirrored `docs.docker.com/ai/sandboxes/customize/`
(`kit-reference/index.html`, `kits/index.html`). v2 has been the spec
since sbx 0.36; our generator was still emitting the 0.31-era v1 shape.

### 33.1 The v1 → v2 renames

| v1 | v2 |
| --- | --- |
| `network.allowedDomains` / `deniedDomains` | `permissions.network.allow` / `.deny` |
| `commands:` / `commands.initFiles` | `setup:` / `setup.files` |
| `network.publishedPorts`, `publishedPorts` | top-level `ports:` |
| `memory`, `agentContext` | `agentInstructions.content` |
| `kind: agent` / `agent:` | `kind: sandbox` / `sandbox:` |
| `credentials.sources.<id>` | `credentials:` list, keyed by `service` |
| `tmpfs:` | `volumes:` entries with `type: tmpfs` |
| `settings:`, `kitDir`, `persistence` | removed |

These are not merely deprecated aliases: in a `schemaVersion: "2"` spec a
v1 field is a **decode error**, so the migration is all-or-nothing. The
generated spec is now `schemaVersion: "2"`, `kind: mixin`, with
`permissions.network.allow` and `setup.install`;
`test_no_v1_field_survives_in_a_v2_spec` pins that no v1 key creeps back.

### 33.2 `setup.startup` — the run-on-every-start hook

The key §32.4 was blocked on. Three load-bearing details from the
reference, each of which the naive guess gets wrong:

- **`command` is an argv array, not a shell string.** No `&&`, no `|`, no
  redirection unless you spell `["sh", "-c", "..."]` yourself.
- **`user` defaults to `"1000"`** (agent) — the opposite of
  `setup.install`, which defaults to `"0"` (root).
- **`background: false` (the default) blocks *later startup commands*
  until this one exits** — it never gates the agent entrypoint. A daemon
  therefore needs `background: true` or it stalls the rest of the list.

And the contract the docs state outright: startup commands "must be
idempotent … they run on every sandbox start and replay on container
restarts."

`wmf_sbx_kit.daemon_startup_command()` emits

```sh
if python3 -c '...connect_ex(("127.0.0.1", 9977)) == 0 → exit 1...'; then
  exec git -c 'safe.directory=*' daemon ... --port=9977 /home/agent
fi
```

wrapped as `["sh", "-c", <that>]`, `user: "1000"`, `background: true`.
The probe inverts its exit status, so the guard is "start it only if
nothing is already listening". The `if`/`fi` form matters: the obvious
`probe && exec daemon` **exits 1 when it skips**, which would log a failed
startup command on every restart. MEASURED in the dev sandbox: first run
starts the daemon, second run exits 0 and leaves it alone, and
`git ls-remote git://127.0.0.1:PORT/sub` serves refs through it.

The argv is `wmf_sbx_setup.daemon_argv()`, the same function
`start_daemon` uses, so the install-time and every-start daemons can't
drift apart (`test_the_startup_daemon_is_the_same_command_the_install_step_starts`).
`start_daemon` stays in the install step: the positional/no-kit path has
no kit to carry a startup command, and the guard makes the pair safe.

Still host-unverified: that sbx actually runs this at create and again
after `sbx stop` + `sbx run --name`.

## 34. Third sandbox: the startup command runs, the port moves — MEASURED **[cananian, host, 2026-09-08]**

`sbx/bin/wmf-sbx-create Translate Parsoid`, then a `stop`/start cycle.

### 34.1 `setup.startup` works, exactly as documented

- `sbx kit validate /tmp/mwkit` → `VALID`, **no warnings** (§33.1's v2
  migration). `requires:` and `environment:` survive v2's stricter
  decoder, which was the one field group I couldn't check against the
  docs mirror.
- `sbx create` reports `register 4 startup command(s), run on every
  container start` — the `claude` kit's 3 plus ours, listed as
  `+ sh -c if python3 -c 'import socket,sys; sys.exit(1 if socke…
  (kit=mediawiki-kit, background, user=1000)`. It also prints
  `view startup log: cat /var/log/sbx-kit-startup.log`, so the log path I
  had guessed at is real.
- After `sbx stop` + a restart, `pgrep -af git-daemon` inside the sandbox
  shows **one** daemon on 9977. §15.4's gap — "nothing restarts the
  daemon after `sbx stop`" — is closed, and the guard kept it to one
  process.
- Note for future probing: `pgrep -f 'git daemon'` finds **nothing**; the
  process re-execs as `/usr/lib/git-core/git-daemon`, so match
  `git-daemon`.

### 34.2 The published host port moves on every container start

The daemon came back, but `git ls-remote sandbox-mw-translate` still
failed with `errno=Connection refused`. sbx re-applies the publish itself
on start — and assigns a **new ephemeral host port each time**:

    before stop:   git://127.0.0.1:32783/...   (recorded at create)
    after restart: 127.0.0.1  32784  ->  9977  (per `wmf-sbx ports`)

So every URL in the host repos is stale from the first restart onward.
This is the *second* half of §32.4, and the one the daemon fix alone
could never have solved. cananian's own words: "Note that when the git
daemon came up again, it came up on a different port, one higher than
before."

Fixed by never trusting a recorded port: `wmf_sbx_create.refresh_host_port`
looks the mapping up (publishing again if there is none), rebuilds each
URL from the remote's recorded `sandboxPath`, and hands the result to
`sync_remotes`, which re-points anything carrying our marker. Callers:

- **`wmf-sbx-rm`**, before the unfetched-work guard probes anything —
  otherwise the guard reports every remote as unreachable and pushes the
  engineer to `--force`, destroying exactly the work it exists to
  protect.
- **`wmf-sbx-resume NAME`** (new), which is now what `wmf-sbx-create`
  prints as the reconnect command. It starts the sandbox (`exec … true`
  — a stopped sandbox publishes nothing to look up), re-points the
  remotes, then hands over to `wmf-sbx run --name`. It also carries the
  §20 job: `resumeArgs:` in `~/.config/wmf-sbx/repos.yaml` is the agent
  CLI tail to re-apply on every attach, since that doesn't survive a
  stop. Anything after `--` overrides it for one invocation.

### 34.3 The reset exemption and the DWIM fix hold up

Parsoid came up on `pfragment-claude-stack` — the host branch — while
core and UniversalLanguageSelector are on `master`, and every clone
carries `checkout.defaultRemote=origin`. That is both §32.1's ambiguous
checkout fixed and §32.2's "don't reset what the engineer named" working
in a real sandbox. The plan file confirms the flags upstream of it:
`requested` true for Translate and Parsoid only, `resetAll` false.

Not re-checked, because the walk only descends one level: Translate
itself and Vector, which live in `Extensions/` and `Skins/`
subdirectories.

## 35. Publishing in the kit, and the first-attach `--continue` problem

Two changes cananian asked for after §34's round trip, both about doing
the port/attach plumbing in its final form rather than debugging a
temporary one.

### 35.1 The daemon's port is declared by the kit

`build_kit_spec` now emits v2's top-level

    ports:
    - container: 9977
      name: git-daemon

instead of relying on `wmf-sbx-create` to run `sbx ports --publish` after
create. The difference is *when*: a post-create publish covers the one
container start that happened to follow it, while a declared port is
re-published on every start. (This closes the deferred still-to-do item
from §33.1; the argument against it — "publishing at create time also
means publishing for sandboxes nobody browses" — is thin next to a
daemon that is unreachable until someone remembers to publish it, and the
mapping is localhost-only either way.)

No `protocol:`, because the reference says an empty protocol publishes
**IPv4 only** (`127.0.0.1`), which is what the daemon wants. ~~`tcp` also
publishes `::1`, where an IPv6-first client can pick the dead half and
get a connection reset.~~ **Wrong, or at least not the whole story: on
the host, an empty protocol published both families anyway (§36.3).** It
costs nothing — every URL we write names `127.0.0.1` literally.

This does **not** make the host port stable — it is still ephemeral and
still moves on every start (§34.2). `wmf_sbx_create.publish_daemon_port`
therefore became `ensure_published_host_port`: look the mapping up first,
publish only if there is none. The publish path stays for an explicit
`--kit` that doesn't declare the port.

### 35.2 `--continue` is the default tail — from the *second* attach

sbx 0.39 no longer needs `--dangerously-skip-permissions`, so the tail
worth defaulting is `--continue`. But cananian hit the obvious edge:

> the very first execution of claude after creation needs to have no
> arguments, since there's nothing to continue yet

— claude exits 1 with "No conversation found to continue" rather than
starting a fresh session. So the state file gained an `attached` flag
(`wmf_sbx_state.new_state`; `SCHEMA_VERSION` deliberately not bumped,
since every reader uses `.get`). `wmf-sbx-resume` sets it after a
*clean* attach and only adds `--continue` once it is set. A non-zero exit
doesn't set it: a run that died on the way in started no conversation,
and a sticky flag would make every later resume ask for one that never
existed.

Related behaviour, all in `wmf_sbx_resume.default_agent_args`:

- `resumeArgs: --continue` in `repos.yaml` is the natural thing to write,
  so a config-supplied `--continue`/`-c` is *stripped* on the first
  attach rather than passed through into an immediate exit 1.
- A tail that already names a session (`--continue`, `-c`, `--resume`,
  `-r`) is left alone — two answers to the same question.
- An explicit `--` tail on the command line is passed through verbatim.
  `--no-continue` opts out for one invocation.

### 35.3 `wmf-sbx-rm` starts a stopped sandbox instead of refusing

Also from §34's round trip: `wmf-sbx-rm` on a *stopped* sandbox failed —
the unfetched-work guard's port lookup found nothing, and
`ports --publish` against a stopped sandbox returns
`500 … no container endpoint with IP address found`. The guard then
reported every remote unreachable and refused, leaving `--force` as the
only way out. Removing a sandbox you stopped weeks ago is the *normal*
case, so the guard now calls `create_mod.start_sandbox` (`exec … true`,
the only thing that starts a stopped sandbox — there is no `sbx start`)
and retries the lookup before giving up.

## 36. Fourth sandbox: the kit publishes, the guard races — MEASURED **[cananian, host, 2026-09-08]**

`sbx/bin/wmf-sbx-create --kit-out /tmp/mwkit4 Cite`, then attach twice,
`stop`, and `wmf-sbx-rm`. §35's three changes all did what they were
meant to; the round trip found two new things.

Confirmed working:

- **The kit publishes its own port.** `sbx create` printed
  `Published git-daemon: localhost:32794 -> 9977/tcp` on its own — no
  `ports --publish` call from us — and `sbx kit validate` still says
  `VALID`. The `name:` label shows up in that line, which makes it worth
  having.
- **`--continue` gating.** First attach:
  `wmf-sbx run --name mw-cite`. Second: `... -- --continue`, and claude
  resumed session `1452362e…` rather than starting a new one. State shows
  `"attached": true`.
- **`wmf-sbx-rm` on a stopped sandbox** no longer dead-ends — it started
  the sandbox and got as far as probing (see §36.1 for how far).

### 36.1 The container is up before the daemon is

The first `wmf-sbx-rm` after `sbx stop` still refused:

    warning: `wmf-sbx ports mw-cite --publish 9977` failed (exit 1): ERROR: … 500 …
      no container endpoint with IP address found
    error: could not reach the sandbox's git daemon …
      /home/cananian/Projects/Wikimedia/core (sandbox-mw-cite): fatal: read error: Connection reset by peer

The same command a moment later worked. cananian's read — "looks like a
race condition to me: the first time … something hadn't fully completed
or started up yet" — is right, and the error text says which half. The
first line is the *expected* stopped-sandbox failure that triggers the
auto-start (§35.3). What follows is the race: `sbx exec … true` returns
as soon as the container is up and the startup commands have been
*launched*, and ours is `background: true`, so the daemon is still
starting when the probe arrives.

**`Connection reset by peer`, not `Connection refused`, is the tell.**
Docker's port proxy accepts the connection on the published host port
whether or not anything is listening behind it, and resets once it finds
nothing. So a host-side connect can't distinguish "ready" from "too
early" — which is why `wmf_sbx_create.wait_for_daemon` probes *inside*
the sandbox (`sbx exec … python3 -c 'connect_ex(("127.0.0.1", 9977))'`),
retrying up to 20 times at 0.5s. `wmf-sbx-rm`'s guard calls it between
the start and the re-point.

### 36.2 The guard counted Gerrit's commits as the agent's work

The second run got past the daemon and refused for a different reason: 9
unfetched tips in `core`, 5 in `Vector`. None of it was agent work —
cananian: "the gerrit origin remote was more recently fetched inside the
sandbox than it was on my host."

`git daemon` advertises the whole of `refs/`, remote-tracking refs
included, and `unfetched_tips` was treating every non-tag ref as
candidate work. So a sandbox that ran `git fetch origin` looked like a
pile of unrecoverable commits. That is the guard crying wolf, and a
guard that cries wolf is a guard that trains you to type `--force`.

`unfetched_tips` now skips `refs/remotes/*` outright, and skips any other
ref whose sha *equals* a remote-tracking tip (a branch sitting exactly on
`origin/master`, which is what every clone looks like until the agent
commits). Both classes are recoverable from the origin the sandbox pulled
them from; neither dies with the sandbox. Anything the agent actually
committed on top is still reported —
`test_agent_commits_on_top_of_upstream_are_still_reported` pins that, and
all three new tests fail against the old logic.

### 36.3 An empty `protocol:` does not mean IPv4-only

The kit-reference claim I built on in §35.1 didn't hold:

    HOST IP     HOST PORT   SANDBOX PORT   PROTOCOL
    127.0.0.1   32794       9977           tcp
    ::1         32794       9977           tcp

Both families, from a `ports:` entry with no `protocol`. Harmless here —
every URL we write names `127.0.0.1` literally, so the `::1` half is
never dialled — but the comment in `build_kit_spec` now says "following
the reference" rather than claiming a behaviour we've measured otherwise.

### 36.4 A pre-existing remote in Cite

Also in that output, and *not* a bug:

    warning: …/Extensions/Cite: remote 'sandbox-mw-cite' already exists and
    isn't ours (marker: none) -- leaving it alone.

§4 of `DESIGN-host-remotes.md` working as designed: a remote with no
`wmfSbxSandbox` marker is a remote a human made, and we neither steal the
name nor silently re-point it. Worth clearing by hand
(`git -C …/Extensions/Cite remote remove sandbox-mw-cite`) if it's a
leftover from before this tooling existed — otherwise Cite is the one
repo of the three that never gets a working sandbox remote.

## 37. Fifth sandbox: the round trip is clean — MEASURED **[cananian, host, 2026-09-08]**

`wmf-sbx-create Cite` → attach → attach → `stop` → `wmf-sbx-rm`, with
§36's two fixes in. Everything §36 found is gone:

- the kit published `git-daemon: localhost:32796 -> 9977/tcp` itself
  during `sbx create`, no `ports --publish` from us;
- the first attach ran `wmf-sbx run --name mw-cite` with no trailing
  args;
- `wmf-sbx-rm` on a **stopped** sandbox succeeded on the *first* try —
  auto-start, daemon wait, re-point, zero unfetched tips, and all three
  host remotes removed.

One cosmetic defect left, fixed below.

### 37.1 Don't announce the failure you went there to cause

The only noise in the transcript was the first line of `wmf-sbx-rm`:

    warning: `wmf-sbx ports mw-cite --publish 9977` failed (exit 1): ERROR: … 500 …
      no container endpoint with IP address found

which printed *before* the auto-start that then made everything work. A
stopped sandbox is the case this path exists for, and its publish failure
is a precondition, not news — printing it makes a command that worked
look like one that didn't, and teaches the engineer to ignore the word
`warning:` from this tool.

`quiet=` is now plumbed through `publish_daemon_port` →
`ensure_published_host_port` → `refresh_host_port`, and
`wmf_sbx_rm.check_unfetched` passes `quiet=True` on the *speculative*
first refresh only. The retry after the auto-start stays loud: a failure
there is genuine and has to explain itself.

The test for this passed vacuously at first (the fake failed `--publish`
unconditionally, so the retry never succeeded and the loud path never
ran). `test_wmf_sbx_rm.fake_run` now models a stopped-then-running
sandbox — `ports` returns `[]` and `--publish` returns the real 500 text
while stopped, and only `exec` flips it — which is what makes the
assertion mean anything.

### 37.2 `git safe-reset origin/master`

Requested QoL: all four of `git safe-reset`, `… origin`, `… origin
master` and `… origin/master` now mean the same thing. The slash form is
how git prints a remote-tracking branch, so it's the form that's in your
scrollback.

Split on the **first** slash only (branch names routinely contain
slashes, remote names don't), only when no BRANCH argument followed, and
only when the leading component is a real remote — so a remote literally
named `weird/name` still wins over the convenience.

The same change adds an explicit unknown-remote check, because the
failure mode without it is unreadable: `git safe-reset nosuch/master`
took `nosuch/master` as the remote, fell through to the `main` branch
fallback (no `refs/remotes/nosuch/master/master` exists), and died three
commands later with `pathspec 'main' did not match any file(s) known to
git`. It now says `No such remote 'nosuch/master'` and lists the ones
there are.

`tests/test_git_safe_reset.py` is the first test for this script, and it
drives **real repos in tempdirs** — the script's whole job is git's
behaviour, so faking git would only re-assert my reading of it. Internal
callers are unaffected: `wmf_sbx_setup.reset_to_upstream` passes a bare
remote name.

## 38. The setup report: `/var/log` plus an `exec` to read it back [2026-09-08]

Closes §32.1. cananian picked the shape: "it appears that various things
log to /var/log (ie /var/log/sbx-kit-startup.log) so let's log errors to
/var/log and then have `wmf-sbx-create` read it back out with an `exec`
after create."

**Sandbox side.** `wmf_sbx_setup.main` now swaps `sys.stderr` for a
`SetupLog` for the duration of the run. It writes through to the real
stderr (so nothing is *lost* to `sbx create`'s log, just still collapsed),
mirrors everything into `/var/log/wmf-sbx-setup.log`, and collects every
line starting `error:` or `warning:` — the two prefixes this script has
used all along, so no call site had to change. At the end it writes
`/var/log/wmf-sbx-setup.status`:

```json
{"version": 1, "exit": 1, "problems": ["warning: git safe-reset origin failed in /home/agent/Parsoid"],
 "log": "/var/log/wmf-sbx-setup.log"}
```

**Host side.** `wmf_sbx_create.report_setup_problems` runs
`wmf-sbx exec NAME -- cat /var/log/wmf-sbx-setup.status`, and prints the
problems (plus the exact `exec ... cat` for the full log). Called *before*
the `sbx create` returncode check — a create that failed inside the setup
step is when the report matters most — and `quiet` on that path, since a
create that died earlier may have left no sandbox to exec into and
"couldn't read the report" would be the wrong headline (§37.1 again).

Details worth keeping:

- **Only the script's own narration is captured.** The commands it runs
  (composer, npm, git) inherit the real fd 2 and are untouched. That is
  the point: their combined output is tens of thousands of lines, and a
  report nobody reads is what we already had.
- **A crash is a report too.** `main` catches everything, puts the
  traceback in the log and one `error: wmf-sbx-setup did not finish` line
  in the status. An unhandled exception was previously the single most
  invisible failure available.
- **The status is versioned and the log paths come from one function
  each** (`setup_log_path`/`setup_status_path`), because unlike the plan
  file the two ends really can be different versions: the status is
  written by the script frozen into the kit at create time and read by
  whatever `wmf-sbx-create` the engineer runs later.
- **`log_dir=None` disables both**, which is what the tests and hand
  invocations use; a `/var/log` that can't be written costs the report and
  nothing else (verified by running the script as a non-root user).
- Only reported for a kit we generated — an explicit `--kit` has no
  `wmf-sbx-setup` in it, so there is no report to miss.

## 39. The clone takes the host repo's own path [2026-09-08]

§30's "one real wart": the agent starts in the read-only host mirror and
cannot `/cd` itself out. cananian's call is §30's third option — "use
`mount --move` to move the original read-only bind-mount host directory
`~/Foo` to `~/.sbx-originals/Foo` … and then creating the clones in the
pathnames originally occupied by the read-only mounts. Then we don't have
to change Claude's idea of its working directory (because it's the same!)"

Per writable repo under `hostHome`, `setup_repo` now does:

| step | why this order |
| --- | --- |
| `mount --move <literal> /home/agent/.sbx-originals/<rel>` | frees the literal path *before* anything points at it |
| `git clone --shared <originals>/<rel> /home/agent/<rel>` | so the alternates line names a path the alias won't shadow |
| `mount -o remount,ro,bind <originals>/<rel>` | same lock-down as before, at the mirror's new address |
| `mount --bind /home/agent/<rel> <literal>` | the literal path now *is* the clone |

The parallel tree is untouched by all this: the clone is still really at
`/home/agent/<rel>`, still what the daemon serves, still what
`parallel_tree_remotes` and `MW_INSTALL_PATH` name. The literal path is
an alias for it, not a move of it.

**Why the clone is not simply created at the literal path.** Because of
what a restart does. `mount --move` and the alias both live in the
container's mount namespace, which `sbx stop` throws away (§31.1); the
sbx mount then reappears at the literal path. With the clone *at* the
literal path, the mirror comes back on top of it and the work is hidden —
"a worse failure than not doing it at all". With the clone in the
parallel tree and the literal path an alias, a restart costs the alias
and nothing else: `/home/agent/<rel>` is still there, still writable,
still what the host's remotes point at.

**The one thing a restart does break** is the alternates line, which
names `/home/agent/.sbx-originals/<rel>` — a path that only exists while
the move is in force. So the restart handling is not optional garnish;
it is what keeps the clones' borrowed objects reachable. `setup.startup`
is where it goes (§33.2), with `user: "0"` since startup commands
otherwise run as the agent. What a real stop/resume leaves behind was
measured before that was written — §40.

**Best-effort throughout.** `move_original` and `bind_over` warn and
return False instead of raising, and `setup_repo` falls back to the
pre-§39 layout on either. A cosmetic improvement to the working directory
must not be able to kill a create. A path that isn't a mount point at all
(a hand invocation) is skipped silently — mounting a clone over a real
directory would hide files rather than a mirror, and a `warning:` per
repo would fill the §38 report for no reason.

> **"Cosmetic" is right about the fallback and wrong about the feature**
> (§82.5, 2026-09-14). Falling back to the pre-§39 layout beats failing a
> create, which is all this paragraph claims. But the working directory
> itself is load-bearing: the agent cannot `/cd`, every restart resets
> cwd to the primary workspace, and Claude Code keys its project state
> and memory directory off the cwd path. That is why §82.3's scratch
> primary was deferred.

`:ro` repos are untouched: the literal path keeps showing the pristine
original, which is exactly what `:ro` asked for. Repos outside `hostHome`
keep today's remount-in-place, since they have no parallel clone to alias
to.

`HOME_CLAUDE_MD` was rewritten to match: it no longer tells Claude to run
`/cd` (§30 — a slash command a model cannot invoke), it describes the
layout, and it says what to do if a write fails, which is now also the
symptom of a resumed sandbox that has lost its aliases.

**Known cosmetic effects, neither judged worth fixing:** the daemon's
`--base-path=/home/agent --export-all` now also exports
`.sbx-originals/<rel>`, i.e. serves the host its own repo back; and the
originals sit under `$HOME`, where a `find` that doesn't skip dotdirs
will see every file twice. `.sbx-originals` is hidden for that second
reason.

## 40. What a restart leaves behind, and the `--restore` pass — MEASURED **[cananian, host, 2026-09-08]**

Sandbox `mw-alias` (`wmf-sbx-create --name mw-alias Cite`, so Cite rw plus
core and Vector `:ro`), probed, `wmf-sbx stop`ped, probed again.

**Right after create — §39 works.** All three mirrors are at
`/home/agent/.sbx-originals/Projects/Wikimedia/...`, `virtiofs ro`; the
literal path `/home/cananian/Projects/Wikimedia/Extensions/Cite` is the
`overlay` clone; `stat -c %d:%i` is `28:131166` for both the literal path
and `/home/agent/Projects/Wikimedia/Extensions/Cite`, i.e. the alias and
the parallel-tree clone are the same directory; the alternates line names
`.sbx-originals/.../Cite/.git/objects`; a write to the literal path
succeeds; **6707 commits reachable**.

**After `wmf-sbx stop` and a restart — everything mounted is gone.**

```
host /home/cananian/…/Extensions/Cite virtiofs rw,nosuid,nodev,relatime 0 0
drwxr-xr-x  2 root root … /home/agent/.sbx-originals/…/Extensions/Cite
28:131166                 /home/agent/…/Extensions/Cite
commits reachable in the clone: 5
```

Four things to read out of that, in ascending order of importance:

1. `.sbx-originals/<rel>` is back to the empty `root root` directory
   `os.makedirs` left; the mirror is at its own path again.
2. The clone survives untouched at the same inode — it lives in the
   container's writable layer, not in any mount. §39's "a restart costs
   the alias and nothing else" holds.
3. **6707 → 5 commits.** The alternates line points into
   `.sbx-originals`, which is now empty, so every borrowed object is
   unreachable and the clone is down to its own five commits. Predicted in
   §39; this is the measurement.
4. **The mirror comes back `rw`** — not `ro`. This is not a §39
   regression, it is a pre-existing hole §39 happened to expose: the
   read-only lock-down has always been a `mount -o remount,ro,bind` in the
   container's namespace (§15.2), so *every* sandbox resumed since the
   beginning has had its primary workspace's host directory writable from
   inside until something re-applied it. (The probe proved it the hard
   way: its own post-restart `touch` wrote `ALIAS-WRITE-TEST` into
   cananian's real Cite checkout.) sbx's own create-time `:ro` mounts are
   enforced below the namespace and are unaffected — core and Vector came
   back `ro`.

**The manual repair works**, which is what makes a startup command the
right answer:

```
$ wmf-sbx exec mw-alias -- sudo sh -c "mkdir -p …/.sbx-originals/$REL &&
      mount --move $LIT …/.sbx-originals/$REL &&
      mount --bind /home/agent/$REL $LIT && echo REPAIRED"
REPAIRED
…
commits reachable in the clone: 6707
```

`sudo` is available to `exec` (uid 1000, in group `sudo`), and the
`mount --move` of a mount sbx re-made is allowed. Note the repair left
the mirror `rw` — hence `restore_repo` does move → **remount ro** → bind,
in that order, so a failed remount can't be masked by a successful alias.

**The implementation.** `setup_repo` now returns a layout entry —
`{literal, dest, orig, mode}` with `mode` in `alias | clone | bind |
inplace` — and `run_setup` writes them all to
`/home/agent/.sbx-originals/layout.json` (versioned; a future version is
refused rather than misread). The kit registers
`restore_startup_command()`: `python3 /home/agent/wmf-sbx-setup
--restore`, `user: "0"`, no `background`, **first** in `setup.startup` so
the daemon that follows serves clones whose objects resolve. Per mode:

| mode | what `--restore` redoes |
| --- | --- |
| `alias` (§39, the normal case) | move → remount `orig` ro → bind over the literal path |
| `clone` (move failed at setup) | remount the literal mirror ro — it must **not** move, the clone's alternates point at it |
| `bind` (`:ro` in-home) | remount literal ro, re-bind into the parallel tree, remount `dest` ro |
| `inplace` (outside `hostHome`) | remount the literal path ro |

An entry whose `dest` doesn't exist yet is skipped: startup commands run
on the *first* container start too, before install has cloned anything.
Re-running with the alias already in place is a no-op (`samefile`). A repo
that fails doesn't stop the others; the status is 1 and the narration
lands in `/var/log/wmf-sbx-restore.log`, with a
`wmf-sbx-restore.status` beside it in the §38 format — a separate pair
from the setup log, so a restart can't overwrite the create-time report.

**Unverified on a real restart** — this is the design answering a
measurement, not itself measured. What to check on the next sandbox: that
`--restore` runs at all as `user: "0"`, that `commits reachable` is back
to 6707 without a manual repair, and that the mirror is `ro` again.

## 41. The restart pass runs, reports success, and does nothing — MEASURED **[cananian, host, 2026-09-08]**

A fresh `wmf-sbx-create --name mw-alias Cite` on the §40 code. The kit
registered the new startup command — `+ python3 /home/agent/wmf-sbx-setup
--restore (kit=mediawiki-kit, user=0)`, so `user: "0"` is accepted — and
install ran clean (62.2s, and `wmf-sbx-create` printed no §38 report,
i.e. no problems).

Then the container was stopped and restarted (the probe's own
`wmf-sbx exec` printed "Sandbox mw-alias started successfully"), and:

```
host /home/cananian/…/Extensions/Cite virtiofs rw,nosuid,nodev,relatime
39:20010778 /home/cananian/…/Extensions/Cite     <- the mirror
28:786526   /home/agent/…/Extensions/Cite        <- the clone
/home/agent/.sbx-originals/…/Cite/.git/objects   <- alternates, dangling
error: unable to normalize alternate object path: …
fatal: bad object refs/heads/review/c_scott_ananian/1298393
--- restore log ---
{ "version": 1, "exit": 0, "problems": [], "log": "/var/log/wmf-sbx-restore.log" }
```

**The restore pass ran and reported success while restoring nothing.**
The status file proves it ran (only `--restore` writes that file). §40's
prediction about the *damage* was exactly right; its answer didn't take.

Two things were wrong, and only the second is a mystery:

1. **The pass was unfalsifiable.** A fully successful restore printed
   nothing at all — `remount_readonly`/`move_original`/`bind_over` are
   silent on success — so "worked" and "did nothing" produced byte-identical
   reports. Fixed: `restore_repo` narrates one line per repo, and
   `verify_repo` now checks the post-conditions after each one
   (`samefile(literal, dest)`, `ismount(orig)`, and every path named in
   `objects/info/alternates` existing) and reports each failure as an
   `error:` line, which is what the §38 machinery turns into a problem
   list. The next run of this cannot come back "exit 0, problems: []"
   with the mounts missing.
2. **Why the startup command's mounts weren't there.** Unresolved.
   Candidates, in order of suspicion: sbx runs startup commands in a
   private mount namespace (a `mount --move` there would succeed and
   vanish on exit — which fits *every* observation, including the silence);
   or they run before the workspace mounts exist, in which case
   `os.path.ismount(literal)` is False and `move_original` bails with the
   one plain line it deliberately doesn't count as a problem. The next
   host round-trip distinguishes them: the narration now says which.

**What doesn't depend on the answer.** A mount made from `sbx exec`
*is* measured to stick (§40's manual repair), so `wmf-sbx-resume` now
runs the same pass itself — `sbx exec NAME -- sudo python3
/home/agent/wmf-sbx-setup --restore` — right after starting the container
and before re-pointing the remotes, and reports what it says through the
§38 reader (`read_setup_status` and `report_setup_problems` grew a
`status_path`/`label`, so the restore report is read exactly like the
setup one). `--no-restore` opts out. It is idempotent, so it costs one
`samefile` per repo when the startup command did work.

That makes the startup command belt-and-braces rather than the only
thing standing between a resumed sandbox and a five-commit repo — and it
covers `wmf-sbx exec`/`wmf-sbx run` only in as much as the engineer went
through `wmf-sbx-resume`, which is the documented path.

## 42. The full stop/resume cycle, clean — MEASURED **[cananian, host, 2026-09-08]**

`wmf-sbx-create --name mw-alias Cite`, probe, `wmf-sbx stop`,
`wmf-sbx-resume mw-alias -- --version`, probe. Both probes, identically:

```
overlay /home/cananian/…/Extensions/Cite overlay rw,…      <- the clone, at the host path
28:1048670 /home/cananian/…/Extensions/Cite
28:1048670 /home/agent/…/Extensions/Cite                    <- same directory
/home/agent/.sbx-originals/…/Cite/.git/objects              <- alternates, resolvable
literal write: LITERAL-WRITABLE
mirror write:  touch: … '/home/agent/.sbx-originals/…/Cite/RO-TEST': Read-only file system
commits reachable in the clone: 7765
```

So §39 and §40 both hold across a restart: the agent's starting directory
is the writable clone, the clone is the same object as its parallel-tree
twin, its borrowed objects resolve, and the host mirror is read-only
again — the §40 hole closed. `wmf-sbx-resume --dry-run` lists the new
step, and the real resume ran it between the start and the re-point.

**Who did the restoring: the startup command.** The resume-side pass
found nothing left to do —

```
restoring 3 repo(s) from /home/agent/.sbx-originals/layout.json
/home/cananian/…/Extensions/Cite: restoring (alias)
/home/cananian/…/Extensions/Cite: already the clone; nothing to do
… (core, Vector the same)
```

— and since it runs *after* the container start, "already" means the
`setup.startup` entry had done the work. `user: "0"` startup commands can
therefore mount, and their mounts are visible to later `sbx exec`s. That
retires §41's private-mount-namespace hypothesis.

**§41's failure is unexplained and unreproduced.** The old sandbox's
`/var/log/wmf-sbx-restore.log` was genuinely empty (re-read with the `--`
that was missing the first time) and its `layout.json` was correct — all
three repos `mode: alias`. Under the §40 code, an empty log means the pass
took one of the two paths that printed nothing at all: the
`samefile` "already restored" no-op, or the missing-`dest` first-start
skip. Both are now narrated, so the same log would name the path taken.
The most likely story is that the restore ran on the create-time container
start (before install had cloned anything → the `dest` skip) and did not
run again on the start that the probe triggered; that would make it a
question about *when* sbx runs startup commands, not about what they can
do. Not chased further: the sandbox is gone, and both belts now hold.

What remains true regardless: nothing here is verified for a resume that
goes through plain `sbx run`/`wmf-sbx exec` instead of `wmf-sbx-resume`.
The startup command covers that case, and §41 is the reminder that it is
the belt we have less evidence for.

## 43. `-- --version` is not an attach — REPORTED **[cananian, host, 2026-09-08]**

Fallout from §42's own test procedure:

```
$ sbx/bin/wmf-sbx-resume mw-alias
+ … wmf-sbx run --name mw-alias -- --continue
No conversation found to continue
ERROR: agent exited with code 1
```

The `attached` flag exists to stop exactly this (§20/§35): `--continue`
is only defaulted on once an agent has actually run in the sandbox. But
`mark_attached` fired on *any* clean exit of `wmf-sbx run`, and §42's
verification step was `wmf-sbx-resume mw-alias -- --version` — which
prints `2.1.246 (Claude Code)`, exits 0, and starts no conversation. The
flag went up, and the next plain resume asked claude to continue nothing.

Two changes, because the flag can also go stale for reasons we don't
control (a pruned conversation, a `~/.claude` wiped inside the sandbox):

* **Only a tail that could have started a conversation sets the flag.**
  `starts_a_conversation` says no to the informational flags and to every
  subcommand in `claude --help` (`mcp`, `doctor`, `install`, …), and yes
  to everything else, including a bare prompt. A subcommand only counts
  in first position, so `--agent rm` stays a flag's value.
* **A `--continue` we added is ours to retract.** If the run exits
  non-zero and the `--continue` came from us rather than from the
  caller's own `--` tail, resume says so, clears `attached`, and
  relaunches once without it. A `--continue` the engineer typed is left
  alone — retrying that would be second-guessing an explicit request.

Deliberately not done: verifying the conversation exists by looking for
`~/.claude/projects/<slug>/*.jsonl` inside the sandbox before adding the
flag. It would be authoritative, but it buys an `sbx exec` on every
resume and a dependency on Claude Code's on-disk layout, to replace a
recovery that costs one relaunch on the rare occasion it's wrong.

## 44. The attach flag heals itself; the exec path is still unmeasured — MEASURED **[cananian, host, 2026-09-08]**

§43's fix, on the sandbox that broke it, doing both halves in one command:

```
$ sbx/bin/wmf-sbx-resume mw-alias
+ … wmf-sbx exec mw-alias -- true
+ (re-applying mw-alias's mount layout)
+ … wmf-sbx run --name mw-alias -- --continue
No conversation found to continue
ERROR: agent exited with code 1

warning: that looks like a --continue with no conversation to continue;
  forgetting this sandbox's attach flag and starting a fresh session.
+ … wmf-sbx run --name mw-alias
Attaching to existing sandbox "mw-alias" …
```

and `attached` is `true` again in the state file afterwards — set the
second time by a tail that really did start a conversation. The engineer
types one command and gets the session they asked for; the stale flag
costs one failed launch, once.

Two old loose ends closed in the same transcript: Cite's unmarked
`sandbox-mw-cite` remote is gone (§36.4 — removed by hand; the only
`sandbox-*` remote left is the marked `sandbox-mw-alias`), and
Translate's host checkout really is on `master` (§34.3's open question,
`git lol` shows `HEAD -> master`).

### 44.1 The measurement that doesn't measure what it looks like

The interesting question was §42's caveat: does a start that
`wmf-sbx-resume` never sees — a plain `sbx run`, an `sbx exec` — get its
mounts back from the startup command alone?

```
$ wmf-sbx stop mw-alias
$ wmf-sbx exec mw-alias git -C ~/Projects/Wikimedia/Extensions/Cite log --oneline | wc -l
Sandbox mw-alias started successfully
6707
```

6707 is the restored number and 5 is the broken one (§40), so this reads
like a pass. It isn't one. The host shell expanded `~` before `sbx` saw
it, so the path probed was `/home/cananian/…/Extensions/Cite` — the
*literal* path, and the literal path has the same commit count either
way. Restored, it is the alias to the clone (6707). Unrestored, it is
sbx's own bind mount of the real host repo, which is where those 6707
commits came from in the first place. The two hypotheses predict the same
number, so the number distinguishes nothing. (My command, my mistake:
§40's 6707/5 was measured at `/home/agent/…`, and I wrote the probe with
a `~` that doesn't point there.)

What would distinguish them: the commit count at `/home/agent/…`, the
inode of the literal path against the clone's, and the mount options on
the moved-aside mirror.

### 44.2 `wmf-sbx-setup --verify`

So stop hand-writing the probe. `--verify` reads `layout.json` and runs
the same `verify_repo` post-conditions `--restore` runs, changing
nothing — no mounts, no root, no `/var/log` — and says so per repo:

```
$ wmf-sbx exec mw-alias -- python3 /home/agent/wmf-sbx-setup --verify
verifying 3 repo(s) from /home/agent/.sbx-originals/layout.json
/home/cananian/…/Extensions/Cite: ok (alias)
…
```

with `error:` lines and exit 1 otherwise. Three checks make it worth
having over a shell one-liner:

* **The alias**: `samefile(dest, literal)` — the thing 44.1's probe
  couldn't see past.
* **The alternates**: every path in the clone's `objects/info/alternates`
  still exists. This is the 6707→5 failure, and git reports it as
  nothing at all until you walk history.
* **The mount options** — new, and the one a human probe keeps forgetting.
  `os.path.ismount(orig)` proves the mirror was moved aside; it says
  nothing about whether the move came back `ro`, and §40's fourth finding
  is precisely a mount that is in the right place and writable. So
  `readonly_problem()` parses `/proc/self/mountinfo` (topmost mount at
  the path wins — §39 stacks mounts deliberately) and reports a mirror
  that came back `rw` as an error in its own right. An entry not in
  mountinfo at all is *not* reported as writable: that's the callers'
  own check, and claiming a failure we haven't measured would be §41 in
  reverse. This also gives `inplace` repos their first post-condition —
  the read-only remount is the entire mode, and nothing checked it.

The bypass attempt stays the human's job, because a `touch` that
*succeeds* writes into the engineer's real checkout (§40's probe did
exactly that). Belt and braces, on a resumed sandbox:

```
$ wmf-sbx exec NAME -- python3 /home/agent/wmf-sbx-setup --verify
$ wmf-sbx exec NAME -- sudo touch /home/agent/.sbx-originals/<rel>/RO-TEST
touch: … Read-only file system            <- what it should say
```

## 45. The startup command does not restore an `exec`-started sandbox — MEASURED **[cananian, host, 2026-09-08]**, and **wrong: see §46**

> **Correction.** §45.1's conclusion — "the `setup.startup` entry did not
> restore this container" — is false. It restores every container start,
> including this one; it just hadn't finished when the exec'd command
> ran. §46 has the measurement and the two logs that settle it. The rest
> of this section (the `:ro`/mode reading, the snapshot lesson, §45.2's
> read-only confirmation, §45.3) stands; §45.2's "the only measured-safe
> way back in" is narrowed by §46 from "the others don't restore" to
> "the others don't wait".

`--verify` earned its keep on the first run. A fresh `wmf-sbx-create
--name mw-alias Cite`, then, with the sandbox still up from create:

```
$ wmf-sbx exec mw-alias -- python3 /home/agent/wmf-sbx-setup --verify
verifying 3 repo(s) from /home/agent/.sbx-originals/layout.json
/home/cananian/…/Extensions/Cite: ok (alias)
/home/cananian/…/core: ok (alias)
/home/cananian/…/Skins/Vector: ok (alias)
```

All three `alias`, including the two the wrapper passed to `sbx create`
as `:ro`, which is correct and worth stating plainly now that something
prints the mode: the create-time `:ro` is the *enforcement* layer (below
the namespace, unliftable from inside), while the typed `:ro` is the
parallel-tree strategy switch (clone vs bind). core and Vector were
discovered by the dependency walk, not typed, so they get writable
clones behind read-only mirrors — `build_sbx_command`'s comment, now
confirmed from the other end.

**First, a lesson about the sandbox's copy of the script.** The command
was first tried on the *old* mw-alias:

```
error: could not read the plan file --verify: No such file or directory: '--verify'
```

`/home/agent/wmf-sbx-setup` is copied in at create time and never
updated, so it's a snapshot of the repo as it was that day — an old
sandbox parses `--verify` as a plan-file path. Any fix to the setup
script needs a new sandbox to test against; there is no in-place upgrade
path today, and probably shouldn't be one (the script is what built the
layout it would be restoring).

### 45.1 The answer to §42's open question is no

`wmf-sbx-resume`, a session, `wmf-sbx stop`, then the same `--verify` —
which starts the container itself, since a stopped sandbox has to come
up to run an exec:

```
$ wmf-sbx exec mw-alias -- python3 /home/agent/wmf-sbx-setup --verify
Sandbox mw-alias started successfully
verifying 3 repo(s) from /home/agent/.sbx-originals/layout.json
error: /home/cananian/…/Cite is not the clone at /home/agent/…/Cite
error: /home/agent/.sbx-originals/…/Cite is not a mount point (the host mirror was not moved aside)
error: /home/agent/…/Cite borrows objects from /home/agent/.sbx-originals/…/Cite/.git/objects, which does not exist
… the same three for core and Vector
```

Nine post-conditions, nine failures: **the `setup.startup` entry did not
restore this container.** §42 inferred that it had — the resume-side pass
found the alias already up and nothing else could have done it — and that
inference is now falsified for the `exec` path at least. The kit
registers the command (`sbx create` lists it: `+ python3
/home/agent/wmf-sbx-setup --restore (kit=mediawiki-kit, user=0)`), so
this is about whether it *runs*, or *finishes*, not whether it exists.

A third observation from the same transcript constrains it. Two execs
later, with no `wmf-sbx stop` in between, sbx printed `Sandbox mw-alias
started successfully` **again** — so the container an `exec` starts does
not stay up. That makes each exec on a stopped sandbox its own container
start, its own fresh mount namespace, and its own race with whatever
startup commands do. Two hypotheses fit everything measured so far:

* **A race.** Startup commands run asynchronously with the exec'd
  command. §42's restore pass was the *second* exec of its sequence
  (`exec true` first), so the startup command had a head start; §45.1's
  verify was the first, and lost. Predicts: a later `--verify` against
  the same container start succeeds, and `/var/log/wmf-sbx-restore.log`
  contains a full narration block.
* **They don't run on an exec-start at all** — only on the start that
  `sbx run` does. Predicts: `--verify` keeps failing however long you
  wait, and the restore log holds nothing for that start.

`/var/log/wmf-sbx-restore.log` and `/var/log/sbx-kit-startup.log` tell
them apart, and this is what §38/§41's logging was built for. Not
guessed at further until they're read.

### 45.2 The resume path is sound, and is now the only one that is

Everything `wmf-sbx-resume` covers still holds. After a resume, from the
same session:

```
$ wmf-sbx exec mw-alias -- sudo touch /home/agent/.sbx-originals/…/Cite/RO-TEST
touch: … 'RO-TEST': Read-only file system
```

The same `touch` before the resume **succeeded** — but harmlessly, and
the reason matters: with the mounts gone, `.sbx-originals/<rel>` is the
empty root-owned directory `os.makedirs` left behind (§40, finding 1),
not the mirror. `ls` on the host confirmed nothing landed in the real
Cite. The write that *does* reach the engineer's checkout in that state
is one to the **literal** path, which §40 already measured the hard way;
no reason to poke it again.

So: `wmf-sbx-resume` is not a convenience wrapper. It is the only
measured-safe way back into a stopped sandbox, because it does not trust
the startup command — and we now know it is right not to. `sbx run
--name` and a bare `sbx exec` can both put an agent in a sandbox whose
clones have lost their objects and whose host mirrors are writable. That
belongs in `SECURITY.md` alongside the §40 finding.

> **Narrowed by §46.3.** The startup command *does* restore, on every
> path — so "can put an agent in a sandbox whose mirrors are writable"
> is true only for the first seconds of the container's life, not
> indefinitely. `wmf-sbx-resume`'s advantage is that it waits for the
> layout and says so; the others don't wait and don't tell you.

### 45.3 The `--continue` retry fires for a second, legitimate reason

The resume after the stop injected `--continue` and got "No conversation
found to continue" again, and the §43 recovery absorbed it again. The
flag wasn't stale this time: the previous session really did run and exit
0. It just left no conversation — attaching and exiting without typing
anything is a session that started nothing.

`starts_a_conversation` cannot see that; only the sandbox's
`~/.claude/projects/` can, which §43 deliberately declined to read. The
recovery is doing exactly the job it was written for, at exactly the
advertised price: one failed launch, one warning, one relaunch. Two
sightings, two clean recoveries — leaving it alone.

## 46. It was a race, and the logs said so — MEASURED **[cananian, host, 2026-09-08]**

Same sandbox, same command, ten seconds apart:

```
$ wmf-sbx stop mw-alias
$ wmf-sbx exec mw-alias -- python3 /home/agent/wmf-sbx-setup --verify
Sandbox mw-alias started successfully
error: … is not the clone at …          (nine of these)
$ sleep 10
$ wmf-sbx exec mw-alias -- python3 /home/agent/wmf-sbx-setup --verify
/home/cananian/…/Extensions/Cite: ok (alias)
/home/cananian/…/core: ok (alias)
/home/cananian/…/Skins/Vector: ok (alias)
```

So the startup command does run, and does restore. It simply does not
block the `sbx exec` that started the container, and the verify that
raced it lost. §45.1's conclusion is retracted.

**Later caveat [2026-09-25].** This held on sbx 0.42.1. On 2026-09-19,
in `sbx-translate` on sbx v0.43.0, a container start ran no startup
command at all — see §97. So "the startup command does run" is a
measurement of one sbx version, not a rule.

`/var/log/sbx-kit-startup.log` is the record, and it is a good one —
sbx's dispatcher timestamps every run and names every command:

```
=== dispatcher run 2026-09-08T17:52:58Z ===
> /etc/durable-startup.d/001-startup-claude/000-cmd.sh
ok …
> /etc/durable-startup.d/002-startup-mediawiki-kit/000-cmd.sh
restoring 3 repo(s) from /home/agent/.sbx-originals/layout.json
/home/cananian/…/Extensions/Cite: restoring (alias)
… core, Vector
ok /etc/durable-startup.d/002-startup-mediawiki-kit/000-cmd.sh
=== dispatcher complete ===
```

Five dispatcher runs across the session, one per container start — which
means one per `sbx exec` on a stopped sandbox, since each of those is a
start. Three things to read out of it:

1. **Our entry is fourth.** The claude kit's three commands run first,
   and one of them shells out to the MCP gateway. That's the length of
   the window, and it's why `--wait` defaults to 20s rather than 2.
2. **`restoring (alias)` with no `already the clone` after it** means the
   pass did real work on that start — the narration §41 asked for,
   finally being read for what it was written for.
3. **Two of the five runs say `already the clone; nothing to do`.** A
   fresh namespace can't already have the alias, so those runs are ones
   where the container was *already up* — the dispatcher re-runs the
   startup commands on an exec against a running sandbox too, not only
   on a cold start. That also explains §42's evidence: its restore pass
   was the second exec of a sequence, so the dispatcher from the first
   had already won.

### 46.1 Wait, don't race

`wmf-sbx-resume` used to start the container and immediately run its own
`--restore` over `sbx exec`. That is two processes calling
`mount --move` at the same three paths and appending to the same log,
for no benefit now that the dispatcher is known to do the work. It now
runs `--verify --wait` first and only falls back to `--restore` if the
wait runs out:

```
+ … wmf-sbx exec mw-alias -- true
+ (waiting for mw-alias's mount layout)
+ … wmf-sbx exec mw-alias -- python3 /home/agent/wmf-sbx-setup --verify --wait=20
+ … wmf-sbx run --name mw-alias
```

The polling loop lives *inside* the sandbox (`--verify --wait SECONDS`,
0.25s between checks) so the whole wait is one `sbx exec` rather than one
per poll. The `--restore` fallback stays: "measured to work" is not
"cannot fail", and it is the belt that made §41 survivable.

### 46.2 The restore log now appends

It was opened `"w"`, so it only ever held the most recent run — and it
runs on every container start, i.e. on every `sbx exec`. That makes an
empty restore log ambiguous between "never ran" and "ran and was
overwritten", which is exactly the ambiguity §41 was read through. It
now appends, with a `=== --restore <timestamp> ===` header per run and a
1 MiB cap (about ten lines per run, so thousands of starts). The setup
log stays truncating: there is only ever one run of it.

### 46.3 What this does to the security claim

§45.2 said `wmf-sbx-resume` is the only measured-safe way back into a
stopped sandbox. Narrowed: every path restores, so the difference is
*when*, not *whether*. A bare `sbx run --name` gets its mounts a second
or two into the container's life, most likely before the agent inside has
finished starting — but during that window the host mirrors are writable
and the clones' borrowed objects are unreachable, and nothing on that
path waits for it or would tell you. `wmf-sbx-resume` waits; that is the
whole of its advantage, and it is a smaller advantage than §45.2
claimed. The residual window belongs in `SECURITY.md` with the rest.

## 47. Sixth sandbox: the 0.42.1 recreate — MEASURED **[verified in-sandbox + cananian, host, 2026-09-08]**

*(`RESUME.md`, cited throughout this section, was folded back into this
file and `IMPLEMENTATION-PLAN.md` §16.5 and removed 2026-09-11, once this
section confirmed the re-measurement was done — see §0's "Recreating this
dev sandbox" for what its §4 held.)*

`RESUME.md` was written for exactly this moment: sbx 0.39.0 → 0.42.1,
sandbox destroyed and recreated with the `--no-deps`, `~/Projects/Wikimedia/…`
`wmf-sbx-create` line from `RESUME.md` §4. Working through `RESUME.md` §5 /
`IMPLEMENTATION-PLAN.md` §16.5's re-measure list in order:

**Version confirmed** — `wmf-sbx version --json` on the host:
`client.version` and `server.version` both `v0.42.1`, revision `cc6e400a…`,
`api_version: 0.28.0`.

### 47.1 Layout came up clean

`python3 /home/agent/wmf-sbx-setup --verify` right after create, from
inside the sandbox:

```
verifying 8 repo(s) from /home/agent/.sbx-originals/layout.json
/home/cananian/Projects/Wikimedia/wmf-claude: ok (alias)
/home/cananian/Projects/Wikimedia/core: ok (bind)
/home/cananian/Projects/Wikimedia/Skins: ok (bind)
/home/cananian/Projects/Wikimedia/Extensions: ok (bind)
/home/cananian/Projects/Wikimedia/Parsoid: ok (bind)
/home/cananian/Projects/Wikimedia/mediawiki-config: ok (bind)
/home/cananian/Projects/Wikimedia/integration-config: ok (bind)
/home/cananian/Projects/Wikimedia/docs.docker.com: ok (bind)
```

8 repos, one alias (the writable primary) and seven binds (the `:ro`
extras) — matches `RESUME.md` §4's line exactly. Item 1 of the re-measure
list: **passes**.

### 47.2 The `:ro` bypass still holds, reconfirmed by writing not reading

Per the absolute rules, attempted the write rather than trusted
`/proc/mounts` — but against a **new**, uniquely-named file rather than
existing tracked content, so a surprise success would cost nothing to
clean up:

```
$ sudo mount -o remount,rw /home/cananian/Projects/Wikimedia/core
(no output — "succeeds" exactly as before)
$ sudo touch /home/cananian/Projects/Wikimedia/core/.wmf-sbx-write-probe-4867
touch: cannot touch '…': Read-only file system
```

Item 4: **still holds on 0.42.1**. The remount is still cosmetic; the
mount is still backed read-only below the namespace where `sudo` can't
reach it. §22's finding is unchanged by the upgrade.

### 47.3 `sbx`/`wmf-sbx` is not reachable inside the sandbox at all

Checked before asking cananian for anything: `which sbx wmf-sbx` and
`type wmf-sbx` both come up empty inside the sandbox, and there's no copy
under `/home/agent/bin`. So items 2 (the restart race), 3 (the published
port's protocol string), and 5 (`sbx policy ls --wide`) are not things a
fresh Claude instance can shortcut by looking harder in-sandbox — they
are genuinely host-only, same as every other sandbox-lifecycle question
in this project. Asked cananian for the exact command sequences from
`RESUME.md` §5 items 2, 3, and 5; results below, from
`responses14.txt` **[cananian, host, 2026-09-08]**.

### 47.4 The restart race is unchanged on 0.42.1 — the residual window stays open

Item 2, the single highest-value question in `RESUME.md` §5: does 0.42's
startup dispatcher now block the `sbx exec` that triggers a container
start? **No.**

```
$ wmf-sbx stop wmf-claude-sbx
Sandbox 'wmf-claude-sbx' stopped; state preserved.
$ wmf-sbx exec wmf-claude-sbx -- python3 /home/agent/wmf-sbx-setup --verify
Sandbox wmf-claude-sbx started successfully
error: /home/cananian/…/wmf-claude is not the clone at /home/agent/…
error: /home/agent/.sbx-originals/…/wmf-claude is not a mount point (the host mirror was not moved aside)
error: /home/agent/…/wmf-claude borrows objects from …, which does not exist
error: /home/agent/…/core is not a mount point           (six more of these)
$ sleep 10
$ wmf-sbx exec wmf-claude-sbx -- python3 /home/agent/wmf-sbx-setup --verify
verifying 8 repo(s) from /home/agent/.sbx-originals/layout.json
/home/cananian/…/wmf-claude: ok (alias)
… ok (bind) × 7
```

Same shape as §46 on 0.39.0, down to the same three error strings on the
primary. `sbx-kit-startup.log` confirms the dispatcher ran the restore
inside the same window and finished before the ten-second mark — so the
window is still "a few seconds," not indefinite — but it still does not
block the exec that woke the container. **§46.3's residual stands
unchanged on 0.42.1**: `wmf-sbx-resume`'s wait is still the entire
security advantage it has over a bare `sbx exec`/`sbx run --name`, and
`SECURITY.md` §4/§9 needs no correction, just this confirmation.

Bonus confirmation of a different fix: the log now shows **three**
dispatcher runs, timestamped 43 and 51 minutes apart, all still present
in one file — §46.2's switch from truncating to appending is doing its
job across a real multi-restart session, not just in the unit tests.

### 47.5 The published port: `tcp4`, one row, exactly as predicted

Item 3:

```
$ wmf-sbx ports wmf-claude-sbx --json
[{"host_ip": "127.0.0.1", "host_port": 32770, "sandbox_port": 9977, "protocol": "tcp4"}]
```

One row, `protocol: "tcp4"` written out explicitly (0.39 published this
with an *empty* protocol string and a second `::1` row — §36.3). This is
exactly IMPLEMENTATION-PLAN.md §16.2 item 1's predicted breaking change,
and exactly the case `wmf_sbx_create.ipv4_mapping` was written ahead of
the upgrade to handle: it already treats `tcp4` as a match, not just an
absent/`"tcp"` protocol. No further code change needed. Item 3: **passes**.

### 47.6 The network policy: same six rules, same shape

Item 5:

```
$ wmf-sbx policy ls wmf-claude-sbx --type network --wide
```

Five `source: local` default rules (`default-ai-services`,
`default-package-managers`, `default-code-and-containers`,
`default-cloud-infrastructure`, `default-os-packages`) plus the kit's own
rule (`source: kit`, the wiki-family domains + the small fixed list) —
same six rules `SECURITY.md` §1 describes for 0.39, same shape. No
narrowing, no widening, nothing new to account for. Item 5: **passes**,
i.e. `SECURITY.md` §1's finding needs no update and `--deny-network`
remains the standing open recommendation there.

### 47.7 Two new problems, from cananian working the sandbox by hand

Not on any re-measure list — found by cananian actually using the thing:

1. **A `wmf-sbx-create` hang with no visible cause.** Before the create,
   `wmf-sbx ls` hit a client/server version-mismatch prompt from the sbx
   CLI itself (`Docker Sandboxes has been updated and needs to restart …
   Restart now? (y/N):`) — nothing to do with our code. Minutes later,
   `wmf-sbx-create` itself hung with **no prompt visible at all**, and had
   to be Ctrl-C'd; the resulting traceback (`responses13.txt`) pointed at
   `existing_sandbox_names()`'s `run([WMF_SBX, "ls", "-q"],
   capture_output=True, text=True)` in `wmf_sbx_create.py`. Working theory:
   that internal `sbx ls -q` call can hit the same restart prompt, but
   because it's run with `capture_output=True` and no `stdin=` override,
   the prompt text lands in the captured (and, at that point, unread)
   stdout instead of the terminal, while stdin still points at the
   terminal — so the subprocess sits waiting for a keypress the user has
   no way of knowing is expected. cananian's ask: every internal `sbx`
   invocation that isn't supposed to be interactive should pass
   `stdin=subprocess.DEVNULL`, so a stray prompt fails fast and loud
   instead of hanging invisibly. Investigating which call sites need it.
2. **`wmf-sbx-resume`'s `--continue` didn't stick.** cananian had to
   manually pass `--resume <session-id>` after a resume; the documented
   §35.2 behavior (default `--continue` from the second attach onward) did
   not visibly take effect. Investigating against 0.42.1's `sbx run`
   behavior and the attach-count state `wmf-sbx-resume` reads.

### 47.8 Both root-caused and fixed — MEASURED, cananian + Claude, 2026-09-08

Both of 47.7's problems, confirmed unrelated to 0.42.1 itself (the upgrade's
documented breaking changes, §16.2, mention neither): they were latent bugs
in `sbx/bin/`, just newly noticed because this is the first time anyone
worked the sandbox by hand this hard.

1. **The invisible hang, root-caused.** 47.7's working theory was right.
   Confirmed by reading every non-attach `WMF_SBX` call site in
   `wmf_sbx_create.py`: all 8 used `capture_output=True, text=True` with no
   `stdin=` override — `existing_sandbox_names()`, `publish_daemon_port()`,
   `lookup_published_host_port()`, `read_setup_status()`,
   `start_sandbox()`, `wait_for_sandbox_mounts()`,
   `restore_sandbox_mounts()`, `wait_for_daemon()`. Any one of them can hit
   an unexpected `sbx` prompt (a version-mismatch restart prompt is the one
   actually seen), and because stdin is left connected to the real
   terminal while stdout is captured into an unread buffer, the process
   hangs waiting on a keypress the user is never shown is expected.

   **Fix**: added `stdin=subprocess.DEVNULL` to all 8 call sites. A stray
   prompt now gets immediate EOF instead of hanging — fast, visible
   failure instead of a silent one, per cananian's exact ask. Deliberately
   **not** applied to the two calls that are genuinely interactive:
   `wmf_sbx_create.py`'s `run(cmd, env=env)` (the real `sbx create ...
   claude ...` attach) and `wmf_sbx_resume.py`'s `run(cmd).returncode`
   (the real `sbx run --name` attach) — nor to `wmf_sbx_rm.py`'s
   `remove_sandbox()`, whose confirmation prompt is supposed to reach the
   terminal.

2. **The `--continue` bug, root-caused.** Nothing in the create path ever
   marked a sandbox as attached. `wmf_sbx_state.new_state()` hardcoded
   `"attached": False`; `wmf_sbx_resume.py` has the read side right
   (`default_agent_args()` strips `--continue` when `not attached`,
   `set_attached()` flips the flag after its own successful attach) — but
   the very first real conversation, the interactive `sbx create ...
   claude ...` session inside `wmf-sbx-create` itself, never told the
   state file it happened. So the first `wmf-sbx-resume` after any
   `wmf-sbx-create` always read `attached=False` and stripped
   `--continue`, exactly matching cananian's symptom (had to pass
   `--resume <session-id>` by hand).

   **Fix**: `wmf_sbx_state.new_state()` gained an `attached=False` keyword
   parameter (`sbx/bin/wmf_sbx_state.py`); `wmf_sbx_create.add_host_remotes()`
   (`sbx/bin/wmf_sbx_create.py`) now passes `attached=True` — safe because
   `add_host_remotes()` is only ever reached from `main()` after the real
   attach (`result = run(cmd, env=env)`) has already returned 0.

Both fixes are unit-tested (`test_wmf_sbx_state.py`: `new_state()` defaults
to `attached=False`, honors `attached=True`; `test_wmf_sbx_create.py`: new
`AddHostRemotesTests` asserts the state `add_host_remotes()` saves and
reloads comes back `attached=True`; `ExistingSandboxNamesTests` and
`LookupPublishedHostPortTests` updated to expect `stdin=subprocess.DEVNULL`
on their calls). Full suite: 559 unit tests + 126 template tests, all
green. **Not yet re-verified end-to-end on the host** — that needs
cananian to actually `wmf-sbx-create` → detach → `wmf-sbx-resume` and
confirm both that no hang recurs and that `--continue` now resumes the
right session. The `--continue` half is checkable any time; the stdin-hang
half specifically needs the failure mode to recur (an `sbx`-native prompt
landing on one of the fixed call sites), which realistically means the
*next* sbx upgrade after 0.42.1 — see the "Still to do" checklist entry
below for the exact repro procedure cananian gave (2026-09-08): leave
`sandboxd` running, `dpkg -i` the new version, run a plain
`wmf-sbx-create`. Failing loudly (an error, a visible prompt) is fine and
expected; hanging with nothing on screen is the bug.

## 48. `git-review-check`: sandbox and `local` remotes get a local check, not Gerrit's — DONE 2026-09-08 (cananian's design, Claude's implementation)

Fixes the "Still to do" item below about `git safe-reset` failing its
`git-review-check` gate against `sandbox-<name>` or `local`. Source is
`sbx/bin/git-review-check`, called by `sbx/bin/git-safe-reset`.

**The fix, per cananian's proposed design**: `git-safe-reset` now passes
its `UPSTREAM` argument through (`git-review-check "$UPSTREAM"`, was a
bare `git-review-check`). `git-review-check` special-cases two remote
shapes before ever touching `.gitreview`/Gerrit:

- `local` — sbx's own remote name (inside a sandbox) for the host mirror.
- `sandbox-*` — the host-side remote convention into a sandbox's clone
  (`wmf_sbx_remotes.py`).

For either, the question "would resetting past HEAD lose work" gets an
entirely local answer instead of a Gerrit query: `git remote update
$REMOTE` (cheap, idempotent even right after git-safe-reset's own `git
remote update`), then `git for-each-ref --contains=HEAD --format='%(refname)'
refs/remotes/$REMOTE/*` — non-empty means a copy of HEAD already exists on
the far side of that remote, so nothing is lost. Any other remote (plain
`origin`, or anything not shaped like the above) is unaffected: same
`.gitreview` + Gerrit-commit-search behavior as before.

One implementation wrinkle worth recording: the first attempt parsed
`git branch -r --contains HEAD` line-by-line, stripping a leading `"* "` /
`" "` — wrong, because `branch -r`'s indentation is two spaces, so a
single strip left a stray leading space and every comparison silently
failed (all four "already reachable" tests failed with "Not yet
reachable" until this was caught). `git for-each-ref` sidesteps the whole
parsing problem — no leading whitespace, no `-> ` alias noise, and its own
`--contains` does the ancestry test git-side rather than shelling out
twice.

Unit-tested against real repos in `sbx/bin/tests/test_git_review_check.py`
(not mocked, same posture as `test_git_safe_reset.py`): HEAD already/​not-yet
reachable via `local` and via `sandbox-*`, a remote one commit ahead of
HEAD still counts as reachable (ancestor, not just exact tip), a
plain/unshaped remote name falls through to the unchanged Gerrit path, and
an end-to-end `git-safe-reset sandbox-mw-cite` against a repo that *does*
have a `.gitreview` now succeeds without ever hitting Gerrit. Full suite:
567 unit tests (was 559) + 126 template tests, all green.

**Not yet verified against a real sandbox on the host** — the unit tests
use plain local git remotes standing in for `local`/`sandbox-<name>`;
cananian's original repro (`git safe-reset sandbox-wmf-claude-sbx` on the
host, `! git safe-reset local` inside a running session) hasn't been
re-run against this fix.

### 48.1. Refinement: verify, don't guess from the name — DONE 2026-09-08

cananian's follow-up, same day: name-prefix matching (`local|sandbox-*`)
is a guess, not a check. A host repo can have an unrelated remote someone
happened to name `local` or `sandbox-something`, and it would silently
get the weaker local-only check instead of the real Gerrit one. Two
changes, replacing the `case "$REMOTE" in local|sandbox-*)` block:

- **`local` is now gated on `$SANDBOX_NAME`** being set — Docker
  Sandboxes' own env var, confirmed present in a live sandbox (§24-ish),
  not something a host repo's remote naming
  can spoof. No `$SANDBOX_NAME` (i.e. we can't confirm we're actually
  inside a sandbox) → a remote named `local` falls through to the
  ordinary Gerrit path, same as any other name.
- **Any other `REMOTE` is checked against wmf-sbx's own host-side state**
  instead of its name: `${XDG_STATE_HOME:-~/.local/state}/wmf-sbx/sandboxes/*.json`
  (one file per sandbox, written by `wmf_sbx_state.py`/`sync_remotes()` in
  `wmf_sbx_remotes.py` when `wmf-sbx-create` actually wires the remote
  up). `git-review-check` now shells a small inline `python3` snippet
  that scans those files' `"remotes"` lists for an entry whose `"remote"`
  equals `$REMOTE` *and* whose `"hostDir"` (already realpath'd when
  written, per `wmf_sbx_create.py`'s `resolve_repo_spec`) matches this
  repo's own realpath — so a same-named remote registered against a
  *different* repo doesn't count either.

No bash JSON parsing was written by hand — the python3 snippet is
self-contained (doesn't import `wmf_sbx_state.py`, since that module
isn't necessarily on `sys.path` from wherever `git-review-check` ends up
installed) but mirrors its `state_dir()` convention exactly.

Tests (`test_git_review_check.py`) extended to cover the new gate: `local`
without `$SANDBOX_NAME` now falls through to Gerrit; a `sandbox-`-shaped
remote name with no matching state file falls through to Gerrit; a state
file that registers the remote against a *different* repo's `hostDir`
doesn't count. All existing "already/not-yet reachable" cases now set up
a real state file (or `$SANDBOX_NAME`) instead of relying on the name
alone. 11 tests in this file (was 6), full suite 570 (was 567) + 126
template tests, all green.

### 48.2. `wmf-sbx-ls-remotes`: the state-file lookup is now a real tool — DONE 2026-09-11

cananian's follow-up: pull §48.1's inline `python3` snippet out of
`git-review-check` into a standalone utility, `wmf-sbx-ls-remotes`, both
so it's independently testable and so it's independently *useful* — run
it in any repo to see what sandbox remotes wmf-sbx thinks it registered
there.

`sbx/bin/wmf_sbx_ls_remotes.py` (thin CLI wrapper `wmf-sbx-ls-remotes`,
same pattern as `wmf-sbx-resolve`/`wmf-sbx-rm`, so it can now import
`wmf_sbx_state.py` directly instead of duplicating its `state_dir()`
convention by hand):

- `find_repo_root(start=None)` — walks upward from `start` (default: the
  current directory) for a `.git` entry (directory or, for a
  worktree/submodule, a file), returning the realpath'd directory that
  holds it, or `None`.
- `find_remotes(repo_root, env=None)` — every `{sandbox, hostDir, remote,
  url}` across all `wmf_sbx_state.list_names()` state files whose
  `hostDir` realpath's to `repo_root`, sorted by sandbox then remote.
- CLI: `--path DIR` (default: cwd) to start the walk from, `--json` to
  print the full entry (one JSON object per line) instead of just the
  bare remote name (the default, one per line) — matches the tool's
  design brief exactly.

`git-review-check`'s `is_local_check_target()` now locates
`wmf-sbx-ls-remotes` next to itself (`$(dirname "${BASH_SOURCE[0]}")`,
the same directory both scripts ship from) or, failing that, on `PATH`,
and asks it directly: `"$LS_REMOTES" --path "$REPO_ROOT" | grep -Fqx --
"$REMOTE"`. No JSON parsing left in the bash script at all. If the tool
can't be found anywhere, the check just falls through to the ordinary
Gerrit path, same as any other unrecognized remote.

New `test_wmf_sbx_ls_remotes.py` (14 tests: `find_repo_root`,
`find_remotes`, and a handful of true subprocess CLI tests covering
`--json` vs. plain output and the not-a-git-repo error). The existing
`test_git_review_check.py` suite (11 tests) passes unchanged against the
refactor — confirms it's behavior-preserving, not just re-tested. Full
suite: 584 unit tests (was 570) + 126 template tests, all green.

Still not verified against a real sandbox on the host (same caveat as
§48).

## 49. Python code moved out of `sbx/bin`, into a proper `sbx/src/wmf_sbx/` package — DONE 2026-09-11

cananian's request: `sbx/bin` should be safe to add to `$PATH` -- every
`wmf_sbx_*.py` module living there put the whole python source tree on
PATH alongside the actual CLI entry points. Fix, following the PyPA
src-layout convention cananian pointed at: importable code moves to
`sbx/src/wmf_sbx/`, one file per command with the `wmf_sbx_` prefix
dropped (`wmf_sbx_resolve.py` -> `wmf_sbx/resolve.py`, imported as
`wmf_sbx.resolve`), and `sbx/bin` keeps only the thin, non-importable
CLI wrappers and the two bash `HELPER_SCRIPTS` (`git-safe-reset`,
`git-review-check`).

Three questions asked before starting, all resolved by cananian:

- **Packaging**: `sys.path` only, no `pyproject.toml`/`setup.py` for
  pip. This was never an installed package and doesn't become one --
  the bin/ wrappers and the tests both add `sbx/src` to `sys.path` by
  hand before importing, same zero-install-step philosophy as before
  the move. (`sbx/src/wmf_sbx/setup.py` is a different thing entirely --
  see below -- and this decision doesn't touch it.)
- **Tests**: one `sbx/tests/` directory, sibling to `src/` (the other
  PyPA convention), holding both the python-module tests and the two
  bash-script tests (`test_git_review_check.py`,
  `test_git_safe_reset.py`) together -- they'd only artificially split
  otherwise.
- **`__main__.py`**: yes, add a dispatcher, so `python3 -m wmf_sbx
  <command>` works anywhere `sbx/src` is on `PYTHONPATH`, as a second
  entry point alongside (not instead of) the `sbx/bin/wmf-sbx-<command>`
  wrappers that stay the primary, PATH-visible way in.

What moved (all via `git mv`, so history follows):

- The 10 `wmf_sbx_*.py` modules -> `sbx/src/wmf_sbx/{create,deps,kit,
  ls_remotes,remotes,resolve,resume,rm,setup,state}.py`.
- `sbx/bin/requirements.txt` -> `sbx/requirements.txt`.
- `sbx/bin/tests/*` (12 files) -> `sbx/tests/` (same basenames); the
  empty `sbx/bin/tests/` directory was removed.

New files: `sbx/src/wmf_sbx/__init__.py` (package docstring, points
here) and `sbx/src/wmf_sbx/__main__.py` (the dispatcher -- a `COMMANDS`
dict of subcommand name -> module name, resolved with
`importlib.import_module` and invoked via that module's `main(argv)`;
`create`/`resume`/`rm`/`resolve`/`ls-remotes` are wired up, matching the
bin/ wrappers that already existed). `sbx/tests/test_wmf_sbx_main.py`
tests it (8 tests: unknown command, no args, `--help`, every advertised
command actually resolves to a module with a callable `main`, a mocked
dispatch check, and three real subprocess `python3 -m wmf_sbx ...`
end-to-end checks -- including that `resolve --help` reaches the
submodule's own argparser, not just that the dispatcher recognized the
name).

Internal imports switched from the old flat-namespace pattern --
`sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` then
`import wmf_sbx_state as state_mod` -- to ordinary package-relative
imports, `from . import state as state_mod`. Safe because nothing
anywhere invokes a `wmf_sbx_*.py` file directly as a script; every call
site is either a `sbx/bin/wmf-sbx-<command>` wrapper (which puts
`sbx/src` on `sys.path` and does `from wmf_sbx.<mod> import main`) or a
test/`__main__.py` import -- both load the module as part of the
`wmf_sbx` package, which is exactly when a relative import resolves.

Two modules compute sibling paths from their own `__file__` and needed
recalculating for the new, one-directory-deeper location:

- `wmf_sbx/create.py`'s `WMF_SBX` (the `sbx/bin/wmf-sbx` safety wrapper
  it shells out to, which stays in `bin/`) now walks up three levels
  (`wmf_sbx/create.py` -> `wmf_sbx/` -> `src/` -> `sbx/`) before
  crossing back into `bin/`.
- `wmf_sbx/kit.py`'s `REPO_ROOT` (for `DEFAULT_PROFILE_PATH`) and
  `HELPER_SCRIPT_DIR` (for the `HELPER_SCRIPTS` it copies into every
  generated kit, §15.4) go through a new `SBX_ROOT` constant computed
  the same three-levels-up way; `STATIC_SETUP_SCRIPT` now just points at
  the sibling `setup.py` in the same package directory, since `kit.py`
  and `setup.py` live together again post-move.

Both were verified against the real filesystem (path exists / is a
directory) before running the suite, not just eyeballed -- these are
exactly the kind of silent breakage (kit generation or sandbox launch
failing only at runtime, months later) a depth-off-by-one in a
`__file__` computation causes.

`sbx/bin/wmf-sbx-{create,ls-remotes,resolve,resume,rm}` were rewritten
to the same three-line pattern: insert `../src` onto `sys.path`, `from
wmf_sbx.<mod> import main`, call it. `sbx/src/wmf_sbx/setup.py` is the
one module that does *not* get a `sbx/bin/wmf-sbx-setup` wrapper or a
`from . import` anywhere -- `write_kit_dir()` (`wmf_sbx/kit.py`) copies
it verbatim (dropping the `.py`) into every generated kit as
`/home/agent/wmf-sbx-setup`, and it's deliberately import-free so it
still runs with no sibling modules present inside the sandbox. That
property is unaffected by the move; confirmed it imports nothing
internal either before or after.

All 12 moved test files got matching path fixes: `sys.path.insert` now
points at `../src` instead of `..`; `import wmf_sbx_X as x` became
`import wmf_sbx.X as x`; the two bash-test files' and
`test_wmf_sbx_ls_remotes.py`'s `BIN` constant (they invoke bin/ scripts
as subprocesses) gained the `bin` path component that used to be
implicit in living inside `sbx/bin/tests/`. One real bug turned up
here: `test_wmf_sbx_resolve.py`'s `EXAMPLE_CONFIG` had two `".."`
levels baked in (right for `sbx/bin/tests/`, two levels below `sbx/`;
wrong for `sbx/tests/`, one level below) -- it was silently resolving
to a nonexistent path, `load_config` was returning zero rules, and two
tests were failing on "no config rule matches" rather than on a path
error. Fixed by dropping the extra `..`; grepped the rest of the test
files for the same double-`..` shape afterward and found no other
instances.

Also swept every moved module's docstrings/comments for stale
`wmf_sbx_X.py`-style cross-references (`sed -E
's/wmf_sbx_([a-z_]*)\.py/wmf_sbx\/\1.py/g'`) to the new `wmf_sbx/X.py`
style that matches where the file actually lives now -- comments only,
no code changes, re-verified with the full suite afterward.

Final state: `python3 -m unittest discover -s sbx/tests` -- 592 tests
(was 584; +8 for `test_wmf_sbx_main.py`), all green. `py_compile` clean
across every touched file. `./tests/test-templates.sh` (126 tests,
unrelated top-level suite) unaffected, confirming no collateral damage
outside `sbx/`. `sbx/bin` now holds only bash scripts (`wmf-sbx`,
`git-safe-reset`, `git-review-check`) and the five thin python CLI
wrappers -- none of them importable as `wmf_sbx_*` modules -- so it's
now the directory cananian can actually recommend adding to `$PATH`.

## 50. `wmf-sbx-start`: recover a sandbox's git remotes without attaching an agent — DONE 2026-09-11

The bug cananian described: exiting the claude session inside a sandbox
does *not* stop it -- it stays `running`, and `git fetch sandbox-<name>`
on the host keeps working, for a while. But sbx eventually stops an idle
sandbox on its own regardless, moving it to `stopped`, and a stopped
sandbox publishes no port at all -- the `sandbox-<name>` remotes'
git daemon goes unreachable, silently, with no session left running to
notice. If the engineer only realizes this after the fact and wants to
pull whatever the sandbox has out of it, `wmf-sbx-resume` is the wrong
tool: it does the exact restore that's needed (start the container back
up, put the mount layout back, re-point the remotes at the daemon's new
host port) but then attaches an agent via `wmf-sbx run --name`, which is
neither wanted nor necessary just to make `git fetch` work again.

Fix: `wmf_sbx/resume.py`'s "start the container / redo the mounts /
re-point the remotes" logic (previously inlined in its `main()`) is now
its own function, `start_and_restore()` -- same behavior, same
parameters (`no_restore`, `no_remotes`, `dry_run`, `run`, `env`), same
return convention (False only if `wmf-sbx exec ... true` itself
couldn't start the container). `resume.main()` calls it and then goes on
to build and run the `wmf-sbx run --name` command exactly as before --
this refactor changes nothing observable about `wmf-sbx-resume`, and the
existing `test_wmf_sbx_resume.py` suite (which exercises `main()`
end-to-end, not the internals) confirms that by passing unmodified.

New `wmf_sbx/start.py` is the thin wrapper `wmf-sbx-resume` no longer
has to be for this case: it parses `NAME [--no-remotes] [--no-restore]
[--dry-run]`, validates the name, and calls `start_and_restore()` --
nothing else. No `wmf-sbx run`, ever; `test_never_builds_a_run_command`
in the new `test_wmf_sbx_start.py` (9 tests, mirroring the equivalent
`test_wmf_sbx_resume.py` cases: start-then-wait-then-repoint ordering, a
timed-out wait falling back to `--restore`, `--no-remotes`/`--no-restore`
each skipping their step, `--dry-run` doing nothing, a sandbox with no
recorded state still starting cleanly, an unstartable sandbox returning
1, and an invalid name being refused up front) asserts it directly.
Wired up the same way as every other command: `sbx/bin/wmf-sbx-start`
(three-line wrapper, same pattern as the other five), and `"start":
"wmf_sbx.start"` added to `wmf_sbx/__main__.py`'s `COMMANDS` -- picked
up automatically by `test_wmf_sbx_main.py`'s
`test_every_advertised_command_resolves_to_a_real_module`, which
iterates `COMMANDS` rather than naming commands, so it needed no edit.

Idempotent by construction, like the restore it wraps: safe to run
against a sandbox that's already `running` (`start_sandbox` no-ops,
the mount restore checks each mount before making it, the remote
re-point is a bare lookup-and-rewrite either way) -- so this is also
just a plain "make sure I can fetch from this sandbox" command, not
only a stopped-sandbox recovery tool.

Full suite: 601 tests (was 592; +9 for `test_wmf_sbx_start.py`), all
green. `./tests/test-templates.sh` unaffected (126/126).

Still open, carried over from the "Still to do" item this closes: a
README that mentions `wmf-sbx-start` as the answer when a sandbox's git
remotes stop working -- there is no README yet (separate item below).

## 51. `wmf-sbx-exec`: `wmf-sbx-start`'s restore, then a one-off command — DONE 2026-09-11

cananian's follow-on to §50: "let's make a `wmf-sbx-exec` wrapper as
well, which just does the `wmf-sbx-start` actions ... and then runs
`wmf-sbx exec`." The gap it closes is the same stopped-sandbox bug as
§50, reached through a different door. `sbx exec NAME -- CMD` does
start a stopped sandbox as a side effect (there's no `sbx start`), but
that's *all* it does -- it never puts the mount layout back or
re-points the `sandbox-<name>` host remotes at the daemon's new host
port. Run a bare `sbx exec NAME -- git log` against a sandbox that's
been stopped a while and it lands in the five-commit clone from §40,
`--shared` alternates pointing nowhere -- the same failure, just via
`exec` instead of `run`.

New `wmf_sbx/exec.py` is a thin composition of two things that already
exist: `resume.start_and_restore()` (§50 -- start, wait/restore mounts,
re-point remotes), then `[wmf-sbx, "exec", NAME, "--", *CMD]` with the
caller's actual command. No new restore logic. `split_exec_args()`
mirrors `resume.split_agent_args()` exactly and for the same reason:
argparse can't tell a flag *meant for us* from one meant for the
wrapped command once both can appear on the same line, so everything
after a literal `--` is taken verbatim as the command, even if it looks
like `--dry-run`. Missing a command after `--` (or no `--` at all) is a
usage error (`parser.error`, exit 2) -- there's nothing sensible to run
otherwise. Same `--no-remotes` / `--no-restore` / `--dry-run` flags as
`wmf-sbx-start`, for the same reasons, since they gate the exact same
restore step; `--dry-run` prints what `start_and_restore()` would do
plus the final `exec` line and runs nothing. The command's own exit
code is `wmf-sbx-exec`'s exit code, via `run(cmd).returncode` with
stdio inherited (unchanged from how `resume.py`'s final attach runs) --
the wrapped command may itself be interactive, or just expect its
output to reach the terminal directly.

`exec` is a Python 3 builtin function, not a keyword, so `wmf_sbx/exec.py`
/ `import wmf_sbx.exec` / `importlib.import_module("wmf_sbx.exec")`
(used by `__main__.py`'s dispatcher) are all fine -- checked before
naming the file this, since a collision there would have been an
annoying one to debug later. Wired up the same way as every other
command: `sbx/bin/wmf-sbx-exec` (three-line wrapper), `"exec":
"wmf_sbx.exec"` added to `__main__.py`'s `COMMANDS` -- again picked up
automatically by `test_wmf_sbx_main.py`, no edit needed there.

`test_wmf_sbx_exec.py` (12 tests): `split_exec_args()` on its own
(no separator, a separator with a command, a separator with an empty
tail), then `main()` end-to-end -- start-wait-repoint happens before
the actual command runs, the command's exit code is returned as-is,
an unstartable sandbox returns 1 and the command never runs at all, no
command after `--` is a usage error, `--no-remotes`/`--no-restore` each
skip their step, `--dry-run` runs nothing, an invalid name is refused
up front, a sandbox with no recorded state still execs (just doesn't
try to re-point anything). The fake `run` needed one wrinkle
`test_wmf_sbx_start.py` didn't: the mount-verify/`--restore` calls
`start_and_restore()` makes on the way in use the same `wmf-sbx exec
NAME -- ...` shape as the caller's own command, so the stub has to
recognize the `wmf-sbx-setup` tail specifically and always let it
succeed -- otherwise a test asserting the *command's* exit code
(`cmd_rc`) would accidentally also fail the mount wait and trigger an
unwanted `--restore` fallback.

Full suite: 613 tests (was 601; +12 for `test_wmf_sbx_exec.py`), all
green. `./tests/test-templates.sh` unaffected (126/126).

## 52. `upstream_plan`: `local`/`origin` for any cloned repo, not just Gerrit ones — DONE 2026-09-11

cananian: "we're only adding the `local` remote in the sandbox and
adding/syncing the `git remote` on the host if the directory in the
sandbox is cloned and *if the directory corresponds to a gerrit
project*. That latter restriction is not correct ... no gerrit-specific
knowledge is needed for that." GitLab's project-listing API research
(`DESIGN-repo-resolution.md`'s open item) stays explicitly out of scope.

Root cause was narrower than "gerrit-specific gating" suggests: the
host-side `sandbox-<name>` remote (`remotes.py`/`parallel_tree_remotes`)
was never gated on canonical at all. Only `upstream_plan` (§8.1's
`upstreamUrl`, consumed by `configure_remotes` to do the in-sandbox
`origin`→`local` rename) dropped any repo with no canonical, and the
*only* way a raw-path argument (`is_raw_path` -- the common case, see
`DESIGN-repo-resolution.md`) got a canonical at all was
`canonicals_for_kit` → `resolve.reverse_resolve`, which tries `.gitreview`
parsing first -- a Gerrit-only convention GitLab clones never have -- and
only falls back to an *exact* (non-wildcard) `repos.yaml` rule.

Fix: `upstream_plan` (`create.py`) gained a second fallback,
`host_upstream_url(host_dir, run)`, tried when there's no canonical or
`clone_url` doesn't know the scheme. It runs
`git -C <host_dir> remote get-url origin` and uses the result only if it
starts with `http://`/`https://` -- same no-SSH-agent reasoning as §8.1's
own URL choice (`NOTES.md` #8). This needs zero forge knowledge: it reads
whatever the host's own checkout is already configured with, so it works
identically for a GitLab clone, a Gerrit clone missing from
`repos.yaml`, or anything else already checked out on the host.
`upstream_plan` gained an injectable `run=subprocess.run` parameter
(the codebase's usual testability convention) and its one call site
(`create.py`'s `main()`) now passes `run=run`.

Left alone: `canonicals_for_kit`/`reverse_resolve` themselves (kit
environment-variable wiring like `MW_CORE_REPO` is a separate concern
from remote setup, and still needs an actual canonical, not just a URL);
`configure_remotes` (`setup.py`) needed no change at all, since it just
consumes whatever `upstreamUrl` shows up in the plan.

New tests in `test_wmf_sbx_create.py`: `UpstreamPlanTests` extended with
the host-origin-fallback success case, the non-http(s)-origin rejection
case, and the not-a-git-repo case; new `HostUpstreamUrlTests` for the
helper directly (http origin, failure, ssh origin, `OSError` if `git`
itself is missing). Full suite: 620 tests (was 613; +7), all green.
`./tests/test-templates.sh` unaffected (126/126). `DESIGN-setup-steps.md`
§8.1 updated to describe the fallback.

## 53. `sbx-` as the one prefix, for both the sandbox name and its host remote — DONE 2026-09-11

cananian: "currently the default sandbox name has a `mw-` prefix, and
the default remote name has a `sandbox-` prefix ... the remote usually
starts `sandbox-mw-` because it has both the sandbox- prefix *and* the
sandbox name `mw-` prefix. Can we make these consistent (and shorter)
by using `sbx-` as the prefix for everything?" i.e. `wmf-sbx-create
~/Wikimedia/VisualEditor` should name the sandbox `sbx-visualeditor`
*and* name its host remote `sbx-visualeditor` too, not
`sandbox-sbx-visualeditor`.

Two independent one-line changes, previously compounding: `default_sandbox_name`
(`create.py`) built the sandbox name as `f"mw-{slug}"`; `remote_name_for`
(`remotes.py`) then wrapped *any* sandbox name (default or `--name`
custom) as `f"sandbox-{sandbox_name}"` for the host remote. Switched the
former to `f"sbx-{slug}"` (`sbx-sbx` for the empty-slug fallback, same
shape as the old `mw-sbx`) and the latter to return `sandbox_name`
unchanged -- the sandbox name already carries the one prefix that
matters, so the remote just reuses it verbatim. A `--name custom`
sandbox now gets a remote literally named `custom`, same as it always
implicitly did modulo the `sandbox-` wrapper.

`remote_name_for` is the single place remote names are computed
(`create.py`'s `add_host_remotes`/`print_remote_add_reminder`, `rm.py`'s
missing-state fallback message, `resume.py`'s two advisory messages all
call it) -- no other file had its own copy of the `sandbox-{name}`
string to fix, though several docstrings/comments did (updated for
accuracy, not behavior: `remotes.py`, `state.py`, `start.py`, `exec.py`,
`rm.py`, `resume.py`, `create.py`, `git-review-check`, `README.md`).

Tests: `DefaultSandboxNameTests`, `MainDryRunTests`,
`MainNameCollisionTests`, `MainRunTests`, `ParallelTreeRemotesTests`,
`PrintRemoteAddReminderTests` (`test_wmf_sbx_create.py`) and
`MainTests.test_dry_run_starts_nothing` (`test_wmf_sbx_start.py`) had
`mw-cite`/`sandbox-mw-*` literals asserting the old shape -- updated to
`sbx-cite`/`mw-*` (a bare sandbox-name literal, unprefixed, is what
several of these tests already used as an arbitrary example name
unrelated to `default_sandbox_name`, so those were left alone). New
`RemoteNameForTests` in `test_wmf_sbx_remotes.py` covers
`remote_name_for` directly. Full suite: 629 tests (was 620; +9 for
`link_parsoid_checkout`'s own unrelated addition, then +2 net here after
fixing 9 pre-existing assertions and adding 2 new ones), all green.
`./tests/test-templates.sh` unaffected (126/126).

## 54. `link_parsoid_checkout`: literal path, no dead guard, before every skin/extension — DONE 2026-09-11

cananian, after seeing the actual generated `LocalSettings.php` tail from
`wmf-sbx-create gerrit:mediawiki/extensions/VisualEditor
gerrit:mediawiki/services/parsoid`, found three things wrong with §Parsoid
task's output:

1. `$parsoidInstallDir` was set to the parallel-tree path
   (`/home/agent/Projects/Wikimedia/Parsoid`), meaningless to the human
   developer on the host, who only ever sees the literal, host-mirrored
   path (`/home/cananian/Projects/Wikimedia/Parsoid`) -- invisible to
   them that it's actually a bind-mount alias over the parallel-tree
   clone (see §15.2's `setup_repo`/`bind_over`, and the earlier
   conversation this task grew out of).
2. The `if ( $parsoidInstallDir !== 'vendor/wikimedia/parsoid' ) { ... }`
   guard from the MediaWiki.org recipe is dead code here:
   `$parsoidInstallDir` is always an absolute path this script sets, so
   the guard can never be false. Copying it byte-for-byte "to match the
   docs" (the original rationale, see §Parsoid) wasn't worth the
   confusion of a permanently-true condition.
3. The block was inserted where `wfLoadExtension( 'Parsoid' )` used to
   be -- i.e. down among the other extensions, *after* skins load. But
   the autoloader intercept has to run before *anything* might reference
   a Parsoid class, including another extension loaded ahead of it, not
   just before Parsoid's own `wfLoadExtension`.

Fixed all three in `setup.py`:

- `run_setup`'s per-repo loop now sets `parsoid_dest = literal_path`
  (was `dest`, the parallel-tree path) -- `setup_repo`'s "alias" mode
  makes `literal_path` *be* the writable clone, so that's the path with
  meaning on the host side.
- `PARSOID_LOCAL_SETTINGS_TEMPLATE` drops the `!== 'vendor/wikimedia/parsoid'`
  wrapper; the function body it guarded is dedented to top level.
- Added `PARSOID_ENABLED_SKINS_MARKER = "// Enabled skins."` --
  `mw-install:sqlite`'s own comment, always emitted ahead of every
  `wfLoadSkin`/`wfLoadExtension` call it writes. `link_parsoid_checkout`
  now inserts the block just above that marker instead of in place of
  `PARSOID_LOAD_EXTENSION_LINE`, and separately deletes the original
  `wfLoadExtension( 'Parsoid' );` call from wherever it landed (now
  redundant/would double-load the extension, since the inserted block
  makes that same call itself). Missing marker is a new warn-and-continue
  failure mode, same contract as the existing missing-extension-line one.

Tests: `LinkParsoidCheckoutTests` fixtures now include a
`// Enabled skins.` line (matching real `mw-install:sqlite` output);
`test_replaces_the_wfloadextension_line` gained position assertions
(block before the marker, marker before `wfLoadSkin`) and a
`vendor/wikimedia/parsoid` absence check; new
`test_missing_skins_marker_warns_and_leaves_the_file_alone`.
`MainSetupChainTests.run_main_with_fake_install`'s faked
`LocalSettings.php` gained the marker line; `test_a_writable_parsoid_clone_gets_linked_into_localsettings`
now asserts the literal host path, not the parallel-tree clone path.
Full suite: 630 tests (was 629; +1 net), all green.

## 55. Getting the plugin into the sandbox: what sbx already provides, and the GitLab token — PLANNED 2026-09-11

Design docs: `sbx/DESIGN-plugin-integration.md` and, split out of it,
`sbx/DESIGN-gitlab-integration.md`. Nothing implemented; this is the
plan for the "Still to do" item about porting the parent package's
skills, agents, hooks and MCP servers into the generated kit. cananian
asked for particular attention to GitLab auth, since better GitLab (as
opposed to Gerrit) repo handling may need a token — the investigation
found the two subjects barely touch, which is why they are two docs
(55.4).

Four findings changed the shape of the plan, all measured in this
sandbox rather than read off documentation:

**55.1 `~/.claude/skills` is a mount, so the obvious implementation is
out.** `/proc/mounts` has `none /home/agent/.claude/skills virtiofs
rw,nosuid,nodev` — sbx's shared agent-skills store, mounted into every
sandbox. Anything a kit ships in `files/home/.claude/skills/` is
shadowed at container start. `~/.claude/plugins/` is by contrast an
ordinary directory (already holding `marketplaces/claude-plugins-official/`),
so the route in is the *plugin* wiring the nono pack already knows how
to write — marketplace JSON, `known_marketplaces.json`,
`installed_plugins.json`, `enabledPlugins` — all six targets of which
live under `$HOME` and so are reachable from a kit's `files/home/` tree.
That also keeps the `/wmf-claude:` namespace and brings `agents/` and
the `SessionStart` hook along, which the skills store cannot carry. Two
caveats recorded in the doc: those are Claude Code *internal* state
files with no format guarantee (so assert the plugin loaded at startup
rather than trusting the write), and `settings.json` already exists with
sbx's own keys, so the `wiring/settings-merge.json` denies have to be
merged in a `setup.install` step, not dropped as a static file.

**55.2 IMPLEMENTATION-PLAN §9 is obsolete.** Phase 4 budgets two weeks
to build a host-side broker so the sandbox can reach the Phabricator and
Gerrit MCP servers. sbx ships exactly that: `sbx mcp add NAME --command
… --args …` registers a host-side stdio server, `--static-mcp a,b`
exposes it, and `~/.claude.json` in this sandbox is *already* pre-seeded
with the gateway (`http://mcp-gateway.docker.internal/mcp`,
`Authorization: Bearer proxy-managed`). So the work is a few lines in
`build_sbx_command` plus host-side registration, not a transport. Two
consequences: the pre-seeded entry means nothing may ship a whole
`~/.claude.json` (it would clobber the gateway), and a gateway-mediated
server **runs on the host, outside the sandbox boundary** — tolerable
here because both servers are anonymous public readers, but it is the
first thing this project deliberately runs outside the boundary and it
belongs in the `SECURITY.md` rewrite already owed by the
read-only-lockdown item. The one blocking unknown is tool naming: if
the gateway renames `mcp__phabricator__*` / `mcp__gerrit__*`, the skills
and `session-start.sh` reference those literally, and editing them
collides with the "do not fork `skills/`" non-goal. Fallback if so: a
`:ro` mount of the host wmf-claude checkout, which sbx places at the
same literal path inside — so `gerrit-mcp-server/.venv/bin/python` still
resolves, the servers stay stdio children of Claude Code, and the names
are unchanged.

**55.3 The hook can't ship as-is.** `bin/session-start.sh` states that
the session is sandboxed by nono and describes `bin/claude --chrome` /
`--local-web` tiers that don't exist here. Shipping it unedited tells
the model false things about its own environment, which is worse than
shipping no hook. That makes Seam 1 (`WMF_CLAUDE_SANDBOX_BACKEND`,
`hooks/sandbox-context/<backend>.txt`) a prerequisite rather than a
parallel nicety — and the sbx text it needs is largely `HOME_CLAUDE_MD`,
which argues for one source rather than two.

**55.4 GitLab: there is no auth configuration to reuse, and the question
turned out to be orthogonal.** Audited the whole parent package:
`mcp-phabricator` is explicitly anonymous, `gerrit-mcp-server` reads a
public Gerrit, `chrome-devtools-mcp` is local CDP, and
`profiles/wmf-engineer.json` declares no credential at all. The only
credential the package handles is the Claude API key, and that's nono's.
No GitLab skill, no GitLab MCP server, nothing to inherit.

More useful than that negative result: the GitLab work splits along the
sandbox boundary, and the half that motivates it — the three open to-dos
about bare-name search, `forked_from_project` and `local` remotes for
non-Gerrit clones — is entirely **host-side**, in `wmf_sbx.resolve` /
`wmf_sbx.create`, running before any sandbox exists. It needs a plain
host token (and must keep working with none, since public projects
answer anonymously), and sbx's credential machinery has nothing to do
with it. Only *in-sandbox* GitLab access wants the proxy-managed
placeholder. Since that is a different subject from getting a plugin
into a kit, it moved to `sbx/DESIGN-gitlab-integration.md`; the single
surviving link between the two docs is that if the agent ever uses
GitLab's own MCP endpoint, it arrives via the plugin wiring.

(The reusable *shape* for that case comes from the sbx base image rather
than from us: its official marketplace ships a `gitlab` plugin that is
nothing but `{"type": "http", "url": "https://gitlab.com/api/v4/mcp"}`,
and a `github` plugin demonstrating `${VAR}` expansion in a plugin
`.mcp.json`. Details in the GitLab doc.)

Also noticed in passing, not fixed: `skills/standalone-vuln-audit/`
exists on disk but is missing from `package.json`'s `artifacts` list.
The top-level `CLAUDE.md` warns the skill list lives in three places;
it has already drifted in one of them. Whatever enumerates skills for
the kit should read the `skills/` directory rather than adding a fourth.

## 56. Seam 1: the SessionStart hook stops describing a sandbox it isn't in — DONE 2026-09-11

First item in `sbx/DESIGN-plugin-integration.md`'s order of work, and a
prerequisite for shipping the plugin's hook into a generated kit at all:
`bin/session-start.sh` asserted things about the environment that are
false under sbx, and a confidently wrong SessionStart block is worse than
a missing one.

**56.1 Three bullets were nono-specific, not one.** The plan
(`IMPLEMENTATION-PLAN.md` §6) named only the "sandboxed by nono
(OS-level enforcement)" paragraph. Re-reading the file found two more:
"Phabricator and Gerrit MCP servers are registered" (not under sbx, not
yet) and the local-wiki testing ladder, which tells the model that
Tier 1 "needs the session launched with `bin/claude --local-web`" — a
flag that does not exist here, attached to a claim that a plain session
"can't reach the wiki at all", when in fact under sbx the wiki runs on
this container's own loopback and plain `curl` reaches it.

So the extraction is two files per backend rather than one:
`hooks/context/<backend>/sandbox.txt` (what enforces the boundary) and
`.../environment.txt` (which MCP servers exist, how to exercise a local
wiki). Two files, not one concatenated block, because the backend text
occupies two positions in the bullet list — the sandbox paragraph before
the skills list and the environment bullets after it — and keeping both
positions is what makes the nono output byte-for-byte what it was.
Directory named `hooks/context/` rather than the plan's
`hooks/sandbox-context/`, since it now holds more than the sandbox
paragraph.

**56.2 Verified by diffing old against new, not by reading.** Stashed
the change, captured the hook's output for both the no-broker and
broker cases, unstashed, diffed. Exactly two lines differ, both
intended (56.3 and 56.4). Everything else — wording, ordering, the
blank line before the docker block — is identical.

**56.3 `WMF_CLAUDE_DOCKER_MODE=broker|native|none`**, defaulting to
`broker` when `WMF_DOCKER_BROKER_URL` is set and `none` otherwise, as
the plan specified. The mwdocker block's text needed one edit to serve
both container modes: it said tools "live in a container reached through
a broker", which is false in `native` mode, and now says "through the
`mwdocker` shim" — true in both, and the shim is what the run-tests and
lint skills actually call either way. An explicit mode beats the
broker-URL inference, so a backend with a stale `WMF_DOCKER_BROKER_URL`
in the environment can still say `none`.

**56.4 The skill list had drifted, as `CLAUDE.md` warned it would.**
`standalone-vuln-audit` existed in `skills/` but was in neither
`package.json`'s `artifacts` nor the hook's list — so the nono pack has
been shipping without it and no session has ever been told it exists.
Added to both, and `tests/test-templates.sh` now walks `skills/*/` and
asserts each name appears in both other places, which turns the
three-places gotcha from an instruction into a failing test.

**56.5 Fail closed, both knobs.** An unrecognised backend emits neither
context file and warns on stderr (stderr, so the engineer sees it and
the model does not read the warning as context); an unrecognised docker
mode emits no mwdocker block and warns. In both cases the
backend-agnostic bullets — the skills list, the `git add -p` recipe, the
commit conventions, STE, the review-after-drafting rule — still go out.
The failure mode is "says less", never "says something false".

**56.6 The kit now declares the backend.** `build_kit_spec` puts
`WMF_CLAUDE_SANDBOX_BACKEND=sbx` in `environment.variables`. Inert until
the plugin is actually shipped into the kit (§55's Route A), but it
costs one variable and means the hook is never wrong on the day it
lands. `WMF_CLAUDE_DOCKER_MODE` is deliberately left unset: the hook's
own default of `none` is correct for a kit that installs PHP into this
container rather than reaching a second one through `mwdocker`.

Tests: `tests/test-templates.sh` gained the backend-aware block (default
is nono; sbx emits sbx text and no nono sandbox text; sbx claims no
`bin/claude --local-web` and no registered MCP servers; unknown backend
omits and warns while keeping the shared bullets; every backend
directory has both files), the container-tooling block (none/broker/
native/explicit-override/unknown), and the skill-list consistency walk —
156 passing, 0 failing. `sbx`: 631 tests (was 630), all green.

Still open, and named in `DESIGN-plugin-integration.md`: the sbx
environment text asserts there is no browser MCP in this backend, which
is true today but is the thing §8's deferred chrome-devtools item would
change; and `/wmf-claude:check-nono-update` is still offered under every
backend. The second is deliberate — an offered skill that happens not to
apply is harmless, unlike a claim about the environment — and the test
says so where someone would otherwise "fix" it.

## 57. Step 2, the half that needed no host: six of eleven open questions — MEASURED 2026-09-11

`DESIGN-plugin-integration.md` §5 step 2 says "measure, on the host,
before building anything", and lists eleven open questions across that
doc's §6 and `DESIGN-gitlab-integration.md` §5. Six of them turned out
to be answerable from inside this dev sandbox, which is the *better*
place to ask several of them: the question is what a **sandbox's** Claude
Code build does, and the host's build is a different install.

All measured 2026-09-11 in `wmf-claude-sbx`, Claude Code 2.1.269.

### 57.1 A plugin-root `.mcp.json` works — and renames every tool

Plugin-integration §6 Q4. Built a throwaway plugin (`plugin.json` plus
`.mcp.json` plus a 25-line dependency-free stdio MCP server exposing one
`probe_ping` tool) and ran
`claude -p --output-format stream-json --verbose --plugin-dir …`.

The `init` event answers it directly:

```json
"mcp_servers":[{"name":"plugin:probe-plugin:probemcp","status":"connected"},
               {"name":"mcp-gateway","status":"connected"}]
```

So **yes**: Claude Code reads MCP servers declared at a plugin root,
`${CLAUDE_PLUGIN_ROOT}` expands in the `args`, and the server connects and
answers — the model called it and got `PONG` back. That is the good news,
and it composes exactly as §4 hoped with Route A.

The bad news is in the tool name. The model's `tool_use` was:

```
mcp__plugin_probe-plugin_probemcp__probe_ping
```

`mcp__plugin_<plugin>_<server>__<tool>`, not `mcp__<server>__<tool>`.
Shipping a `.mcp.json` in the wmf-claude plugin would therefore surface
Phabricator as `mcp__plugin_wmf-claude_phabricator__phabricator_get_task`,
and every literal tool name in `skills/review-patch`,
`skills/write-commit-msg`, the `gerrit-reviewer` agent and
`bin/session-start.sh` would be wrong. That collides with the
"do not fork `skills/`" non-goal, for a mechanism whose whole appeal was
that it needs no mutation of Claude's state files.

**Conclusion: prefer `claude mcp add --scope user` in a startup step.**
It is what `bin/wmf-claude-setup` already runs on the host, and it is the
only registration route measured so far that keeps the names the skills
already use. The plugin `.mcp.json` is a documented fallback, not the
default — and it is the right mechanism the day a *new* server arrives
that no skill names literally.

### 57.2 The gateway serves five meta-tools, not the servers' own

Plugin-integration §6 Q2, partly. Spoke JSON-RPC to
`http://mcp-gateway.docker.internal/mcp` directly — `tools/list` is
refused until you complete the `initialize` handshake and echo the
`Mcp-Session-Id` response header back on every later call, which is why
`claude mcp list` (which only says "connected") cannot answer this.

In this sandbox's default *dynamic* mode the gateway serves exactly five
tools: `mcp-find`, `mcp-add`, `mcp-config-set`, `mcp-exec`, `code-mode`.
A server's own tools are reached by *calling* `mcp-exec` with a name —
so through the gateway the model's tool is `mcp__mcp-gateway__mcp-exec`,
never `mcp__phabricator__phabricator_get_task`. The catalog here is
empty (`mcp-find` matches nothing for any query), because nothing was
registered host-side for this sandbox.

What `--static-mcp` changes is still genuinely unknown and still needs
the host — but note the prefix cannot improve: Claude Code derives it
from the *config entry* name, which is `mcp-gateway` whatever the
gateway mounts. Static mode can at best yield
`mcp__mcp-gateway__phabricator_get_task`. **Route 1 cannot preserve the
existing tool names**, and that was the thing §4 said would decide it.
Route 3 (`:ro` mount of the built servers, registered with
`claude mcp add`) is now the front-runner, on the evidence — **wrong, in
the end: Route 3's venv cannot cross the boundary and Route 2 is the
answer. See §59.**

Packaged the probe as `sbx/bin/wmf-sbx-gateway-tools` — the one
`wmf-sbx-*` script that runs *inside* a sandbox rather than on the host.
It is how we check the naming under `--static-mcp` without a second
hand-typed handshake, and it belongs in the §7 acceptance test.

### 57.3 `permissions.deny` **is** honoured under `bypassPermissions`

Plugin-integration §6 Q5, and the answer changes what step 3 ships.
Ran `claude -p --permission-mode bypassPermissions` with a `--settings`
file carrying one deny rule, pointed at a canary file, and read the
`permission_denials` array out of the result event.

| deny pattern | Read attempt | verdict |
| --- | --- | --- |
| `Read(/tmp/probe-secret/**)` | **succeeded**, canary returned | not matched |
| `Read(//tmp/probe-secret/**)` | denied | honoured |
| `Read(**/*.pem)` | denied | honoured |
| `Bash(cat:*)` | denied | honoured |

So the deny list is worth porting — it is not dead weight under
`bypassPermissions`, which was the worry. But the first row is a trap
worth keeping: a **single-slash absolute path silently matches nothing**.
Claude Code wants `//` for filesystem-absolute patterns; `~/…` and `**`
globs work as written. `wiring/settings-merge.json` has no single-slash
absolute patterns today (checked, all 50-odd entries), so it ports
as-is — but anything *added* to that list needs this rule, and a rule
that fails open by silently matching nothing is exactly the kind that
ships as false assurance.

One thing the table shows in passing: the first probe defeated
`Read(…)` by reaching for `Bash(cat …)`, unprompted. Under nono that is
what the OS-level profile is for. Under sbx the container is that layer,
and the deny list is a speed bump inside it, not a boundary — the sbx
`SECURITY.md` rewrite (§5 step 5) should say so plainly rather than let
a ported deny list imply otherwise.

### 57.4 `npm ci` in core fetches no browser at all

Plugin-integration §6 Q6, read off core's own `package.json`,
`package-lock.json` and `Gruntfile.js` from Gerrit rather than guessed.

- The QUnit target's default browser is **headless Firefox**, not Chrome:
  `karma.main.browsers = ['FirefoxHeadless']`. Chrome is the separate
  `grunt qunit:chrome` target (`ChromeCustom`, `base: ChromeHeadless`,
  flags from `CHROMIUM_FLAGS` — the Gruntfile's own comment says Chrome
  needs `--no-sandbox` in Docker/CI).
- Neither launcher downloads anything. `karma-chrome-launcher` 3.1.0
  depends on `which` and nothing else; `@wikimedia/karma-firefox-launcher`
  on `which` and `is-wsl`. They *locate* a system browser.
- The only lockfile entries with install scripts are `esbuild`,
  `geckodriver`, `edgedriver`, `@scarf/scarf` and a `vue-demi` — drivers,
  not browsers. `@puppeteer/browsers` is present but as a wdio runtime
  dependency: it downloads when Selenium asks it to, not at `npm ci`.

So the deferred chrome-devtools item (§8) costs "install a browser into
the image or the kit", exactly as written — `npm ci` does not do it for
us. And the finding is bigger than §8: **`grunt qunit` cannot run in a
generated sandbox today either**, because no Firefox is installed and
nothing will fetch one. That belongs in the kit's setup story, not in
the chrome-devtools follow-up, since it blocks a test suite the
`run-tests` skill already offers to run.

### 57.5 GitLab: anonymous reads work, MCP needs a token

`DESIGN-gitlab-integration.md` §5 Q1, Q2 and Q4, all against
`gitlab.wikimedia.org` from inside this sandbox with no credential:

- `GET /api/v4/projects?search=wmf-claude&per_page=3` → **200**, real
  results. §2's no-token path is real, not wishful, and stays a fully
  working path.
- `GET /api/v4/projects/repos%2Fproduct-safety-and-integrity%2Fwmf-claude`
  → 200 (id 4323, public, default branch `main`), so URL-encoded path
  lookup works anonymously too — that is the call repo resolution wants.
- `forked_from_project` **is** present for an anonymous caller: a fork of
  our own repo (id 5038) returns
  `forked_from_project.path_with_namespace =
  repos/product-safety-and-integrity/wmf-claude`. Fork detection needs no
  token.
- `/api/v4/mcp` answers **401**, not 404, to both GET and POST — the WMF
  instance *does* serve GitLab's MCP endpoint, and it is exactly as
  token-gated as §3 assumed. Nothing anonymous to be had there.

Only Q3 is left in that doc, and it is a host fact: is `glab` installed
and authenticated on cananian's laptop?

### 57.6 What is left for the host

Five: whether `sbx create` takes `--static-mcp` (§6 Q1); what the
gateway serves under it (§6 Q2, the remaining half); whether `sbx mcp`
demands `sbx login` (§6 Q3); whether the two MCP submodules are actually
checked out and built in the host clone (§6 Q7 — they are empty here and
in `.sbx-originals`, so Routes 1 and 3 both hang on it); and `glab`.
`sbx mcp add`'s own flags have to be read before its invocations can be
written down, so this is deliberately one round of inventory rather than
a guess that wastes a round trip.

## 58. Step 2 on the host: `--static-mcp` is real, `sbx mcp add` has no `--env` — MEASURED **[cananian, host, 2026-09-11]**

Round 1 of the §57.6 list (`responses15.txt`). Three questions answered,
and two things found that the design had not thought to ask.

### 58.1 The three answers

- **`--static-mcp` exists on `sbx create`** (§6 Q1 — yes), and on
  `sbx run` too. Both say the set "is chosen once at creation time";
  `run` adds that it "cannot be changed when re-attaching to an existing
  sandbox". Our CLI-reference mirror was stale, as suspected. It takes a
  comma-separated list or repeated flags, accumulating either way. So the
  flag belongs in `build_sbx_command` at create time, not in
  `wmf-sbx-resume`/`wmf-sbx-start`.
- **`sbx mcp` does not require `sbx login`** (§6 Q3 — no). `sbx mcp ls`
  exits 0 and prints `LOCAL · managed by you · ✓ on` with no servers
  registered. No new hard dependency for every user, which was the worry.
- **The MCP submodules are checked out and built on the host** (§6 Q7 —
  yes): `gerrit-mcp-server` at `e54c722`, `mcp-phabricator` at `cee06f2`
  (the SHA `951454b` pinned), with `node_modules/` present and
  `.venv/bin/python` in place. Routes 1 and 3 both have something to run.
  They are empty only in the sandbox's clone, because `git clone` does
  not recurse.

### 58.2 `sbx mcp add` has no `--env`, and both our servers need one

Its flags are `--command`, `--args`, `--dir`, plus the `--url`/OAuth
family. There is no way to set an environment variable for a host-side
stdio server — and `bin/wmf-claude-setup` registers both of ours *with*
environment variables:

```
claude mcp add --scope user phabricator -e PHABRICATOR_USERNAME=$PHAB_USER \
  -- node $ROOT/mcp-phabricator/src/index.js
claude mcp add --scope user gerrit -e PYTHONPATH=$ROOT/gerrit-mcp-server/ \
  -e PYTHONDONTWRITEBYTECODE=1 -- $ROOT/gerrit-mcp-server/.venv/bin/python \
  $ROOT/gerrit-mcp-server/gerrit_mcp_server/main.py stdio
```

Not fatal: `env` is itself an executable, so
`--command env --args "PYTHONPATH=…,…/python,…/main.py,stdio"` carries
them, and `--args` is a comma-separated list with no comma in any of our
values. A wrapper script would also do. But it is a wart on Route 1 that
nothing in the docs warned about, and it is one more thing that has to
be *generated* on the host rather than declared in the kit.

### 58.3 Docker's own warning is the SECURITY.md text we owed

`sbx mcp add --help` says a `--command` server "runs as a subprocess on
the HOST, outside the sandbox", and then, in capitals:

> WARNING: Local servers are for ad-hoc development only. They have no
> identity, no verifiable supply chain, and no sandboxing. The process
> runs with your host user's full permissions — it can read your
> filesystem, access your network, and call any API your user can.

That is §4 Route 1's cost in the vendor's own words, and it is stronger
than the note this project wrote for itself ("a small step, both read
public WMF data anonymously"). It also lands on Route 1's *other* flank:
the two servers are ours, but they are registered by a mechanism Docker
explicitly scopes to ad-hoc development. Quote it in the sbx
`SECURITY.md` rather than paraphrasing it.

### 58.4 `sbx mcp load` exists — a server can be attached to a *running* sandbox

Not in our docs mirror: `sbx mcp load` "Load an already-registered MCP
server into a running sandbox". So `--static-mcp` at create is not the
only path, and a sandbox that was created without a server is not a dead
end — relevant to `wmf-sbx-start`/`wmf-sbx-resume`, which exist precisely
to repair a sandbox after a restart. Also present: `sbx mcp inspect`,
`sbx mcp auth`, `sbx mcp rm`. Worth a look before step 4 settles on a
route.

### 58.5 Route 3's new question: the venv's Python version

`gerrit-mcp-server/.venv/bin/python` is a **relative symlink to
`python3`** in the same directory, not an absolute path into the host's
interpreter as §4 Route 3 assumed. Route 3 mounts the checkout `:ro` at
the same literal path, so the venv's `python3` will resolve to whatever
that symlink points at — and the sandbox's own Python is **3.14**, while
the host's is whatever built the venv. If they differ, the venv's
`lib/python3.X/site-packages` does not match the interpreter and every
import fails.

This is the first concrete thing that could sink Route 3 for the Gerrit
server (the Node one has no such coupling, provided `node_modules` holds
no native `.node` binaries). Round 2 asks for `pyvenv.cfg`, the symlink
target, and `python3 -V` on the host.

### 58.6 Two incidental facts worth keeping

- **`sbx --version` is not a flag**; it is `sbx version`. Anything we
  write that probes the version must use the subcommand.
- **The host's Claude Code is *older* than the sandbox's**: 2.1.236 vs
  2.1.269. So §57's measurements were taken in the right place — a
  question about what Claude Code does inside a sandbox must be asked
  inside a sandbox, and an answer from the host could be a version
  behind.
- `glab` is installed (1.36.0) but **knows only `gitlab.com` and has no
  token** (`x No token provided`). So `DESIGN-gitlab-integration.md` §5
  Q3 is answered: `glab auth token` contributes nothing for
  `gitlab.wikimedia.org` today, and §2's precedence chain effectively
  starts at `$GITLAB_TOKEN`. The `glab` step stays in the chain — it
  costs nothing and a user may well authenticate later — but it must not
  be the thing the design leans on, and it needs `GITLAB_HOST` (or
  `--hostname`) to ask about the WMF instance at all.

## 59. Step 2, round 2: the venv cannot cross the boundary, and Route 2 wins on measurement — MEASURED **[cananian, host, 2026-09-11]** + in-sandbox 2026-09-12

Round 2 of the §57.6 list (`responses16.txt`) answered the question that
was meant to sink Route 3, and it did sink it. Two of my own command bugs
cost the round its other two answers — including the one §58 called
decisive. Rather than hand over a third round and wait, I asked the
question a different way, from inside the sandbox, where it turned out to
be answerable in full. **Step 2 is now done, and the answer is Route 2.**

> **Superseded the next day: the answer is Route 1.** Every measurement
> below stands; the ranking does not. I weighed tool naming, install cost
> and version coupling, and never asked where each server's credential
> lives — which is the question that decides it, because a server running
> in the sandbox puts its token in the sandbox. See **§60**.

### 59.1 The Gerrit venv is bound to the host's Python, and the host's is 3.12

§6 item 9, answered **no**: the venv does not survive being mounted into
a sandbox. From the host:

```
python3 -V                        Python 3.12.3
.venv/pyvenv.cfg                  home = /usr/bin
                                  include-system-site-packages = false
                                  version = 3.12.3
                                  executable = /usr/bin/python3.12
.venv/bin/python   -> python3     (relative)
.venv/bin/python3  -> /usr/bin/python3   (ABSOLUTE)
.venv/lib/python3.12/
node -v                           v18.19.1
```

§58.5 had the symlink story half right. `bin/python` is relative, but it
points at `bin/python3`, which is an **absolute** link to
`/usr/bin/python3` — and inside the sandbox that is Python **3.14**
(`/usr/bin/python3 -> python3.14`; the image carries no 3.12 at all, and
`uv python list` shows 3.12 only as "download available"). So under
Route 3 the venv's interpreter is 3.14 reading a `lib/python3.12/site-packages`
built for 3.12, with `include-system-site-packages = false` removing even
the accidental fallback. Every import fails. There is no fix that keeps
the mount read-only: the version is baked into the directory name.

The Node half of Route 3 is clean — `find mcp-phabricator/node_modules
-name '*.node'` is empty, so nothing is compiled against the host's ABI.
One wrinkle in the other direction: `mcp-phabricator/package.json`
declares `engines: {"node": ">=20.0.0"}` and the **host** is v18.19.1, so
the host build is the one running under-spec (npm doesn't enforce
`engines` by default). The sandbox's v22.22.1 is what the package asks
for.

### 59.2 Two bugs of mine, and what they cost

Worth recording as method, not just as errata — this is the third round
of hand-over batches and the failure modes are now clear.

- **Block 9 measured nothing.** I wrote `timeout 5 … | tail -5` and then
  `echo "exit=${PIPESTATUS[0]}"` as a *separate* command, by which point
  `PIPESTATUS` had been reset. Output was a bare `exit=0`. Exit codes
  must be captured in the same compound command as the thing they
  describe: `timeout 5 cmd >out 2>&1; echo exit=$?; tail -5 out`.
- **Block 10's `gerrit` registration never ran.** My `--args "<one long
  comma-separated value>"` was wrapped across lines, so `sbx` got
  `ERROR: flag needs an argument: --args` and bash then tried to execute
  the value as a command. The `--command env --args "KEY=V,…"`
  workaround for §58.2's missing `--env` is therefore **still
  unproven**.
- **Block 11, the decisive one, never ran either** — and that one is on
  the design, not the shell. I asked for
  `--static-mcp phabricator,gerrit`, so the failed `gerrit` registration
  made the whole `sbx create` fail with `400 Bad Request: unknown
  --static-mcp server(s): gerrit`, and blocks 11–13 all printed
  "sandbox 'mcp-probe' not found". `--static-mcp phabricator` alone would
  have answered the question. **A probe must depend on as little as the
  question allows**; coupling two servers into one flag to save a
  sandbox-create cost the round its whole point.

### 59.3 What round 2 did establish, host-side

- **`sbx mcp add --command node --args <path>` works** and is
  host-path-bound: `sbx mcp inspect phabricator` reports
  `Command: node /home/cananian/…/mcp-phabricator/src/index.js` and
  `Resolved: /usr/bin/node`. The registration is a *host* fact, resolved
  against the host's `PATH` — so it also pins Route 1 to the host's
  node v18, the one that is below the package's `engines` floor.
- `sbx mcp ls` → `LOCAL · managed by you · ✓ on`, "1 server · local only".
- **`sbx mcp load <name> --sandbox <sandbox>`** (§58.4, now with its help
  text): the server must already be in the local MCP store, both remote
  and local-stdio are supported, and "Connected agents see the new
  server's tools immediately via the standard MCP `tools/list_changed`
  notification — no agent restart required." That is the repair path
  `wmf-sbx-start`/`wmf-sbx-resume` would have needed under Route 1.
- Incidentals from the `sbx create` resolve output: the shared skills
  store is `~/.local/state/sandboxes/sandboxes/agent-skills →
  /home/agent/.claude/skills · 0 folders` (so Route B's store is real
  and empty), the image is `docker/sandbox-templates:claude-code-docker`,
  and the workspace mounts `rw`.

### 59.4 Route 2, measured in-sandbox: ~6 seconds, and the names are exact

Route 2 ("build the servers inside the sandbox") was ranked last in §4 on
two assumptions: that it needs PyPI egress, and that "every
`wmf-sbx-create` pays the install time". The first is true and cheap; the
second is not true at all.

Both servers clone anonymously from GitLab and build from scratch under
the image's own toolchain:

| step | command | time | size |
| --- | --- | --- | --- |
| gerrit | `uv venv --python 3.14` + `uv pip install -r requirements.txt` | **3.2 s** | 38 MB |
| phabricator | `npm ci --omit=dev` | **3.1 s** | 38 MB |

`requirements.txt` is `uv pip compile`-generated with `--generate-hashes`,
so the install is pinned and verified. `requires-python = ">=3.12"`, and
3.14 satisfies it: the server imports and completes a real stdio
handshake, `serverInfo.name` = `gerrit`, `tools/list` returns all 20 tools
(`get_commit_message`, `query_changes`, …). `mcp-phabricator` likewise:
`serverInfo.name` = `phabricator-mcp-server`, four tools
(`phabricator_get_task`, …).

Then the question §58 called decisive, asked of the mechanism Route 2
actually uses. `claude --mcp-config <file>` merges extra stdio servers
*without* `--strict-mcp-config` clobbering what is already there, so the
naming can be measured without touching `~/.claude.json`:

```
$ echo 'reply with the single word ok' | claude -p \
    --output-format stream-json --verbose --mcp-config probe.json
"mcp_servers":[{"name":"mcp-gateway","status":"connected"},
               {"name":"phabricator","status":"connected"},
               {"name":"gerrit","status":"connected"}]
```

and the `init` event's `tools` array contains, verbatim:

```
mcp__gerrit__get_commit_message        mcp__phabricator__phabricator_get_task
mcp__gerrit__query_changes             mcp__phabricator__phabricator_search_tasks
… 20 in all                            … 4 in all
mcp__mcp-gateway__mcp-exec             (the gateway's five, still there)
```

**These are exactly the names `skills/`, the `gerrit-reviewer` agent and
`bin/session-start.sh` already use**, alongside — not instead of — sbx's
gateway entry. The three naming mechanisms now measured:

| mechanism | the model sees | verdict |
| --- | --- | --- |
| in-sandbox stdio (`claude mcp add --scope user`, `--mcp-config`) | `mcp__gerrit__get_commit_message` | **keeps the names** |
| plugin-root `.mcp.json` (§57.1) | `mcp__plugin_wmf-claude_gerrit__…` | renames |
| sbx MCP gateway (§57.2) | `mcp__mcp-gateway__mcp-exec` | renames, and hides |

The one remaining escape hatch for Route 1 — that `--static-mcp` might
make sbx write per-server entries into `~/.claude.json` instead of the
single gateway entry — is still unmeasured, and no longer matters: even
if it did, Route 1 would still run both servers as host processes with
the host's node 18 and the `--env` wart, under Docker's own "ad-hoc
development only … no sandboxing … your host user's full permissions"
warning (§58.3). Route 2 needs none of that.

### 59.5 So Route 2, and the cost is two domains

What it takes, against what §4 assumed:

- **PyPI in `EXTRA_DOMAINS`.** A generated kit allows 15 domains today
  (12 wiki-family globs plus `github.com`, `packagist.org`,
  `registry.npmjs.org`). `registry.npmjs.org` already covers the Node
  half; the Python half needs `pypi.org` and `files.pythonhosted.org`.
  Confirmed this dev sandbox reaches both (200), and that its policy is
  broader than a kit's — the kit change is still required.
- **No submodule dependency, and nothing extra in the kit.**
  `*.wikimedia.org` is already allowed, so the sandbox can clone
  `gitlab.wikimedia.org/kharlan/gerrit-mcp-server` and
  `gitlab.wikimedia.org/egardner/mcp-phabricator` itself. That drops
  §4's "the sources have to get into the kit" cost *and* Route 1's
  dependency on a host checkout with submodules initialised. Clone at
  the SHA the parent repo pins (the kit generator knows it) so the
  sandbox is not tracking a moving branch.
- **~6 s and 76 MB per sandbox**, in `setup.install`, off the critical
  path of everything else the kit does.

### 59.6 What is left for the host

Nothing that blocks step 4. Two loose ends worth a one-line command each
whenever the host is in front of us:

- `sbx mcp rm phabricator` — round 2 left a host-side registration
  behind, and Route 1 is out, so it should not linger. (It is inert until
  a sandbox is created with `--static-mcp`, but §58.3 is the reason not
  to keep unused ones.)
- For the record only: `sbx create --static-mcp phabricator` +
  `jq '.mcpServers' /home/agent/.claude.json` +
  `wmf-sbx-gateway-tools`, to close §6 item 2 properly.

## 60. Route 1 after all: credentials decide it, node 18 was the bug, and the naming problem has a fix — MEASURED + BUILT 2026-09-12

**cananian's call, and it overrules §59:** if the MCP servers run *in*
the sandbox, then every credential they need is in the sandbox too. That
is a non-starter, whatever the tool names come out as. So Route 1 —
servers on the host, behind sbx's gateway — and the two problems §59
raised against it (the naming, and "our initial attempts at running the
phabricator MCP server were running into issues") get solved rather than
routed around.

Both are now solved. The naming has a fix that is *better* than parity,
and the phabricator failure was exactly what cananian suspected.

### 60.1 The credential argument, with the specifics

Not hypothetical for either server:

- `mcp-phabricator/src/index.js` reads **`PHABRICATOR_API_TOKEN`**
  (alongside `PHABRICATOR_URL`, `PHABRICATOR_USERNAME`,
  `PHABRICATOR_CONTACT_{NAME,EMAIL}`). A Conduit token is a full-account
  credential.
- `gerrit-mcp-server` takes a JSON config at `$GERRIT_CONFIG_PATH` whose
  auth methods are `http_basic` (`username` + `auth_token`),
  **`gitcookies`** (a path, default `~/.gitcookies`), or `gob-curl`.
  `~/.gitcookies` is *push* credentials for all of Gerrit.

Run the server inside the sandbox and the agent can read both: an
environment variable is in `/proc/<pid>/environ` and in `env`, a config
file is a file. Nothing about the sandbox boundary helps — the boundary
is exactly what the credential would be on the wrong side of. §4 had
been ranking routes on tool naming and install cost, which are
engineering problems; this one is the threat model, and it dominates.

Both servers read public data *anonymously* today, so nothing is
currently exposed either way — but a route is not worth adopting if it
breaks the moment someone authenticates, which is the obvious next step
for both (posting a review comment, filing a task).

sbx's proxy-managed secrets are the other way to keep a credential out
of a sandbox, and they do not cover this: they inject per provider at
the network layer, while Conduit takes its token as a POST form field
rather than a header, and Gerrit basic-auth against
`gerrit.wikimedia.org` is not a provider sbx knows. Not pursued.

### 60.2 The phabricator server on node 18: `File is not defined`

Reproduced exactly, and then confirmed from the host's own process. Ran
`mcp-phabricator` under node 18.19.1 (the host's version, downloaded into
this sandbox to match):

```
node_modules/undici/lib/web/webidl/index.js:537
webidl.is.File = webidl.util.MakeTypeAssertion(File)
ReferenceError: File is not defined
Node.js v18.19.1
```

The `File` global arrived in node 20. The dependency floor is not
`mcp-phabricator`'s own `engines: {"node": ">=20.0.0"}` but its
`cheerio` → `undici`, both of which declare **`>=20.18.1`**; `npm` does
not enforce `engines`, so the install succeeded and only the *run*
failed. Under node 22.22.1 (the sbx image's) the same tree starts and
serves its four tools.

So: **upgrade the host's node to ≥20.18.1** — 22 LTS matches the
sandbox, which keeps one less version difference in play. Then
re-register, because `sbx mcp add` resolves the command at registration
time (`sbx mcp inspect phabricator` → `Resolved: /usr/bin/node`); a new
`node` earlier on `PATH` does not retroactively change a stored
registration.

**And note what `sbx mcp ls` claimed while this was broken:**
`phabricator local stdio ✓ ready`. "Ready" means the command path
resolved, not that the server can start. Do not treat it as a health
check.

### 60.3 The gateway mounts a host server *dynamically*, into a
sandbox that already exists

The best thing found today, and it removes a whole layer from Route 1.

`mcp-find` with a **non-empty** query matched nothing for any word tried
(`time`, `fetch`, `git`, `everything`) — which is what §57.2 saw and read
as "the catalog is empty". With an **empty** query:

```json
[{"name": "phabricator", "type": "server"}]
```

That is the host-side registration cananian made in round 2, visible to
a sandbox created days earlier. `tools/call mcp-add {"name":
"phabricator"}` then made the gateway **launch the host process on
demand** — and returned its whole stderr, which is how §60.2's diagnosis
came from the host's own node rather than from my reconstruction of it.

Consequences:

- **`--static-mcp` is optional.** (*Optional, but we want it anyway —
  §61.3 found that static mode deletes `mcp-add` and friends from the
  gateway, which is worth more than the flexibility.*) §58.1 worried that
  the static set "is
  chosen once at creation time" and could not be changed on re-attach,
  which would have forced the flag into `build_sbx_command` and left
  `wmf-sbx-start`/`wmf-sbx-resume` with a gap. Dynamic `mcp-add` needs
  no create-time flag, works on a running sandbox, and needs no
  `sbx mcp load` either.
- **A sandbox created today can use a server registered tomorrow.** No
  recreate, no restart.
- ~~`mcp-find`'s search appears to match on something other than the
  name (both our servers have no description).~~ **Wrong, corrected in
  §62.2:** `mcp-find {"query": "gerrit"}` does match by server name. The
  empty results above were mine: only `phabricator` was registered at the
  time, and none of `time`/`fetch`/`git`/`everything` is its name. The
  empty query still enumerates, and is what to use when you want the
  whole list.

### 60.4 `sbx/bin/wmf-sbx-mcp-proxy`: the names, and a narrowing

Route 1's one real defect is that everything the gateway serves arrives
as `mcp__mcp-gateway__<tool>` (§57.2), while `skills/`, the
`gerrit-reviewer` agent and `bin/session-start.sh` name tools literally.
The fix is a stdio MCP server, in the sandbox, named after the host-side
server it fronts:

```
claude mcp add --scope user phabricator -- \
    wmf-sbx-mcp-proxy phabricator --tools phabricator_get_task,...
```

It speaks JSON-RPC on stdin/stdout to Claude Code and streamable HTTP to
the gateway, `mcp-add`s its server if the gateway offers that (dynamic
mode) or finds it already mounted (`--static-mcp`), drops the five
meta-tools, applies an optional allowlist, and forwards `tools/list` and
`tools/call` unchanged. Because Claude derives the prefix from the config
*entry* name, the tools come out `mcp__phabricator__phabricator_get_task`
— the names the skills already use. **No credential enters the sandbox:
the proxy forwards JSON-RPC, and the real server still runs on the host.**
Python 3, stdlib only, single file, like `wmf-sbx-gateway-tools`.

It is also a *reduction* in what the sandbox's agent can reach, which is
the part worth keeping even if upstream someday fixes the naming. By
default every sbx sandbox hands the model five meta-tools including
`mcp-add` ("Add an MCP server to this session … its tools become
available immediately") and `code-mode` ("Create a sandboxed JavaScript
execution tool"). The model can therefore mount servers and evaluate JS
of its own accord. Measured fix, this sandbox, Claude Code 2.1.269:

| `permissions.deny` entry | gateway status | `mcp__*` tools offered to the model |
| --- | --- | --- |
| *(none)* | connected | the five meta-tools |
| `mcp__mcp-gateway` | **connected** | **none** |

A whole-server deny removes the tools from the session entirely under
`bypassPermissions` — the server still connects, the model simply never
sees it. The proxy is unaffected: the deny applies to Claude's tool
layer, not to our own HTTP calls. So the shipped configuration is
`deny: ["mcp__mcp-gateway"]` plus one proxy entry per server, and the
agent's MCP surface is exactly the tools we listed and nothing else.

Verified so far:

- **29 unit tests** (`sbx/tests/test_wmf_sbx_mcp_proxy.py`) against a
  fake gateway that reproduces SSE framing, the `Mcp-Session-Id`
  handshake, the meta-tools, and a server that appears only after
  `mcp-add`: naming, meta-tool suppression, allowlist filtering (and the
  two-proxies-one-gateway case), call forwarding, refusal of anything not
  advertised, `--no-add` for static mode, plain-JSON as well as SSE
  replies, and a dead gateway degrading to an empty tool list instead of
  killing the session.
- **Live against the real gateway**, as a real Claude Code MCP server:
  `"mcp_servers":[{"name":"mcp-gateway","status":"connected"},{"name":"phabricator","status":"connected"}]`
  with zero `mcp__phabricator__*` tools — correct, because the host-side
  server cannot start on node 18 (§60.2).

The last step — the tools actually arriving as
`mcp__phabricator__phabricator_get_task` — needs a host-side server that
runs. That is §60.5, and nothing else blocks it.

### 60.5 What the host owes, and what it does not

Three commands, then the rest is measurable from in here, because the
gateway exposes host registrations to a running sandbox (§60.3):

```bash
R=/home/cananian/Projects/Wikimedia/wmf-claude
# 1. node >= 20.18.1 (22 LTS preferred), then re-register with its path:
sbx mcp rm phabricator
sbx mcp add phabricator --command env --args \
  "PHABRICATOR_USERNAME=$PHAB_USER,$(command -v node),$R/mcp-phabricator/src/index.js"
# 2. gerrit, and the `env` trick §58.2 needs, on ONE line:
sbx mcp add gerrit --command env --args \
  "PYTHONPATH=$R/gerrit-mcp-server/,PYTHONDONTWRITEBYTECODE=1,$R/gerrit-mcp-server/.venv/bin/python,$R/gerrit-mcp-server/gerrit_mcp_server/main.py,stdio"
```

That also finally proves or disproves the `env`-as-command workaround
for the missing `--env`, on both servers.

Not needed any more: `--static-mcp` (§60.3), a new `wmf-sbx-create` flag,
`sbx mcp load`, or a sandbox recreate.

## 61. Round 3: the host server runs, the names come out right, and `--static-mcp` turns out to be a hardening — MEASURED **[cananian, host, 2026-09-12]** + in-sandbox 2026-09-12

`responses17.txt`. cananian upgraded the host's node and re-registered;
everything §60 predicted then fell out in one pass, plus two findings
neither of us was looking for.

### 61.1 The `env` trick works, and item 8 is closed

```console
$ wmf-sbx mcp add phabricator --command env --args \
    PHABRICATOR_USERNAME=cscott,/home/cananian/.nave/installed/26.8.2/bin/node,`pwd`/mcp-phabricator/src/index.js
MCP server "phabricator" registered (type: local)
$ wmf-sbx mcp inspect phabricator
Command:   env PHABRICATOR_USERNAME=cscott /home/cananian/.nave/installed/26.8.2/bin/node …/src/index.js
Resolved:  /usr/bin/env
```

So `sbx mcp add`'s missing `--env` (§58.2) is a non-problem:
`--command env --args "KEY=V,…,cmd,args"` registers cleanly and resolves
to `/usr/bin/env`. Round 2's `ERROR: flag needs an argument: --args` was
my line continuation, not sbx. **§6 item 8 closed.** The one constraint
worth remembering: `--args` is comma-split, so a value containing a comma
has nowhere to hide.

Node is now 26.8.2 (nave-installed, registered by absolute path, which is
what makes the registration independent of `PATH`). The server starts —
the gateway lists its four tools where it previously returned a node
traceback (§60.2).

### 61.2 The payoff: `mcp__phabricator__phabricator_get_task`, from a
credential that never left the host

Driven in this sandbox against the live gateway, with exactly the
configuration §4 proposes to ship — one proxy entry, plus the
whole-server deny:

```jsonc
// --mcp-config
{"mcpServers": {"phabricator": {"command": ".../sbx/bin/wmf-sbx-mcp-proxy",
  "args": ["phabricator", "--tools", "phabricator_get_task,phabricator_search_tasks"]}}}
// --settings
{"permissions": {"deny": ["mcp__mcp-gateway"]}}
```

`claude -p --output-format stream-json --verbose`, prompt *"Use
phabricator_get_task to fetch T1 and reply with only its title"*:

| | |
| --- | --- |
| `mcp_servers` | `mcp-gateway: connected`, `phabricator: connected` |
| `mcp__*` tools | `mcp__phabricator__phabricator_get_task`, `mcp__phabricator__phabricator_search_tasks` — **and nothing else** |
| `result` | `Get puppet runs into logstash` |
| `permission_denials` | `[]` |

Four things at once, all of them the point:

- **the literal names survive** — these are the names `skills/`, the
  `gerrit-reviewer` agent and `session-start.sh` already write;
- **the allowlist is enforced**: two tools offered out of the four the
  gateway serves, filtered in the sandbox before the model ever sees them;
- **the gateway's five meta-tools are invisible** while its entry is still
  `connected`;
- **a real call returns real data** — T1's title came from the host-side
  node process, through the gateway, through the proxy, with no denial and
  no token anywhere in the sandbox.

Route 1 is now verified end to end for phabricator; §62.5 repeats it for
gerrit. What registering the second server then revealed — that the
gateway's tool namespace is flat and unattributed, so this `--tools`
argument cannot be optional — is §62.

### 61.3 `--static-mcp` *removes* `mcp-add`, `mcp-find` and `mcp-config-set`

In cananian's `mcp-probe`, created `--static-mcp phabricator`, the gateway
serves **six** tools: the server's own four, plus `code-mode` and
`mcp-exec`. The other three meta-tools are simply gone. Compare this
sandbox, which is dynamic: five meta-tools and no server tools until
something calls `mcp-add`.

Two consequences, in opposite directions.

**It answers §6 item 2's last corner, and not in static mode's favour.**
`jq '.mcpServers' ~/.claude.json` inside `mcp-probe` still shows exactly
one entry — the `mcp-gateway` HTTP endpoint — so sbx does *not* write
per-server config entries under `--static-mcp`, and the model would see
`mcp__mcp-gateway__phabricator_get_task`. **The proxy is needed in both
modes**, which is the last thing that could have made it unnecessary.

**But static mode is a genuine hardening**, and this reverses §60.3's
"`--static-mcp` is optional, so skip it". Denying `mcp__mcp-gateway`
hides `mcp-add` from the model; `--static-mcp` means the gateway *has no*
`mcp-add` to call. Removing a capability beats hiding it, and the two
servers are known at kit-generation time, so:

- **generate with `--static-mcp phabricator,gerrit`** and pass `--no-add`
  to each proxy (the proxy tolerates a missing `mcp-add` either way —
  §60.4 — but say so explicitly rather than relying on the fallback);
- **keep the deny too**: `code-mode` and `mcp-exec` survive static mode,
  and `code-mode` is an arbitrary-JS path that no `Bash(...)` rule
  touches.

The cost is the one §58.1 flagged: the static set is fixed at creation.
`sbx mcp load <name> --sandbox <s>` is the documented escape hatch for a
running sandbox (§58.4) — unmeasured *against a static-mode gateway*,
which is the one thing worth checking before leaning on it. If it turns
out not to work there, the fallback is dynamic mode plus the deny, which
§61.2 just measured working.

### 61.4 A second reason for the deny: `mcp-add` duplicates every tool

Noticed by accident, and it is not cosmetic. When the proxy calls
`mcp-add`, the server is mounted into the **sandbox-wide gateway
session** — not into the proxy's private view. So this session's own
`mcp-gateway` entry immediately began offering
`mcp__mcp-gateway__phabricator_get_task` alongside the proxy's
`mcp__phabricator__phabricator_get_task`: two differently-named routes to
one host process, of which the skills name one.

With `deny: ["mcp__mcp-gateway"]` the duplicate is invisible (§61.2
measured exactly that: two tools total, both proxy-named). Without it,
dynamic mode hands the model a second copy of everything the proxy
mounts, and the `--tools` allowlist governs only one of the two copies.
The deny is part of the shipped configuration, not a nicety.

## 62. Both servers up: the gateway's namespace is flat, and that makes `--tools` mandatory — MEASURED **[cananian, host, 2026-09-12]** + in-sandbox 2026-09-12

`responses18.txt` registered gerrit, which put two servers behind one
gateway for the first time — and immediately exposed a defect in the
proxy that no single-server test could have found. This section is the
one that changed the code: `--tools` went from an optional narrowing to a
hard requirement.

### 62.1 Gerrit registered, and the gateway's list is now 26 tools long

The `env` trick from §61.1, second time:

```console
$ wmf-sbx mcp add gerrit --command env --args \
    PYTHONPATH=`pwd`/gerrit-mcp-server,PYTHONDONTWRITEBYTECODE=1,\
`pwd`/gerrit-mcp-server/.venv/bin/python,\
`pwd`/gerrit-mcp-server/gerrit_mcp_server/main.py,stdio
MCP server "gerrit" registered (type: local)
Resolved:  /usr/bin/env
```

Worth noting what this sidesteps: the venv's `bin/python` is named
directly, so no `PATH`, no `source activate`, and §59.1's
"the venv is bound to the host's Python 3.12" is a non-issue — the
interpreter *is* the host's. `PYTHONDONTWRITEBYTECODE=1` keeps the
gateway from littering `__pycache__` in the checkout.

In a fresh `mcp-probe` created `--static-mcp phabricator,gerrit`,
`wmf-sbx-gateway-tools` reports **26 tools**: gerrit's 20, phabricator's
4, and static mode's two survivors `code-mode` and `mcp-exec` (§61.3).
This sandbox, dynamic, reports **29** — the same 24 plus all five
meta-tools.

Gerrit's 20: `abandon_change`, `add_reviewer`,
`changes_submitted_together`, `create_change`, `get_bugs_from_cl`,
`get_change_details`, `get_commit_message`, `get_file_diff`,
`get_most_recent_cl`, `list_change_comments`, `list_change_files`,
`post_review_comment`, `query_changes`,
`query_changes_by_date_and_filters`, `revert_change`,
`revert_submission`, `set_ready_for_review`, `set_topic`,
`set_work_in_progress`, `suggest_reviewers`. Five of those are read-only
and are the five `skills/` and the `gerrit-reviewer` agent actually name
(`get_change_details`, `get_commit_message`, `get_file_diff`,
`list_change_comments`, `list_change_files`); the other fifteen include
`abandon_change`, `post_review_comment` and `create_change`, which write
to Gerrit under the engineer's own credential. That asymmetry is the
argument for the allowlist being a *policy* statement and not just a
naming aid.

### 62.2 There is no tool→server attribution anywhere in the gateway's API

The defect: with both servers mounted, the proxy's old default ("expose
every non-meta tool the gateway serves") would have made
`mcp__gerrit__phabricator_get_task` and
`mcp__phabricator__get_commit_message` real, working tool names. Two
proxies, one flat list, no way to tell whose is whose.

So I looked for attribution in every place it could plausibly live. It is
in none of them:

| where | what came back |
| --- | --- |
| `tools/list` entries | `name`, `description`, `inputSchema`/`outputSchema` — **no `_meta`, no `annotations`**, nothing naming a server |
| `mcp-find {"query": ""}` | `[{"name":"gerrit","type":"server"},{"name":"phabricator","type":"server"}]` — server names, no tools |
| `mcp-find {"query": "gerrit"}` | `[{"name":"gerrit","type":"server"}]` — matches the name, still no tools |
| `mcp-add {"name":"gerrit"}` | `Added "gerrit" with 20 tools.` — a **count**, not names |
| tool names themselves | phabricator's happen to be prefixed; gerrit's are not (`get_commit_message`) — so the convention is the servers', not the gateway's, and cannot be relied on |

(The `mcp-find` row also corrects §60.3, which guessed the search matched
on something other than the name. It matches the name; round 2's empty
results were mine — only `phabricator` was registered then and I searched
`time`, `fetch`, `git`, `everything`.)

The one moment attribution *is* observable is an `mcp-add`: whatever
appears in `tools/list` between the before and after snapshots belongs to
the server just mounted. Which leads straight to:

### 62.3 Mounts are sandbox-wide and outlive the gateway session

Two fresh `initialize` handshakes, different session ids, same listing:

```console
session 1 id='XCWA4MSAKNXB2W3LC6556OSOCV'
  tools: 29 | non-meta: ['abandon_change', 'add_reviewer', …]
session 2 id='CXILLZD2B7QJB7BBVAYLSEF725'
  tools: 29 | non-meta: ['abandon_change', 'add_reviewer', …]
```

`Mcp-Session-Id` is per-connection, but what `mcp-add` mounts is not:
it is sandbox state, and it persists. §61.4 saw one face of this (the
mount showed up in *another* client's tool list); this is the other.

So the `mcp-add` diff is non-empty exactly once per sandbox per server —
the first mount ever. A proxy that discovered its tools that way would
work on the first start after sandbox creation and serve **nothing** on
every start after it. That is a worse failure than serving nothing at
all, because it is intermittent.

### 62.4 What the proxy does now

`sbx/bin/wmf-sbx-mcp-proxy`, three changes:

- **`--tools` is `required=True`.** It is the only thing that can say
  which of a flat, unattributed list belongs to this server, and it
  doubles as the write-tool policy §62.1 argues for.
- **The `mcp-add` diff is demoted to a cross-check.** When it *is*
  available (first mount, dynamic mode) the proxy records what appeared
  and drops any allowlisted name the diff says is not ours, with a log
  line. A typo'd or copy-pasted allowlist that names the other server's
  tool gets caught on that one start; on later starts the allowlist
  stands alone, which is why it has to be right.
- **No allowlist fails closed.** The library-level path (nothing can
  reach it through the CLI) logs "cannot tell which of the gateway's N
  tools belong to *server*. Exposing none." and serves zero tools; calls
  are refused too. `visible_tools()` checks the allowlist *before* the
  diff, deliberately — so even a successful diff does not turn into
  discovery.

`sbx/tests/test_wmf_sbx_mcp_proxy.py` grew to 37 tests over this, the
load-bearing ones being `MultiServerTests` (two proxies, one gateway,
each keeping to its own tools) and `DynamicAttributionTests` (the diff
attributes, the diff is not discovery, and an already-mounted server
still serves its allowlist).

### 62.5 Gerrit, end to end

Same shape as §61.2, same shipped configuration — one proxy entry with
the five read-only tools, plus `deny: ["mcp__mcp-gateway"]`. Prompt:
*"Use get_commit_message on change 1338945. Reply with ONLY the first
line."*

| | |
| --- | --- |
| `mcp_servers` | `mcp-gateway: connected`, `gerrit: connected` |
| `mcp__*` tools | exactly `mcp__gerrit__get_change_details`, `…get_commit_message`, `…get_file_diff`, `…list_change_comments`, `…list_change_files` |
| tool actually called | `mcp__gerrit__get_commit_message` |
| `result` | `Use preferred string type for useBottomSheet` |
| `permission_denials` | `[]` |

Five tools out of the 29 the gateway serves — the 15 write tools and
phabricator's 4 filtered out in the sandbox before the model saw them,
and the credential (`auth_token`/`gitcookies_path`, §60.1) never left the
host. **Route 1 is now verified end to end for both servers**, which
closes the MCP half of design step 4.

## 63. `MW_INSTALL_PATH` moves to the literal host path — DONE 2026-09-12

cananian's TODO: *"MW_INSTALL_PATH is being set to the /home/agent/...
path; it should be set to the /home/cananian/... path."*

It was, and deliberately. §39 gave every writable repo two paths to one
directory — the clone really lives at `/home/agent/<rel>`, and the
literal host path is a bind-mount alias for it — and pointed
`MW_INSTALL_PATH`, `MW_CORE_REPO`, `PARSOID` and `MW_VENDOR_REPO` at the
parallel one *because the alias does not survive `sbx stop`*. The
container's mount namespace is rebuilt from its config, the alias goes,
and the literal path shows sbx's own mirror again.

§40 fixed that in the right place: `--restore` is a `setup.startup` step
that rebuilds the whole layout on every container start. So in steady
state the two paths are equivalent, and the tiebreak becomes which one
everything *else* says — the agent's working directory, the engineer's
own shell, every path in a stack trace or an error message. That is the
literal one. `kit.py` now uses `path` directly rather than
`setup_mod.parallel_path(...) or path`.

Two things this does not fix, both worth writing down rather than
quietly assuming away:

- **wmf-sbx-setup still works in the parallel path.** `core_dest` is
  `dest`, so composer, npm and `composer mw-install:sqlite` all run with
  a cwd of `/home/agent/<rel>`, and whatever they bake into
  `LocalSettings.php` and `vendor/` names that path. Both paths reach the
  same files, so nothing is broken today — but `$IP` and those files now
  disagree on spelling, and anything that compares them (a `realpath()`
  check; a path-keyed cache) would notice. Bind mounts are not symlinks:
  each path is its own real path, so `realpath()` does *not* collapse
  them. Making `core_dest` literal too is a bigger change — it touches
  the recorded layout and the git daemon — and is not obviously right,
  since §39 chose the parallel path for the clone on purpose.
  **Superseded by §63.1**, which does exactly that, and finds that it
  touches neither the layout nor the daemon.
- **The restart window is back in scope.** Between a container start and
  `--restore` finishing, the literal path is whatever sbx mounted there.
  `SECURITY.md` §4 already lists closing that race as an open follow-up
  ("everything that can start a container should wait for the layout, not
  just `wmf-sbx-resume`"); this makes it matter slightly more. The answer
  is still to close the race once, not to spell one directory two ways
  everywhere else.

`test_repo_environment_vars_point_at_the_clone_not_the_host_mirror` is
renamed to `..._use_the_literal_path_the_agent_works_in` and inverted.
The out-of-`host_home` case (`/elsewhere/vendor`) is deliberately kept in
the same test: it has no parallel path at all, so it is unchanged, and
pinning it next to the changed cases is what shows the switch did not
reach it.

### 63.1 `wmf-sbx-setup` works in the literal path too

cananian, on the first caveat above: *"run composer, npm, and the sqlite
install with a cwd of `/home/cananian/<rel>`. We only need to work in
`/home/agent` until the initial git clone and bind mount is complete;
once the bind mount is complete (both during initial setup and during
restore) everything should work in `/home/cananian/<rel>` so references
to `/home/agent` don't get written out unexpectedly."*

That is the right line to draw, and it is sharper than "which path is
nicer": the parallel path is *load-bearing* only while the two paths are
not yet the same directory. `setup_repo` needs it — it is the thing it
clones into, and `mount --bind` needs a real source — and so does
`restore_repo`, for the same reason. After the alias mount is up, every
remaining step is just "run a command in this repo", and there the
choice is free. Anything that bakes a path into a file that outlives the
mounts (`.env`, `LocalSettings.php`, `vendor/composer/*`, the
`extensions/` symlinks) should therefore spell it the way the agent's
`$PWD`, the engineer's shell and `MW_INSTALL_PATH` (§63) already do.

The change is one helper and one local:

```python
def work_path(entry):
    if entry["mode"] == "clone":
        return entry["dest"]
    return entry["literal"]
```

and, in `run_setup`'s repo loop, `work = work_path(entry)` feeding
`configure_remotes`, `clones`, `links`, `core_path` and `parsoid_path`.
`mediawiki_setup`'s `core_dest`/`parsoid_dest` parameters are renamed to
`core_path`/`parsoid_path` to stop the name asserting something untrue,
and `clones` entries become `(literal_path, work_path, reset_remote)` —
`literal_path` stays first, because that is the repo's *identity* (it is
what the plan and the command line name it, and what `--keep` matches).

Mode `"clone"` is the exception because it is the fallback where the
alias did **not** take: the host mirror never moved aside, the literal
path is still it (read-only), and `dest` is the only writable copy. Modes
`"bind"` (a `:ro` opt-out) and `"inplace"` have no clone at all, so their
literal path is the original — which is exactly what they mean.

Three things this turned out **not** to touch, against the guess in §63's
caveat:

- **The recorded layout.** `write_layout` records what `setup_repo` did
  (`literal`, `dest`, `orig`, `mode`), not what ran afterwards.
  `work_path` is derived from an entry, never stored.
- **The git daemon.** It is started with `--base-path=/home/agent
  --export-all` and the host remotes' URLs are built from
  `os.path.relpath(dest, SANDBOX_HOME)`. That has to stay parallel — it
  is how one daemon serves every repo under one base path — and it is
  untouched, because nothing about a fetch URL is a working directory.
- **`--restore`.** It only re-does mounts; it runs no composer, npm or
  installer, and writes no paths into files. So "during restore" needed
  no code change: what makes restore land in the literal path is that
  *setup* wrote the literal path into `.env`, `LocalSettings.php` and the
  symlinks in the first place, and restore puts the alias back under it.

One real behaviour change, worth stating rather than discovering: the
`extensions/<Name>` symlinks now point at
`/home/cananian/.../Extensions/<Name>` instead of `/home/agent/...`. In
the window between a container start and `--restore` finishing, those
resolve to the read-only host mirror rather than to the clone — the same
restart race as §63's second caveat, and with the same answer (close the
race once; `SECURITY.md` §4). Nothing loads the wiki before the startup
step runs.

## 64. Step 3: the plugin ships in the kit, as a marketplace — DONE 2026-09-12

`DESIGN-plugin-integration.md` §5 step 3, Route A. The generated kit now
carries the whole `wmf-claude` plugin, and a sandbox comes up with its 15
skills, 5 agents and the SessionStart hook loaded, with no network fetch,
no GitLab token and no host checkout mounted in.

### The way in is `~/.claude/plugins/`, not `~/.claude/skills/`

`~/.claude/skills` is an sbx virtiofs mount (§57), so a kit's
`files/home/` cannot put anything there — whatever the kit writes is
covered by the mount at boot. `~/.claude/plugins/` is an ordinary
directory, so the marketplace route works: write the marketplace
manifest, write the two state files Claude Code keeps there, and enable
the plugin in `settings.json`.

That means the kit writes three files that are Claude Code **internal
state with no format guarantee** — `known_marketplaces.json`,
`installed_plugins.json`, and `enabledPlugins` in `settings.json`. The
formats are not invented here: `wiring/*.json` is the same content the
nono pack's `json_merge` directives already apply on the host, read and
`$HOME`/`$NOW`-expanded by `kit.plugin_state_files`, so the two ends
cannot drift apart about a format neither of them owns. And because it
*is* unguaranteed, a `setup.startup` step checks the result through the
only supported reader there is (`plugin_check_startup_command`):

```
claude plugin list 2>&1 | grep -q wmf-claude || echo 'error: ...' >&2
```

grepped, not exit-status-tested — `claude plugin list` exits 0 even with
nothing installed (MEASURED). It never fails the start; a sandbox with no
plugin is still a working sandbox, and `report_setup_problems` surfaces
the line.

### Two copies of the tree, not a symlink

On the host, `package.json`'s wiring makes both
`marketplaces/wikimedia/plugins/wmf-claude` and
`cache/wikimedia/wmf-claude/0.1.0` symlinks to the one checkout. A kit
ships files, so `write_plugin_tree` copies the tree to both (a few
hundred KB of markdown). Both are load-bearing and neither is redundant:
the marketplace copy is what `"source": "./plugins/wmf-claude"` resolves
against, so `claude plugin list` can describe the plugin at all; the
cache copy is `installPath`, which is what `${CLAUDE_PLUGIN_ROOT}`
becomes when a skill or the hook actually runs.

What goes in it (`PLUGIN_TREE_DIRS`): `.claude-plugin`, `agents`,
`hooks`, `skills`, `templates` — taken **whole**, so a skill added to the
repo is in the next kit with no list to update. That is deliberate:
`standalone-vuln-audit` was once shipped by the plugin and named by
neither `package.json`'s `artifacts` nor the session-start hook (§56.4),
and a directory cannot drift from itself. `bin/` is the exception and is
*not* taken whole — it is mostly host-side launchers (`bin/claude`,
`bin/launch-docker-broker`) that mean nothing in an sbx sandbox and would
only invite the agent to run them; only `bin/session-start.sh`, the one
file `hooks.json` invokes, comes along, chmod 0755.

`installed-plugin.json` spells `0.1.0` out twice and has no `$VERSION` to
expand, while the cache directory is named from
`.claude-plugin/plugin.json`. `check_state_version` raises if those
disagree, so a manifest bump that misses the wiring fails on the host at
kit-generation time rather than as a failed startup check inside a
sandbox.

### `settings.json` is merged, never dropped

sbx writes its own `~/.claude/settings.json` (`permissions.defaultMode:
bypassPermissions`, `model`, `themeId`, …), so a `files/home/` drop would
replace it wholesale and leave the agent asking for approval it has no
terminal to give (§55.1). Instead the patch travels as **data** —
`files/home/.claude/wmf-sbx-settings.json`, inspectable next to the file
it patches — and `wmf-sbx-setup --settings` deep-merges it in the
sandbox.

Two details of that merge are worth stating:

- **Lists are unioned, not replaced.** The only list patched is
  `permissions.deny`, where replacing would silently drop a deny the
  engineer or a later sbx added.
- **A settings.json that is unreadable or not a JSON object is left
  alone and reported.** These denies are a second layer of defense;
  worth having, never worth throwing away whatever else is in there.

It runs **twice**, which is one job in two places on purpose.
Install-only loses if sbx rewrites `settings.json` on a later container
start; startup-only loses the first session, because startup commands do
not block the `sbx exec` that triggered the start (§46). The merge is
idempotent and writes nothing when nothing changes, so the second run
costs a file read.

The install and startup halves are spelled differently and have to be:
`setup.install`'s `command` is a **shell string** (its neighbours are
`apt-get …`), `setup.startup`'s is an **argv array** run with no shell.
`settings_merge_argv` is the single source both are built from. A decode
error in a v2 spec is fatal, so this is not a stylistic point.

Only `deny` is ported from `wiring/settings-merge.json`. `allow` and
`ask` are redundant under `bypassPermissions`, and `sandbox: {enabled:
false}` turns off *nono's* in-process sandbox and means nothing here —
shipping them would be noise in a file the engineer may well read. Plus
`mcp__mcp-gateway` (§60.4), which step 4 needs and which costs nothing
to add now.

### Measured, on this session's own config

Generated a kit, copied its `files/home/.claude/plugins/` over
`/home/agent/.claude/plugins/`, ran the real `--settings` merge against
this sandbox's real `settings.json`, and asked Claude Code:

```
$ claude plugin list
  ❯ wmf-claude@wikimedia   Version: 0.1.0   Scope: user   Status: ✔ enabled
$ claude plugin details wmf-claude
  Skills (15)  check-nono-update, compare-rebase, init-project, lint,
               manual-test, perf-audit, review-patch, run-tests,
               stage-hunks, standalone-vuln-audit, test-coverage,
               trace-prod-route, vuln-audit, write-commit-msg,
               write-phab-task
  Agents (5)   mediawiki-dev, test-writer, jupyter-notebook,
               mediawiki-explore, gerrit-reviewer
  Hooks (1)    SessionStart  (harness-only — no model context cost)
  Always-on:   ~2,144 tok added to every session
```

Two things fell out of doing it live rather than in a unit test. The
merge kept `defaultMode: bypassPermissions` and `model` untouched while
adding the denies — the §55.1 failure mode, not reproduced. And the
`mcp__mcp-gateway` deny took effect **immediately, mid-session**: the
gateway's 29 tools disappeared from this session's own tool list the
moment the merge landed, and came back when the backup was restored.
That is §60.4 confirmed from the receiving end, and it also says the
deny does not need a restart to bite.

Backup and restore of `~/.claude/{plugins,settings.json}` bracketed the
whole thing; the config is back to exactly what it was.

~2,144 tokens always-on is the price of the plugin in every session. It
is worth knowing before step 4 adds MCP tool schemas on top.

## 65. Step 4: the MCP servers, on the host, behind an allowlisting proxy — DONE 2026-09-12

`DESIGN-plugin-integration.md` §5 step 4, Route 1. Both halves are now
code: the host registers the real servers with `sbx mcp add`, and the
generated kit ships `wmf-sbx-mcp-proxy` plus one `claude mcp add
--scope user` per server, each with its own `--tools` allowlist.

The credential constraint is what picked this shape and it is worth
restating, because every simplification available here gives it up: the
servers authenticate as the engineer, so they run where the engineer's
secrets already are — the host — and the sandbox gets a shim that speaks
to them through sbx's gateway. Nothing the agent can read is a
credential.

### 65.1 The kit side, verified live in this sandbox

Generated a kit for both servers, dropped its
`files/home/.local/bin/wmf-sbx-mcp-proxy` and
`files/home/.claude/wmf-sbx-mcp.json` into place, and ran the step the
spec runs:

```console
$ python3 src/wmf_sbx/setup.py --mcp ~/.claude/wmf-sbx-mcp.json
Added stdio MCP server gerrit with command: …/wmf-sbx-mcp-proxy gerrit --tools … --no-add to user config
Added stdio MCP server phabricator with command: … to user config
$ python3 src/wmf_sbx/setup.py --mcp ~/.claude/wmf-sbx-mcp.json   # again
the gerrit MCP server is already registered
the phabricator MCP server is already registered
$ claude mcp get gerrit
  Status: ✔ Connected
```

Both **connect** — Claude Code started each proxy and got a `tools/list`
back. The second run is a no-op, which it has to be: like the settings
merge, this runs at install *and* on every container start (§46), and a
restart must not churn `~/.claude.json`.

`register_mcp_servers` reads `~/.claude.json`'s `mcpServers` to decide
and mutates only through the CLI. Two reasons, both measured: `claude
mcp get` starts the server to health-check it (too expensive once per
server per start), and `~/.claude.json` also holds the session history,
the onboarding state and sbx's own `mcp-gateway` entry — a botched
rewrite of it is a broken Claude Code, not a missing MCP server. A
changed entry is `remove` then `add`, because `claude mcp add` refuses a
name that is taken (exit 1, MEASURED) and there is no `claude mcp set`.

### 65.2 The allowlist holds at call time, not just in `tools/list`

The thing §62.2 made mandatory, now checked from both ends:

```console
$ … tools/list   (gerrit proxy)
['get_change_details', 'get_commit_message', 'get_file_diff',
 'list_change_comments', 'list_change_files']
$ … tools/call abandon_change   (phabricator proxy)
{"error": {"code": -32603,
           "message": "phabricator does not serve a tool named 'abandon_change'"}}
$ … tools/call phabricator_get_task   (phabricator proxy)
{"result": {"content": [{"type": "text", "text": "MCP error -32602: …"}]}}
```

The gateway serves `abandon_change` — it is gerrit's, and the gateway's
namespace is flat and unattributed — and the proxy refuses it anyway.
That is the cross-server confusion §62.2 found, closed. The third call
is a real round trip: my deliberately wrong argument type was rejected
by the *host's* phabricator process, through the gateway, through the
proxy.

Backup and restore of `~/.claude.json` bracketed all of it; the file is
byte-for-byte what it was, `mcpServers` back to just `mcp-gateway`.

### 65.3 The host side is written but **not measured**

`wmf-sbx-create` now registers the servers before it builds the kit
(`ensure_host_mcp_servers`) and passes `--static-mcp <the ones that
took>` to `sbx create`. None of that can be exercised from inside a
sandbox — there is no host `sbx mcp` in here — so it is unit-tested
against fakes that encode the shapes §61.1/§62.1 measured, and **the
next host run should check it**. Specifically:

- ~~`sbx mcp ls` parsing.~~ **Settled by §67**: `--json` exists, and
  `host_mcp_registrations` uses it, keeping the column parse as a
  fallback for an older sbx. (The guess here — key off the second field
  being `local`/`remote` — did read the real table correctly; it just
  isn't the contract the flag is.)
- The `--command env --args "KEY=V,…,cmd,args"` spelling, which §61.1
  and §62.1 did measure by hand, now built by `host_mcp_plan`.
- `--static-mcp phabricator,gerrit` in `build_sbx_command`. §59.2 is why
  only confirmed-registered names go in it: an unknown one fails the
  whole create with `400 Bad Request: unknown --static-mcp server(s)`.

Everything on this path fails **closed and quietly**: an unbuilt
submodule, a host node below 20.18.1 (§60.2 — and `sbx mcp ls` calls
such a server `✓ ready` anyway, so this is the only place it can be
caught), an unreadable MCP store, or a failed `sbx mcp add` each drop
that server from the kit and print why. The sandbox comes up without the
Phabricator/Gerrit tools, which is a working sandbox; registering a
server that cannot start would be a failed MCP server in every session,
and a `--static-mcp` name sbx does not know fails the create outright.

An existing registration is never overwritten. It may be the engineer's
own, carrying a username, a token or a path this has no business
replacing, and nothing distinguishes theirs from ours.

### 65.4 Two small decisions worth recording

- **The Phabricator username is read, not prompted for.** It comes from
  `claude mcp get phabricator` on the host — the same public output
  `bin/wmf-claude-setup` recalls it from — or `$PHABRICATOR_USERNAME`.
  `wmf-sbx-create` runs non-interactively too often to grow a prompt,
  and the server works without it: it reads a public tracker
  anonymously, and the username is only the default filter for "my
  tasks".
- **The allowlist is hand-written, not derived.** Grepping the plugin
  tree for `mcp__<server>__<tool>` to *build* it would turn a sentence
  like "never call `mcp__gerrit__abandon_change`" into a grant. So
  `MCP_SERVER_TOOLS` is explicit, and a test greps the tree the other
  way round: every tool the plugin names must be in the list. Explicit
  grant, automated coverage check.

## 66. `realpath` was only half-applied: the checkout's own path escaped it — DONE 2026-09-12

§1 established the rule — `~/Wikimedia` is a symlink to
`~/Projects/Wikimedia` on this host, sbx mounts the path you literally
pass, and it does not recreate the symlinks that path went through, so
inside the sandbox the link's own name doesn't exist. Every repo
argument has been `realpath`'d since.

The first host run of step 4 (`wmf-sbx-create --dry-run Cite`, from
`~/Projects/Wikimedia/wmf-claude`) printed the create line as:

```
+ /home/cananian/Wikimedia/wmf-claude/sbx/bin/wmf-sbx create --name sbx-cite ...
```

No `Projects/`. The repo arguments were realpath'd; **the path the code
derives for itself was not**. `create._SBX_ROOT` and `kit.SBX_ROOT` were
`abspath(__file__)`, and `abspath` does not resolve symlinks — so when
`wmf-sbx-create` is reached through the symlinked name (which it is:
that's what's on `PATH`), every path derived from the module's own
location carries the symlink.

That mattered more than cosmetically, because step 4 added
`MCP_REPO_ROOT = dirname(_SBX_ROOT)`: the checkout whose `mcp-phabricator`
and `gerrit-mcp-server` get registered. `sbx mcp add` resolves the
command **once**, at registration time, and the registration outlives the
process — so a symlinked (or, on a host where the two are genuinely
different trees, stale) path gets baked in permanently, and the node/
submodule preflight would have checked the wrong tree.

Fixed by `realpath`ing at the three places the code learns where it is:

- `create._SBX_ROOT` → `WMF_SBX`, `MCP_REPO_ROOT`
- `kit.SBX_ROOT` → `REPO_ROOT`, `DEFAULT_PROFILE_PATH`, `MCP_PROXY_SOURCE`,
  `HELPER_SCRIPT_DIR`, and `STATIC_SETUP_SCRIPT`

and at two more where a *host* path is compared against realpath'd ones:

- `host_home` (`create.main` and `kit.build_plan`/`build_kit_spec`). This
  is what repo directories are made relative to for the parallel tree; a
  symlinked `$HOME` against realpath'd repos would put every repo
  "outside host_home" and silently drop its parallel path.
- `wmf-sbx resolve`'s printed/`--json` path, so it reports what
  `wmf-sbx-create` would actually mount rather than what was typed.

The same `abspath(__file__)` was in all seven `sbx/bin/` entry-point
shims, where it is a straightforward bug independent of mounts: they do
`sys.path.insert(0, dirname(abspath(__file__)) + "/../src")`, so a
`~/.local/bin/wmf-sbx-create` symlinked at the script resolves `../src`
relative to `~/.local/bin` and the import fails outright. Measured:
symlinking the script into `/tmp` and running it works after the change
and could not have before. Installing these on `PATH` by symlink is the
obvious thing to do, so it was worth fixing whether or not anyone had
hit it yet.

Deliberately **not** realpath'd: the `node` that `shutil.which` finds for
the Phabricator registration. That one is a host command, not a mount,
and `/usr/bin/node` → `/etc/alternatives/node` → … is a redirection worth
keeping live; collapsing it would pin the registration to one nvm
version.

The general rule, then: `realpath` every host path that will be
**mounted, stored, or compared** — including the ones the code computes
about itself. `abspath` is not a substitute; it only makes a path
absolute.

## 67. `sbx mcp ls --json` exists, and the host store already holds both servers — MEASURED **[cananian, host, 2026-09-12]**

Two things came out of the second host dry run.

**The §66 fix holds.** The create line now reads
`/home/cananian/Projects/Wikimedia/wmf-claude/sbx/bin/wmf-sbx create …`
— the `Projects/` that the symlink had been eating.

**`sbx mcp ls` takes `--json`.** The table:

```
LOCAL · managed by you · ✓ on

  gerrit        local stdio   ✓ ready
  phabricator   local stdio   ✓ ready

2 servers · local only
```

and the same thing as data:

```json
{
  "gateway": {"name": "LOCAL", "local": true, "operator": "managed by you",
              "decision": "local", "signed_in_as": "cscott"},
  "servers": [
    {"name": "gerrit", "transport": "local stdio", "status": "ready", "type": "local"},
    {"name": "phabricator", "transport": "local stdio", "status": "ready", "type": "local"}
  ]
}
```

`host_mcp_registrations` now asks for `--json` first. The column parse
stays as a fallback for an sbx that predates the flag — and it does read
this table correctly (the decoration has no `local`/`remote` in its
second field, so it falls out) — but a documented key is a contract and
column positions are a layout. Two failure modes are kept apart
deliberately: `--json` exiting non-zero means *try the table*, while
`--json` exiting **zero** with something unparseable means *unreadable*,
which registers nothing. Reading that as "no servers" would re-`add`
over whatever is actually there.

**Superseded by §75.4** (2026-09-14): the table parse is gone. Every
failure here is already safe — unreadable registers nothing — so the
fallback was buying nothing and carrying a column layout to maintain.

**Both servers are already registered on this host**, from
`bin/wmf-claude-setup`. So on cananian's machine the
`ensure_host_mcp_servers` path exercises its never-overwrite branch, not
its add branch: it will find both, add neither, and pass both to
`--static-mcp`. Worth knowing before reading a create's output and
concluding the registration code ran. A host where they are *not* yet
registered is still unmeasured.

## 68. The node floor is a preflight, and it is the one MCP problem that stops the create — DONE 2026-09-12

cananian's host runs node v18.19.1 by default. Registering the
Phabricator server from that shell is the failure §60.2 describes: `sbx
mcp add` succeeds, `sbx mcp ls` says `✓ ready`, and the server dies the
first time a tool is called, inside the sandbox, with a 500 and a
`ReferenceError: File is not defined` somewhere the engineer will never
look. So `host_mcp_plan` now checks `node --version` against a floor
before it builds a command, and `ensure_host_mcp_servers` refuses to
create.

### 68.1 Where the floor comes from

`required_node_version() = max(MIN_NODE_VERSION, declared engines)`.

`MIN_NODE_VERSION = (20, 18, 1)` is the measured floor from §60.2 —
cheerio → undici, two levels below the server. mcp-phabricator's own
`package.json` says `engines: {"node": ">=20.0.0"}`, which is *lower*
and would not have caught v20.0 failing. Reading `engines` instead of
the measured number would therefore reintroduce the bug; reading it *as
well* costs nothing and catches a future submodule bump that raises its
own floor. Hence `max`, and hence the comment on `MIN_NODE_VERSION`
saying so, because the obvious "cleanup" here is to delete the constant.

`declared_node_engine` understands `>=X[.Y[.Z]]` and a bare `X.Y.Z`, and
returns `None` for a range, an `||`, or anything else — a guess it can't
make. Since the value can only raise the floor, failing to parse is
safe, and there is no path by which it lowers one.

### 68.2 Why *this* problem is fatal when the others aren't

Everything else `host_mcp_plan` can complain about — an unbuilt
submodule, a comma in the checkout path, an unreadable MCP store — drops
that server, prints why, and lets the create finish. A sandbox without
the Phabricator tools is a working sandbox; the agent is told they're
absent and uses the web UI.

The node floor is different on three counts:

1. **Nothing else fixes itself and this doesn't.** An unbuilt submodule
   is a state of the checkout: the printed `git submodule update --init`
   fixes it and the *next* create picks the server up with no further
   action. Node is a property of the shell `wmf-sbx-create` was launched
   from, and `sbx mcp add` resolves the command **once**, at
   registration time. A registration made under node 18 stays broken
   after node 22 arrives on PATH; it has to be removed and re-added.
2. **The failure is remote in time and place from its cause.** It
   surfaces as a tool call 500ing inside a sandbox, days later, with
   `✓ ready` in every status output in between.
3. **The fix is the engineer's to choose.** cananian enters a `nave`
   environment and relaunches. Someone else upgrades their system node.
   A third person doesn't want the MCP servers at all. The error names
   all three rather than picking one:

   ```
   error: the sandbox's MCP servers cannot be set up on this host:

     phabricator: /usr/bin/node is v18.19.1, below the v20.18.1 its
     dependencies need (sbx/NOTES.md §60.2)

   This is not something to work around by registering it anyway.
   ...
   Either:
     - put a new enough `node` on PATH and re-run -- activate whatever
       version manager you use (nave, nvm, fnm, asdf, volta ...), or
       upgrade the system node; 22 LTS matches the sandbox image's,
     - or pass --no-mcp to create the sandbox without the Phabricator
       and Gerrit tools ...
   ```

   The message names the resolved `node` path, not just the version,
   because on a host with a version manager "*which* node is this?" is
   the actual question, and the answer is what tells the engineer
   whether their manager's shell is active.

`McpProblem` is a `str` subclass carrying `.server` and `.stop` so this
distinction can be made structurally while every problem still prints as
plain text. `stop` ones become an `McpPreflightError`; `main()` catches
it and returns 1, having printed the block above. No `node` on PATH at
all, and a `node` that won't say its version, are `stop` for the same
reasons.

### 68.3 The bug the filter uncovered

Dropping a server whose command this invocation cannot build was
**already wrong**, independently of the node check, and a test failure
(`['gerrit'] != ['gerrit', 'phabricator']`) is what surfaced it:
`ensure_host_mcp_servers` was passing only the servers *it* had planned
to the kit, so a server already registered on the host was silently
dropped from `--static-mcp` whenever this invocation couldn't build its
command.

That is exactly cananian's host: default node 18, phabricator registered
earlier from a nave shell (§67). Before the fix he'd have lost the
Phabricator tools he already has, quietly. Now:

- the kit gets `sorted(set(plan) | (already & MCP_SERVER_TOOLS))` — an
  existing registration reaches the proxy whether or not this shell
  could have created it. Intersecting with `MCP_SERVER_TOOLS` keeps
  unrelated servers in the engineer's store out of our kit; we have no
  allowlist for those;
- problems about an already-registered server are filtered out before
  `stop` is evaluated, so nothing fails a create over a server nobody
  was about to add.

Both halves follow from one rule: **a registration already in the host's
store carries its own resolved command, and nothing here touches it.**

### 68.4 Not done

An *existing* registration that points at a too-old node is still
undetected. It would mean parsing `sbx mcp inspect phabricator`'s
`Resolved:` line, whose format is unmeasured, and then deciding whether
to offer to remove and re-add someone else's registration. Out of scope
for now; the check covers the case that creates the problem.

## 69. Route 1's host half, live — and `sudo` has its own PATH — MEASURED **[cananian, host, 2026-09-12]** + in-sandbox 2026-09-12

### 69.1 Both node paths behave

§68's preflight, run for real. Under the default node:

```console
$ node --version
v18.19.1
$ wmf-sbx mcp rm phabricator
MCP server "phabricator" removed
$ sbx/bin/wmf-sbx-create --dry-run Cite ; echo "exit-$?"
...
  phabricator: /usr/bin/node is v18.19.1, below the v20.18.1 its dependencies need (sbx/NOTES.md §60.2)
...
exit-1
```

Gerrit is not mentioned — it is still registered, so §68.3's filter
dropped its problems before `stop` was evaluated, exactly as intended.
`--dry-run --no-mcp` exits 0.

Then `nave use latest` (v26.8.2) and the same command prints what it
would run and exits 0. So the preflight is real at `--dry-run`, which is
what makes it a preflight rather than a create-time surprise.

### 69.2 `--command env --args` works on a real host

The spelling §61.1 measured by hand, now built by `host_mcp_plan` and
run by `ensure_host_mcp_servers`:

```console
$ sbx/bin/wmf-sbx-create Cite
+ .../wmf-sbx mcp add phabricator --command env --args PHABRICATOR_USERNAME=cscott,/home/cananian/.nave/installed/26.8.2/bin/node,/home/cananian/Projects/Wikimedia/wmf-claude/mcp-phabricator/src/index.js
Resolving MCP server "phabricator"...
INFO: mcpruntime: resolving local server command
INFO: mcpruntime: local server command resolved
MCP server "phabricator" registered (type: local)
```

§65.3's second and third open items are closed: the `env` command
registers, and `--static-mcp gerrit,phabricator` was accepted. Note the
absolute nave path in the stored registration — §68's point about the
command being resolved once, visible.

### 69.3 …and then the create failed at the last install step

```
✗ python3 /home/agent/wmf-sbx-setup --mcp /home/agent/.claude… (kit=mediawiki-kit, user=0, exit 1)
WARN: mcp gateway teardown
ERROR: failed to apply kit to sandbox
```

Five minutes of work thrown away at the final step, with no reason
shown: `sbx create` collapses an install command to one line, so the
step's own stderr never reached the terminal. That is exactly the §32.1
failure mode the `/var/log/wmf-sbx-setup.log` machinery exists to
prevent — and `--mcp` is one of the two steps deliberately *not* logging
there, on the reasoning that "sbx already collects that step's output".
It collects it and does not show it. See the to-do below.

Reproduced in-sandbox in one command, as root, which is what the install
step is:

```console
$ sudo python3 src/wmf_sbx/setup.py --mcp /tmp/mcp-test.json
sudo: claude: command not found
error: could not register the ztest-gerrit MCP server (exit 1)
```

**`sudo` does not use the target user's PATH.** It replaces PATH with
its own `secure_path` (MEASURED here:
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin`),
and `claude` lives in `/home/agent/.local/bin`. So `as_agent(["claude",
...])` can never find it, while the identical command run *by* the agent
always does. That asymmetry is why §65.1 verified this step and still
missed the bug: §65.1 ran it by hand, as the agent, in a live sandbox.
The install-time run is the only one that is root, and it is the only
one that fails.

`kit.plugin_check_startup_command`'s docstring already said "`claude` is
installed at ~/.local/bin/claude" as its reason for `user: "1000"`. The
same fact needed applying one function over.

Fix: `claude_executable()` — `SANDBOX_CLAUDE_BIN`
(`/home/agent/.local/bin/claude`) when it is executable, else
`shutil.which`, else the bare name for hosts and tests. Not "put a PATH
back through sudo": `sudo -u agent -H sh -lc ...` would work too, but it
adds a login shell (and this repo's CLAUDE.md is emphatic about what a
sourced profile can do to a non-interactive command) to avoid naming a
path we already know.

Verified as root in a sandbox, with `~/.claude.json` backed up and
restored around it: registers in 0.5s, and a second run — as root and as
the agent — says "already registered" and touches nothing.

The alternative fix, `"user": "1000"` on the install step so it matches
its startup twin and never goes near sudo, is also right and was not
taken: the install-step schema's user field is unmeasured from here, and
guessing it wrong fails a create in a *new* way. `claude_executable` is
correct for both callers regardless. Worth revisiting.

Note that the failed create left the host registration behind — `sbx mcp
add phabricator` had already succeeded — so the next create takes the
never-overwrite branch for both servers.

## 70. Fourteen deny rules that denied nothing — MEASURED **[Claude Code 2.1.269, 2026-09-12]**

Every `wmf-sbx-resume` printed the same eight stderr warnings and then a
"Settings Warning" panel listing six more rules, and I had seen them in
responses19.txt and read past them as cosmetic. They are not cosmetic.
**A rule Claude Code rejects is skipped, not enforced** — the file loads,
the rest of it applies, and the rejected line is simply gone. Fourteen
rules in `wiring/settings-merge.json` had been in that state, six of them
in the deny list, i.e. six documented security controls that were inert.
That is worse than not shipping them, because `SECURITY.md` described
them as protection.

Both spellings were wrong for the same underlying reason: I had guessed
the pattern language instead of measuring it.

### 70.1 `:*` is a prefix match, and it may only end the pattern

```
Invalid permission rule "Bash(find:* -exec*)" was skipped: The :*
pattern must be at the end. Move :* to the end for prefix matching, or
use * for wildcard matching
```

So `Bash(cmd:*)` means "any command starting with `cmd`" and nothing may
follow it. `Bash(ssh:*)` and `Bash(git config core.hooksPath:*)` are
fine. The six `find` denies and the eight `find` allows were not.

Measured, with the old rule in place, in a sandbox:

| rule | `find . -exec rm {} \;` |
|---|---|
| `Bash(find:* -exec*)` (old) | **allowed** — the rule was skipped |
| `Bash(find *-exec*)` (new) | denied |

Any `*` that is not the terminating `:*` is an ordinary wildcard matched
against the whole command string. Hence `find *-exec*`. The missing space
is deliberate: `find * -exec*` requires something between `find` and the
predicate, which the path-less form `find -exec rm {} \;` does not have.
Both forms are denied by `find *-exec*`, and `find . -name '*.py'` stays
allowed — all three verified live, by running the commands, not by
reading the file.

### 70.2 Only `Edit(path)` is consulted for file writes

```
Permission deny rule: Write(~/.claude/settings.json) is not matched by
file permission checks — only Edit(path) rules are. Use
Edit(~/.claude/settings.json) instead (Edit rules cover all file-editing
tools).
```

`Edit(path)` is the file-permission vocabulary; it covers Write and every
other file-editing tool. `Write(path)` is not a narrower rule, it is a
no-op. All eight `Write(...)` denies were removed — after asserting that
each one had an `Edit(...)` twin already in the list, so nothing was
relaxed. The eight warnings the engineer saw were the *safe* half of this
bug: the paths were still protected by their twins. The six `find` rules
had no twin.

### 70.3 The validator is free: `claude --settings FILE doctor`

`doctor` parses the settings file and prints an "Invalid settings" block
naming every rule the *running* Claude Code rejected, with the reason. It
makes no API call and needs no session, so it can be run against a
generated patch as a test. Used it on the real `kit.settings_patch()`
output: 47 deny rules, no invalid-settings block.

This is now the authority for rule spelling — not the docs, not my
reading of them. `sbx/tests/test_wmf_sbx_kit.py` additionally guards the
two shapes measured here (`:*` mid-pattern, and a leading `Write(`) so a
regression is caught without a Claude Code binary in the test
environment.

### 70.4 Opportunistic re-measurement: the "Linux glob caveat" is stale

`SECURITY.md` carried a caveat that `Read(**/*.pem)`-style globs did not
fire on Linux. Re-measured on 2.1.269 against `deep/nested/secret.pem`
and `sample.env`: both are denied. The caveat is rewritten as "no longer
reproducible on Claude Code 2.1.269" rather than deleted, since it was
true of some earlier version and the rules it describes are load-bearing.
`bin/wmf-claude-setup`'s Linux warning is left in place for the same
reason.

### 70.5 Noted, not chased

In the sandbox's `settings.json` the last deny entry appears at column 0:

```json
      "Write(~/.claude/skills/**)",
"mcp__mcp-gateway"
```

Our `json.dump` cannot produce that. Something downstream — sbx's own
claude kit, textually — is inserting the gateway deny into the file we
wrote. It works, and the JSON is valid, but it means the file we generate
is not quite the file that loads. Worth knowing before debugging a future
settings mystery.

## 71. The kit's `files/home` drop loses the executable bit — **[cananian, host, 2026-09-12]**

`wmf-sbx-create --no-mcp ~/Wikimedia/JsonCodec` produced a working
sandbox whose every session began:

```
SessionStart:startup hook error
Failed with non-blocking status code: /bin/sh: 1:
/home/agent/.claude/plugins/marketplaces/wikimedia/plugins/wmf-claude/bin/session-start.sh:
Permission denied
```

So the plugin *loaded* — `plugin_check_startup_command` passes, and it is
right to, because `claude plugin list` names the plugin — and its one
hook could not run. The agent silently lost its entire orientation block:
the skill and agent list, the "you are in a sandbox" reminder, the
backend-specific bullets. §64's check answers "did Claude Code find the
plugin", which turns out not to be the same question as "does the plugin
work".

### 71.1 The diagnosis

`write_kit_dir` chmods the file 0755 in the staging tree — that is
asserted by a test that predates this bug
(`PluginTreeTests.test_...`, `os.access(hook, os.X_OK)`), and the staged
kit really does come out 0755. What puts it in the sandbox is sbx's own
`files/home` copy, and the mode did not survive it.

`noexec` was the other candidate and is ruled out: nothing under `/home`
or `/` in an sbx sandbox is mounted `noexec` (checked `/proc/mounts` —
only `/proc`, `/sys`, `/dev/shm`, `/dev/pts`, `/dev/mqueue`, `/run/secrets`
are, all of them irrelevant). Mode is what is left.

Not measured directly inside the failing sandbox, because the fix makes
the measurement itself: `--exec-bits` prints `made <path> executable (was
0644)` per file and a `N restored, M already executable` summary, so the
next create's install log says plainly whether sbx still strips the bit.
If it prints `0 restored`, this section is wrong and the cause is
something else.

**CONFIRMED — [cananian, host, 2026-09-13].** A fresh
`wmf-sbx-create ~/Wikimedia/Extensions/Cite`, then in the sandbox:

```
$ ls -l /home/agent/bin/git-safe-reset
-rw-r--r-- 1 agent agent ... /home/agent/bin/git-safe-reset
```

That file is the clean control, and it was chosen before the result was
known: `write_kit_dir` stages it 0755 (`os.chmod(dest, 0o755)` in
`install_helper_scripts`), sbx drops it into `~/bin`, nothing in the
sandbox touches it afterwards, and it is deliberately **not** in
`exec_bit_paths()` — so our own fix cannot have produced the reading. It
came out 0644. sbx's `files/home` drop strips the executable bit.

Two supporting readings from the same create, both consistent and
neither sufficient on its own:

- the startup step reported `exec bits: 0 restored, 3 already
  executable` — as it should, since the *install* step had already
  restored them an hour earlier in the same sandbox;
- `grep -i -e restored -e 'exec bit' /tmp/create-cite.log` matched
  nothing, although the step ran (67 ms). An install step's stdout and
  stderr do not reach the create log at all, even on success. That is
  the same blind spot §69.3 hit from the other side — there the log
  showed a failure with no output to explain it — and it is now on the
  to-do list in its own right.

### 71.2 Why this was invisible until now

Three near misses, all for the same reason — **nothing that ran the
staged file also ran it the way the sandbox does**:

- the git helpers (`git-safe-reset`, `git-review-check`) are shipped by
  the same drop and *do* work, because `install_helper_scripts` copies
  them onto PATH with `install -m 0755`, which states the mode again. The
  drop's mode never mattered to them. §30 recorded "mode 755" for
  `/usr/local/bin/git-safe-reset` and that was `install`'s doing, not
  sbx's;
- §64 verified the plugin by `cp`ing a staged tree over `~/.claude/plugins`
  *inside* a sandbox — a copy that preserves modes — and then asking
  `claude plugin list`. Neither half exercised the drop;
- `~/.local/bin/wmf-sbx-mcp-proxy` has the same exposure and has never
  been run from a real create: §65.1 ran the *registration* step, which
  only stores a command string, and responses21's live create got as far
  as `✓ --mcp` for the same reason. Claude Code spawns that command with
  no interpreter in front of it, so an unexecutable proxy means every MCP
  server in the sandbox fails to start. Same bug, one session later.

The pattern worth keeping: **a step that verifies its own artifact by a
path the product does not use verifies nothing.** It is §69.3's lesson
(`sudo` has its own PATH) in a different costume.

### 71.3 The fix

`wmf-sbx-setup --exec-bits PATH [PATH ...]`: chmod 0755, one path per
argument, reporting what it changed. `kit.exec_bit_paths()` is the list —
derived from `PLUGIN_EXECUTABLES` × `plugin_tree_dests()` plus the proxy
when the kit ships one — and it is a kit step twice, exactly like the
settings merge and for the same unmeasured reason:

- **install**, inserted *before* the long MediaWiki step rather than
  after it, so a create that dies in composer still leaves a sandbox
  whose hook runs;
- **startup**, because whether sbx re-drops `files/home` on a later
  container start is not something we have measured, and three chmods are
  cheaper than the answer. Root, not the agent (`user: "1000"`): chmod
  needs ownership, and the drop's ownership is sbx's business. The
  startup half can lose a race with the first session (§46: startup steps
  do not block the `sbx exec` that triggered the start), which is why the
  install half is the one that matters.

The anti-drift test is the important one:
`ExecBitTests.test_every_program_the_kit_stages_is_in_the_list` walks a
real staged kit, collects every file with any `x` bit, subtracts the two
helpers `install -m 0755` covers, and asserts the remainder *is*
`exec_bit_paths()`. Ship a new executable and forget the list and that
test fails, rather than a sandbox failing six weeks later.

(The same anti-drift trick is why the two `settings.json` steps and the
two `~/.claude.json` steps of §72.3 are picked out of the spec by the
*file they patch* and not by `--settings`, which both of them pass.)

### 71.4 Collateral: six tests indexed `install[1]`

Inserting one install step broke six `BuildKitSpecTests` at once, all of
them reaching for the setup step by position. They now go through
`setup_install_command(spec)`, which finds it by content (the
`wmf-sbx-setup` step with no `--flag`). Positional indexing into a list
whose whole purpose is to grow is a test that fails for the wrong reason.

The §72.3 step caught three more of the same family a day later —
`len(install) == 5`, `len(startup) == 6`, and a `--settings` match that
now had two hits. Asserting *how many* steps a spec has is the same
mistake as asserting which position one is in: both are assertions about
the list rather than about the step.

### 71.5 Not fixed by this

An **existing** sandbox is not repaired: `/home/agent/wmf-sbx-setup` is a
create-time snapshot and the spec is baked in at create, so neither half
of the new step exists in a sandbox made before it. The one-line manual
repair, from the host, is

```console
$ wmf-sbx exec <name> -- sh -c 'chmod 0755 \
    /home/agent/.claude/plugins/marketplaces/wikimedia/plugins/wmf-claude/bin/session-start.sh \
    /home/agent/.claude/plugins/cache/wikimedia/wmf-claude/*/bin/session-start.sh'
```

plus `/home/agent/.local/bin/wmf-sbx-mcp-proxy` if the sandbox has MCP.
Otherwise: recreate.

## 72. Route 1 end to end at last — three bugs, one of them not ours — MEASURED **[cananian, host, 2026-09-13]**

`wmf-sbx-create ~/Wikimedia/Extensions/Cite` with MCP, then a real
interactive session in it. This is the measurement the previous to-do
list called "the one that would have caught §71 a week earlier", and it
is the first time anything has *spawned* `~/.local/bin/wmf-sbx-mcp-proxy`
rather than storing its command line.

What worked, and is now measured rather than assumed:

```
$ wmf-sbx exec sbx-cite -- /home/agent/.local/bin/claude mcp list
mcp-gateway:  http://mcp-gateway.docker.internal/mcp (HTTP) - ✔ Connected
gerrit:       /home/agent/.local/bin/wmf-sbx-mcp-proxy gerrit --tools … --no-add - ✔ Connected
phabricator:  /home/agent/.local/bin/wmf-sbx-mcp-proxy phabricator --tools … --no-add - ✔ Connected
```

Both proxies are executable (the §71 fix landed), both run, both complete
the gateway handshake, both survive Claude Code's health check, and the
tools appear to the model under their literal names. The registration is
static-mode (`--no-add`), as generated.

Then the session was asked for a Phabricator task, and the three bugs
below fell out of one prompt.

### 72.1 The plugin check asserted a cause it never measured

Both dispatcher runs logged:

```
error: the wmf-claude plugin did not load; `claude plugin list` does not name it.
```

— in a sandbox where the plugin demonstrably *had* loaded. The step ran

```sh
claude plugin list 2>&1 | grep -q wmf-claude || echo 'error: …' >&2
```

with a bare `claude`, whose own docstring two lines above said the binary
lives at `~/.local/bin/claude`. §69.3 is the same bug: `sudo` uses
`secure_path`, not the target user's PATH. And because the `2>&1` fed
`claude: not found` into `grep -q`, "no claude here" and "the plugin is
missing" were the same observation — so the error named a cause the step
had never measured, and named the wrong one.

Fixed: absolute path with a `command -v` fallback, and four outcomes
instead of two — plugin present (silent), plugin absent (quotes what
`plugin list` *did* print), `plugin list` failed (says so, with its
output, and does **not** claim the plugin is missing), no `claude` at all
(says that). Every branch still exits 0; a sandbox with no plugin is
still a working sandbox.

The old test asserted the script *contained* the string
`claude plugin list` — which is exactly why it never noticed. The new
`PluginCheckScriptTests` runs the script for real, four times, against a
stub `claude` on a PATH that holds nothing else, and asserts the four
messages are distinguishable. **A test that greps the command it is
testing tests the spelling, not the behaviour.**

### 72.2 `HTTP 404: session not found`: the proxy never re-handshook

The tool call itself failed, twice:

> The Phabricator MCP tool call failed both times with HTTP 404: session
> not found — this looks like the host-side gateway session for the
> Phabricator MCP server isn't currently established (it's the proxy that
> carries your credentials through to this sandbox).

Two findings in that one paragraph.

**The bug.** `Gateway._post` captured `Mcp-Session-Id` on the first reply
and echoed it on every request forever; `Proxy.ensure_connected`
short-circuits on `self.connected`, so nothing could ever establish a
second session. MCP's streamable HTTP transport says a server that has
dropped a session answers **404**, and that the client's move is to start
a new session with a fresh `initialize` — so the proxy was being told
exactly what to do and had no code path to do it.

Fixed: a `GatewaySessionLost` subclass raised from `_post` on a 404
**that we sent a session id with** (a 404 with no session id is a wrong
URL, not an expiry — and re-handshaking against a wrong URL would loop);
`Gateway.call` answers it by clearing the id, re-running `connect()`, and
retrying the call exactly once. `initialize` is excluded, since it is the
recovery rather than a thing to recover from, and a `_reconnecting` flag
keeps the recovery's own calls out of the retry path.

The reconnect also re-runs the mount-and-list before the retry. Session
expiry normally leaves the mount (§62.3), but a gateway that *restarted*
lost both, and then the retried `tools/call` would name a tool the new
gateway does not serve.

**The surfaced error.** The model that received `HTTP 404: session not
found` reasoned its way to host-side credentials and asked the engineer
to go check them — a diagnosis with nothing behind it, produced because
the error it was handed said nothing about what had actually happened.
The message now says, in the error itself, that this is a gateway-side
session expiry and *not* an authentication failure, and that the
host-side server's credentials never enter the sandbox. An error message
a model will read is part of the interface.

What is *not* explained: why the session went away. An idle timeout is
the guess; the health check's own `tools/list`, seconds after its
`initialize`, worked fine. It is on the to-do list, because if sessions
expire in seconds then re-handshaking on demand is the wrong shape.

### 72.3 The "auto mode" prompt is a Claude Code nudge, not our regression

Every new session opened with:

```
Make auto mode your default permission mode?
  1. Yes, set auto mode as my default permission mode
  2. No, keep bypass permissions
```

Reported as "something in startup is suppressing bypass permissions (this
is a regression)". It is not: option 2 reads *keep* bypass permissions,
which is the tell that bypass is still in force and this is an upsell,
not a mode change. MEASURED in Claude Code 2.1.269:

- the binary's string table holds `hasSeenAutoDefaultNudge`,
  `shouldShowAutoDefaultNudge`, `autoModeOptInDismissed`,
  `autoModeDeclined` — the vocabulary of a one-time nudge, alongside
  `impression`/`decline` telemetry;
- `~/.claude.json` in a sandbox that has been through it holds
  `"hasSeenAutoDefaultNudge": true` at top level, beside
  `hasCompletedOnboarding`, `remoteControlUpsellSeenCount` and the rest
  of that family;
- our `settings.json` merge cannot have caused it anyway:
  `merge_settings` is a **deep** merge, so `permissions.deny` lands
  beside sbx's `permissions.defaultMode` rather than over it.

So it is once per *install* — and every new sandbox is a new install,
which is why it looked like a regression: it arrives on every create,
before the session the create was for can start.

Fixed by seeding that one key. `wmf-sbx-setup --settings` already takes
an optional target, so this is a second patch file through the same deep
merge, aimed at `~/.claude.json`: `{"hasSeenAutoDefaultNudge": true}`,
install-time and startup-time like its neighbours. The merge writes only
when the key is missing, so the read-modify-write window against a
running Claude Code exists on the first start after a create and not on
every start.

Two things worth being explicit about, since this seeds a flag saying a
human saw something they did not:

- **it changes no permission.** Bypass is what the sandbox already runs
  in (`defaultMode: bypassPermissions`, §55.1, because there is no
  terminal to approve anything at) and what the nudge's own "no" keeps.
  Seeding the flag picks the same answer the engineer was going to pick,
  once, instead of every create;
- **it is one line to undo.** Drop `claude_json_patch()`'s entry and the
  prompt comes back.

Not measured: that setting the key actually suppresses the prompt. The
key's name, its neighbours, and `shouldShowAutoDefaultNudge` in the same
binary all say so, but the confirming measurement is a create with the
seed in place — which the "re-run the MCP measurement" to-do will do
anyway.

## 73. Route 1 passes — and the sandbox's own docs say the opposite of the truth — MEASURED **[cananian, host, 2026-09-14]**

A fresh `wmf-sbx-create Cite` with the §72 fixes in the kit, then one
prompt. All three fixes hold:

```
❯ fetch Phabricator task T1 and print its title
  Called phabricator
● T1's title is: "Get puppet runs into logstash" (status: Resolved).
```

That is the measurement the previous to-do said "Route 1 has never
passed": a host-side MCP server, whose credentials the sandbox cannot
see, answering a model's tool call through the proxy. Also measured, in
the same run:

- no `the wmf-claude plugin did not load` line — the plugin check now
  finds `claude` under `sudo`'s `secure_path` (§72.1). The step is
  visible in the startup list as `sh -c c=/home/agent/.local/bin/claude
  [ -x "$c" ] || c=$(co…`;
- no auto-mode prompt. The `~/.claude.json` seed works, which was the one
  thing §72.3 flagged as inferred rather than measured. Two `--settings`
  install steps and two startup steps now run, as designed;
- no 404 reached the model. This note first read that as "the session
  survived, so the re-handshake was never exercised" — which was a guess
  with nothing behind it, and §74 says it was wrong: the gateway's idle
  timeout is 45 seconds, so the session was certainly dead by the time
  the model called a tool, and what the log shows is the recovery
  working silently.

Two things the same log shows that are *not* fixed.

### 73.1 `wmf-sbx-resume` opens a brand-new sandbox with `--continue`

```
+ wmf-sbx run --name sbx-cite -- --continue
No conversation found to continue
ERROR: agent exited with code 1
warning: that looks like a --continue with no conversation to continue;
         forgetting this sandbox's attach flag and starting a fresh session.
```

It recovered — the warning and the retry are ours and they worked — but
a sandbox created seconds earlier has no conversation *by construction*,
so this round trip is avoidable. The attach flag should not be set until
a session has actually run. Filed below rather than fixed here.

### 73.2 The sandbox's global CLAUDE.md tells the agent the opposite of the truth

`sbx` writes its own CLAUDE.md at the top of the workspace tree, and it
is in every session's context alongside ours. Its "Git workspace mode"
section says to choose between two modes with:

```sh
if [ -d /run/sandbox/source ]; then echo "clone mode"; else echo "direct mode"; fi
```

MEASURED in this sandbox: `/run/sandbox/source` does not exist, so the
probe says **direct mode** — and direct mode's description is

> The host working tree is mounted directly into the sandbox, so your
> edits, commits, and branches appear on the host **immediately**. There
> is no separate copy to keep in sync.

which is false here in both halves. We are in neither of sbx's modes:
`wmf-sbx-setup` makes its own clone and bind-mounts it over the host
path (§39), so there *is* a separate copy, and nothing in it reaches the
host until the engineer fetches it over the git daemon.

This is the mechanism behind a failure the engineer has been reporting
for weeks — agents ending a turn with "all changes made in local copies,
waiting for instructions to commit", leaving the work unreachable. It
was being read as a disposition problem. It is a documentation problem:
an agent that has been told its edits are already on the host has no
reason to commit, and is *correctly* reasoning from what it was given.

We cannot edit sbx's file (it is generated into the container at create
time, on the overlay, owned by no repo). What we can do is contradict it
from the one CLAUDE.md we do write, `~/.claude/CLAUDE.md`. Three
additions there:

- **name the other document and say which wins.** Two files disagreeing
  in context is worse than one being wrong: an agent that notices the
  conflict has no way to resolve it. Ours now quotes the "direct mode"
  claim, says it is false here, and says why (this clone was made by
  wmf-sbx, not by sbx's `--clone`);
- **correct the remote name.** sbx's text says the host fetches from
  `sandbox-<name>`; `remotes.remote_name_for` returns the sandbox name
  verbatim, so it is `<name>` with no prefix. And rather than describe
  the name, tell the agent where to *read* it — `$SANDBOX_NAME` or
  `hostname` — because the branch, the directory and the repo name all
  look like plausible guesses and none of them is it;
- **say to end the turn with a commit**, on a branch, even unfinished,
  and to name the branch and commit in the reply. The instruction only
  works if the escape hatch comes with it, so it does: `--amend` or
  `reset --soft HEAD^` next turn. An instruction to commit early that
  does not say how to take it back is asking the model to choose between
  two things it has been told not to do.

The tests assert the corrections, not the prose: that the text names
`/run/sandbox/source` and does not leave "direct mode" standing
unqualified, that it names `$SANDBOX_NAME`, that it never tells anyone
to `fetch sandbox-`, and that the commit instruction ships with
`--amend`. Worth noting the §72.1 lesson cuts the other way for a
document: a CLAUDE.md's strings *are* its behaviour, so matching on them
is testing the thing itself — but only when the assertion is about what
the text tells the reader to do, not about how it is worded.

### 73.3 The gateway's hostname does not resolve in the sandbox at all

Noted while trying to measure the session-expiry question (§72.2), and
worth its own subsection because it is the Route 1 security property
showing through as an operational fact. MEASURED in two sandboxes, one
with MCP attached (`sbx-cite`) and one without (`wmf-claude-sbx`):

```
$ echo $MCP_GATEWAY_URL
http://mcp-gateway.docker.internal/mcp
$ getent hosts mcp-gateway.docker.internal; echo $?
2
```

The name does not resolve **in either**. It is not "a gateway is
attached here and not there": nothing in the sandbox ever resolves it.
`mcp-gateway.docker.internal` is not in `$no_proxy`, so a normal client
hands the whole URL to the sandbox HTTP proxy at
`gateway.docker.internal:3128`, which resolves it outside the sandbox —
and which is also where the `Bearer proxy-managed` sentinel is swapped
for the real credential (`MCP_SENTINEL_TOKEN_NAME=proxy-managed`).

So the gateway is reachable only *through* the proxy, and a client that
bypasses the proxy loses DNS and credentials together. That is the
design working: the sentinel is useless to anything that can't get to
the injection point, and the credential is never in the sandbox to
begin with. `wmf-sbx-mcp-proxy` gets this right by doing nothing special
— plain `urllib.request.urlopen` honours `$http_proxy`.

A first attempt at the probe script set `ProxyHandler({})` to "keep the
measurement clean", which disabled exactly the mechanism under test and
produced `[Errno -5] No address associated with hostname` — while the
same sandbox's Claude session called Phabricator successfully in another
window. **The disproof was one window away from the failure.** Two
hypotheses, and the one this note first wrote down (a gateway attached
in one sandbox and not the other) was contradicted by a `getent` in the
sandbox where MCP demonstrably works.

## 74. The gateway's idle timeout is 45 seconds — MEASURED **[in-sandbox, wmf-claude-sbx, 2026-09-14]**

§72.2 left "why did the gateway drop the session" open and guessed an
idle timeout. It is one, and it is far shorter than the guess assumed.

Probed from inside a sandbox against `$MCP_GATEWAY_URL`, through
`$http_proxy` like any other client (§73.3). Each session is opened, left
alone, and touched exactly **once**, at its own age — probing one session
repeatedly would reset the timer being measured:

```
idle  41s -> ALIVE        idle  46s -> HTTP 404 session not found
idle  43s -> ALIVE        idle  47s -> HTTP 404 session not found
idle  44s -> ALIVE        idle  48s -> HTTP 404 session not found
idle  45s -> ALIVE        idle  49s -> HTTP 404 session not found
```

45 seconds, sharp, with a coarser sweep (5/10/15/20/30/40 alive,
50/60 dead) either side of it and an hour-long run where 60s through
3600s were all dead.

### 74.1 It is a timeout, not an eviction — and the first run could not tell

The first sweep opened five sessions, then a sixth as a control, probed
the control immediately (ALIVE) and the five at increasing ages (all
404). That is the signature of an idle timeout **and** of a gateway that
keeps only its newest session, and the run was built in a way that could
not separate them. Two controls settled it:

- open session A, open session B, probe A **with no idle time at all**:
  both ALIVE. Sessions coexist, so nothing is being evicted;
- open one session, open nothing after it, probe at 75s: 404. So the
  death is idleness, not company.

Worth keeping as a pattern. The first design looked rigorous — one probe
per session, precisely because a probe resets the timer — and was still
confounded, because *every* session was older than the control. A
measurement that rules out one artifact is not thereby clean.

### 74.2 What it explains, and what it means for the fix

45 seconds is shorter than the gap between a Claude Code session
starting its stdio servers and the model reaching for a tool. So:

- **§72.2's 404 is fully explained.** The proxy handshook when Claude
  Code spawned it, survived the health check's `tools/list` seconds
  later, and then sat idle while the engineer typed. The first real tool
  call was minutes past the cutoff. There was nothing unusual about that
  session at all;
- **the recovery runs constantly, not exceptionally.** Any tool call
  more than 45s after the last one pays a re-handshake. §73's "no 404"
  is therefore evidence the recovery *works*, not evidence it was idle.

That raises the question §72.2 flagged — if sessions expire this fast, is
re-handshaking on demand the wrong shape, and should the proxy keep the
session warm? MEASURED, and no:

```
initialize + notifications/initialized:  min 2 / median 2 ms
tools/list on a live session:            min 1 / median 1 ms
```

The gateway is a localhost-ish hop through the sandbox proxy, so the
recovery costs single-digit milliseconds against a tool call that goes
on to hit Phabricator over the network. In static mode there is no
re-mount either: `--no-add` sets `try_add` false, so `_mcp_add` returns
without a call and the reconnect is `initialize` + `tools/list`.

So: lazy re-handshake stays, and no keepalive. A keepalive would trade
2 ms on a call nobody is waiting on for a request every 30 seconds per
proxy process for the life of every sandbox — worse on every axis. Left
alone deliberately, recorded here so it is not "discovered" again.

Two caveats on the number itself. It was measured against
`wmf-claude-sbx`'s gateway, not `sbx-cite`'s — same sbx build, different
instance — and nothing here establishes that 45s is configurable or
stable across sbx versions. The proxy does not depend on the value, only
on 404 meaning what the spec says it means, which is why it is worth
knowing and not worth hard-coding.

## 75. The Phabricator username gets a real stash, and a divergence check — DONE 2026-09-14

`PHABRICATOR_USERNAME=cscott` was hard-wired into what `wmf-sbx-create`
registers, and the to-do above said the reason: there was nowhere else to
read it from. `bin/wmf-claude-setup` prompted for the username, recalled
the previous answer by scraping `claude mcp get phabricator`, and stored
it only in the registration it then made. One installer's registration
doubling as the other installer's config.

### 75.1 The stash went into the parent repo, not sbx

`bin/wmf-claude-setup` now writes `~/.config/wmf-claude/config.json`
(`$XDG_CONFIG_HOME` honoured), with `config_get`/`config_set` helpers over
`jq` — which is already a hard dependency, and `~/.config/wmf-claude/` is
already where `setup.sh` keeps docker-egress overrides. `config_set`
preserves every other key and refuses to write over a file that is not
valid JSON rather than discarding what a future version put there.

Deliberately in the parent repo and in a commit that does not mention
sbx: the checkout installer prompting for an answer and then having
nowhere to keep it is a wart of that installer, upstreamable on its own.
sbx is just the second reader.

Nothing in that file is a credential — it is the answers to
`wmf-claude-setup`'s prompts, all of which are public — so it is plain
JSON with no special mode, and the README says so, because a file
under `~/.config` that *looks* like it might hold a token gets treated
like one.

`create.phabricator_username` reads three sources, most explicit first:
`$PHABRICATOR_USERNAME`, the config file, then the old `claude mcp get`
scrape — kept for a host whose last install predates the file. Every way
of failing to read the config (absent, unreadable, not JSON, not an
object, not a string, blank) is "no answer stored": a create must not die
over a file it does not own.

### 75.2 The check that closes the other half

The divergence gap the to-do named is the nastier one, because it has no
symptom. `sbx mcp add` has no update-in-place, and
`ensure_host_mcp_servers` deliberately never overwrites an existing
registration — so an engineer who changes their Phabricator username
keeps getting sandboxes that filter "my tasks" by the old one. The tool
answers. It just answers about somebody else.

So when `phabricator` is already registered, the create now reads the
username back out of it and compares. The read is `wmf-sbx mcp inspect
phabricator --json` — **nearly every `sbx` subcommand takes `--json`**
(cananian, 2026-09-14), and this one hands back the stored argv as a
list:

```json
{
  "name": "phabricator",
  "type": "local",
  "command": [
    "env",
    "PHABRICATOR_USERNAME=cscott",
    "/home/cananian/.nave/installed/26.8.2/bin/node",
    "/home/cananian/Projects/Wikimedia/wmf-claude/mcp-phabricator/src/index.js"
  ],
  "requires_oauth": false,
  "resolved_command": "/usr/bin/env"
}
```

One element of `command` either is the assignment or is not. The first
cut of this scraped `PHABRICATOR_USERNAME=([^\s,"']+)` out of the human
`Command:` line §61.1 recorded, which works and is still the wrong thing
to do: it parses a layout, and it cannot tell an argument from a path
that happens to contain the string.

Unlike `host_mcp_registrations`, which keeps a table parse for an sbx
that predates `mcp ls --json`, there is **no** text fallback here. That
one needs an answer — reading "no servers" when there are some would
re-`add` over them. This one is free to fail: not knowing the registered
username means not stopping the create, which is where it started.

A mismatch is a `stop` problem, with its own advice block rather than the
node floor's — `McpProblem` grew an `advice` field and
`mcp_preflight_advice` prints the blocking problem's own, falling back to
`NODE_FLOOR_ADVICE`.

**It stops; it does not fix.** Re-registering means `wmf-sbx mcp rm
phabricator` first, and that server belongs to the host: removing it cuts
the Phabricator tools out from under every sandbox holding a session
against it, to fix something that has been quietly true since the day the
username changed. Worth an interruption, not worth a surprise. So the
create prints the command and stops, and the engineer runs it when no
session needs it. (`rm`, not `remove` — §61.1's spelling.)

Four things are explicitly *not* a mismatch, because none is evidence of
disagreement and a create must not stop on the absence of evidence: an
sbx whose `mcp inspect` has no `--json`, output that does not parse, a
registration that names no `PHABRICATOR_USERNAME` at all (an engineer's
own wrapper may pass it another way), and a host that has no username of
its own to compare.

That left the rest of the human-output parsing to re-check; §75.4 does
it.

### 75.4 Everything else that reads `sbx`'s output, swept — MEASURED **[cananian, host, 2026-09-14]**

**`sbx ls --json`** exists too, and carries rather more than the names:

```json
{"sandboxes": [
  {"name": "sbx-cite", "id": "06f806a4-…", "agent": "claude",
   "status": "stopped",
   "workspaces": ["/home/cananian/Projects/Wikimedia/Extensions/Cite",
                  "/home/cananian/Projects/Wikimedia/core:ro", …]},
  {"name": "wmf-claude-sbx", "id": "8178072c-…", "agent": "claude",
   "status": "running",
   "ports": [{"host_ip": "127.0.0.1", "host_port": 32773,
              "sandbox_port": 9977, "protocol": "tcp4"}],
   "workspaces": [ … ]}
]}
```

`existing_sandbox_names` now reads that instead of splitting `sbx ls
-q`'s one-name-per-line output. Same `stdin=DEVNULL` (§48 — that call is
the one that hung), same "unreadable means an empty set", since `sbx
create` is the final authority on a name collision either way. Note the
`:ro` suffix in `workspaces` and the whole `ports` array: if anything
here ever needs to ask what a sandbox has mounted or where its daemon is
published without going through our own state file, this is where the
answer already is.

**The `sbx mcp ls` table fallback is deleted.** It was there for an sbx
predating `mcp ls --json`, and it was debt: a column layout to keep
working, bought with nothing, because every failure on this path is
already safe — `None` means unreadable, which registers nothing. The
test that used to assert the table parsed now asserts the opposite, that
a table-shaped stdout reads as unreadable, and that only one command
runs.

**`sbx exec` has no `--json`** (cananian), so `setup_status` keeps
finding the outermost braces in its output (§41). That one is not our
JSON being reformatted anyway — it is the status file's contents, with
sbx's `Sandbox NAME started successfully` chrome around it.

So the rule for this codebase, now that it holds everywhere it can:
**ask `sbx` for `--json` and parse that, or don't ask.** The one
remaining text parse is `sbx exec`'s, and it exists because there is no
alternative.

### 75.3 The tests were reading the developer's own config

Worth recording as a hazard rather than a bug fixed. `HostMcpTests` had
no `$XDG_CONFIG_HOME` of its own, which did not matter while the config
file did not exist — and the moment it did, those tests would read
whatever the developer's real `~/.config/wmf-claude/config.json` said and
pass or fail by whose machine ran them. The class now points
`$XDG_CONFIG_HOME` at an empty directory in `setUp` and drops
`$PHABRICATOR_USERNAME`. **Adding a config file is adding a way for the
suite to read the host**, and the failure it buys is one that reproduces
nowhere.

### 75.5 The divergence check, driven on the host — MEASURED **[cananian, host, 2026-09-14]**

`responses26.txt`, six steps, all six as designed.

**It fires, and it fires on the right condition.** The mismatch was
forced three ways — `$PHABRICATOR_USERNAME` for one command, the config
file edited under it, and a genuinely re-registered server — and each
stopped the create with exit 1 and the advice block. The control matters
more than any of them: the identical `--dry-run` with nothing overridden
reached `+ (would register on the host: gerrit, phabricator)` and exited
0. Same repo, same dependency walk, one variable. Without it, three
exit-1s prove only that a dry-run can fail.

**Both directions read correctly.** Step 6 was the real shape — the
registration stale at `not-cscott`, the host current at `cscott` — and
the advice adapted: "If `not-cscott` was right after all, put it back
instead". That is the direction that actually happens, and the one the
unit tests exercise least directly, since they mostly move the host's
answer rather than the registration's.

**The legacy scrape earned its keep, on a real host.** Step 5 had to run
`bin/wmf-claude-setup` first, because `~/.config/wmf-claude/config.json`
did not exist yet — and the prompt still offered `Phabricator username
[cscott]:`. With no config file, that default came from the
`claude mcp get phabricator` scrape: §75.1's fallback, recalling from the
old source and storing to the new one in a single run (`+ saved to
/home/cananian/.config/wmf-claude/config.json`). A host upgrading into
the stash keeps its answer, measured rather than assumed.

**A wart in the exercise, not the code.** Step 6's restore depends on
`/tmp/phab-registration.json` and on re-`add`ing from a nave-active
shell: the stored command carries an absolute
`/home/cananian/.nave/installed/26.8.2/bin/node`, which `shutil.which`
only reproduces if the version manager is active (§68). Anything that
tells an engineer to `mcp rm` should hand them the exact argv back, not
a recipe for reconstructing it. Worth remembering if the advice block
ever grows a "and here is how to put it back" line.

## 76. `SECURITY.md` catches up with the MCP work — DONE 2026-09-14

`DESIGN-plugin-integration.md` step 5's remainder, closed. Three parts,
and only one of them was writing:

**Two sections asserted something that had stopped being true.** §7.4 was
titled "…and one half is unmeasured" and said the host side "cannot be
exercised from inside a sandbox… 'fails closed' is a code reading, not an
observation, until someone runs `wmf-sbx-create` on a host". Someone did,
four times (§67, §69, §72–73, §75.5), and §10's first bullet repeated the
same claim. Both now say what was measured and name the runs. The honest
residue is stated rather than dropped: of the four fail-closed paths, only
the node floor has actually been tripped in anger, and no tool call has
gone through a *generated* kit's gerrit entry.

Also in §7.4, the "one deliberate non-action" paragraph — an existing
registration is never touched, a stale one stays stale, `mcp inspect` is
how you notice — **understated the code as of §75**. A stale username no
longer waits to be noticed; it stops the create. The paragraph keeps the
non-action as the rule and names the read as its one exception, with the
reason the fix is not automatic: `mcp rm` cuts every sandbox holding the
server open.

**§7.5 is new: the residual risk of a proxy-managed secret.** "Credentials
never enter the sandbox" is true and is not the whole story, and the whole
story had only ever been written in NOTES. The sandbox holds
`Bearer proxy-managed`; the value is unreadable, uncopyable,
unexfiltratable. What it cannot stop is *spending*: the injection point
authenticates the sandbox, not the caller inside it, so a compromised
dependency in a test run reaches the gateway with the same authority the
model has. Two things narrow it, both measured rather than assumed — the
per-server allowlist is enforced in the sandbox before the request is made
(§65.2), so the authority on offer is read-only even to a caller that
skips Claude Code entirely; and §73.3's finding that
`mcp-gateway.docker.internal` resolves in no sandbox at all, so bypassing
the proxy to dodge the injection point loses DNS in the same move. **DNS
and credential fail together.** That is the sentence §7.5 exists to carry.

**Route B's shared skills store is moot, not owed.** Step 5 listed it as a
cross-sandbox trust boundary to document. Step 3 shipped Route A — the
plugin tree copied into each kit — so there is no shared store, and a
boundary section about a thing that does not exist is worse than no
section. Recorded here so it is not re-added from the design doc's list
without someone noticing Route A won.

**Correction, same day**: that last paragraph was wrong, and §77.2 has
the measurement that says so. The store is real, shared and mounted into
every sandbox; Route A means we do not *use* it, not that it is not
there.

## 77. Gerrit through a generated kit, and the store I said did not exist — MEASURED **[cananian, host + gerrit-probe, 2026-09-14]**

`responses27.txt`. A fresh `wmf-sbx-create Vector --name gerrit-probe`,
then the three probes. All three pass, and the create output carries a
fourth thing nobody asked for.

### 77.1 The gerrit half of a generated kit, closed

The gap §76 split out of the integration to-do. What a *generated* kit
writes, read back inside the sandbox:

```console
$ jq -r '.mcpServers.gerrit.args | join(" ")' ~/.claude.json
gerrit --tools get_change_details,get_commit_message,get_file_diff,\
list_change_comments,list_change_files --no-add
```

Exactly `MCP_SERVER_TOOLS["gerrit"]`, in order, plus `--no-add`. That is
the list §62.5 typed by hand, now produced by `wmf-sbx-setup --mcp`, and
the first time the two have been compared.

Both ends hold against it:

```console
$ … tools/call abandon_change   (gerrit proxy, in-sandbox)
{"id":2,"error":{"code":-32603,
  "message":"gerrit does not serve a tool named 'abandon_change'"}}
```

and the model's call goes through — `get_commit_message` on change
1338945 → `Use preferred string type for useBottomSheet`, the same
string §62.5 got, now with the entry generated rather than typed. Note
the refusal is the *gerrit* proxy refusing a *gerrit* write tool; §65.2
measured the cross-server case (phabricator's proxy refusing gerrit's
tool). Both directions of the allowlist are now observed.

`--static-mcp gerrit,phabricator` on the `sbx create` line, nine install
commands, ten startup commands, no plugin-load warning, no auto-mode
prompt. The proxy names its gateway session after the sandbox
(`mcp-gateway-gerrit-probe`).

**Unprompted, and worth more than the probes**: asked "what MCP tools do
you have available", the session listed gerrit's five and phabricator's
four and said of the third — *"mcp-gateway: listed as configured, but I
haven't seen its specific tool names surface yet."* The deny works at the
tool layer, not the config layer: the model can still read the entry's
*name* out of `~/.claude.json` and knows a gateway is there. That is the
design (§7.3: a deny hides tools, it does not unregister a server), and
it is the first time we have seen what the model makes of it. It did not
try to reach it.

### 77.2 §76 was wrong about the skills store: it exists, and it is shared

The create's own resolve output, which I had not thought to read as a
security fact:

```
skills  /home/cananian/.local/state/sandboxes/sandboxes/agent-skills
        → /home/agent/.claude/skills · 0 folders
```

Not per-sandbox. One host directory, symlinked into *every* sandbox's
`~/.claude/skills`, mounted `rw` (§55.1: `none
/home/agent/.claude/skills virtiofs rw,nosuid,nodev`). §59.3 had already
written down "Route B's store is real and empty" as an incidental; §76
then reasoned from "we shipped Route A" to "there is no shared store to
draw a boundary around", which does not follow. Route A means nothing of
*ours* goes in it. The directory is there either way.

So the cross-sandbox trust boundary `DESIGN-plugin-integration.md` step 5
asked for is not moot, and it is not conditional on ever shipping Route
B. If the agent in one sandbox can create a file under
`~/.claude/skills`, every other sandbox on this host loads it at the next
start — a skill is instructions to a model, so this is a write channel
from a compromised sandbox into a clean one, and it survives `sbx rm` of
the sandbox that wrote it (§4: the store is host state, not container
state).

**Not yet measured: whether the agent can write there.** `rw` in
`/proc/mounts` is the mount option, not the answer — ownership and mode
inside the sandbox decide it, and virtiofs can present a directory the
container user cannot create in. One command settles it, and it is in
the to-do list below rather than guessed at here. Both outcomes are worth
knowing: writable is a boundary to document and probably to close;
read-only is a mitigation we did not know we had.

The lesson for these notes: **"we don't use it" and "it isn't there" are
different sentences**, and I collapsed them while writing a security
document, which is the worst place to do it.

### 77.3 It is writable, and the deny that covers it was already there

MEASURED, in `gerrit-probe`, by attempting the write rather than reading
the mode:

```console
agent@gerrit-probe:~$ touch ~/.claude/skills/.probe && echo WRITABLE
WRITABLE
```

So the channel is open at the filesystem layer. Two things then matter
more than the finding itself.

**The mitigation already ships, and it is not a boundary.**
`wiring/settings-merge.json` has carried `Edit(~/.claude/skills/**)`
since the nono pack, `settings_patch()` ports the whole `deny` list into
every generated kit, and §70.2 is why the rule is spelled `Edit(` and not
`Write(`. So the model's file-editing tools are already refused there —
without anyone having thought about this particular threat, which is the
argument for keeping deny lists broad. What it does **not** cover is
`bash -c 'echo … > ~/.claude/skills/evil.md'`: a Bash deny is a prefix
match on a command string (§70.1) and anything that can run a shell can
spell the same write a hundred other ways. And the agent has passwordless
root (`SECURITY.md` §8), so it could remount or chmod its way past
anything we set from in here anyway.

**Which means no in-sandbox control can close this.** Every lever we own
— the settings deny, file modes, a mount option — is on the wrong side of
the boundary, because the thing we are defending against is a compromised
agent that is root in that sandbox. The deny is worth keeping as a guard
against the *model* being talked into it by a prompt injection, which is
the likelier failure by a wide margin. It is not worth writing down as
the mitigation, because a real attacker steps over it.

The fix is host-side and not ours: `agent-skills` mounted `ro` into
sandboxes that have no reason to write it, or made per-sandbox. That is
an upstream question (`SECURITY.md` §10), and it is a good one — sbx
gives each sandbox its own everything else, and then hands them all one
shared mutable directory whose contents are instructions to a model.

### 77.4 …and it is one directory, seen from two sandboxes

The inference §77.3 left open, closed the same afternoon, and closed by
accident — which is the part worth keeping.

`touch ~/.claude/skills/.probe` in `gerrit-probe` (cananian, a second run
of the probe without the `rm`). Then, in `wmf-claude-sbx`, a different
sandbox entirely:

```console
agent@wmf-claude-sbx:~$ stat -c '%n  mtime=%y' ~/.claude/skills/.*
.crossprobe-from-wmf-claude-sbx  mtime=2026-09-14 13:13:24  ← written here
.probe                           mtime=2026-09-14 13:12:27  ← written in gerrit-probe
```

Written in one sandbox, read in another, 57 seconds apart. Not a shared
*kind* of directory — the same directory.

Then the cleanup did the rest of the work: `rm -f ~/.claude/skills/.probe`
**from `wmf-claude-sbx`** removed `gerrit-probe`'s file, exit 0. So the
channel is not append-only. A sandbox can plant a skill in every other
sandbox on the host, and it can equally **replace or delete** the skills
another sandbox relies on — silently, with no signal on the other side.
That is worse than the write channel §7.6 was written about, and it came
out of tidying up rather than out of a probe designed to find it.

**The trap that nearly hid all of this**: `ls ~/.claude/skills/` printed
nothing, because every probe file starts with a dot. An empty `ls` was
about to be read as "the store is not shared". It means "no non-hidden
entries", and a dotfile is exactly what someone planting a skill would
use. Use `ls -la` on any directory whose emptiness is load-bearing.

## 78. `sbx settings list --json`: no skills knob, but the SSH one at last — MEASURED **[cananian, host, 2026-09-14]**

`responses28.txt`, 29 settings. Read looking for something that would let
us close §77's shared skills store from the host.

**There is nothing for it in `settings`.** No key mentions skills,
`agent-skills`, or per-sandbox agent state; the closest thing to a mount
control is `ssh.workspaceRoot`, which is about SSH auto-created
sandboxes. Same for §3's writable primary workspace: no knob. Two of our
three standing "ask upstream" items are unreachable from the *config*
surface, which is worth knowing — it means they are not defaults we
failed to read.

> **Corrected 2026-09-14 (§79.4): that is a claim about one surface, and
> I wrote it as a claim about all of them.** `sbx settings` is not the
> only place a knob can live, and the upstream issue (#506) refers to an
> existing **`--no-share-skills`** option — a *create-time flag*, which
> this survey could not have seen and which I never went looking for. The
> conclusion "so §7.6 stays an upstream ask rather than a setting we had
> not found" did not follow from what was measured, and the original
> sentence has been narrowed above. This is §77.2's mistake a second
> time, one level up: **check the surface you actually searched, and say
> which one it was.**

Three findings that were not what I was looking for:

### 78.1 `ssh.agentForwardingEnabled` — §2's second layer, named

```
ssh.agentForwardingEnabled  bool    default true   requires_restart
ssh.agentSocketPath         string  default ""     requires_restart
```

§2 has said "adopt it as soon as the setting name is known" since 0.42.0
shipped. It is known. The host is at the default — `true` — so agent
forwarding is enabled daemon-wide today and `wmf-sbx`'s `unset
SSH_AUTH_SOCK` is the only thing between a bare `sbx` typed out of habit
and the engineer's Gerrit keys. Nothing in this project forwards an agent
deliberately, so the setting costs us nothing.

`requires_restart` cuts our way for once: §2's complaint is that
forwarding is sticky to the *daemon* and any subcommand can start it
implicitly, which is exactly why a daemon-level setting covers the cases
the wrapper cannot (a `sbx` invoked by something that is not our alias).
Both layers stay: a settings key that a `settings set` can silently
revert is not a boundary either.

**Set** the same day (cananian):

```console
$ wmf-sbx settings set ssh.agentForwardingEnabled false
Setting "ssh.agentForwardingEnabled" updated: value=false source=override
This setting takes effect only after a daemon restart: run `sbx daemon restart`.
```

Two things about that last line, both from cananian.

**The tool suggests a daemon restart without saying what one costs.** §8
measured it and `sbx/bin/wmf-sbx`'s header carries it: a daemon stop
kills every running sandbox without letting it save state — every live
Claude session on the machine, not just the one in the repo you happen to
be sitting in. `SECURITY.md` was the file that had just told a reader to
restart, and it did not carry the warning; it does now. **Anywhere we
repeat a suggestion to restart the daemon, the warning goes with it.**
That is a general rule for this repo, not a note about this setting: we
inherit sbx's suggestions when we quote them, including the ones that
omit their own cost.

**And no restart was needed here**, which the tool's message cannot know.
The running daemon was started through `wmf-sbx`, so `SSH_AUTH_SOCK` was
never in its environment and it has no agent to forward; the restart
would re-establish a property it already has. Any daemon started *after*
the change drops forwarding on its own. So the current daemon is covered
by the wrapper and every future one by the setting, with no window
between them and nothing to restart. The restart is only required when
the running daemon is already tainted — which is §8's recovery procedure,
not this.

### 78.2 One setting on this host is not at its default

```json
{"key": "claude.remoteControl", "source": "override", "value": true,
 "default": false,
 "description": "Allow Claude Code's /remote-control channel to
   authenticate with its own session token instead of the host
   credential."}
```

Every other key reads `"source": "default"`. This one was set
deliberately by someone, and this file has never recorded it. Not
flagged as a problem — authenticating a channel with a session token
*instead of* the host credential is plausibly the safer of the two, and
Remote Control is a real feature someone may want — but a security
document that describes the host's posture should not be silently wrong
about the one place the host departs from stock. Asked; not yet answered.

**Answered the same day: cananian set it, deliberately.** So it is a
known deviation, not a surprise, and it stays. What it buys is Remote
Control — following a session from another device — and what it changes
is which credential authenticates that channel: the session's own token
rather than the host credential. Recorded in `SECURITY.md` §2 as the one
place this host departs from stock, with the direction of the trade
named: it narrows what the channel can authenticate as, and it widens
who can reach a running session to anyone holding that session token.
Nothing in this project depends on it either way.

### 78.3 Kit generation has two undocumented dependencies

`wmf-sbx-create` builds a kit in a temp directory and passes `--kit
/tmp/wmf-sbx-kit-…`. That works because of two defaults nobody chose:

```
kit.allowLocalKits    bool  true   "Allow installing kits from local
                                    directories or ZIP files. Set to
                                    false to require a remote source."
kit.requireSignature  bool  false  "Require a valid signature from a
                                    trusted signer (kit.trustedSigners)
                                    before installing any kit."
kit.trustedSigners    json  [{"identityRegexp": "^.*@docker\\.com$", …}]
```

Flip either one and every `wmf-sbx-create` on this machine stops working
— `allowLocalKits: false` refuses a local directory outright, and
`requireSignature: true` wants a signature from a `*@docker.com`
identity, which our generated kits will never have. An engineer at an org
that hardens these centrally would hit it, and the failure would arrive
as an sbx error with nothing in our docs to connect it to. Recorded here
so it is one search away.

The security reading is the mirror image and worth stating: the reason
our kit generation works at all is that sbx will install an unsigned kit
from any local path, and a kit's `install` and `startup` commands run as
root in the sandbox. That is the engineer's own machine writing its own
kit, so it is not a boundary crossing — but it is why `--kit` paths
should never be world-writable, and `wmf-sbx-create`'s use of
`tempfile.mkdtemp` (0700) rather than a fixed `/tmp` name is load-bearing
for a reason it was probably not chosen for.

### 78.4 The proxy knobs that §7.5 quietly depends on

```
no_proxy.sandbox  string  ""   "No-proxy exception list for sandbox
                                egress only (overrides no_proxy)."
proxy.sandbox     string  ""   "Upstream proxy for sandbox egress only."
```

§7.5's whole argument is that the gateway is reachable only *through* the
sandbox HTTP proxy, which is where `Bearer proxy-managed` becomes a real
credential — so a client that bypasses the proxy loses DNS and
credentials together (§73.3). That holds because `no_proxy.sandbox` is
empty. Anything added to it is an exception to credential injection for
those hosts, and `mcp-gateway.docker.internal` in particular would not
open a hole so much as break MCP outright (the name does not resolve in
the sandbox at all). Both empty here; noted as a precondition of §7.5
rather than an assumption.

## 79. Locking the skills store, and measuring what that is worth — MEASURED **[verified in-sandbox, 2026-09-14]**

cananian's call, after §78 established there is no setting for it:
*"Let's remount the skills directory read-only on startup to close the
gap the same way we're doing for the primary workspace, and then draft a
bug report."* Both halves shipped. The order of that sentence is the
important part — the remount is what we can do, the report is what would
actually fix it, and §77.3's argument that no in-sandbox control closes
this is unchanged by shipping one.

### 79.1 Both directions, in a live sandbox

Measured here rather than reasoned about, on this sandbox's own mount:

```console
agent@wmf-claude-sbx:~$ awk '$5=="/home/agent/.claude/skills"{print $6}' /proc/self/mountinfo
rw,nosuid,nodev,relatime
agent@wmf-claude-sbx:~$ sudo mount -o remount,ro,bind /home/agent/.claude/skills
agent@wmf-claude-sbx:~$ awk '$5=="/home/agent/.claude/skills"{print $6}' /proc/self/mountinfo
ro,nosuid,nodev,relatime
agent@wmf-claude-sbx:~$ touch /home/agent/.claude/skills/.probe2
touch: cannot touch '…/.probe2': Read-only file system
```

So `remount,ro,bind` takes on a virtiofs mount that is not itself a bind
mount, and the write fails for real — not the §22 / `sbx-releases#556`
shape where the remount reports success and writes still land.

Then the other direction, which is the one worth having in writing:

```console
agent@wmf-claude-sbx:~$ sudo mount -o remount,rw,bind /home/agent/.claude/skills
agent@wmf-claude-sbx:~$ touch /home/agent/.claude/skills/.probe2 && echo BYPASSED
BYPASSED
```

One command, first try. **This is a second layer, not a boundary**, for
exactly the reason `SECURITY.md` §3 gives about the repo mirrors: the
remount lives in the container's own mount namespace and the agent is
root there (§8). It stops a careless write, an agent that installs a
skill without thinking about where `~/.claude/skills` goes, and a
prompt-injected one that does not think to try `sudo`. It stops nothing
that means it.

It is still worth shipping, on two grounds. It costs nothing — Route A
(§64) means nothing of ours is ever written there — and it changes the
default from "writable unless someone noticed" to "writable on purpose",
which is the distinction §77.4's accidental `rm` turned on.

### 79.2 Where it hooks in, and why both places

`wmf_sbx_setup.lock_shared_skills`, called from two paths:

- **install** (`run_setup`), immediately after `install_helper_scripts`
  and *before* any repo work — the window before it is read-only is the
  one worth keeping short;
- **every container start** (`run_restore`), before the layout is even
  loaded. That placement is deliberate twice over: the remount is
  mount-namespace state, so `sbx stop` discards it exactly as it discards
  the repos' (§40); and unlike the repos this one is worth locking on the
  *first* start too, when there is no `layout.json` yet and the old code
  returned early.

`--verify` reports a store that came back writable, through the same exit
status and the same `--wait` polling as the repos, so it fails the way a
mirror that came back `rw` fails rather than quietly (§41's lesson,
applied to a mount that is not a repo).

Three deliberate non-actions:

- **A sandbox with no shared store is not touched.** If nothing is
  mounted at the path it is a plain directory, private to this sandbox,
  and none of our business — `shared_skills_problem` returns `None`
  rather than claiming a problem, the same distinction `readonly_problem`
  makes in the same words (§41 in reverse).
- **A failed remount warns, it does not fail the setup.** A sandbox
  whose skills store could not be locked is worse than one where it
  could, and far better than no sandbox.
- **The `Edit(~/.claude/skills/**)` deny stays.** It covers the likelier
  failure (the model talked into it by an injection) and the remount
  covers a different one; neither is the mitigation, and §77.3 already
  says why.

### 79.3 The report, written before we knew it was a duplicate

`sbx/reference/upstream/skills-store-writable.md` — a paste-ready issue
body for `docker/sbx-releases` with the four-step reproduction (mounted
shared, writable, visible in a second sandbox, deletable from it), the
persistence-past-`sbx rm` argument, three suggested fixes in increasing
size, and the workaround above written out for anyone else who hits it.

Two things kept out of the body deliberately. It asks the filer to check
for a security policy first — this is a sandbox-to-sandbox isolation
bypass, and the public tracker may not be where it goes — and it does not
present our remount as a fix, because it is not one.

**Superseded the next day by §79.4: the bug was already filed as
`docker/sbx-releases#506`.** The file stays in the tree, retitled as our
reproduction record rather than as something to file — the measurements
in it are still the evidence behind §79.1–79.2, and the two findings
#506 does not make (the delete channel, and persistence past `sbx rm`)
are the material for a comment on it if cananian wants one.

### 79.4 Already filed — `docker/sbx-releases#506` — and upstream has accepted it

cananian, 2026-09-14. **Not ours to file: someone got there three weeks
earlier, and a maintainer answered the same day.**
[docker/sbx-releases#506](https://github.com/docker/sbx-releases/issues/506),
*"sbx 0.39.0 enables back door communication between agents in separate
sandboxes by default"*, filed by `cash` on 2026-08-25 against 0.39.0.
Open, unlabelled, **assigned to `rcjsuen`** — a Docker/sbx maintainer.

It names the same mount (`.local/state/sandboxes/sandboxes/agent-skills`,
"mounted writable into every sandbox", "enabled by default", with a nod
to other paths on other OSes) and frames it as a communication channel
between agents, which is the same finding as §77.2–§77.3 arrived at
independently. `cash` followed up with the motivating case: health data
under controls that forbid public inference endpoints, defeated by a
second sandbox on the same host that has internet access.

**The maintainer's reply, 2026-08-25 — this is the important part:**

> The concern is valid and we are looking at adding a
> `--shared-skills-rw` to make it writable but by default the mount will
> be read-only. Note that this change will only affect new sandboxes as
> we cannot change the bind mount behaviour dynamically after the fact.

So the fix we would have asked for is the fix they intend: **`ro` by
default, `--shared-skills-rw` to opt out.** Three things follow.

1. **It had not shipped as of 0.42.1** — our mount is `rw` (§77.2,
   measured on 0.42.1). "Looking at adding" on 2026-08-25, still open on
   2026-09-14. So §79.1–79.2's remount is what we have in the meantime,
   and the watch item in the to-do list is how we notice the day it
   changes.
2. **"Only affects new sandboxes" is an upgrade instruction, not a
   footnote.** Bind-mount behaviour is fixed at create time, so the
   release that ships `ro` leaves every *existing* sandbox `rw` — ours
   included. Taking the fix means **recreating** sandboxes, not just
   upgrading sbx; until each one is recreated, our remount is still the
   only thing standing there. Recorded in `SECURITY.md` §9.
3. **Our remount degrades correctly when it lands.** `lock_shared_skills`
   already no-ops when the mount is not a mountpoint *or* is already
   `ro` (§79.2), so on a post-fix sandbox it does nothing and says
   nothing, and on a pre-fix one it keeps doing its job. Nothing to
   change on our side when the fix ships except to stop calling it the
   mitigation.

**Two flag names, and they are not the same flag.** The issue *body*
refers to **`--no-share-skills`** as something that already exists (its
complaint is not that there is no opt-out, but that an opt-out "puts the
onus on all our staff to be aware of the flag and use it consistently" —
a fair and different argument). The maintainer's reply proposes
**`--shared-skills-rw`** as the *future* opt-*in* to writability. Both
are create-time flags, so §78's `settings` survey could not have seen
either; see the correction inset there. **Neither is verified on
0.42.1**: not in our docs mirror,
`docs.docker.com/reference/cli/sbx/create/` renders empty through a
fetch (§3 warned the `.md` stubs at that depth are empty; this time
`index.html` came back bare too), and `sbx` is not installed in this
sandbox. One command on the host settles both:

```console
$ wmf-sbx create --help | grep -i skill
```

**If `--no-share-skills` exists today, it outranks everything in
§79.1–79.2**, because it is enforced host-side — which is exactly what
the read-only remount cannot be (§79.1: the agent lifts it with one
`sudo mount`). It would belong in `build_sbx_command` unconditionally;
we never write to the store (Route A puts the plugin under
`~/.claude/plugins/`), so there is nothing to weigh. In the to-do list
below as its own item, ahead of the watch item.

**cscott commented on #506 the same day** we found it, adding the two
things the issue did not claim: that a writer reaches sandboxes on
unrelated repositories, in other projects, and *not yet created*; that
modifying or deleting others' skills causes interference even without
malice; and that the change **outlives removal of the sandbox that made
it**. That closes the "should we comment" question — §77.4's
measurements are on the record.

### 79.5 I reported "no maintainer response", and there were three comments

**Correction, same day, cananian.** The paragraph above originally said
#506 had been "public and unanswered for three weeks", and built an
argument on it: that upstream was not engaged, so waiting was not a
strategy. Every part of that was wrong. The issue is assigned to a
maintainer, the maintainer replied within 44 minutes, and the reply
contains a concrete design — the one we wanted.

**How.** I fetched the issue's rendered HTML page and read the body.
The comment thread was not in what came back, and I wrote *absent from
my fetch* as *absent upstream*. The comments were one request away the
whole time:

```console
$ curl -s https://api.github.com/repos/docker/sbx-releases/issues/506/comments
```

which returns author, date and body for all three. `author_association`
there is `NONE` even for `rcjsuen`, so that field would not have caught
it either; the assignment would have, and cananian simply knew.

This is the third turn of the same screw in one thread — §77.2 (read the
mode instead of attempting the write), §78/§79.4 (surveyed `settings`
and concluded about *all* surfaces), and now this. The general form is
worth writing down once: **a negative finding is a claim about the
method, and it is only as strong as the method.** "I did not find X"
becomes "X is not there" only when the search covered where X would be.
Say which surface was searched — `sbx settings`, the rendered issue
page, `ls` without `-a` — and the reader can see the hole without having
to re-derive it. Applied here: what I could honestly have written from a
page fetch was *"the issue body does not mention a fix; I did not read
the comments."*

### 79.6 Neither flag exists on 0.42.1 — MEASURED **[host, cananian, 2026-09-14]**

cananian ran the check §79.4 asked for and pasted the whole thing.
**`sbx create --help` on 0.42.1 has no skills flag of any kind.** Not
`--no-share-skills`, not `--shared-skills-rw`. The full flag list is
`--allow-network`, `--clone`, `--cpus`, `--deny-network`, `--env`,
`--env-file`, `--image-ref`, `--kit`, `--kit-arg`, `--kit-args-file`,
`--memory`, `--name`, `--on-timeout`, `--platform`, `--profile`,
`--publish`, `--quiet`, `--static-mcp`, `--template`, `--ttl`,
`--volume`, plus the two global ones. Nothing matches `skill`.

**So there is no host-side opt-out today, at all.** That resolves the
question §79.4 left open, and it resolves it the unwelcome way:

- **`--shared-skills-rw` has not shipped**, exactly as the still-open
  issue implies. Expected.
- **`--no-share-skills` is not there either**, which was *not* expected
  — #506's body refers to it as a thing that exists and that staff have
  to remember to use. Either it predates 0.42.1 and was removed, or it
  lives on a surface `create --help` does not show, or the reporter was
  describing something they wished for or misremembered. We do not know
  which, and it does not matter much: we cannot pass a flag that the
  binary we run does not accept.
- Therefore **§79.1–79.2's in-sandbox remount is the whole of our
  answer** until #506's fix ships — not a second layer behind a
  host-side boundary, because there is no host-side boundary to be
  behind. `SECURITY.md` §7.6 already says the remount is not a boundary;
  this is the sentence that says nothing else is either.

**The surface actually searched, and the one that wasn't.** This is
`sbx create --help` — the parent command. `create` also has per-agent
subcommands (`sbx create claude`, `codex`, `copilot`, …), and a cobra
subcommand can define flags of its own that the parent's help does not
list. That hole is small and cheap to close — `sbx create claude --help
| grep -i skill` — and it is named here rather than left implicit,
which is §79.5's rule applied on the spot rather than a week later.

> **Closed the same day** (§82.4): cananian ran exactly that command and
> it matched nothing. Both surfaces are now searched, so "0.42.1 has no
> skills flag" is entitled to the scope it is stated in.

**One flag worth a second look, unrelated to skills:** `--profile
string — Governance profile to assign to the sandbox`. That is a
policy surface we have never examined, and both of this thread's
"is there a knob?" misses (§78, §79.4) were about looking on too few
surfaces. It may be enterprise/centralised-governance only — §1's
"Blocked by org policy" path — in which case it is not ours to set, but
that is a guess and it is written here as one. In the to-do list below.

## 80. The host browser is one unauthenticated POST away — MEASURED **[verified in-sandbox, 2026-09-14]**

cananian's to-do said "watch for a `browser.enabled` setting, and mention
it in `SECURITY.md`", pointing at
[docker/sbx-releases#577](https://github.com/docker/sbx-releases/issues/577).
Reading it turned up a reach *out* of the sandbox that `SECURITY.md` §8
did not list, so this is the measurement behind the new §7.7.

### 80.1 What #577 says, and who answered

`rhcarvalho`, 2026-09-10, against 0.42.1: the egress proxy at
`gateway.docker.internal:3128` also serves `POST /_sbx/browser-open`,
which makes host `sandboxd` launch the host's default browser — `open`
on macOS, desktop portals on Linux — with no user confirmation. They
ask for `sbx settings set browser.enabled false`, or a kit permission,
or a host prompt. Their two risks: the host browser carries live session
cookies for GitHub, Google, AWS, corporate SSO, so an agent can drive
OAuth/account-linking/CSRF-shaped flows; and any guest process at all can
put tabs on the host desktop.

`kiview` answered the same day — "valid points and good suggestion. We'll
look into adding this as a setting." No assignee, no milestone, and
**nothing about it in 0.43.0-rc3's notes** (§81), so it has not landed.

The lineage is the part worth keeping: **0.42.0 fixed a sandbox escape on
this same endpoint** — the D-Bus/arbitrary-host-command bug that is the
first of the three fixes `SECURITY.md` §9 wanted the upgrade for. So this
control plane has already been wrong once in a way that reached the host.

### 80.2 The shim is a convenience, not the interface

`/usr/local/bin/xdg-open` is a 895-byte `/bin/sh` script, present in this
image:

```sh
body=$(jq -nc --arg url "$1" --arg session "${SBX_HOST_SESSION_ID:-}" …)
curl -sf --noproxy '*' -X POST "http://gateway.docker.internal:3128/_sbx/browser-open" \
  -H "Content-Type: application/json" -d "$body"
```

`SBX_HOST_SESSION_ID` is set in our environment
(`fe2453a1b6d046ee4ccd75df6943b529`), and `SBX_NO_DISPLAY` is not — the
latter is the SSH-from-a-headless-host case, where the shim prints the
URL instead. Nothing here is a capability the shim holds: the endpoint is
a plain HTTP POST to a host: port every process in the sandbox can reach,
and `with_entries(select(.value != ""))` means even the session id is
optional in the body.

### 80.3 Two probes, and what gates it

**Attempted, not read** — but chosen so that nothing could open. First,
does the endpoint exist at all:

```console
$ curl -s -i --noproxy '*' -X POST \
    http://gateway.docker.internal:3128/_sbx/browser-open \
    -H 'Content-Type: application/json' -d '{}'
HTTP/1.1 400 Bad Request
only https URLs are supported
```

Live, unauthenticated, and answering `curl` — not the shim. Then, what
does it check a URL against? `example.invalid` is reserved by RFC 2606
and cannot resolve or be allowlisted, so this could not open anything
whatever the answer was:

```console
$ … -d '{"url":"https://example.invalid/wmf-sbx-probe"}'
HTTP/1.1 403 Forbidden
Blocked by network policy: domain example.invalid:443
  detail: no matching allow rule — blocked by default deny policy
```

**That is the finding.** The gate is the *network allow policy* — the
same rule set, and byte-for-byte the same refusal text, that §1 and
`CLAUDE.md` document for ordinary egress. There is no separate browser
allowlist and no confirmation step. So **every domain the sandbox may
talk to is also a domain the sandbox may open in cananian's browser**,
and §1's permissive default list is doing a second job we had never
counted.

### 80.4 What I did not test, and why that is not laziness

Whether an *allowed* domain really opens a tab. The test is the same
`curl` with `https://github.com/` in it, and it would put a tab on
cananian's desktop — an outward-facing effect on someone else's machine,
to confirm something #577 reports first-hand and the shim's existence
implies. The standing rule is *attempt the bypass rather than read the
mode* (§22, §77.2), and the 403 above is that attempt: it proves the
endpoint is live and that policy is what stands in front of it. What
remains unproven is only the last hop, and cananian can settle it in one
command whenever it is worth a tab.

Recorded here rather than left implicit, per §79.5: the surface searched
was the endpoint's *refusal* behaviour; the surface not searched was its
*success* behaviour.

### 80.5 Why there is nothing for us to ship

Deleting `/usr/local/bin/xdg-open` from the image would remove the polite
path and leave the endpoint, which any process reaches with `curl` — the
§7.6 shape exactly, a guard one layer below where the decision lives, and
this time not even a guard against a careless caller, because the careless
caller is `xdg-open` and the deliberate one is three lines of shell.

The lever that would work is §1's allowlist, since that is what the
endpoint consults. Narrowing the network policy was already the biggest
single item on `SECURITY.md` §10 for egress reasons; it now closes two
holes instead of one, which is worth knowing when it gets prioritised.

## 81. Reading 0.43.0-rc3's release notes before upgrading — **[read 2026-09-14]**

`rcjsuen` replied to cscott on #506 fifteen minutes after the comment
landed: *"Please give
[v0.43.0-rc3](https://github.com/docker/sbx-releases/releases/tag/v0.43.0-rc3)
a whirl and test with `--skills=readwrite|readonly|off`."* Published
2026-09-09; it is the only 0.43 pre-release, and it is a **release
candidate**, which is the first thing the plan below has to take a view
on. Everything in this section is from the published notes, not measured.

### 81.1 #506 is fixed, and the flag has a third name

> `sbx create`/`sbx run` now share skills read-only by default via a new
> tri-state `--skills=off|readonly|readwrite` flag; the retired
> `--no-share-skills` flag still works as a deprecated alias for
> `--skills=off`. There is also a new `skills.defaultMode` to set the
> desired default behaviour.

Four things fall out.

1. **`readonly` by default** is the host-side boundary §7.6 has been
   missing. It is enforced where the mount is made, which is the whole
   point — the agent cannot `sudo mount` its way past a decision taken
   outside its namespace.
2. **`--skills=off` is better than `readonly` for us, and we should take
   it.** `readonly` still *mounts* the store, so a compromised sandbox
   elsewhere that is running `readwrite` can still plant a skill that
   *this* sandbox reads and acts on. Read-only protects the store from
   us; `off` protects us from the store. We have no reason to want either
   direction: Route A (§64) ships our plugin as a per-kit copy under
   `~/.claude/plugins/`, so nothing of ours is in the shared store and
   nothing of ours reads it. `off` removes the channel instead of
   narrowing it.
3. **`skills.defaultMode`** is a *settings* key, and it is the answer to
   the question §78 asked and §79.5 flagged as wrongly scoped. There was
   no such key on 0.42.1 and there is one on 0.43. Setting it to `off`
   covers sandboxes created outside our wrapper — the same
   belt-and-braces argument as `ssh.agentForwardingEnabled` (§78.1), and
   the same limit: a setting is not a boundary, it is a default.
4. **`--no-share-skills` did exist**, and is now "retired" — so #506's
   body was right and §79.6's measurement was also right. The
   reconciliation is that 0.42.1's `sbx create --help` does not print it;
   a deprecated flag can be hidden from help and still parse. §79.6 said
   "we cannot say why it isn't there" and that is now answered: it was
   there, hidden. Worth keeping as a reminder that `--help` is a surface
   too, and an incomplete one.

Our `build_sbx_command` change is §82.

### 81.2 Sandboxes now stop themselves, which touches more of our code than the skills fix

> Sandboxes created with `sbx create` now stop automatically after
> becoming idle.

No knob is named in the notes and none of our code knows about this. It
matters because *everything* we built around container restarts now fires
on a schedule instead of only when someone types `stop`:

- **§4's residual window becomes routine.** The host mirrors are writable
  for the first seconds of every container start, until the `--restore`
  startup command remounts them. That window used to open when cananian
  restarted something; now it opens whenever a sandbox has been idle and
  someone comes back to it. Same size, many more occurrences.
- **The git daemon dies with the container.** Host-side
  `git fetch sandbox-<name>` is how cananian reviews this work, and an
  idle-stopped sandbox has no daemon listening. This is very likely the
  cause of the `wmf-sbx-rm` to-do already on the list ("often refuses
  because it can't connect to git daemon") — on 0.42.1 that needs an
  explicit stop to reproduce; on 0.43 it will happen by itself, and that
  to-do stops being a wart and becomes the normal path.
- **`wmf-sbx-resume`'s `--verify --wait=20`** (§46) is exactly the right
  shape for this and should need no change — but it is now on the hot
  path rather than the cold one, so its timeout is worth a second look
  with real numbers.

Nothing to change before the upgrade; a lot to *measure* after it.
`RESUME.md` §5.2 is that list, and it sits directly behind the two
measurements that justify the upgrade at all.

### 81.3 `inspect` grows mount information — a host-side check we have never had

> `daemon inspect` and `inspect` will now show mount information

Every mount claim in this file was measured from *inside* a sandbox,
which is the wrong side for questions about what was mounted and how
(§22 is the whole cautionary tale: `/proc/mounts` says `rw` after a
remount that changed nothing). A host-side view of the mount table
settles `:ro` on the extras, the primary's `rw`, and the skills mode
directly. First thing to run after the upgrade.

### 81.4 The rest, sorted by whether it touches us

**Security fixes we want** (beyond #506): credential handling hardened in
the egress proxy, so a client-supplied credential the proxy did not issue
is not forwarded to managed provider hosts; `sbx exec` no longer
synchronises credentials that were not configured for the sandbox;
unrelated credentials no longer switch Claude sandboxes into
Anthropic-API-key mode; credential-binding consent now **defaults to
decline**; the OAuth credential gate no longer lets a third-party kit
re-declare a built-in agent's OAuth service and inherit its trust
(self-heals on daemon restart, no recreate needed); git kit cloning
hardened against `GIT_CONFIG_PARAMETERS` injection from the inherited
environment; signed git kits materialise from the commit's blobs alone,
so host git config — smudge filters, LFS, line endings, hooks — can no
longer alter the checked-out bytes. That last one matters to us as a
kit-shipping project and it bears on the `kit.requireSignature` to-do.

**Behaviour we need to watch:**

- `sbx run --model` now honours the selected model (was falling back to
  the harness default) — irrelevant to us, but it means `--model`
  behaviour changed under `wmf-sbx-resume`'s feet if it ever grows one.
- IP-literal network allow rules (`[::1]:8080`, CIDR) are enforced again
  after a regression; our localhost/published-port work lives near this.
- "Claude sandboxes can now access the Claude Code documentation" — a new
  allowlist entry, i.e. §1's default list grew. Re-run §24's policy dump.
- Balanced policies now allow the NodeSource APT repository — same.
- Name validation is now client-side: >63 chars, or trailing `-`/`.`, are
  rejected. `wmf-sbx-create` builds names from repo names; worth a test.
- The minimum memory is now 512 MiB (shell use cases only).

**Breaking, but not for us:** `shareSkills` in `sbxenv.yaml` becomes
`skills` — we do not use `sbxenv.yaml` (grepped: no reference anywhere in
the tree, and §3's note that it was never adopted still holds). MCP OAuth
client secrets are renamed `mcp:<server>:client_secret` from
`mcp:<server>.client_secret` and the old name is **no longer read** — we
store none (grepped), and Route 1 keeps MCP credentials on the host
outside sbx's vault entirely, so this passes us by. `sbx run
--list-providers` is removed; we never called it.

**A quiet one worth flagging:** "formerly built-in agents resolve from a
pinned, content-verified kit revision instead of the community repo's
latest commit", and separately `kit.allowExtractedAgents`. `claude` is
still built-in so this is not ours yet, but it is the supply-chain story
for the agents that moved out, and `kit.allowExtractedAgents=false` is a
new setting for the `wmf-sbx-create` settings-check to-do to consider
alongside `kit.allowLocalKits` and `kit.requireSignature`.

### 81.5 The one thing the notes do not say

Nothing in 0.43.0-rc3 addresses **§3: the primary workspace is mounted
read-write and `sbx create` rejects `:ro` on it.** That is still the
largest structural gap, and the upgrade does not close it.

But cananian's own reading of the 0.42.1 help text points at something
better than waiting, and it is already available on **0.42.1** — see
§82.2. It deserves a measurement before the upgrade, because it is a
question about `sbx create`'s argument handling and the answer is
cheaper to trust if we have it on both versions.

## 82. Two changes to make *before* the upgrade — one DONE, one MEASURED, DESIGNED, then DEFERRED in favour of asking upstream 2026-09-14

§81 read the release notes. This is what falls out of them into our own
tree, and the rule both halves obey: **the checkout has to be correct on
both versions**, because the upgrade is cananian's to run, at a time of
their choosing, and a code change that only works after it turns a
version bump into a flag day.

### 82.1 `supported_skills_flag`: ask the binary, don't do version arithmetic

`build_sbx_command` now takes a `skills_flag=None` keyword and appends it
after `--kit` and before the `claude` positional; `main` fills it in from
a new `supported_skills_flag(run=subprocess.run)`, which runs
`wmf-sbx create --help` and greps the output:

```python
if "--skills" in help_text:
    return "--skills=off"
if "--no-share-skills" in help_text:
    return "--no-share-skills"
return None
```

`--skills=off`, not `readonly`, for §81.1's reason: `readonly` protects
the store from us, `off` protects us from the store, and we want neither
direction (Route A, §64, keeps our plugin out of the shared store
entirely).

**Why a probe and not `if version >= 0.43`.** Three reasons, in
increasing order of how much they cost when ignored.

1. We do not have a version number in hand at that point in `main`, and
   getting one means a second subprocess whose output format is its own
   parsing problem. The help text is the thing we actually want to know
   about: *does this binary accept this flag*.
2. A version comparison bakes in a claim about a release that has not
   happened. 0.43.0-rc3 is an **rc**; if the flag is renamed again before
   0.43.0 final, a version check silently passes the wrong flag and
   `sbx create` dies, while the probe silently passes nothing and we are
   back to the remount. Both fail; only one fails safe.
3. §81.1's fourth point is the direct warning: `--no-share-skills` was
   real on 0.42.1 and **absent from `--help`** (§79.6 measured the
   absence correctly and drew the wrong inference from it). So the probe
   is itself an incomplete surface — it can only produce false negatives,
   never false positives, which is the direction we can live with. It
   will not find a hidden deprecated alias. It will never invent a flag
   the binary rejects.

When neither flag is found, `main` prints a warning naming
`SECURITY.md §7.6`, says the store will be writable, says the remount is
what we have, and points at 0.43+ and #506 — and then **creates the
sandbox anyway**. Failing the create would be a worse trade: it makes
today's supported version unusable to protect against a risk we have
lived with since §77.

Two details that are easy to get wrong and are pinned by tests:

- `stdin=subprocess.DEVNULL` on the probe, per the standing rule — a
  `--help` that somehow asks a question should die, not hang invisibly
  behind sbx's collapsed output (§69.3).
- The flag goes **before the agent positional**. `sbx create [flags]
  AGENT [PATH...]` puts the agent first among the positionals; a flag
  emitted after `claude` is parsed by the `claude` *subcommand* (§79.6:
  `create` has real per-agent subcommands), which is a different flag set
  and a different answer. `test_the_skills_flag_goes_before_the_agent_positional`
  is the guard.

10 tests added (`SupportedSkillsFlagTests` plus three in
`BuildSbxCommandTests`), with `HELP_0_42_1` taken verbatim from
cananian's host paste and `HELP_0_43` derived from it by substitution, so
the fixture for the version we cannot run is honestly labelled as
synthetic. Suite: 803 tests, OK.

### 82.2 No primary workspace — three shapes, and the one measurement that picks between them

cananian: *"I believe the latest version of sbx allows starting it
'without any workspace directory' which may also solve the 'first named
directory is mounted read-write' problem."*

The capability is real and it is **already on 0.42.1** — it is in the
help text cananian pasted for a different purpose:

> Omit the path to create a sandbox without a workspace bind mount: the
> agent then works in the container's own filesystem instead of on your
> files.

with `# Create without a workspace bind mount` / `sbx create claude` in
the examples. So this needs no upgrade at all. What it needs is a
measurement, because the usage line is

```
sbx create [flags] AGENT|SANDBOX_KIT [PATH...]
```

— the workspaces are *positional*, and "the primary" is just "the first
one". "Omit the path" therefore means omit **all** of them, which by
itself gives us a sandbox that cannot see the repos it is supposed to
clone. It is only useful in combination, and there are three shapes:

**A. `sbx create claude A:ro B:ro …` — every path read-only, no rw one.**
The clean answer if it works: nothing of the host's is writable, and
`wmf-sbx-create` changes by deleting the warning at `create.py:1743` and
the `:ro`-skips-the-primary special case. Whether it works is exactly the
question: is "primary must be read/write" a rule about *position* (the
first positional is the primary, full stop) or about *the set* (there
must exist one rw workspace)? §22 measured the error message —
`ERROR: primary workspace must be read/write (remove ':ro' or ':readonly')`
— which reads positional, but that was one path, and a one-path command
cannot distinguish the two readings. **This is the measurement.** One
command answers it.

**B. A throwaway scratch directory as the primary**, with every real repo
as a `:ro` extra. Works on today's grammar whatever the answer to A is,
because the primary genuinely is read-write — it is just an empty
directory we created for the purpose (say
`~/.local/state/wmf-sbx/scratch/<name>/`) that nothing reads and nothing
writes. Costs: a new host directory per sandbox to create and to clean up
in `wmf-sbx-rm`, and a nonsense path in `sbx ls` output. Closes the gap
as completely as A does.

**C. `--clone` on the primary.** Not new, and worth restating here
because it is the shape people reach for: sbx bind-mounts the host repo
**read-only** at `/run/sandbox/source` and clones it onto the container's
own filesystem (§2). That closes §3 for the primary, host-side, on
0.42.1. We do not use it because our generated kit already does the
writable-cloning itself for *every* repo (`create.py` docstring), and
`--clone` covers only the first workspace — so adopting it would mean two
different clone mechanisms in one sandbox, with the primary's parallel
tree arriving by a path `wmf-sbx-setup` does not lay out. Rejected on
consistency, not on security; if A and B both fail, reopen it.

**What changes in our code, for A or B.** Less than it looks. `primary`
in `wmf-sbx-plan.json` is a *semantic* marker — "the repo I am here to
work on", which is what `repos_to_leave_alone` keeps (`setup.py:1597`) —
and it is only incidentally `repos[0]["path"]` (`setup.py:1963`,
`kit.py:856`). Decoupling sbx's positional-0 from the plan's `primary` is
a keyword argument, not a redesign. `build_sbx_command`'s primary/extras
split (`create.py:1724`) is where it lands.

**Why measure it before the upgrade and not after.** The answer is a fact
about `sbx create`'s argument handling, and it is worth having on *both*
versions: if A works on 0.42.1 and we take it, the upgrade has one less
variable in it; if A works only on 0.43, that is a reason to upgrade
rather than a reason to wait; and if A fails on both, B is unaffected by
the version and we can do it today. `RESUME.md` §6 has the commands.

This is the largest structural gap in `SECURITY.md` (§3), it is the one
thing 0.43.0-rc3 does **not** fix (§81.5), and it turns out we may have
been able to close it since before we started.

### 82.3 A is rejected; B needs no permission slip — MEASURED **[cananian, host, 2026-09-14]**

cananian ran it within the hour:

```console
$ sbx/bin/wmf-sbx create --name probe-allro claude \
    ~/Projects/Wikimedia/wmf-claude:ro ~/Projects/Wikimedia/core:ro
ERROR: primary workspace must be read/write (remove ':ro' or ':readonly')
```

**Shape A is dead.** Two paths, the same error as the one-path case in
§22, and no sandbox created. The rule is about *position*: the first
positional is the primary, and the primary is read/write, full stop. It
is not "the set must contain one read/write member".

**But that distinction, which §82.2 built a measurement around, turns out
not to matter** — because **shape B satisfies both readings**. A
throwaway empty directory in positional 0 is a read/write primary under
the positional rule *and* a read/write member under the set rule. So
there was never an acceptance question to answer for B, and there is no
second probe to ask for: whatever rule sbx is enforcing, B is inside it.
Worth noticing as a small failure of my own framing — I designed a
measurement to choose between A and B when the honest shape was "A is a
long shot, B always works, measure A because it is one command and it
would be cheaper if it worked". It wasn't; it isn't; B it is.

**What B costs, precisely, and the one thing it breaks.** sbx starts the
agent in the primary workspace's path (§39). Today that is
`~/Projects/Wikimedia/wmf-claude`, which `wmf-sbx-setup` has already
aliased to the writable parallel clone — so "the starting cwd *is* the
writable clone, nothing has to `cd`". Move positional 0 to a scratch
directory and the starting cwd becomes the scratch directory, which is
empty. That is the whole cost, and it has a one-line fix that is not even
novel: **mount the primary's writable clone over the scratch path too.**
`wmf-sbx-setup` already mounts clones over paths — it is the same
operation it performs for every repo — and a single directory reachable
by two paths is exactly the arrangement the sandbox's own `CLAUDE.md`
already documents for `~/Projects/…` and `~/<path>`. cwd lands in the
clone; so does the literal host path; nothing has to `cd`.

**The shape, concretely:**

```
sbx create --name NAME --kit KIT --skills=off claude \
  ~/.local/state/wmf-sbx/scratch/NAME \      ← positional 0, empty, ours
  ~/Projects/Wikimedia/wmf-claude:ro \       ← the real primary, now an extra
  ~/Projects/Wikimedia/core:ro …             ← unchanged
```

and the host's `wmf-claude` checkout is `:ro` at the `sbx create` layer —
backed below the namespace, where `sudo mount -o remount,rw` cannot reach
it (§22). That is `SECURITY.md` §3's gap closed, not narrowed.

**What changes in our code.** Less than the prose suggests, because
`primary` already means two different things and only one of them moves:

- `create.py:1724`'s `primary_dir` / `extra_dirs` split — the scratch
  directory becomes sbx's positional 0, and the user's primary joins the
  extras with the same unconditional `:ro` every other extra gets.
- `wmf-sbx-plan.json`'s `primary` keeps pointing at the **real** repo. It
  is a semantic marker — "the repo I am here to work on", which is what
  `repos_to_leave_alone` reads (`setup.py:1597`) — and it is only
  incidentally `repos[0]["path"]` (`setup.py:1963`, `kit.py:856`). Those
  two call sites are the ones that need an explicit value rather than an
  index.
- `setup.py` gains the second bind mount described above, and the scratch
  path has to be in `layout.json` so `--restore` reinstates it on every
  container start (§46) — the same discipline as every other mount.
- `create.py:1743`'s "':ro' on the primary cannot be enforced" warning is
  deleted. It stops being true.
- `wmf-sbx-rm` removes the scratch directory it created.

**Not done here**, deliberately. It changes where the agent wakes up,
which is user-visible, and it touches the restore path, which is the
subtlest code in the project (§40–§46). It is in the to-do list with this
section as its design note, and it is cananian's call whether it goes in
before or after the 0.43 upgrade — it is independent of both.

> **Deferred, and "one-line fix" was too optimistic** — §82.5. The
> paragraph above beginning "What B costs, precisely" understates it: the
> mount fixes the contents of the cwd but not its *name*, and the name is
> what the agent orients by, what every restart resets, and what Claude
> Code keys its project state and memory directory off. Everything else
> in this section stays valid as a fallback design; §82.6 is the route
> being tried first.

### 82.4 The 0.42.1 skills question is now completely negative — MEASURED **[cananian, host, 2026-09-14]**

The same paste closed §79.6's named residual:

```console
$ sbx/bin/wmf-sbx create claude --help | grep -i skill
$
```

No match, and no error — so `create`'s **per-agent subcommand** help
printed, and it mentions no skills flag either. §79.6 searched
`sbx create --help` (the parent) and said out loud which surface it had
not searched; that surface is now searched. **0.42.1 has no skills flag
on either command**, and the negative finding is entitled to its scope
for the first time.

One honest caveat, in the spirit of §79.5: an empty `grep` and empty
output are indistinguishable from each other in a pipe. What rules out
"the command failed and printed nothing" is that no error text appeared
on the terminal — `grep` only swallows stdout, and the failure in the
line above it printed its `ERROR:` exactly as expected. That is good
evidence, not proof, and it is not worth a second round trip.

### 82.5 Shape B is deferred: the working directory is load-bearing **[cananian, 2026-09-14]**

§82.3 recommended shape B and called its one cost — the agent wakes up in
an empty scratch directory — a thing with "a one-line fix that is not
even novel". cananian deferred the implementation on exactly that point,
and the objection is not about the mount, it is about what the *path*
means to the agent:

> in the past the sandboxed claude always started in "the working
> directory" and we were largely unsuccessful in convincing the sandboxed
> claude to start somewhere else. The `/cd` command available to the user
> was unavailable to the agent, and so every startup began with confusion
> about where it was and where the code it wanted to work on way, and
> that confusion persisted through every restart when the directory would
> be reset to the "primary workspace". That's why we had to abandon our
> original clone design in favor of bind-mounting on top of
> `/home/cananian/<path>` — because we were unable to convince Claude to
> make its default workspace directory `/home/agent/<path>` instead.

**This is corroborated by our own §39**, which exists *only* because of
it: §30's "one real wart" was "the agent starts in the read-only host
mirror and cannot `/cd` itself out", and §39's whole four-step mount
dance (`mount --move`, clone, remount `ro`, `mount --bind` back over the
literal path) was chosen over the simpler parallel-tree-only layout so
that "we don't have to change Claude's idea of its working directory
(because it's the same!)". §39 calls that "a cosmetic improvement to the
working directory" — in the context of its best-effort fallback, which is
fair — but the standalone reading of that phrase is wrong, and this
section is the correction.

**Where §82.3's one-line fix actually falls short.** Mounting the
writable clone over the scratch path does fix the *contents*: cwd would
hold the right files. It does not fix the *name*, and three things key
off the name rather than the bytes:

1. **The agent's own orientation.** Waking up in
   `~/.local/state/wmf-sbx/scratch/wmf-claude-sbx` with the project's
   files in it is a worse starting position than waking up in
   `~/Projects/Wikimedia/wmf-claude`, because every path the agent then
   writes, greps for, or reports to the user is that nonsense path —
   which matches neither the host's checkout nor `CLAUDE.md`'s account of
   the layout nor the `/home/agent/<rel>` parallel tree. §39 bought the
   third coincidence on purpose; B spends it.
2. **Claude Code's project-scoped state is keyed by the cwd path.**
   Measured here, in this sandbox: `~/.claude.json` has a `projects` map
   keyed `"/home/cananian/Projects/Wikimedia/wmf-claude"`, and the memory
   directory this project's handoff convention depends on is
   `~/.claude/projects/-home-cananian-Projects-Wikimedia-wmf-claude/` —
   the path with the slashes beaten into dashes. A scratch cwd moves that
   key, so `RESUME.md` §1's "restore the memory seed" step would restore
   into a directory the new session does not read. That is not a cosmetic
   cost; it is the handoff.
3. **Restart resets it, every time.** Per cananian above, the confusion
   "persisted through every restart when the directory would be reset to
   the primary workspace" — so this is not a one-time orientation cost
   paid at create, it is paid again at every `wmf-sbx-resume`, and 0.43's
   idle auto-stop (§81) makes restarts routine rather than rare.

**The hint cananian found, and why it does not rescue B.** The release
note is:

> Sandboxes can now be created without a workspace bind mount by omitting
> the path in `sbx create`. Note that this only affects the `create`
> command; `sbx run` still defaults to mounting the current directory as
> the primary workspace.

**Provenance, since §81's reading of 0.43.0-rc3 did not contain it:** it
is from **v0.42.0**, confirmed against
`api.github.com/repos/docker/sbx-releases/releases/tags/v0.42.0` (the
same paragraph also documents `sbx env` with no `workspace:` key). So it
is a capability of the version we are already on, not something the
upgrade brings, which matches §82.2 finding the same thing in 0.42.1's
help text.

cananian's reading is right and it is worth writing down: the second
sentence implies `sbx run` *can* establish a primary workspace from the
cwd — but only in the case where `create` established none. Give `create`
a scratch primary and that door closes; the sandbox has a primary
workspace already, and it is the scratch directory, permanently. **B and
the `run`-picks-the-cwd behaviour are mutually exclusive**, which makes B
strictly worse than it looked, not merely equal.

There is a fourth shape hiding in there — **create with *no* workspace at
all and let `run` supply it** — and it is not worth pursuing: it puts the
host repo back at read/write (that is what "mounting the current
directory as the primary workspace" means), so it gives up the entire
point of the exercise, and it makes the mount layout depend on which
directory the human happened to be standing in.

**Status.** Shape B stays designed (§82.3) and unimplemented. The to-do
below is downgraded from "close `SECURITY.md` §3" to "deferred, and here
is what would have to be true first". The route we are taking instead is
§82.6.

### 82.6 Ask upstream for a read-only primary workspace instead **[cananian, 2026-09-14]**

cananian: *"It would probably be better to ask to be able to make the
primary workspace read-only, since the sbx maintainers seem reasonably
responsive to feature requests, especially ones related to sandbox
hardening."*

That is shape A (§82.2) — which the measurement in §82.3 killed as a
*fact about today's binary*, not as a design. If `sbx create claude
A:ro B:ro` were accepted, the primary stays at positional 0, the cwd
stays `~/Projects/Wikimedia/wmf-claude`, §39's arrangement is untouched,
and there is no scratch directory to create, mount, restore or clean up.
**Shape A has none of §82.5's costs.** It is the same gap closed with
strictly less machinery — the only thing standing in the way is one
positional rule in someone else's argument parser.

**The ask is small, and most of it already exists.** 0.42.0 already
supports a sandbox with *no* workspace bind mount, so the "no writable
host workspace" case is implemented and shipped; and `:ro` extras are
genuinely enforced below the namespace, which we know because that is
[#556](https://github.com/docker/sbx-releases/issues/556) — cscott's own
report ("sbx read-only bind mounts are reversible", 2026-09-06), which
`rcjsuen` triaged the same morning and which was confirmed fixed and
closed by `kiview` on 2026-09-07. So the request is not "build read-only
mounting", it is "stop rejecting `:ro` in the first positional, and treat
the resulting state the way you already treat a sandbox created with no
workspace at all".

**Evidence that asking works, which is cananian's premise and it holds.**
Three data points, all first-hand: #556 above (filed, triaged within
hours, fixed two releases later); #506, the skills store (filed by
`cash`, accepted by `rcjsuen` the same day, shipped in 0.43.0-rc3 — §81);
and #577, the `/_sbx/browser-open` hole, where `kiview` replied "we'll
look into adding this as a setting" (§80.1) — the weakest of the three,
and still a reply.

**Searched before drafting, so the negative finding names its surface**
(§79.5): the GitHub search API over `repo:docker/sbx-releases`, titles
only, for `workspace` (23 issues) and for `read-only OR readonly OR :ro`
(4). Nothing asks for a read-only primary. The nearest neighbours found
that way are [#136](https://github.com/docker/sbx-releases/issues/136)
("Ability to add new workspaces to a sbx after creation", open) and
[#388](https://github.com/docker/sbx-releases/issues/388) (nested `:ro`
under an `rw` parent silently unmounts on Windows — open, unassigned;
irrelevant to us today because none of our mounts nest, but it *would*
become relevant if we ever mounted a repo inside a writable parent).

> **And the caveat cashed in the same evening.** The paragraph above
> ended "titles are a thin surface: a body-text search might find one,
> and this was not run". It would have found one.
> [#430](https://github.com/docker/sbx-releases/issues/430), "Consider
> adding 'remote' workflow" (`louismrose`, 2026-08-11, open) asks for a
> sandbox with *no* local mount at all, cloning from the git remote
> instead, and objects to `--clone`'s read-only `/run/sandbox/source` on
> the grounds that it exposes untracked and `.gitignore`d files. It is
> the adjacent request, and its title contains neither `workspace` nor
> `read-only`. cananian found it and cited it in #586; our search could
> not have. **Named surface, honestly stated, and still wrong about the
> world** — which is the point of naming it, and an argument for running
> the second search rather than only flagging it.

### 82.7 cananian filed it — `#586`, and what our draft had that it does not **[2026-09-14]**

Filed the same evening, independently of the draft: **[#586](https://github.com/docker/sbx-releases/issues/586)**,
*"Feature request: allow read-only primary workspace for sandbox
hardening"*, cscott, 21:25Z, open, no comments at time of writing. The
ask is exactly §82.6's: `sbx create --name probe-allro claude
/path/to/my/project:ro` should succeed instead of erroring.

**Two arguments it makes that our draft did not**, both better than what
we had: that writable mounts compromise **isolation between sandboxes
working the same repository on separate tasks**, and the open-source
framing — read exposure is fine ("all of our code is available on the
internet anyway"), unexpected writes to the host filesystem are not. It
also frames `--clone`'s workflow as the model to generalise and cites
#430 as the neighbouring request.

**Five things our draft has that #586 does not**, in the order they are
worth saying if the thread needs a comment. Filing one is cananian's, as
the issue is:

1. **The scratch-primary workaround, pre-empted.** The cheapest reply a
   maintainer can give #586 is "use an empty directory as your primary
   and mount the repo `:ro` as an extra" — it works today, we designed
   it (§82.3), and we rejected it because the primary workspace's *path*
   is the agent's working directory, the agent cannot change it, restart
   resets it, and Claude Code keys per-project state off it (§82.5).
   Highest value of the five, because it is the difference between a
   discussion and a close-as-already-possible.
2. **The two-path measurement.** #586 shows the one-path error; §82.3
   also ran `claude A:ro B:ro`, which fails identically. That
   distinguishes "position 0 must be rw" from "the set must contain an
   rw member" and points at a single validation as the fix.
3. **Most of it already exists upstream**: `:ro` enforced below the
   mount namespace since 0.39.0 (#556 — cscott's own report), and
   no-workspace sandboxes since 0.42.0. Reframes the ask from "build a
   feature" to "drop a check".
4. **Cheaper paths to yes**: warn instead of erroring, an explicit
   `--allow-readonly-workspace`, or `:ro` in position 0 read as the
   opt-in itself.
5. **Why `--clone` does not already cover it** for a setup that clones
   every workspace itself: it applies to the first workspace only, so
   adopting it would mean two clone mechanisms and two layouts in one
   sandbox.

`reference/upstream/readonly-primary-workspace.md` is re-headed
accordingly: **FILED as #586**, body kept as follow-up material rather
than as a report.

**What we do while we wait.** Nothing new: §79's in-sandbox remount plus
§39's `mount -o remount,ro,bind` of the originals stays the mitigation,
`SECURITY.md` §3 stays open with both routes named, and shape B remains
on the shelf as the fallback if the request is declined — it is a real
design with a real cost, not a dead end.

### 82.8 The skills remount has never actually run in this sandbox — MEASURED **[in-sandbox, 2026-09-14]**

Checked while sweeping for anything that would be lost at recreate time:

```console
$ mount | grep -i skills
none on /home/agent/.claude/skills type virtiofs (rw,nosuid,nodev,relatime)
$ ls -A /home/agent/.claude/skills | wc -l
0
$ grep -c lock_shared_skills /home/agent/wmf-sbx-setup
0
```

**`rw`, and the store is empty.** The third command is the explanation
and the reason this is not a bug: `/home/agent/wmf-sbx-setup` is the copy
installed when this sandbox was created on **2026-09-08**, and §79's
`lock_shared_skills` was written on **2026-09-14**. The mitigation exists
in the checkout, is unit-tested, and has never executed on this machine,
because nothing re-installs the in-sandbox setup script into a running
sandbox.

Three consequences worth carrying into the upgrade:

1. **The §79 write-up is accurate but easy to misread.** It says the
   remount is "all there is" until #506 lands. In this sandbox there has
   been *less* than that — the store has been writable and shared with
   every other sandbox on the host for the whole six days of this work.
   Nothing was planted (the directory is empty), but that is a fact about
   what happened, not about what was prevented.
2. **The before/after comparison in `RESUME.md` §5.1 needs this**, or the
   next instance will credit `--skills=off` with an improvement over a
   read-only mount that was never there.
3. **It generalises**: the in-sandbox half of this project
   (`wmf-sbx-setup`, 72 KB, copied in at create) is pinned at create
   time. Any fix we write to it reaches only sandboxes created *after*
   it — the same "recreate is not optional" rule §81 states for sbx's own
   bind-mount decisions applies to our own code, and for the same reason.
   Worth remembering the next time a setup-side fix looks like it can be
   deployed by committing it.

### 82.9 The §47.7/§47.8 stdin-hang bug regressed — 4 newer call sites missed the sweep — FIXED **[cananian, host, 2026-09-14; Claude, 2026-09-14]**

The re-verification `RESUME.md` §5.4 item 7 asked for happened by accident,
during the actual recreate attempt, and reproduced the exact failure mode
§47.8's fix was meant to prevent for good.

**What cananian saw** (`responses30.txt`, host, 2026-09-14): `wmf-sbx
mcp ls --json`, run by hand, hit sbx's own client/server version-mismatch
prompt (`Docker Sandboxes has been updated and needs to restart ...
Restart now? (y/N):`) and had to be Ctrl-C'd. Minutes later — after
cananian answered `y` and the daemon restarted — the same dry-run's
`wmf-sbx-create --dry-run` hung too, silently, and the interrupt traceback
pointed at `create.py`'s `ensure_host_mcp_servers()` →
`host_mcp_registrations()` → `run([WMF_SBX, "mcp", "ls", "--json"],
capture_output=True, ...)`: the same shape as 47.7's original report, one
line off.

**Root cause: the sweep was point-in-time, not structural.** §47.8 (2026-
09-08) read "every non-attach `WMF_SBX` call site" that existed *that
day* and added `stdin=subprocess.DEVNULL` to all 8. The MCP-registration
code (`host_mcp_plan`, `host_mcp_registrations`,
`registered_phabricator_username`, `phabricator_username`) was written
**after** that sweep, as part of Route 1 (§65/§72/§73), and nobody
re-ran the audit against the new call sites. Grepping
`sbx/src/wmf_sbx/create.py` for `capture_output=True` this session found
4 that never got the fix:

| line | function | invokes |
|---|---|---|
| 1060 | `node_version()` | `node --version` |
| 1113 | `phabricator_username()` | `claude mcp get phabricator` |
| 1235 | `host_mcp_registrations()` | `wmf-sbx mcp ls --json` — **the one that actually hung** |
| 1281 | `registered_phabricator_username()` | `wmf-sbx mcp inspect phabricator --json` |

**Fix**: `stdin=subprocess.DEVNULL` added to all 4, matching the other 8
exactly — none of these four is a genuinely-interactive attach (those
stay as `run(cmd, env=env)` / `run(cmd).returncode` with the terminal
attached, per §47.8's exceptions list, untouched here). `node_version`
and the `claude mcp get` call in `phabricator_username` are not `sbx`
invocations and were not the one observed hanging, but they are the same
shape — an internal, meant-to-be-non-interactive subprocess call with
`capture_output=True` and no `stdin=` override — so they got the same
fix for the same reason rather than waiting for their own incident.
Unit-tested: `test_wmf_sbx_create.py` gained
`NodeVersionAndPhabricatorUsernameStdinTests` and updated
`HostMcpTests.test_registration_listing_reads_the_json` /
`.test_the_stored_username_is_read_out_of_the_json` to assert the
`stdin=subprocess.DEVNULL` kwarg the same way `ExistingSandboxNamesTests`
already did. Full suite green (805 unit tests, up from 803 by the 2 new
ones; 156 template tests — one template-test failure seen in this
sandbox, `session-start lost the default nono sandbox paragraph`, is
this session's own `WMF_CLAUDE_SANDBOX_BACKEND=sbx` leaking into the
"default" check and is unrelated to this change, present on `main`
before it).

**Not yet re-verified end-to-end**: like §47.8 before it, the fix is
unit-tested against a fake `run`, not against a real `sbx` binary
mid-restart-prompt. The next `wmf-claude-sbx` recreate is the real test
— if `wmf-sbx-create` on the far side of an sbx daemon restart now fails
fast (or succeeds outright) instead of hanging on `mcp ls --json`, this
is confirmed.

**The general lesson, worth stating plainly**: this is a class of fix
that a one-time sweep cannot keep true. Anywhere `create.py` gains a new
`run([WMF_SBX or "claude" or another CLI, ...], capture_output=True,
...)` call, it needs `stdin=subprocess.DEVNULL` at the same time it is
written, not as a follow-up audit. Worth a code-review note the next
time a patch touches this file, rather than another dated NOTES section
after the next incident.

## 83. Post-upgrade: 0.43.0-rc3 confirmed, and the skills mount is gone entirely — MEASURED **[in-sandbox + cananian, host, 2026-09-14]**

*(`RESUME.md`, cited throughout this and the following section, was
folded back into this file's §0 and removed
2026-09-15, once §84 confirmed the re-measurement was done.)*

The recreate in `responses30.txt` (§82.9) completed after the manual
daemon restart. This sandbox — `wmf-claude-sbx`, this NOTES entry
written from inside it — is the first one built on the far side of it.
Two of `RESUME.md` §5.1's checks, now answered:

**Version, confirmed on the host:**

```console
cananian@cscott-framework:~/Wikimedia/wmf-claude$ wmf-sbx version --json
{
  "client": {"version": "v0.43.0-rc3", ...},
  "server": {"state": "running", "version": "v0.43.0-rc3", "api_version": "0.31.0"}
}
```

Both client and server are `v0.43.0-rc3`. The rest of §5's checklist
applies in full — this was not a false start.

**The skills mount: not `ro`, not present at all.** MEASURED in-sandbox,
two independent ways:

```console
agent@wmf-claude-sbx:~$ grep -i skills /proc/self/mountinfo
agent@wmf-claude-sbx:~$ ls -la ~/.claude/
# (no `skills` entry in the listing at all)
agent@wmf-claude-sbx:~$ touch ~/.claude/skills/.probe
touch: cannot touch '/home/agent/.claude/skills/.probe': No such file or directory
```

`grep` against `mountinfo` finds nothing (contrast §82.8's `rw` line for
the old 0.42.1-created sandbox); `~/.claude/` has no `skills` directory
at all, not even an empty one; `touch` fails with **ENOENT**, not
**EROFS** — there is nothing there to be read-only. This is the
strongest of the three outcomes `RESUME.md` §5.1 named ("absent" beats
"present and `ro`" beats "present and `rw`"), and it is the one that
means `--skills=off` actually took: `wmf-sbx-create --dry-run`'s own
output confirms the flag was on the command line (§82's transcript,
`responses30.txt` line 343: `... create --name wmf-claude-sbx
--skills=off --static-mcp ...`).

**Not run**: the `sudo mount -o remount,rw,bind` escalation check
`RESUME.md` §5.1 also asked for. There is nothing to remount — the
mount does not exist — so the check does not apply; an absent mount is
already the stronger guarantee the escalation check exists to rule out
for a merely-`ro` one. A separate in-session attempt to `touch` the path
via the Bash tool was refused by the permission layer before it reached
the filesystem at all (denied, no error text), which is a different
thing from a filesystem-level EROFS/ENOENT and not evidence about the
mount either way; the answer above came from cananian running the same
`touch` directly.

**What this changes, per `RESUME.md` §5.1's closing instruction:**
`lock_shared_skills` (§79) needed no code change — it already no-ops on
an absent mount exactly as it does on an already-`ro` one — but the
*framing* in `SECURITY.md` §7.6 is now stale for any sandbox created
after this recreate: it describes the remount as "the only thing
standing there" against a `rw`-by-default mount, which was true for
every sandbox built before 0.43 and false for every one built after.
§7.6, §9.1 and §10's #506 bullet all need the same edit: the shared
store is gone for new sandboxes, the remount becomes the accident guard
it was always meant to be rather than the load-bearing mitigation, and
the **open risk that remains is only for sandboxes not yet recreated**
— this document should stop presenting §7.6 as live risk for a freshly
created sandbox. `SECURITY.md` edited in this same pass; see its §7.6
epilogue and §10's #506 bullet for the closing note.

`RESUME.md` §5.1 item 2 (`skills.defaultMode` on the host) and the rest
of §5.2–§5.4 are host-side or otherwise not yet done from this sandbox.

## 84. Round two of the post-upgrade measurements — MEASURED **[cananian, host, 2026-09-14]**

`responses31.txt` closes out most of the rest of `RESUME.md` §5. In order:

### 84.1 `skills.defaultMode` exists, and is still at its shipped default

```console
"default": "readonly",
"key": "skills.defaultMode",
"source": "default",
"value": "readonly"
```

`source: default` — nobody has overridden it on this host. That is fine
for every sandbox `wmf-sbx-create` builds, because it always passes an
explicit `--skills=off` when the binary supports it (§82.1); the setting
only matters as a fallback for a sandbox built *without* our wrapper — a
bare `sbx create`, or someone else's kit. The same belt-and-braces
argument as `ssh.agentForwardingEnabled` (§78.1) applies: a setting is
not a boundary, it is a default, and this one is worth raising to
`readwrite`'s opposite. Left as a decision for cananian below rather than
made here, since it changes host-wide behaviour outside this project.

### 84.2 `wmf-sbx inspect --json` cross-checks every in-sandbox measurement, and contradicts none of them

```console
$ wmf-sbx inspect --json wmf-claude-sbx
```

Three things checked against what §83 measured from the *inside*, this
time from the host:

- `additional_workspaces`: all seven extras (`core`, `Skins`, `Extensions`,
  `Parsoid`, `mediawiki-config`, `integration-config`, `docs.docker.com`)
  carry `"read_only": true`.
- `workspace` (the primary, `wmf-claude`) carries no `read_only` key at
  all — read-write, as expected.
- No `skills` key anywhere in the object. §83 found the mount absent from
  inside; this is the same answer from the host side of the boundary that
  §81.3 said we had never had before. Nothing here overturns §83.

Also visible for the first time: `runtime_mounts: []` (nothing dynamic
mounted post-create) and `daemon_version`/`daemon_uptime` matching the
version already confirmed in §83. First clean use of `sbx inspect`'s new
mount information (§81.3); it delivered exactly what the release notes
promised and found no discrepancy to report.

### 84.3 Network policy re-dumped: same five global defaults, but our own scoped rule grew

```console
$ wmf-sbx policy ls wmf-claude-sbx --type network --json
```

Shape unchanged from `SECURITY.md` §1 / `NOTES.md` §24: five
`source: local` default rules apply to every sandbox on this host
(`default-ai-services`, `default-package-managers`,
`default-code-and-containers`, `default-cloud-infrastructure`,
`default-os-packages`) plus one `scope: sandbox:wmf-claude-sbx` rule that
is ours. `nodesource.com:443` is present in `default-package-managers`,
confirming §81.4's "Balanced policies now allow the NodeSource APT
repository" line by name.

The one thing that changed is our own rule. The kit's declared
`permissions.network.allow` list (visible in the same dry-run transcript,
`responses31.txt` lines 389–406) names only the wiki family, `github.com`,
`packagist.org`, and `registry.npmjs.org`. The live policy for
`kit:wmf-claude-sbx` carries those *plus* `code.claude.com`,
`mcp-proxy.anthropic.com`, and `bridge.claudeusercontent.com` — none of
which our kit asked for. sbx 0.43 is merging Claude-agent-specific
entries into the sandbox's own scoped rule alongside what the kit
declared. This is §81.4's "Claude sandboxes can now access the Claude
Code documentation" note, with exact names attached, and it means the
scoped-rule population is no longer *only* what we write in the kit —
worth remembering the next time this rule's contents look surprising.

No contradiction of `SECURITY.md` §1's characterization ("the allowlist
widens, it does not restrict"); if anything this reconfirms it — the
widening now includes entries neither the kit nor the local defaults
account for on paper.

### 84.4 Name-length rejection is real, but the client only checks it at `create`, not at `--dry-run`

A 64-character `--name` sailed straight through `wmf-sbx-create --dry-run`
— full kit generation, `wmf-sbx-plan.json`, `wmf-sbx-mcp.json`, the final
command line, no complaint — and only failed once the real
`/home/cananian/.../sbx/bin/wmf-sbx create ...` command actually ran:

```
ERROR: sandbox name cannot exceed 63 characters: aaaa...
```

Confirms §81.4's claim. Also a caveat for us: **`--dry-run` does not
validate the name**, so a `wmf-sbx-create` test that only ever calls
`--dry-run` would never catch a name that is too long. Not an action item
today — nothing in `wmf-sbx-create`'s naming scheme (agent + repo name)
gets remotely close to 63 characters — but worth remembering if that
scheme ever changes.

### 84.5 The per-agent subcommand help now lists `--skills` — §79.6's residual closes on 0.43

```console
$ wmf-sbx create claude --help | grep -i skill
      --skills string   Shared skills store mode: off, readonly, or readwrite ...
```

0.42.1 found nothing here (§82.4) because the flag did not exist yet
anywhere. On 0.43.0-rc3 it is in the `claude` subcommand's own `--help`
output, not only the parent's — the two surfaces agree, and the residual
§79.6 named ("the per-agent subcommands could define their own flags")
is closed for this flag.

### 84.6 Idle auto-stop measured directly for the first time: on the order of 36–37 seconds, and polling does not reset it

Using a disposable `sbx-cite` sandbox (`wmf-sbx-create Cite`):

```console
$ wmf-sbx-exec sbx-cite -- true
$ wmf-sbx-exec sbx-cite -- true
$ while true; do date; wmf-sbx ls | fgrep cite; sleep 1; done
Mon Sep 14 06:44:12 PM EDT 2026   sbx-cite   claude   running   ...
...                                                    (running every ~1.5s)
Mon Sep 14 06:44:48 PM EDT 2026   sbx-cite   claude   running   ...
Mon Sep 14 06:44:49 PM EDT 2026   sbx-cite   claude   stopped
```

The only activity was the two `exec -- true` calls right before the loop
started; everything after that was a `wmf-sbx ls` poll, once a second,
from *outside* the sandbox. It stopped anyway, about 36–37 seconds after
the last real exec. **Listing/polling a sandbox's state does not reset
its idle clock** — the same rule §74 found for the MCP gateway's
45-second session timeout, on a different subsystem (whole-container stop
vs. one MCP session), landing in the same rough range.

This confirms §81.2 at face value and puts a number on "routine rather
than rare": half a minute of inactivity, not minutes, is enough to stop a
sandbox. `SECURITY.md` §4's residual restart window will now fire far
more often than the "someone typed `stop`" cadence it was written against.

**Still open**: the actual cost side of this — whether `git fetch`
correctly fails while stopped, how long `wmf-sbx-resume --verify --wait=20`
actually takes to bring it back, and whether 20 seconds is still enough
headroom. cananian's Ctrl-C landed right after confirming the "stopped"
state, before that half ran. Next commands, assuming `sbx-cite` is still
around:

```bash
wmf-sbx ls | fgrep cite                 # confirm it's still 'stopped'; if gone: sbx/bin/wmf-sbx-create Cite
time git fetch sbx-cite                 # expect failure — no daemon listening while stopped
time wmf-sbx-resume sbx-cite --verify --wait=20
wmf-sbx ls | fgrep cite                 # confirm 'running' again; note the new git-daemon port
sbx/bin/wmf-sbx-rm sbx-cite             # clean up the throwaway once done
```

## 85. The generated api-testing config works from core and from outside it — MEASURED **[in-sandbox `sbx-translate`, 2026-09-17]**

`write_api_testing_config` (design §5.5) writes core's
`.api-testing.config.json` from the install parameters plus `$wgSecretKey`
out of `LocalSettings.php`. Both routes the design claims were run against
the live wiki in this sandbox (`composer serve`, `http://localhost:4000`):

```bash
# from core: the library finds the file in the working directory
node_modules/.bin/mocha --timeout 0 tests/api-testing/action/Edit.js
#   23 passing (7s)

# from an extension: the absolute path in API_TESTING_CONFIG_FILE finds it
cd ../Extensions/Translate
API_TESTING_CONFIG_FILE=/…/core/.api-testing.config.json \
  /…/core/node_modules/.bin/mocha --timeout 0 /…/core/tests/api-testing/action/Edit.js
#   23 passing (3s)
```

So `base_uri` is the server root with a trailing slash (the library strips
the slash and appends `api.php` itself), and the docroot install needs no
script path between them. The file the trap in design §2.8 asked for is
this one, and the kit now exports the variable that finds it. No extension
in this sandbox ships `tests/api-testing/`, so the second run used core's
own spec from the extension's directory — which tests the same lookup.

## 86. `mw-install-browser`, and both browser suites — MEASURED **[in-sandbox `sbx-translate`, 2026-09-17]**

Design §5.3 asked for a helper that installs a browser on demand. Every
step was run by hand here first, then run again through the finished
helper:

```
chrome@stable        32 s   (@puppeteer/browsers, from the Chrome for Testing CDN)
chromedriver@stable   1 s
apt-get install      13 s   (ten packages)
~/.cache/puppeteer  416 MB
```

So the "about 1 minute, about 420 MB" the guide promises holds. A second
run takes 8 s: `install` is a no-op once the version is in the cache.

**Ten packages, not sixteen, and only four carry the `t64` suffix.** The
design said sixteen from a hand count. `ldd chrome | grep "not found"`
names exactly ten libraries on this image, and `apt-cache policy` gives a
candidate for six of them under their plain name:

```
libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libxkbcommon0
libasound2t64 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64
```

The helper therefore carries the plain names and falls back to `<name>t64`
when `apt-cache policy` reports no candidate, which is what a virtual
package left behind by the 64-bit `time_t` transition looks like. That
keeps the helper working on an image from before or after the rename.

Two traps found while writing it:

- `set -o pipefail` plus `apt-cache policy … | grep -q` reports the
  *pipeline* as failed: `grep -q` exits at the first match and
  `apt-cache` then dies of SIGPIPE. The candidate test took the `t64`
  branch for every package. Read the candidate into a variable instead.
- Progress messages must go to stderr. The helper captures the path the
  puppeteer CLI prints on stdout, so anything else on stdout ends up in
  the path it links.

Chrome needs no `--no-sandbox` here; its own sandbox works in this
container. It writes D-Bus and NetworkManager errors to stderr and runs
anyway, so the check reads the DOM, not the exit status.

Both suites then ran against the live wiki (`composer serve`), which is
what the browser was for:

```bash
cd "$MW_INSTALL_PATH"
MW_SCRIPT_PATH=/ CHROME_BIN=/usr/bin/chromium \
  npx grunt karma:chrome --qunit-component=Translate
#   9 tests completed  (Chrome Headless 153)

CI=true MW_SCRIPT_PATH=/ CHROME_BIN=/usr/bin/chromium \
  MEDIAWIKI_USER=Admin MEDIAWIKI_PASSWORD=adminpassword \
  npx wdio ./tests/selenium/wdio.conf.js --spec tests/selenium/specs/page.js
#   6 passing (10.3s)
```

The credentials are the ones §5.4 now exports; a wrong password fails in
the `before all` hook with `Could not login: Failed`, not with anything
about the browser. `MW_SCRIPT_PATH` had to be given by hand because this
sandbox predates the `/` default.

## 87. The first blind acceptance run — MEASURED **[`sbx-testverify`, cananian on the host + a blind agent, 2026-09-17]**

The run in design §9, with `sbx/acceptance/TESTING.md` as the only task
text. Repos: Translate, Cite, Parsoid, plus the `core`, `Vector` and
`UniversalLanguageSelector` checkouts the dependency walk added, all at
upstream master (`--reset-all`).

**It worked.** The agent found `~/MEDIAWIKI-TESTING.md` through `CLAUDE.md`,
ran every applicable suite in all six repos, installed a browser with
`mw-install-browser` (49 s), and ran karma, wdio and api-testing green
against the live wiki. Its first fully green suite came 1 min 38 s after
its first message. It read phan's undeclared-symbol errors as the expected
missing-sibling noise, which is what the guide says. It left cypress alone
and said why.

Counts it measured that the guide lacked: `--testsuite=core:unit` 18740
tests, 38 s; `--testsuite=parsertests` 2404 tests, 13 s; core `npm test`
about 60 s. These are now in the guide.

**Two findings, both real, both fixed in the commit that adds this note:**

1. **Core's `npm test` failed on a clean checkout, because of us.** The
   `.api-testing.config.json` that `wmf-sbx-setup` writes into core (§5.5)
   had four-space indents. Core's `.gitignore` hides the file from git, but
   core's `grunt lint` runs eslint over every JSON file, and the `indent`
   rule wants tabs: 7 errors, one per indented line. Checked here: the same
   content with tab indents passes `npx eslint` in core with no output.
   `api_testing_config_contents` now indents with a tab, and
   `write_api_testing_config` replaces a copy in the old four-space form
   (it is ours, not an engineer's edit), so `wmf-sbx-resume` repairs an
   older sandbox.

   The agent worked around it by adding the file to `.eslintignore`,
   running, and restoring `.eslintignore`. It reported the workaround, as
   the task list asked.

2. **Concurrent PHPUnit runs against one core break each other.** The
   agent ran several `composer phpunit:entrypoint` suites in parallel.
   Translate's failed with `no such table: unittest_searchindex` in
   `CloneDatabase::destroy`; alone, it passed (1137 tests, 4.8 s). The
   wiki is SQLite (`$wgSQLiteDataDir = "cache/"`), and every run clones
   its test tables under the same `unittest_` prefix in the same place.
   The guide now says to run PHPUnit suites one at a time per core
   checkout, and quotes the error.

Minor: the guide said 1712 structure tests, the run found 1713. Upstream
drift. The guide now says that counts move with upstream commits.

Not a finding: `$TMPDIR` was unset, so the long-`$TMPDIR` socket trap did
not appear. The guide presents it as conditional, which is right.

What the exported session log adds (`/export`, read after the fixes):

- **The agent was Sonnet 5**, not Opus. The guide was enough for the
  smaller model. Keep that in mind when you compare runs.
- **It ran 17 minutes** end to end, 34 suite runs.
- **It ran everything in parallel from the start**: five PHPUnit suites,
  four `composer test`, four phan runs, and `mw-install-browser`, all as
  background jobs. The PHPUnit collision (finding 2) came from that
  habit, not from a strange choice. It ran the karma suites one at a time,
  so it saw that they share a browser and a wiki.
- **Cite's karma run is in the log but not in the report table**: 3
  passed, 15 skipped, 1.5 s. A report-writing slip, not a sandbox finding.
- **The log hides the commands.** `/export` shows every tool call as "ran N
  shell commands". For the second run, copy out the session's `.jsonl`
  too. `sbx/acceptance/README.md` says where it is.
- **The `wmf-sbx exec -i -u agent … tee` copy line works** as written in
  the runbook [cananian, host, 2026-09-18], also in the `wmf-sbx exec`
  spelling. The runbook now uses that spelling.

## 88. The second blind acceptance run — MEASURED **[`sbx-testverify`, cananian on the host + a blind agent (Sonnet 5), 2026-09-18]**

Same repos and task list as §87, a new sandbox, with the §87 fixes in.
Inputs: `REPORT.md`, the `/export` log, and this time the session
`.jsonl`, which holds the actual commands.

**Both §87 fixes held.** Core's `npm test` passed on a clean checkout
(60.6 s). The agent ran PHPUnit suites one at a time and quoted the
guide's reason. First green suite at about 59 s.

Only 2 tool calls failed in the whole session. Three findings, all now
in the guide:

1. **`composer serve &` then an immediate `curl` gets 000 (exit 7).** The
   agent did put `sleep 2` between them; still refused. A later `sleep 3`
   got 200. Here, warm, the first 200 came after 0.36 s, so the delay is
   a cold-start cost. The guide's §5.1 now polls for up to 30 s instead of
   curling once, and the sbx environment text says to retry before
   reading a 000 as a failure.
2. **Translate's phan reports `\Spyc` undeclared, which the guide did not
   explain.** Spyc is a Composer library in Translate's own `vendor/`,
   which phan does not read. See the new "Merge extension Composer
   dependencies into core" to-do. The guide now names this cause beside
   the missing-sibling one. (Fixed in §89: the merge removes the cause.) The agent diagnosed it from
   `mediawiki-phan-config`'s source, correctly.
3. **Parsoid has an api-testing suite, which the guide did not mention.**
   `npm run api-testing` in `$PARSOID`: 87 passing, 48 pending, 3.1 s,
   against the running wiki with the generated config. Now in the guide's
   Parsoid section.

Timings the guide now uses: `--testsuite=core:unit` 18740 tests in 8.8 s
alone (§87's 38 s was under parallel load); `mw-install-browser` 24.7 s
(§87: 49 s).

Not findings: two slips of the agent's own (`VAR=x time cmd` in bash,
and a `tail -100` that hid output), which it reported as such.

## 89. Extension Composer libraries merged into core's `vendor/`, as quibble does — MEASURED **[in-sandbox `sbx-translate`, 2026-09-18]**

The fix for §88 finding 2, option 1 of its to-do, chosen by cananian.
`setup.py` `write_composer_local` writes core's `composer.local.json`
with quibble's `CreateComposerLocal` globs (`extensions/*/composer.json`,
`skins/*/composer.json`), tab-indented because core's eslint lints JSON.
`mediawiki_setup` now makes the symlinks *before* the composer loop, so
core's update sees the extensions. Every repo keeps its own
`composer update`, as in CI (cananian: "Keep the local composer update to
match quibble and keep local CI working"): quibble's
`ExtSkinComposerTest` and the `mwext-phan-*` jobs install inside the
extension too, and each `.phan/config.php` requires
`vendor/mediawiki/mediawiki-phan-config` from the extension's own
`vendor/`. phan still reads only core's `vendor/`, so it sees one copy of
each library. An engineer's own, different `composer.local.json` is left
alone with a warning, as for `.env`.

Measured by hand in `sbx-translate` with the same file:

- Core's `composer update`: 4.7 s. It added `mustangostang/spyc` (from
  Translate) and `composer/installers` to core's `vendor/`; nothing
  removed, no version changed.
- Translate `composer phan`: no `\Spyc` error any more. The errors left
  are the known missing-sibling ones (Elastica, other extensions).
- Translate PHPUnit from core: 1090 tests, OK, 3.5 s. The wiki serves
  (`Special:Version` 200) and siteinfo lists all five extensions.

**Parsoid, on purpose, differs from CI.** Quibble clones
`mediawiki/services/parsoid` into `services/parsoid`, which the glob does
not reach. Here, Parsoid's checkout is linked at `extensions/Parsoid`, so
the glob merges its `composer.json`. cananian: since it has an
`extension.json`, treat it as an extension. The cost: core also requires
`wikimedia/parsoid`, so each `Wikimedia\Parsoid\*` class is in two
places, and every core `composer update` or `dump-autoload` prints 359
`Ambiguous class resolution` warnings (all Parsoid, none other). Composer
uses the first, which is `extensions/Parsoid/src` -- the checkout, which
is what `link_parsoid_checkout` wants anyway.

**So the file also excludes `vendor/wikimedia/parsoid/` from the
classmap** (cananian: 359 warnings to ignore cost tokens, and Parsoid is
already a special case). Measured: core's `composer update` 3.2 s with 0
warnings; the classmap holds 359 `extensions/Parsoid` entries and no
vendor ones; PSR-4 lists the checkout first, then the vendor copy.
The exclusion is written **only when Parsoid is a workspace clone**
(cananian: without one, the wiki must use the library copy in
`vendor/wikimedia/parsoid`). PSR-4 would still find the vendor copy, but
outside the optimized classmap, for no gain. Setup treats either form of
the file as its own and rewrites it to the right one.
`Wikimedia\Parsoid\Parsoid` loads from the checkout; the wiki and
`rest.php/v1/page/Main_Page/html` answer 200; `--testsuite=parsertests`
2404 tests OK.

Side note, not new: `composer phpunit:config` from the `/home/agent`
alias of core dies with "Cannot redeclare class ComposerAutoloaderInit…",
because the wiki's files name the `/home/cananian` path and PHP loads
both. The guide already says to `cd "$MW_INSTALL_PATH"`.

## 90. The third and fourth blind acceptance runs, and `MEDIAWIKI_HAS_INTEGRATION_TESTS` — MEASURED **[`sbx-testverify` and `sbx-testverify-noparsoid`, cananian on the host + blind agents (Sonnet 5), 2026-09-18; checks in `sbx-translate`]**

Run 3 used §87's repo set, with the §89 merge in. Run 4 used a new set,
Flow and TemplateData with no Parsoid (Vector came in through the
closure), to check that the guide is not fitted to one set of repos.
Inputs: each run's `REPORT.md`, `/export` log and session `.jsonl`.

**The §89 merge held.** Run 3 had no `\Spyc` error. In run 4, core's
`composer.local.json` had no classmap exclusion, as intended.

**Run 3: most gaps were already in the guide.** The agent said the
guide lacks Parsoid's `--> NO UNEXPECTED RESULTS <--` signal, the Cypress
behind Cite's `selenium-test`, and the unrun Translate Jest files. All
three were there (§4, §3). Two were real and are now in:

1. The Bash tool starts each call in the primary repo, so a `cd` does not
   carry over. Now in guide §1.
2. The guide did not say to run `composer phan` in core. The agent ran it:
   clean, 78 s (80 to 110 s across runs). Now in guide §2, with the note
   that core has no sibling noise.

Run 3's one "genuine" Translate phan finding,
`UnusedPluginSuppression` at `src/TtmServer/ElasticSearchTtmServer.php:177`,
is the sibling gap and not the §89 merge. Elastica is in Translate's
`.phan/config.php` and absent here, so phan cannot type
`$resultset->getResults()`, and it does not raise the
`PhanPossiblyUndeclaredVariable` that the suppression is for. Reproduced
in `sbx-translate`: it is the only non-`PhanUndeclared*` issue.
`mw-install-browser` took 22.4 s; the guide now says 20 to 50 s.

**Run 4: five findings.**

- **A. A second `composer serve` fails, and §5.1's poll hides it.** A
  server from earlier in the session held port 4000. The new one exited
  with `Failed to listen on 127.0.0.1:4000 (reason: Address already in
  use)`, and the poll got 200 from the old one. §5.1 now tests for a
  running server first, logs to `/tmp/mw-serve.log`, and stops polling
  with the log when `kill -0 $pid` fails. Both branches checked here.
- **B. 17 REST failures in core's `npm run api-testing`**, all in
  `REST/Creation.js` and `REST/Update.js`: 400 and
  `The "Content-Type" parameter must be set.` The agent's cause was
  wrong. It said `php -S` does not set `CONTENT_TYPE` from a lowercase
  header. On PHP 8.5.4 it does (a raw socket test shows both
  `CONTENT_TYPE` and `HTTP_CONTENT_TYPE` for either case). The real
  cause is in core. Commit `0ca91a896ad` (2026-02-02, T412668) declares
  `Content-Type` as a `header` parameter of `EditHandler`.
  `Rest\Validator\ParamValidatorCallbacks::getValue()` reads
  `$request->getHeaders()[$name]`, an exact-name lookup, although
  `HeaderContainer` is case-insensitive everywhere else. Under `php -S`,
  `getallheaders()` keeps the names as sent, and api-testing's
  `REST.post()`/`put()` send `content-type` (captured raw: lower case, one
  header). So the lookup misses. `curl -H 'content-type:
  application/json'` to `rest.php/v1/page` gives the 400; `Content-Type`
  gives 201. It is a core bug, to report upstream (`hasParam()` has the
  same lookup). CI does not see it where the web server gives
  normalized names. Guide §6 item 7 now names the 17 failures and the
  cause. A task for it is not filed yet.
- **C. Flow's `npm run api-testing` runs `mocha`, and Flow does not
  depend on it**: `sh: 1: mocha: not found`. A repo defect. Guide §3 now
  says to read the script. It first said not to install mocha; cananian
  reversed that: an agent must install a missing test tool and run the
  suite, not skip it, and also report the gap. The guide now gives
  `npm install --no-save mocha`, which leaves `package.json` and the
  lock file as they are (checked here, npm with mocha 12.0.2).
- **D. The sibling gap reaches past phan.** Core's
  `--testsuite=structure` fails `ResourcesTest::testValidDependencies`
  (Flow's `ext.flow.visualEditor` names VisualEditor modules), and
  TemplateData's selenium spec passes with all 3 tests skipped (they need
  VisualEditor). Guide §3 now lists both and the phan suppression above.
- **E. `Flow\Tests\Import\TemplateHelperTest::testRemoveFromHtml` #3**
  expects `data-mw='...'` and gets `data-mw="{&quot;...`. This is an
  HTML serializer difference, probably the vendor Parsoid or its DOM
  library against Flow's expectation. Not a sandbox or guide matter, and
  not checked further.
- **F.** The guide did not say lint, phan and Jest can run at the same
  time across repos. Guide §2 now says so.

Run 4's time to first green was lost when the agent's context was
compacted.

**`MEDIAWIKI_HAS_INTEGRATION_TESTS=1` with a Parsoid clone** (cananian,
from [mediawiki.org/wiki/Parsoid](https://www.mediawiki.org/wiki/Parsoid)).
Core's `tests/phpunit/bootstrap.php` skips `LocalSettings.php` when it
guesses that a run holds only unit tests (a path under `/unit/`, or only
`core:unit`/`extensions:unit` suites). Then the Parsoid intercept that
setup writes into `LocalSettings.php` does not run, and unit tests use
`vendor/wikimedia/parsoid`. The variable (`1`, `true` or `yes`) turns
the guess off. `kit.parsoid_env_vars` sets it when the sandbox has core
and a writable Parsoid clone, the same condition setup uses for the
intercept (not for a `:ro` Parsoid). Core's `core:unit` gives the same
result with and without it here (18700 tests, OK), and the
`Running without MediaWiki settings` line goes away. It is in the kit
environment, so a sandbox made before this change does not have it.
Guide §0 and §4, and the kit's CLAUDE.md, say so.

## 91. Cypress runs in the sandbox with two CDN hosts and three apt packages — MEASURED **[in-sandbox `sbx-translate`, with `*.cypress.io` allowed for the test, 2026-09-18]**

Guide §6 item 6 and the kit say Cypress e2e tests cannot run here. That
is true only because the network policy blocks the binary download.
With `*.cypress.io` allowed on the host (temporary), Cite's real spec
passes. Hosts were logged with a small CONNECT proxy in front of the
sandbox proxy (`HTTPS_PROXY=http://127.0.0.1:3129`), which could also
refuse chosen hosts.

**Hosts.** The binary install uses exactly two:
`download.cypress.io` (`/desktop/15.20.1?platform=linux&arch=x64`) and
`cdn.cypress.io` (the redirect target, which the CLI follows itself).
At run time Cypress opens `api.cypress.io` and `cloud.cypress.io`. The
proxy refused both on every run and all specs still passed, so they are
not needed. Electron once opened `redirector.gvt1.com`, and Chrome opens
Google hosts (`accounts.google.com`, `update.googleapis.com`,
`clients2.google.com`, ...). These are browser background traffic, and
tests pass with them refused.

**Packages.** `cypress verify` first failed with `spawn Xvfb ENOENT`,
and `ldd` on the binary showed `libgtk-3.so.0` missing.
`sudo apt-get install -y --no-install-recommends xvfb xauth libgtk-3-0t64`
fixes both: 29 packages, 37 MB, 8 s, all from `archive.ubuntu.com`
(already allowed). After that `cypress verify` passes in 1.6 s with no
network. The bundled Electron is the default browser, so
`mw-install-browser` is not needed. Chrome for Testing also works, but
only by path (`--browser /usr/bin/chromium`); `--browser chrome` says
not found.

**Install.** Cite pins `CYPRESS_CACHE_FOLDER=./tests/cypress/.cache`
(gitignored) in each of its `cypress*` scripts. `npx cypress install`
with that variable, in the repo, takes about 13 s and unpacks to 812 MB
(Cypress 15.20.1). A repo without the variable uses `~/.cache/Cypress`.
Setup's `npm ci` keeps `CYPRESS_INSTALL_BINARY=0`, so nothing downloads
until an agent asks.

**Results.**

- A probe spec (edit a page, preview `<ref>`, check the footnote)
  passes in Electron (5 s) and in Chrome (6 s).
- Cite's `npm run selenium-test` with only the sandbox's extensions:
  exit 0 and 0 specs, 18 s. Cite's `setupNodeEvents` drops every spec
  whose extension (Popups, VisualEditor) is not loaded. This is the §90
  finding D sibling gap again; it passes and tests nothing.
- With Popups loaded (a depth-1 clone, linked into `extensions/`), the
  `referencePreviews` spec first failed in its `beforeEach`:
  `waitForModuleReady('ext.cite.referencePreviews')` got `registered`.
  Popups' `onBeforePageDisplay` adds no modules unless
  `areDependenciesMet()`, and with the default `PopupsGateway`
  (`mwApiPlain`) that needs TextExtracts and PageImages.
  `$wgPopupsGateway = 'restbaseHTML'` meets it, and Cite's reference
  previews do not use the gateway. Then the spec passes: 3 of 3, 12 s
  through `cypress:worker`, 26 s through `selenium-test`.

**Traps for the guide.**

- `cypress-parallel` (Cite's `selenium-test`) rewrites the tracked
  `tests/cypress/parallel-weights.json` on every run. Restore it with
  `git checkout` before a commit.
- `cypress-parallel`'s reporters print no error text. For the failure
  message, run one spec: `npm run cypress:worker -- --spec <file>`.
- Screenshots, videos (Cite sets `video: true`) and `runner-results/`
  land in the repo. All are gitignored.

**Plan** (done in §92):

- **(a) kit.** Add `download.cypress.io` and `cdn.cypress.io` to
  `EXTRA_DOMAINS` for the MediaWiki kit, not `*.cypress.io`: the run-time
  API hosts are not needed and would send run data out. Replace the
  "deliberately not here" comment (`kit.py`), the "cannot run here at
  all" line in `HOME_CLAUDE_MD`, and the `setup.py` docstring about
  `CYPRESS_INSTALL_BINARY=0`, which stays.
- **(b) `mw-install-cypress [<repo>]`**, next to `mw-install-browser`:
  apt-install `xvfb xauth libgtk-3-0t64` with `--no-install-recommends`
  if missing; in the repo, read `CYPRESS_CACHE_FOLDER` from its
  `package.json` scripts (else the default), run `npx cypress install`,
  then `npx cypress verify`; report the time and the size. One install
  per repo, because each repo can pin its own Cypress version and cache.
- **Guide.** Rewrite §6 item 6 and the §3 `selenium-test` note: install
  with `mw-install-cypress`, run the repo's script, use `cypress:worker
  --spec` for errors, give Chrome by path, restore
  `parallel-weights.json`, and say which specs a missing sibling
  extension skips (0 specs is not a pass).

## 92. `mw-install-cypress` and the two kit hosts — MEASURED **[in-sandbox `sbx-translate`, and bare `ubuntu:26.04` containers, 2026-09-18]**

This implements §91's plan (a) and (b), and the guide change.

**The apt list in §91 was short.** `xvfb xauth libgtk-3-0t64` worked in
`sbx-translate` only because `mw-install-browser` had already installed
Chrome's libraries there. `ldd` on the Cypress 15.20.1 binary, and a bare
`ubuntu:26.04` container, show that Electron also needs NSS and ALSA and
GBM. With only `xvfb xauth libgtk-3-0t64`, the binary stops at
`libnspr4.so: cannot open shared object file`. The measured set is
`xvfb xauth libgtk-3-0 libnss3 libasound2 libgbm1` (GTK brings ATK, Pango
and Cairo), with the same `t64` retry as `mw-install-browser`. With it,
`ldd` finds everything and `cypress verify` passes in the container.

**`cypress install` exits 0 when the download is refused.** With the
logging proxy refusing `download.cypress.io`, it printed
`Downloading Cypress`, stopped, and exited 0. So the helper does not
trust the exit status: it checks for `<cache>/<version>/Cypress/Cypress`
and prints the network message if the binary is not there.

**The helper** (`sbx/helpers/mw-install-cypress [<repo>]`, shipped and
installed like `mw-install-browser`):

1. It reads `CYPRESS_CACHE_FOLDER` from the repo's `package.json` scripts,
   relative to the repo, so that the repo's own `npm run` scripts find the
   binary. Without one, it uses `~/.cache/Cypress`.
2. It apt-installs the packages above that are missing.
3. It runs the repo's `cypress install`, checks for the binary, and runs
   `cypress verify`.

Measured: 12 s for a fresh install in Cite (812 MB), 1 s for a second run,
and a clear error for a repo without Cypress (Translate). In a bare
container with a stub `sudo`, it installed the six packages and passed.

**Kit.** `download.cypress.io` and `cdn.cypress.io` are in
`EXTRA_DOMAINS`. Not `*.cypress.io`, and not the run-time API hosts. §24
shows the `kit:` row as the only rule that allows the wiki family, so a
kit entry does allow a host that no local rule allows. The acceptance run
checks this for the two hosts with the host's temporary `*.cypress.io`
rule removed. Setup keeps `CYPRESS_INSTALL_BINARY=0`: now for size, not
because the download fails.

**Guidance.** The kit's CLAUDE.md now says that Cypress can run, that the
binary is about 800 MB, to install it only when the change is likely to be
covered by the repo's Cypress specs, that a run whose specs need missing
extensions passes with 0 tests, and to say so when it skips Cypress. Guide
§5.6 has the detail (how to decide, the extension check, the install, the
traps from §91). §3 and §6 item 6 now point there.

**With TextExtracts and PageImages also loaded,** Popups needs no
`$wgPopupsGateway` override. Cite's `selenium-test` gave 1 spec, 3 passing,
in 27 s. Of Cite's five specs, four are under `ve-cite/` and need
VisualEditor, and `templates.cy.js` also needs TemplateData.

**Acceptance.** `sbx/acceptance/README.md` has a third repo set (Cite,
Popups, TextExtracts, PageImages). Part A is the full `TESTING.md`. Part B
is two narrow tasks in a new sandbox, `TASK-php-change.md` (no Cypress
expected) and `TASK-preview-change.md` (Cypress expected), to measure the
"only when it helps" advice. `TESTING.md` item 6 now says "browser or
browser test tool", which does not name Cypress.

## 93. The Cypress acceptance run — MEASURED **[`sbx-testverify-cypress` and `sbx-testverify-cypress-judge`, cananian on the host + blind agents, 2026-09-18; checks in `sbx-translate`]**

The run follows `sbx/acceptance/README.md` "A third repo set, for
Cypress": Cite, Popups, TextExtracts and PageImages. The temporary
`*.cypress.io` rule was scoped `sandbox:sbx-translate`, so it did not
apply to these sandboxes.

**The kit row is enough.** `policy ls` showed `cdn.cypress.io` and
`download.cypress.io` in the `kit:sbx-testverify-cypress` row, and no
other Cypress rule. `mw-install-cypress` worked in both sandboxes: 15 s
for the binary, 21 s with apt, 812 MB in `tests/cypress/.cache`. It
installed only `xvfb xauth libgtk-3-0t64`, because `mw-install-browser`
had already installed the other libraries. apt printed debconf's
"unable to initialize frontend" lines, because no tty is present. Both
helpers now run apt with `DEBIAN_FRONTEND=noninteractive`.

**Part A** (the full TESTING.md): `referencePreviews.cy.js` 3/3; the
agent reported that the 4 `ve-cite` specs did not run (no
VisualEditor). It restored `parallel-weights.json`, but listed that as a
step it invented, so the guide now says why the file changes. Cite was
clean afterwards. Core api-testing gave the 17 expected failures (§90
finding B). Time to first green: 426 s.

**Part B, session 1** (PHP maintenance-script change): no install. The
agent grepped `tests/cypress/e2e` for the script, found nothing, and said
so. Host check: `no Cypress binary`. **Session 2** (reference-preview
fade threshold): the agent read the spec, checked the loaded extensions
with the §5.6 siteinfo command, ran `mw-install-cypress .`, and ran
`referencePreviews.cy.js` alone with `--browser /usr/bin/chromium`: 3/3,
including "includes scrollbar and fadeout on long previews". Both
decisions are the ones the kit asks for.

**A plan bug in Part B.** Session 1 left its branch checked out, and
session 2 first committed on top of it. The agent saw this, moved the
commit to a new branch from `origin/master`, and reset the session-1
branch. The README now checks out `master` in Cite between the sessions.

**Finding A: Popups' Node QUnit tests do not run, and report success.**
`npm run test:unit` (and so `npm test`) runs `mw-node-qunit ... |
tap-mocha-reporter dot`. On Node 22 (this image: v22.22.1),
`mw-node-qunit` 7.0.0 `src/dom.js:26` does `global.navigator = ...`, and
Node 22's `navigator` is a read-only global, so it throws `Cannot set
property navigator of #<Object> which has only a getter`. The reporter
then prints `0 passing (NaNms)`, and the pipe's exit status is the
reporter's: 0. Reproduced in `sbx-translate` on Popups `7aaa530`.
`NODE_OPTIONS=--no-experimental-global-navigator` turns the global off:
203 passing in 2 s. The guide (§3 Jest, §6 item 9) now gives the flag
and says to read the count of a piped script. CI presumably runs an
older Node; this is an upstream bug in `mw-node-qunit` and in Popups'
script (a pipe hides the exit status), to file.

**Finding B: Popups has a wdio suite.** The guide said no measured repo
had one. `CI=true TMPDIR=/tmp/wdio npx wdio tests/selenium/wdio.conf.js`
from Popups: 3/3, 7 s. The guide now says so.

**Smaller findings, and what changed:**

- phan sibling noise: PageImages 5, Popups 4, Vector 4, TextExtracts 0
  `PhanUndeclared*`. Added to the Cite count in §3.
- No list says which suites a repo has; the agent read `package.json`,
  `tests/` and `extension.json`. §3 now says that this is the method.
- Not changed: `~/.claude/CLAUDE.md` does not say where the workspace
  CLAUDE.md is; the shell `time` keyword with `VAR=x` prefixes (use
  `time env VAR=x ...`); Chrome's dbus lines on stderr, which are
  harmless; which API call to use for a check (a TASK wording matter).
- Core's structure suite ran 1613 tests here, not 1712: the set of
  loaded extensions sets the count.

## 94. Testing-instructions bookkeeping — DONE **[`sbx-translate`, 2026-09-18]**

`sbx/DESIGN-testing-instructions.md` is done except §5.6, and its status
line now says so. The upstream offers that §6.3, §6.4 and §6.5 named,
and the two bugs that the acceptance runs found outside wmf-claude, are
written up in `sbx/upstream/` as `PHAB-TASK-1.md` to `PHAB-TASK-5.md`,
each with a `PHAB-ATTACHMENT-<n>.patch`, and indexed in its `README.md`.
Each patch was measured:

1. **Core REST header case** (§90 finding B). `ParamValidatorCallbacks`
   now uses `hasHeader()`/`getHeader()`, which are case-insensitive.
   674 REST unit tests pass; a new `ValidatorTest` case fails without
   the fix. api-testing `Creation.js` + `Update.js`: 12 pass / 15 fail
   before, 27 pass after.
2. **wdio-mediawiki `MW_SCRIPT_PATH`**: `=== undefined`, as the
   Gruntfile does. Measured on the npm copy in `node_modules` (core's
   `wdio.conf.js` loads that one, not `tests/selenium/wdio-mediawiki`):
   `page.js` 6 passing with `MW_SCRIPT_PATH=`. Restored afterwards.
3. **Popups `mw-node-qunit`** 7.0.0 → 7.10.0, which fixes the navigator
   crash. It exposes one real failure (`settingsDialogRenderer >
   #render`, every run) and one flaky test (`wait`, 1 in 3). The task
   says so, and suggests `set -o pipefail` for the script.
4. **wmf-claude permission rules** (§70): against `origin/main`
   (`4b5316d`), keeping its nono paragraph in SECURITY.md.
5. **wmf-claude `run-tests`**: a copy of the plugin patch.
   `git apply --check` passes on `origin/main` for 4 and 5.

`sbx/acceptance/` is removed: the phase it served is over. Recover it
with `git show 54941cd:sbx/acceptance/README.md` (also `TESTING.md`,
`TASK-php-change.md`, `TASK-preview-change.md`). References to it in the
sections above are historical. The to-do list now has one item for the
upstream tasks, one for §5.6 (the two phan/dependency items merged), and
the optional items from §93.

## 95. Editing sbx's workspace CLAUDE.md — MEASURED + DONE **[`sbx-translate`, and `sbx-claudemd-probe` by cananian on the host, 2026-09-18]**

This closes the to-do "Say where the workspace CLAUDE.md is" (§93, Part
A finding d), and its follow-up, "amend sbx's file in place". The host
log is in `/home/agent/responses33.txt` in `sbx-translate`.

**Measured, sbx v0.43.0-rc3:**

- **Where.** sbx writes `dirname(<primary workspace>)/CLAUDE.md`. With
  Translate or Cite as primary, that is `.../Extensions/CLAUDE.md`. With
  `core` as primary (`sbx-claudemd-probe`), it is
  `~/Projects/Wikimedia/CLAUDE.md`. `find /home -name CLAUDE.md` shows
  only that file and `~/.claude/CLAUDE.md`. Owner `agent`, 18406 bytes,
  sha256 `74566b152ca3e8b9`, the same text in every sandbox.
- **When.** In `sbx-translate`: the kit install wrote its status file
  at 08:55:36.416, sbx wrote the CLAUDE.md at 08:55:37.671, and the
  startup log begins at 08:55:39.135. So sbx writes the file after the
  install steps and before the startup steps. An install step cannot
  edit it; a startup step can.
- **Kept.** The host appended a marker line to the file, then did
  `wmf-sbx stop` + `start`, `wmf-sbx resume`, and an idle stop + `start`.
  After each, the inode (567174), mtime, size and hash were unchanged,
  and the marker was still there. sbx writes the file once, at create.
- **Not yet measured:** an sbx upgrade (see "Still to do").

**Implemented:**

- `sbx/patches/sbx-claude-md/upstream.md` is sbx's text (the snapshot),
  and `edits.json` holds four section edits, keyed on headings: replace
  "Git workspace mode" (neither of sbx's modes; commit, the engineer
  fetches; the remote is `$SANDBOX_NAME`), replace "Git Authentication"
  with "Git remotes and pushing" (`origin` is Gerrit, `local` is the
  host checkout, no PR flow), remove ".NET Aspire", and shorten "Claude
  Code: Environment Persistence" to one paragraph. Each edit has a
  `contains` guard: text that must still be in the section.
- `wmf-sbx-setup --claude-md EDITS [CLAUDE.md]` applies them. A section
  runs to the next heading of the same or a higher level; headings in
  fenced code do not count; headings match after whitespace
  normalization. All edits apply or none do. Before the first edit it
  saves sbx's text to `~/.claude/wmf-sbx-upstream-CLAUDE.md`. The edited
  file starts with a marker comment that holds a hash of the edits, so
  any later run leaves the file alone. (The hash only records which
  edits were applied. An old sandbox keeps the edits file it was created
  with, so new edits reach only new sandboxes.) The write is atomic and keeps
  the owner and mode. The result goes to
  `/var/log/wmf-sbx-claude-md.status`, and the command always exits 0:
  a failed edit does not stop the sandbox.
- The kit runs it as a root startup step, after the claude.json step,
  so it runs on every start. The kit ships `edits.json` as
  `~/.claude/wmf-sbx-claude-md.json`.
- On the host, `wmf-sbx create` (generated kit), `resume` and `start`
  run it once more, and print the status file's problems through
  `report_setup_problems`, with the line "to fix: run `wmf-sbx
  refresh-claude-md NAME` ...". A sandbox without the edits file (made
  before this change) is skipped without a message. `wmf-sbx exec` does
  not run it.
- `wmf-sbx refresh-claude-md NAME` (or `--file PATH`) reads sbx's text
  out of a sandbox (the saved copy, else the live file if it is not
  edited), shows the diff from the snapshot, writes the new snapshot
  (not with `--dry-run`), and applies the edits. It exits 1 and names
  each edit that fails. A kit unit test applies the edits to the
  snapshot, so a snapshot that the edits do not fit fails the tests.
- `~/.claude/CLAUDE.md` now names the file's path (from the plan; with
  no plan, "the parent directory of the directory you started in"),
  says it is in context already, is in no repo, and is not to be edited
  or committed, and keeps the corrections for when the edit failed.

Against the snapshot, `wmf-sbx refresh-claude-md --file
.../Extensions/CLAUDE.md --dry-run` says "no change" and "all 4 edit(s)
apply".

**Measured in a new sandbox** [cananian, host, `sbx-claudemd-probe2`
with `core` as primary, 2026-09-18]. The create output lists the step
(`python3 /home/agent/wmf-sbx-setup --claude-md ...`, user 0) among the
10 startup commands. After create: the status file says `"exit": 0,
"problems": []`; the first line of `~/Projects/Wikimedia/CLAUDE.md` is
`<!-- wmf-sbx: edited by wmf-sbx-setup --claude-md (edits
da2888a84a9c) -->`; and `~/.claude/wmf-sbx-upstream-CLAUDE.md` holds
sbx's 18406 bytes. `wmf-sbx stop` + `start` printed no problem.
`wmf-sbx refresh-claude-md --dry-run sbx-claudemd-probe2` read the saved
copy (sha256 `74566b152ca3e8b9`), found no change from the snapshot, and
applied all 4 edits. (Inside `wmf-sbx exec`, `~` is `/home/agent`, so
name the workspace CLAUDE.md by its host path.)

## 96. The sbx v0.43.0 upgrade keeps the CLAUDE.md edits — MEASURED **[cananian, host, 2026-09-18; log in `/home/agent/responses34.txt` in `sbx-translate`]**

The upgrade was v0.43.0-rc3 → **v0.43.0** (final), not v0.45.0-rc3.
Nothing was destroyed: all four sandboxes were listed as `stopped` after
the daemon restart, and `sbx-translate` came back with `wmf-sbx resume`.

- **The restart prompt.** After the install, the new client found the
  old daemon (`server: v0.43.0-rc3`, `api_version 0.31.0`). A plain
  `wmf-sbx ls` in a terminal asked "Docker Sandboxes has been updated
  and needs to restart. All running sandboxes will be stopped. Restart
  now? (y/N)". `wmf-sbx start` (stdin is `/dev/null`, §47.8) did not
  hang: sbx exited 1 with "ERROR: ensure daemon: cannot prompt for
  restart: stdin is not a terminal". So the §47.8/§82.9 check passes.
  `start_sandbox` now adds a next step when it sees "needs to
  restart": run `wmf-sbx ls` in a terminal and answer y, with the
  warning that this stops every running sandbox.
- **The old sandbox keeps its edited file.** In `sbx-claudemd-probe2`,
  before and after the upgrade and the daemon restart: inode 173997,
  size 11211, mtime 2026-09-18 20:51:33, first line the wmf-sbx marker
  (`edits da2888a84a9c`). The status file after `wmf-sbx start` says
  `"exit": 0, "problems": []` — but that does **not** prove the startup
  step ran again on the start, because the create-time run writes the
  same file and nothing there carries a timestamp (§97). `wmf-sbx start`
  also printed
  "(re-applying sbx-claudemd-probe2's mount layout)", as expected after
  a daemon restart.
- **sbx's text did not change.** In `sbx-claudemd-probe3`, created on
  v0.43.0, `wmf-sbx refresh-claude-md` read 18406 bytes, sha256
  `74566b152ca3e8b9`: the same as rc3. No change from the snapshot, and
  all 4 edits apply. `edits.json` keeps `"upstream": "sbx v0.43.0-rc3"`,
  which is still exact.

Still open: the same check for v0.44 or v0.45 (see "Still to do").

## 97. A container start that ran no startup command, and the dead git daemon — MEASURED + FIXED **[in-sandbox `sbx-translate`, 2026-09-25; the start itself was 2026-09-19]**

cananian could not fetch from `sbx-translate`: "your git daemon doesn't
seem to be running anymore". It was not. No `git daemon` process, and
`connect_ex` on port 9977 refused.

**The cause is one level up: no startup command ran on that container
start.** The daemon is a startup command (§35.2), so it never came back.

- **The evidence.** The container booted 2026-09-19T17:11:18Z
  (`/proc/uptime`). The newest dispatcher banner in
  `/var/log/sbx-kit-startup.log` is `=== dispatcher run
  2026-09-15T16:53:24Z ===` — four days *older* than the boot. The only
  2026-09-19 line in any log is the `--restore` status at 17:12:05Z, and
  that one came from `wmf-sbx`'s own post-start restore pass on the
  host, not from the dispatcher.
- **The repair.** By hand, as the agent:
  `setsid nohup git -c safe.directory='*' daemon --reuseaddr
  --export-all --base-path=/home/agent --listen=0.0.0.0 --port=9977
  /home/agent`. Then `connect_ex` 0, and `git ls-remote
  git://127.0.0.1:9977/Projects/Wikimedia/wmf-claude` listed 10 refs.
  The host could fetch again.
- **The fix in `wmf-sbx`.** `create.py` gained `ensure_startup_ran(name)`
  and `ensure_git_daemon(name)`, and `resume.py::start_and_restore` calls
  both — so `wmf-sbx start`, `resume` and `exec` all get them, before the
  mount restore and before the remotes that need the daemon.
  `ensure_startup_ran` runs `STARTUP_RAN_PROBE` in the sandbox: it reads
  the boot time from `/proc/uptime` and the newest dispatcher banner from
  the log, and exits 1 when the newest run predates the boot. On 1,
  `wmf-sbx` runs `sudo sh /etc/durable-startup.d/run.sh` itself.
  `ensure_git_daemon` probes the port and, if it is closed, starts the
  daemon under `setsid`. Both are best-effort: they warn and continue,
  and `ensure_git_daemon` re-probes and says "the host can fetch nothing
  from it" when the port is still closed.
  **Why `setsid`, and why the kit step keeps its plain `exec`.** A
  process backgrounded inside an `sbx exec` dies when that exec ends, so
  the daemon the dispatcher starts under `ensure_startup_ran` does not
  outlive it. `ensure_git_daemon` therefore runs in a *later* exec, by
  which time that short-lived daemon is gone, and its own daemon gets a
  session of its own. The kit's startup step (`git_daemon_startup_command`)
  is left alone: under a normal container start it has kept a daemon
  alive for days, and `setsid` there would buy nothing this ordering does
  not already cover.
- **This probe says the same thing about this sandbox today.** Running
  `STARTUP_RAN_PROBE` in `sbx-translate` exits 1. So the fix would have
  caught it.

**What is *not* settled: why the start skipped them.** This sandbox was
created 2026-09-15 under **v0.43.0-rc3**; the daemon that started it on
2026-09-19 was **v0.43.0**, upgraded in between (§96). Everything
container-side is rc3-era — `run.sh` and every `NNN-cmd.sh` are dated
Sep 15 — but *running* them at container start is the host daemon's job.
Three explanations are alive, and none can be told apart from inside:

1. **A v0.43.0 regression that affects every sandbox.**
2. **The new daemon does not run startup for a container an older sbx
   registered** — so only sandboxes that predate an upgrade. The most
   likely of the three, because it needs no general regression and it
   explains why §46 held before.
3. **Startup never runs for a container started implicitly by `sbx
   exec`.** §46 measured the opposite on sbx 0.42.1, so this too would
   be a regression [cananian, 2026-09-25].

The fix does not depend on the answer: it tests the symptom (no
dispatcher run since boot; nothing listening on the port), not the
version. But do not write "sbx v0.43.0 does not run startup commands"
anywhere — the measurement is one pre-upgrade sandbox, one start.

**The evidence on the v0.43.0 upgrade is gone.** The two sandboxes that
straddled it, `sbx-claudemd-probe2` and `sbx-claudemd-probe3`, were
removed on 2026-09-25 before the check ran [cananian]. Nothing else on
the host was created under rc3, so this upgrade cannot be re-tested. The
check moves to the **next** upgrade, where the same shapes exist again:
see "Still to do".

**The experiment that separates the three** needs one sandbox created
*before* the upgrade and one created *after* it. Always use `wmf-sbx
--upstream exec` to read the log, because the new `ensure_startup_ran`
adds a dispatcher run of its own and spoils the evidence:

```bash
# This one-liner is the whole test: does the newest dispatcher run come
# after the boot?
SHOW='grep "dispatcher run" /var/log/sbx-kit-startup.log | tail -3;
      python3 -c "import time,datetime;u=float(open(\"/proc/uptime\").read().split()[0]);print(\"boot\", datetime.datetime.fromtimestamp(time.time()-u, datetime.UTC))"'

# 1. A sandbox created BEFORE the upgrade. Stop it, start it, and look.
wmf-sbx stop "$OLD" && wmf-sbx start "$OLD"
wmf-sbx --upstream exec "$OLD" -- sh -c "$SHOW"

# 2. A sandbox created AFTER it, the same way.
wmf-sbx stop "$NEW" && wmf-sbx start "$NEW"
wmf-sbx --upstream exec "$NEW" -- sh -c "$SHOW"
```

Read it like this: a run newer than the boot in **both** means the start
runs startup again, and the 2026-09-19 miss was a one-off of v0.43.0 (or
of the daemon restart that came with it). Newer in the new sandbox only
means explanation 2 — pre-upgrade containers are skipped. Newer in
neither means explanation 1, a general regression, and it is an upstream
bug to file. Do the runs with `wmf-sbx start`, and then repeat one with
`wmf-sbx exec` alone, to test explanation 3 as well.

## 98. Path-shortcut sandbox names — MEASURED, host-verified **[in-sandbox, 2026-09-26; host-verified 2026-09-29]**

Implements "Allow shortcut sandbox names" below for `wmf-sbx-resume`,
`wmf-sbx-rm`, `wmf-sbx-start`, and `wmf-sbx-exec`: a NAME argument
shaped like a filesystem path (`.`, `..`, `./x`, `../x`, or an absolute
path — `wmf_sbx.state.is_path_shortcut()`) is realpath'd and looked up
against every sandbox's recorded `primaryDir` (`wmf_sbx.state.
resolve_name_arg()`). One match resolves silently (with a `+ resolved
'.' to sandbox 'mw-cite'` note on stderr); zero or more than one is a
StateError, refusing rather than guessing.

`primaryDir` is a new field in the per-sandbox state file, written once
at `wmf-sbx-create` time from the same `primary_dir` local that already
existed there for other purposes — see sbx/DESIGN-host-remotes.md §2.
A sandbox created before this landed has no `primaryDir` and is only
reachable by its plain name, same as always.

Covered by new tests in `test_wmf_sbx_state.py` (the regex, `is_path_
shortcut`, `find_by_primary_dir`, `resolve_name_arg`) and one
resolve-through-to-the-real-call test added to each of
`test_wmf_sbx_resume.py`, `test_wmf_sbx_rm.py`, `test_wmf_sbx_start.py`,
and `test_wmf_sbx_exec.py`. Not runnable against a real `sbx` in this
dev sandbox (§47.3) — cananian still needs to check `wmf-sbx-resume .`
against a live sandbox on the host.

**Host-verified 2026-09-29** (§101): `wmf-sbx exec .`, `wmf-sbx start .`,
`wmf-sbx resume .`, and `wmf-sbx rm . --dry-run` all resolved `.` to the
sandbox whose primary workspace was the current directory, exactly as
designed.

## 99. `wmf-sbx cp` path shortcuts — MEASURED, host-verified; one bug found and fixed **[in-sandbox, 2026-09-26; host-verified 2026-09-29]**

Finishes §98 for `cp`. `sbx cp [flags] SRC DST` requires exactly one of
SRC/DST to be `SANDBOX:PATH` (reference: `~/Projects/Wikimedia/
docs.docker.com/reference/cli/sbx/cp` — its only own flag is
`-L`/`--follow-link`). `wmf_sbx.cp.resolve_cp_arg()` looks at each of
SRC and DST, splits on the first `:`, and — only when the part before
the colon is path-shaped (`wmf_sbx.state.is_path_shortcut()`) — resolves
that part alone with `resolve_name_arg()` and reassembles it with the
PATH half untouched. A bare host path (no colon) or an already-valid
`NAME:PATH` passes straight through.

`wmf-sbx cp foo .:bar` (the motivating case) now resolves `.` against
the sole sandbox whose `primaryDir` is the current directory and runs
`sbx --upstream cp foo <name>:bar`.

`cp` joined `REDIRECT_VERBS` in `bin/wmf-sbx`, `COMMANDS` in
`wmf_sbx/__main__.py` (a fourth place that lists these commands,
alongside `bin/wmf-sbx-<verb>` on disk and `test_wmf_sbx_dispatch.py`'s
own consistency check — the last of those caught the `__main__.py` miss
before it shipped). `bin/wmf-sbx-cp` is the usual thin entry point.

Covered by `test_wmf_sbx_cp.py`: `resolve_cp_arg` directly (plain path,
already-valid name, `.`-shortcut, absolute-path shortcut, a PATH half
with its own colons, no-match), and `main()` for the SRC-side and
DST-side shortcut cases, `-L`, `-D` placement, and exit-code forwarding.
Not runnable against a real `sbx` in this dev sandbox (§47.3) —
cananian still needs to check `wmf-sbx cp foo .:bar` against a live
sandbox on the host.

**Host-verified 2026-09-29, one bug found (§101):** upstream `sbx cp`
requires the container-side PATH to be absolute ("container path must be
absolute (use SANDBOX:/path)") and rejects it otherwise — but
`resolve_cp_arg` was reassembling `<resolved-name>:<PATH>` with PATH
untouched, so `wmf-sbx cp foo .:bar` still sent a bare `bar` upstream and
failed. Fixed: when NAME was a path shortcut, a relative PATH is now
anchored at that shortcut's own directory (`<primary_dir>/<PATH>`, or
just `<primary_dir>` when PATH is empty) — the sandbox's mount of that
repo sits at that same absolute path inside the sandbox (~/.claude/
CLAUDE.md's repo layout), the same as `sbx cp`'s own `NAME:/path`
examples expect. An already-valid `NAME:PATH` (NAME not a shortcut) is
left alone, since there is no directory to anchor to — write an absolute
container path there, same as plain `sbx cp` requires.

Also discovered live: **`sbx cp` does not support sandbox-to-sandbox
copies at all** (`wmf-sbx cp core:bar .:probe-back.txt` → "ERROR: copying
between sandboxes is not supported"). That is an upstream limitation, not
a resolution bug — see the to-do below.

## 100. GitLab project search, upstream and origin-URL identification — MEASURED live, unit-tested **[in-sandbox, 2026-09-27]**

Steps 1–4 of the GitLab plan, the resolver half of
`DESIGN-gitlab-integration.md`. Live findings against
`https://gitlab.wikimedia.org/api/v4`, **anonymous** (no token):

- The instance holds **4,427 projects**. Top-level groups: `repos`
  1,039, `toolforge-repos` 1,070, `cloudvps-repos` 5, `data-engineering`
  0, `people` 0. About half of all projects are personal-namespace forks,
  so a bare-name search across everything is mostly noise (a bare
  `wmf-claude` matches about twenty forks and one real repo).
- Works anonymously: `GET /projects` (keyset pagination through
  `Link: rel="next"`; offset pagination gives `x-total`),
  `/projects?search=`, `/groups/<g>/projects?include_subgroups=true&search=`,
  `/projects/<url-encoded path>`, and the `archived=true` filter (121
  archived projects in all).
- Does **not** work anonymously: `/search?scope=projects` and `/api/v4/mcp`
  both answer 401. The earlier to-do bullet that suggested
  `/search?scope=projects` was wrong.
- `simple=true` is smaller but drops `archived` and `forked_from_project`.
  The full record has `forked_from_project`. `mr_default_target_self`
  is absent or `None` for an anonymous caller.
- Latency is very high on this network: 4 s to 75 s per call (75 s for one
  100-project keyset page). Every design choice below avoids calls.

What is implemented (`resolve.py`, `create.py`):

1. `gitlab_search(substring)` mirrors `gerrit_search`: returns
   `(names, archived)`. It queries the groups `repos`, then
   `toolforge-repos`, and stops at the first group with a hit. Personal
   namespaces are never searched. The archived subset costs a second
   query (`archived=true`), made only when the first found something.
2. Bare names: `resolve_canonical_path(..., gitlab=...)` consults GitLab
   **only when Gerrit has no match**. Gerrit is where MediaWiki code is,
   and it answers fast. A name in both forges must be written
   `gitlab:<path>`. The same final-segment, prefer-non-archived,
   error-on-ambiguity rules apply (`_pick_exact`). `resolve()` turns the
   fallback on; a caller that passes its own `search` fake does not reach
   the network unless it also passes `gitlab`.
3. `reverse_resolve` falls back to the checkout's own `origin` URL
   (`git_origin_url` + `canonical_from_url`) after `.gitreview` and the
   exact rules. It recognises `https://gitlab.wikimedia.org/…` and the
   `git@gitlab-ssh.wikimedia.org` ssh forms, with or without `.git`.
4. `gitlab_upstream(path)` follows `forked_from_project` to the root
   project (depth limit 5, cycle-safe), unless `mr_default_target_self`
   is true. Unknown means "this project". `create.upstream_plan` uses it
   for `gitlab:` canonicals, so the sandbox clone's `origin` is the real
   upstream and not the engineer's fork. **Judgment call:** if you would
   rather keep `origin` on the fork, drop the `gitlab_upstream` call in
   `upstream_plan`. If GitLab does not answer, `upstream_plan` uses the
   canonical's own URL.

Tests: `GitlabSearchTests`, `GitlabFallbackTests`, `GitlabUpstreamTests`,
`CanonicalFromUrlTests`, `ReverseResolveOriginTests` in
`test_wmf_sbx_resolve.py`; two `UpstreamPlanTests` cases in
`test_wmf_sbx_create.py`. All use injected fakes. No live network.

Not done: token plumbing (`DESIGN-gitlab-integration.md` §2; needed for
private projects and a real `mr_default_target_self`); a full listing with
an on-disk cache (step 6); recorded-response fixtures from the live
instance.

**Spot-checked live 2026-09-28**, network back to normal speed (0.2 s–0.8 s
per call, down from 4 s–75 s): `gitlab_search("wmf-claude")` finds
`repos/product-safety-and-integrity/wmf-claude` (plus the `homebrew-wmf-claude`
sibling, no false hits from personal forks); `resolve_canonical_path`
falls back to it correctly; `gitlab_project` and `gitlab_upstream` on that
same project return cleanly (it has no `forked_from_project`, so it's its
own upstream, as expected for a repo nobody has forked); `canonical_from_url`
round-trips both the https and ssh forms of its own clone URL. Anonymous
`mr_default_target_self` is still `None`, confirming the token gap above.

## 101. Host verification of §98/§99: path shortcuts confirmed, `cp` PATH bug found and fixed — MEASURED on host **[2026-09-29]**

cananian ran the §98/§99 checklist against a real sandbox
(`wmf-sbx create ~/Projects/Wikimedia/core`, sandbox `sbx-core`). Full
transcript: `responses36.txt`. Results:

- `primaryDir` was recorded correctly in the state file, matching §98's
  design.
- The Gerrit `commit-msg` hook was present and executable in the
  sandbox clone (`test -x ...hooks/commit-msg`), and a throwaway commit
  made from inside the sandbox picked up a `Change-Id:` trailer — the
  actual behavior the write-commit-msg skill assumes. (The `diff`
  against the host mirror's hook produced no output, i.e. an exact
  copy — confirms the seeding from setup.py §8b1e972.)
- `wmf-sbx exec .`, `start .`, `resume .`, and `rm . --dry-run` all
  resolved `.` to `sbx-core` as designed (§98 done).
- `wmf-sbx cp` path shortcuts resolved the NAME half correctly, but
  every case with a *relative* container PATH failed upstream with
  `ERROR: container path must be absolute (use SANDBOX:/path), got
  "bar"` — `resolve_cp_arg` was leaving PATH untouched. **Fixed** (see
  §99): a relative PATH after a shortcut NAME is now anchored at that
  shortcut's own directory before being sent upstream, so
  `wmf-sbx cp foo .:bar` now sends `foo sbx-core:<primary_dir>/bar`.
  Covered by new cases in `test_wmf_sbx_cp.py`
  (`test_an_already_absolute_container_path_is_left_alone`,
  `test_no_path_at_all_resolves_to_the_shortcuts_own_directory`, and the
  updated existing cases). Not yet re-run against the live sandbox —
  the fix landed after this host session ended; re-verify next time
  (see the to-do below).
- The no-match case (`wmf-sbx cp foo .:bar` from `/tmp`) was refused
  before touching real `sbx`, as designed, with the expected message.
- `-D`/`-L` flag placement matched what was designed.
- **New limitation found, not a bug in our code:** `sbx cp` itself has
  no sandbox-to-sandbox copy — `wmf-sbx cp core:bar .:probe-back.txt`
  (both sides `NAME:PATH`) failed with "ERROR: copying between sandboxes
  is not supported". `wmf_sbx.cp` only resolves shortcuts; it never
  taught upstream `sbx cp` a capability upstream doesn't have. See the
  to-do below for what supporting this would take.

## 102. Round two of host verification: `cp` fix confirmed, GitLab remotes confirmed, and a literal-NAME gap found — MEASURED on host, FIXED **[2026-10-06]**

cananian re-ran the §99/§101 `cp` scenario and the §100 GitLab-remotes
scenario (both A and B from the "Git remotes for cloned non-Gerrit
repos" to-do item) against real sandboxes. Full transcript:
`responses37.txt`. Results:

- **`wmf-sbx cp` relative-PATH fix (test 1), confirmed.** Run against
  `sbx-translate`: a path-shortcut NAME (`.`) with a relative PATH now
  resolves and anchors correctly (`+ resolved '.:bar' to
  'sbx-translate:<primary_dir>/bar'`), the copy succeeds, and the
  already-absolute-PATH and empty-PATH cases both still behave as
  §99/§101 describe. No regressions.
- **GitLab remotes, test 2A (existing checkout, named by path),
  confirmed.** `wmf-sbx create ~/Projects/Wikimedia/wmf-claude` produced
  `origin` = `https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude.git`
  and `local` = the host mirror's `git://` URL, exactly as §100 designs
  it — including the fork-upstream case: the host checkout's prior
  `origin` was the user's personal fork (`cscott/wmf-claude`), and
  `reverse_resolve`/`gitlab_upstream` correctly repointed the sandbox's
  `origin` at the true upstream (`repos/product-safety-and-integrity/wmf-claude`),
  not at the fork. `origin` fetched successfully from inside the
  sandbox, and the independent host-side `sbx-wmf-claude` remote was
  still added to the host checkout as expected.
- **GitLab remotes, test 2B (bare name, fresh clone), confirmed.**
  `wmf-sbx create wmf-claude` with no path resolved the bare name via
  `gitlab_search`'s fallback and cloned from the true upstream, same as
  2A.
- **New gap found, not previously tested: a literal sandbox NAME
  (already valid, not a path shortcut) with a relative or empty PATH.**
  cananian tried `wmf-sbx cp responses37.txt wmf-claude-sbx:` against
  the real dev sandbox, using its actual name rather than a `.`/path
  shortcut, and it failed upstream the same way the original §101 bug
  did ("container path must be absolute"). This was not a timing issue
  — the fix that landed for §101 only anchored a relative PATH when NAME
  was a path shortcut (`state.is_path_shortcut`); it left a literal,
  already-valid sandbox NAME's PATH untouched by design, since
  `resolve_cp_arg` had no way to look up a plain NAME's own workspace
  directory. (`wmf-claude-sbx` specifically also predates the
  `primaryDir` field entirely — see §82.2/§98 — so it couldn't have been
  anchored this way regardless of the gap below; it does have exactly
  one `remotes` entry recorded, which could be a fallback source of a
  directory in the future, but that's out of scope here.)

  **Fixed**, with the user's explicit go-ahead (asked via
  AskUserQuestion; answer: "Yes, extend it (Recommended)"): added
  `state.primary_dir_for(name, env)` (NAME → recorded `primaryDir`, or
  `None` if there is no state for it, the state predates the field, or
  the name doesn't even parse as one) and taught `cp.resolve_cp_arg` to
  look a literal NAME's `primaryDir` up and anchor a relative PATH at it
  the same way it already does for a path-shortcut NAME — falling back
  to leaving PATH untouched (today's behavior) when `primaryDir` is
  unknown. Covered by new/updated cases in `test_wmf_sbx_state.py`
  (`PrimaryDirForTests`) and `test_wmf_sbx_cp.py`
  (`test_an_already_valid_sandbox_name_also_anchors_a_relative_path`,
  `test_a_sandbox_without_a_recorded_primary_dir_is_left_alone`, and the
  `MainTests` cases that used a bare `mw-cite:bar` destination). Full
  suite: 1022 passed. **Not yet re-run against a live sandbox** — the
  fix landed after this host session ended; see the to-do below. Because
  `wmf-claude-sbx` has no recorded `primaryDir`, re-running cananian's
  exact `wmf-sbx cp responses37.txt wmf-claude-sbx:` probe will **still**
  fail even with the fix — a sandbox created before `primaryDir` existed
  needs an absolute container PATH regardless of which form of NAME was
  used to reach it. Re-verify on a sandbox created *after* this fix.

## 103. The literal-NAME fix confirmed, and a `cp`-right-after-`create` race found and fixed — MEASURED on host, FIXED **[2026-10-06]**

cananian re-ran §102's extension against a brand-new sandbox
(`wmf-sbx create ~/Wikimedia/Extensions/MultiTitle`, sandbox
`sbx-multititle`), using the literal sandbox name rather than a `.`
shortcut. Full transcript: `responses38.txt`. Results:

- **The literal-NAME anchoring itself, confirmed.** `wmf-sbx cp
  /tmp/probe.txt sbx-multititle:bar` printed `+ resolved
  'sbx-multititle:bar' to
  'sbx-multititle:/home/cananian/Wikimedia/Extensions/MultiTitle/bar'` —
  exactly §102's fix. The empty-PATH and already-absolute-PATH cases
  with a literal NAME also matched §99/§101's shortcut-NAME behavior.
- **New bug found, flagged by cananian: the very first `cp` right after
  `create` silently did nothing.** The first `wmf-sbx cp
  /tmp/probe.txt sbx-multititle:bar` printed `Sandbox sbx-multititle
  started successfully`, reported success, and exited 0 — but the
  immediately following `wmf-sbx exec sbx-multititle -- test -f
  .../bar && echo copied` printed nothing, i.e. the file was not
  there. Re-running the *identical* `cp` a second time worked (`exec
  ... test -f` then printed `copied`).

  **Not a timing coincidence, and not specific to the literal-NAME
  extension** — this is §45.1's long-documented race, just reached
  through `cp` instead of `exec`: a sandbox `sbx cp` hasn't accessed
  yet is not running, the first access starts its container as a side
  effect (the "started successfully" line), and that start races the
  startup dispatcher's restore of the mount layout (§40/§46) without
  waiting for it. `wmf-sbx exec` already wins this race on purpose
  (`wmf_sbx.resume.start_and_restore`, with its own
  `+ (waiting for NAME's mount layout)` line — visible in this same
  transcript on every `exec` call) because every `wmf-sbx-exec` goes
  through it; `wmf-sbx cp` never did, since it only ever resolved
  arguments and handed off straight to upstream `sbx cp`. The first
  copy after `create` landed during (or just before) the mount swap and
  was discarded with it; the second ran against the settled mount and
  stuck.

  **Fixed**: `wmf_sbx.cp.main` now calls `start_and_restore` (the same
  function `wmf-sbx exec` uses, with `claude_md=False` the way `exec`
  passes it) for every sandbox named on either side of the `cp`,
  *before* handing off to upstream `cp` — not only for the literal-NAME
  case this round happened to be testing, but for a path-shortcut NAME
  and an unknown NAME too, since all three equally start a stopped
  container as a side effect. New helper `cp._sandbox_name_in()` reads
  the NAME half back out of an already-`resolve_cp_arg`'d SRC/DST (so
  it always sees a real sandbox name, never a path shortcut) and
  `validate_name()`-filters it, so a plain host path that happens to
  contain a colon is not mistaken for one.

  `test_wmf_sbx_cp.py`'s `MainTests` rewritten to match: `setUp` now
  mocks `create_mod.refresh_host_port` the same way
  `test_wmf_sbx_exec.py` does, `fake_run` grew the same three-way branch
  (the `exec ... true` start probe, the `wmf-sbx-setup`
  verify/startup-ran calls, and everything else) exec's own tests use,
  and every case that resolves to a real sandbox name now expects the
  four `start_and_restore` calls ahead of the actual `cp` command. Two
  new cases: a sandbox that refuses to start is an error and `cp` never
  runs (mirrors `test_wmf_sbx_exec.py`'s own case for the same
  failure), and a sandbox with no recorded state at all still gets
  started (resolve_cp_arg already leaves its PATH alone, but it's a
  live container `sbx exec ... true` can still be asked to start).
  Full suite: 1024 passed. **Re-verified live, 2026-10-06**
  (`responses39.txt`): the exact repro from `responses38.txt`, repeated
  against a fresh `sbx-multititle` with this fix applied -- the first
  `cp` now shows the `exec ... -- true` / mount-layout wait before the
  actual copy, and the immediately following `exec ... test -f &&
  echo copied` printed `copied` on the first try.

## 104. Rebase onto upstream 0.78 main, and the upstream MRs — host-verified, MRs OPEN **[2026-10-08]**

`work/cscott/sbx` is rebased onto upstream `main` 10aefc1 (nono 0.78),
following `HANDOFF.md` (in cananian's checkout, not committed). The
pre-rebase tip is kept as `work/cscott/sbx-pre-rebase` (f312704).
Host-verified: the unit suite (1027 OK, `responses43.txt` and later),
`tests/test-templates.sh`, both SessionStart diffs, the kit domains read
from the 0.78 profile, and HANDOFF §5's live sandbox check.
`tests/test-profile.sh` fails on the host on both trees for a reason
outside this branch: nono refuses the profile with "Landlock deny-overlap
is not enforceable on Linux", because the deny on
`~/.cargo/credentials(.toml)` sits under the `~/.cargo` allow of
`group:rust_runtime`. That is upstream's to fix (a #WMF-Claude task).

### 104.1 The upstream merge requests

The backend-neutral parts of the branch went upstream as three MRs, each
cut from upstream `main` 10aefc1, with no `sbx/` files. The source
branches are in cananian's fork; the same commits are in the sandbox
clone as `mr/*`.

| MR | Source branch | Commits | What |
|---|---|---|---|
| [!132](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/132) | `session-start-seam` | 9d6ba6f, 7fbd0bd | HANDOFF MR A + B: `standalone-vuln-audit` in `package.json` and the hook, the three-way skill-list test; then the SessionStart backend seam, `hooks/context/nono/`, the byte-identical fixtures |
| [!131](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/131) | `setup-config` | 94f6f15 | HANDOFF MR C: the Phabricator username in `~/.config/wmf-claude/config.json` |
| [!130](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/130) | `run-tests-entrypoint` | 7f61e7e | HANDOFF MR D: `composer phpunit:entrypoint` in `run-tests` |

Check their status (read-only, works from a sandbox):

```bash
P='https://gitlab.wikimedia.org/api/v4/projects/repos%2Fproduct-safety-and-integrity%2Fwmf-claude'
for m in 130 131 132; do
  curl -s "$P/merge_requests/$m" \
    | jq -r '"!\(.iid) \(.state) \(.merge_commit_sha // .squash_commit_sha // "-") \(.title)"'
done
```

The MR versions differ from what the branch carries:

- **!132 has no `WMF_CLAUDE_DOCKER_MODE`.** It could not be justified
  upstream without sbx, and sbx does not need it: the kit leaves it
  unset, and with no broker URL the hook already emits no `mwdocker`
  block. The branch's `session-start.sh` still has the
  `broker|native|none` switch and its tests.
- **!132 checks the backend name** against `^[a-z0-9][a-z0-9_-]*$`, so
  that it cannot select a file outside `hooks/context/`. `sbx` passes.
  Its header comments are rewritten in STE.
- **!131 writes to `$HOME/.config/wmf-claude`**, not
  `$XDG_CONFIG_HOME/wmf-claude`, to match the rest of the repo (the
  sandboxed agent cannot write there). It adds six tests of
  `config_get`/`config_set` in `tests/test-templates.sh`. Its comments
  are rewritten in STE.
- **!130 is the plugin patch applied unchanged.** It was not run
  against a live wiki: in the wmf-claude sandbox, core's skins are
  symlinks to host paths that are not mounted, so `phpunit:config`
  fails before PHPUnit starts.

### 104.2 What to do when each one merges

Rebase `work/cscott/sbx` onto the new upstream `main`. Per MR:

- **!132:** take upstream's `bin/session-start.sh`,
  `hooks/context/nono/*`, `tests/fixtures/session-start/*`, and the
  seam and skill-list sections of `tests/test-templates.sh`; drop the
  branch's versions. Then decide about `WMF_CLAUDE_DOCKER_MODE`: the
  simplest course is to drop it from the branch too (and the `native`
  tests, and its sentence in `CLAUDE.md`), since nothing sets it. Keep
  `sbx/plugin-overlay/hooks/context/sbx/`. Re-check that a kit hook
  still prints "sandboxed by sbx" and none of nono's GET-only,
  `--allow-post` or `--local-db` text
  (`PluginTreeTests.test_the_shipped_hook_speaks_for_the_sbx_backend`).
- **!131:** take upstream's `bin/wmf-claude-setup` and README
  paragraph; drop 18dcfc7's versions.
- **!130:** **delete
  `sbx/patches/plugin/01-run-tests-composer-entrypoint.patch` in the
  same rebase.** Once upstream has the change, the patch no longer
  applies and the kit build fails. Update `sbx/upstream/README.md`'s
  MR D line and `sbx/DESIGN-testing-instructions.md` §2.1 to say it
  landed.

If a reviewer asks for changes, amend the `mr/*` commit (or the fork
branch), not `work/cscott/sbx`; the branch picks the result up at the
rebase. After all three merge, the only non-`sbx/` diffs left against
upstream should be `SECURITY.md`'s pointer paragraph and `.gitignore`.

## 105. Phase 1 of the Lima port: rebase onto main + !127 + !130–!132 **[2026-10-09]**

Phase 1 of `lima-port/HANDOFF-LIMA.md` §11, done in a Claude Code cloud
session (not in an sbx sandbox). Branches, both pushed to the GitHub
mirror (`cscott/wmf-claude`):

- `lima-port-base`: upstream `main` 10aefc1, then Kosta's !127 rebased
  (clean), then !132, !130 and !131 as cherry-picks, then one lint fix
  for !127 (`23e921c`, kept separate so that it can go to Kosta). !131
  conflicted with !132 only in `tests/test-templates.sh`: each adds its
  own section at the same place; both are kept.
- `lima-port`: `lima-port-base`, then this branch's sbx commits, then the
  `lima-port/` handoff documents.

What changed in the replay of the 12 sbx commits (§104.2 followed):

- **79189f9 (Seam 1)** is reduced to `hooks/context/sbx/*` and their
  `package.json` entries. The hook, the nono files, the fixtures and the
  skill-list test come from !132. `WMF_CLAUDE_DOCKER_MODE` is removed:
  nothing sets it (the kit asserts that it is unset). Do not drop the
  commit completely: 0e9d60e and 833633b edit the files it adds.
- **18dcfc7 (setup config)** is dropped for !131. Consequence:
  `wmf_claude_config()` in `create.py` read `$XDG_CONFIG_HOME`, which
  !131 does not use. Fixed in its own commit; the tests isolate the file
  through `HOME`.
- **!130:** `sbx/patches/plugin/01-run-tests-composer-entrypoint.patch`
  is deleted. Without that, 23 kit tests failed ("does not apply").
- All other sbx commits applied cleanly. After them, `sbx/` was
  byte-identical to ae82db0, and outside `sbx/` the branch differed
  from `lima-port-base` only in `SECURITY.md` and `.gitignore`, as §104.2
  said it would.

Results on `lima-port` (Linux, x86_64):

- `python3 -m unittest discover -s sbx/tests`: 1027 tests, OK.
- `tests/test-templates.sh`: 279 passed, 0 failed (includes the
  byte-for-byte SessionStart fixtures).
- `tests/test-lima.sh`: 102 passed, 0 failed, as a non-root user with
  `limactl` 2.2.1 and `shellcheck` 0.11.0. Before `23e921c`, two
  shellcheck checks failed; the CI image installs neither tool, so CI
  skips them.
- `tests/test-profile.sh`: fails, for the reason in §104 (nono refuses
  the profile on Linux: the `~/.cargo/credentials` deny under the
  `group:rust_runtime` allow). The profile files are the same as on
  `main`, so this branch does not change the result.

Open MR not taken: !102 (jforrester, draft, remote build broker). It
does not touch Lima, and it conflicts with !127 in `bin/claude`.

## 106. Phase 2 of the Lima port: the golden image builder **[2026-10-09]**

`wmf-sbx image build|ls|rm|prune`, in `sbx/src/wmf_sbx/image.py`,
`lima.py` and `image-build.sh`, on branch `lima-port` (6406d32, 087dfd6,
3592303). Decision D1 (A): a Lima builder VM from the Debian 13 image
that `lima/wmf-claude.yaml` pins. Run in a Claude Code cloud sandbox:
Linux x86_64, Lima 2.2.1, QEMU 8.2.2 **without KVM** (TCG), so the times
are slow and only comparable with each other.

What the build does: apt packages (`kit.BASE_PACKAGES` plus the tools,
Node, nftables and the Chrome for Testing libraries), nono 0.78.0
(checked against `SHA256SUMS.txt`), Claude Code from the `stable`
channel (2.1.286, checked against the release manifest; not the
`install.sh` pipe), the wmf-claude tree at HEAD, built, root-owned, in
`/opt/wmf-claude.<rev>`, and the helpers in `/usr/local/bin`. Then it
seals (host keys, `cloud-init clean --logs --seed --machine-id`, the
builder's user and sudoers file, the build-time CAs) and creates
`agent` with the host uid and gid (D10). The export is `qemu-img
convert -O qcow2`; the entry is renamed into place, so a failed build
leaves nothing.

Results:

- Build: 1563 s (26 min, TCG). Image `d16a1c5496fa83fe`: 2.3 GiB on
  disk, 20 GiB virtual, qcow2 with no backing file, mode 0444, SHA-256
  in `golden.sha256`.
- Two sandboxes (`images:` = the golden file, a full copy as D2 says):
  `limactl create` 7 s and 10 s (2.4 GiB copy each); both booted to
  READY in 74 s together. Different machine-ids and ed25519 host keys.
  `agent` uid 30033 = host uid, no other groups, home 0750; Lima's user
  `engineer` at the template's `user.uid` 59998. No `wmfbuilder`; no
  build-time CA left. nono 0.78.0, Claude Code 2.1.286 (the agent can
  run it), Node v20.19.2, PHP 8.4.26, Composer 2.8.8, all three MCP
  servers built.
- `image build` again: "in the cache" in 2 s, no VM. The golden
  checksum is unchanged after both sandboxes booted from it.

Found on the way:

- **Lima gives its user the host uid** unless the template sets
  `user.uid`. The first build's `wmfbuilder` got 30033. The builder now
  uses 59999, and every sandbox template must set another uid too,
  because `agent` has the host uid.
- **The network re-signs github.com** (the cloud sandbox's interception
  CA; a corporate TLS-inspection proxy does the same). The first build
  stopped at the nono download. `WMF_SBX_CA_CERTS` gives the builder
  extra CAs through Lima's `caCerts`; the seal removes them.
- `sudo` in the builder drops the proxy variables (as in the MR !127
  baseline), so the build passes them explicitly, with a loopback proxy
  rewritten to 192.168.5.2, as Lima does.
- **Debian 13 has Node 20**, not the Node 22 that MediaWiki CI uses
  (`DESIGN-setup-steps.md` §7.2 measured 22 on Ubuntu). **Decision
  (cananian): install Node 22** in phase 5, decoupled from the OS
  version: nave, fresh-node, or a pinned newer Debian/Ubuntu package
  (HANDOFF-LIMA.md §11, phase 5).
- Not done in phase 2: `status` (phase 3), `vz` (the Mac list for
  Kosta), and the sbx plugin overlay and patches, which belong to the
  session (phase 6).

## 107. Phase 3 of the Lima port: the sandbox lifecycle **[2026-10-09]**

Decision (cananian): **Lima only from phase 3.** The verbs keep no
Docker path; the `sbx-docker-final` branch has the Docker backend.
Branch `lima-port`, commits f788b81 (template), b0809e1 (lifecycle) and
the exec fix after it.

What there is now:

- `template.py` + `sandbox-provision.sh`: the per-sandbox Lima config
  (closed like Kosta's; `plain: false`; read-only git-dir mounts under
  `/run/wmf-sbx/host/`; Lima's user `engineer` at uid 59998) and
  Kosta's security provisioning (no sudo/docker for the agent, the host
  and LAN block, agent egress TCP 80/443, no TIOCSTI). A loopback proxy
  in the host's proxy variables gets exactly its port through the
  block.
- `vm.py`: instance `wmf-sbx-NAME`, create/start/stop/delete, agent
  commands, and six invariants that `create`, `start` and `status`
  check.
- `state.py`: `backend: "lima"`, the image key, the driver, the repos;
  `require()` refuses a name with no state file (or a Docker one).
- Verbs: `create`, `start`, `stop`, `status`, `ls`, `exec`, `cp`, `rm`,
  `image`, `resolve`, `ls-remotes`. `resume`/`run`: "not ported yet"
  (phase 6). `bin/wmf-sbx` never calls Docker `sbx`.

Real round trip (Linux, QEMU TCG, Lima 2.2.1, image d16a1c5496fa83fe):

- `create --no-deps --image ... --name demo ~/repos/demo`: 313 s (the
  2.4 GiB copy, the resize to 60 GiB and the first boot). All six
  invariants ok.
- `exec`: runs as `agent`, uid 30033 (the host's), only group `agent`,
  the fixed PATH; `sudo` asks for a password; exit status 7 came back
  as 7. `--engineer -w /tmp` is refused.
- `cp`: a 300 KB binary and a directory tree in and back out; SHA-256
  equal; owner `agent` in the guest.
- `stop` + `start`: 126 s; invariants ok after the reboot.
- `rm`: "n" leaves everything; "y" deletes the VM and the state;
  `rm golden` (a Lima VM that wmf-sbx did not make) is refused.

Found: `exec` without `-w` ran in the primary workspace, which does not
exist in the VM before phase 4 clones it (env exit 125). It now falls
back to the agent's home for the default only.

Left for later phases: mounts and host remotes (4), MediaWiki setup and
Node 22 (5), the session (6). The Docker-only helpers in `create.py`
(kit, ports, `sbx` calls) are now dead code with their tests; delete
them in C1.

## 108. Phase 4 of the Lima port: git transport and mounts **[2026-10-09]**

Branch `lima-port`: f2020a9 (kernel), 06cfab2 (phase 4), da89f39
(submodule check), and the commit that records this.

What there is now (lima-port/HANDOFF-LIMA.md §6):

- `repos.py` + `sandbox-repos.sh`: each repo's git dir (`git rev-parse
  --git-common-dir`) is mounted read-only under `/run/wmf-sbx/host/`.
  In the VM, a `git clone --shared` of the mount is at the host path,
  on the host's branch, with `origin` = upstream and `local` = the
  mount; dependencies are reset to upstream. A `:ro` repo is cloned by
  `engineer`, so the agent can read it and not write it.
- `remote_helper.py` + `bin/git-remote-wmfsbx`: `wmfsbx://NAME/PATH`.
  `connect git-upload-pack` only; upload-pack runs as the agent through
  `limactl shell`, hooks off. Push is refused.
- `create` adds a host remote with the sandbox's name to each repo and
  suspends gc (`remotes.py`); `rm` removes the remotes and restores gc.
- A seventh invariant: every git dir in `/etc/fstab` is mounted.

Found on the real VM (Linux, QEMU TCG, Lima 2.2.1):

1. **Debian's cloud kernel has no 9p and no virtiofs.** The first create
   wrote the fstab entries and mounted nothing, so the clone step
   stopped ("not mounted"). With `linux-image-amd64` installed and the
   cloud kernels purged, the mounts came up after a reboot. The golden
   image now does this, and the build checks the modules (f2020a9).
   The phase 0 9p measurements had run on an Ubuntu cloud image, whose
   kernel has 9p.
2. **9p `fscache` goes stale.** After the host moved a loose ref into
   `packed-refs`, the guest still read the old loose ref, and `git
   fetch local` got the old commit. With `9p.cache: none` a host
   commit, `pack-refs`, a full repack and `gc --prune=now` were each
   seen at once; `git log --all` over the mount took 0.5 s. Lima writes
   the option to fstab at boot, so `limactl edit` takes effect one boot
   later.
3. **A `:ro` clone needs `safe.directory` for PATH/.git too.**
   upload-pack opens it by that path; without it, "dubious ownership",
   and the `rm` guard refused because it could not probe.
4. **The phase 2 image had no MCP servers.** The checkout that built it
   had no submodules checked out, and `git submodule foreach` skips
   those without a word. The build now refuses such a checkout
   (da89f39).

Checked and fine: the agent cannot write the mount; guest root's
`remount,rw` shows `rw` but a write fails and the host file does not
change (the "no mount is writable" invariant then reports FAIL, as it
should); `uploadpack.packObjectsHook` in the agent's repo config does
not run on a host fetch; push is refused; the `rm` guard names the
unfetched commit; host `git safe-reset NAME` works; `rm` leaves the
host config with no remote, gc key or `wmfSbx` marker.

`lima-port/checks/phase4.sh NAME PRIMARY [RO_REPO]` runs the checks on
a live sandbox (for Kosta on `vz`, too).

Clean run from the rebuilt image (`f0d9a41e9170ed39`, kernel
6.12.111+deb13-amd64, the MCP servers in `/opt/wmf-claude.*`):

- `image build`: 2442 s (41 min, TCG; phase 2 took 26 min, without the
  kernel swap).
- `create --no-deps --name p4 ~/src/BoilerPlate ~/src/dep:ro`: 247 s,
  the seven invariants ok, both clones made, both host remotes added,
  gc suspended in both repos.
- `lima-port/checks/phase4.sh p4 ~/src/BoilerPlate ~/src/dep`: 18 of
  18 ok.
- `rm p4`: the VM, the remotes, the gc keys and the markers are gone.

Left: the nono `--read /run/wmf-sbx/host/…` grant comes with the session
(phase 6). Host `git maintenance` and alternates are still unverified.

## Still to do

- [x] Implement `sbx/DESIGN-setup-steps.md` — everything after the
      symlinks: `git safe-reset --force` in every non-primary parallel
      clone, `composer update`, `.env`, `npm ci`, and
      `composer mw-install:sqlite -- --with-extensions` in the core clone.
      **DONE** 2026-09-07 (§29). Unit-tested only: every step is a `run=`
      injection point, so the tests pin what runs and in what order, not
      that composer and npm succeed against a real checkout.
- [x] **Run the whole chain in a real sandbox.** **DONE** 2026-09-08
      (§30): `wmf-sbx-create Translate` → a working wiki serving
      `Special:Version` with all three repos loaded, in 65s. `sudo -u
      agent -H`, `--with-extensions`, `composer serve`, the daemon, the
      host remotes and the read-only mounts all behaved. Three bugs found
      and fixed (`MW_INSTALL_PATH` aimed at the host mirror, `sudo chown
      -h` silently leaving root-owned symlinks, `--reference` copying a
      685 MB object store per clone). The fixes themselves are so far only
      unit-tested — **the next `wmf-sbx-create` should re-check §30's
      list**, in particular that `MW_INSTALL_PATH` now points under
      `/home/agent`, that `php maintenance/run.php version` works with no
      override, that the symlinks come out `agent agent`, and that
      `core/.git` is now megabytes rather than hundreds of them.
- [x] **Decide what `git safe-reset` should reset a dependency clone
      *to*** (§30). **DONE** 2026-09-08 (§31.2), and confirmed in a real
      sandbox the same day (§32): `git remote -v` shows `local` = host
      mirror and `origin` = Gerrit in every clone, and core and Vector
      both landed on `origin/master`. Parsoid did **not**, and that
      exposed a regression the swap itself introduced — an ambiguous
      DWIM `git checkout master` with two remotes and no local branch;
      fixed with `checkout.defaultRemote` (§32.1), so far only
      unit-tested.
- [x] **Make a failed setup step visible** (§32.1). **DONE** 2026-09-08
      (§38), cananian's call: log to `/var/log` and have `wmf-sbx-create`
      read it back with an `exec` after create. Unit-tested and smoke-run
      against a scratch tree; not yet seen on a real `sbx create`.
- [x] **Fix the starting working directory** (§30). **DONE** 2026-09-08,
      cananian's call: `mount --move` the host mirror to
      `~/.sbx-originals/<rel>`, clone from there, and bind the clone over
      the path it vacated, so the starting cwd *is* the writable clone and
      nothing has to `/cd`. `HOME_CLAUDE_MD` no longer asks for the `/cd`
      it can't do. Setup side §39, **confirmed on the host** the same day
      (§40: same inode for the alias and the clone, writable, 6707
      commits). Stop/resume side §40: neither mount survives `sbx stop`
      (§31.1) and the clone drops to 5 reachable commits, so a
      `wmf-sbx-setup --restore` startup command (`user: "0"`, first in
      `setup.startup`) redoes them from a `layout.json` written at setup.
      Confirmed on the host (§40).
- [x] **Make the restart pass actually restore** (§41 → §42). One sandbox
      came back up with an exit-0 report and none of its mounts. Fixed by
      making the pass falsifiable (narration per repo, plus `verify_repo`
      asserting the alias, the moved-aside mirror and every alternates
      path) and by having `wmf-sbx-resume` run it over `sbx exec` as well.
      **Confirmed on the host** 2026-09-08 (§42): create → stop → resume
      leaves the clone at the host path, the mirror read-only, and 7765
      commits reachable, with the startup command doing the work and the
      resume-side pass finding nothing left to do. The §41 failure itself
      was never reproduced; if it recurs the log now names the path taken.
- [x] **The read-only lock-down doesn't survive a restart** — found while
      measuring §40 and *older than §39*: the `remount,ro,bind` on a host
      mirror is namespace state, so every resumed sandbox has had its
      primary workspace's host directory writable from inside. `--restore`
      closes it for generated kits — **confirmed on the host** (§42: a
      `sudo touch` of the mirror after a resume gets "Read-only file
      system"), from both the startup command and `wmf-sbx-resume`, which
      no longer trusts the startup command to have run. `wmf-sbx-setup
      --verify` now reports a mirror that came back `rw` as an error in
      its own right, rather than leaving it to whoever remembers to try a
      write (§44.2). Written up in `sbx/DESIGN-parallel-clone-tree.md` §2
      and, from the security-consequences angle, `sbx/SECURITY.md` §3-§4
      (the latter now covers this comprehensively — the hand-written-kit
      hole included — so there's nothing further to migrate here).
- [x] **Declare the published ports in the kit** (§33.1). **DONE**
      2026-09-08 (§35.1): the generated kit carries a top-level `ports:`
      with the daemon's 9977 and no `protocol`, so sbx re-publishes it on
      every container start; `ensure_published_host_port` looks the
      mapping up and only publishes when there is none. **Confirmed on
      the host** the same day (§36): `sbx create` published `git-daemon:
      localhost:32794 -> 9977/tcp` itself. One documented behaviour did
      not hold — an empty `protocol:` published both IPv4 and IPv6 (§36.3).
- [x] **Confirm the gc suspension end to end** (§31.3). **DONE**
      2026-09-08 (§32): all five host repos carried `gc.auto=0`,
      `gc.pruneExpire=never`, both `(unset)` sentinels and
      `wmfsbx.gcsuspended=true` while `mw-translate` was up, and
      `git config --local --list` showed nothing left in any of them
      after `wmf-sbx-rm`.
- [x] **Move the git daemon to a kit startup command** (§32.4, §15.4).
      **DONE** 2026-09-08 (§33.2): `setup.startup` was the missing key,
      documented in the docs mirror rather than guessable from
      `sbx kit inspect`. The generated kit now carries a guarded,
      idempotent `git daemon` entry. **Confirmed on the host** the same
      day (§34.1): 4 startup commands registered, and exactly one daemon
      listening after a `stop`/start. The remotes still needed §34.2's
      port re-point to be usable. Unblocks §31.1's `mount --move` idea.
- [x] **Update the generated kit to kit-spec v2** (§32.5, §33.1).
      **DONE** 2026-09-08, and `sbx kit validate` came back `VALID` with
      no warnings on the host (§34.1).
- [x] Implement `sbx/DESIGN-dependency-walk.md` — walk `requires` /
      `dev-requires` / `suggests` from `extension.json`/`skin.json` so
      `wmf-sbx-create <one extension>` pulls in its whole closure plus
      `core` and `Vector`, and symlink the results into the core clone's
      `extensions/`/`skins/` during in-sandbox setup. **DONE**
      2026-09-07: the walk in `sbx/bin/wmf_sbx_deps.py` (§27), the
      symlinks in `wmf_sbx_setup.link_into_core()` driven by a host-built
      JSON plan file (§28). `--no-deps` / `--no-dev` / `--no-suggests`
      narrow the closure. Not yet exercised end-to-end in a real sandbox
      — that wants the §7-setup-steps chain around it. Supersedes the older sketch in
      the extension-dependency bullet above, which assumed the link name
      was the `name` field — it isn't (§1 of that doc: 44 of 298 local
      manifests have `name` != directory, some with spaces).
- [x] Implement `sbx/DESIGN-host-remotes.md` — **DONE** 2026-09-07.
      `wmf-sbx-create` now runs the `git remote add` commands it used to
      only print (`--no-remotes` reverts to printing), backed by
      per-sandbox state under
      `${XDG_STATE_HOME:-~/.local/state}/wmf-sbx/sandboxes/<name>.json`,
      and `wmf-sbx-rm [--dry-run] [--keep-remotes] [--force] [--prune]`
      tears them back down. The forcing constraint was that host ports are
      ephemeral and recycled, so a leaked remote doesn't just clutter a
      config — it can later fetch from an unrelated sandbox; hence the
      opportunistic `--prune` pass `wmf-sbx-create` runs on every launch,
      which catches sandboxes removed by a bare `sbx rm` or `sbx logout`.
      **Still unverified on the host** (see §26): everything about
      `sbx rm`'s prompt-and-exit-0 behaviour is encoded from cananian's
      earlier transcripts, and the end-to-end create → commit → fetch →
      `wmf-sbx-rm` loop has never been run against a real `sbx`.
- [x] ~~`sbx create --help`~~ — confirmed via the CLI reference mirror
      (§3, §4): `sbx create [flags] AGENT PATH [PATH...]`, `--clone` is
      exactly the boolean-first-workspace-only flag described in §2, no
      hidden flag for a second push-back clone.
- [x] ~~`sbx policy ls` / `sbx policy log`~~ — confirmed domain-only (§3).
- [x] ~~`sbx run -t` / template image docs~~ — confirmed (§4).
- [x] ~~Test the `env -u SSH_AUTH_SOCK sbx create ...` mitigation on the
      host (§8)~~ — tested for real, **failed**: see the 2026-09-05 update
      to §8 above. Per-invocation stripping around `sbx create` alone is
      not sufficient; superseded by the `sbx/bin/wmf-sbx` wrapper, which
      every `sbx` invocation (not just `create`) must now go through.
- [x] ~~**[human]** Re-verify end-to-end with the daemon restarted clean and
      driven only through `wmf-sbx` for a full session~~ — **PASSED**
      (§8 2026-09-05 update): after a full prior session driven only
      through `wmf-sbx`, `SSH_AUTH_SOCK` is unset and `ssh-add -l` fails
      inside the resulting sandbox. Daemon did not get retainted.
- [x] ~~**A start `wmf-sbx-resume` never saw does NOT get its mounts
      back**~~ — **it does; §45.1 was a race.** Settled 2026-09-08 by the
      log-reading sequence this bullet asked for (§46): the first
      `--verify` after a `stop` fails nine post-conditions, ten seconds
      later the same command is `ok (alias)` three times over, and
      `/var/log/sbx-kit-startup.log` shows the dispatcher running our
      `--restore` on every container start. It's the first hypothesis:
      the startup command runs and works, but doesn't block the `sbx exec`
      that triggered the start. `wmf-sbx-resume` now waits for the layout
      (`--verify --wait=20`) instead of racing it with a second restore
      pass, and the restore log appends so a later reader can't be misled
      the way §41 was. Residual: on paths that *don't* wait (`sbx run
      --name`, a bare `sbx exec`) the host mirrors are writable for the
      first seconds of a container start — for `SECURITY.md`, not for
      code.

- [x] **The sbx 0.39.0 → 0.42.1 upgrade. DONE**, measured and closed out
      2026-09-08 — §47 has the full re-measurement and write-up.
      0.42's startup dispatcher
      does *not* block the triggering exec, so §46's residual window
      stands (see §47.2/§47.7-8 for what changed instead: two bug fixes
      found along the way).
- [x] **Find and set 0.42's SSH-forwarding disable** (§8, `SECURITY.md`
      §2). The release notes say it exists but don't name the setting.
      The `wmf-sbx` wrapper stays regardless. cananian, 2026-09-08: the
      setting is documented in
      https://github.com/docker/sbx-releases/issues/121#issuecomment-5580321741
      — read that before searching the release notes again.
      **Found** 2026-09-14 (§78.1): `ssh.agentForwardingEnabled`, bool,
      default `true`, `requires_restart` — plus `ssh.agentSocketPath` for
      the fixed-socket variant. It did not need the issue thread;
      `wmf-sbx settings list --json` names it. **Set to `false`** the
      same day by cananian. No daemon restart was needed and none was
      run: this host's daemon was started through the wrapper, so it has
      no agent to forward, and every future daemon reads the setting —
      both covered, by different mechanisms, with no window between them.
      The restart the tool suggests is not free (§78.1), and that warning
      now travels with the advice in `SECURITY.md` §2.
- [x] **Document or undo `claude.remoteControl`** (§78.2). The one
      setting on this host not at its default — `true` against a default
      of `false`, letting Claude Code's `/remote-control` channel
      authenticate with its own session token instead of the host
      credential. Asked 2026-09-14; **answered the same day — cananian
      set it deliberately**, so it is documented rather than undone:
      `SECURITY.md` §2 now names it as this host's one departure from
      stock, with which way the trade runs. Nothing here depends on it.
- [x] **Close the shared skills store as far as we can from inside**
      (§79). **DONE** 2026-09-14, cananian's call: `wmf-sbx-setup`
      remounts `~/.claude/skills` read-only at install and on every
      container start, and `--verify` reports it if it comes back
      writable. MEASURED both ways (§79.1) — the `ro` remount makes the
      write fail for real, and `sudo mount -o remount,rw,bind` undoes it
      on the first try, so this is a second layer and not the fix. Kept
      separate from the upstream item below on purpose.
- [x] **Raise the shared skills store with upstream** (§77, §79.3, §79.4,
      `SECURITY.md` §7.6, §10). **Already filed, by someone else:**
      [`docker/sbx-releases#506`](https://github.com/docker/sbx-releases/issues/506),
      2026-08-25, against 0.39.0 — cananian found it 2026-09-14. Same
      mount, same "back door between agents" framing; assigned to a
      maintainer, who accepted it the same day and described the fix
      (`ro` by default, `--shared-skills-rw` to opt in). cscott added
      our two extra findings — the modify/delete channel and persistence
      past `sbx rm` — as a comment on 2026-09-14. Our write-up stays at
      `reference/upstream/skills-store-writable.md` as the reproduction
      record. Nothing left to file; the watch item below is what remains.
- [x] **Watch every new sbx release for #506's fix to land** (§79.4,
      `SECURITY.md` §7.6, §9). cananian's ask, 2026-09-14: **check the
      release notes on every upstream upgrade** for the shared
      agent-skills store. **It landed, in v0.43.0-rc3** — read 2026-09-14,
      written up as §81: `--skills=off|readonly|readwrite`, `readonly` by
      default, plus a `skills.defaultMode` setting. The watch is
      discharged; the *habit* it asked for is not, and is now its own
      item below. Our code side is §82.1 (done, and version-agnostic);
      the upgrade itself is the item below that.
- [x] **Upgrade to 0.43 and recreate the sandboxes** (§81, §82,
      `RESUME.md`). **DONE** 2026-09-14 — `wmf-claude-sbx` recreated on
      `v0.43.0-rc3` (§83), skills mount confirmed absent (§83), and
      `RESUME.md` §5's post-upgrade checklist worked through in two
      rounds (§83, §84): version, skills mount, `wmf-sbx inspect`'s new
      host-side mount view, the network policy dump, name-length
      rejection, and the per-agent `--help` residual all measured and
      match the release notes. Two threads still open, listed below:
      `skills.defaultMode`'s global default, and the idle-auto-stop
      resume-cost timing (§84.6).
- [x] **Measure whether `sbx create` will accept a workspace list with no
      read-write member** (§82.2). **DONE** 2026-09-14, cananian, on the
      host: **no.** `ERROR: primary workspace must be read/write` for a
      two-path command as well as a one-path one, so the rule is
      positional. Shape A is dead; shape B is the answer, and it needed
      no probe of its own — a throwaway read/write primary satisfies both
      candidate readings of the rule. Write-up and design in §82.3.
- [x] **File the upstream feature request: let the primary workspace be
      `:ro`** (cananian, 2026-09-14; §82.6). **DONE** the same evening —
      [#586](https://github.com/docker/sbx-releases/issues/586), filed by
      cananian, who wrote their own version rather than using our draft.
      This is the preferred route to closing `SECURITY.md` §3: it needs no
      scratch directory and costs no working directory (§82.5).
- [x] **Search the per-agent subcommand's help for a skills flag**
      (§79.6's named residual). **DONE** 2026-09-14, cananian:
      `wmf-sbx create claude --help | grep -i skill` matches nothing, so
      both surfaces are searched and §79.6's negative finding is entitled
      to its scope (§82.4).
- [x] **Verify which skills flags `sbx create` actually has on 0.42.1**
      (§79.4). **DONE** 2026-09-14, cananian ran it on the host:
      **neither exists.** `sbx create --help` lists 21 flags and none
      matches `skill` — not `--shared-skills-rw` (unshipped, as the open
      issue implies) and not `--no-share-skills` (which #506's body
      describes as existing; we cannot say why it isn't there). Recorded
      with the full flag list in §79.6. **Consequence: there is no
      host-side opt-out today**, so §79's in-sandbox remount is not a
      second layer behind a boundary — it is all there is, until #506
      lands. One cheap residual: the per-agent subcommands could define
      their own flags, so `sbx create claude --help | grep -i skill`
      would close the last of it.
- [x] **Re-verify the §47.7/§47.8 stdin-hang fix at the next sbx upgrade.**
      **DONE, the hard way** — 2026-09-14, during the actual 0.42.1→0.43
      recreate (§82.9): the exact predicted failure mode recurred
      (`wmf-sbx mcp ls --json` hit a real version-mismatch restart prompt
      and hung), but on a call site the original sweep never covered,
      because `host_mcp_registrations()` and 3 siblings were written
      *after* §47.8 and were never added to it. Root-caused and fixed —
      `stdin=subprocess.DEVNULL` added to all 4 remaining call sites,
      unit-tested. **Still open**: a real end-to-end confirmation that
      `wmf-sbx-create` now fails fast rather than hanging when it hits
      this prompt live — the next recreate attempt is that test.
- [x] **Integrate skills and MCP servers from the parent `wmf-claude`
      package into the sbx kit.** sbx sandboxes currently launch a bare
      `claude`, without the `skills/`, `agents/`, and MCP server
      (`mcp-phabricator/`, `gerrit-mcp-server/`) wiring that
      `wmf-claude`'s nono-based launcher (`bin/claude` /
      `bin/launch-claude.sh`) sets up. **Planned** 2026-09-11 (§55) in
      `sbx/DESIGN-plugin-integration.md`. Seam 1 done (§56); the design's
      step 2 measurements done (§57–62) and **no longer blocking**. The
      route is **Route 1**: the servers stay on the host behind sbx's MCP
      gateway, so their credentials never enter the sandbox (§60.1), and
      `sbx/bin/wmf-sbx-mcp-proxy` — written and tested, §60.4 — fronts
      each one as an in-sandbox stdio server so the literal
      `mcp__phabricator__*` / `mcp__gerrit__*` names survive.
      **Verified end to end for both servers** 2026-09-12 (§61.2
      phabricator, §62.5 gerrit): the literal tool names, the `--tools`
      allowlist enforced, the gateway's meta-tools invisible, and real
      fetches — with the credentials on the host. `--tools` is
      **mandatory**, because the gateway's tool namespace is flat and
      unattributed (§62.2) and mounts outlive the session (§62.3). Host
      prerequisite: node ≥20.18.1 (§60.2, done — 26.8.2).
      **DONE** 2026-09-14. Step 3 (the plugin in the kit) landed
      2026-09-12 (§64); step 4 (the proxy entries in `setup.startup` with
      a per-server allowlist, the `sbx mcp add` calls in
      `wmf-sbx-create`, `--static-mcp` in `build_sbx_command`, and
      `mcp__mcp-gateway` in the ported deny list) landed the same day
      (§65). The host half §65.3 called "written but not measured" has
      since been run four times: `mcp ls --json` and the real store
      (§67), the `--command env` registration and both node paths (§69),
      Route 1 end to end from a kit-built sandbox (§72–73), and the
      username divergence check (§75.5). What used to be the *risk* on
      this path — a registration silently diverging from the engineer's
      config — is now a preflight that stops the create.
      Two pieces of the design doc's scope are split out below rather
      than carried here: the one prompt that would exercise gerrit from
      a kit-built sandbox, and step 5's `SECURITY.md` remainder.
- [x] **Gerrit has never been called from a kit-built sandbox.** §62.5
      is a real end-to-end model call — the five read-only tools, the 15
      write tools filtered out, `get_commit_message` on change 1338945 —
      but the proxy entry there was hand-registered in `wmf-claude-sbx`.
      The one create-driven run that exercised the whole chain (§73,
      `wmf-sbx-create Cite`) asked for a Phabricator task and nothing
      else, so the gerrit half of a *generated* kit — the `--tools`
      allowlist as `wmf-sbx-setup --mcp` writes it, against
      `--static-mcp gerrit` as `build_sbx_command` passes it — has never
      had a tool call put through it. Both servers are registered on the
      host (responses26 step 4 lists `gerrit, phabricator`), so this is
      one prompt in a sandbox, not work. What it would catch: a
      per-server allowlist that is right when typed by hand and wrong
      when generated.
      **DONE** 2026-09-14 (§77.1): `gerrit-probe`, a fresh
      `wmf-sbx-create Vector`. The generated `--tools` list is exactly
      `MCP_SERVER_TOOLS["gerrit"]`, the proxy refuses `abandon_change`
      with `-32603`, and the model's `get_commit_message` on 1338945
      returns the same string §62.5 got by hand.
- [x] **Can the agent write to the shared skills store?** §77.2. One
      host directory — `~/.local/state/sandboxes/sandboxes/agent-skills`
      — is symlinked into *every* sandbox's `~/.claude/skills` and
      mounted `rw` (§55.1). We ship Route A, so nothing of ours goes in
      it, but that is not the same as it not being there (§76 said it
      was, wrongly). If the agent can create a file there, a compromised
      sandbox writes instructions that every other sandbox on the host
      loads at its next start, and it outlives `sbx rm` of the sandbox
      that wrote it. In any sandbox:
      ```console
      $ ls -ld ~/.claude/skills; id
      $ touch ~/.claude/skills/.probe && echo WRITABLE; rm -f ~/.claude/skills/.probe
      ```
      **Attempt the write** — the `ls -ld` is context, not evidence
      (`feedback_verify_security_by_bypass`). Writable means a
      `SECURITY.md` boundary section and probably a mitigation; read-only
      means a mitigation we did not know we had. Either way §7.6 stops
      saying "unmeasured".
      **DONE** 2026-09-14 (§77.3): `WRITABLE`, in `gerrit-probe`. The
      tool-layer deny `Edit(~/.claude/skills/**)` already shipped and
      already reaches the sandbox, which is worth keeping against a
      prompt injection and is not a boundary — bash writes go around it
      and the agent is root anyway. Nothing we can set from inside the
      sandbox closes this; it is now an upstream ask (`SECURITY.md` §10)
      for a `ro` or per-sandbox store.
- [x] **Observe the cross-sandbox half of §7.6.** That a file written in
      one sandbox's `~/.claude/skills` appears in another's is supported
      — one host path in every `sbx create`'s resolve output, plus
      §55.1's mount — but has not been seen. `touch` in one sandbox,
      `ls` in a second. It is the step that turns "a shared directory"
      into "a channel", so it should not stay an inference.
      **DONE** 2026-09-14 (§77.4): written in `gerrit-probe`, read in
      `wmf-claude-sbx` — and the cleanup then deleted `gerrit-probe`'s
      file *from* `wmf-claude-sbx`, so it is a read/write/delete channel,
      not just a plant. Also the reason `ls -la` is now in the probe: a
      plain `ls` hid both files and nearly read as "not shared".
- [x] **`sbx/SECURITY.md` owes the MCP work its last paragraph**
      (`DESIGN-plugin-integration.md` step 5). §7.1–7.2 shipped as §7.4
      on 2026-09-12, but three things were left: two sections asserting
      the host side was unmeasured, the proxy-managed token never
      written up as a residual risk, and Route B's shared skills store.
      **DONE** 2026-09-14 (§76). The first is now false and is written
      as measured; the second is §7.5, built on §73.3's measurement; the
      third is §7.6 — written off the same morning as moot because we
      ship Route A, and corrected that afternoon by §77.2. The store is
      shared and mounted `rw` into every sandbox whether anything of ours
      goes in it or not. Its one open question has its own item above.
- [x] **QoL: `git review-check` doesn't know it's being asked from inside
      or about a sandbox.** cananian, 2026-09-08, two related frictions
      from actually using the parallel clone tree:
      - On the host, `git safe-reset sandbox-wmf-claude-sbx` is a
        convenient way to pull the latest committed-in-sandbox work into
        the host's local copy of *this* repo — but it only works because
        this particular repo isn't cloned from Gerrit. In a Gerrit-backed
        repo, `review-check` would object that the commit hasn't been
        uploaded to Gerrit yet, even though it *has* been pushed
        somewhere real (the sandbox).
      - Symmetrically, inside a running Claude session, `! git safe-reset
        local` looks like a good way to pull host commits into the
        sandbox's working copy mid-session, but hits the same
        "not upstreamed to Gerrit" objection.
      **DONE** 2026-09-08 (§48), cananian's proposed design: teach
      `git-review-check` that `sandbox-<name>` and `local` are never
      Gerrit, and substitute a local containment check — is HEAD already
      reachable from that remote's refs — for the Gerrit-upload query.
      Refined same day (§48.1) so which remotes qualify is verified
      against wmf-sbx's host state / `$SANDBOX_NAME`, not guessed from the
      remote's name. Not yet re-verified against a real sandbox on the
      host (§48).
- [x] Refactor the "small inline python3 snippet" in git-review-check
      into a small standalone utility, aka "wmf-sbx-ls-remotes". **DONE**
      2026-09-11 (§48.2).
- [x] Move python code out of `sbx/bin` so that we can recommend users
      add `sbx/bin` to their PATH (without getting other cruft). **DONE**
      2026-09-11 (§49).
- [x] Write a README describing basic operation. Should mention
      `wmf-sbx-start` (§50) as the fix if your git remotes into a
      sandbox stop working.
- [x] Extract a `wmf-sbx-start` command which does the
      `wmf-sbx-resume` work but without actually launching Claude,
      since an exited sandbox will eventually switch from 'running' to
      'stopped'. **DONE** 2026-09-11 (§50).
- [x] MW_INSTALL_PATH is being set to the /home/agent/... path; it
	  should be set to the /home/cananian/... path. **DONE**
	  2026-09-12 (§63).
- [x] Check out sbx/reference/MEDIAWIKI-TESTING.md (a report from
      another Claude agent on setting up testing of an extension
	  against MediaWiki) and turn this into a CLAUDE.md memory and/or
	  use it to improve our default install.  Perhaps we should
      allowlist the URL used for the chrome download?
      **In progress** 2026-09-15: measured and written up as
      `sbx/DESIGN-testing-instructions.md` (the plan) and
      `sbx/templates/MEDIAWIKI-TESTING.md` (the in-sandbox guide). Yes to
      the allowlist: MediaWiki pins no browser version, so the kit should
      allow `googlechromelabs.github.io` and let `@stable` resolve normally
      (design §2.4). The planned `mw-install-browser` goes in the new
      `sbx/helpers/` (in-sandbox scripts, not on the host's PATH), where
      `wmf-sbx-mcp-proxy` and `wmf-sbx-gateway-tools` now live too.
      `git-safe-reset` and `git-review-check` stay in `sbx/bin/`, since
      they run on the host as well. The api-testing config goes in core,
      whose `.gitignore` already lists it (design §5.5). Plugin text that
      only our sandbox reads moves to `sbx/plugin-overlay/`, and a shared
      file keeps a patch in `sbx/patches/plugin/`; the kit applies both to
      the copy it installs, so nothing outside `sbx/` changes (design §6).
      The VisualEditor report itself is deleted; the design doc supersedes
      it. A blind acceptance run in a fresh sandbox closes the work
      (design §9). Done so far: the overlay and patch mechanisms, the
      move of the sbx context files into the overlay, correction 1 and
      correction 3 (design §6.1-§6.4), and the sandbox configuration of
      §5.1, §5.2, §5.4 and §5.5, then §5.3 (`mw-install-browser`, §86),
      §3 (the guide ships to `~/MEDIAWIKI-TESTING.md`), §4 (the CLAUDE.md
      section) and correction 2. Not yet implemented: §5.6 (its own to-do
      below). The acceptance run of §9 has run twice (§87, §88).
      **DONE** 2026-09-18 (§94), except §5.6, which is the next to-do.
      Five acceptance runs (§87, §88, §90, §93), the Cypress install
      (§91–§93), and the upstream write-ups in `sbx/upstream/`.
- [x] **Merge extension Composer dependencies into core, as quibble
      does.** Found by the second acceptance run (§88): phan reports
      `\Spyc` undeclared in Translate, although
      `extensions/Translate/vendor/mustangostang/spyc` is there. Cause:
      `mediawiki-phan-config` adds only `$MW_VENDOR_PATH/vendor` (else
      core's `vendor/`), and `wmf-sbx-setup` runs `composer update` in each
      repo on its own. Quibble instead writes core's `composer.local.json`
      with `extra.merge-plugin.include` = `extensions/*/composer.json`,
      `skins/*/composer.json` (`quibble/commands.py`, read 2026-09-18), so
      extension libraries land in core's `vendor/`. The same change in
      `setup.py` means: write that file before core's `composer update`.
      Core's `.gitignore` lists `composer.local.json`. Open questions: an
      engineer's own `composer.local.json` (leave it alone, as with `.env`?),
      and whether one library in two `vendor/` trees gives autoload
      trouble at runtime. Until then the guide names this second cause of
      phan's undeclared-class errors.

      **DONE** 2026-09-18 with option 1 (§89). The engineer's own file is
      left alone. The one library in two trees is `wikimedia/parsoid`;
      the checkout wins, and the vendor copy is out of the classmap (§89).

      Where the packages come from — the options:

      1. **Merge each repo's `composer.json` into core, as quibble does**
         (above). The package set follows the checked-out trees, so a
         patch that adds a package or changes a version works at once.
         This is the preferred option.
      2. **Use `gerrit:mediawiki/vendor` as core's `vendor/`, as
         production does** [cananian, 2026-09-18]. In production, core's
         `vendor/` is a checkout of that repo: one merged, locked copy of
         the packages that all *production* extensions need. Probably not
         the one to pick. An extension patch that adds a package or
         changes a version usually goes through CI first, and the
         `mediawiki/vendor` update comes later. With `mediawiki/vendor`
         as the source, such a patch would fail in the sandbox until
         someone regenerated `mediawiki/vendor` too. It also does not
         cover extensions that do not run in production.
      3. **Leave each repo's own `vendor/` as it is, and point phan at it**
         (for example, `$MW_VENDOR_PATH` per run, or an extra directory in
         the phan config). Smallest change, but it changes phan's input
         only. The wiki still runs with a package set CI never builds.
- [x] README should mention minimum node version and/or we should
      check this during setup? **DONE** 2026-09-12 (§68): checked, and
      a create that would have to register the Phabricator server with
      too old a node now stops with the remedies spelled out, instead of
      warning past it. In the README under the MCP section.
- [x] The clone-mode instructions in the global CLAUDE.md in the
      sandbox describe the host retrieving commits via a remote named
	  `sandbox-<name>`. That is out of date: the remote is now named
	  `sbx-<name>`; further, the instructions should direct the agent
	  to read from $SANDBOX_NAME/hostname to get the *actual* name
      used on the host, instead of guessing.  Further, these
      instructions should suggest that the agent end its turn with a
	  commit, where appropriate, so that the work can be fetched
	  on the host. (I've often had the agent end its turn with "all
	  changes made in local copies, waiting for instructions to
      commit" which leaves the work inaccessible from the host. Better
	  to commit at the end of a turn, and then amend or reset the
      commit on the next turn, rather than leave the changed files
	  inaccessible from the host.)
      **DONE** 2026-09-14 (§73.2) — with one correction to the premise
      and one thing found underneath it. The remote is `<name>`, not
      `sbx-<name>`: the name already carries the prefix, so there is no
      second one (`remotes.remote_name_for`). And that file is sbx's own
      boilerplate, which we cannot edit — so the fixes went into
      `HOME_CLAUDE_MD`, the CLAUDE.md we *do* write, which now names the
      other document and says which wins. The thing underneath: that
      document's mode probe answers "direct mode" here, and direct mode
      promises the agent that its edits "appear on the host
      **immediately**". That is very likely the whole cause of the
      "waiting for instructions to commit" endings — the agent was
      reasoning correctly from a false premise it had been handed.

- [x] Make `--kit` optional so `wmf-sbx-create` doesn't require every user
      to hand-maintain their own kit directory — **DONE**: §15,
      `sbx/DESIGN-kit-generation.md`, `sbx/bin/wmf_sbx_kit.py`. cananian's
      personal `~/.config/wmf-sbx/repos.yaml` was also drafted (not
      committed — personal, lives outside the repo); `mediawiki/vendor`
      is at `~/Wikimedia/mediawiki-vendor` on the host.
- [x] Design the per-user config file mapping Gerrit path prefixes to local
      directory conventions — **DONE**: `sbx/DESIGN-repo-resolution.md`,
      implemented in `sbx/bin/wmf_sbx_resolve.py` (§10) and consumed by the
      Layer A launcher (§14). Does not hardcode cananian's own layout —
      `sbx/reference/example-repos.yaml` is a runnable starting point for
      any user's own `~/.config/wmf-sbx/repos.yaml`.
- [x] Research a canonical (non-`LocalSettings.php`) source for MediaWiki
      extension/skin inter-dependencies — **CONFIRMED**: `extension.json`'s
      top-level `requires` object is exactly this, and is universal across
      the read-only-mounted `Extensions/` tree (spot-checked ~20 files).
      Shape: `requires.MediaWiki` (a version constraint, e.g. `">= 1.45"`)
      and `requires.extensions` (a map of required extension name →
      version constraint, e.g. `{"Scribunto": "*"}`,
      `{"BlueSpiceFoundation": ">= 4.1"}`). A dependency walker just needs
      to read each candidate extension's `extension.json`, collect the
      keys of `requires.extensions`, and recurse — no `LocalSettings.php`
      parsing needed. `skin.json` presumably has the same `requires` shape
      for skins (not yet spot-checked).
      **Followed up 2026-09-07 in `sbx/DESIGN-dependency-walk.md`**, which
      settles the "presumably": `skin.json` uses the *same* schema file
      (`docs/extension.schema.v2.json`; there is no separate skin schema),
      and `requires`/`dev-requires`/`suggests` each accept a `skins`
      sub-object as well as `extensions`. Also corrects the implicit
      assumption here that a `requires.extensions` key is a directory
      name — it is matched against the `name` field, which differs from
      the directory for 44 of 298 local manifests.
- [x] A `wmf-sbx-run`/`wmf-sbx-resume` launcher, sibling to
      `wmf-sbx-create`/`wmf-sbx-resolve`, for re-attaching to an existing
      sandbox rather than creating a new one. **DONE** 2026-09-08 (§34.2)
      as `wmf-sbx-resume NAME [-- AGENT_ARGS...]`, built earlier than
      planned because §34.2 turned it from a convenience into the thing
      that keeps the host remotes usable: it starts the sandbox, re-points
      every `sandbox-<name>` remote at the daemon's newly published host
      port, then attaches with `wmf-sbx run --name`.

      It also does §20's original job: the agent CLI args after `--` don't
      persist across a stop/restart, so cananian had to retype
      `sbx run <name> -- --continue --dangerously-skip-permissions` on
      every re-attach. `resumeArgs:` in `~/.config/wmf-sbx/repos.yaml` is
      that tail now (a list, or a plain string), and anything after `--`
      overrides it for one invocation. sbx 0.39 no longer needs
      `--dangerously-skip-permissions`, so the only thing defaulted is
      `--continue`, and only from the second attach onward — see §35.2
      for why the first one has to go without.

      Still open, and the natural home for it: redoing any *other*
      create-time-only setup that turns out not to survive a restart —
      reapplying `-e` if §20's open question about `-e`-on-re-attach turns
      out to matter, or re-running `sbx policy allow network --sandbox`
      calls from the caching design (`sbx/DESIGN-template-caching.md` §4).
      The daemon half of §15.4 no longer needs it: the kit's startup
      command restarts the daemon itself (§33.2, confirmed §34.1).
- [x] **The Phabricator username is baked into a stored registration, and
      there is nowhere better to read it from** (cananian, §69's run: the
      `sbx mcp add` line hard-wires `PHABRICATOR_USERNAME=cscott`).
      Checked `bin/wmf-claude-setup` for the stash to reuse: **there
      isn't one.** It prompts, recalls the previous answer from `claude
      mcp get phabricator`'s public output, and stores it only in the
      `claude mcp add -e` registration it then makes. So
      `create.phabricator_username` already reads the only stash that
      exists, and `$PHABRICATOR_USERNAME` overrides it (§65.4). Two real
      gaps remain, both from `sbx mcp add` having no `--env` and no
      update-in-place:
      - a host that never ran `wmf-claude-setup` has nothing to read, and
        the server is registered with no username at all, silently — the
        "my tasks" filter then just returns nothing useful;
      - a username changed later (a re-run of `wmf-claude-setup`) does not
        reach an `sbx mcp` registration already made, because we never
        overwrite one — and nothing notices the two have diverged.
      Worth considering: compare the two at create time and say so, and/or
      a real stash (`~/.config/wmf-sbx/repos.yaml`?) that both installers
      read, rather than one installer's registration doubling as the other
      installer's config.
      **DONE** 2026-09-14 (§75), both halves, and both of the things that
      paragraph called "worth considering". The stash exists, in the
      parent repo rather than sbx: `bin/wmf-claude-setup` writes
      `~/.config/wmf-claude/config.json` and `phabricator_username` reads
      it ahead of the `claude mcp get` scrape, which stays as the fallback
      for installs that predate it — first gap closed. For the second,
      the create now reads the username back out of an existing
      registration with `wmf-sbx mcp inspect --json` and stops if it
      disagrees, naming `wmf-sbx mcp rm phabricator` rather than running
      it: the server is the host's, and removing it mid-session takes the
      Phabricator tools away from every sandbox holding it open.
      **Measured on the host** 2026-09-14 (§75.5): six steps, including a
      genuine `mcp rm` + re-`add` at `not-cscott`, the reversed message,
      and a negative control that reaches `would register` and exits 0.
      The config-file path ran live for the first time in the same pass —
      with no config file yet, `wmf-claude-setup` recalled `cscott` from
      the old `claude mcp get` scrape and stored it to the new stash.
- [x] **Confirm the §71 diagnosis from a real create's log.** **DONE**
      2026-09-13 — though not the way this item expected. The create log
      said nothing at all (install-step stdout is invisible even on
      *success*, which is the item above in a new costume), and the
      startup step's `0 restored, 3 already executable` only proves the
      install step got there first. What settled it was the control:
      `~/bin/git-safe-reset`, staged 0755 and outside `exec_bit_paths()`,
      reads 0644 in the sandbox (§71.1).
- [x] **Nothing runs the MCP proxy end to end yet.** **DONE** 2026-09-13
      (§72): a real session in an MCP sandbox spawned the proxy, listed
      both servers ✔ Connected, and called a Phabricator tool. It found
      three bugs — two in our code, one not ours.
- [x] **Re-run the MCP end-to-end measurement after the §72 fixes.**
      **DONE** 2026-09-14 (§73): a fresh `wmf-sbx-create Cite`, and
      `phabricator_get_task` returned T1's title to the model. Route 1
      works end to end. The §72.3 seed is confirmed too — no auto-mode
      prompt — which was the one thing that note called inferred.
- [x] **Find out why the gateway dropped the session at all** (§72.2).
      **DONE** 2026-09-14 (§74): a 45-second idle timeout, measured to
      the second. Nothing was wrong with that session — every session is
      dead by the time a model reaches for a tool. This item guessed that
      "seconds rather than minutes" would mean keeping the session warm;
      measured, the re-handshake costs ~2 ms, so lazy recovery stays and
      a keepalive would be strictly worse (§74.2).
- [x] **`wmf-sbx-rm` often refuses because it can't connect to git
      daemon**; it should `wmf-sbx-start` before refusing instead of
      forcing the user to do so (or to use `--force`).
      **DONE** 2026-09-15: `check_unfetched` (rm.py) used to only
      `start_sandbox`+`wait_for_daemon` when the port lookup itself came
      back empty (a fully-stopped container). sbx's own idle-auto-stop
      (§81.2) breaks that: it restarts the container on the `sbx exec`
      inside `start_sandbox`, and the port mapping can be back before
      the daemon inside is, so the old guard's port-already-published
      shortcut skipped the wait entirely and reported "unreachable" —
      pushing the engineer to `--force` in what is now the *normal*
      case, not an edge case. Fixed by always calling
      `start_sandbox`+`wait_for_daemon` before the port refresh, rather
      than only when the refresh comes back `None` — `start_sandbox` is
      a no-op `sbx exec ... true` on an already-running sandbox, so this
      costs nothing there. New regression test
      (`test_a_daemon_still_starting_after_an_idle_restart_is_waited_for`)
      models the port-published-but-daemon-still-coming-up case
      directly; all 18 existing rm.py tests already modeled a
      fully-stopped sandbox and pass unchanged.
- [x] **`wmf-sbx-exec` should accept the arguments of the parent `sbx
      exec` command and pass them through**, so that a command like
	  `wmf-sbx exec -it <sandboxname> bash` (to launch an interactive shell)
	  works.  Documentation of the command line arguments is in
	  ~/Projects/Wikimedia/docs.docker.com/reference/cli/sbx/exec
      **DONE** 2026-09-15: replaced the old hand-rolled `NAME -- CMD
      [ARGS...]` syntax (a custom `split_exec_args` pre-scan for a
      literal `--`) with `argparse.REMAINDER`, matching the real
      `sbx exec [flags] SANDBOX COMMAND [ARG...]` exactly -- flags
      first, then NAME, then everything after is the command, verbatim,
      no `--` needed. Added the real `sbx exec`'s own option set
      (`-d/--detach`, `--detach-keys`, `-e/--env` and `--env-file`
      (both repeatable), `-i/--interactive`, `--privileged`, `-t/--tty`,
      `-u/--user`, `-w/--workdir`) plus its global `-D/--debug` (placed
      before the `exec` subcommand in the constructed command, same as
      `--cloud` in the real CLI's own examples). Left out `--cloud` and
      `--cloud-api-url`: this tool only ever tracks local sandboxes, and
      the two are meaningless apart from each other. Combined short
      flags work for free (`-it` parses as `-i -t`) since argparse
      already supports that. This is a clean break from the old `--`
      syntax by design -- no internal caller depended on it (only the
      exec.py tests themselves did, updated here) -- though a stray
      leading `--` before the command still gets forwarded unchanged to
      the real `sbx exec`, which is cobra-based and strips it the same
      way it always did, so old invocations most likely keep working
      as a side effect, just no longer documented or relied on. Also
      dropped the now-unnecessary `--` from the printed
      `wmf-sbx exec NAME cat ...` hints in create.py/rm.py for
      consistency with the new syntax.
- [x] **Add settings checks to `wmf-sbx-create`**
      On startup, check the ssh forwarding settings and warn the user
	  if they are set to permit ssh inside sandboxes. Don't fail the
	  build, because we have mitigations in place, but make it a loud
	  warning because our mitigations don't protect against a running
	  daemon with preexisting access to ssh keys.  Also check the
	  `kit.allowLocalKits`, `kit.requireSignature`, `no_proxy.sandbox`
      and `proxy.sandbox` options and fail if they are not set to the
	  needed values, explaining the issue.  (Use
	  `wmf-sbx settings list --json` to dump the settings values.)
	  **DONE** 2026-09-14: new `sbx/src/wmf_sbx/settings.py` module
	  (`read_settings` -- a best-effort `wmf-sbx settings list --json`
	  call, same shape as `existing_sandbox_names`; `preflight` --
	  pure value logic, easy to unit test without a subprocess), wired
	  into `create.py`'s `main()` right after arg parsing so it runs
	  before config/repo resolution and fires under `--dry-run` too
	  (same "would this create work? has to include the answer 'no'"
	  rule as the MCP preflight, §68). `ssh.agentForwardingEnabled=true`
	  only warns (§2's daemon-wide caveat); `kit.allowLocalKits=false`,
	  `kit.requireSignature=true`, and a non-empty `no_proxy.sandbox` or
	  `proxy.sandbox` (§78.4, `SECURITY.md` §7.5.1) hard-fail with an
	  explanation. The two kit checks are skipped with an explicit
	  `--kit` (they gate *our generated* kit, same carve-out as
	  `--reset-all`/`--no-mcp`/`--kit-out`); the two proxy checks are
	  not, since those preconditions don't depend on which kit is used.
	  An unreadable settings list (no `sbx` on `PATH`, old sbx without
	  the command, ...) only warns and skips the checks -- it must never
	  block a create on our own ability to introspect the host.
- [x] [QOL] **Make the output of `wmf-sbx-create` prettier**
	  In the final instruction to the user, use the bare name
	  (`wmf-sbx-resume`) if that command is already on the user's
	  PATH; only output the full path name to that command if it
	  is actually necessary.  Further, the default sbx output uses
	  color on the terminal (ask for a dump of raw output), including
	  dimming out routine logs and highlighting in blue the final
	  suggested command (`sbx run --name <sandbox name>`).  We should
	  match this coloring scheme when running on a tty and in
	  particular use the same blue highlight for our `wmf-sbx-resume`
	  command.
	  **DONE** 2026-09-14: new `sbx/src/wmf_sbx/color.py` module (gated
	  on `stream.isatty()` and `NO_COLOR`), applied to `create.py`'s
	  routine trace lines (dim), the suggested-command lines (blue
	  highlight), and the genuine `error:` lines (red, used sparingly —
	  warnings stay uncolored). The red is an inferred "Tokyo Night"
	  value, `#f7768e`, since every captured reference transcript was an
	  all-success run with no failing command to extract a real red
	  from — revisit if one ever turns up. `build_sbx_run_command` now
	  prefers the bare `wmf-sbx-resume` name when `PATH` resolves it to
	  this repo's own copy (via `shutil.which` + a realpath check),
	  falling back to the full path otherwise.
- [x] **Watch for a browser.enabled setting, and mention it in SECURITY.md**
	  See https://github.com/docker/sbx-releases/issues/577
	  **DONE** 2026-09-14 (§80, `SECURITY.md` §7.7 and §8). The mention is
	  written, and it says more than the item asked for, because the
	  endpoint turned out to be reachable by a plain `curl` from inside
	  the sandbox — the `xdg-open` shim is a convenience, not the
	  interface — and gated by **nothing but the network allow policy**
	  (identical 403 to ordinary egress for a denied domain). So every
	  domain on the allowlist is a domain a sandbox can open in
	  cananian's browser, unprompted. No `browser.enabled` setting exists
	  on 0.42.1 and 0.43.0-rc3's notes do not add one; #577 is open,
	  answered by `kiview` with "we'll look into adding this as a
	  setting", no assignee and no milestone (comments read via the
	  **API**, §79.5).
	  Follow-up watch is folded into the release-notes item above, and the
	  mitigation is `SECURITY.md` §10's "narrow the network policy", which
	  now closes two holes instead of one.
- [x] **wmf-sbx redirects**
      Make `wmf-sbx rm` redirect to `wmf-sbx-rm` and similarly for
      `create`, `resume`, `resolve`, `start`, `ls-remotes`, and
      `exec`.  If the first argument is `--upstream` then do the
      current behavior (ie just invoke `sbx` after dropping SSH
      permissions).  That is, `wmf-sbx --upstream create ...` will invoke
      `sbx create ...` after dropping the ssh agent, but `wmf-sbx
      create ...` will invoke `wmf-sbx-create ...`.  Make sure this
	  also works when only `wmf-sbx` is on the user's PATH.
      **DONE** 2026-09-15: `bin/wmf-sbx` now dispatches `create exec
      ls-remotes resolve resume rm start` to the matching sibling
      `wmf-sbx-<verb>` script (realpath-resolved next to `wmf-sbx`
      itself, falling back to a PATH lookup, so it works whether
      `wmf-sbx` is reached directly, through a symlink, or is the only
      one of the eight scripts on PATH), and reserves `--upstream` as
      the literal first argument to bypass the redirect and go straight
      to the real `sbx`, as before. A missing sibling errors out clearly
      instead of silently falling back to unsafe raw `sbx` -- an
      incomplete install should never look like it worked.

      The dispatch has to run, and `SSH_AUTH_SOCK` has to be stripped,
      *before* the real-`sbx`-on-PATH lookup and self-loop guard, not
      after: a redirect never touches the real binary itself (the
      `wmf-sbx-<verb>` script it execs into calls back into `sbx`
      through its own `--upstream`-flagged `WMF_SBX` invocation, not
      through this exec), so gating every redirect on a real `sbx`
      being installed would make all seven verbs depend on an install
      step they don't actually need -- caught by the plan's own manual
      smoke-test step, which expected `wmf-sbx create --help` to print
      `wmf-sbx-create`'s own help even with no `sbx` on PATH.

      That `--upstream`-in-`WMF_SBX`-invocations point is the other
      half of this: every internal `[WMF_SBX, ...]` call site across
      `create.py`, `rm.py`, `resume.py`, `exec.py`, and `settings.py`
      needed `--upstream` inserted right after `WMF_SBX`, since those
      modules invoke the real `sbx` on their own behalf and would
      otherwise recurse into themselves (or into a much heavier wrapper
      than intended) once bare `wmf-sbx <verb>` started redirecting.
      This covers `run`/`ls`/`ports`/`mcp`/`settings` call sites too --
      none of those verbs redirect, so `--upstream` makes no behavior
      difference for them today, but it is one rule ("every internal
      call to `WMF_SBX` carries `--upstream`") instead of "carries it
      only for the seven verbs that currently redirect," which is the
      rule that would silently need remembering again the next time a
      verb gains a redirect.

      Also added: `RedirectVerbsAgreeTests` in
      `test_wmf_sbx_dispatch.py` asserts `bin/wmf-sbx`'s
      `REDIRECT_VERBS` (parsed out of the script) equals
      `wmf_sbx.__main__.COMMANDS`, and that every redirected verb has a
      `bin/wmf-sbx-<verb>` sibling on disk -- so adding a verb to one
      without the others fails a test instead of silently going to raw
      `sbx` (same pattern as the skill-list consistency check in this
      repo's own top-level `CLAUDE.md`). `README.md`'s install section
      described `wmf-sbx` as a plain alias for `sbx`; updated it to
      describe the redirect and `--upstream`.

      Tests: `sbx/tests/test_wmf_sbx_dispatch.py` (new) drives
      `bin/wmf-sbx` as a real subprocess against stub `sbx`/
      `wmf-sbx-<verb>` scripts, covering the redirect itself,
      `--upstream`, non-redirect/no-args/`--help` fallthrough, a
      missing sibling, the PATH-fallback and symlink-resolution sibling
      lookups, that `SSH_AUTH_SOCK`-stripping/`--cloud`-blocking still
      hold on both the `--upstream` and fallthrough paths, and the
      `REDIRECT_VERBS`/`COMMANDS`/on-disk-sibling agreement above. The
      existing Python-wrapper test suites (`test_wmf_sbx_create.py`,
      `test_wmf_sbx_exec.py`, `test_wmf_sbx_resume.py`,
      `test_wmf_sbx_start.py`, `test_wmf_sbx_settings.py`) got their
      expected-command assertions updated for the inserted `--upstream`;
      `test_wmf_sbx_rm.py` needed no changes, since its assertions were
      already substring/membership checks that hold either way.
- [x] **wmf-sbx redirects `run`, too**
      Follow-up to the audit above ("compatibility audit against
      upstream docs"): `run` was the one redirect-worthy verb the first
      pass missed, because it only partially overlaps with an existing
      wrapper. Upstream `sbx run [AGENT|SANDBOX_KIT] [PATH...] [--name
      NAME] [-- AGENT_ARGS...]` does two different things depending on
      its arguments: with `--name`, it re-attaches to an existing
      sandbox -- the same job `wmf-sbx-resume` already does, better
      (mount restore, remote repoint, `--continue` default -- see
      resume.py's docstring); without it, it creates a brand-new
      sandbox from an agent/kit, the same job `wmf-sbx-create` already
      does, better (cloning, dependency walk, MCP registration). A
      straight rename of `wmf-sbx-resume` to `wmf-sbx-run` was proposed
      and rejected: it only covers the first half, and would have
      broken the ad-hoc `wmf-sbx run <agent> <path>` create path that
      happens to work today only because `run` isn't currently a
      redirect verb and falls through to the real `sbx run`.

      **DONE** 2026-09-15: added a new, narrower `wmf-sbx-run` (`run.py`
      + `bin/wmf-sbx-run`, added to `REDIRECT_VERBS` and
      `wmf_sbx.__main__.COMMANDS` alongside the existing seven) that
      handles only the re-attach half: given `--name NAME` (either
      `--name NAME` or `--name=NAME`, anywhere in argv, respecting a
      `--` agent-args boundary via resume.py's own
      `split_agent_args`), it forwards straight to `wmf-sbx-resume`.
      Given anything else -- including the create shape, and including
      `--name NAME` *combined* with a positional (`sbx run --name X
      claude /path` still names a new sandbox in upstream, not an
      existing one) -- it refuses with an error pointing at `wmf-sbx
      create`, rather than silently doing a partial, un-cloned,
      dependency-free create, forwarding a confusing "unrecognized
      arguments" from resume's own argparse, or (worse) silently
      attaching without the mount/remote repair `wmf-sbx-run` exists to
      guarantee. This closes the muscle-memory footgun (an engineer
      typing the upstream `sbx run --name NAME` spelling out of habit
      now gets the smart wrapper instead of the raw one) without
      reviving the rejected rename's regression.

      resume.py's own docstring opening (about `sbx run --name NAME`
      leaving two things stale) got a parenthetical added: that
      staleness is now specific to the bypassed paths (`wmf-sbx
      --upstream run --name NAME`, or bare `sbx` invoked directly) --
      `wmf-sbx run --name NAME` itself redirects here first and no
      longer has the problem.

      Tests: `sbx/tests/test_wmf_sbx_run.py` (new) -- `extract_name`'s
      argv surgery (space form, `=` form, position-independence, last-
      occurrence-wins, a dangling `--name` with no value left alone for
      whatever parses `rest` next), and `main()`'s dispatch decision
      with `resume_mod.main` mocked out (delegates on `--name`, forwards
      extra flags and the `-- AGENT_ARGS` tail unchanged, passes through
      `run`/`env`/the return code, refuses and never touches resume
      without `--name`, and refuses and never touches resume when
      `--name` is combined with a create-shape positional).
      `RedirectVerbsAgreeTests` (existing, dynamic -- see the entry
      above) enforces `REDIRECT_VERBS`, `COMMANDS`, and the on-disk
      sibling all agree without needing its own update.
- [x] **Run the Cypress acceptance set** (§92). Done: §93.
- [x] **Say where the workspace CLAUDE.md is** (§93, Part A finding d),
      and amend sbx's file in place. **DONE** 2026-09-18, §95.
- [x] **Optional: say that Chrome's dbus lines are harmless** (§93).
      Headless Chrome prints dbus errors on stderr in the browser suites,
      and the Part A agent reported them. One line in the guide's browser section.
      **DONE** 2026-09-18: a paragraph in §5.2 of
      `sbx/templates/MEDIAWIKI-TESTING.md`, quoting the message.
- [x] **Allow shortcut sandbox names** - see §98 (resume/rm/start/exec)
      and §99 (cp) above.
- [ ] **Read the release notes of every new sbx release before
      upgrading**, not only for #506 (cananian, 2026-09-14). §81 is what
      that looks like when it is done properly: one pass turned up an
      idle auto-stop that lands on §4's restart window and on the
      `wmf-sbx-rm` git-daemon to-do, a `--model` behaviour change, an
      allowlist that grew, and new name validation — none of which we
      would have found by grepping for the thing we were looking for.
      The release notes are a measurement surface; treat them like one.
- [ ] **Watch #586, and comment if the thread needs it** (§82.7). Five
      arguments from our draft are not in the filed issue, listed in
      §82.7 in priority order — first among them the pre-emption of "just
      use an empty scratch directory as your primary", which is the
      cheapest way for the issue to be closed as already-possible.
      `reference/upstream/readonly-primary-workspace.md` holds the
      material. **Commenting is cananian's**, as the issue is theirs.
- [ ] **DEFERRED: give `sbx create` a throwaway scratch directory as its
      primary workspace** (§82.3 is the design; §82.5 is the deferral).
      cananian's objection, 2026-09-14: the agent cannot `/cd`, every
      restart resets cwd to the primary workspace, and §39 exists
      precisely so that cwd is the real project path — a scratch primary
      spends that on purpose. Mounting the clone over the scratch path
      fixes the contents but not the *name*, and the name is what Claude
      Code keys `~/.claude.json`'s `projects` map and the
      `~/.claude/projects/-home-…/memory/` directory off (so it would
      break §0's memory-restore steps). Also mutually exclusive with the
      "`sbx run` supplies the workspace from cwd" behaviour, since that
      only applies when `create` established no workspace. **Reopen this
      only if §82.6's request is declined**; the design and the full code
      impact list (`create.py`'s primary/extras split, the two `repos[0]`
      call sites, `setup.py`'s mount pass, `layout.json`, the warning at
      `create.py:1743`, `wmf-sbx-rm`'s cleanup) are in §82.3 and stay
      valid.
- [ ] **Look at `sbx create --profile` — a governance surface we have
      never examined** (§79.6). Spotted in the 0.42.1 create help:
      "Governance profile to assign to the sandbox". Possibly
      enterprise/centralised-policy only (§1's "Blocked by org policy"
      path), in which case it is not ours to set — but that is a guess,
      and this thread has now twice concluded "there is no knob" after
      searching too few surfaces (§78, §79.4). `sbx profile --help`, or
      whatever the sibling verb turns out to be, on the host.
- [ ] **Periodically audit front-end code for `capture_output=True` calls
      missing `stdin=subprocess.DEVNULL`.** §82.9's lesson (originally
      scoped to `create.py` alone): a one-time sweep does not stay true as
      the tree grows new call sites, and the pattern has since regressed
      into other files too — `remotes.py` (`_git`'s config/query helper,
      and the unfetched-remote `git ls-remote` check, the latter a real
      hang risk since it can prompt for an SSH host key or HTTP
      credentials with no tty attached) and `kit.py` (a `git apply`
      helper) were both found missing it in the same 2026-09-26 session
      that first wrote this item as `create.py`-only. Route 1's MCP code
      is the earlier proof — it added 4 call sites to `create.py` in one
      PR and none got the fix. `grep -n 'capture_output=True'
      sbx/src/wmf_sbx/*.py sbx/bin/*` cross-checked against
      `stdin=subprocess.DEVNULL` on the same call is a 30-second check;
      make it routine rather than incident-driven.

      **Where it matters most:** a call is riskiest when it's the *first*
      `sbx` invocation of a given command's execution — that's where an
      anomalous `sbx` prompt (expired login, a required upgrade, etc.) is
      most likely to surface, since later calls in the same run are on a
      warmed-up daemon that has already gotten past whatever the first
      call would have hit. Conditionals and early returns often make it
      genuinely hard to prove which call is first on a given codepath, so
      default to auditing (and fixing) *every* `capture_output=True` call
      the same way; reserve skipping the fix for a call you can actually
      prove can never be first in its codepath (e.g. it only runs after
      another call in the same function has already succeeded).
- [ ] **Decide whether to set `skills.defaultMode` to `off` host-wide**
      (§84.1). Belt-and-braces only — `wmf-sbx-create` always passes an
      explicit `--skills=off` itself, so this changes nothing for our own
      sandboxes. It matters only for a sandbox built without our wrapper.
      cananian's call: `wmf-sbx settings set skills.defaultMode off`.
- [ ] **Measure the idle-auto-stop resume cost** (§84.6): whether
      `git fetch <name>` fails cleanly while stopped, how long
      `wmf-sbx-resume --verify --wait=20` actually takes, and whether 20
      seconds of headroom is still enough now that this path is routine
      rather than rare. Commands are at the end of §84.6.
- [ ] **Remove the `gerrit-probe` sandbox from the host, if it is still
      there.** It was the second sandbox used for §77's cross-sandbox
      skills measurement and has served its purpose:
      `wmf-sbx-rm gerrit-probe` when convenient.
- [ ] **Use the dependency set WMF CI uses, not `extension.json` and
      `skin.json`** (design §5.6, which asked only for the phan half of
      this). `deps.py` walks the manifests, and a manifest declares what
      the extension needs *to run*. What is guaranteed to work is what CI
      checks out, and CI is quibble (https://doc.wikimedia.org/quibble/),
      which gets its repo list from `gerrit:integration/config`. So the
      manifests are a third source of truth, and the weakest of the three.

      **Where CI keeps it** [fetched from Gerrit, 2026-09-17]:
      `zuul/parameter_functions.py` loads two YAML maps from the same
      repo and injects them as the `EXT_DEPENDENCIES` and
      `SKIN_DEPENDENCIES` job parameters.

      - `zuul/dependencies.yaml` -- 330 entries, for the test jobs. An
        entry carries a `dependencies:` list and a `recurse:` flag, so
        the closure is the map's to compute, not ours.
      - `zuul/phan_dependencies.yaml` -- 201 entries, for the phan jobs
        only. A different, usually wider, set.

      **The three sets for Translate, measured** [in-sandbox
      `sbx-translate`, 2026-09-17]:

      | source | count | names |
      | --- | --- | --- |
      | `extension.json` `requires` | 1 | UniversalLanguageSelector |
      | CI `dependencies.yaml` (`recurse: false`) | 5 | UniversalLanguageSelector, cldr, EventLogging, Scribunto, VisualEditor |
      | CI `phan_dependencies.yaml` | 7 | AbuseFilter, AdminLinks, cldr, Echo, Elastica, Scribunto, TranslationNotifications |

      The walk clones the first row, so a sandbox is missing four repos
      that the test jobs have and seven that the phan job has. (Design
      §5.6 counts 32 errors on a pristine Cite tree for the same reason.)

      The phan row is exactly what Translate's `.phan/config.php` names in
      `directory_list`, and that agreement is enforced, not lucky: a
      config that names a directory the CI map does not check out fails
      in CI, because the directory is not there. **So either file can be
      the source, and which one to read is a later design decision.**
      Reading `.phan/config.php` needs no network and no second repo, but
      it means parsing PHP, which has no structured form to parse -- a
      regex over literal `'../../extensions/<Name>'` strings is a guess
      about formatting that a reformat can break. The
      `integration/config` YAML is already structured and a python script
      can read it directly, which is the reason to prefer it [cscott,
      2026-09-17]. It also covers the test-job dependencies, which the
      phan config says nothing about.

      **The layout problem, and it is the hard part** [measured
      2026-09-17]. `.phan/config.php` hard-codes `../../extensions/<Name>`,
      which is relative and assumes quibble's layout: real directories at
      `<core>/extensions/<Name>`. A wmf-sbx sandbox mirrors the
      *engineer's* layout instead, and links it into core:
      `core/extensions/Translate` is a symlink to
      `~/Projects/Wikimedia/Extensions/Translate`. The kernel resolves
      `..` from the real directory, not the logical one, so from inside
      that extension `../../extensions` is
      `~/Projects/Wikimedia/extensions`, which does not exist (and differs
      in case from the engineer's own `Extensions/`). Proof:

      ```bash
      cd /home/cananian/Projects/Wikimedia/core/extensions/Translate
      pwd      # .../core/extensions/Translate   (logical)
      pwd -P   # .../Extensions/Translate        (real)
      ls ../../extensions
      # ls: cannot access '../../extensions': No such file or directory
      ```

      So cloning the dependencies is necessary and **not sufficient**:
      the sibling paths still would not resolve.

      **How the engineer solves this on the host** [from cscott,
      2026-09-17]: a lowercase `extensions` symlink beside the real
      `Extensions` directory, so `../../extensions/<Name>` resolves to
      `../../Extensions/<Name>`. It works most of the time, and it fails
      for the repos that live somewhere else for historical reasons. This
      sandbox shows both halves of that. The parallel tree holds
      `Extensions/{Cite,Translate}` and `Skins/Vector`, but
      `UniversalLanguageSelector` sits at the top level beside them, so
      even with the symlink `../../extensions/UniversalLanguageSelector`
      would find nothing. And wmf-sbx recreates only the repos, not the
      symlink between them, so a sandbox does not get the trick at all.

      Options to weigh, none chosen:

      - *Bind-mount each repo into `core/extensions/<Name>`* instead of
        symlinking it. A bind mount resolves `..` to the mount point, so
        the relative paths work exactly as they do under quibble,
        whatever the engineer's own layout is. setup.py already knows how
        to leave an existing mount alone. This is the only option that
        makes the sandbox match CI rather than match the host.
      - *Rebuild the engineer's symlink, but better.* The sandbox knows
        where every repo is, so it can write a lowercase `extensions`
        (and `skins`) directory of per-repo symlinks, next to the repos,
        for each distinct grandparent directory a repo has -- which fixes
        the outliers the host trick misses. The cost is that phan then
        passes in the sandbox and still fails on the host, so the sandbox
        stops reproducing what the engineer sees.
      - *Clone the CI dependencies as real directories under
        `core/extensions/`* and leave the engineer's own repos
        symlinked. Nobody edits a CI dependency, so it needs no parallel
        tree. Mixed layout, and it only helps the repos we clone.
      - *Give the sandbox its own phan config*, which loses whatever the
        checked-in one says.

      **Shape of the rest of the work.**

      1. *Getting the maps*, if the YAML is the source. They are two files
         in a Gerrit repo, so either fetch them at create time over
         gitiles (one request each, and a stale-cache question when the
         host is offline) or vendor a snapshot in `sbx/` (no network, but
         it goes stale silently). `deps.py` already has the gitiles fetch,
         the retry policy and the "unreachable, do not pretend this is a
         leaf" rule to reuse. A third option is to clone
         `integration/config` itself, which is also how an engineer would
         get it by hand.
      2. *Which map applies.* Both, probably: `dependencies.yaml` for a
         sandbox that runs tests, plus `phan_dependencies.yaml` because
         `composer test` runs phan. Honour each entry's `recurse` flag
         rather than always taking the closure.
      3. *What kind of clone these get.* `walk()`'s output feeds the same
         clone, link, remote and `git safe-reset` machinery as everything
         else, and a CI dependency needs none of it: nobody edits or
         commits in one. Shallow, no host remote, and possibly not even a
         separate parallel-tree clone would be right, and no such class of
         clone exists today, so this is new plumbing in `setup.py`, not
         only a change in `deps.py`.
      4. *Keeping the manifest walk.* It still answers a question the CI
         map does not: what an extension needs in order to *load* in the
         wiki this sandbox installs. The CI maps name repos that CI
         checks out; a sandbox needs both, so the sets are unioned, not
         swapped.

      **Alternatives considered and rejected.** Generating a phan baseline
      per sandbox would silence the noise and every real undeclared-class
      error with it. Editing `.phan/config.php` to drop the missing
      directories would do the same and dirty the engineer's checkout, so
      the diff the engineer fetches would carry it.

      **When it lands**, the explanation of the noise comes out of three
      places: the "expect undeclared-symbol errors that are not yours"
      paragraph in §3 of `sbx/templates/MEDIAWIKI-TESTING.md`, trap 4 in
      its §6, and the last paragraph of the "Running tests" section in
      `kit.py`'s `HOME_CLAUDE_MD`.

      **Folded in** [2026-09-18]: this item replaces an older one, "Clone
      the sibling extensions `.phan/config.php` names", which proposed a
      regex over the literal `../../extensions/<name>` strings and shallow
      clones, and warned rather than failed on a name that does not
      resolve. The analysis above supersedes the regex; the shallow
      clones and the warning still apply. This is the one part of
      `sbx/DESIGN-testing-instructions.md` that is not done.
- [ ] **Generate the testing section per sandbox.** The testing section
      of the sandbox's CLAUDE.md is static for now: it says "if you are
      working on an extension, then ...", and the agent works out which
      case applies. The kit knows which repos a sandbox holds, so it could
      instead name each repo's actual entry points — `composer.json` and
      `package.json` scripts (including whether and how Jest is wired),
      `tests/` subdirectories, `QUnitTestModule` in `extension.json`,
      `tests/parser/*.txt` — and say which runners do not apply. Cost: a
      generator to keep correct against upstream conventions. See
      `sbx/DESIGN-testing-instructions.md` §8.
- [x] Live GitLab project-listing API research, and bare-name search
      that uses it. Done in §100: `gitlab_search`, GitLab fallback for
      bare names, and `origin`-URL identification in `reverse_resolve`.
      Still open: a live spot-check of `gitlab_search` once the network
      is fast, and the full listing with a cache (step 6).
- [x] Find the "true" upstream of a GitLab fork. Done in §100:
      `gitlab_upstream` follows `forked_from_project`, and
      `upstream_plan` points `origin` at it. Still open: a token, so
      `mr_default_target_self` is visible (anonymous calls do not see it).
      Context: https://www.mediawiki.org/wiki/GitLab/Workflows/Making_a_merge_request
- [ ] Token plumbing for the GitLab API, per
      `sbx/DESIGN-gitlab-integration.md` §2 (§55.4): host-side only, with
      the working no-token path kept, and never written into a kit spec,
      the plan JSON, or the sandbox environment.
- [x] Git remotes for cloned non-Gerrit repos. The `origin`/`local`
      split already covers them through `upstream_plan` (§52 and §100).
      **Done — host-confirmed 2026-10-06, see §102**: both the
      existing-checkout (A) and bare-name (B) scenarios below matched
      the design, including the fork-upstream repoint away from the
      user's personal fork. Commands kept for reference / re-running
      after a future change:
      ```bash
      # A. Existing checkout, named by its path -- exercises
      #    reverse_resolve()'s origin-URL fallback (no .gitreview here)
      wmf-sbx create ~/Projects/Wikimedia/wmf-claude
      cat "${XDG_STATE_HOME:-$HOME/.local/state}/wmf-sbx/sandboxes/sbx-wmf-claude.json"
      #    (rename sbx-wmf-claude below to whatever name it actually picked)

      # `origin` should be the true GitLab upstream, `local` the host
      # mirror (setup.configure_remotes, sbx/NOTES.md #52)
      wmf-sbx exec sbx-wmf-claude -- git -C ~/Projects/Wikimedia/wmf-claude remote -v
      # expect:
      #   local   git://127.0.0.1:<port>/Projects/Wikimedia/wmf-claude (fetch/push)
      #   origin  https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude.git (fetch/push)

      # origin should actually be fetchable from inside the sandbox
      wmf-sbx exec sbx-wmf-claude -- git -C ~/Projects/Wikimedia/wmf-claude fetch origin --quiet && echo "origin fetch OK"

      # the host-side remote (added by wmf_sbx.remotes, independent of
      # the GitLab lookup) should still be there too
      git -C ~/Projects/Wikimedia/wmf-claude remote -v | grep sbx-wmf-claude

      wmf-sbx rm sbx-wmf-claude

      # B. Bare name, no path at all -- exercises gitlab_search's Gerrit
      #    fallback and a *fresh* clone via clone_url()/gitlab_upstream.
      #    This clones into whatever new directory repos.yaml maps a
      #    bare "wmf-claude" to (not the existing checkout above) --
      #    check that path before running, and rm -rf it afterwards.
      wmf-sbx create wmf-claude
      ```
- [ ] Run `bin/wmf-claude-build` to completion in this sandbox (no `nono` on
      `PATH`) to fully close Phase 0 item 6.
- [ ] Extract the exhaustive `allow_domain` list from
      `profiles/wmf-engineer.json` (Phase 0 item 7) — partially addressed
      for the wiki-family subset by §15's `wiki_family_domains()`, which
      reads that list dynamically rather than hand-copying it; the item
      itself is about a fuller/general extraction, not just the kit's use.
- [ ] **An install step's own output is invisible, and not only when it
      fails** (§69.3, widened 2026-09-13 by §71.1).
      `sbx create` prints one collapsed line per install command, so
      `--mcp`'s `sudo: claude: command not found` never reached the
      terminal and a five-minute create died saying only `exit 1`. The
      `/var/log/wmf-sbx-setup.log` machinery exists for exactly this
      (§32.1) and `--settings`/`--mcp` opt out of it on the grounds that
      sbx captures install output — which it does, and then doesn't show.
      Either log those two as well (a separate file, so they can't clobber
      the record of the run that built the sandbox), or have them echo a
      failure somewhere `report_setup_problems` will find. Note the
      failing create leaves no sandbox to `sbx exec` into, so the file has
      to survive on the host side or be printed at the moment of failure.
      The `--exec-bits` step then showed the same hole from the success
      side: it printed a one-line summary designed to be read in the log
      (§71.1), ran in 67 ms, exited 0, and `grep` of the create log found
      neither the summary nor any per-file line. A step that reports its
      own findings to a channel nobody reads has not reported them.
      See https://github.com/docker/sbx-releases/issues/575 for an
      upstream bug report on this topic; we should watch this for
      improvements upstream.
- [ ] **`wmf-sbx-resume` attaches with `--continue` to a sandbox that has
      never had a session** (§73.1), which always fails —
      `No conversation found to continue`, exit 1 — and is then
      recovered by our own "forgetting this sandbox's attach flag"
      warning. Don't record the attach flag until a session has actually
      run, or check for a conversation before using it. Costs one wasted
      `sbx run` per create today; the recovery means it is a wart, not a
      bug.
- [ ] **Watch upstream for a bug fix for no-color bug**
	  Just after we implemented support for colorizing the `sbx
	  create` output it looks like it was broken upstream:
	  https://github.com/docker/sbx-releases/issues/589
	  And should be fixed in 0.44.0.
- [ ] **Re-examine the "edits to shared files" commit**
      Our code should be upstreamable w/o breaking/changing anything
	  in the default 'nono' sandbox.  The "edits to shared files"
      ought to be either separated out into their own patch with an
	  independent commit message and justification that references
      only upstream, or redone in a way where we can dynamically patch the
      "upstream" (aka nono-specific) version of the file for sbx without
	  directly modifying the files used by upstream.
- [ ] **Seam 2: make `wmf-claude` setup non-interactive**, migrated from
      `IMPLEMENTATION-PLAN.md` §6 (upstream-framed as unattended install
      for CI and onboarding automation, independent of which backend is
      in play). `bin/wmf-claude-setup` blocks on `read -rp` for the
      Phabricator username, and `setup.sh` blocks on a "Press Enter to
      continue" gate; neither can run in an image build or a CI job.
      Needs: `WMF_CLAUDE_PHAB_USER` to override the prompt (precedence:
      env var, then the value recovered from `claude mcp get
      phabricator`, then the prompt); `WMF_CLAUDE_NONINTERACTIVE=1` to
      skip the confirmation gate and turn any remaining prompt into a
      hard error rather than a hang; an audit for other blocking reads
      (`setup.sh`'s Homebrew prompt already handles EOF by declining —
      keep that). Low difficulty, under a day, per the original estimate.
- [ ] **Seam 3: factor a backend-agnostic `wmf-claude-configure` path**,
      migrated from `IMPLEMENTATION-PLAN.md` §6 (upstream-framed as what
      a CI job needs to test the plugin/MCP layer without installing a
      sandbox at all). `setup.sh` and `bin/wmf-claude-setup` both
      hard-require `nono` before reaching anything else; everything
      *after* the version-floor check and `nono pull` — build, MCP
      registration, the chrome-devtools config, the docker-egress
      templates, the shell alias — is independent of nono except the
      alias itself. Needs: extract the nono-independent portion into
      `bin/wmf-claude-configure` (build, register the Phabricator/Gerrit
      MCP servers, write `chrome-devtools-mcp/mcp-config.json`, merge
      `wiring/settings-merge.json` into `~/.claude/settings.json`);
      `bin/wmf-claude-setup` keeps the nono preflight, version floor,
      pack pull, `~/.nono/sessions` chmod, and alias install, then calls
      `wmf-claude-configure`; gate the nono-specific steps on
      `WMF_CLAUDE_BACKEND` (default `nono`); move `nono`, and only
      `nono`, out of the shared dependency loop into the backend-gated
      section (`node`, `npm`, `python3`, `claude`, `git`, `jq` stay
      shared). Low-medium difficulty, two to three days, mostly care not
      to regress the existing install path — test both
      `WMF_CLAUDE_BACKEND=nono` (unchanged) and the configure-only path.
      Both seams share the working method from the original §4: fork
      locally and stack these as small, self-contained commits on top of
      upstream `main` that don't mention `sbx`/Docker Sandboxes/a second
      backend anywhere, don't change behavior for an existing nono user,
      keep `tests/test-profile.sh`/`tests/test-templates.sh` green, and
      follow the repo's own commit conventions
      (`component: Subject`, ASD-STE100, `Assisted-by:`, no
      `Co-Authored-By:`) — they're meant to be cherry-picked upstream
      later.
- [ ] **One still-open gap from the old multi-target-git-remote
      Requirements list** (`IMPLEMENTATION-PLAN.md` §8, superseded in
      design by §15/`sbx/DESIGN-parallel-clone-tree.md`). Originally three
      checklist items; the other two are done now (see the strikethrough
      entries below), leaving just:
      - **A full `wmf-sbx ls`** listing sandboxes, their parallel clones,
        and unfetched-commit counts per clone. Today's `wmf-sbx-ls-remotes`
        only lists the host-side `sandbox-*` remotes, not the fuller
        per-sandbox/per-clone picture.
      - ~~Seed the Gerrit `commit-msg` hook into each parallel-tree
        clone~~ — done (2026-09-26): `clone_into_parallel_tree`
        (`sbx/src/wmf_sbx/setup.py`) now calls `seed_commit_msg_hook`
        right after the `git clone --shared` succeeds, copying the host
        mirror's own `.git/hooks/commit-msg` into the clone whenever the
        clone has a `.gitreview` (Gerrit-backed) and the mirror actually
        has a hook to copy. No network needed, and nothing changes for a
        non-Gerrit repo or a host checkout with no Gerrit access set up.
      - ~~`wmf-sbx cp` for copying non-git files between the host and a
        sandbox's parallel tree~~ — done, `sbx/src/wmf_sbx/cp.py` (path-
        shortcut support added on top of it in this same session, see the
        `wmf-sbx cp` items above).
- [ ] **`sandbox.resources` (CPU/memory limits)**, sbx 0.42's addition,
      not yet adopted (`sbx/DESIGN-kit-generation.md`'s 0.42.1 addendum,
      formerly `IMPLEMENTATION-PLAN.md` §16.4 item 3). A MediaWiki
      sandbox running composer, npm, and phpunit is exactly the workload
      worth bounding — add it to the generated kit spec.
- [ ] **`sbx version --json`**, not yet adopted (formerly
      `IMPLEMENTATION-PLAN.md` §16.4 item 4). Would let `wmf-sbx-create`
      state the sbx version in its setup report — the fact that made
      every dated measurement in this file need its own date, since
      behavior has changed between sbx releases more than once.
- [ ] **File the tasks in `sbx/upstream/`** (§94). Each
      `PHAB-TASK-<n>.md` is a Phabricator task in the write-phab-task
      form: (1) the REST header-case bug in core (§90 finding B); (2) the
      empty `MW_SCRIPT_PATH` guard in `wdio-mediawiki` (design §6.5); (3)
      Popups' `mw-node-qunit` false green (§93 finding A); (4) whether
      the Linux glob caveat is stale (§70.4) -- re-measure first, it has
      no patch. 1-3 each have a tested `PHAB-ATTACHMENT-<n>.patch`.
      cananian files them; the Phabricator tool here is read-only. Add
      each T-number to `sbx/upstream/README.md`. When upstream takes a
      fix, delete that pair.
      Two former tasks are gone (rebase onto upstream `main`,
      2026-10-08): the `wiring/settings-merge.json` fixes, which upstream
      made itself, and the `run-tests` PHPUnit entrypoint, which goes
      upstream as a merge request. Until it lands, keep
      `sbx/patches/plugin/01-run-tests-composer-entrypoint.patch`; delete
      it in the rebase that brings in the upstream change.
- [ ] **Find out why a container start can run no startup command — at
      the next upgrade** (§97). `sbx-translate` came up on 2026-09-19
      with no git daemon, because the startup dispatcher never ran.
      `wmf-sbx` now repairs that, but the cause is one of three: a
      v0.43.0 regression for every sandbox, a new daemon that skips
      startup for containers an *older* sbx registered, or an `sbx
      exec`-triggered start that never runs startup (which §46 measured
      as false on 0.42.1). **The v0.43.0 evidence is gone** — both probe
      sandboxes were removed on 2026-09-25 before the check ran, and no
      other rc3-era sandbox is left [cananian]. So this rides along with
      the CLAUDE.md check below, which needs the same two sandboxes:
      1. **Before** the upgrade, note which sandboxes exist; they become
         the "created before" side. `sbx-translate` is one of them.
      2. **After** it, create one, and run §97's test on both: stop,
         `wmf-sbx start`, then read the log with `wmf-sbx --upstream
         exec` (plain `exec` re-runs the dispatcher and spoils it).
      3. Repeat one start with `wmf-sbx exec` alone, for explanation 3.
      4. Keep both sandboxes until the check is done. If the answer is a
         regression, file it upstream, as one more task in
         `sbx/upstream/`.
- [ ] **Check the CLAUDE.md edits at the next sbx release upgrade**
      (§95, §96). Wait for a v0.44 or v0.45 *release*; do not move to a
      pre-release for this [cananian, 2026-09-19]. §96 measured only
      v0.43.0-rc3 → v0.43.0, where sbx's text did not change, so it
      could not show whether sbx rewrites the file when its text *does*
      change. Before the upgrade, write `sbx/RESUME.md` (NOTES §0; the
      last one is `git show 17803bb:sbx/RESUME.md`), and record the
      inode, mtime and first line of the workspace CLAUDE.md in a
      sandbox that already exists. (`sbx-claudemd-probe2` and `-probe3`
      played that part for v0.43.0 and are gone [cananian, 2026-09-25];
      make a fresh probe, or use a sandbox in use. Name the file by its
      host path — inside `wmf-sbx exec`, `~` is `/home/agent`.) The
      startup-command check in the item above wants the same two
      sandboxes, so do both in one pass. After the upgrade:
      1. Create a sandbox on the new version, and run `wmf-sbx
         refresh-claude-md <name>` (not `--dry-run`). It shows whether
         sbx's text changed, updates the snapshot, and says which edits
         fail. Fix `edits.json` until all apply, and set its
         `"upstream"`.
      2. If the text changed: start the old sandbox, and check again.
         Does sbx write its new text over the old file (new inode or
         mtime, marker gone)? If it does, check that the startup step
         edited the new text (`/var/log/wmf-sbx-claude-md.status`, the
         marker line, and `~/.claude/wmf-sbx-upstream-CLAUDE.md` holding
         the new text, not the old copy). A rewritten file has no
         marker, so the startup step should save the new text and edit
         it with the edits file the old sandbox was created with.
         If sbx does not rewrite old sandboxes, they keep the old text,
         and only new sandboxes get the new one.
      3. Read the new text for new sections that are wrong for wmf-sbx.
         Also: the kept text still tells the agent to ask for `sbx
         ports` and `sbx policy`, not `wmf-sbx ports` and `wmf-sbx
         policy`. An edit could fix that.
- [ ] **Optional: warn about `time VAR=x cmd`** (§93). The shell's `time`
      keyword does not accept an environment prefix. The guide could say
      `time env VAR=x cmd`, one line in §6 of
      `sbx/templates/MEDIAWIKI-TESTING.md`.
- [ ] **Optional: give `sbx-translate` its api-testing config.** This
      sandbox predates design §5.5, so it has no
      `.api-testing.config.json` in core and no `API_TESTING_CONFIG_FILE`.
      The §90 fix was measured with a config built by hand from
      `wmf_sbx.setup.read_secret_key` and `api_testing_config_contents`.
      Recreate the sandbox, or write the file once in core.
- [x] **Verify the §98/§99 path-shortcut work, and the Gerrit
      commit-msg-hook seeding, on the host.** Done — see §101. §98
      (resume/rm/start/exec shortcuts) and the commit-msg-hook seeding
      both matched what was designed, no changes needed. §99 (`cp`) found
      a real bug (relative container PATH rejected by upstream `sbx cp`),
      now fixed; see the item just below to re-verify that fix live.
- [x] **Re-verify the `wmf-sbx cp` relative-PATH fix (§99/§101) on the
      host.** **Done — host-confirmed 2026-10-06, see §102, test 1**:
      the shortcut-anchoring fix behaves exactly as designed, no
      regressions. Commands kept for reference:
      ```bash
      cd ~/Projects/Wikimedia/wmf-claude
      git fetch wmf-claude-sbx <branch>   # whatever branch has the fix
      git log --oneline -3                # confirm the cp.py fix is in it

      wmf-sbx create ~/Projects/Wikimedia/core
      cd ~/Projects/Wikimedia/core
      touch /tmp/probe.txt
      wmf-sbx cp /tmp/probe.txt .:bar
      # stderr should now read something like:
      #   + resolved '.:bar' to 'sbx-core:/home/cananian/Projects/Wikimedia/core/bar'
      # and the copy should succeed (no "container path must be absolute" error)

      wmf-sbx exec sbx-core -- test -f ~/Projects/Wikimedia/core/bar && echo copied

      # empty-PATH case (new: NAME: with nothing after the colon)
      wmf-sbx cp /tmp/probe.txt .:
      wmf-sbx exec sbx-core -- test -f ~/Projects/Wikimedia/core/probe.txt && echo copied

      # an already-absolute container PATH must still pass through untouched
      wmf-sbx cp /tmp/probe.txt .:/tmp/baz
      wmf-sbx exec sbx-core -- test -f /tmp/baz && echo copied

      wmf-sbx rm sbx-core
      ```
      Also re-check the other cp scenarios from §101 (absolute-path
      shortcut, sandbox-to-host direction, no-match refusal, `-D`/`-L`
      placement) still behave, since none of that logic changed.
- [x] **Re-verify the literal-sandbox-NAME anchoring extension (§102)
      on the host.** Done, 2026-10-06, against a freshly created
      `sbx-multititle` (`responses38.txt`): `wmf-sbx cp /tmp/probe.txt
      sbx-multititle:bar` correctly resolved to
      `sbx-multititle:/home/cananian/.../MultiTitle/bar`, exactly as
      §102 predicted. Doing this run surfaced a separate, previously
      unguarded bug, now written up as §103 and fixed below.

- [x] **Re-verify the §103 `cp`-right-after-`create` race fix on the
      host.** Done, 2026-10-06, same repro as §103's original find
      (`responses39.txt`, against a fresh `sbx-multititle`): the first
      `wmf-sbx cp /tmp/probe.txt sbx-multititle:bar` right after
      `create`, with no delay, now shows the `exec ... -- true` /
      `(waiting for sbx-multititle's mount layout)` sequence before the
      actual `cp`, and the very next `wmf-sbx exec sbx-multititle --
      test -f .../bar && echo copied` printed `copied` on that first
      try -- no redundant second `cp` needed, unlike §103's original
      (buggy) transcript.
- [ ] **Support sandbox-to-sandbox `wmf-sbx cp`.** Host-confirmed
      2026-09-29 (§101): plain `sbx cp SANDBOX:PATH SANDBOX:PATH` refuses
      with "ERROR: copying between sandboxes is not supported" — this is
      an upstream `sbx` limitation, not something `resolve_cp_arg` can
      paper over by rewriting arguments. Two ways to actually support it:
      1. Detect both sides resolving to `NAME:PATH` and do it as two
         upstream `cp` calls through a host-side temp file (SRC sandbox →
         temp → DST sandbox), cleaning up the temp file even on failure.
      2. If both sandboxes' primary/extra workspaces are host directories
         `wmf-sbx` already knows about, a plain host `cp`/`rsync` between
         those two host paths may be faster and avoid the sandbox git
         daemons entirely — worth checking whether that gives the same
         result the user expects (host trees can be ahead of/behind what
         `sbx cp` would read from the running container).
      Whichever approach, keep it opt-in/obvious in the `+ resolved ...`
      trace — a copy that silently goes through a host temp file is a
      surprising thing for a `cp` command to do.
- [ ] **Test --reset-all** - while debugging a different issue,
      created a sandbox with `--reset-all` and it started the
	  sandbox with my committed-but-not-upstreamed work in the
	  workspace directory.  Have a conversation about the present
	  semantics of `--reset-all`: the goal for that is to reset
	  the target to the *upstream* master/main HEAD, not the *host*
	  master/main.  That requires identifying what the "upstream"
	  really is, which should default to the gerrit.wikimedia.org or
	  gitlab.wikimedia.org remote (whatever its name is) and should
	  probably not permit --reset-all if it doesn't know what the
	  upstream is (aka what to reset *to*).
- [ ] The VisualEditor extension has a submodule; that submodule
      is not being cloned/initialized correctly. Look into this.
- [ ] For the filesystem-path shortcut to sandbox name: if the
      shortcut is ambiguous (more than one sandbox currently running
	  with the given working directory) and the input is a TTY,
	  prompt the user to select which sandbox they want instead of
	  erroring out.
- [ ] Store the user's model preference persistently.
- [ ] Right now the first time we --resume we get a "update installed,
      restart to update" message from claude.  Fix that.
- [ ] **Follow the upstream MRs** (§104): !130, !131, !132. Check their
      state with §104.1's loop; rebase per §104.2 as each one merges.
