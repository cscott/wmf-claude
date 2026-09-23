# wmf-claude

[Claude Code](https://claude.ai/code) for Wikimedia Foundation engineers, with
sandboxing and pre-configured MCPs, skills and agents.

Claude runs under [nono](https://github.com/nolabs-ai/nono), which enforces a
security profile **at the kernel level** — Seatbelt on macOS, Landlock on
Linux. No VMs or containers. Claude runs as a normal process, and the OS
refuses the syscalls the profile doesn't allow. It reads and writes the
directory you launched from, has access to Wikimedia sites and the Claude API,
and little else; SSH keys, credentials, and browser profiles are denied. That
meaningfully narrows what a mistake or a prompt injection can reach. See
[`SECURITY.md`](SECURITY.md) for limitations.

Phabricator and Gerrit MCP servers are preloaded and set up for anonymous use.
Claude can read public tasks and changes; writing tasks or pushing patches
fails.

## Install

```bash
git clone --recurse-submodules https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude.git
cd wmf-claude
./setup.sh
source ~/.zshrc     # or ~/.bashrc — picks up the new `claude` alias
claude mcp list     # should list phabricator and gerrit
```

Now run `claude` from any project directory and it starts sandboxed. `setup.sh`
is interactive, safe to re-run, and prompts for your Phabricator username.

**Prerequisites:** nono 0.78+, Claude Code, Node + npm, Python 3,
[uv](https://docs.astral.sh/uv/) (optional), jq, git. `setup.sh` checks for each
and tells you what's missing — on macOS it offers to `brew install` it; on
Linux it points you at the nono releases page, where you should match the
version in `.nono-version`.

## Working across repos

Claude writes only in the directory you launched from. Point it elsewhere
explicitly — nono flags go before `--`, everything after goes to Claude:

```bash
cd ~/src/mediawiki/core
claude --read ~/src --                # read other repos for context
claude --allow ~/src/mediawiki --     # read-write across the MediaWiki repos
```

## VS Code

Point the extension at the wrapper, using an absolute path, so its sessions are
sandboxed too:

```json
{
  "claudeCode.claudeProcessWrapper": "/absolute/path/to/wmf-claude/bin/claude"
}
```

The extension gives you nowhere to pass nono flags, so sessions needing
`--allow`, `--docker`, or `--chrome` must start from a terminal.

## MediaWiki-Docker

A sandboxed session has no PHP, composer, or npm on the host, and handing it
the Docker socket would defeat the sandbox — the socket is root on the host.
Launch with `--docker` instead: Claude runs dev tools *inside* your container
through a broker that stays outside the sandbox, and the `run-tests` and `lint`
skills use it automatically.

```bash
claude --docker=mediawiki     # service name from your docker-compose.yml
claude --docker=auto          # or let it pick
```

**Caveat:** the sandbox can't restrict your container's network. To close that
off, recreate the containers with an egress override and have the broker verify
it at startup:

```bash
docker compose -f docker-compose.yml \
  -f ~/.config/wmf-claude/egress-none.yml up -d

claude --docker=mediawiki --egress=none
```

Use `egress-allowlist.yml` with `--egress=allowlist` to keep `composer install`
working. Custom layouts: [`docs/local-testing.md`](docs/local-testing.md).

## Testing against your local wiki

The `manual-test` skill checks changes against a local dev wiki, cheapest tier
first. Both modes are opt-in, because they open localhost ports the sandbox
otherwise denies:

```bash
claude --local-web   # curl + /w/api.php: status, redirects, HTML, API responses
claude --chrome      # screenshots, console errors, a11y tree, click-throughs
```

`--chrome` needs `bin/launch-test-chrome` in another terminal, since Chrome
can't run inside the sandbox. Its debug port is unauthenticated, so **use
throwaway dev-wiki accounts only**.

## More

| | |
|---|---|
| [`SECURITY.md`](SECURITY.md) | What Claude can and cannot reach, and the residual risks |
| [`docs/local-testing.md`](docs/local-testing.md) | Local-wiki tiers and Docker setup in full |
| [`docs/advanced.md`](docs/advanced.md) | All flags, env vars, updating, custom profiles, troubleshooting |
| [`docs/security-rationale.md`](docs/security-rationale.md) | Why each security choice was made |
| [`skills/`](skills/), [`agents/`](agents/) | The skills and agents themselves |
