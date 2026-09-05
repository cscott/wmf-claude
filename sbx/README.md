# wmf-sbx

A single-command launcher for MediaWiki dev sandboxes on top of Docker's
`sbx` ("Docker Sandboxes"), replacing hand-written `sbx create` lines and
`wmf-claude`'s nono-based sandboxing. One command clones the repo(s) you
name, walks their MediaWiki dependencies, wires up a running wiki, and
gives the host a git remote to pull your agent's work back out of.

Design docs and the full findings log live alongside this file
(`NOTES.md`, `SECURITY.md`, `DESIGN-*.md`) —
this is the terse how-to-use version. `RESUME.md`, when present, is a handoff written
for the Claude instance in the *next* sandbox, across an sbx upgrade;
read it first if you are that instance, and ignore it otherwise.

## Install

You should install docker sbx from
[the instructions on docker's github](https://github.com/docker/sbx-releases#manual-install-from-release-artifacts).
You should also run `./setup.sh` as described in the
[top-level README](../README.md).  Installing `glab` (aka `apt-get
install glab`) is helpful if you plan to work with gitlab repositories.

Add `sbx/bin` to your `$PATH`:

```bash
export PATH="/path/to/wmf-claude/sbx/bin:$PATH"
```

Everything here is invoked as `wmf-sbx-<command>`, or as `wmf-sbx
<command>` — `sbx/bin/wmf-sbx` redirects `create`, `exec`, `ls-remotes`,
`refresh-claude-md`, `resolve`, `resume`, `rm`, `run`, and `start` to
the matching `wmf-sbx-<command>` wrapper automatically, so both
spellings reach the same place for those nine. (`run` is narrower than the rest: it only
understands upstream's `--name NAME` re-attach spelling — the one place
`sbx run` overlaps with `wmf-sbx-resume` — and forwards there; anything
else, including the create-a-new-sandbox form, is refused with a pointer
to `wmf-sbx create`.) Never invoke bare `sbx` yourself — it [forwards your SSH agent
into every sandbox and the forwarding is sticky to the
daemon](https://github.com/docker/sbx-releases/issues/121). `wmf-sbx`
strips your SSH agent out of every invocation, redirected or not; add
`--upstream` as the first argument to reach the raw `sbx` binary instead
of a `wmf-sbx-<command>` wrapper (SSH agent still stripped).

Worth doing once, as a second layer under the wrapper:

```bash
wmf-sbx settings set ssh.agentForwardingEnabled false
```

This disables forwarding daemon-wide, so it covers an `sbx` started by
something that is not you. **It only takes effect for daemons started
after the change, and `sbx daemon restart` kills every running sandbox
without letting it save state** — every live session on the machine, not
just yours. The sandboxes come back with `wmf-sbx-start`, but the
conversations in them do not, and each one then comes back through the
restart path §4 describes. If your daemon has only ever been started
through `wmf-sbx` it has no agent to forward and there is nothing to
restart; otherwise schedule it for when nothing is running.
`SECURITY.md` §2 has the detail.

## Naming a repo

Any argument to `wmf-sbx-create` (and `wmf-sbx-resolve`, which just does
this lookup standalone) can be:

- a **bare short name** (`Cite`) — resolved by querying Gerrit for
  projects whose final path segment matches exactly. Zero or multiple
  matches is an error that lists the candidates;
- a **full project path**, with or without a scheme (`Cite` →
  `mediawiki/extensions/Cite`, or explicitly `gerrit:mediawiki/extensions/Cite`);
- an **existing local directory** — reverse-resolved back to its
  canonical project.

The canonical project path is then mapped to a local directory (existing,
or to clone into) via your `~/.config/wmf-sbx/repos.yaml` rules —
longest-match-first, e.g. `gerrit:mediawiki/extensions/{name}` →
`~/Wikimedia/Extensions/{name}`. See `DESIGN-repo-resolution.md` for the
full matching semantics and an example config.

## Dependencies are included automatically

By default `wmf-sbx-create` walks `extension.json`/`skin.json`
`requires` (and `dev-requires`, `suggests`) and adds every dependency it
finds — so `wmf-sbx-create Translate` also pulls in
`UniversalLanguageSelector`, `mediawiki/core`, `mediawiki/skins/Vector`,
etc., and symlinks them into core so the wiki actually loads. Turn this
off with `--no-deps` (mount only what you named — useful for a non-wiki
sandbox, e.g. working on `wmf-claude` itself) or narrow it with
`--no-dev` / `--no-suggests`.

## `wmf-sbx-create` options

```
wmf-sbx-create PRIMARY [EXTRA ...]
```

- `PRIMARY` — the sandbox's writable workspace; the starting cwd inside
  the sandbox. Append `:ro` to make even the primary read-only (unusual).
- `EXTRA ...` — additional repos, each independently `:ro` or not.
  `:ro` mounts the host directory read-only instead of making a writable
  clone — good for reference repos you won't edit (e.g. `core:ro` when
  you're only touching an extension: it also skips the whole MediaWiki
  setup, since that's gated on core being writable).
- `--name NAME` — sandbox name (default: `sbx-<primary repo's slug>`,
  e.g. `wmf-sbx-create Cite` → `sbx-cite`). The host remote
  `wmf-sbx-create` adds is named the same as the sandbox.
- `--kit PATH` / `--kit-out DIR` — use an explicit sbx kit instead of the
  generated MediaWiki one; `--kit-out` keeps the generated kit on disk
  for inspection.
- `--config PATH` — alternate `repos.yaml` (default
  `~/.config/wmf-sbx/repos.yaml`).
- `--no-deps`, `--no-dev`, `--no-suggests` — narrow the dependency walk
  (see above).
- `--no-mcp` — don't register the Phabricator and Gerrit MCP servers on
  the host, and leave the proxy out of the generated kit (see below).
- `--reset-all` — reset every clone to its upstream default branch,
  including the repos named on the command line (by default those keep
  whatever branch your host checkout was on).
- `--no-remotes` — don't touch host `.git/config`; print the
  `git remote add` commands instead of running them.
- `--dry-run` — print the resolution plan, the generated kit, and the
  `sbx create` line; touch nothing.

## The Phabricator and Gerrit MCP servers

`wmf-sbx-create` registers both MCP servers **on the host** (`sbx mcp
add`), where the credentials they act under live — your Phabricator
identity, your Gerrit identity. Nothing inside the sandbox ever sees
them. The sandbox gets `wmf-sbx-mcp-proxy` instead, a small
stdio shim that forwards to sbx's MCP gateway, and the generated kit
registers one proxy entry per server so the tools keep their literal
names: `mcp__phabricator__phabricator_get_task`,
`mcp__gerrit__get_commit_message`.

Each proxy entry carries a `--tools` allowlist, and only **read-only**
tools are on it — four for Phabricator, five for Gerrit. Gerrit's
fifteen writing tools (`abandon_change`, `post_review_comment`,
`create_change`, …) would act under your credential, so they are not
served to the agent at all. The allowlist is also what keeps the
gateway's flat namespace honest: without it, `mcp__gerrit__…` would
happily answer a Phabricator tool call.

A server is registered — and offered in the kit — only if it can
actually start. `wmf-sbx-create` preflights the built submodules and an
existing host registration of the same name (yours is never
overwritten), and prints why it skipped anything it skipped. `--no-mcp`
skips the whole thing.

### You need node ≥ 20.18.1 on `PATH`

Only to *register* the Phabricator server; the sandbox brings its own
node, and everything else here works regardless.

The version that matters is whatever `node --version` reports in the
shell you run `wmf-sbx-create` from. If yours is older and a server
would have to be registered, the create **stops** rather than
registering it:

```
error: the sandbox's MCP servers cannot be set up on this host:

  phabricator: /usr/bin/node is v18.19.1, below the v20.18.1 its
  dependencies need (sbx/NOTES.md §60.2)
```

This is deliberate, and not worth working around: `sbx mcp add` would
succeed, `sbx mcp ls` would report `✓ ready` — "ready" only means the
command path resolved — and the server would then fail *inside every
sandbox*, at the moment a tool is called. Worse, `sbx mcp add` resolves
the command once and stores it, so putting a newer node on `PATH`
afterwards does not repair a registration already made; it has to be
removed and re-added by hand.

Fix it whichever way suits your host:

- activate your node version manager (`nave`, `nvm`, `fnm`, `asdf`,
  `volta`, …) and re-run `wmf-sbx-create` from that shell — this is a
  one-time cost, since an existing registration is reused forever after;
- or upgrade the system node (22 LTS matches the sandbox image's);
- or pass `--no-mcp` and work without the Phabricator and Gerrit tools.

Servers **already** registered on the host are used as they are, so once
the registration exists your everyday shell's node no longer matters.

See `SECURITY.md` §7 for the threat model.

## Working with Gerrit

The loop:

1. **Create and start the sandbox**:

   ```bash
   wmf-sbx-create Cite
   wmf-sbx-resume <sandbox name>
   ```

2. **Push your host-side changes into the sandbox**, from inside
   it, to keep Claude working from the same state:

   ```
   ! git safe-reset local
   ```

   Note the `!` prefix tells Claude to execute the given shell command.
   `local` is the sandbox's remote pointing at your host checkout —
   already configured, no setup needed.

   Note that the initial sandbox is created matching whatever your
   local git HEAD was in the directories mentioned in the
   `wmf-sbx-create` command-line, so you can skip this step initially.

3. **Put Claude to work**

4. **Pull the agent's work onto the host**, from your host checkout:

   ```bash
   git safe-reset sbx-cite
   ```

   (`wmf-sbx-create` adds this remote automatically, named the same as
   the sandbox itself.) `git safe-reset` refuses if your working
   tree is dirty or you have unsubmitted local commits it would discard —
   pass `--force` past that once you're sure.

5. **Review and test on the host.** Make your own amendments if you
   want — edit, amend the commit, whatever.

6. Repeat 2–5 until you're happy.

7. **Upload for review from the host**, as usual:

   ```bash
   git commit --amend   # tidy the message, if needed
   git review
   ```

## If the sandbox's git remote seems to go away

`sbx` stops an idle sandbox on its own; a stopped sandbox publishes no
port, so `git fetch <name>` starts failing even though nothing
was removed. Bring it back without attaching an agent:

```bash
wmf-sbx-start <name>
```

This restarts the container, restores its mount layout, and re-points
the `<name>` host remote at the daemon's new port. (Reconnecting
with an agent instead — `wmf-sbx-resume <name>` — does the same repair.)

## Cleaning up

```bash
wmf-sbx-rm <name>
```

Removes the sandbox *and* the host-side `<name>` remotes
`wmf-sbx-create` added — plain `sbx rm` leaves those behind, and a
recycled host port can later make a stale remote silently point at an
unrelated sandbox. `--dry-run` to preview, `-f`/`--force` to skip the
unfetched-commit guard and confirmation prompt.

## Changing what the plugin tells a sandbox

The kit ships the wmf-claude plugin (`skills/`, `agents/`, `hooks/`,
`templates/`) into every sandbox. Nothing under `sbx/` may change those
directories in the repo: upstream wmf-claude runs on nono, and an edit
there is an edit a merge has to undo. Two places take the change instead:

- **`sbx/plugin-overlay/<path in the plugin tree>`** — a whole file, for
  text only an sbx sandbox reads. `hooks/context/sbx/environment.txt` is
  the example. The overlay wins over a repo file of the same path.
- **`sbx/patches/plugin/NN-name.patch`** — a unified diff (`git diff`
  output, paths relative to the repo root), for a file the nono backend
  shares. The kit applies the patches in name order to the copy it
  installs. A patch that no longer applies fails the kit build and the
  test suite, which is the signal to refresh it against the new upstream
  text or to drop it.

A fix that is right for nono too belongs in the shared file, offered
upstream. See `DESIGN-testing-instructions.md` §6.

## When sbx's own CLAUDE.md changes

sbx writes a CLAUDE.md of its own into every sandbox, in the parent
directory of the primary workspace. Some of its sections are wrong for a
wmf-sbx sandbox (its "Git workspace mode", for one). A startup step
edits them, using the heading-keyed edits in
`sbx/patches/sbx-claude-md/edits.json`. `upstream.md` beside it is the
sbx text those edits were written against.

A new sbx release can change that text enough that the edits no longer
apply. The sandbox still works, but `wmf-sbx create`, `wmf-sbx resume`
and `wmf-sbx start` print a "CLAUDE.md edit failed" error. To fix it:

```bash
wmf-sbx refresh-claude-md <name>
```

This copies sbx's new text into `upstream.md`, shows the diff, and says
which edits fail. Change `edits.json` until the command passes, run the
unit tests, and commit both files. See `NOTES.md` §95.
