# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repo contains [nono](https://github.com/anthropics/nono) security sandbox profiles for Wikimedia Foundation engineers using AI coding agents. It also bundles MCP servers (Phabricator, Gerrit) as git submodules.

## Structure

- `profiles/` — nono profile JSON files
- `profiles/wmf-engineer.json` — the primary profile, extending the built-in `claude-code` base profile
- `bin/claude` — wrapper script that launches Claude Code inside the nono sandbox with MCP server access
- `setup.sh` — installs the nono profile, MCP server dependencies, and registers MCP servers globally in Claude Code
- `tests/test-profile.sh` — integration tests using `nono why`, `nono policy validate`, and `nono run`
- `mcp-phabricator/` — Phabricator MCP server submodule (Node.js)
- `gerrit-mcp-server/` — Gerrit MCP server submodule (Python)

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
./tests/test-profile.sh
```

Tests cannot run inside a nono sandbox (nested sandboxing doesn't work). Run directly or in CI.

## Key Design Decisions

- Blocks SSH and git push to prevent accidental pushes to Gerrit or production infrastructure
- Denies access to shell configs (`~/.bashrc`, `~/.zshrc`, etc.) and credential files (`~/.netrc`, `~/.npmrc`, `~/.composer/auth.json`) to prevent credential exfiltration
- Network uses a deny-by-default approach: `minimal` profile (LLM APIs only) plus explicit allowlists for Wikimedia domains and language documentation sites
- No hardcoded filesystem grants in the profile — `bin/claude` wrapper adds `--allow` for the bundled MCP submodules; engineers can pass additional `--allow`/`--read` flags for other paths
- `capability_elevation: false` prevents runtime prompts to escalate permissions
