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
- `bin/claude` — launcher: splits args at `--`, runs `nono run --profile wmf-engineer ... -- bin/launch-claude.sh --plugin-dir <repo> ...`. Rejects `--capability-elevation`, `--trust-override`, and `--dangerously-skip-permissions`.
- `bin/launch-claude.sh` — runs inside the sandbox; prints the WMF banner then execs `claude`.
- `bin/session-start.sh` — `SessionStart` hook output; lists available skills/agents and reminds Claude it's sandboxed.
- `skills/<name>/SKILL.md`, `agents/*.md` — namespaced as `/wmf-claude:<name>`; agents auto-invoked by description match.
- `templates/CLAUDE.md`, `templates/mediawiki/{CLAUDE.md,settings.json}` — starters dropped by `/wmf-claude:init-project`.
- `setup.sh` — local-dev install: nono profile, MCP submodule deps, MCP registration, `claude` shell alias. Does **not** apply `wiring/settings-merge.json` — that ships only with the signed nono pack today.
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
- **Local-wiki testing is tiered for token cost.** Plain `bin/claude` can't reach localhost web ports. `bin/claude --local-web` opens 80/443/8080 (`--open-port`, localhost-only) for the curl-first Tier 1, or `--local-web=PORT[,PORT]` to narrow; `--chrome` opens 9222 + loads the MCP and implies `--local-web`. The `manual-test` skill drives the ladder; ports are opened per-invocation in `bin/claude` (not the static profile). Residual-risk note in `SECURITY.md`.
- **Repo location:** currently `gitlab.wikimedia.org/kharlan/wmf-claude` (personal namespace). Eventual home: `gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude`. When the move happens, search-and-replace `kharlan/wmf-claude`. The `wiring/` files use `"wikimedia"` as the marketplace name (and the symlink path assumes `$NS == wikimedia`); revisit when the pack is published.
