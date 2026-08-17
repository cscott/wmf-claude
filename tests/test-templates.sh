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
         "$FAKE_REPO/profiles"
cp "$REPO_ROOT/bin/claude" "$FAKE_REPO/bin/claude"
# bin/claude checks the profile file exists before launching.
touch "$FAKE_REPO/profiles/wmf-engineer.json"
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

echo "--- bin/claude MCP + profile grants ---"
# MCP grants resolve under the repo; the profile loads by path.
out="$(run_fake_claude)"
if grep -qx "NONO_ARG: $FAKE_REPO/mcp-phabricator" <<<"$out"; then
  pass "MCP grants point at the repo"; else fail "MCP grant not under the repo"; fi
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
if grep -q -- '--chrome' <<<"$out" && grep -q -- '--local-web' <<<"$out" && grep -q -- '--docker' <<<"$out"; then
  pass "--help documents --chrome, --local-web, and --docker"; else fail "--help is missing a wrapper flag"; fi
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
  *inspect*)
    if [[ -n "${STUB_DOCKER_PRIVILEGED:-}" ]]; then
      echo '[{"HostConfig":{"Privileged":true,"Binds":[]},"Mounts":[]}]'
    elif [[ -n "${STUB_DOCKER_CAP:-}" ]]; then
      echo '[{"HostConfig":{"Privileged":false,"CapAdd":["CAP_SYS_ADMIN"],"Binds":[]},"Mounts":[]}]'
    else
      echo '[{"HostConfig":{"Privileged":false,"Binds":[]},"Mounts":[]}]'
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

# --docker after `--` is a claude arg, not a wrapper flag.
out="$(cd "$DOCKER_CWD" && PATH="$FAKE_REPO/bin:$PATH" \
  bash "$FAKE_REPO/bin/claude" -- --docker 2>&1)"
if ! grep -q 'managed Docker broker' <<<"$out" && ! grep -q 'no broker handshake' <<<"$out"; then
  pass "--docker after -- is not treated as a wrapper flag"; else fail "--docker after -- was wrongly consumed"; fi
rm -rf "$DOCKER_CWD"

echo "--- session-start hook: Docker broker routing ---"
# The hook is the always-present, repo-agnostic signal that tells Claude to run
# dev tools via mwdocker. It must stay silent unless the broker is attached.
if [[ "$(WMF_DOCKER_BROKER_URL='' bash "$REPO_ROOT/bin/session-start.sh" | grep -c mwdocker)" == "0" ]]; then
  pass "session-start says nothing about mwdocker without a broker"
else
  fail "session-start mentions mwdocker even without a broker"
fi
if WMF_DOCKER_BROKER_URL=http://127.0.0.1:5000 bash "$REPO_ROOT/bin/session-start.sh" \
   | grep -q 'prefixing them with `mwdocker`'; then
  pass "session-start tells Claude to use mwdocker when a broker is attached"
else
  fail "session-start does not route to mwdocker when a broker is attached"
fi

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
