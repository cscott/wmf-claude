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
  "$REPO_ROOT/profiles/wmf-engineer.json"; do
  if jq -e . "$f" >/dev/null 2>&1; then pass "$(rel "$f")"; else fail "$(rel "$f")"; fi
done

echo ""
echo "--- nono pack ↔ filesystem consistency ---"
# Every artifacts[].path in package.json must exist on disk.
while IFS= read -r p; do
  if [[ -e "$REPO_ROOT/$p" ]]; then pass "package.json artifact exists: $p"; else fail "package.json artifact missing: $p"; fi
done < <(jq -r '.artifacts[].path' "$REPO_ROOT/package.json")

echo ""
echo "--- bin/ script syntax ---"
for f in "$REPO_ROOT/bin/session-start.sh" "$REPO_ROOT/bin/launch-claude.sh" "$REPO_ROOT/bin/claude"; do
  if bash -n "$f" 2>/dev/null; then pass "$(rel "$f")"; else fail "$(rel "$f")"; fi
done

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
