---
description: Check what's new in nono since the version pinned in .nono-version. Use when the user wants to see release notes for newer nono versions, evaluate whether to bump the pin, or audit how upstream changes affect the wmf-engineer profile and sandbox.
allowed-tools:
  - Bash(cat .nono-version)
  - Bash(nono --version)
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

2. **Fetch upstream release notes.** Use `WebFetch` against `https://github.com/always-further/nono/releases` and ask for:
   - Every release tag newer than the baseline (semver compare, not lexicographic).
   - For each: headline changes, especially around:
     - **Profile schema** (new fields, removed fields, validation tightening). The wmf-engineer profile uses `extends`, `security.{signal_mode,process_info_mode,ipc_mode,capability_elevation}`, `filesystem.{read,deny}`, `environment.allow_vars`, `network.{block,network_profile,allow_domain,open_port}`, `workdir.access`, and `unsafe_macos_seatbelt_rules`. Flag anything that touches those.
     - **Sandbox enforcement** (Landlock/seatbelt/seccomp behavior, Mach lookup rules, env-var filtering).
     - **`nono run` flags** — particularly `--workdir`, `--allow-cwd`, `--silent`, `--profile`. The CI test scripts (`tests/test-profile.sh`) depend on these.
     - **`nono pull` / pack format** changes (we ship a pack via `package.json`).
     - **Linux-only regressions or fixes.** History: 0.51 broke `nono run --workdir --allow-cwd` on Linux (job 820086); CI was pinned to 0.50 until 0.53 unblocked it. Always check whether new releases fix or reintroduce CI-Linux issues.

3. **Cross-check the wmf-engineer profile.** Read `profiles/wmf-engineer.json` and decide:
   - Do any new schema fields make our profile safer or simpler? (Example: 0.52.0 added `deny_vars`; we already use an `allow_vars` allowlist so it's not needed.)
   - Do any deprecations affect us? (Example: 0.52.0 deprecated `nono learn` — we don't use it.)
   - Were any fields we use renamed or removed?

4. **Cross-check CI and setup.** Read `.gitlab-ci.yml` and `setup.sh`:
   - Does CI rely on a flag whose behavior changed?
   - Does `setup.sh`'s `NONO_MIN` floor (`0.44.0`) still make sense, or has the schema-compat floor moved?

5. **Report.** Give the user a structured summary:
   - **Versions between baseline and latest** (one line each, e.g. `0.52.1 — schema fix for environment block`).
   - **Profile-relevant changes** (with the specific field/group).
   - **CI/test-relevant changes** (with the specific flag or behavior).
   - **Recommendation:** bump `.nono-version` to X, optionally bump `package.json` `min_nono_version` to Y, or stay pinned because Z.

6. **If the user agrees to bump**, only then edit:
   - `.nono-version` — new version string, no extra whitespace.
   - `package.json` `min_nono_version` — only bump if a newly-required field/behaviour can't be expressed under the old floor.
   - Do NOT silently edit the profile to use new features; surface the option and let the user decide.

## What not to do

- Don't bump `.nono-version` without showing the user the changelog first — CI runs against this pin, so a bad bump breaks the test job.
- Don't summarise a release as "no relevant changes" without actually reading the release notes for that tag; "no notes on the page" can mean "release page is sparse," not "nothing changed." If WebFetch returns thin content, fetch the individual tag page (`/releases/tag/vX.Y.Z`).
- Don't recommend `deny_vars`, `learn`, or other features that conflict with the profile's current allowlist-first posture without explaining the trade-off.
