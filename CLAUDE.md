# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

Two artifacts in one tree:

1. A **Claude Code plugin** (`.claude-plugin/plugin.json` plus top-level `skills/`, `agents/`, `hooks/`, `bin/`).
2. A **nono pack** (`package.json` plus `wiring/`) that wraps the plugin and a security profile, signed at release time and installable via `nono pull`.

## File layout

- `profiles/wmf-engineer.json` — nono security profile, extends `claude-code` from the `always-further/claude` pack
- `wiring/settings-merge.json` — Claude-Code-permission-layer denies (separate defense-in-depth layer from the profile)
- `wiring/{marketplace,known-marketplaces,installed-plugin,enabled-plugin}.json` — what the nono pack patches into `~/.claude/` at install time. See `package.json` `wiring` for the full set of `json_merge`, `write_file`, and `symlink` directives.
- `bin/claude` — launcher: splits args at `--`, runs `nono run --profile profiles/<name>.json ... -- bin/launch-claude.sh --plugin-dir <repo> ...`. Loads `profiles/$WMF_CLAUDE_PROFILE.json` (default `wmf-engineer`). Rejects `--capability-elevation`, `--trust-override`, and `--dangerously-skip-permissions`.
- `bin/launch-claude.sh` — runs inside the sandbox; prints the WMF banner then execs `claude`.
- `bin/session-start.sh` — `SessionStart` hook output; lists available skills/agents and reminds Claude it's sandboxed.
- `bin/wmf-claude-build` — non-interactive: vendors the MCP submodule deps (npm + pip/uv venv) in the checkout. Called by `bin/wmf-claude-setup`.
- `bin/wmf-claude-setup` — per-user config invoked by `setup.sh`: runs the build, pulls the base nono pack, registers the MCP servers, writes the chrome MCP config, installs the `claude` alias. Does **not** copy the profile — `bin/claude` loads it by path.
- `bin/lib-output.sh` — shared color/`step`/`ok`/`fail` helpers, sourced by the install scripts.
- `bin/launch-docker-broker` — Python HTTP broker, runs **outside** the sandbox, holds the Docker socket; runs an allowlisted set of dev tools inside a pinned compose service. Started by `bin/claude --docker=SERVICE`.
- `bin/mwdocker` — in-sandbox client (curl/jq) for the broker; `mwdocker <bin> <args>` forwards to it. Put on `PATH` per-session by `bin/claude --docker`.
- `skills/<name>/SKILL.md`, `agents/*.md` — namespaced as `/wmf-claude:<name>`; agents auto-invoked by description match.
- `templates/CLAUDE.md`, `templates/mediawiki/{CLAUDE.md,settings.json}` — starters dropped by `/wmf-claude:init-project`.
- `setup.sh` — checkout install: preflight deps, then `bin/wmf-claude-build` + `bin/wmf-claude-setup`. Does **not** apply `wiring/settings-merge.json` — that ships only with the signed nono pack today.
- `mcp-phabricator/`, `gerrit-mcp-server/` — MCP server submodules.

## Tests

```bash
./tests/test-profile.sh    # nono profile / sandbox behaviour (cannot run inside a sandbox)
./tests/test-templates.sh  # plugin manifest, skill/agent frontmatter, JSON validity
```

## Gotchas

- **Skill list lives in three places**: `skills/<name>/`, `package.json` `artifacts`, and `bin/session-start.sh`. Keep all three in sync when adding or removing a skill.
- **Don't add keychain Mach services (`com.apple.securityd` et al.) to the profile's deny list.** Claude Code reads its login token from the keychain on every startup; denying breaks `/login`. See `SECURITY.md` for the residual-risk analysis and mitigations.
- **Two layers of permission control.** The nono profile (`profiles/wmf-engineer.json`) is OS-level; `wiring/settings-merge.json` adds Claude-Code-tool-level denies (e.g. `Bash(ssh:*)`, `Read(**/*.pem)`, `Edit(~/.claude/**)`). When changing one, consider whether the other should change too.
- **The profile is loaded by absolute path, and is selectable.** `bin/claude` passes `--profile <repo>/profiles/$WMF_CLAUDE_PROFILE.json` (default `wmf-engineer`; nono's `--profile` takes `NAME_OR_PATH`). `extends: claude-code` still resolves against the installed `always-further/claude` pack. Nothing copies the profile into `~/.config/nono/profiles/` — don't reintroduce that. To add a profile (e.g. `wmf-data-scientist`), drop `profiles/<name>.json` in and launch with `WMF_CLAUDE_PROFILE=<name>`.
- **Install is `git clone --recurse-submodules` + `./setup.sh`.** The MCP servers need a build step (npm + a Python venv), which is procedural, so the install can't be a pure `nono pull` / declarative-wiring step. `setup.sh` preflights deps (offering `brew install` when Homebrew is present) and hands off to `bin/wmf-claude-setup`, which runs `bin/wmf-claude-build`.
- **Local-wiki testing is tiered for token cost.** Plain `bin/claude` can't reach localhost web ports. `bin/claude --local-web` opens 80/443/8080 (`--open-port`, localhost-only) for the curl-first Tier 1, or `--local-web=PORT[,PORT]` to narrow; `--chrome` opens 9222 + loads the MCP and implies `--local-web`. The `manual-test` skill drives the ladder; ports are opened per-invocation in `bin/claude` (not the static profile). Residual-risk note in `SECURITY.md`.
- **Docker dev tools go through a broker, never the socket.** The sandbox is never given the Docker socket (socket = host root). `bin/claude --docker=SERVICE[:WORKDIR]` (or `=auto`) starts `bin/launch-docker-broker` outside the sandbox, opens its ephemeral localhost port (per-invocation, off the static profile), and tears it down on exit; `--docker` (bare) attaches to a manually-started broker. Claude reaches it via the `mwdocker` shim, advertised three ways that must stay in sync: the `session-start.sh` conditional block (the global signal, gated on `WMF_DOCKER_BROKER_URL`), the `run-tests`/`lint` skills, and `templates/mediawiki/CLAUDE.md`. The broker refuses privileged/socket-mounted containers; full threat model in `SECURITY.md`. Container egress is restrictable via the `templates/docker-egress/` compose overrides (`egress-none.yml`, `egress-allowlist.yml` + squid) — the engineer applies them at `docker compose up` from an agent-unwritable copy, and `--egress=none|allowlist` makes the broker verify the isolation at startup.
- **Repo location:** `gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude`. The `wiring/` files use `"wikimedia"` as the marketplace name (and the symlink path assumes `$NS == wikimedia`); revisit when the pack is published.
