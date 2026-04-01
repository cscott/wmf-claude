# wmf-nono

[Nono](https://github.com/anthropics/nono) security profiles for Wikimedia Foundation engineers using AI coding agents.

## Quick start

```bash
git clone --recurse-submodules https://gitlab.wikimedia.org/kharlan/wmf-nono.git
cd wmf-nono
./setup.sh
```

Then launch Claude Code from your project directory:

```bash
~/path/to/wmf-nono/bin/claude
```

To grant access to additional paths (e.g. MCP servers cloned elsewhere), pass nono flags before `--`:

```bash
~/path/to/wmf-nono/bin/claude --allow ~/src/mcp-phabricator --read ~/src/mediawiki/LocalSettings.php -- --resume
```

Flags before `--` go to nono, flags after go to claude. If there's no `--`, everything goes to claude.

## What the wrapper does

`bin/claude` runs Claude Code inside the `wmf-engineer` nono sandbox with read+write access to the bundled MCP servers. Engineers can pass additional `--allow`/`--read` flags for paths outside the working directory (e.g. MCP servers installed elsewhere, config files).

## Profiles

### wmf-engineer

Claude Code profile for WMF engineers. Extends the built-in `claude-code` profile with:

**Blocked commands:** ssh, ssh-keygen, ssh-agent, ssh-add, scp, sftp, git-push, git-receive-pack, git-send-pack

**Denied paths:** `~/.ssh`, `~/.env`, `~/.bashrc`, `~/.zshrc`, `~/.profile` (and variants), `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.composer/auth.json`

**Network access:** LLM APIs (via `minimal` network profile) plus:
- Wikimedia domains: `*.wikimedia.org`, `*.wikipedia.org`, `*.mediawiki.org`, `*.wikidata.org`, `*.wiktionary.org`, `*.wikibooks.org`, `*.wikiquote.org`, `*.wikivoyage.org`, `*.wikisource.org`, `*.wikinews.org`, `*.wikiversity.org`, `*.wikifunctions.org`
- Documentation sites: MDN, php.net, docs.python.org, docs.rs, doc.rust-lang.org, nodejs.org, pkg.go.dev

**Filesystem:**
- Working directory: read+write
- All other paths denied unless explicitly granted via `--allow` or `--read` flags

**Inherited protections (from claude-code/default):**
- No access to credentials, keychains, browser data
- Destructive commands blocked (rm, sudo, chmod, mv, cp, pip, npm, brew, etc.)
- File deletion blocked outside user-writable paths

## What setup.sh does

1. Installs the `wmf-engineer` nono profile to `~/.config/nono/profiles/`
2. Installs dependencies for the bundled MCP servers (npm for phabricator, pip/uv for gerrit)
3. Prompts for your Phabricator username
4. Registers both MCP servers globally in Claude Code (`claude mcp add --scope user`)
5. Prints a shell alias you can add for easy access

## Bundled MCP servers

Included as git submodules and registered globally by `./setup.sh`:

- `mcp-phabricator/` — Phabricator MCP server (Node.js) — requires your Phabricator username
- `gerrit-mcp-server/` — Gerrit MCP server (Python)

If you previously configured these MCP servers at project scope, remove the old configs to avoid conflicts:

```bash
claude mcp remove gerrit -s local
claude mcp remove phabricator -s local
```

## Running tests

```bash
./tests/test-profile.sh
```

Requires `nono` and `jq`.
