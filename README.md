# wmf-claude

Reduce the risk of using [Claude Code](https://claude.ai/code)
as a Wikimedia Foundation engineer. Claude runs inside a
[nono](https://github.com/always-further/nono) sandbox
with Phabricator and Gerrit MCP integration, scoped
network access (Wikimedia + language docs), and
defense-in-depth permission rules to reduce the risk of
accidental pushes or credential exposure.

## Prerequisites

Install these first — `setup.sh` checks for them and exits
with install hints if any are missing:

- **[nono](https://github.com/always-further/nono)** 0.44+ — the sandbox runtime (tested version is pinned in [`.nono-version`](./.nono-version); CI installs that version)
  ```bash
  brew install nono
  ```
- **[Claude Code](https://docs.claude.com/en/docs/claude-code/setup)**
  ```bash
  curl -fsSL https://claude.ai/install.sh | bash
  ```
- **Node.js + npm** — Phabricator MCP server (`brew install node`)
- **Python 3** (uv preferred, falls back to `venv`) — Gerrit MCP server (`brew install python3`)
- **git** — `brew install git`, or `xcode-select --install`

You'll also need your Phabricator username (the one you
log into [phabricator.wikimedia.org](https://phabricator.wikimedia.org)
with) — `setup.sh` prompts for it.

## Install

```bash
git clone --recurse-submodules \
  https://gitlab.wikimedia.org/kharlan/wmf-claude.git
cd wmf-claude
./setup.sh
source ~/.zshrc   # or ~/.bashrc
```

`setup.sh` installs the nono profile, builds both MCP
servers, registers them globally with Claude Code, and
adds a `claude` shell alias that runs sandboxed by
default. To bypass the sandbox for a single invocation,
use `\claude` or `command claude`.

If you already have a different `alias claude=` in your
rc file, setup leaves it alone and prints the line for
you to install manually.

Verify the MCP servers registered:

```bash
claude mcp list   # should list phabricator and gerrit
```

Then run `claude` from any project directory.

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

## Updating

```bash
git pull
git submodule update --init --recursive
./setup.sh
```

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
