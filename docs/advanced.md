# Advanced usage

Reference material that most people never need. Start with the
[README](../README.md).

## Full flag reference

Two different kinds of flag, which `bin/claude` treats differently.

**Wrapper flags** are consumed by `bin/claude` itself and never reach nono.
They must appear **before** `--`; after it they are passed to Claude Code as
unknown options and do nothing:

| Flag | Effect |
|---|---|
| `--local-web[=PORTS]` | Open localhost web ports — see [local testing](local-testing.md) |
| `--chrome` | Load the chrome-devtools MCP; implies `--local-web` |
| `--docker[=SERVICE[:WORKDIR]]` | Run dev tools in a container through the broker |
| `--egress=none\|allowlist` | Verify the container's egress override at startup. **Requires `--docker=SERVICE`** — with a bare `--docker` (hand-started broker) it exits with an error. |
| `--minimax` | Allow egress to `api.minimax.io` (MiniMax models) for this session. Not in the static profile. |
| `--allow-post=HOSTS` | Allow POST (and every other method) to hosts that the profile keeps read-only, for this session. Comma-separated; a host under a profile wildcard works (`--allow-post=test.wikipedia.org`), as does the wildcard itself (`--allow-post='*.wikidata.org'`). A host that the profile does not list read-only is refused, and so are phabricator and gitlab (all methods there would open writes such as `git push`). |
| `--local-db[=PORT]` | Open the local MariaDB/MySQL port (default 3306) for this session. Not in the static profile. |
| `--landlock-only` | **Linux only.** Required with any flag that opens a localhost port, until [nolabs-ai/nono#1786](https://github.com/nolabs-ai/nono/issues/1786) is fixed. Runs nono with `--sandbox-policy landlock`, which weakens egress (see [SECURITY.md](../SECURITY.md)). Refuses ports 80 and 443; `--local-web` defaults to 8080. |
| `--ide` | Connect to the open JetBrains/VS Code plugin window: opens its localhost port and runs with `signal_mode: allow_all`. Automatic when `CLAUDE_CODE_SSE_PORT` is set: the plugin sets it when it launches `claude`, and in the IDE's own terminal. |

**nono flags** go before `--`; everything after `--` is passed to Claude:

| Flag | Effect |
|---|---|
| `--allow PATH` | Read + write another directory |
| `--read PATH` | Read-only access to another directory |
| `--allow-command CMD` | Permit a command the profile normally blocks |
| `--override-deny PATH` | Lift a deny rule for one path (pair with `--read-file`) |
| `--read-file PATH` | Grant a single file, rather than a directory |

Example — read a sibling repo for context while keeping writes confined, and
lift one deny:

```bash
claude --read ~/src/mediawiki/extensions \
  --override-deny ~/.kube/config --read-file ~/.kube/config --
```

`bin/claude` **rejects** `--capability-elevation`, `--trust-override`, and
`--dangerously-skip-permissions`. Sandbox-weakening flags don't compose with the
threat model, so there is no supported way to pass them.

## Environment variables

| Variable | Effect |
|---|---|
| `WMF_CLAUDE_PROFILE` | Load `profiles/<name>.json` instead of `wmf-engineer` |
| `WMF_CLAUDE_NO_PAUSE` | Skip the Enter pause that follows startup notices. (`WMF_CLAUDE_QUIET`, which hid the old banner, is now a no-op.) |
| `WMF_CLAUDE_SKIP_UPDATE` | Skip the update check and prompt entirely |
| `WMF_CLAUDE_NONO_VERBOSE` | Show nono's banner and its exit summary of denied paths (off by default: most denials there are expected) |
| `WMF_CLAUDE_SKIP_CHROME` | Skip building the chrome-devtools MCP during setup |

## Startup notices and the status line

Claude Code redraws the terminal as it starts, so anything the launcher printed
is gone almost immediately. Two things compensate:

- When `bin/claude` printed a notice that needs action (Chrome not started yet,
  a policy file that differs from the commit, a failed update), it waits up to
  60 s for Enter before starting Claude Code. A clean launch does not pause, and
  neither does `claude -p` or a piped launch (VS Code). `WMF_CLAUDE_NO_PAUSE=1`
  skips the pause.
- The status line shows the session's sandbox state for as long as it runs:
  `WMF nono sandbox · --local-db --docker=mediawiki (egress none) --allow ~/src`,
  plus a note when the install is behind `origin/main`. Notices about that state
  (behind count, Docker service, egress mode) are printed but do not pause. If
  you have your own `statusLine` in `~/.claude/settings.json`, its output is
  appended after the sandbox segment, not replaced. It is read once, at launch,
  from the user-level file only: inside the sandbox the project's `.claude/` and
  `~/.claude` are agent-writable, so the status line never takes a command from
  a file there.

## Using a different security profile

Drop a profile at `profiles/<name>.json` and launch with it:

```bash
WMF_CLAUDE_PROFILE=wmf-data-scientist claude
```

`bin/claude` passes the profile to nono by absolute path, so nothing needs to be
copied into `~/.config/nono/profiles/`. A custom profile should still
`extends: claude-code`, which resolves against the installed `nolabs-ai/claude`
pack.

## Workspace layout

Clone each repo to a path mirroring its Gerrit/GitLab project path, so a project
name maps to a predictable location:

```
~/src/
  mediawiki/core
  mediawiki/extensions/GrowthExperiments
  integration/quibble
  operations/puppet
  repos/product-safety-and-integrity/wmf-claude
```

This matters more than it looks: the directory you launch from is both your
write blast radius and the key for Claude's per-project memory and `CLAUDE.md`.
A stable entrypoint per area accumulates useful context over time, while
launching from a broad root like `~/src` dumps every project into one memory
store and grants write access to all of them.

## Bypassing the sandbox

To run Claude Code unsandboxed for a single invocation, skip the alias:

```bash
\claude          # or: command claude
```

If you already had a `claude` alias when you ran `setup.sh`, it was left alone
and the alias line was printed for you to install by hand.

Fish users get an `abbr` in `~/.config/fish/conf.d/wmf-claude.fish` that expands
inline, so the sandbox invocation stays visible before you press Enter.

## Updating

`bin/claude` checks on every launch and, when your install is on `main` with a
clean tree, lists the pending commits and offers to update in place. (The
once-a-day throttle applies to the background `git fetch` that refreshes the
count, not to the check or the prompt — while you are behind, you are asked
each launch.) Say yes and it
fast-forwards, updates the submodules, runs `setup.sh`, and relaunches on the
new version.

On a feature branch or a dirty tree it won't touch anything — it prints the
command instead. That is also the command to use for a manual update:

```bash
git -C <install> pull \
  && git -C <install> submodule update --init --recursive \
  && <install>/setup.sh
```

The submodule step is not optional. `git pull` leaves submodules at their old
SHAs, and `bin/wmf-claude-build` only initializes them when they are missing, so
skipping it silently keeps a stale MCP server.

If an update is interrupted after the checkout has moved — Ctrl-C in `setup.sh`,
say — `bin/claude` prints `UPDATE INCOMPLETE` with the command to finish it on
every launch until `setup.sh` completes. If a fast-forward is refused (an
untracked file upstream now tracks, for example) you are not asked again for
that revision; the command is printed instead.

Set `WMF_CLAUDE_SKIP_UPDATE=1` to silence the check entirely.

## Submitting patches

The sandbox blocks SSH — Gerrit's port 29418 and GitHub's port 22 — so
`git review` and any SSH push fail from inside a session. This is deliberate:
it keeps an agent from publishing code on your behalf.

Work with it rather than around it. Claude stages commits and writes the
messages inside the session; you run the push from a normal terminal, outside
the sandbox:

```bash
git review        # your usual Gerrit push, from a regular shell
```

HTTPS push to Gerrit *is* reachable from inside the sandbox (it stays a plain
tunnel), but only with a Gerrit HTTP password. The sandbox denies
`~/.gitcookies` and `~/.git-credentials`, but not the macOS keychain, where
git's `osxkeychain` helper keeps a password you have used before — see
[residual risks](../SECURITY.md#residual-risks). HTTPS push to GitLab is
blocked: the profile allows `git-upload-pack` (fetch) there, not
`git-receive-pack`.

## Troubleshooting

**Something fails with "Permission denied" or "Operation not permitted".**
Relaunch with `WMF_CLAUDE_NONO_VERBOSE=1 claude` and repeat the step. At exit,
nono lists the paths it denied. Most are expected (`~/.ssh`, browser
profiles, `/home` paths the Claude binary probes). Ignore its "Fix flags"
line: it suggests grants such as `--read ~`, which open far more than the
failing step needs. Grant the narrowest path instead, with
`claude --read DIR -- ...` or `--allow DIR`.

**MCP servers registered twice.** If you previously added the Phabricator or
Gerrit MCP servers at project scope, they conflict with the global registration:

```bash
claude mcp remove gerrit -s local
claude mcp remove phabricator -s local
```

**Phabricator MCP calls fail, but `/mcp` shows it connected.** A Phabricator
API token puts the MCP in Conduit mode, which sends every read as a POST to
`/api/<method>`. The profile refuses that. Without a token the MCP scrapes the
web interface, which the profile allows. `bin/launch-claude.sh` blanks
`PHABRICATOR_API_TOKEN`, so a token in `mcp-phabricator/.env` has no effect.
A token in the MCP entry itself overrides that. `bin/claude` warns at launch
when the user-scope entry (`claude mcp add -e PHABRICATOR_API_TOKEN=…`) has
one; `./setup.sh` re-adds that entry without it. Other places also override
the blank value, and `bin/claude` does not check them: a local-scope entry
(`claude mcp remove phabricator -s local`, run inside that repository), a
project `.mcp.json`, and an `env` block in a `settings.json`. Remove the token
there. The value `${PHABRICATOR_API_TOKEN}` is fine: it expands inside the
sandbox, where the token is blank.

**`setup.sh` says nono is too old.** Upgrade it first, then re-run:

```bash
brew upgrade nono     # or a fresh .deb / .rpm from the nono releases page
./setup.sh
```

**`--chrome` fails to connect.** The MCP attaches to a Chrome started *outside*
the sandbox. Run `bin/launch-test-chrome` in a separate terminal first.

**A path or domain is denied.** That is the sandbox working. Grant it
deliberately with `--allow` / `--read` rather than reaching for a bypass; see
[`SECURITY.md`](../SECURITY.md) for what is denied and why.

**Still stuck.** File it on the
[#WMF-Claude](https://phabricator.wikimedia.org/tag/wmf-claude/) workboard in
Phabricator, with the profile and flags you launched with and the nono denial
if there is one. For quick questions, ask in
[#ai-coding](https://wikimedia.enterprise.slack.com/archives/C0ATKE72JG6) in
Wikimedia Slack.
