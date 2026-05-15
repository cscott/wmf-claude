---
description: Trace which production service serves a given Wikimedia hostname/path by walking ATS backend mappings and gateway-check.lua hostmatch/default rules. Use before patching gateway/proxy/cache-layer changes to confirm the fix lands in the right service.
disable-model-invocation: false
argument-hint: "<url-or-host/path>"
allowed-tools:
  - Bash(test:*)
  - Bash(find:*)
  - Bash(grep:*)
  - Read
  - Glob
---

# Trace Production Routing

Given a URL or `<host>/<path>` on a Wikimedia-hosted hostname, figure out which production service (api-gateway, rest-gateway, mw-api-ext, ...) actually receives the request. Catches cases where a hostname implies one service but ATS' gateway-check.lua plugin overrides the destination per path. Origin of this skill: T426323, where api.wikimedia.org/service/lw/recommendation/* looked like an api-gateway concern but was fully migrated to rest-gateway.

## Preconditions

- An `operations/puppet` checkout must be available somewhere on disk. This skill is read-only and runs entirely locally — no MCP, no network.

## Locate the puppet checkout

Paths differ per engineer. Resolve in this order, stopping at the first match:

1. **Environment override.** If `$WMF_PUPPET_DIR` is set and points to a valid checkout, use it.
2. **Search under `$HOME`.** Run a bounded find for directories named `puppet`, then filter to the operations/puppet repo by checking for two marker files (both must exist):
   ```bash
   find "$HOME" -maxdepth 5 -type d -name puppet 2>/dev/null
   ```
   For each candidate, verify with `test -f "$d/manifests/site.pp" && test -d "$d/hieradata/common/profile/trafficserver"`. The trafficserver hieradata path is what makes this the WMF operations/puppet repo specifically (vs a generic Puppet codebase).
3. **Resolution.** If exactly one candidate matches, use it. If multiple match, ask the user which to use and suggest they set `$WMF_PUPPET_DIR` so the skill doesn't re-prompt. If none match, tell the user to clone the repo (`git clone ssh://gerrit.wikimedia.org:29418/operations/puppet`) or set `$WMF_PUPPET_DIR` to point at their existing checkout, then stop.

Substitute the resolved path for `<puppet>` in the rest of these steps.

## Steps

1. **Parse `$ARGUMENTS`** into a hostname and a path. Accept:
   - Full URL: `https://api.wikimedia.org/service/lw/recommendation/foo`
   - `<host><path>`: `api.wikimedia.org/service/lw/recommendation/foo`
   - Path only: assume hostname `api.wikimedia.org`

2. **ATS backend mapping.** `grep -n` `<puppet>/hieradata/common/profile/trafficserver/backend.yaml` for the hostname. Find the matching `target:` block and report its `replacement:` (e.g. `https://api-gateway.discovery.wmnet:8087`). Note whether the match is a `type: map` (exact) or `type: regex_map`.

3. **gateway-check.lua override.** Read `<puppet>/modules/profile/files/trafficserver/gateway-check.lua.conf`. Walk the rules in this order (mirroring the Lua plugin):
   - **`ignore`** — if `ignore[<hostname>]` lists a substring that appears in the path, the gateway check is bypassed entirely. Report and stop the override lookup.
   - **`hostmatch[<hostname>]`** — if present, this REPLACES the default rules for that hostname (literal hostname match only, no wildcards). Walk its patterns and find the first whose Lua-pattern matches the path.
   - **`groupmatch[<group>]`** — if the hostname appears in `groups[<group>]`, merge those rules into default. Lookup is order-undefined; report which group(s) apply.
   - **`default`** — base rules, merged with the chosen group/hostmatch overrides.
   For the first matching path pattern, report the `{destination, port, load_fraction}` tuple. Note that `gateway_paths` rules use Lua patterns (e.g. `%w`, `%-`), not POSIX regex.

4. **Final destination.** Combine the two layers:
   - If gateway-check matched with `load_fraction = 1`, that destination is the unconditional override.
   - If `0 < load_fraction < 1`, traffic is split — report both destinations with the percentages.
   - If gateway-check did not match, the ATS backend mapping's `replacement:` is the destination.

5. **Output** — concise summary, e.g.:

   ```
   https://api.wikimedia.org/service/lw/recommendation/api/v1/translation/page-collection-groups

   ATS backend mapping (backend.yaml):
     target: http://api.wikimedia.org/service
     replacement: https://api-gateway.discovery.wmnet:8087/service

   gateway-check.lua (gateway-check.lua.conf):
     hostmatch["api.wikimedia.org"]:
       "/service/lw/recommendation(.*)" → rest-gateway.discovery.wmnet:4113 (load_fraction=1)

   Production destination: rest-gateway (100%)
   Fix location: operations/deployment-charts helmfile.d/services/rest-gateway/values.yaml
   ```

6. **If multiple hostmatch patterns match** (Lua tables are unordered, so the plugin returns the first match it finds — which is non-deterministic), report ALL matches and warn the user that production behaviour for that path may be unpredictable.

## Notes

- `gateway-check.lua` runs as an ATS remap plugin AFTER the initial `regex_map`/`map` in `backend.yaml`. The ATS backend is what `gateway-check.lua` mutates.
- Load_fraction is sampled per request via `math.random()`; with `load_fraction < 1` you cannot predict where any single request lands.
- For non-api.wikimedia.org hosts (e.g. en.wikipedia.org), the `default` block + `groupmatch` typically govern; check which `groups[]` the host belongs to.
- The Lua pattern syntax is similar to regex but not identical: `%w` (word char), `%-` (literal hyphen), `(.*)` (capture group). Don't paste these into a POSIX `grep -P` and expect the same result.

## Input

$ARGUMENTS
