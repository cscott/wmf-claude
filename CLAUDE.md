# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repo contains [nono](https://github.com/always-further/nono) security sandbox profiles for Wikimedia Foundation engineers using AI coding agents. It also bundles MCP servers (Phabricator, Gerrit) as git submodules.

## Structure

This repo is two artifacts in one tree:

1. A **Claude Code plugin** (`.claude-plugin/plugin.json` plus top-level `skills/`, `agents/`, `hooks/`, `bin/`).
2. A **nono pack** (`package.json` plus `wiring/`) that wraps the plugin and a security profile, signed at release time and installable via `nono pull`.

- `profiles/wmf-engineer.json` — nono security profile, extends `claude-code` from the `always-further/claude` nono pack
- `.claude-plugin/plugin.json` — Claude Code plugin manifest (name, version, description)
- `package.json` — nono pack manifest (artifacts + wiring directives)
- `skills/<name>/SKILL.md` — namespaced as `/wmf-claude:<name>`
- `agents/*.md` — auto-invoked by description match
- `hooks/hooks.json` — registers a `SessionStart` hook that emits WMF-environment context for Claude; references `bin/` via `${CLAUDE_PLUGIN_ROOT}`
- `bin/claude` — wrapper that launches Claude Code inside the nono sandbox with `--plugin-dir` set
- `bin/session-start.sh` — emits context for Claude at session start
- `templates/CLAUDE.md` — starter dropped by the `init-project` skill
- `templates/mediawiki/{CLAUDE.md,settings.json}` — MediaWiki conventions appended by `init-project --mediawiki`
- `wiring/*.json` — patches the nono pack merges into `~/.claude/{settings.json,plugins/installed_plugins.json,plugins/known_marketplaces.json}` at install time. Includes `settings-merge.json` with the defense-in-depth deny rules.
- `setup.sh` — installs the nono profile, MCP server dependencies, and the `claude` shell alias
- `tests/test-profile.sh` — nono profile / sandbox behavior
- `tests/test-templates.sh` — validates plugin + pack: skill/agent frontmatter, JSON validity (plugin manifest, hooks, wiring, package.json), package.json artifact paths exist, bin script syntax
- `mcp-phabricator/`, `gerrit-mcp-server/` — MCP server submodules

## Profile Schema

Profiles are JSON files with these key sections:
- `extends` — base profile to inherit from (e.g. `claude-code`)
- `meta` — name, version, description
- `security` — signal mode, capability elevation settings
- `policy` — `add_deny_commands` and `add_deny_access` lists that extend the base profile's restrictions
- `filesystem` — `read` (read-only) and `allow` (read+write) path lists beyond the working directory
- `network` — `network_profile` base (e.g. `minimal`), plus `allow_domain` for additional allowed domains
- `workdir` — working directory access level

## Running Tests

```bash
./tests/test-profile.sh    # nono profile / sandbox behavior
./tests/test-templates.sh  # bundled skills, agents, settings.json, hooks
```

`test-profile.sh` cannot run inside a nono sandbox (nested sandboxing doesn't work). Run directly or in CI. `test-templates.sh` is filesystem-only and works anywhere `python3` and `jq` are available.

## Repo location

Currently lives at `gitlab.wikimedia.org/kharlan/wmf-claude` (personal namespace) and is referenced as such throughout the manifests, install instructions, and templates. The eventual home is `gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude`. When that move happens, search-and-replace `kharlan/wmf-claude` across the repo. The nono pack `wiring/` files use `"wikimedia"` as the Claude Code marketplace name, which doesn't have to match the GitLab path; revisit that name choice when the pack is actually published (the symlink path it generates assumes `$NS` matches `"wikimedia"`).

## Key Design Decisions

- Blocks SSH push at the network layer — ports 22 and 29418 aren't in the `minimal` profile allowlist, so `git push` to any SSH remote (including `ssh://gerrit.wikimedia.org:29418`) fails. HTTPS push to `gerrit.wikimedia.org` is reachable at the network layer (same host+port as the Gerrit REST API used by the MCP server), but requires a Gerrit-generated HTTP password that WMF engineers on the standard SSH workflow don't have configured. `github.com` isn't in the allowlist, so GitHub push is also blocked. Residual gap: HTTPS push to a Wikimedia host (e.g. `gitlab.wikimedia.org`) would succeed if the user has credentials in the macOS keychain — `~/.netrc` and `~/.config/gh` are denied, but the keychain is accessible.
- Denies access to shell configs (`~/.bashrc`, `~/.zshrc`, etc.) and credential files (`~/.netrc`, `~/.npmrc`, `~/.composer/auth.json`) to prevent credential exfiltration
- Network uses a deny-by-default approach: `minimal` profile (LLM APIs only) plus explicit allowlists for Wikimedia domains and language documentation sites
- No hardcoded filesystem grants in the profile — `bin/claude` wrapper adds `--allow` for the bundled MCP submodules; engineers can pass additional `--allow`/`--read` flags for other paths
- `capability_elevation: false` prevents runtime prompts to escalate permissions
