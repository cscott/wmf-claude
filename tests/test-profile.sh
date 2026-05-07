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

why_host() {
  local host="$1"
  local output
  output=$(nono why --silent --profile "$PROFILE" --workdir "$WORKDIR" --host "$host" --json 2>/dev/null \
    | sed -n '/^{/,/^}/p') || true
  echo "$output" | jq -r '.status' 2>/dev/null || echo "error"
}

# Run a command inside the sandbox, return 0 if it succeeds, 1 if blocked
run_sandboxed() {
  nono run --profile "$PROFILE" --workdir "$WORKDIR" --allow-cwd --silent -- "$@" >/dev/null 2>&1
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

# --- Filesystem: working directory (via nono run, since workdir grants are runtime-only) ---
echo ""
echo "--- Filesystem: working directory access ---"

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

# --- Allowed commands ---
echo ""
echo "--- Allowed commands ---"

if run_sandboxed ls "$WORKDIR"; then
  green "PASS: ls in workdir allowed"
  ((PASS++))
else
  red "FAIL: ls in workdir should be allowed"
  ((FAIL++))
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

# --- Network: blocked hosts ---
echo ""
echo "--- Network: arbitrary hosts denied ---"
assert_status "example.com denied" "denied" "$(why_host example.com)"
assert_status "google.com denied" "denied" "$(why_host google.com)"

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
for svc in com.apple.metadata.mds; do
  if jq -e --arg svc "$svc" '.unsafe_macos_seatbelt_rules[] | select(contains($svc))' "$PROFILE" >/dev/null 2>&1; then
    green "PASS: mach-lookup deny present for $svc"
    ((PASS++))
  else
    red "FAIL: mach-lookup deny missing for $svc"
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
# Probes that the Mach lookup rule for com.apple.metadata.mds actually fires.
# `mdfind` queries Spotlight, which talks to the metadata server over Mach.
echo ""
echo "--- Runtime: Spotlight Mach access denied ---"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "SKIP: macOS-only test (Mach lookup rules don't apply on Linux)"
elif run_sandboxed mdfind 'kMDItemDisplayName=*'; then
  red "FAIL: 'mdfind' should be denied by mach-lookup rule for com.apple.metadata.mds"
  ((FAIL++))
else
  green "PASS: 'mdfind' denied (Spotlight Mach service blocked)"
  ((PASS++))
fi

# --- Runtime: env-var allowlist filters credential-shaped vars ---
# Verifies environment.allow_vars actually filters at the syscall layer, not
# just in the JSON. Sets two synthetic vars in the parent env, runs `env`
# inside the sandbox, and checks which ones survived the allowlist.
echo ""
echo "--- Runtime: env-var allowlist filtering ---"
env_probe_out=$(AWS_FAKE_PROBE=should-be-filtered \
                CLAUDE_FAKE_PROBE=should-pass-through \
                nono run --profile "$PROFILE" --workdir "$WORKDIR" \
                  --allow-cwd --silent -- env 2>/dev/null || true)
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

# --- Summary ---
echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
