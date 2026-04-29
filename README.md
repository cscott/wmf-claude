# wmf-claude

Use [Claude Code](https://claude.ai/code) safely as a
Wikimedia Foundation engineer: a sandboxed runtime with
Phabricator and Gerrit integration, access to Wikimedia
domains and language docs, and reduced risk of accidental
pushes or credential exposure.

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
./tests/test-profile.sh
```

Requires `nono` and `jq`.
