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
- `filesystem` — `deny`, `read` (read-only), and `allow` (read+write) path lists beyond the working directory
- `environment.allow_vars` — env var allowlist; vars not in the list are dropped before the sandboxed child sees them
- `network` — `network_profile` base (e.g. `minimal`), plus `allow_domain` for additional allowed domains
- `unsafe_macos_seatbelt_rules` — escape hatch for raw Seatbelt S-expressions when nono lacks a typed capability (e.g., Mach service denies)
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

- Blocks SSH push at the network layer — ports 22 and 29418 aren't in the `minimal` profile allowlist, so `git push` to any SSH remote (including `ssh://gerrit.wikimedia.org:29418`) fails. HTTPS push to `gerrit.wikimedia.org` is reachable at the network layer (same host+port as the Gerrit REST API used by the MCP server), but requires a Gerrit-generated HTTP password that WMF engineers on the standard SSH workflow don't have configured. `github.com` isn't in the allowlist, so GitHub push is also blocked.
- Denies access to shell configs (`~/.bashrc`, `~/.zshrc`, etc.) and credential files (`~/.netrc`, `~/.npmrc`, `~/.composer/auth.json`) to prevent credential exfiltration
- Blocks Spotlight metadata recon at the Mach layer via `unsafe_macos_seatbelt_rules`: `com.apple.metadata.mds` is denied. Spotlight indexes filenames and metadata across the entire user home regardless of nono's filesystem deny rules, so `mdfind` would otherwise be a recon channel for sensitive file paths (e.g., `*.pem` keys). Verified at runtime by `tests/test-profile.sh` — `mdfind` fails inside the sandbox.
- Keychain Mach services (`com.apple.securityd`, `com.apple.SecurityServer`, `com.apple.SecurityAgent`) are **intentionally not denied**. Claude Code stores its login token in the login keychain (item: `Claude Code-credentials`) and reads it via `securityd` on every startup; denying that service makes `/login` and the persistent-session UI fail (the user sees "Not logged in" on every launch). The base `claude-code` profile's `filesystem.allow` + `filesystem.bypass_protection` on `~/Library/Keychains` is specifically for this. Residual risk: a prompt-injected agent could call `security find-internet-password -s <wmf-host>` to extract credentials stored for Wikimedia services. Mitigations are layered, not absolute: (a) the network policy only permits egress to Wikimedia and LLM-vendor domains, so attacker-controlled exfil endpoints are unreachable; (b) the env-var allowlist removes most credential-shaped material from the agent's process env; (c) WMF engineers on the standard SSH-based git workflow typically don't have HTTPS push credentials in the keychain in the first place.
- Filters environment variables via `environment.allow_vars` — a deliberately narrow allowlist (15 entries) covering only what's strictly needed: `PATH`, `HOME`, `TERM`, locale (`LANG`, `LC_*`), Claude/Anthropic/nono internals (`CLAUDE_*`, `CLAUDECODE`, `ANTHROPIC_*`, `NONO_*`), and the non-credential MCP vars the Phabricator/Gerrit submodules read (`PHABRICATOR_URL`/`USERNAME`/`CONTACT_*`, `GERRIT_BASE_URL`/`CONFIG_PATH`). Everything else (including `AWS_*`, `GH_TOKEN`, `NPM_TOKEN`, `KUBECONFIG`, `GCLOUD_*`, `GIT_*`, `SSH_AUTH_SOCK`, `EDITOR`, `TMPDIR`, `USER`, language version managers, GUI/X11 vars, etc.) is dropped before the child sees it. Git identity comes from `~/.gitconfig` (which the base profile grants read access to). `PHABRICATOR_API_TOKEN` is intentionally omitted — WMF MCP usage is anonymous (read-only Phabricator access).
- Network uses a deny-by-default approach: `minimal` profile (LLM APIs only) plus explicit allowlists for Wikimedia domains and language documentation sites
- No hardcoded filesystem grants in the profile — `bin/claude` wrapper adds `--allow` for the bundled MCP submodules; engineers can pass additional `--allow`/`--read` flags for other paths
- `capability_elevation: false` prevents runtime prompts to escalate permissions
- `signal_mode`, `process_info_mode`, and `ipc_mode` are explicitly set to their hardened defaults (`isolated` / `shared_memory_only`) so the posture is visible in the profile, not implicit.

### Open hardening follow-ups

- Keychain access remains the largest residual exfil surface. Closing it cleanly needs either (a) per-binary Seatbelt rules that allow Claude Code itself to talk to `securityd` while denying tool-spawned subprocesses (`security`, `bash`-spawned children) — brittle and chains badly through Claude Code's tool framework, or (b) a credential-proxy mechanism that lets nono fetch the OAuth token outside the sandbox and inject it into the child without `securityd` access — Claude Code would need to support a non-keychain credential source for this to be useful.
- LaunchServices (`allow_launch_services: true` inherited from base) is still reachable. Could leak app-launch availability / open arbitrary apps. Worth narrowing once we know which engineer workflows depend on it.
- Mach service controls live in the `unsafe_macos_seatbelt_rules` escape hatch. If nono promotes Mach denies to a typed capability, migrate.
