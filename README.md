# wmf-claude

Use [Claude Code](https://claude.ai/code) safely and
productively as a Wikimedia Foundation engineer:

- **Sandboxed runtime** — Claude runs inside a
  [nono](https://github.com/always-further/nono) sandbox
  with Phabricator and Gerrit MCP integration, access to
  Wikimedia domains and language docs, and reduced risk
  of accidental pushes or credential exposure
- **Claude Code plugin** — skills for commit messages,
  Phabricator tasks, and Gerrit patch review; agents for
  code review, MediaWiki development, and analytics
  notebooks

## Prerequisites

Install these before running setup:

- **[nono](https://github.com/always-further/nono)** — the
  sandbox runtime (**0.44 or newer**; 0.44 moved the
  `claude-code` profile to a registry pack that `setup.sh`
  pulls). See nono's README for platform-specific install
  instructions.
- **Claude Code** — install via the
  [official installer](https://docs.claude.com/en/docs/claude-code/setup).
- **Node.js + npm** — for the Phabricator MCP server.
- **Python 3** — for the Gerrit MCP server. `uv` is used
  if available, otherwise `python3 -m venv`.
- **`jq`** — only needed if you run `./tests/test-profile.sh`.

You'll also need a Phabricator username (the one you log
into [phabricator.wikimedia.org](https://phabricator.wikimedia.org)
with) — `setup.sh` will prompt for it.

## Install

```bash
git clone --recurse-submodules \
  https://gitlab.wikimedia.org/kharlan/wmf-claude.git
cd wmf-claude
./setup.sh
```

`setup.sh` will append a `claude` alias to your shell
config (`~/.zshrc` or `~/.bashrc`) — reload your shell to
pick it up:

```bash
source ~/.zshrc   # or ~/.bashrc
```

The alias shadows the system `claude` binary so plain
`claude` runs sandboxed by default. To bypass the sandbox
for a single invocation, use `\claude` or `command claude`.

If you already have a different `alias claude=` in your
rc file, setup will leave it alone and print the line for
you to install manually.

From any project directory:

```bash
claude
```

## Verify

Confirm the MCP servers registered:

```bash
claude mcp list
```

You should see both `phabricator` and `gerrit`. Optionally
run the profile tests (requires `jq`):

```bash
./tests/test-profile.sh
```

## Plugin (skills + agents)

The repo is a **Claude Code plugin** — when loaded, you get
WMF-specific skills and agents. The wrapper `bin/claude`
auto-loads it via `--plugin-dir`, so plain `claude` (the
alias) gives you everything.

You'll see a startup banner immediately before Claude Code's
own banner (it fires *after* nono's capability table so it
doesn't get scrolled past):

```
  ╭─ WMF Claude ─────────────────────────────────────────╮
  │  sandboxed by nono · plugin: wmf-claude              │
  │  Use /wmf-claude:init-project in a repo to bootstrap │
  ╰──────────────────────────────────────────────────────╯
```

(Suppress with `WMF_CLAUDE_QUIET=1`.)

**Skills** (namespaced as `/wmf-claude:<name>`):

| Skill | What it does |
|-------|--------------|
| `/wmf-claude:write-commit-msg` | Drafts WMF-format commit messages — `component:` subject, `Why:`/`What:` body, `Assisted-by:` + `Bug:` + `Change-Id:` trailers. Verifies the `Bug:` ref via the Phabricator MCP. |
| `/wmf-claude:write-phab-task [component] [desc]` | Generates Phabricator task markdown |
| `/wmf-claude:review-patch [change-id]` | Fetches and reviews a Gerrit change |
| `/wmf-claude:init-project [--mediawiki]` | Drops a starter `CLAUDE.md` into the current repo |
| `/wmf-claude:run-tests`, `/wmf-claude:test-coverage`, `/wmf-claude:lint`, `/wmf-claude:manual-test`, `/wmf-claude:compare-rebase` | MediaWiki workflows (no-ops outside MW repos) |

**Agents** (auto-invoked by description):

| Agent | When it fires |
|-------|---------------|
| `gerrit-reviewer` | Reviewing a Gerrit patch |
| `jupyter-notebook` | Creating/editing notebooks for WMF analytics (stat1010, `wmfdata`) |
| `mediawiki-dev`, `mediawiki-explore`, `test-writer` | MediaWiki development tasks |

A `SessionStart` hook (`bin/session-start.sh`) puts a
short context block into Claude's prompt so it knows it's
running inside the WMF environment regardless of how the
plugin was loaded.

### Three install paths

1. **Local dev (default for setup.sh)** — the `claude`
   alias passes `--plugin-dir <wmf-claude>` to load this
   checkout directly. `git pull` to update.
2. **Claude Code marketplace via git** *(future)* — once
   the marketplace JSON is reachable from your projects,
   `/plugin marketplace add <repo-url>` then
   `/plugin install wmf-claude@wikimedia` will work.
3. **Signed nono pack** *(future)* — `package.json` is
   in the repo; once GitLab CI signing is supported, a
   tagged release will publish to the nono registry and
   engineers can `nono pull kharlan/wmf-claude` to install
   with cryptographic verification.

### Per-project CLAUDE.md

CLAUDE.md is inherently per-project. Run
`/wmf-claude:init-project` (optionally `--mediawiki`)
inside a repo to drop a starter `CLAUDE.md` you can
customize. It refuses to overwrite an existing one.

### Defense-in-depth settings

The signed nono pack will merge the following into
`~/.claude/settings.json` at install time. Until then,
the same JSON lives in `wiring/settings-merge.json` for
reference — copy fragments manually if you want them
applied today.

- **`WebFetch` is in `ask`** (not `allow`) — Claude
  prompts the engineer for each new domain. nono doesn't
  gate `WebFetch` because it's fetched server-side by
  Claude's API rather than from the laptop, so this is the
  primary barrier on egress to attacker URLs and the most
  important rule in this list.
- **`sandbox.enabled: false`** — nono is the OS-level
  security boundary; Claude Code's softer in-process
  `sandbox` would add friction without restricting an
  already sandboxed session.
- **`Edit/Write` blocked on per-project `.claude/hooks/**`
  and `.claude/settings.json`** — Claude can't tamper with
  engineer-defined per-project hooks or broaden its own
  allowlist mid-session.
- **`Edit/Write` blocked on user-global `~/.claude/`
  configuration** (`settings.json`, `settings.local.json`,
  `hooks/`, `plugins/`, `agents/`, `skills/`) — a
  prompt-injected Claude can't install/disable plugins,
  rewrite the user-global allowlist, or replace shipped
  agent/skill definitions for future sessions. The nono
  pack itself writes to these paths at install time via the
  nono CLI, which runs outside Claude's tool surface.
- **`Bash(find:* -exec*)`, `-execdir`, `-delete`, `-ok`,
  `-okdir`, `-fprint*` blocked** — `find` only allows
  read-only traversal forms; `-exec` is otherwise
  effectively shell escape.
- **`Bash(git config core.hooksPath:*)` blocked** — no
  redirecting commit hooks to attacker-controlled paths.
- **`Read(.git/config)` and `Read(.git/credentials*)`
  blocked** — credentials embedded in remote URLs aren't
  leaked into context.

## What you get

- **Phabricator MCP** — look up tasks, search, read
  comments directly from Claude
- **Gerrit MCP** — query changes and reviews
- **Wikimedia network access** — all `*.wikimedia.org`,
  `*.mediawiki.org`, `*.wikipedia.org`, codesearch, and
  the rest of the wiki family
- **Documentation sites** — php.net, MDN, docs.python.org,
  docs.rs, doc.rust-lang.org, nodejs.org, pkg.go.dev
- **Working directory read+write** — Claude can edit your
  code, run tests, use git (but not push)

## What's blocked

- `git remote`, `ssh`, `scp`, `sftp` — no remote URL
  tampering or remote access
- SSH-based `git push` (including Gerrit on port 29418
  and GitHub) — blocked at the network layer
- `~/.ssh`, `~/.gnupg`, `~/.netrc`, `~/.npmrc`,
  `~/.pypirc`, `~/.composer/auth.json`, `~/.docker/config.json`,
  `~/.kube/config`, `~/.config/gh/` — credentials stay
  private
- `~/.password-store`, `~/.config/bitwarden`,
  `~/.config/keepassxc`, `~/Library/Application Support/{1Password,Bitwarden,Enpass}`
  — password managers and secret stores
- `~/Library/Mail`, `~/Library/Messages`, `~/.thunderbird`,
  Slack/Discord/Signal/Telegram app data — private
  communications can't be read
- `~/Library/Mobile Documents` — iCloud Drive contents
- `~/.bashrc`, `~/.zshrc`, `~/.profile` (and variants) —
  shell configs can't be read or modified
- Destructive commands (`rm`, `sudo`, `chmod`, `mv`, etc.)
  are blocked by the base nono profile
- All network access except LLM APIs and the allowlisted
  domains above

**Push caveat:** HTTPS push to a Wikimedia host (e.g.
`gitlab.wikimedia.org`) can still succeed if you have a
token stored in your macOS keychain — the keychain is
reachable inside the sandbox. The standard WMF Gerrit
SSH workflow is fully blocked.

**Note:** Claude can make HTTP requests to allowlisted
Wikimedia domains. While credential files are blocked,
be aware that Wikimedia APIs are reachable within the
sandbox.

## Granting additional access

Pass nono flags before `--` to allow extra paths:

```bash
# Read+write access to another directory
claude --allow ~/src/mediawiki --

# Read-only access
claude --read ~/src/schemas/event/secondary

# Allow a normally-blocked command for a session
claude --allow-command rm --

# Override a deny rule for a specific path
claude --override-deny ~/.kube/config \
  --read-file ~/.kube/config --
```

Flags before `--` go to nono, flags after go to claude.
If there's no `--`, everything goes to claude.

`--allow-command` and `--override-deny` are permitted
because it's better to make targeted exceptions within
the sandbox than to bypass it entirely. The following
flags are blocked by the wrapper:

- `--capability-elevation` — the profile disables this
- `--trust-override` — disables trust verification
- `--dangerously-skip-permissions` (claude flag) —
  bypasses all permission checks

## VS Code

Set the process wrapper in your VS Code settings to use
the sandbox with the Claude Code extension:

```json
{
  "claudeCode.claudeProcessWrapper":
    "/absolute/path/to/wmf-claude/bin/claude"
}
```

## How it works

This repo uses [nono](https://github.com/always-further/nono),
a capability-based sandbox, to run Claude Code with
restricted filesystem, network, and command access.

`setup.sh` installs the nono profile, sets up both MCP
servers (npm + pip/uv), registers them globally in Claude
Code, and appends a shell alias to `~/.zshrc` or
`~/.bashrc`. `bin/claude` is a thin
wrapper that launches `nono run` with the right profile
and grants access to the bundled MCP server submodules.

## Keeping it up to date

Pull the latest changes and re-run setup to pick up new
profile rules or MCP server updates:

```bash
git pull
git submodule update --init --recursive
./setup.sh
```

## Troubleshooting

If you previously configured Phabricator or Gerrit MCP
servers at project scope, remove them to avoid conflicts:

```bash
claude mcp remove gerrit -s local
claude mcp remove phabricator -s local
```

## Running tests

```bash
./tests/test-profile.sh    # nono profile / sandbox behaviour (requires nono, jq)
./tests/test-templates.sh  # plugin manifest, skill/agent frontmatter, JSON validity
```

`test-profile.sh` cannot run inside a nono sandbox (nested
sandboxing doesn't work) — run directly or in CI.
`test-templates.sh` is filesystem-only and works anywhere
`python3` and `jq` are available.
