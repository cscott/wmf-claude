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

Phabricator, Gerrit, and GitLab MCP servers are preloaded and set up for
anonymous use. Claude can read public tasks, changes, and merge requests;
writing tasks or pushing patches fails.

## Install

```bash
git clone --recurse-submodules https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude.git
cd wmf-claude
./setup.sh
source ~/.zshrc     # or ~/.bashrc — picks up the new `claude` alias
claude mcp list     # should list phabricator, gerrit, and gitlab
```

Now run `claude` from any project directory and it starts sandboxed. You should see the text "`WMF nono sandbox`", confirming that your session is sandboxed.

Note: either restart all open terminals, or run the appropriate `source` command in each.

`setup.sh` is interactive, safe to re-run, and prompts for your Phabricator username.
It saves the username to `~/.config/wmf-claude/config.json`, so a re-run doesn't ask again.
Nothing in that file is a credential: the Phabricator MCP server reads public data
anonymously, and the username is only the default "my tasks" filter. Edit or delete
the file freely; setup rewrites it.


**Prerequisites:** nono 0.78+, Claude Code, Node + npm, Python 3,
[uv](https://docs.astral.sh/uv/) (optional), jq, git. `setup.sh` checks for each
and tells you what's missing — on macOS it offers to `brew install` packages; on
Debian or Ubuntu `setup.sh` offers to download and install the
recommended version of nono.  On other Linux distributions, use the package from the
[nono releases page](https://github.com/nolabs-ai/nono/releases).

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

## JetBrains / PhpStorm

In *Settings → Tools → Claude Code*, set **Claude CLI path** to the absolute
path of `bin/claude`, then restart PhpStorm. The plugin launches the wrapper
itself, so its sessions are sandboxed. From a terminal, `claude --ide` connects
to the open PhpStorm window instead (the one whose project contains the current
directory, when several are open). A `claude` run in the IDE's own terminal is
an IDE session too, because the IDE sets `CLAUDE_CODE_SSE_PORT` there; use a
separate terminal for a session without IDE mode.

IDE sessions run with `signal_mode: allow_all`. Claude Code checks that the IDE
is alive by signalling its process, which the default `isolated` mode denies;
with `allow_all` the session can also signal or kill your other processes. It
can also talk to the plugin, which runs outside the sandbox: a diff you accept
in PhpStorm is written by PhpStorm, so check its path first.

On Linux, a snap-installed PhpStorm strips `~/.local/bin` from `PATH`, so the
session cannot find `claude`. Use the JetBrains Toolbox or tar.gz install, or
`sudo ln -s ~/.local/bin/claude /usr/local/bin/claude`.

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

## Bugs and questions

Bugs and feature requests go to the
[#WMF-Claude](https://phabricator.wikimedia.org/tag/wmf-claude/) workboard in
Phabricator. Say which profile and flags you launched with, and paste the nono
denial if there is one.

For quick questions, ask in
[#ai-coding](https://wikimedia.enterprise.slack.com/archives/C0ATKE72JG6) in
Wikimedia Slack.

## More

| | |
|---|---|
| [`SECURITY.md`](SECURITY.md) | What Claude can and cannot reach, and the residual risks |
| [`docs/local-testing.md`](docs/local-testing.md) | Local-wiki tiers and Docker setup in full |
| [`docs/advanced.md`](docs/advanced.md) | All flags, env vars, updating, custom profiles, troubleshooting |
| [`docs/security-rationale.md`](docs/security-rationale.md) | Why each security choice was made |
| [`skills/`](skills/), [`agents/`](agents/) | The skills and agents themselves |
