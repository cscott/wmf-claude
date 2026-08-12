#!/bin/bash
# Integration tests for nono profiles.
# Requires: nono >= 0.41, jq
#
# Usage:
#   ./tests/test-profile.sh [profile-path]
#
# Default profile: profiles/wmf-engineer.json

set -uo pipefail

PROFILE="${1:-profiles/wmf-engineer.json}"
WORKDIR="$(pwd)"
HOME="${HOME:-/root}"
PASS=0
FAIL=0

# nono 0.51-0.53 broke sandbox command execution on Linux: cat/ls/touch/env all
# exit non-zero inside the sandbox regardless of how the workdir grant is
# expressed (--allow-cwd, --allow $WORKDIR, profile-level filesystem.allow:
# ["$WORKDIR"] all fail the same way). Symptom is the same whether the test
# command is a binary in /usr/bin or a builtin — so the regression isn't
# specific to workdir access. The static (jq-based) tests below still verify
# the profile's structural posture; skip the runtime probes on Linux until
# upstream lands a fix and we can re-enable them by removing this gate.
SKIP_RUNTIME=false
if [[ "$(uname -s)" != "Darwin" ]]; then
  SKIP_RUNTIME=true
fi

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }

assert_status() {
  local description="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    green "PASS: $description"
    ((PASS++))
  else
    red "FAIL: $description (expected=$expected, got=$actual)"
    ((FAIL++))
  fi
}

# Query nono why and return the status field.
# nono may emit log lines to stdout before the JSON, so we extract only the JSON object.
why_path() {
  local path="$1" op="$2"
  local output
  output=$(nono why --silent --profile "$PROFILE" --workdir "$WORKDIR" --path "$path" --op "$op" --json 2>/dev/null \
    | sed -n '/^{/,/^}/p') || true
  echo "$output" | jq -r '.status' 2>/dev/null || echo "error"
}

# Run a command inside the sandbox, return 0 if it succeeds, 1 if blocked.
# Note: no --allow-cwd. nono 0.51-0.53 break that flag on Linux (cat/ls/touch
# all exit non-zero even though the profile says workdir.access:readwrite).
# Our profile grants $WORKDIR via filesystem.allow directly, so the runtime
# flag is redundant. Drop this comment once upstream lands a fix.
run_sandboxed() {
  nono run --profile "$PROFILE" --workdir "$WORKDIR" --silent -- "$@" >/dev/null 2>&1
}

echo "=== Profile validation ==="
echo "Profile: $PROFILE"
echo ""

# --- Structural validation ---
echo "--- Structural validation ---"
if nono profile validate "$PROFILE" >/dev/null 2>&1; then
  green "PASS: profile is valid JSON with valid group references"
  ((PASS++))
else
  red "FAIL: profile validation failed"
  ((FAIL++))
fi

# --- Submodules ---
echo ""
echo "--- Submodules ---"
for mod in mcp-phabricator gerrit-mcp-server; do
  if [[ -f "$WORKDIR/$mod/README.md" ]]; then
    green "PASS: submodule $mod is present"
    ((PASS++))
  else
    red "FAIL: submodule $mod is missing or empty (run: git submodule update --init)"
    ((FAIL++))
  fi
done

# --- Filesystem: denied paths ---
echo ""
echo "--- Filesystem: sensitive paths denied ---"
assert_status "~/.ssh read denied" "denied" "$(why_path "$HOME/.ssh" read)"
assert_status "~/.ssh write denied" "denied" "$(why_path "$HOME/.ssh" write)"
assert_status "~/.gnupg read denied" "denied" "$(why_path "$HOME/.gnupg" read)"

for f in .env .bashrc .bash_profile .bash_history .zshrc .zprofile .zsh_history .profile .netrc .npmrc .pypirc; do
  assert_status "~/$f read denied" "denied" "$(why_path "$HOME/$f" read)"
done
assert_status "~/.composer/auth.json read denied" "denied" "$(why_path "$HOME/.composer/auth.json" read)"
assert_status "~/.config/composer/auth.json read denied" "denied" "$(why_path "$HOME/.config/composer/auth.json" read)"
assert_status "~/.docker/config.json read denied" "denied" "$(why_path "$HOME/.docker/config.json" read)"
assert_status "~/.kube/config read denied" "denied" "$(why_path "$HOME/.kube/config" read)"
assert_status "~/.config/gh read denied" "denied" "$(why_path "$HOME/.config/gh" read)"

# Password managers and secret stores not covered by claude-code's deny_credentials group.
assert_status "~/.password-store read denied" "denied" "$(why_path "$HOME/.password-store" read)"
assert_status "~/.config/bitwarden read denied" "denied" "$(why_path "$HOME/.config/bitwarden" read)"
assert_status "~/.config/keepassxc read denied" "denied" "$(why_path "$HOME/.config/keepassxc" read)"
assert_status "~/Library/Application Support/1Password read denied" "denied" "$(why_path "$HOME/Library/Application Support/1Password" read)"

# Mail and messaging clients (info-leak class).
assert_status "~/Library/Mail read denied" "denied" "$(why_path "$HOME/Library/Mail" read)"
assert_status "~/Library/Messages read denied" "denied" "$(why_path "$HOME/Library/Messages" read)"
assert_status "~/Library/Application Support/Slack read denied" "denied" "$(why_path "$HOME/Library/Application Support/Slack" read)"
assert_status "~/Library/Application Support/Signal read denied" "denied" "$(why_path "$HOME/Library/Application Support/Signal" read)"

# iCloud Drive sync.
assert_status "~/Library/Mobile Documents read denied" "denied" "$(why_path "$HOME/Library/Mobile Documents" read)"

# --- Filesystem: every denied path is also save-prompt suppressed ---
# A path in deny but absent from suppress_save_prompt produces a one-keystroke
# [g] grant prompt at runtime that bakes filesystem.bypass_protection into the
# user's profile, defeating the deny. Anything we deliberately deny must also be
# suppressed so nono never offers to grant it.
echo ""
echo "--- Filesystem: denies are save-prompt suppressed ---"
unsuppressed=$(jq -r '(.filesystem.deny // []) - (.filesystem.suppress_save_prompt // []) | .[]' "$PROFILE" 2>/dev/null)
if [[ -z "$unsuppressed" ]]; then
  green "PASS: every filesystem.deny path is in suppress_save_prompt"
  ((PASS++))
else
  red "FAIL: deny paths missing from suppress_save_prompt (grantable at runtime):"
  printf '  %s\n' $unsuppressed
  ((FAIL++))
fi

# --- Filesystem: working directory (via nono run, since workdir grants are runtime-only) ---
echo ""
echo "--- Filesystem: working directory access ---"

if $SKIP_RUNTIME; then
  echo "SKIP: nono sandbox-command regression on Linux (see SKIP_RUNTIME note)"
else
  if run_sandboxed cat "$WORKDIR/README.md"; then
    green "PASS: workdir read allowed"
    ((PASS++))
  else
    red "FAIL: workdir read should be allowed"
    ((FAIL++))
  fi

  if run_sandboxed sh -c "touch '$WORKDIR/.test-write-probe' && rm '$WORKDIR/.test-write-probe'"; then
    green "PASS: workdir write allowed"
    ((PASS++))
  else
    red "FAIL: workdir write should be allowed"
    ((FAIL++))
    rm -f "$WORKDIR/.test-write-probe"
  fi
fi

# --- Network: profile allowlist structure ---
# nono why --host only checks the capability layer, which is blocked for every
# host on this profile (all traffic goes through the proxy). So to verify the
# proxy-level allowlist we assert the profile JSON directly.
echo ""
echo "--- Network: profile allowlist structure ---"

if jq -e '.network.allow_domain | index("github.com")' "$PROFILE" >/dev/null 2>&1; then
  red "FAIL: github.com should not be in allow_domain (would enable HTTPS push to GitHub)"
  ((FAIL++))
else
  green "PASS: github.com not in allow_domain"
  ((PASS++))
fi

if jq -e '.network.allow_domain | index("*.wikimedia.org")' "$PROFILE" >/dev/null 2>&1; then
  green "PASS: *.wikimedia.org in allow_domain"
  ((PASS++))
else
  red "FAIL: *.wikimedia.org should be in allow_domain (required for Gerrit MCP)"
  ((FAIL++))
fi

for port in 22 29418; do
  if jq -e ".network.open_port | index($port)" "$PROFILE" >/dev/null 2>&1; then
    red "FAIL: port $port should not be in network.open_port (would enable SSH push)"
    ((FAIL++))
  else
    green "PASS: port $port not in network.open_port"
    ((PASS++))
  fi
done

# --- Network: read-only docs domains (allow_domain endpoint rules) ---
# Static documentation hosts are allow-listed as endpoint-restricted objects
# that permit only read methods (GET, HEAD). Any endpoint rule forces nono TLS
# interception, so a write request (POST/PUT/...) is rejected with 403 before it
# leaves the sandbox. We assert the structure here; the live GET=200 / POST=403
# check is in SECURITY.md (needs external egress, cannot run nested in a sandbox).
echo ""
echo "--- Network: docs domains are read-only (GET/HEAD) ---"
DOCS_READ_ONLY=(docs.python.org docs.rs doc.rust-lang.org developer.mozilla.org \
                nodejs.org pkg.go.dev www.php.net php.net)
for d in "${DOCS_READ_ONLY[@]}"; do
  # Object entry whose endpoints are non-empty, all read methods (GET/HEAD), and
  # include at least one GET (so write verbs 403 while reads still resolve).
  if jq -e --arg d "$d" '
        .network.allow_domain
        | map(select(type=="object" and .domain==$d))[0] as $e
        | ($e != null)
          and (($e.endpoints | length) > 0)
          and (all($e.endpoints[]; .method=="GET" or .method=="HEAD"))
          and (any($e.endpoints[]; .method=="GET"))
      ' "$PROFILE" >/dev/null 2>&1; then
    green "PASS: $d is read-only (GET/HEAD; no write method permitted)"
    ((PASS++))
  else
    red "FAIL: $d should be an endpoint-restricted read-only (GET/HEAD) entry"
    ((FAIL++))
  fi
  # Must not ALSO appear as a plain string, which would re-open every method.
  if jq -e --arg d "$d" '.network.allow_domain | index($d)' "$PROFILE" >/dev/null 2>&1; then
    red "FAIL: $d also present as a plain all-methods entry (read-only bypassed)"
    ((FAIL++))
  else
    green "PASS: $d not duplicated as a plain all-methods entry"
    ((PASS++))
  fi
done

# The model API must stay a plain tunnel: endpoint rules would intercept its
# TLS, which 403s the POST-based model calls and routes Claude's own traffic
# through nono in plaintext. Guard against anyone adding rules there.
for d in api.anthropic.com claude.ai platform.claude.com; do
  if jq -e --arg d "$d" '.network.allow_domain | index($d)' "$PROFILE" >/dev/null 2>&1; then
    green "PASS: $d stays a plain tunnel (not intercepted)"
    ((PASS++))
  else
    red "FAIL: $d must remain a plain allow_domain entry (endpoint rules would break the API)"
    ((FAIL++))
  fi
done

# --- Allowed commands ---
echo ""
echo "--- Allowed commands ---"

if $SKIP_RUNTIME; then
  echo "SKIP: nono sandbox-command regression on Linux (see SKIP_RUNTIME note)"
else
  if run_sandboxed ls "$WORKDIR"; then
    green "PASS: ls in workdir allowed"
    ((PASS++))
  else
    red "FAIL: ls in workdir should be allowed"
    ((FAIL++))
  fi
fi

# --- Network: localhost port access ---
echo ""
echo "--- Network: localhost port access ---"

# Verify profile declares open_port for MySQL.
# Runtime enforcement works on macOS but not yet on Linux
# (nono blocks localhost connections under seccomp/landlock even with open_port set).
# TODO: replace with a real sandboxed connection test once nono supports open_port on Linux.
if jq -e '.network.open_port | index(3306)' "$PROFILE" >/dev/null 2>&1; then
  green "PASS: network.open_port includes 3306 (MySQL)"
  ((PASS++))
else
  red "FAIL: network.open_port should include 3306 for local MySQL access"
  ((FAIL++))
fi

# --- Network: arbitrary hosts denied ---
# Structural, not `nono why --host`: since nono 0.62 the domain filter is applied
# at runtime, so `nono why` reports every host "allowed" and no longer reflects
# allow_domain (job 882901). allow_domain is non-empty (asserted above), so the
# proxy denies any unlisted host; a host absent from allow_domain matches no entry
# — not an exact host (plain or object .domain) nor a *.suffix wildcard.
echo ""
echo "--- Network: arbitrary hosts denied ---"
for host in example.com google.com; do
  if jq -e --arg h "$host" '
        [.network.allow_domain[] | if type=="object" then .domain else . end]
        | any(.[]; . as $e | $e == $h or (($e|startswith("*.")) and ($h|endswith($e[1:]))))
      ' "$PROFILE" >/dev/null 2>&1; then
    red "FAIL: $host is covered by allow_domain (should be denied)"; ((FAIL++))
  else
    green "PASS: $host not in allow_domain (denied by proxy)"; ((PASS++))
  fi
done

# --- Network: live egress enforcement (runtime) ---
# Behavioural check the structural one can't do: egress through a fresh sandbox
# must be refused for a non-allowlisted host and succeed for an allowlisted one.
# Runtime-only — skipped on Linux CI (SKIP_RUNTIME), unrunnable nested in a sandbox.
echo ""
echo "--- Network: live egress enforcement (runtime) ---"
if $SKIP_RUNTIME; then
  echo "SKIP: nono sandbox-command regression on Linux (see SKIP_RUNTIME note)"
else
  if run_sandboxed curl -sS -m 15 -o /dev/null https://example.com; then
    red "FAIL: egress to non-allowlisted example.com should be denied"; ((FAIL++))
  else
    green "PASS: egress to non-allowlisted example.com denied"; ((PASS++))
  fi
  if run_sandboxed curl -sS -m 15 -o /dev/null https://en.wikipedia.org; then
    green "PASS: egress to allowlisted en.wikipedia.org allowed"; ((PASS++))
  else
    red "FAIL: egress to allowlisted en.wikipedia.org should be allowed"; ((FAIL++))
  fi
fi

# --- Security: process isolation modes ---
echo ""
echo "--- Security: process isolation modes ---"
for mode in signal_mode process_info_mode; do
  if [[ "$(jq -r ".security.$mode" "$PROFILE")" == "isolated" ]]; then
    green "PASS: security.$mode is isolated"
    ((PASS++))
  else
    red "FAIL: security.$mode should be 'isolated'"
    ((FAIL++))
  fi
done
if [[ "$(jq -r '.security.ipc_mode' "$PROFILE")" == "shared_memory_only" ]]; then
  green "PASS: security.ipc_mode is shared_memory_only"
  ((PASS++))
else
  red "FAIL: security.ipc_mode should be 'shared_memory_only'"
  ((FAIL++))
fi

# --- Mach service denials (keychain hardening) ---
# The base claude-code profile grants readwrite to ~/Library/Keychains via a
# built-in exception, but keychain access in practice flows through securityd
# over Mach lookup, not direct file reads. Denying mach-lookup for the security
# daemons closes the practical exfil path.
echo ""
echo "--- Mach service denials ---"
# com.apple.metadata.mds blocks Spotlight (mdfind) recon of filesystem layout.
# Keychain-related daemons (com.apple.securityd, SecurityServer, SecurityAgent)
# are intentionally NOT denied: Claude Code itself stores its OAuth token in
# the login keychain and reads it via securityd on startup, so denying that
# service breaks `/login` and "Not logged in" appears on every session start.
# The base claude-code profile's filesystem.allow + bypass_protection on
# ~/Library/Keychains is exactly for this. The env-var allowlist (above)
# removes most secret material the agent could otherwise observe.
# Use a prefix match on com.apple.metadata.* so any new metadata.mds variant
# (e.g. mds_stores) is also denied; the asserted substring is the prefix.
for substring in 'com.apple.metadata.'; do
  if jq -e --arg s "$substring" '.unsafe_macos_seatbelt_rules[] | select(contains($s))' "$PROFILE" >/dev/null 2>&1; then
    green "PASS: mach-lookup deny present for ${substring}*"
    ((PASS++))
  else
    red "FAIL: mach-lookup deny missing for ${substring}*"
    ((FAIL++))
  fi
done

# --- Environment variable allowlist (credential exfil hardening) ---
# When environment.allow_vars is set, only matching variables are passed to the
# sandboxed child. We assert that common credential-shaped vars are NOT in the
# allowlist (so they get filtered out).
echo ""
echo "--- Environment: credential-shaped vars filtered ---"
if [[ "$(jq -r '.environment.allow_vars | length' "$PROFILE")" -gt 0 ]]; then
  green "PASS: environment.allow_vars is configured"
  ((PASS++))
else
  red "FAIL: environment.allow_vars should be set to filter env"
  ((FAIL++))
fi
for var_pattern in "AWS_*" "GITHUB_TOKEN" "GH_TOKEN" "NPM_TOKEN" "GCLOUD_*" "AZURE_*" "KUBECONFIG" "DOCKER_*"; do
  if jq -e --arg p "$var_pattern" '.environment.allow_vars | index($p)' "$PROFILE" >/dev/null 2>&1; then
    red "FAIL: $var_pattern should NOT be in environment.allow_vars (credential exfil risk)"
    ((FAIL++))
  else
    green "PASS: $var_pattern is filtered out"
    ((PASS++))
  fi
done

# --- Runtime: Spotlight (mdfind) denied (macOS only; runs outside sandbox) ---
# Probes that the mach-lookup deny on com.apple.metadata.* actually fires.
# `mdfind` queries Spotlight via the metadata daemons; when blocked, it
# returns no results. Exit code alone is unreliable — on recent macOS,
# mdfind exits 0 with empty output when it can't reach the daemon, so
# we count result lines instead. The query is intentionally broad
# (kMDItemDisplayName=*) so an unsandboxed run would return thousands.
echo ""
echo "--- Runtime: Spotlight Mach access denied ---"
if $SKIP_RUNTIME; then
  echo "SKIP: macOS-only test (Mach lookup rules don't apply on Linux)"
else
  mdfind_lines=$(nono run --profile "$PROFILE" --workdir "$WORKDIR" \
                   --silent -- mdfind 'kMDItemDisplayName=*' \
                   2>/dev/null | wc -l | tr -d ' ')
  if [[ "$mdfind_lines" -gt 0 ]]; then
    red "FAIL: 'mdfind' returned $mdfind_lines result(s) inside sandbox — Spotlight Mach access not blocked"
    ((FAIL++))
  else
    green "PASS: 'mdfind' returned no results (Spotlight Mach service blocked)"
    ((PASS++))
  fi
fi

# --- Runtime: env-var allowlist filters credential-shaped vars ---
# Verifies environment.allow_vars actually filters at the syscall layer, not
# just in the JSON. Sets two synthetic vars in the parent env, runs `env`
# inside the sandbox, and checks which ones survived the allowlist.
echo ""
echo "--- Runtime: env-var allowlist filtering ---"
if $SKIP_RUNTIME; then
  echo "SKIP: nono sandbox-command regression on Linux (see SKIP_RUNTIME note)"
else
  env_probe_out=$(AWS_FAKE_PROBE=should-be-filtered \
                  CLAUDE_FAKE_PROBE=should-pass-through \
                  nono run --profile "$PROFILE" --workdir "$WORKDIR" \
                    --silent -- env 2>/dev/null || true)
  if printf '%s\n' "$env_probe_out" | grep -q '^AWS_FAKE_PROBE='; then
    red "FAIL: AWS_FAKE_PROBE leaked through to sandbox child (allow_vars not enforced)"
    ((FAIL++))
  else
    green "PASS: AWS_FAKE_PROBE filtered out by allow_vars"
    ((PASS++))
  fi
  if printf '%s\n' "$env_probe_out" | grep -q '^CLAUDE_FAKE_PROBE='; then
    green "PASS: CLAUDE_FAKE_PROBE passed through (CLAUDE_* prefix matches)"
    ((PASS++))
  else
    red "FAIL: CLAUDE_FAKE_PROBE was filtered (CLAUDE_* prefix should match)"
    ((FAIL++))
  fi
fi

# --- Summary ---
echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
