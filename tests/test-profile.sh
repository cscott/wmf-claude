#!/bin/bash
# Integration tests for nono profiles.
# Requires: nono, jq
#
# Usage:
#   ./tests/test-profile.sh [profile-path]
#
# Default profile: profiles/wmf-engineer.json

set -euo pipefail

PROFILE="${1:-profiles/wmf-engineer.json}"
WORKDIR="$(pwd)"
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
  nono why --silent --profile "$PROFILE" --workdir "$WORKDIR" --path "$path" --op "$op" --json 2>/dev/null \
    | sed -n '/^{/,/^}/p' | jq -r '.status'
}

why_host() {
  local host="$1"
  nono why --silent --profile "$PROFILE" --workdir "$WORKDIR" --host "$host" --json 2>/dev/null \
    | sed -n '/^{/,/^}/p' | jq -r '.status'
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
if nono policy validate "$PROFILE" >/dev/null 2>&1; then
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

# --- Blocked commands ---
echo ""
echo "--- Blocked commands ---"

for cmd in ssh scp sftp; do
  if run_sandboxed "$cmd" 2>/dev/null; then
    red "FAIL: $cmd should be blocked"
    ((FAIL++))
  else
    green "PASS: $cmd is blocked"
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

# --- Network: blocked hosts ---
echo ""
echo "--- Network: arbitrary hosts denied ---"
assert_status "example.com denied" "denied" "$(why_host example.com)"
assert_status "google.com denied" "denied" "$(why_host google.com)"

# --- Summary ---
echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
