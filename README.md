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

Installation hints below cover macOS (Homebrew), Debian-like Linuxes (apt),
and Fedora / RHEL (dnf/COPR).

`setup.sh` checks for these and exits with install hints if any are
missing. Install them up front:

- **[nono](https://github.com/always-further/nono)** 0.61+, the sandbox runtime (tested version is pinned in [`.nono-version`](./.nono-version); CI installs that version)

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

  Fedora (official COPR repository):

  ```bash
  sudo dnf install 'dnf-command(copr)'
  sudo dnf copr enable always-further/nono
  sudo dnf install nono-cli
  ```

  Fedora / RHEL manual RPM fallback (download the `.rpm` from
  [GitHub Releases](https://github.com/always-further/nono/releases)):

  ```bash
  NONO_VERSION=$(cat .nono-version)
  wget "https://github.com/always-further/nono/releases/download/v${NONO_VERSION}/nono-cli-${NONO_VERSION}-1.$(rpm -E %_arch).rpm"
  sudo dnf install "./nono-cli-${NONO_VERSION}-1.$(rpm -E %_arch).rpm"
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

1. Pulls the always-further/claude nono pack (the base profile + hooks).
2. Updates Claude Code to the current version.
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

Where you launch `claude` matters for two reasons:

- **Sandbox** — it grants read-write to that directory and everything
  beneath; parents and siblings stay out of reach unless you grant them. The
  launch dir is your write blast radius.
- **Memory and `CLAUDE.md`** — both are keyed to it. Claude accumulates
  per-project memory and loads that project's `CLAUDE.md` from where you
  launch, so a stable entrypoint builds up context over time; a broad root
  like `~/src` dumps every project into one store and grants write to
  everything.

So keep a deliberate entrypoint per area you work in, and widen access with
flags rather than by launching higher up. MediaWiki work, from core:

```bash
cd ~/src/mediawiki/core
claude --read ~/src --              # read other repos for context; writes stay in core
claude --allow ~/src/mediawiki --   # or read-write across the MediaWiki repos, when you need it
```

A separate area gets its own entrypoint (and its own memory):

```bash
cd ~/src/integration/quibble
claude
```

Prefer `--read` for context you only need to read; reach for `--allow`,
scoped as tightly as the task allows, when you need to write across repos.

Clone each repo to a path mirroring its Gerrit/GitLab project path, so a name
maps to a predictable location:

```
~/src/
  mediawiki/core
  mediawiki/extensions/GrowthExperiments
  integration/quibble
  operations/puppet
  repos/product-safety-and-integrity/wmf-claude
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
| `standalone-vuln-audit` | Runs a security review tuned to microservice and standalone apps | 

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
accessibility snapshots, and click-through checks. `./setup.sh` builds
it (unless `WMF_CLAUDE_SKIP_CHROME` is set); launch with `--chrome`
(which implies `--local-web`). Chrome runs outside the
sandbox (it calls IOKit at startup and can't run inside), so start it in
a separate terminal first:

```bash
bin/launch-test-chrome   # one terminal: Chrome outside the sandbox, throwaway profile
claude --chrome          # another: curl tier + the chrome-devtools MCP
```

The MCP attaches over `127.0.0.1:9222`. That port is unauthenticated
and reachable by other local processes, so use throwaway dev-wiki
accounts only, never real credentials.

## Running dev tools in Docker

If your wiki runs in Docker (MediaWiki-Docker, MWDD, …), the host has no
PHP/composer/npm, so the sandboxed session can't run phpcs, phpunit, or
composer directly. Handing the sandbox the Docker socket is not an option
— socket access is root on the host and would defeat the sandbox. Instead
the socket stays outside the sandbox behind a small, locked-down broker.

Launch with the service name of your MediaWiki container:

```bash
claude --docker=mediawiki        # detect the compose file, start + own the broker
claude --docker=auto             # let it pick the service from the compose file
claude --docker=mediawiki:/var/www/html/w   # set the in-container working dir
```

The broker starts automatically and stops when the session exits. Claude
then runs dev tools through the `mwdocker` shim — `mwdocker composer
phpcs`, `mwdocker vendor/bin/phpunit <path>` — which the `run-tests` and
`lint` skills do for you. Only an allowlisted set of binaries is
permitted (`composer`, `php`, `npm`, `vendor/bin/phpunit|phpcs|phpcbf|phan`),
the broker requires a per-session token, and it refuses to serve a
`--privileged` or socket-mounting container. See `SECURITY.md` for the
threat model and residual risks.

For a custom layout (compose file elsewhere, custom workdir), start the
broker yourself and attach with bare `--docker`:

```bash
bin/launch-docker-broker --service mediawiki --compose-file path/to/compose.yml
claude --docker
```

### Restricting the container's network

By default the container's network is NOT restricted by the sandbox: code
that Claude runs in it (composer scripts, `php -r`) can reach any host.
Two compose overrides in `templates/docker-egress/` close that down:

- `egress-none.yml` — the PHP containers get no route out at all. Lint,
  tests, and maintenance scripts work; `composer install`/`npm install`
  do not.
- `egress-allowlist.yml` + `squid-allowlist.conf` — the PHP containers
  reach only allowlisted package-registry hosts through a squid sidecar,
  so installs work too.

`setup.sh` installs the files to `~/.config/wmf-claude/`, which the
sandboxed agent cannot write (it never overwrites your customized
copies). Recreate your containers with the override:

```bash
docker compose -f docker-compose.yml \
  -f ~/.config/wmf-claude/egress-none.yml up -d
```

Then launch with the matching mode so the broker verifies the isolation
and refuses to start when the override is missing:

```bash
claude --docker=mediawiki --egress=none        # or --egress=allowlist
```

## Updating

Pull and re-run setup. It is safe to re-run any time:

```bash
git pull && git submodule update --init --recursive && ./setup.sh
```

If `setup.sh` reports that nono is too old, upgrade it first, then re-run:

```bash
brew upgrade nono     # or a fresh .deb / .rpm from the nono releases page
./setup.sh
```

In an interactive terminal, the `claude` wrapper reminds you when an update
is available: it prints a one-line notice (with the exact command to run) if
your installed nono is older than the pinned version, or if your wmf-claude
checkout is behind `origin/main`. It only notifies, it never pulls for you.
The remote check runs as a background `git fetch origin` at most once a day,
so it never slows a launch, and the behind-count clears as soon as you pull.
The check is skipped for non-interactive runs such as `claude -p` and the
VS Code extension. Silence it anywhere with `WMF_CLAUDE_SKIP_UPDATE=1`.

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
