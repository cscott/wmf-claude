# wmf-claude

Use [Claude Code](https://claude.ai/code) safely as a
Wikimedia Foundation engineer.

Three commands to get a sandboxed Claude Code with
Phabricator and Gerrit integration, access to Wikimedia
domains and language docs, and reduced risk of accidental
pushes or credential exposure:

```bash
git clone --recurse-submodules \
  https://gitlab.wikimedia.org/kharlan/wmf-claude.git
cd wmf-claude
./setup.sh
```

Add the alias printed by setup, then from any project:

```bash
wmf-claude
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

- `git push`, `git remote`, `ssh`, `scp`, `sftp` — no
  accidental pushes, remote URL tampering, or remote access
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

**Note:** Claude can make HTTP requests to allowlisted
Wikimedia domains. While credential files are blocked,
be aware that Wikimedia APIs are reachable within the
sandbox.

## Granting additional access

Pass nono flags before `--` to allow extra paths:

```bash
# Read+write access to another directory
wmf-claude --allow ~/src/mediawiki --

# Read-only access
wmf-claude --read ~/src/schemas/event/secondary

# Allow a normally-blocked command for a session
wmf-claude --allow-command rm --

# Override a deny rule for a specific path
wmf-claude --override-deny ~/.kube/config \
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
Code, and prints a shell alias. `bin/claude` is a thin
wrapper that launches `nono run` with the right profile
and grants access to the bundled MCP server submodules.

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
