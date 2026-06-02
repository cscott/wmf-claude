# wmf-claude

Reduce the risk of using [Claude Code](https://claude.ai/code)
as a Wikimedia Foundation engineer. Claude runs inside a
[nono](https://github.com/always-further/nono) sandbox
with Phabricator and Gerrit MCP integration, scoped
network access (Wikimedia + language docs), and
defense-in-depth permission rules to reduce the risk of
accidental pushes or credential exposure.

## Quick start

```bash
git clone https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude.git
cd wmf-claude
./setup.sh
source ~/.zshrc   # or ~/.bashrc, to pick up the new `claude` alias
```

Then run `claude` from any project directory and it runs inside the
sandbox.

`./setup.sh` is interactive: it shows you what it's about to do and
waits for you to press Enter before touching anything. Re-run it any
time to update; it's safe to re-run.

### Before you run setup

wmf-claude is developed and tested on macOS. It also runs on Linux, with
one caveat: Claude Code ignores glob patterns in its permission deny rules
on Linux, so the nono sandbox (not the Claude Code permission layer) is what
blocks sensitive files inside your workdir. Avoid keeping secrets in your
workdir on Linux!

Installation hints below cover macOS (Homebrew) and Debian-like Linuxes (apt).

`setup.sh` checks for these and exits with install hints if any are
missing. Install them up front:

- **[nono](https://github.com/always-further/nono)** 0.56+, the sandbox runtime (tested version is pinned in [`.nono-version`](./.nono-version); CI installs that version)

  macOS:

  ```bash
  brew install nono
  ```

  Debian / Ubuntu (download the `.deb` from
  [GitHub Releases](https://github.com/always-further/nono/releases)):

  ```bash
  NONO_VERSION=$(cat .nono-version)
  wget "https://github.com/always-further/nono/releases/download/v${NONO_VERSION}/nono-cli_${NONO_VERSION}_$(dpkg --print-architecture).deb"
  sudo dpkg -i "nono-cli_${NONO_VERSION}_$(dpkg --print-architecture).deb"
  ```

- **[Claude Code](https://docs.claude.com/en/docs/claude-code/setup)**

  ```bash
  curl -fsSL https://claude.ai/install.sh | bash
  ```

- **Node.js + npm**, for the Phabricator MCP server

  macOS: `brew install node`
  Debian: `sudo apt install nodejs npm`

- **Python 3** (uv preferred, falls back to `venv`), for the Gerrit MCP server

  macOS: `brew install python3`
  Debian: `sudo apt install python3` (already included on most systems)

- **[uv](https://docs.astral.sh/uv/)** (optional but recommended), builds the
  Gerrit MCP server and fetches its own Python

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

  Or follow the [official install instructions](https://docs.astral.sh/uv/getting-started/installation/)
  for alternative methods.

- **jq**, for parsing JSON config during setup

  macOS: `brew install jq`
  Debian: `sudo apt install jq`

- **git**

  macOS: `brew install git`, or `xcode-select --install`
  Debian: `sudo apt install git`

Have your **Phabricator username** handy too (the one you log into
[phabricator.wikimedia.org](https://phabricator.wikimedia.org) with);
`setup.sh` prompts for it.

### What `./setup.sh` does

After you press Enter, it:

1. Updates Claude Code to the current version.
2. Copies the nono profile to `~/.config/nono/profiles/`.
3. Builds both MCP servers (npm + pip/uv) in this checkout.
4. Registers the phabricator + gerrit MCP servers globally with Claude Code.
5. Appends a `claude` shell alias to `~/.zshrc` or `~/.bashrc` that runs sandboxed by default.

Fish users get an `abbr` in `~/.config/fish/conf.d/wmf-claude.fish`
that expands inline so the sandbox path is visible.

If you already have a different `claude` alias (`alias claude=` for
bash/zsh, `abbr -a claude` for fish), setup leaves it alone and prints
the line for you to install manually. To bypass the sandbox for a
single invocation, use `\claude` or `command claude`.

### Verify it worked

```bash
claude mcp list   # should list phabricator and gerrit
```

Then run `claude` from any project directory.

## Recommended workspace layout

The sandbox grants read-write access to the directory you launch
`claude` from, and everything beneath it. Parents and siblings stay
out of reach unless you grant them explicitly, so where you start
`claude` decides what it can touch.

For work that spans repos, keep all your clones under one root (for
example `~/src`) and launch `claude` from that root:

```bash
cd ~/src
claude
```

Claude can then read and edit any repo in the tree within a single
session, rather than one repo per launch.

Clone each repo to a path that mirrors its Gerrit or GitLab project
path, so a project name maps to a predictable location:

```
~/src/
  mediawiki/core
  mediawiki/extensions/GrowthExperiments
  operations/puppet
  repos/product-safety-and-integrity/wmf-claude
```

This avoids duplicate clones and lets Claude resolve a project name to
its checkout without guessing.

To keep launching `claude` from inside a single repo instead, grant the
shared root explicitly:

```bash
claude --allow ~/src --
```

## Skills and agents

The repo is also a Claude Code plugin — the `claude`
alias auto-loads it, so you get WMF-specific skills and
agents out of the box. (Suppress the session banner with
`WMF_CLAUDE_QUIET=1`.)

**Skills** — namespaced as `/wmf-claude:<name>`:

| Skill | What it does |
|-------|--------------|
| `write-commit-msg` | Drafts a WMF-format commit message for staged changes (component subject, Why/What body, Bug/Change-Id trailers). Verifies `Bug:` refs via Phabricator MCP. |
| `write-phab-task` | Generates Phabricator task markdown |
| `review-patch` | Fetches and reviews a Gerrit change |
| `init-project [--mediawiki]` | Drops a starter `CLAUDE.md` into the current repo |
| `run-tests`, `test-coverage`, `lint`, `manual-test`, `compare-rebase` | MediaWiki workflows |

**Agents** auto-invoke by description match:
`gerrit-reviewer`, `mediawiki-dev`, `mediawiki-explore`,
`test-writer`, `jupyter-notebook` (WMF analytics:
stat1010, `wmfdata`).

## Granting additional access

Pass nono flags before `--`; everything after goes to
Claude:

```bash
# Read+write another directory
claude --allow ~/src/mediawiki --

# Read-only
claude --read ~/src/schemas/event/secondary --

# Allow a normally-blocked command
claude --allow-command rm --

# Override a deny rule for a specific path
claude --override-deny ~/.kube/config \
  --read-file ~/.kube/config --
```

The wrapper rejects `--capability-elevation`,
`--trust-override`, and `--dangerously-skip-permissions`
— sandbox weakening flags don't compose with the threat
model.

## VS Code

Point the Claude Code extension at the wrapper:

```json
{
  "claudeCode.claudeProcessWrapper":
    "/absolute/path/to/wmf-claude/bin/claude"
}
```

## Per-project CLAUDE.md

Run `/wmf-claude:init-project` (optionally `--mediawiki`)
inside a repo to drop a starter `CLAUDE.md`. It refuses
to overwrite an existing one.

## Local-wiki testing

The `manual-test` skill verifies changes against your local dev wiki,
cheapest tier first, so a full browser session isn't spent on checks a
request can answer. Both tiers are opt-in per session because they open
localhost ports the sandbox otherwise denies.

**Tier 1 — curl + the API.** `claude --local-web` opens the localhost
web ports (default 80/443/8080) so the session can curl your wiki and
hit `/w/api.php` for HTTP status, redirects, rendered HTML, and API
responses. Narrow to your own setup with `--local-web=443` (or a
comma-separated list). No browser, no MCP loaded, and the output is
plain text you can `/compact` away. `--open-port` is localhost-only, so
this does not widen external network access.

**Tier 2 — chrome-devtools MCP.** For screenshots, console errors,
accessibility snapshots, and click-through checks. Enable it once by
answering `y` at the chrome-devtools prompt in `./setup.sh`, then launch
with `--chrome` (which implies `--local-web`). Chrome runs outside the
sandbox (it calls IOKit at startup and can't run inside), so start it in
a separate terminal first:

```bash
bin/launch-test-chrome   # one terminal: Chrome outside the sandbox, throwaway profile
claude --chrome          # another: curl tier + the chrome-devtools MCP
```

The MCP attaches over `127.0.0.1:9222`. That port is unauthenticated
and reachable by other local processes, so use throwaway dev-wiki
accounts only, never real credentials.

## Updating

```bash
git pull
git submodule update --init --recursive
./setup.sh
```

In an interactive terminal, the `claude` wrapper reminds you when an update
is available: it prints a one-line notice if your installed nono is older
than the pinned version, or if your wmf-claude checkout is behind
`origin/main`. It only notifies, it never pulls for you. The remote check
runs as a background `git fetch origin` at most once a day, so it never
slows a launch, and the behind-count clears as soon as you pull. The check
is skipped for non-interactive runs such as `claude -p` and the VS Code
extension. Silence it anywhere with `WMF_CLAUDE_SKIP_UPDATE=1`.

## Submitting patches

The sandbox blocks SSH (Gerrit port 29418, GitHub port 22), so the
standard `git review` push does not work from inside a `claude`
session. Claude can stage commits and write commit messages, but the
push itself runs in your normal terminal, outside the sandbox:

```bash
git review        # or your usual Gerrit push, from a regular shell
```

HTTPS push to Wikimedia hosts is reachable from inside the sandbox, but
only with a Gerrit HTTP password configured, which most engineers on
the SSH workflow do not have. See [`SECURITY.md`](SECURITY.md) for the
reasoning behind blocking SSH.

## Security model

Network is deny-by-default with Wikimedia, wiki-family,
and language-doc domains allowlisted. SSH push (Gerrit
29418, GitHub 22) is unreachable. Credentials, password
managers, shell configs, and private comms are blocked
at the filesystem layer. See [`SECURITY.md`](SECURITY.md)
for the full threat model, allowlist, and residual-risk
analysis.

**Push caveat:** HTTPS push to a Wikimedia host can
still succeed if you have a token in your macOS keychain
— the keychain is reachable inside the sandbox so
Claude Code's `/login` works. The standard WMF Gerrit
SSH workflow is fully blocked.

## Troubleshooting

If you previously configured Phabricator or Gerrit MCP
servers at project scope, remove them to avoid conflicts
with the global registration:

```bash
claude mcp remove gerrit -s local
claude mcp remove phabricator -s local
```
