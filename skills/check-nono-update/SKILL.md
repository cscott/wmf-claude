---
description: Check what's new in nono since the version pinned in .nono-version. Use when the user wants to see release notes for newer nono versions, evaluate whether to bump the pin, or audit how upstream changes affect the wmf-engineer profile and sandbox.
allowed-tools:
  - Bash(cat .nono-version)
  - Bash(nono --version)
  - Bash(nono list:*)
  - Bash(nono profile validate:*)
  - Bash(jq:*)
  - Read
  - Edit
  - WebFetch
---

# Check for nono updates

Compare the pinned nono version (`.nono-version` at the repo root) against upstream releases and surface what's changed — especially anything that touches the profile JSON schema, sandbox enforcement, `nono run` flags, or known CI-Linux regressions.

`.nono-version` is consumed by both `setup.sh` and `.gitlab-ci.yml`; bumping it changes what users install locally **and** what CI tests against.

## Steps

1. **Read the current pin.**
   - `cat .nono-version` — strip whitespace; this is the baseline.
   - `nono --version` if available — note the engineer's local version (may be ahead of or behind the pin).

2. **Fetch upstream release notes.** Use `WebFetch` against `https://github.com/nolabs-ai/nono/releases` and ask for:
   - Every release tag newer than the baseline (semver compare, not lexicographic).
   - For each: headline changes, especially around:
     - **Profile schema** (new fields, removed fields, validation tightening). The wmf-engineer profile uses `extends`, `security.{signal_mode,process_info_mode,ipc_mode,capability_elevation}`, `filesystem.{read,deny}`, `environment.allow_vars`, `network.{block,network_profile,allow_domain}`, `filesystem.suppress_save_prompt`, `workdir.access`, and `unsafe_macos_seatbelt_rules`. Flag anything that touches those.
     - **Sandbox enforcement** (Landlock/seatbelt/seccomp behavior, Mach lookup rules, env-var filtering).
     - **`nono run` flags** — particularly `--workdir`, `--allow-cwd`, `--silent`, `--profile`, `--listen-port` (the `/login` callback bind on macOS), `--allow-file`, `--allow-domain`, `--open-port`. The CI test scripts (`tests/test-profile.sh`) depend on these.
     - **`nono pull` / pack format** changes (we ship a pack via `package.json`).
     - **Linux-only regressions or fixes.** History: 0.51 broke `nono run --workdir --allow-cwd` on Linux (job 820086); CI was pinned to 0.50 until 0.53 unblocked it. Always check whether new releases fix or reintroduce CI-Linux issues.

3. **Cross-check the wmf-engineer profile.** Read `profiles/wmf-engineer.json` and decide:
   - Do any new schema fields make our profile safer or simpler? (Example: 0.52.0 added `deny_vars`; we already use an `allow_vars` allowlist so it's not needed.)
   - Do any deprecations affect us? (Example: 0.52.0 deprecated `nono learn` — we don't use it.)
   - Were any fields we use renamed or removed?
   - Run `nono profile validate profiles/wmf-engineer.json`. A parse error naming
     a field we do not use comes from the base pack we extend, not from us.

4. **Cross-check the base pack.** We extend `claude-code` from `nolabs-ai/claude`.
   Compare the installed version (`nono list --installed --json`) with the registry
   (`WebFetch` `https://registry.nono.sh/api/v1/packages/nolabs-ai/claude/versions`). An
   abandoned pack keeps a schema nono has already dropped — that is what broke
   0.77.0, where `always-further/claude` still used the removed `undo` key.

5. **Cross-check CI and setup.** Read `.gitlab-ci.yml` and `bin/wmf-claude-setup`:
   - Does CI rely on a flag whose behavior changed?
   - `bin/wmf-claude-setup` reads the minimum version from `package.json`
     `min_nono_version`. Does that value still make sense, or has the
     schema-compat minimum moved? Keep it in sync with the base pack's own
     `min_nono_version`.

6. **Report.** Give the user a structured summary:
   - **Versions between baseline and latest** (one line each, e.g. `0.52.1 — schema fix for environment block`).
   - **Profile-relevant changes** (with the specific field/group).
   - **CI/test-relevant changes** (with the specific flag or behavior).
   - **Recommendation:** bump `.nono-version` to X, optionally bump `package.json` `min_nono_version` to Y, or stay pinned because Z.

7. **If the user agrees to bump**, only then edit:
   - `.nono-version` — new version string, no extra whitespace.
   - `package.json` `min_nono_version` — only bump if a newly-required field/behaviour can't be expressed under the old floor.
   - Do NOT silently edit the profile to use new features; surface the option and let the user decide.

## What not to do

- Don't bump `.nono-version` without showing the user the changelog first — CI runs against this pin, so a bad bump breaks the test job.
- Don't summarise a release as "no relevant changes" without actually reading the release notes for that tag; "no notes on the page" can mean "release page is sparse," not "nothing changed." If WebFetch returns thin content, fetch the individual tag page (`/releases/tag/vX.Y.Z`).
- Don't recommend `deny_vars`, `learn`, or other features that conflict with the profile's current allowlist-first posture without explaining the trade-off.
