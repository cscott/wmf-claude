#!/bin/bash
# Tests for the wmf-claude plugin and bundled templates: skills/agents have
# valid YAML frontmatter, every JSON file (plugin manifest, hooks, wiring,
# nono pack manifest, MW settings template) is valid, and bin scripts pass
# `bash -n` syntax checks.
#
# Usage: ./tests/test-templates.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }

pass() { green "PASS: $*"; ((PASS++)); }
fail() { red   "FAIL: $*"; ((FAIL++)); }

rel() { realpath --relative-to="$REPO_ROOT" "$1" 2>/dev/null || echo "$1"; }

# Validate YAML frontmatter (between --- markers at start of file).
validate_frontmatter() {
  local file="$1"
  python3 - "$file" <<'PY'
import sys, re
path = sys.argv[1]
text = open(path).read()
m = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
if not m:
    print(f'no frontmatter block in {path}', file=sys.stderr)
    sys.exit(1)
try:
    import yaml
    yaml.safe_load(m.group(1))
except ImportError:
    # Without PyYAML, fall back to a structural check: every non-blank line
    # must start with a key (`key:`) or be a list item (`  - ...`).
    for line in m.group(1).splitlines():
        if not line.strip(): continue
        if not re.match(r'^[A-Za-z_][\w-]*:|^\s*-\s', line):
            print(f'invalid frontmatter line in {path}: {line!r}', file=sys.stderr)
            sys.exit(1)
except Exception as e:
    print(f'invalid YAML in {path}: {e}', file=sys.stderr)
    sys.exit(1)
PY
}

echo "=== Plugin + templates validation ==="
echo ""
echo "--- Skill SKILL.md frontmatter ---"
while IFS= read -r -d '' f; do
  if validate_frontmatter "$f" 2>/dev/null; then pass "$(rel "$f")"; else fail "$(rel "$f")"; fi
done < <(find "$REPO_ROOT/skills" -name 'SKILL.md' -print0)

echo ""
echo "--- Agent .md frontmatter ---"
while IFS= read -r -d '' f; do
  if validate_frontmatter "$f" 2>/dev/null; then pass "$(rel "$f")"; else fail "$(rel "$f")"; fi
done < <(find "$REPO_ROOT/agents" -name '*.md' -print0)

echo ""
echo "--- JSON validity (plugin + nono pack + wiring) ---"
for f in \
  "$REPO_ROOT/.claude-plugin/plugin.json" \
  "$REPO_ROOT/hooks/hooks.json" \
  "$REPO_ROOT/package.json" \
  "$REPO_ROOT/wiring/marketplace.json" \
  "$REPO_ROOT/wiring/known-marketplaces.json" \
  "$REPO_ROOT/wiring/installed-plugin.json" \
  "$REPO_ROOT/wiring/enabled-plugin.json" \
  "$REPO_ROOT/wiring/settings-merge.json" \
  "$REPO_ROOT/templates/mediawiki/settings.json" \
  "$REPO_ROOT/profiles/wmf-engineer.json" \
  "$REPO_ROOT/chrome-devtools-mcp/package.json" \
  "$REPO_ROOT/chrome-devtools-mcp/package-lock.json"; do
  if jq -e . "$f" >/dev/null 2>&1; then pass "$(rel "$f")"; else fail "$(rel "$f")"; fi
done

echo ""
echo "--- chrome-devtools-mcp pin consistency ---"
# package.json must pin an exact version (no caret/tilde) so engineers
# get the reviewed code; package-lock.json must list the same version.
CDP_PIN="$(jq -r '.dependencies["chrome-devtools-mcp"]' "$REPO_ROOT/chrome-devtools-mcp/package.json")"
CDP_LOCKED="$(jq -r '.packages["node_modules/chrome-devtools-mcp"].version' "$REPO_ROOT/chrome-devtools-mcp/package-lock.json")"
if [[ "$CDP_PIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  pass "package.json pins exact version: $CDP_PIN"
else
  fail "package.json must pin an exact version (no ^/~), got: $CDP_PIN"
fi
if [[ "$CDP_PIN" == "$CDP_LOCKED" ]]; then
  pass "package-lock.json matches: $CDP_LOCKED"
else
  fail "package-lock.json version ($CDP_LOCKED) != package.json pin ($CDP_PIN)"
fi

echo ""
echo "--- chrome-devtools-mcp tracked-files allowlist ---"
# Only package.json and package-lock.json should be tracked under
# chrome-devtools-mcp/. mcp-config.json contains absolute $HOME paths from
# the maintainer's machine and must never be force-added; node_modules is
# big and reproducible from the lockfile.
if [[ -d "$REPO_ROOT/.git" || -f "$REPO_ROOT/.git" ]]; then
  UNEXPECTED="$(cd "$REPO_ROOT" && git ls-files chrome-devtools-mcp/ \
                | grep -Ev '^chrome-devtools-mcp/(package\.json|package-lock\.json)$' \
                || true)"
  if [[ -z "$UNEXPECTED" ]]; then
    pass "chrome-devtools-mcp/ tracks only package.json + package-lock.json"
  else
    fail "unexpected tracked files under chrome-devtools-mcp/:"
    while IFS= read -r line; do echo "        $line"; done <<<"$UNEXPECTED"
  fi
else
  pass "chrome-devtools-mcp/ tracked-files check skipped (not a git checkout)"
fi

echo ""
echo "--- nono pack ↔ filesystem consistency ---"
# Every artifacts[].path in package.json must exist on disk.
while IFS= read -r p; do
  if [[ -e "$REPO_ROOT/$p" ]]; then pass "package.json artifact exists: $p"; else fail "package.json artifact missing: $p"; fi
done < <(jq -r '.artifacts[].path' "$REPO_ROOT/package.json")

echo ""
echo "--- bin/ script syntax ---"
for f in "$REPO_ROOT/bin/session-start.sh" "$REPO_ROOT/bin/launch-claude.sh" "$REPO_ROOT/bin/claude" "$REPO_ROOT/bin/launch-test-chrome"; do
  if bash -n "$f" 2>/dev/null; then pass "$(rel "$f")"; else fail "$(rel "$f")"; fi
done

echo ""
echo "--- bin/claude --chrome arg routing ---"
# Run bin/claude in a fake repo with a stubbed `nono` that prints args and
# exits, so we can assert that --chrome is correctly recognized as a wrapper
# flag (and that --open-port 9222 is added to the nono invocation only when
# --chrome is set). Regression test: previously `bin/claude --chrome` (no
# `--` separator) was silently forwarded to claude as an unknown flag.
FAKE_REPO="$(mktemp -d)"
trap 'rm -rf "$FAKE_REPO"' EXIT
mkdir -p "$FAKE_REPO/bin" \
         "$FAKE_REPO/chrome-devtools-mcp/node_modules/.bin" \
         "$FAKE_REPO/mcp-phabricator" \
         "$FAKE_REPO/gerrit-mcp-server"
cp "$REPO_ROOT/bin/claude" "$FAKE_REPO/bin/claude"
touch "$FAKE_REPO/chrome-devtools-mcp/mcp-config.json"
touch "$FAKE_REPO/chrome-devtools-mcp/node_modules/.bin/chrome-devtools-mcp"
chmod +x "$FAKE_REPO/chrome-devtools-mcp/node_modules/.bin/chrome-devtools-mcp"
cat > "$FAKE_REPO/bin/nono" <<'STUB'
#!/bin/bash
printf 'NONO_ARG: %s\n' "$@"
STUB
chmod +x "$FAKE_REPO/bin/nono"

run_fake_claude() {
  PATH="$FAKE_REPO/bin:$PATH" bash "$FAKE_REPO/bin/claude" "$@" 2>&1
}
has_open_port() { grep -qx 'NONO_ARG: --open-port' <<<"$1" && grep -qx 'NONO_ARG: 9222' <<<"$1"; }

out="$(run_fake_claude --chrome)"
if has_open_port "$out"; then pass "bin/claude --chrome opens port 9222"; else fail "bin/claude --chrome did not open port 9222"; fi

out="$(run_fake_claude --chrome --)"
if has_open_port "$out"; then pass "bin/claude --chrome -- opens port 9222"; else fail "bin/claude --chrome -- did not open port 9222"; fi

out="$(run_fake_claude)"
if ! has_open_port "$out"; then pass "plain bin/claude does not open port 9222"; else fail "plain bin/claude leaked --open-port 9222"; fi

# --chrome after `--` is a claude arg, not a wrapper flag — must NOT enable chrome.
out="$(run_fake_claude -- --chrome)"
if ! has_open_port "$out"; then pass "--chrome after -- does not enable chrome mode"; else fail "--chrome after -- incorrectly enabled chrome mode"; fi

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
