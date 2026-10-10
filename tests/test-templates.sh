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
echo "--- permission rule shapes ---"
# Four shapes that parse as valid JSON and silently protect nothing.
# Verified against Claude Code 2.1.280. A `Write(path)` rule is never matched
# by file-permission checks and only emits a startup warning; `Edit(path)`
# already covers Write and NotebookEdit. A Bash rule that holds `:*` anywhere
# but at the end is rejected at load; `Bash(cmd * more)` is the glob form that
# fires. A `Read(**/x)` rule is relative to the session cwd, so it misses the
# same file in a `--read` sibling repo; `//**/x` anchors at the filesystem
# root. A find deny rule that keeps a space before the dash misses
# `find -exec ...`, because GNU find lets you omit the path operand.
SM="$REPO_ROOT/wiring/settings-merge.json"
# `// []` keeps a file with only an allow list readable. Without it jq errors
# to stderr, the command substitution keeps only stdout, and every check below
# passes over an empty rule list.
rules_of() { jq -er '.permissions | ((.allow // []) + (.deny // []) + (.ask // []))[]' "$1"; }
SMN="${SM#"$REPO_ROOT"/}"
RULES="$(rules_of "$SM")" || fail "cannot read permission rules from $SMN"

for f in "$SM" "$REPO_ROOT/templates/mediawiki/settings.json"; do
  n="${f#"$REPO_ROOT"/}"
  frules="$(rules_of "$f")" || fail "cannot read permission rules from $n"
  if grep -q '^Write(' <<<"$frules"; then
    fail "$n has Write() rules (never matched; use Edit())"
  else
    pass "$n has no Write() rules"
  fi
  # Match Claude Code's own rule, not just the single-token case:
  # `Bash(npm run x:* -y)` is as dead as `Bash(find:* -exec*)`.
  BAD=""
  while IFS= read -r rule; do
    [[ -z "$rule" ]] && continue
    c="${rule#Bash(}"; c="${c%)}"
    if [[ "$c" == *':*'* && "$c" != *':*' ]]; then BAD+="${BAD:+, }$rule"; fi
  done < <(grep -E '^Bash\(' <<<"$frules")
  if [[ -n "$BAD" ]]; then
    fail "$n has a Bash rule with :* before the end (never matches): $BAD"
  else
    pass "$n Bash rules end at :* or use the glob form"
  fi
done

# A cwd-relative `**/` rule may stay (Linux ignores glob rules, so the
# explicit forms are kept), but only beside its root-anchored `//**/` twin.
UNANCHORED=""
while IFS= read -r r; do
  [[ -z "$r" ]] && continue
  twin="$(sed 's|(\*\*/|(//**/|' <<<"$r")"
  grep -qxF "$twin" <<<"$RULES" || UNANCHORED+="$r "
done < <(grep -E '^(Read|Edit)\(\*\*/' <<<"$RULES")
if [[ -n "$UNANCHORED" ]]; then
  fail "settings-merge.json cwd-relative rule(s) without a //**/ twin: $UNANCHORED"
else
  pass "settings-merge.json every **/ file rule has a //**/ twin"
fi

# Claude Code loads config from BOTH ~/.claude/ and a project's .claude/, so a
# rule denied at only one scope leaves the other as an open path. This asserts
# the pair stays in step. It is defense in depth, not a boundary: these rules
# bind the Edit/Write tools, while Bash can still reach the same files through
# an interpreter and the OS layer must leave ~/.claude writable for Claude Code. (plugins/** is
# deliberately absent: it is user-global only.)
PARITY_MISSING=""
for cfg in settings.json settings.local.json 'hooks/**' 'agents/**' 'skills/**' 'commands/**'; do
  grep -qxF "Edit(.claude/$cfg)"  <<<"$RULES" || PARITY_MISSING+="project .claude/$cfg; "
  grep -qxF "Edit(~/.claude/$cfg)" <<<"$RULES" || PARITY_MISSING+="user ~/.claude/$cfg; "
done
# Config that steers a future session but lives OUTSIDE ~/.claude/: the nono
# base profile grants read-write on ~/.claude.json (mcpServers, allowedTools,
# trust state), ~/.claude/CLAUDE.md loads into every session in every project,
# and a project .mcp.json defines servers the deny rules do not cover.
for cfg in 'Edit(~/.claude.json)' 'Edit(~/.claude/CLAUDE.md)' 'Edit(.mcp.json)'; do
  grep -qxF "$cfg" <<<"$RULES" || PARITY_MISSING+="$cfg; "
done

if [[ -n "$PARITY_MISSING" ]]; then
  fail "settings-merge.json .claude/ denies not at both scopes: $PARITY_MISSING"
else
  pass "settings-merge.json denies .claude/ config at both project and user scope"
fi

# Every find deny needs both forms. `Bash(find * -exec*)` alone misses
# `find -exec rm {} \;`, because GNU find lets you omit the path operand.
# `Bash(find *-exec*)` alone is worse: it becomes a substring match and denies
# a read-only `find ./pre-delete -name x`, with no prompt to override it.
# The glob syntax has no alternation, so each action takes a pair. This
# applies to the deny list only: the no-path form would widen an allow rule.
DENIES="$(jq -er '.permissions.deny // [] | .[]' "$SM")" || fail "no deny list in $SMN"
LONELY=""
while IFS= read -r rule; do
  [[ -z "$rule" ]] && continue
  case "$rule" in
    "Bash(find -"*)  twin="${rule/find -/find * -}" ;;
    "Bash(find * -"*) twin="${rule/find \* -/find -}" ;;
    *) fail "settings-merge.json find deny rule is neither form: $rule"; continue ;;
  esac
  grep -qxF "$twin" <<<"$DENIES" || LONELY+="${LONELY:+, }$rule (wants $twin)"
done < <(grep -E '^Bash\(find ' <<<"$DENIES")
if [[ -n "$LONELY" ]]; then
  fail "settings-merge.json find deny rule(s) missing the twin form: $LONELY"
elif [[ -n "$DENIES" ]]; then
  pass "settings-merge.json every find deny rule has both forms"
fi

echo ""
echo "--- docker-egress templates ---"
# Structural checks (no YAML parser is guaranteed on the host): each override
# must isolate the PHP services and must not put them back on a routed
# network; the squid config must end in a deny.
for f in egress-none.yml egress-allowlist.yml; do
  t="$REPO_ROOT/templates/docker-egress/$f"
  if [[ -f "$t" ]] && grep -q 'internal: true' "$t"; then
    pass "$f defines an internal network"
  else
    fail "$f missing or lacks an internal network"
  fi
  # The exec-target and jobrunner services must list ONLY [isolated]; putting
  # them on default too would give the agent's code a route out.
  if awk '/^  (mediawiki|mediawiki-jobrunner):$/{svc=1;next} svc&&/networks:/{print;svc=0}' "$t" \
      | grep -qv '\[isolated\]$'; then
    fail "$f gives a PHP service a non-isolated network"
  else
    pass "$f keeps PHP services on the isolated network only"
  fi
done
SQUID_CONF="$REPO_ROOT/templates/docker-egress/squid-allowlist.conf"
if [[ "$(grep '^http_access' "$SQUID_CONF" | tail -n1)" == "http_access deny all" ]]; then
  pass "squid-allowlist.conf ends with deny all"
else
  fail "squid-allowlist.conf must end with 'http_access deny all'"
fi

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
# Every shell script under bin/ (launchers, the build/configure scripts, and
# the sourced lib-output.sh) must parse. Glob so new scripts are covered too.
for f in "$REPO_ROOT"/bin/*; do
  [[ -f "$f" ]] || continue
  # Syntax-check by interpreter: bin/ holds both bash scripts and a Python
  # script (launch-docker-broker), so pick the checker from the shebang.
  shebang="$(head -n1 "$f")"
  if [[ "$shebang" == *python* ]]; then
    # ast.parse checks syntax without writing __pycache__ bytecode.
    if python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null; then
      pass "$(rel "$f")"
    else
      fail "$(rel "$f")"
    fi
  else
    if bash -n "$f" 2>/dev/null; then pass "$(rel "$f")"; else fail "$(rel "$f")"; fi
  fi
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
         "$FAKE_REPO/gerrit-mcp-server" \
         "$FAKE_REPO/gitlab-mcp-server" \
         "$FAKE_REPO/profiles"
cp "$REPO_ROOT/bin/claude" "$FAKE_REPO/bin/claude"
# bin/claude checks the profile file exists before launching. --allow-post reads
# the real network section.
jq '{security: {signal_mode: "isolated"}, network: .network}' \
  "$REPO_ROOT/profiles/wmf-engineer.json" > "$FAKE_REPO/profiles/wmf-engineer.json"
# An IDE terminal sets this, which would put every launch below in IDE mode.
unset CLAUDE_CODE_SSE_PORT
mkdir -p "$FAKE_REPO/wiring"
echo '{}' > "$FAKE_REPO/wiring/settings-merge.json"
touch "$FAKE_REPO/chrome-devtools-mcp/mcp-config.json"
touch "$FAKE_REPO/chrome-devtools-mcp/node_modules/.bin/chrome-devtools-mcp"
chmod +x "$FAKE_REPO/chrome-devtools-mcp/node_modules/.bin/chrome-devtools-mcp"
mkdir -p "$FAKE_REPO/gerrit-mcp-server/gerrit_mcp_server" && touch "$FAKE_REPO/gerrit-mcp-server/gerrit_mcp_server/main.py"
cat > "$FAKE_REPO/bin/nono" <<'STUB'
#!/bin/bash
printf 'NONO_ARG: %s\n' "$@"
env | grep -E '^(WMF_CLAUDE_|CLAUDE_CODE_SSE_PORT=)' | sed 's/^/NONO_ENV: /'
for ((i = 1; i <= $#; i++)); do
  [[ "${!i}" == "--profile" ]] && { j=$((i + 1)); printf 'NONO_PROFILE: %s\n' "$(jq -c . "${!j}")"; }
done
STUB
chmod +x "$FAKE_REPO/bin/nono"
# Report Darwin by default, so the arg-routing checks below do not depend on
# the host. On Linux, bin/claude refuses localhost flags without
# --landlock-only (nolabs-ai/nono#1786). FAKE_UNAME=Linux tests that path.
cat > "$FAKE_REPO/bin/uname" <<'STUB'
#!/bin/sh
if [ "$1" = "-s" ]; then echo "${FAKE_UNAME:-Darwin}"; else command -p uname "$@"; fi
STUB
chmod +x "$FAKE_REPO/bin/uname"

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

echo "--- bin/claude --local-db arg routing ---"
has_db_port() { grep -qx 'NONO_ARG: --open-port' <<<"$1" && grep -qx "NONO_ARG: $2" <<<"$1"; }
out="$(run_fake_claude --local-db)"
if has_db_port "$out" 3306; then pass "bin/claude --local-db opens 3306"; else fail "bin/claude --local-db did not open 3306"; fi
out="$(run_fake_claude --local-db=3307)"
if has_db_port "$out" 3307 && ! grep -qx 'NONO_ARG: 3306' <<<"$out"; then pass "--local-db=3307 opens only 3307"; else fail "--local-db=3307 did not override the port"; fi
out="$(run_fake_claude)"
if ! grep -qx 'NONO_ARG: 3306' <<<"$out"; then pass "plain bin/claude does not open 3306"; else fail "plain bin/claude leaked --open-port 3306"; fi
out="$(run_fake_claude -- --local-db)"
if ! grep -qx 'NONO_ARG: 3306' <<<"$out"; then pass "--local-db after -- does not open the port"; else fail "--local-db after -- was wrongly consumed"; fi
out="$(run_fake_claude --local-db=99999 2>&1)"
if grep -q 'not a valid TCP port' <<<"$out"; then pass "--local-db rejects an invalid port"; else fail "--local-db accepted an invalid port"; fi
# No static port of any kind. /login's callback bind is a Darwin-only
# --listen-port from the launcher (bind-only; Seatbelt cannot filter by port);
# Linux filters per port and is left as it was.
if ! jq -e '((.network.open_port // []) | length > 0) or ((.network.listen_port // []) | length > 0)' "$REPO_ROOT/profiles/wmf-engineer.json" >/dev/null 2>&1; then
  pass "the static profile opens or listens on no port (all per-invocation)"
else
  fail "the static profile has a static open_port/listen_port"
fi
out="$(run_fake_claude)"
if grep -A1 -x "NONO_ARG: --listen-port" <<<"$out" | grep -qx "NONO_ARG: 49152"; then pass "macOS: launcher passes a listen-only port for the /login callback"; else fail "macOS: --listen-port missing — /login cannot bind its callback"; fi
out="$(FAKE_UNAME=Linux run_fake_claude)"
if ! grep -qx "NONO_ARG: --listen-port" <<<"$out"; then pass "Linux: no --listen-port (per-port Landlock bind would not cover an ephemeral callback)"; else fail "Linux: --listen-port passed"; fi

echo "--- bin/claude --landlock-only (nolabs-ai/nono#1786) ---"
run_fake_linux_claude() { FAKE_UNAME=Linux run_fake_claude "$@"; }
has_landlock() { grep -A1 -x 'NONO_ARG: --sandbox-policy' <<<"$1" | grep -qx 'NONO_ARG: landlock'; }
out="$(run_fake_linux_claude --local-web)"
if grep -q 'nolabs-ai/nono#1786' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then pass "Linux: --local-web without --landlock-only refuses to launch"; else fail "Linux: --local-web without --landlock-only launched"; fi
out="$(run_fake_linux_claude --local-db)"
if ! grep -q '^NONO_ARG:' <<<"$out"; then pass "Linux: --local-db without --landlock-only refuses to launch"; else fail "Linux: --local-db without --landlock-only launched"; fi
out="$(run_fake_linux_claude --open-port 9000 --)"
if ! grep -q '^NONO_ARG:' <<<"$out"; then pass "Linux: a user --open-port without --landlock-only refuses to launch"; else fail "Linux: a user --open-port without --landlock-only launched"; fi
out="$(run_fake_linux_claude)"
if grep -q '^NONO_ARG:' <<<"$out" && ! grep -qx 'NONO_ARG: --sandbox-policy' <<<"$out"; then pass "Linux: plain launch keeps the default sandbox policy"; else fail "Linux: plain launch changed the sandbox policy or did not launch"; fi
out="$(run_fake_linux_claude --local-web --landlock-only)"
if has_landlock "$out" && grep -A1 -x 'NONO_ARG: --open-port' <<<"$out" | grep -qx 'NONO_ARG: 8080' \
   && ! grep -qxE 'NONO_ARG: (80|443)' <<<"$out"; then
  pass "Linux: --local-web --landlock-only passes --sandbox-policy landlock and opens only 8080"
else
  fail "Linux: --local-web --landlock-only did not set the policy or opened 80/443"
fi
if grep -q -- '--landlock-only (no seccomp net filter)' <<<"$out"; then pass "Linux: --landlock-only shows in WMF_CLAUDE_SESSION"; else fail "Linux: --landlock-only missing from WMF_CLAUDE_SESSION"; fi
out="$(run_fake_linux_claude --local-web=443 --landlock-only)"
if grep -q 'refuses port 443' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then pass "Linux: --landlock-only refuses --local-web=443"; else fail "Linux: --landlock-only accepted --local-web=443"; fi
out="$(run_fake_linux_claude --local-web=4000,40000-40002 --landlock-only)"
if [[ "$(grep -A1 -x 'NONO_ARG: --open-port' <<<"$out" | grep -xE 'NONO_ARG: [0-9]+' | tr '\n' ' ')" == "NONO_ARG: 4000 NONO_ARG: 40000 NONO_ARG: 40001 NONO_ARG: 40002 " ]] \
   && grep -q -- '--local-web=4000,40000-40002' <<<"$out"; then
  pass "Linux: --local-web=LO-HI opens each port in the range and shows the range"
else
  fail "Linux: --local-web=LO-HI did not open exactly the range"
fi
out="$(run_fake_linux_claude --local-web=400-500 --landlock-only)"
if grep -q 'refuses port 443' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then pass "Linux: --landlock-only refuses a --local-web range that holds 443"; else fail "Linux: --landlock-only accepted a range that holds 443"; fi
out="$(run_fake_linux_claude --local-web=10000-30000 --landlock-only)"
if grep -q 'at most 8192 ports' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then pass "Linux: --local-web refuses a range of more than 8192 ports"; else fail "Linux: --local-web accepted a huge range"; fi
out="$(run_fake_linux_claude --landlock-only --open-port=80 --)"
if grep -q 'refuses port 80' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then pass "Linux: --landlock-only refuses a user --open-port=80"; else fail "Linux: --landlock-only accepted a user --open-port=80"; fi
out="$(run_fake_linux_claude --landlock-only --sandbox-policy landlock --)"
if grep -q 'do not pass both' <<<"$out"; then pass "Linux: --landlock-only with a user --sandbox-policy is refused"; else fail "Linux: --landlock-only with a user --sandbox-policy was accepted"; fi
out="$(run_fake_linux_claude --local-web --sandbox-policy landlock --)"
if grep -q '^NONO_ARG:' <<<"$out" && [[ "$(grep -cx 'NONO_ARG: --sandbox-policy' <<<"$out")" == 1 ]]; then pass "Linux: a user --sandbox-policy launches without a duplicate"; else fail "Linux: a user --sandbox-policy was refused or duplicated"; fi
out="$(run_fake_linux_claude --landlock-only)"
if grep -q '^NONO_ARG:' <<<"$out" && ! grep -qx 'NONO_ARG: --sandbox-policy' <<<"$out"; then pass "Linux: --landlock-only alone does not weaken the sandbox"; else fail "Linux: --landlock-only alone changed the policy or did not launch"; fi
out="$(run_fake_claude --local-web)"
if ! grep -qx 'NONO_ARG: --sandbox-policy' <<<"$out" && grep -qx 'NONO_ARG: 443' <<<"$out"; then pass "macOS: --local-web keeps the default policy and ports"; else fail "macOS: --local-web changed the policy or ports"; fi
out="$(run_fake_claude --local-web --landlock-only)"
if grep -q 'Linux only' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then pass "macOS: --landlock-only is refused"; else fail "macOS: --landlock-only was accepted"; fi

echo "--- bin/claude --minimax arg routing ---"
has_minimax() { grep -qx 'NONO_ARG: --allow-domain' <<<"$1" && grep -qx 'NONO_ARG: api.minimax.io' <<<"$1"; }
out="$(run_fake_claude --minimax)"
if has_minimax "$out"; then pass "bin/claude --minimax allows api.minimax.io for the session"; else fail "bin/claude --minimax did not pass --allow-domain api.minimax.io"; fi
out="$(run_fake_claude)"
if ! has_minimax "$out"; then pass "plain bin/claude does not allow api.minimax.io"; else fail "plain bin/claude leaked --allow-domain api.minimax.io"; fi
out="$(run_fake_claude -- --minimax)"
if ! has_minimax "$out"; then pass "--minimax after -- does not enable MiniMax egress"; else fail "--minimax after -- was wrongly consumed as a wrapper flag"; fi
if ! jq -e '.network.allow_domain[] | strings | select(. == "api.minimax.io")' "$REPO_ROOT/profiles/wmf-engineer.json" >/dev/null 2>&1; then
  pass "api.minimax.io is not in the static profile (opt-in only)"
else
  fail "api.minimax.io is back in the static profile"
fi

echo "--- bin/claude --allow-post arg routing ---"
has_post() { grep -qx 'NONO_ARG: --allow-domain' <<<"$1" && grep -qxF "NONO_ARG: https://$2/**" <<<"$1"; }
out="$(run_fake_claude --allow-post=en.wikipedia.org)"
if has_post "$out" en.wikipedia.org; then pass "--allow-post=en.wikipedia.org opens all methods (host under *.wikipedia.org)"; else fail "--allow-post=en.wikipedia.org did not pass --allow-domain https://en.wikipedia.org/**"; fi
out="$(run_fake_claude '--allow-post=*.wikidata.org,commons.wikimedia.org')"
if has_post "$out" '*.wikidata.org' && has_post "$out" commons.wikimedia.org; then pass "--allow-post takes a comma-separated list, wildcards included"; else fail "--allow-post list did not open both hosts"; fi
out="$(run_fake_claude)"
if ! grep -q 'NONO_ARG: https://' <<<"$out"; then pass "plain bin/claude opens no POST route"; else fail "plain bin/claude leaked an --allow-domain URL"; fi
for bad in example.com api.anthropic.com gerrit.wikimedia.org gitlab.wikimedia.org phabricator.wikimedia.org \
           evil.wikipedia.org.example.com '*.org' 'en.wikipedia.org/**'; do
  if ! run_fake_claude "--allow-post=$bad" | grep -q 'NONO_ARG: https://'; then
    pass "--allow-post=$bad is refused (not a read-only profile host)"
  else
    fail "--allow-post=$bad was accepted"
  fi
done
out="$(run_fake_claude --allow-post)"
if ! grep -q 'NONO_ARG: https://' <<<"$out"; then pass "bare --allow-post is refused"; else fail "bare --allow-post was accepted"; fi

echo "--- bin/claude --local-web arg routing ---"
has_web_ports() {
  grep -qx 'NONO_ARG: 80' <<<"$1" && grep -qx 'NONO_ARG: 443' <<<"$1" && grep -qx 'NONO_ARG: 8080' <<<"$1"
}

out="$(run_fake_claude --local-web)"
if has_web_ports "$out"; then pass "bin/claude --local-web opens 80/443/8080"; else fail "bin/claude --local-web did not open web ports"; fi

out="$(run_fake_claude)"
if ! has_web_ports "$out"; then pass "plain bin/claude does not open web ports"; else fail "plain bin/claude leaked web ports"; fi

# --chrome implies --local-web.
out="$(run_fake_claude --chrome)"
if has_web_ports "$out"; then pass "bin/claude --chrome implies --local-web"; else fail "bin/claude --chrome did not open web ports"; fi

# --local-web after `--` is a claude arg, not a wrapper flag.
out="$(run_fake_claude -- --local-web)"
if ! has_web_ports "$out"; then pass "--local-web after -- does not open web ports"; else fail "--local-web after -- incorrectly opened web ports"; fi

# --local-web=PORT narrows to the given port(s) only.
out="$(run_fake_claude --local-web=8080)"
if grep -qx 'NONO_ARG: 8080' <<<"$out" && ! grep -qx 'NONO_ARG: 443' <<<"$out"; then
  pass "--local-web=8080 opens only 8080"; else fail "--local-web=8080 did not narrow ports"; fi

out="$(run_fake_claude --local-web=80,443)"
if grep -qx 'NONO_ARG: 80' <<<"$out" && grep -qx 'NONO_ARG: 443' <<<"$out" && ! grep -qx 'NONO_ARG: 8080' <<<"$out"; then
  pass "--local-web=80,443 opens 80 and 443 only"; else fail "--local-web=80,443 did not open the listed ports"; fi

# An invalid port value is rejected before reaching nono.
if PATH="$FAKE_REPO/bin:$PATH" bash "$FAKE_REPO/bin/claude" --local-web=abc >/dev/null 2>&1; then
  fail "--local-web=abc was not rejected"; else pass "--local-web=abc is rejected"; fi

echo "--- bin/claude IDE mode ---"
# IDE mode opens the plugin's port, passes --ide, and swaps in a copy of the
# profile with signal_mode allow_all, kept outside the sandbox's grants. It is
# entered from CLAUDE_CODE_SSE_PORT (plugin launch) or --ide (terminal), never
# from a lockfile alone. Lockfiles live in a scratch HOME; a stub lsof reports
# STUB_LSOF_PID (default: this shell) as the listener on STUB_LSOF_LISTENING.
IDE_HOME="$(mktemp -d)"; trap 'rm -rf "$FAKE_REPO" "$IDE_HOME"' EXIT
IDE_RT="$IDE_HOME/.config/wmf-claude/ide-profiles"
mkdir -p "$IDE_HOME/.claude/ide" "$IDE_HOME/bin" "$IDE_HOME/proj/sub" "$IDE_HOME/other" "$IDE_HOME/elsewhere"
cat > "$IDE_HOME/bin/lsof" <<'STUB'
#!/bin/bash
for a in "$@"; do [[ "$a" == -iTCP:* ]] && port="${a#-iTCP:}"; done
[[ " ${STUB_LSOF_LISTENING:-} " == *" $port "* ]] || exit 1
echo "$STUB_LSOF_PID"
STUB
sleep 0 & DEAD_IDE_PID=$!; wait "$DEAD_IDE_PID"
chmod +x "$IDE_HOME/bin/lsof"
ide_lock() {  # $1 port  $2 pid  $3 ideName  $4 workspace folder
  printf '{"pid":%s,"workspaceFolders":["%s"],"ideName":"%s","authToken":"SECRET-TOKEN-%s"}\n' \
    "$2" "$4" "$3" "$1" > "$IDE_HOME/.claude/ide/$1.lock"
}
run_ide_claude() {
  HOME="$IDE_HOME" STUB_LSOF_PID="${STUB_LSOF_PID:-$$}" PATH="$IDE_HOME/bin:$FAKE_REPO/bin:$PATH" \
    bash "$FAKE_REPO/bin/claude" "$@" 2>&1
}
has_ide_port() {  # $1 output  $2 port
  grep -A1 -x 'NONO_ARG: --open-port' <<<"$1" | grep -qx "NONO_ARG: $2"
}

# Plugin launch: the env var alone turns IDE mode on.
ide_lock 60123 $$ PhpStorm "$IDE_HOME/proj"
out="$(CLAUDE_CODE_SSE_PORT=60123 run_ide_claude)"
if has_ide_port "$out" 60123 && grep -qx 'NONO_ARG: --ide' <<<"$out"; then
  pass "CLAUDE_CODE_SSE_PORT opens the IDE port and passes --ide"; else fail "CLAUDE_CODE_SSE_PORT did not enter IDE mode"; fi
if grep -qE "^NONO_ARG: $IDE_RT/wmf-ide-profile\.[0-9]+\.[A-Za-z0-9]+$" <<<"$out" \
   && ! grep -qx "NONO_ARG: $FAKE_REPO/profiles/wmf-engineer.json" <<<"$out"; then
  pass "IDE mode loads a per-session profile copy from ~/.config/wmf-claude"; else fail "IDE mode did not swap the profile"; fi
want_copy="NONO_PROFILE: $(jq -c '.security.signal_mode = "allow_all"' "$FAKE_REPO/profiles/wmf-engineer.json")"
if grep -qxF "$want_copy" <<<"$out"; then
  pass "the profile copy differs from the base only in signal_mode allow_all"; else fail "unexpected profile copy: $(grep '^NONO_PROFILE' <<<"$out")"; fi
ide_copy="$(grep -oE "^NONO_ARG: $IDE_RT/wmf-ide-profile\.[^ ]+" <<<"$out" | head -1)"; ide_copy="${ide_copy#NONO_ARG: }"
if [[ -f "$ide_copy" && "$(ls -l "$ide_copy" | cut -c1-10)" == "-rw-------" \
   && "$(ls -ld "$IDE_RT" | cut -c1-10)" == "drwx------" ]]; then
  pass "the profile copy and its dir are private to the user"; else fail "profile copy or dir not private: $ide_copy"; fi
if grep -qx 'NONO_ENV: CLAUDE_CODE_SSE_PORT=60123' <<<"$out" \
   && grep -qx 'NONO_ENV: WMF_CLAUDE_SESSION=--ide=60123 (signal_mode allow_all)' <<<"$out" \
   && grep -q 'IDE mode: localhost:60123' <<<"$out"; then
  pass "IDE mode is announced and shown in the status line"; else fail "IDE mode notice or status line missing"; fi
out="$(CLAUDE_CODE_SSE_PORT=abc run_ide_claude || true)"
if grep -q 'not a valid TCP port' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then
  pass "an invalid CLAUDE_CODE_SSE_PORT is rejected"; else fail "CLAUDE_CODE_SSE_PORT=abc was not rejected"; fi
# Stale copies of a SIGKILLed session (launcher PID gone, or 0/1) are swept on
# every launch, not only IDE ones.
echo '{}' > "$IDE_RT/wmf-ide-profile.$DEAD_IDE_PID.AbC123"
echo '{}' > "$IDE_RT/wmf-ide-profile.0.AbC123"
echo '{}' > "$IDE_RT/wmf-ide-profile.$$.AbC123"
run_ide_claude >/dev/null
if [[ ! -e "$IDE_RT/wmf-ide-profile.$DEAD_IDE_PID.AbC123" && ! -e "$IDE_RT/wmf-ide-profile.0.AbC123" \
   && ! -e "$ide_copy" ]]; then
  pass "a stale profile copy from a dead session is swept"; else fail "stale profile copy was not swept"; fi
if [[ -e "$IDE_RT/wmf-ide-profile.$$.AbC123" ]]; then
  pass "a live session's profile copy is kept"; else fail "a live session's profile copy was swept"; fi
rm -f "$IDE_RT"/wmf-ide-profile.*

# No trigger: a live lockfile alone changes nothing.
out="$(STUB_LSOF_LISTENING=60123 run_ide_claude)"
if ! grep -qx 'NONO_ARG: --open-port' <<<"$out" && ! grep -qx 'NONO_ARG: --ide' <<<"$out" \
   && grep -qx "NONO_ARG: $FAKE_REPO/profiles/wmf-engineer.json" <<<"$out"; then
  pass "a lockfile alone does not enter IDE mode"; else fail "plain bin/claude guessed an IDE from a lockfile"; fi
# --ide after -- is a Claude Code arg: no port, but say so.
out="$(STUB_LSOF_LISTENING=60123 run_ide_claude -- --ide)"
if ! grep -qx 'NONO_ARG: --open-port' <<<"$out" && grep -q -- '--ide after --' <<<"$out"; then
  pass "--ide after -- does not enter IDE mode and is flagged"; else fail "--ide after -- was mishandled"; fi
# --ide given to Claude Code already is not repeated.
out="$(CLAUDE_CODE_SSE_PORT=60123 run_ide_claude -- --ide)"
if [[ "$(grep -cx 'NONO_ARG: --ide' <<<"$out")" == "1" ]]; then
  pass "--ide is passed to Claude Code once"; else fail "--ide was duplicated or dropped"; fi

# --ide from a terminal: the one live lockfile wins.
out="$(STUB_LSOF_LISTENING=60123 run_ide_claude --ide)"
if has_ide_port "$out" 60123 && grep -qx 'NONO_ENV: CLAUDE_CODE_SSE_PORT=60123' <<<"$out"; then
  pass "--ide picks the single live lockfile and exports its port"; else fail "--ide did not pick the live lockfile"; fi
out="$(STUB_LSOF_LISTENING=60123 run_ide_claude --ide --)"
if has_ide_port "$out" 60123; then pass "--ide -- also enters IDE mode"; else fail "--ide -- did not enter IDE mode"; fi
# Not listening, or a dead pid, is not live.
out="$(STUB_LSOF_LISTENING= run_ide_claude --ide || true)"
if grep -q 'no running IDE plugin' <<<"$out"; then
  pass "--ide skips a lockfile whose port is not listening"; else fail "--ide accepted a non-listening lockfile"; fi
ide_lock 60123 "$DEAD_IDE_PID" PhpStorm "$IDE_HOME/proj"
out="$(STUB_LSOF_LISTENING=60123 STUB_LSOF_PID="$DEAD_IDE_PID" run_ide_claude --ide || true)"
if grep -q 'no running IDE plugin' <<<"$out"; then
  pass "--ide skips a lockfile whose pid is dead"; else fail "--ide accepted a dead-pid lockfile"; fi
# The sandbox can write ~/.claude/ide, so a planted lockfile must not open a port.
rm -f "$IDE_HOME/.claude/ide"/*.lock
ide_lock 3306 0 PhpStorm "/"
out="$(STUB_LSOF_LISTENING=3306 STUB_LSOF_PID=0 run_ide_claude --ide || true)"
if grep -q 'no running IDE plugin' <<<"$out"; then
  pass "--ide skips a lockfile with pid 0"; else fail "--ide accepted a pid-0 lockfile"; fi
ide_lock 3306 $$ PhpStorm "/"
out="$(STUB_LSOF_LISTENING=3306 STUB_LSOF_PID=1 run_ide_claude --ide || true)"
if grep -q 'no running IDE plugin' <<<"$out"; then
  pass "--ide skips a lockfile whose pid is not the port's listener"; else fail "--ide accepted a port another process listens on"; fi
rm -f "$IDE_HOME/.claude/ide"/*.lock
ide_lock 99999 $$ PhpStorm "/"
out="$(STUB_LSOF_LISTENING=99999 run_ide_claude --ide || true)"
if grep -q 'no running IDE plugin' <<<"$out"; then
  pass "--ide skips a lockfile with an out-of-range port"; else fail "--ide accepted port 99999"; fi
rm -f "$IDE_HOME/.claude/ide"/*.lock
# Several live windows: the one whose workspace contains $PWD wins; otherwise
# fail and list them, without the token.
ide_lock 60123 $$ PhpStorm "$IDE_HOME/proj"
ide_lock 60125 $$ 'VS\u001b[2JCode' "$IDE_HOME/other"
out="$(cd "$IDE_HOME/proj/sub" && STUB_LSOF_LISTENING='60123 60125' run_ide_claude --ide)"
if has_ide_port "$out" 60123 && ! has_ide_port "$out" 60125; then
  pass "--ide prefers the window whose workspace contains the cwd"; else fail "--ide did not pick the cwd's window"; fi
out="$(cd "$IDE_HOME/elsewhere" && STUB_LSOF_LISTENING='60123 60125' run_ide_claude --ide || true)"
if grep -q 'found 2 IDE windows' <<<"$out" && grep -q 'port 60123  PhpStorm' <<<"$out" \
   && grep -q 'port 60125  VS?\[2JCode' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then
  pass "--ide lists the candidates, control characters replaced, and stops"; else fail "--ide ambiguity was not reported"; fi
if ! grep -q 'SECRET-TOKEN' <<<"$out"; then
  pass "the candidate list never prints the lockfile's authToken"; else fail "authToken leaked into the error output"; fi

echo "--- bin/claude MCP + profile grants ---"
# MCP grants resolve under the repo; the profile loads by path.
out="$(run_fake_claude)"
if grep -A1 -x "NONO_ARG: --read" <<<"$out" | grep -qx "NONO_ARG: $FAKE_REPO"; then
  pass "repo checkout (incl. MCP servers) granted read-only by path"; else fail "repo read grant missing"; fi
if grep -qx "NONO_ARG: $FAKE_REPO/profiles/wmf-engineer.json" <<<"$out"; then
  pass "profile loaded by path (default wmf-engineer)"; else fail "profile not loaded by path"; fi

# WMF_CLAUDE_PROFILE selects a different profiles/<name>.json.
touch "$FAKE_REPO/profiles/wmf-data-scientist.json"
out="$(WMF_CLAUDE_PROFILE=wmf-data-scientist run_fake_claude)"
if grep -qx "NONO_ARG: $FAKE_REPO/profiles/wmf-data-scientist.json" <<<"$out"; then
  pass "WMF_CLAUDE_PROFILE selects an alternate profile"; else fail "WMF_CLAUDE_PROFILE not honored"; fi
# An unknown profile is rejected before launching.
out="$(WMF_CLAUDE_PROFILE=nope run_fake_claude || true)"
if grep -q "profile 'nope' not found" <<<"$out"; then
  pass "unknown profile is rejected"; else fail "unknown profile not rejected"; fi

echo "--- bin/claude --help ---"
# --help prints wrapper usage and never reaches nono.
out="$(run_fake_claude --help)"
if grep -q 'Wrapper flags:' <<<"$out" && ! grep -q '^NONO_ARG:' <<<"$out"; then
  pass "--help prints wrapper usage without launching"; else fail "--help did not short-circuit"; fi
# Every wrapper flag must be documented in --help.
if grep -q -- '--chrome' <<<"$out" && grep -q -- '--local-web' <<<"$out" && grep -q -- '--docker' <<<"$out" \
   && grep -q -- '--ide' <<<"$out"; then
  pass "--help documents --chrome, --local-web, --docker, and --ide"; else fail "--help is missing a wrapper flag"; fi
# `claude -- --help` is passed through to Claude Code, not intercepted.
out="$(run_fake_claude -- --help)"
if grep -qx 'NONO_ARG: --help' <<<"$out" && ! grep -q 'Wrapper flags:' <<<"$out"; then
  pass "claude -- --help passes through"; else fail "-- --help was wrongly intercepted"; fi

echo "--- bin/claude --docker arg routing ---"
# Managed mode (--docker=SERVICE) starts the real broker, so the fake repo needs
# it on disk plus a stub `docker` (the broker checks `which docker` at startup)
# and a compose file in the launch CWD. The broker binds an ephemeral port; we
# assert bin/claude opened exactly that port and then tore the broker down.
cp "$REPO_ROOT/bin/launch-docker-broker" "$FAKE_REPO/bin/launch-docker-broker"
cp "$REPO_ROOT/bin/mwdocker" "$FAKE_REPO/bin/mwdocker"   # symlinked into the shim dir
chmod +x "$FAKE_REPO/bin/launch-docker-broker" "$FAKE_REPO/bin/mwdocker"
# Stub `docker`: answers the few subcommands bin/claude and the broker use —
# service listing (autodetect/validation), container resolution + inspect
# (privileged-container check). STUB_DOCKER_PRIVILEGED=1 makes inspect report a
# privileged container so the refusal path can be tested.
cat > "$FAKE_REPO/bin/docker" <<'STUB'
#!/bin/bash
case "$*" in
  *"config --services"*) printf 'mediawiki\nmariadb\n' ;;
  *"ps -q"*) echo fakecontainerid ;;
  *"network inspect"*)
    # STUB_DOCKER_NET_OPEN=1 reports a network with a route out, so the
    # --egress refusal path can be tested.
    if [[ -n "${STUB_DOCKER_NET_OPEN:-}" ]]; then
      echo '[{"Internal": false}]'
    else
      echo '[{"Internal": true}]'
    fi ;;
  *inspect*)
    if [[ -n "${STUB_DOCKER_PRIVILEGED:-}" ]]; then
      echo '[{"HostConfig":{"Privileged":true,"Binds":[]},"Mounts":[]}]'
    elif [[ -n "${STUB_DOCKER_CAP:-}" ]]; then
      echo '[{"HostConfig":{"Privileged":false,"CapAdd":["CAP_SYS_ADMIN"],"Binds":[]},"Mounts":[]}]'
    else
      echo '[{"HostConfig":{"Privileged":false,"Binds":[]},"Mounts":[],"NetworkSettings":{"Networks":{"isolated":{}}}}]'
    fi ;;
esac
exit 0
STUB
chmod +x "$FAKE_REPO/bin/docker"
DOCKER_CWD="$(mktemp -d)"
printf 'services:\n  mediawiki:\n    image: x\n  mariadb:\n    image: y\n' > "$DOCKER_CWD/docker-compose.yml"

out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=mediawiki 2>&1)"
# The broker's port is ephemeral, so assert --open-port is present with a
# numeric value, and that the managed-broker note fired.
broker_port="$(grep -A1 -x 'NONO_ARG: --open-port' <<<"$out" | grep -E '^NONO_ARG: [0-9]+$' | head -n1 | grep -oE '[0-9]+')"
if [[ -n "$broker_port" ]] && grep -q 'started a managed Docker broker' <<<"$out"; then
  pass "bin/claude --docker=SERVICE auto-starts a broker and opens its port"
else
  fail "bin/claude --docker=SERVICE did not start a managed broker"
fi
# The egress caveat must be surfaced at opt-in time, not only in SECURITY.md.
if grep -q 'NOT sandbox-restricted' <<<"$out"; then
  pass "--docker warns that the container network is not sandbox-restricted"
else
  fail "--docker did not print the container-egress warning"
fi
# The managed broker must not outlive the session: its per-PID handshake and the
# shim dir are gone, and nothing is left listening on the port it used.
leak=0
for f in "${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"/wmf-docker-broker.*.json \
         "${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"/wmf-docker-shim.*; do
  [[ -e "$f" ]] && leak=1
done
if [[ -n "$broker_port" ]] && command -v lsof >/dev/null 2>&1 \
   && lsof -nP -iTCP:"$broker_port" -sTCP:LISTEN >/dev/null 2>&1; then
  leak=1
fi
if [[ "$leak" == 0 ]]; then
  pass "managed broker + shim dir are torn down when the session exits"
else
  fail "managed broker or shim dir leaked after the session exited"
fi

# --docker=SERVICE:WORKDIR splits correctly: the broker is for 'mediawiki', not
# 'mediawiki:/path' (the note prints the bare service name).
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=mediawiki:/var/www/html/w 2>&1)"
if grep -q "managed Docker broker for 'mediawiki'" <<<"$out"; then
  pass "--docker=SERVICE:WORKDIR splits service from workdir"
else
  fail "--docker=SERVICE:WORKDIR did not split correctly"
fi

# --docker=auto detects the service from the compose file (prefers 'mediawiki').
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=auto 2>&1)"
if grep -q "auto selected service 'mediawiki'" <<<"$out"; then
  pass "--docker=auto detects the service"; else fail "--docker=auto did not detect the service"; fi

# An explicit service that isn't in the compose file fails fast with the list.
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=nope 2>&1 || true)"
if grep -q "not a service" <<<"$out"; then
  pass "unknown --docker=SERVICE is rejected with the available list"; else fail "unknown service not rejected"; fi

# A privileged / socket-mounting container is refused (no opt-out).
out="$(cd "$DOCKER_CWD" && STUB_DOCKER_PRIVILEGED=1 PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=mediawiki 2>&1 || true)"
if grep -qi 'privileged' <<<"$out"; then
  pass "broker refuses a privileged container"; else fail "broker did not refuse a privileged container"; fi

# The check catches host-root capabilities, not just --privileged.
out="$(cd "$DOCKER_CWD" && STUB_DOCKER_CAP=1 PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=mediawiki 2>&1 || true)"
if grep -qi 'SYS_ADMIN' <<<"$out"; then
  pass "broker refuses a container with dangerous capabilities"; else fail "broker did not refuse a SYS_ADMIN container"; fi

# --egress verifies real container state. A non-internal network is refused;
# an internal-only container starts and the launcher reports the posture.
out="$(cd "$DOCKER_CWD" && STUB_DOCKER_NET_OPEN=1 PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=mediawiki --egress=none 2>&1 || true)"
if grep -q 'not network-isolated' <<<"$out"; then
  pass "--egress=none refuses a container with a route out"
else
  fail "--egress=none did not refuse a non-isolated container"
fi
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=mediawiki --egress=none 2>&1)"
if grep -q 'egress=none verified' <<<"$out" && ! grep -q 'NOT sandbox-restricted' <<<"$out"; then
  pass "--egress=none starts against an isolated container and reports it"
else
  fail "--egress=none did not verify an isolated container (got: $out)"
fi

# --egress argument validation happens before anything starts.
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=mediawiki --egress=nope 2>&1 || true)"
if grep -q "takes 'none' or 'allowlist'" <<<"$out"; then
  pass "--egress rejects an unknown mode"; else fail "--egress accepted an unknown mode"; fi
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --egress=none 2>&1 || true)"
if grep -q 'requires --docker=SERVICE' <<<"$out"; then
  pass "--egress without --docker=SERVICE errors clearly"; else fail "--egress without --docker did not error"; fi

# mwdocker against an unreachable broker fails with a diagnostic, not a silent
# set -e abort. (Points at a dead localhost port; curl fails fast.)
out="$(WMF_DOCKER_BROKER_URL=http://127.0.0.1:1 WMF_DOCKER_BROKER_TOKEN=x \
  bash "$FAKE_REPO/bin/mwdocker" composer phpcs 2>&1 || true)"
if grep -q 'could not reach the Docker broker' <<<"$out"; then
  pass "mwdocker reports an unreachable broker clearly"; else fail "mwdocker did not report an unreachable broker"; fi

# mwdocker must forward dash args and a bare "--" without change. Regression
# for two related jq faults: jq parses its own options among the positionals
# after --args (jq 1.8 eats "-c" and "-r", so `mwdocker php -r CODE` lost the
# -r), and jq eats the first bare "--" as its end-of-options marker (so
# `mwdocker npm run lint -- --fix` lost the "--", see !98). Round-trip through
# a real broker with a stub compose command that prints the in-container argv.
BROKER_T="$(mktemp -d)"
cat > "$BROKER_T/fakecompose" <<'STUB'
#!/bin/bash
[[ "$3" == ps ]] && exit 0    # no containers -> the safety check passes
shift 4                       # -f FILE exec -T
[[ "${1:-}" == -w ]] && shift 2
shift                         # service
printf '%s\n' "$@"            # print the in-container argv, one arg per line
STUB
chmod +x "$BROKER_T/fakecompose"
touch "$BROKER_T/docker-compose.yml"
python3 "$REPO_ROOT/bin/launch-docker-broker" \
  --service mediawiki --compose-file "$BROKER_T/docker-compose.yml" \
  --compose-cmd "$BROKER_T/fakecompose" --handshake "$BROKER_T/hs.json" \
  --allow php >"$BROKER_T/broker.log" 2>&1 &
BROKER_PID=$!
for _ in $(seq 50); do [[ -f "$BROKER_T/hs.json" ]] && break; sleep 0.1; done
if [[ -f "$BROKER_T/hs.json" ]]; then
  BROKER_URL="http://127.0.0.1:$(jq -r .port "$BROKER_T/hs.json")"
  # A sandboxed session cannot connect to an ephemeral localhost port, so this
  # round-trip test can only run unsandboxed. Probe /health and skip if blocked.
  if ! curl -fsS --max-time 2 -o /dev/null "$BROKER_URL/health" 2>/dev/null; then
    echo "SKIP: mwdocker dash-arg round-trip (ephemeral localhost port blocked; run unsandboxed)"
  else
    out="$(WMF_DOCKER_BROKER_URL="$BROKER_URL" \
      WMF_DOCKER_BROKER_TOKEN="$(jq -r .token "$BROKER_T/hs.json")" \
      bash "$FAKE_REPO/bin/mwdocker" php -r 'echo "x";' -- --fix 2>&1)"
    if [[ "$out" == php$'\n'-r$'\n''echo "x";'$'\n'--$'\n'--fix ]]; then
      pass "mwdocker forwards dash args and a bare -- intact through a live broker"
    else
      fail "mwdocker mangled dash args or a bare -- (got: $out)"
    fi
  fi
else
  fail "regression-test broker did not start"
  sed 's/^/  /' "$BROKER_T/broker.log" 2>/dev/null || true
fi
kill "$BROKER_PID" 2>/dev/null || true
wait "$BROKER_PID" 2>/dev/null || true
rm -rf "$BROKER_T"

# Artifacts from a SIGKILLed session (launcher PID gone) are swept on next launch.
SWEEP_DIR="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
DEAD_PID=999999   # above macOS's default PID ceiling, so reliably not alive
echo '{"port":1,"token":"x","pid":999998}' > "$SWEEP_DIR/wmf-docker-broker.$DEAD_PID.json"
mkdir -p "$SWEEP_DIR/wmf-docker-shim.$DEAD_PID"
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker=mediawiki 2>&1)"
if [[ ! -e "$SWEEP_DIR/wmf-docker-broker.$DEAD_PID.json" && ! -d "$SWEEP_DIR/wmf-docker-shim.$DEAD_PID" ]]; then
  pass "stale artifacts from a dead session are swept on launch"
else
  fail "stale artifacts were not swept"
  rm -rf "$SWEEP_DIR/wmf-docker-broker.$DEAD_PID.json" "$SWEEP_DIR/wmf-docker-shim.$DEAD_PID"
fi

# Bare --docker (attach mode) with no running broker must error, not hang.
rm -f "${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/wmf-docker-broker.json"
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker 2>&1 || true)"
if grep -q 'no broker handshake' <<<"$out"; then
  pass "bare --docker with no broker errors clearly"; else fail "bare --docker did not error on missing broker"; fi

# WMF_DOCKER_HANDSHAKE moves the attach-mode handshake path (the Lima guest
# launcher runs the broker as another user and publishes the file elsewhere).
out="$(cd "$DOCKER_CWD" && WMF_DOCKER_HANDSHAKE="$DOCKER_CWD/elsewhere.json" PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" --docker 2>&1 || true)"
if grep -q "no broker handshake at $DOCKER_CWD/elsewhere.json" <<<"$out"; then
  pass "WMF_DOCKER_HANDSHAKE selects the attach-mode handshake file"; else fail "WMF_DOCKER_HANDSHAKE was ignored"; fi

# --docker after `--` is a claude arg, not a wrapper flag.
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" -- --docker 2>&1)"
if ! grep -q 'managed Docker broker' <<<"$out" && ! grep -q 'no broker handshake' <<<"$out"; then
  pass "--docker after -- is not treated as a wrapper flag"; else fail "--docker after -- was wrongly consumed"; fi
rm -rf "$DOCKER_CWD"

echo "--- session-start hook: backend-aware context ---"
HOOK="$REPO_ROOT/bin/session-start.sh"

# The default backend is nono. Its output must stay byte for byte what the
# hook emitted before the backend seam, or every nono user's SessionStart
# block changes without notice. The fixtures are copies of that output.
# Unset the selectors, so that the result does not depend on the
# environment of the engineer who runs the test.
FIXTURES="$REPO_ROOT/tests/fixtures/session-start"
if diff -u "$FIXTURES/nono.txt" \
     <(env -u WMF_CLAUDE_SANDBOX_BACKEND -u WMF_DOCKER_BROKER_URL \
         bash "$HOOK" 2>/dev/null) >/dev/null; then
  pass "session-start default output is byte-identical to the nono fixture"
else
  fail "session-start default output differs from tests/fixtures/session-start/nono.txt"
fi
if diff -u "$FIXTURES/nono-broker.txt" \
     <(env -u WMF_CLAUDE_SANDBOX_BACKEND \
         WMF_DOCKER_BROKER_URL=http://127.0.0.1:5000 bash "$HOOK" 2>/dev/null) >/dev/null; then
  pass "session-start default output with a broker is byte-identical to the nono-broker fixture"
else
  fail "session-start broker output differs from tests/fixtures/session-start/nono-broker.txt"
fi

# Fail closed: an unknown backend gets no sandbox text, warns on stderr,
# and still emits the backend-agnostic bullets.
unknown_err="$(mktemp)"
unknown_out="$(WMF_CLAUDE_SANDBOX_BACKEND=definitely-not-a-backend bash "$HOOK" 2>"$unknown_err")"
if ! grep -q 'sandboxed by' <<<"$unknown_out" && grep -q 'stage-hunks' <<<"$unknown_out"; then
  pass "session-start omits the sandbox paragraph for an unknown backend, keeping the rest"
else
  fail "session-start emitted sandbox text for an unknown backend, or dropped the shared bullets"
fi
if grep -q "no sandbox context for backend 'definitely-not-a-backend'" "$unknown_err"; then
  pass "session-start warns on stderr about an unknown backend"
else
  fail "session-start is silent about an unknown backend"
fi
rm -f "$unknown_err"

# A backend must supply both files, or neither.
for d in "$REPO_ROOT"/hooks/context/*/; do
  [[ -d "$d" ]] || continue
  if [[ -r "$d/sandbox.txt" && -r "$d/environment.txt" ]]; then
    pass "backend context complete: $(basename "$d")"
  else
    fail "backend context incomplete: $(basename "$d") is missing sandbox.txt or environment.txt"
  fi
done

# Each file the hook reads must ship in the nono pack.
for f in "$REPO_ROOT"/hooks/context/*/*.txt; do
  rel="${f#"$REPO_ROOT"/}"
  if jq -e --arg p "$rel" 'any(.artifacts[]; .path == $p)' "$REPO_ROOT/package.json" >/dev/null; then
    pass "backend context is a package.json artifact: $rel"
  else
    fail "backend context is missing from package.json artifacts: $rel"
  fi
done

echo "--- session-start hook: Docker broker routing ---"
# The hook is the always-present, repo-agnostic signal that tells Claude to run
# dev tools via mwdocker. It must stay silent unless the broker is attached.
if [[ "$(WMF_DOCKER_BROKER_URL='' bash "$HOOK" 2>/dev/null | grep -c mwdocker)" == "0" ]]; then
  pass "session-start says nothing about mwdocker without a broker"
else
  fail "session-start mentions mwdocker even without a broker"
fi
if WMF_DOCKER_BROKER_URL=http://127.0.0.1:5000 bash "$HOOK" 2>/dev/null \
   | grep -q 'prefixing them with `mwdocker`'; then
  pass "session-start tells Claude to use mwdocker when a broker is attached"
else
  fail "session-start does not route to mwdocker when a broker is attached"
fi

echo ""
echo "--- skill list consistency ---"
# The skill list lives in three places (skills/, package.json artifacts, and
# the session-start hook). It has drifted before. Assert all three agree.
hook_out="$(bash "$HOOK" 2>/dev/null)"
for d in "$REPO_ROOT"/skills/*/; do
  name="$(basename "$d")"
  [[ -f "$d/SKILL.md" ]] || continue
  listed=1
  jq -e --arg p "skills/$name/SKILL.md" \
     'any(.artifacts[]; .path == $p)' "$REPO_ROOT/package.json" >/dev/null || listed=0
  if [[ "$listed" == 1 ]] && grep -q "/wmf-claude:$name" <<<"$hook_out"; then
    pass "skill listed in package.json and session-start: $name"
  else
    fail "skill missing from package.json artifacts or session-start: $name"
  fi
done

echo ""
echo "--- wmf-claude-setup: config.json helpers ---"
# Load only config_get and config_set: the rest of the script installs
# things. Point HOME at a temporary directory.
CFG_HOME="$(mktemp -d)"
cfg_fns="$(awk '/^config_get\(\) \{/,/^}/; /^config_set\(\) \{/,/^}/' "$REPO_ROOT/bin/wmf-claude-setup")"
cfg() {
  HOME="$CFG_HOME" bash -c "
    source '$REPO_ROOT/bin/lib-output.sh'
    WMF_CLAUDE_CONFIG_DIR=\"\$HOME/.config/wmf-claude\"
    WMF_CLAUDE_CONFIG=\"\$WMF_CLAUDE_CONFIG_DIR/config.json\"
    $cfg_fns
    \"\$@\"" cfg "$@"
}
CFG_FILE="$CFG_HOME/.config/wmf-claude/config.json"
if [[ -z "$(cfg config_get phabricatorUsername)" ]]; then
  pass "config_get prints nothing when there is no config file"
else
  fail "config_get printed a value with no config file"
fi
if cfg config_set phabricatorUsername alice >/dev/null \
   && [[ "$(cfg config_get phabricatorUsername)" == "alice" ]]; then
  pass "config_set stores a value that config_get reads back"
else
  fail "config_set/config_get round trip failed"
fi
printf '{"other": 1, "phabricatorUsername": "alice"}' >"$CFG_FILE"
if cfg config_set phabricatorUsername bob >/dev/null \
   && [[ "$(jq -c . "$CFG_FILE")" == '{"other":1,"phabricatorUsername":"bob"}' ]]; then
  pass "config_set keeps the other keys"
else
  fail "config_set dropped or changed other keys: $(cat "$CFG_FILE")"
fi
printf 'not json' >"$CFG_FILE"
if ! cfg config_set phabricatorUsername carol >/dev/null \
   && [[ "$(cat "$CFG_FILE")" == "not json" ]]; then
  pass "config_set does not write over a file that is not valid JSON"
else
  fail "config_set wrote over an invalid JSON file"
fi
if [[ -z "$(cfg config_get phabricatorUsername)" ]]; then
  pass "config_get prints nothing for a file that is not valid JSON"
else
  fail "config_get printed a value from an invalid JSON file"
fi
printf '{"phabricatorUsername": 42}' >"$CFG_FILE"
if [[ -z "$(cfg config_get phabricatorUsername)" ]]; then
  pass "config_get ignores a value that is not a string"
else
  fail "config_get printed a value that is not a string"
fi
rm -rf "$CFG_HOME"

echo ""
echo "--- bin/claude update prompt ---"
# The prompt only fires on a tty, so these drive bin/claude through a real pty.
# What matters is not the happy path but the guards: an install on a feature
# branch or with uncommitted work must never be fast-forwarded out from under
# the engineer. Without coverage those guards can silently invert.
UPD="$(mktemp -d)"
trap 'rm -rf "$FAKE_REPO" "$IDE_HOME" "$UPD"' EXIT

cat > "$UPD/drive.py" <<'DRIVER'
import os, pty, select, sys, time
cwd, answer = sys.argv[1], sys.argv[2]
# Cache dir must live OUTSIDE the checkout: the wrapper writes a fetch stamp
# under it, and an untracked file inside `work` would read as a dirty tree and
# trip the very guard these tests exercise.
env = dict(os.environ, PATH=cwd + "/bin:" + os.environ["PATH"], NO_COLOR="1",
           XDG_CACHE_HOME=os.path.join(os.path.dirname(cwd.rstrip("/")), ".cache"))
# A developer with the documented opt-out exported would otherwise see these
# tests fail rather than run: update_notices returns early on either of these.
env.pop("WMF_CLAUDE_SKIP_UPDATE", None)
env.pop("WMF_CLAUDE_UPDATED", None)
env.pop("WMF_CLAUDE_PROFILE", None)   # fixture only ships wmf-engineer.json
env.pop("WMF_CLAUDE_NO_PAUSE", None)  # the pause after a notice is under test
# Point HOME at a scratch dir: bin/claude reads ~/.claude/settings*.json and
# exits before update_notices if sandbox.enabled is true, which would turn
# every assertion below into an unexplained failure on the developer's machine.
env["HOME"] = os.path.join(os.path.dirname(cwd.rstrip("/")), ".home")
os.makedirs(env["HOME"], exist_ok=True)
pid, fd = pty.fork()
if pid == 0:
    os.chdir(cwd); os.execve("/bin/bash", ["bash", "bin/claude"] + sys.argv[3:], env); os._exit(1)
buf, sent, pressed, deadline = b"", False, 0, time.time() + 60
while time.time() < deadline:
    r, _, _ = select.select([fd], [], [], 0.5)
    if r:
        try: chunk = os.read(fd, 4096)
        except OSError: break
        if not chunk: break
        buf += chunk
        if not sent and b"[y/N]" in buf:
            time.sleep(0.3); os.write(fd, answer.encode() + b"\n"); sent = True
        if buf.count(b"Press Enter to start") > pressed:   # answer "int" sends Ctrl-C instead
            time.sleep(0.2); os.write(fd, b"\x03" if answer == "int" else b"\n"); pressed += 1
    else:
        try:
            if os.waitpid(pid, os.WNOHANG)[0]: break
        except ChildProcessError: break
try: os.waitpid(pid, 0)
except Exception: pass
sys.stdout.write(buf.decode(errors="replace"))
DRIVER

# A fake install: `work` is a clone of `origin.git` that sits two commits behind.
upd_fixture() {
  rm -rf "$UPD/origin.git" "$UPD/work" "$UPD/ahead" "$UPD/.cache"   # .cache: updater markers
  git init -q --bare "$UPD/origin.git"
  git -C "$UPD/origin.git" symbolic-ref HEAD refs/heads/main
  mkdir -p "$UPD/work" && git -C "$UPD/work" init -q -b main .
  git -C "$UPD/work" config user.email t@t && git -C "$UPD/work" config user.name T
  mkdir -p "$UPD/work/bin" "$UPD/work/profiles" "$UPD/work/wiring"
  touch "$UPD/work/profiles/wmf-engineer.json"
  # bin/claude refuses to launch without this; without it every driven launch
  # died right after the prompt and the assertions never noticed.
  echo '{}' > "$UPD/work/wiring/settings-merge.json"
  cp "$REPO_ROOT/bin/claude" "$UPD/work/bin/claude"
  # Prints a marker so tests can tell a launch that reached nono from one that
  # died on a precondition after the prompt.
  printf '#!/bin/bash\necho NONO_LAUNCHED\nfor a in "$@"; do echo "NONO_ARG: $a"; done\nenv | grep "^WMF_CLAUDE_" | sed "s/^/NONO_ENV: /"\n' > "$UPD/work/bin/nono"
  # $1: make setup.sh fail, to exercise the incomplete-update path.
  if [[ "${1:-}" == "failing-setup" ]]; then
    printf '#!/bin/bash\necho SETUP_RAN\nexit 1\n' > "$UPD/work/setup.sh"
  else
    printf '#!/bin/bash\necho SETUP_RAN\n' > "$UPD/work/setup.sh"
  fi
  chmod +x "$UPD/work/bin/nono" "$UPD/work/setup.sh"
  git -C "$UPD/work" add -A && git -C "$UPD/work" commit -qm initial
  git -C "$UPD/work" remote add origin "$UPD/origin.git"
  git -C "$UPD/work" push -q -u origin main
  git clone -q "$UPD/origin.git" "$UPD/ahead"
  git -C "$UPD/ahead" config user.email t@t && git -C "$UPD/ahead" config user.name T
  echo a > "$UPD/ahead/a.txt" && git -C "$UPD/ahead" add -A \
    && git -C "$UPD/ahead" commit -qm "profile: Allow example.wmcloud.org"
  echo b > "$UPD/ahead/b.txt" && git -C "$UPD/ahead" add -A \
    && git -C "$UPD/ahead" commit -qm "wiring: Tighten the find denies"
  git -C "$UPD/ahead" push -q origin main
  git -C "$UPD/work" fetch -q origin
}
upd_head() { git -C "$UPD/work" rev-parse HEAD; }

if ! upd_fixture >/dev/null 2>&1; then
  # Loud, but not a FAIL: a missing/old git is an environment problem, not a
  # defect. CI installs git so this path should never be taken there.
  red "SKIP: bin/claude update prompt — fixture could not be built (git missing or <2.28)"
  red "      The update-prompt guards were NOT exercised in this run."
else
  # Declining must leave the checkout exactly as it was.
  before="$(upd_head)"
  out="$(python3 "$UPD/drive.py" "$UPD/work" n 2>&1)"
  if grep -q 'Update now?' <<<"$out" \
     && grep -q 'profile: Allow example.wmcloud.org' <<<"$out"; then
    pass "update prompt lists the pending commits before asking"
  else
    fail "update prompt did not list pending commits"
  fi
  if [[ "$(upd_head)" == "$before" ]] && ! grep -q SETUP_RAN <<<"$out"; then
    pass "declining the update leaves the checkout untouched"
  else
    fail "declining the update still modified the checkout"
  fi
  if grep -q NONO_LAUNCHED <<<"$out"; then
    pass "the launch proceeds after the prompt is declined"
  else
    fail "the launch died after the prompt (a precondition after update_notices failed)"
  fi
  if ! grep -q 'Press Enter to start' <<<"$out"; then
    pass "no extra pause after the update prompt was answered"
  else
    fail "the launcher paused again after the update prompt was answered"
  fi
  # A notice printed AFTER the prompt was answered must still pause: the reset
  # covers only what was on screen while the prompt waited. server.log as a
  # directory makes the gerrit pre-create fail late in the launch (untracked,
  # so the prompt still fires).
  mkdir -p "$UPD/work/gerrit-mcp-server/gerrit_mcp_server" "$UPD/work/gerrit-mcp-server/server.log"
  touch "$UPD/work/gerrit-mcp-server/gerrit_mcp_server/main.py"
  out="$(python3 "$UPD/drive.py" "$UPD/work" n 2>&1)"
  if grep -q 'Update now?' <<<"$out" && grep -q 'warning: cannot create gerrit-mcp-server/server.log' <<<"$out" \
     && grep -q 'Press Enter to start' <<<"$out" && grep -q NONO_LAUNCHED <<<"$out"; then
    pass "a notice after the answered prompt still pauses"
  else
    fail "a notice printed after the update prompt was answered did not pause"
  fi
  rm -rf "$UPD/work/gerrit-mcp-server"

  # An untracked file must NOT block: Claude Code writes
  # .claude/settings.local.json into any repo where a permission is approved,
  # including this checkout, so treating untracked as dirty disables the
  # prompt permanently on a perfectly updatable install.
  mkdir -p "$UPD/work/.claude"
  echo '{}' > "$UPD/work/.claude/settings.local.json"
  before="$(upd_head)"
  out="$(python3 "$UPD/drive.py" "$UPD/work" n 2>&1)"
  if grep -q 'Update now?' <<<"$out" && [[ "$(upd_head)" == "$before" ]]; then
    pass "untracked files do not block the update prompt"
  else
    fail "untracked files wrongly blocked the update prompt"
  fi
  rm -rf "$UPD/work/.claude"

  # Guard: a tracked modification is real work in progress and must never be
  # fast-forwarded over.
  echo "local edit" >> "$UPD/work/setup.sh"
  before="$(upd_head)"
  out="$(python3 "$UPD/drive.py" "$UPD/work" y 2>&1)"
  if ! grep -q 'Update now?' <<<"$out" \
     && grep -q 'uncommitted changes' <<<"$out" \
     && [[ "$(upd_head)" == "$before" ]]; then
    pass "dirty install is not offered (or given) an update"
  else
    fail "dirty install was offered or given an update"
  fi
  git -C "$UPD/work" checkout -q -- setup.sh

  # Guard: a feature branch is not origin/main and must be left alone.
  git -C "$UPD/work" checkout -q -b feature-branch
  before="$(upd_head)"
  out="$(python3 "$UPD/drive.py" "$UPD/work" y 2>&1)"
  if ! grep -q 'Update now?' <<<"$out" \
     && grep -q 'not on main' <<<"$out" \
     && [[ "$(upd_head)" == "$before" ]]; then
    pass "install on a feature branch is not offered (or given) an update"
  else
    fail "install on a feature branch was offered or given an update"
  fi
  if ! grep -q 'Press Enter to start' <<<"$out"; then
    pass "being behind origin/main alone does not pause (the status line carries it)"
  else
    fail "the behind-origin/main note paused the launch (nags every feature-branch launch)"
  fi
  # A notice that needs action pauses; drive.py sends the key and the launch proceeds.
  out="$(ANTHROPIC_BASE_URL=https://api.minimax.io/v1 python3 "$UPD/drive.py" "$UPD/work" n 2>&1)"
  if grep -q 'Press Enter to start Claude Code' <<<"$out" && grep -q NONO_LAUNCHED <<<"$out"; then
    pass "a startup notice pauses for Enter on a tty, then the launch proceeds"
  else
    fail "a startup notice did not pause on a tty (lost in the redraw), or the launch died"
  fi
  if grep -q 'NONO_ENV: WMF_CLAUDE_UPDATE=2' <<<"$out"; then
    pass "the behind count reaches the status line (WMF_CLAUDE_UPDATE)"
  else
    fail "WMF_CLAUDE_UPDATE not exported for a 2-behind install"
  fi
  # Ctrl-C at the pause ends the launch (with --docker the trap would
  # otherwise tear the broker down and launch anyway).
  out="$(ANTHROPIC_BASE_URL=https://api.minimax.io/v1 python3 "$UPD/drive.py" "$UPD/work" int 2>&1)"
  if grep -q 'Press Enter to start' <<<"$out" && ! grep -q NONO_LAUNCHED <<<"$out"; then
    pass "Ctrl-C at the pause aborts the launch"
  else
    fail "Ctrl-C at the pause fell through to a launch"
  fi
  # `claude -p` never redraws, so a notice prints but nothing pauses.
  out="$(ANTHROPIC_BASE_URL=https://api.minimax.io/v1 python3 "$UPD/drive.py" "$UPD/work" n -p hello 2>&1)"
  if grep -q 'note: ANTHROPIC_BASE_URL' <<<"$out" && ! grep -q 'Press Enter to start' <<<"$out" && grep -q NONO_LAUNCHED <<<"$out"; then
    pass "claude -p from a terminal prints the notice and does not pause"
  else
    fail "claude -p from a terminal paused (or lost the notice)"
  fi
  git -C "$UPD/work" checkout -q main

  # Guard: local commits on main mean merge --ff-only would refuse, so the
  # prompt must not appear and then fail on every launch forever.
  git -C "$UPD/work" checkout -q main
  echo local > "$UPD/work/local.txt"
  git -C "$UPD/work" add -A >/dev/null 2>&1
  git -C "$UPD/work" commit -qm "local tweak" >/dev/null 2>&1
  before="$(upd_head)"
  out="$(python3 "$UPD/drive.py" "$UPD/work" y 2>&1)"
  if ! grep -q 'Update now?' <<<"$out" \
     && grep -q 'diverged from main' <<<"$out" \
     && [[ "$(upd_head)" == "$before" ]]; then
    pass "diverged install is not offered an update it cannot fast-forward"
  else
    fail "diverged install was offered an unusable update"
  fi
  upd_fixture >/dev/null 2>&1   # reset: the local commit broke fast-forwarding

  # Accepting fast-forwards to origin/main and runs setup.sh.
  target="$(git -C "$UPD/work" rev-parse refs/remotes/origin/main)"
  out="$(python3 "$UPD/drive.py" "$UPD/work" y 2>&1)"
  if [[ "$(upd_head)" == "$target" ]] && grep -q SETUP_RAN <<<"$out"; then
    pass "accepting the update fast-forwards and runs setup.sh"
  else
    fail "accepting the update did not fast-forward or did not run setup.sh"
  fi

  # A failure AFTER the merge is the dangerous case: HEAD is already current,
  # so the behind-count is 0 and this notice can never fire again. It has to
  # say so rather than leave a half-updated install behind quietly.
  if ! upd_fixture failing-setup >/dev/null 2>&1; then
    red "SKIP: incomplete-update warning (fixture could not be rebuilt)"
  else
    target="$(git -C "$UPD/work" rev-parse refs/remotes/origin/main)"
    out="$(python3 "$UPD/drive.py" "$UPD/work" y 2>&1)"
    if grep -q 'UPDATE INCOMPLETE' <<<"$out" \
       && grep -q 'repeats until setup.sh succeeds' <<<"$out" \
       && [[ "$(upd_head)" == "$target" ]]; then
      pass "a failure after the merge reports the incomplete update"
    else
      fail "a failure after the merge did not report the incomplete update"
    fi
    # The warning must survive the relaunch: HEAD is now current so no prompt
    # will ever fire again, and the TUI scrolls the one-shot line away.
    out="$(python3 "$UPD/drive.py" "$UPD/work" n 2>&1)"
    if grep -q 'UPDATE INCOMPLETE' <<<"$out" && ! grep -q 'Update now?' <<<"$out"; then
      pass "the incomplete-update warning persists on the next launch"
    else
      fail "the incomplete-update warning was lost after one launch"
    fi
  fi
fi

echo ""
echo "--- Claude Code permission layer ---"
# The tool layer is applied per-launch with `claude --settings`, never written
# into the engineer's ~/.claude/settings.json. That keeps the denies on
# sandboxed wmf-claude sessions and leaves every other Claude Code session on
# the machine alone, so this must not regress to a global install.
out="$(run_fake_claude)"
if grep -qx 'NONO_ARG: --settings' <<<"$out"; then
  pass "bin/claude applies the permission layer with --settings"
else
  fail "bin/claude did not pass --settings (permission layer would be inert)"
fi
if grep -qx "NONO_ARG: $FAKE_REPO/wiring/settings-merge.json" <<<"$out"; then
  pass "bin/claude points --settings at wiring/settings-merge.json"
else
  fail "bin/claude passed --settings with the wrong path"
fi
# Assert the behaviour, not one implementation's identifiers: nothing in the
# install path may write the engineer's global settings file. package.json is
# in scope because its `wiring` block declares exactly that write for the pack.
GLOBAL_WRITERS=""
for f in "$REPO_ROOT"/bin/* "$REPO_ROOT/setup.sh"; do
  [[ -f "$f" ]] || continue
  # Comment lines are dropped first, so a `#` that merely names the path does
  # not trip this; the commands are word-bounded so `backup` or `except` cannot.
  if grep -vE '^[[:space:]]*#' "$f" \
     | grep -qE '((^|[^[:alnum:]_])(cp|mv|tee)([^[:alnum:]_]|$)|>)[^#]*\.claude/settings\.json'; then
    GLOBAL_WRITERS+="${f#"$REPO_ROOT"/} "
  fi
done
# package.json is JSON, so grep by line would miss a pretty-printed directive.
# The plugin-enable merge is expected; the permission layer must NOT be there.
if jq -e '[.wiring[] | select((.file // "") | endswith(".claude/settings.json"))
           | select(.patch == "wiring/settings-merge.json")] | length > 0' \
     "$REPO_ROOT/package.json" >/dev/null 2>&1; then
  GLOBAL_WRITERS+="package.json(wiring) "
fi
if [[ -z "$GLOBAL_WRITERS" ]]; then
  pass "nothing in the install path writes ~/.claude/settings.json"
else
  fail "these still write the engineer's global settings: $GLOBAL_WRITERS"
fi

# Every spelling of the permission bypass is refused; a normal mode still passes.
for bad in --dangerously-skip-permissions --allow-dangerously-skip-permissions --permission-mode=bypassPermissions; do
  out="$(run_fake_claude -- "$bad")"
  if grep -q 'not allowed' <<<"$out" && ! grep -q 'NONO_ARG: run' <<<"$out"; then
    pass "bin/claude refuses $bad"
  else
    fail "bin/claude forwarded $bad to Claude Code"
  fi
done
out="$(run_fake_claude -- --permission-mode bypassPermissions)"
if grep -q 'not allowed' <<<"$out" && ! grep -q 'NONO_ARG: run' <<<"$out"; then
  pass "bin/claude refuses --permission-mode bypassPermissions (two-token form)"
else
  fail "bin/claude forwarded --permission-mode bypassPermissions"
fi
out="$(run_fake_claude -- --permission-mode manual)"
if grep -q 'NONO_ARG: run' <<<"$out"; then
  pass "bin/claude still forwards --permission-mode manual"
else
  fail "bin/claude wrongly refused --permission-mode manual"
fi

echo ""
echo "--- security CLI denied at the tool layer ---"
if jq -e '.permissions.deny | (index("Bash(security:*)") and index("Bash(/usr/bin/security:*)") and index("Bash(git credential*)"))' "$REPO_ROOT/wiring/settings-merge.json" >/dev/null 2>&1; then
  pass "tool layer denies security (both spellings) and git credential helpers"
else
  fail "tool layer does not deny Bash(security:*)"
fi

echo ""
echo "--- MCP server checkouts are read-only ---"
# Read-write would let an agent plant code that runs in every later session.
out="$(run_fake_claude)"
mcp_ok=1
for d in mcp-phabricator gerrit-mcp-server gitlab-mcp-server; do
  if grep -B1 -x "NONO_ARG: $FAKE_REPO/$d" <<<"$out" | grep -qx "NONO_ARG: --allow"; then mcp_ok=0; fi
done
grep -qx "NONO_ARG: --allow-file" <<<"$out" || mcp_ok=0
grep -qx "NONO_ARG: $FAKE_REPO/gerrit-mcp-server/server.log" <<<"$out" || mcp_ok=0
if (( mcp_ok )) && [[ -f "$FAKE_REPO/gerrit-mcp-server/server.log" ]]; then
  pass "MCP server checkouts are read-only; gerrit's server.log is created and granted"
else
  fail "MCP checkouts writable again, or server.log not pre-created for its grant"
fi
# An uninitialised submodule must get neither the file nor the grant.
mv "$FAKE_REPO/gerrit-mcp-server/gerrit_mcp_server/main.py" "$FAKE_REPO/main.py.bak"; rm -f "$FAKE_REPO/gerrit-mcp-server/server.log"
out="$(run_fake_claude)"
if ! grep -qx "NONO_ARG: --allow-file" <<<"$out" && [[ ! -f "$FAKE_REPO/gerrit-mcp-server/server.log" ]]; then
  pass "uninitialised gerrit submodule: no server.log planted, no allow-file grant"
else
  fail "launcher plants server.log into an uninitialised submodule (breaks git submodule update --init)"
fi
mv "$FAKE_REPO/main.py.bak" "$FAKE_REPO/gerrit-mcp-server/gerrit_mcp_server/main.py"

echo ""
echo "--- startup visibility: pause and status line ---"
# Claude Code redraws the terminal as it starts, so launcher output is lost.
# Notices pause for a key (tty only; covered in the pty tests above) and the
# sandbox state is exported for the status line.
if ! grep -q 'WMF_CLAUDE_QUIET\|╭─' "$REPO_ROOT/bin/launch-claude.sh"; then
  pass "launch-claude.sh no longer prints a banner (it was never readable)"
else
  fail "launch-claude.sh still prints a startup banner"
fi

out="$(ANTHROPIC_BASE_URL=https://api.minimax.io/v1 run_fake_claude)"
if grep -q '^note: ANTHROPIC_BASE_URL points at MiniMax' <<<"$out" && ! grep -q 'Press Enter to start' <<<"$out"; then
  pass "a notice on a non-tty launch prints but does not pause"
else
  fail "non-tty launch: notice missing or the launcher paused without a tty"
fi
out="$(run_fake_claude --local-db --minimax)"
if grep -qx "NONO_ENV: WMF_CLAUDE_HOME=$FAKE_REPO" <<<"$out" \
   && grep -qx "NONO_ENV: WMF_CLAUDE_SESSION=--local-db --minimax" <<<"$out" \
   && grep -qx "NONO_ENV: WMF_CLAUDE_PROFILE=wmf-engineer" <<<"$out"; then
  pass "bin/claude exports the session facts for the status line"
else
  fail "bin/claude did not export WMF_CLAUDE_HOME/SESSION/PROFILE"
fi
out="$(run_fake_claude)"
if grep -qx "NONO_ENV: WMF_CLAUDE_SESSION=" <<<"$out"; then
  pass "a plain launch exports an empty session summary"
else
  fail "plain launch: WMF_CLAUDE_SESSION not empty"
fi
# One status-line row: defaults elided, ~ for HOME, grant flags grouped,
# --local-web dropped when --chrome implies it.
out="$(HOME=/home/t run_fake_claude --local-db=3307 --chrome --allow /home/t/src --allow /home/t/.config/x --read /opt/y --allow-net --)"
if grep -qx 'NONO_ENV: WMF_CLAUDE_SESSION=--local-db=3307 --chrome --allow ~/src ~/.config/x --read /opt/y --allow-net' <<<"$out"; then
  pass "session summary is compact (grouped grants, ~ paths, implied flags elided)"
else
  fail "session summary not compact: $(grep '^NONO_ENV: WMF_CLAUDE_SESSION=' <<<"$out")"
fi
out="$(run_fake_claude --local-web=8080 --chrome)"
if grep -qx 'NONO_ENV: WMF_CLAUDE_SESSION=--local-web=8080 --chrome' <<<"$out"; then
  pass "session summary keeps --local-web when its ports were narrowed"
else
  fail "session summary lost a narrowed --local-web: $(grep '^NONO_ENV: WMF_CLAUDE_SESSION=' <<<"$out")"
fi
if jq -e '.environment.allow_vars | index("WMF_CLAUDE_*")' "$REPO_ROOT/profiles/wmf-engineer.json" >/dev/null 2>&1; then
  pass "profile passes WMF_CLAUDE_* into the sandbox (status line needs it)"
else
  fail "profile drops WMF_CLAUDE_*; the status line cannot see the session"
fi
if jq -e '.statusLine.command | test("WMF_CLAUDE_HOME") and test("bin/statusline.sh")' "$REPO_ROOT/wiring/settings-merge.json" >/dev/null 2>&1; then
  pass "settings-merge.json wires statusLine to bin/statusline.sh via WMF_CLAUDE_HOME"
else
  fail "settings-merge.json statusLine missing or not pointing at bin/statusline.sh"
fi
SL="$REPO_ROOT/bin/statusline.sh"
SL_HOME="$(mktemp -d)"; trap 'rm -rf "$FAKE_REPO" "$IDE_HOME" "$UPD" "$SL_HOME"' EXIT
if [[ -x "$SL" ]] && bash -n "$SL"; then pass "bin/statusline.sh is executable and parses"; else fail "bin/statusline.sh missing, not executable, or has a syntax error"; fi
sl_run() {  # $1 stdin json; env from caller
  ( cd "$SL_HOME" && printf '%s' "$1" | HOME="$SL_HOME" WMF_CLAUDE_HOME="$REPO_ROOT" bash "$SL" )
}
out="$(WMF_CLAUDE_PROFILE=wmf-engineer WMF_CLAUDE_SESSION="--local-db=3306 --docker=mediawiki" WMF_CLAUDE_UPDATE= sl_run '{"model":{"display_name":"Opus"}}')"
if [[ "$out" == "WMF nono sandbox · --local-db=3306 --docker=mediawiki" ]]; then
  pass "status line shows the sandbox segment with the session flags"
else
  fail "status line segment wrong: '$out'"
fi
out="$(WMF_CLAUDE_PROFILE=wmf-data-scientist WMF_CLAUDE_SESSION= WMF_CLAUDE_UPDATE=2 sl_run '{}')"
if [[ "$out" == *"(wmf-data-scientist)"* && "$out" == *"update available (2 behind"* ]]; then
  pass "status line names a non-default profile and a pending update"
else
  fail "status line profile/update segment wrong: '$out'"
fi
# The engineer's command arrives as WMF_CLAUDE_STATUSLINE (snapshotted by
# bin/claude at launch), fed the same JSON.
out="$(WMF_CLAUDE_SESSION= WMF_CLAUDE_STATUSLINE='input=$(cat); echo THEIRS-$(printf %s "$input" | jq -r .model.display_name)' sl_run '{"model":{"display_name":"Opus"}}')"
if [[ "$out" == "WMF nono sandbox │ THEIRS-Opus" ]]; then
  pass "status line appends the engineer's own status line, fed the same JSON"
else
  fail "status line did not chain to the engineer's statusLine: '$out'"
fi
out="$(WMF_CLAUDE_SESSION= WMF_CLAUDE_STATUSLINE='echo partial; exit 1' sl_run '{}')"
if [[ "$out" == "WMF nono sandbox │ partial" ]]; then
  pass "status line keeps the engineer's output when their command exits non-zero"
else
  fail "status line dropped output on a non-zero exit: '$out'"
fi
# Three ways a snapshot can lead back here: the merge file's literal, the
# resolved path, and an indirect invocation (stopped by the nested guard).
for self in '"$WMF_CLAUDE_HOME/bin/statusline.sh"' "$REPO_ROOT/bin/statusline.sh" "cd '$REPO_ROOT/bin' && ./statusline.sh"; do
  out="$(WMF_CLAUDE_SESSION= WMF_CLAUDE_STATUSLINE="$self" sl_run '{}')"
  if [[ "$out" == "WMF nono sandbox" ]]; then
    pass "status line does not chain to itself via: $self"
  else
    fail "status line recursed or duplicated via $self: '$out'"
  fi
done
# Settings files inside the sandbox are agent-writable; the status line must
# never take a command from them, only from the launch-time snapshot.
mkdir -p "$SL_HOME/.claude"
for f in settings.json settings.local.json; do
  echo '{"statusLine":{"type":"command","command":"echo PWNED"}}' > "$SL_HOME/.claude/$f"
done
out="$(WMF_CLAUDE_SESSION= WMF_CLAUDE_STATUSLINE= sl_run '{}')"
if [[ "$out" == "WMF nono sandbox" ]]; then
  pass "status line ignores statusLine commands in cwd/HOME settings files (agent-writable)"
else
  fail "status line executed a command from a settings file: '$out'"
fi
rm -rf "$SL_HOME/.claude"
# bin/claude snapshots the user-level statusLine into the env, unsandboxed.
SNAP_HOME="$(mktemp -d)"; trap 'rm -rf "$FAKE_REPO" "$IDE_HOME" "$UPD" "$SL_HOME" "$SNAP_HOME"' EXIT
mkdir -p "$SNAP_HOME/.claude"
echo '{"statusLine":{"type":"command","command":"~/.claude/mine.sh"}}' > "$SNAP_HOME/.claude/settings.json"
out="$(HOME="$SNAP_HOME" run_fake_claude)"
if grep -qx 'NONO_ENV: WMF_CLAUDE_STATUSLINE=~/.claude/mine.sh' <<<"$out"; then
  pass "bin/claude snapshots ~/.claude/settings.json statusLine.command at launch"
else
  fail "bin/claude did not export the engineer's statusLine command: $(grep '^NONO_ENV: WMF_CLAUDE_STATUSLINE' <<<"$out")"
fi
echo '{"statusLine":{"type":"static","command":"nope"}}' > "$SNAP_HOME/.claude/settings.json"
out="$(HOME="$SNAP_HOME" run_fake_claude)"
if grep -qx 'NONO_ENV: WMF_CLAUDE_STATUSLINE=' <<<"$out"; then
  pass "a non-command statusLine is not snapshotted"
else
  fail "non-command statusLine leaked into WMF_CLAUDE_STATUSLINE"
fi

echo "--- Phabricator MCP stays in scraper mode ---"
# Conduit mode sends reads as POST /api/<method>, which the profile refuses.
if grep -qx 'export PHABRICATOR_API_TOKEN=' "$REPO_ROOT/bin/launch-claude.sh"; then
  pass "launch-claude.sh blanks PHABRICATOR_API_TOKEN"
else
  fail "launch-claude.sh does not blank PHABRICATOR_API_TOKEN (a token in .env puts the MCP in Conduit mode)"
fi
# The blank value works only because dotenv does not replace a set variable.
# Check that with the vendored dotenv, when the submodule is built.
if [[ -d "$REPO_ROOT/mcp-phabricator/node_modules/dotenv" ]] && command -v node >/dev/null 2>&1; then
  DOTENV_DIR="$(mktemp -d)"
  echo 'PHABRICATOR_API_TOKEN=api-fake' > "$DOTENV_DIR/.env"
  got="$(cd "$REPO_ROOT/mcp-phabricator" && PHABRICATOR_API_TOKEN= DOTENV_FILE="$DOTENV_DIR/.env" node --input-type=module -e '
    import dotenv from "dotenv";
    dotenv.config({ path: process.env.DOTENV_FILE, quiet: true });
    process.stdout.write(JSON.stringify(process.env.PHABRICATOR_API_TOKEN));' 2>/dev/null)"
  rm -rf "$DOTENV_DIR"
  if [[ "$got" == '""' ]]; then
    pass "dotenv keeps the blank PHABRICATOR_API_TOKEN over the .env value"
  else
    fail "dotenv replaced the blank PHABRICATOR_API_TOKEN with the .env value (got $got)"
  fi
fi
phab_warns() { grep -q '^warning: the phabricator MCP entry in .* sets PHABRICATOR_API_TOKEN' <<<"$1"; }
phab_entry() { printf '{"mcpServers":{"phabricator":{"env":{"PHABRICATOR_API_TOKEN":"%s"}}}}\n' "$1"; }
out="$(HOME="$SNAP_HOME" CLAUDE_CONFIG_DIR= run_fake_claude)"
if ! phab_warns "$out"; then
  pass "no Phabricator token warning without ~/.claude.json"
else
  fail "Phabricator token warning printed without ~/.claude.json"
fi
phab_entry api-fake > "$SNAP_HOME/.claude.json"
out="$(HOME="$SNAP_HOME" CLAUDE_CONFIG_DIR= run_fake_claude)"
if phab_warns "$out"; then
  pass "a token in the user-scope MCP entry gets a warning"
else
  fail "a token in the user-scope MCP entry was not reported"
fi
for v in '' '${PHABRICATOR_API_TOKEN}'; do
  phab_entry "$v" > "$SNAP_HOME/.claude.json"
  out="$(HOME="$SNAP_HOME" CLAUDE_CONFIG_DIR= run_fake_claude)"
  if ! phab_warns "$out"; then
    pass "the value '$v' is not reported as a token"
  else
    fail "the value '$v' was reported as a token"
  fi
done
rm -f "$SNAP_HOME/.claude.json"
CFG_DIR="$SNAP_HOME/alt-config" && mkdir -p "$CFG_DIR"
phab_entry api-fake > "$CFG_DIR/.claude.json"
out="$(HOME="$SNAP_HOME" CLAUDE_CONFIG_DIR="$CFG_DIR" run_fake_claude)"
if phab_warns "$out"; then
  pass "CLAUDE_CONFIG_DIR moves the .claude.json check"
else
  fail "the .claude.json check ignored CLAUDE_CONFIG_DIR"
fi
rm -rf "$CFG_DIR"

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
