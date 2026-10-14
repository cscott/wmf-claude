#!/bin/bash
# Live checks for a contained VM (docs/lima-vm.md#contained-mode) against a real VM: the
# Claude token stays on the host; bin/wmf-claude-model-proxy holds it there, on
# a Unix socket; ssh -R forwards that socket into the guest as 127.0.0.1:PORT;
# Claude Code runs as the agent inside nono (profiles/wmf-contained.json, no egress
# of its own) in bin/claude's contained mode.
#
# Run on the host, outside nono, against a contained VM made from this checkout:
#
#   WMF_CLAUDE_VM=wmf-claude-contained bin/wmf-claude-vm create --contained
#   WMF_CLAUDE_VM=wmf-claude-contained bin/wmf-claude-vm login
#   ./tests/smoke-contained.sh
#
#   WMF_CLAUDE_VM=<name>    Lima instance name (default: wmf-claude-contained)
#   WMF_CLAUDE_MODEL_PROXY_PORT=N   guest port for this script's own bridge (default: 7319)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VM="${WMF_CLAUDE_VM:-wmf-claude-contained}"
PORT="${WMF_CLAUDE_MODEL_PROXY_PORT:-7319}"
(( $# == 0 )) || { echo "usage: $0" >&2; exit 2; }
VMCMD="$REPO_ROOT/bin/wmf-claude-vm"

PASS=0
FAIL=0
red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
pass() { green "PASS: $*"; ((PASS++)); }
fail() { red   "FAIL: $*"; ((FAIL++)); }
info() { printf 'INFO: %s\n' "$*"; }

P=/opt/wmf-claude/profiles/wmf-contained.json
W=/home/agent/work/smoke-contained
AGENT_PATH="/usr/local/bin:/usr/bin:/bin:/home/agent/.local/bin"
KC_SERVICE=wmf-claude-model

g()  { limactl shell --workdir /home/engineer "$VM" -- "$@"; }
ga() { g sudo -H -u agent env -C /home/agent PATH="$AGENT_PATH" "$@"; }
# As the agent, inside nono, as a contained session runs: the profile, the
# forwarded port open, and bin/launch-claude.sh in contained mode setting the
# session environment. A stand-in `claude` in the workspace runs the command
# given, so the checks see exactly what Claude Code would.
sandboxed() {
  ga bash -c 'mkdir -p -m 0700 "/tmp/claude-$(id -u)" && mkdir -p "$1/.smoke-bin" &&
    printf "#!/bin/sh\nexec \"\$@\"\n" > "$1/.smoke-bin/claude" && chmod +x "$1/.smoke-bin/claude"' _ "$W"
  ga env PATH="$W/.smoke-bin:$AGENT_PATH" WMF_CLAUDE_MODEL_PROXY_PORT="$PORT" \
    nono run --silent --allow-cwd --profile "$P" --workdir "$W" --read /opt/wmf-claude --open-port "$PORT" -- \
    /opt/wmf-claude/bin/launch-claude.sh "$@" </dev/null
}

if ! limactl list >/dev/null 2>&1; then red "limactl cannot list instances. Run this outside the nono sandbox."; exit 1; fi
if [[ "$(limactl list --format '{{.Status}}' "$VM" 2>/dev/null)" != Running ]]; then
  red "VM '$VM' is not running. Create it: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm create --contained"
  exit 1
fi
ga test -f "$P" || { red "$P is not in the VM. Run: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm update"; exit 1; }
ga grep -q deny-all.invalid "$P" || { red "the VM has an older wmf-contained profile. Run: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm update"; exit 1; }
ga mkdir -p "$W"
VMDIR=$(limactl list --format '{{.Dir}}' "$VM")
SOCK="$VMDIR/sock/model-smoke.sock"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/wmf-claude/model-proxy-$VM.log"

echo "=== contained VM smoke test: $VM ==="
echo ""
echo "--- contained VM and token ---"
CONTAINED_RECORD="${XDG_CONFIG_HOME:-$HOME/.config}/wmf-claude/vm-$VM-contained"
if [[ -f "$CONTAINED_RECORD" ]]; then pass "the host records $VM as a contained VM"
else fail "$VM is not a contained VM on this host. Recreate it: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm reset --contained"; exit 1; fi
if g test -f /etc/wmf-claude/contained && [[ "$(g stat -c %U /etc/wmf-claude/contained | tr -d '\r')" == root ]]; then pass "the guest marker exists and belongs to root"
else fail "no root-owned /etc/wmf-claude/contained in the guest"; fi
if security find-generic-password -s "$KC_SERVICE" -a "$VM" >/dev/null 2>&1; then pass "token is in the host keychain"
else fail "no token in the host keychain. Store one: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm login"; exit 1; fi
bad=$(ga bash -c 'ls -d /home/agent/.config/wmf-claude/claude-oauth-token /home/agent/.claude/.credentials.json 2>/dev/null;
  grep -rIl "sk-ant-o" /home/agent/.config /home/agent/.claude 2>/dev/null | grep -v projects/ |
    while read -r f; do grep -v -q "sk-ant-oat01-wmf-claude-placeholder" "$f" && echo "$f"; done' | tr -d '\r')
if [[ -z "$bad" ]]; then pass "no token in the agent's config"
else fail "the VM holds token material: $bad. Delete it and revoke that token."; fi
if [[ "$(ga bash -c 'jq -r .hasCompletedOnboarding "$(readlink -f ~/.claude.json)"' 2>/dev/null | tr -d '\r')" == true ]]; then
  pass "first-run onboarding is marked done (no connectivity check)"
else fail "onboarding is not marked done; run: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm login"; fi
mcps=""
for server in phabricator gerrit gitlab; do
  ga bash -c "claude mcp get $server" >/dev/null 2>&1 && mcps+=" $server"
done
if [[ -z "$mcps" ]]; then pass "no MCP servers are registered (no egress for them)"
else fail "MCP servers registered in a contained VM:$mcps"; fi

echo ""
echo "--- the launcher: bin/wmf-claude-vm claude ---"
before=$(wc -l < "$LOG" 2>/dev/null || echo 0)
out=$("$VMCMD" claude smoke-contained -- -p 'Reply with exactly MODEL-OK and nothing else.' 2>&1 | tr -d '\r')
if grep -q 'MODEL-OK' <<<"$out"; then pass "claude -p through bin/wmf-claude-vm (proxy and bridge started and stopped)"
else fail "bin/wmf-claude-vm claude: $(tail -n8 <<<"$out")"; fi
if tail -n +"$((before + 1))" "$LOG" | grep -q '"decision": "allow".*"/v1/messages"'; then pass "the host proxy logged the model call"
else fail "no allowed /v1/messages in the host log"; fi
if ! pgrep -f "wmf-claude-model-proxy.*$VM" >/dev/null && ! ls "$VMDIR"/sock/model-*.sock >/dev/null 2>&1; then
  pass "the session's proxy stopped and removed its socket"
else fail "a model proxy or socket outlived the session"; fi
out=$("$VMCMD" claude smoke-contained --docker=mediawiki 2>&1 | tr -d '\r')
if grep -qE 'not available in a contained VM|contained mode allows no egress' <<<"$out"; then pass "--docker is refused in a contained VM"
else fail "--docker in a contained VM: $(tail -n3 <<<"$out")"; fi
before=$(wc -l < "$LOG")
out=$("$VMCMD" claude smoke-contained -- -p 'Use your WebSearch tool to search for "wikimedia foundation" and report the first result title.' 2>&1 | tr -d '\r')
if tail -n +"$((before + 1))" "$LOG" | grep -q "server tool 'web_search"; then pass "a web search request is refused by the host proxy"
else info "no web_search request reached the proxy (Claude may not have tried): $(tail -n3 <<<"$out")"; fi

echo ""
echo "--- this script's own proxy and bridge, for the checks below ---"
PROXY_PID=""; SSH_PID=""
cleanup() {
  [[ -n "$SSH_PID" ]] && kill "$SSH_PID" 2>/dev/null
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT
python3 -B "$REPO_ROOT/bin/wmf-claude-model-proxy" --socket "$SOCK" --log "$LOG" \
  --keychain-service "$KC_SERVICE" --keychain-account "$VM" </dev/null &
PROXY_PID=$!
for _ in $(seq 50); do [[ -S "$SOCK" ]] && break; sleep 0.1; done
if [[ -S "$SOCK" && "$(stat -f %Lp "$SOCK")" == 600 ]]; then pass "proxy socket is up, mode 0600"
else fail "proxy socket missing or not 0600"; exit 1; fi
SSH_HOST=$(awk '/^Host / {print $2; exit}' "$VMDIR/ssh.config")
ssh -n -F "$VMDIR/ssh.config" -o ControlMaster=no -o ControlPath=none -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -N -R "127.0.0.1:$PORT:$SOCK" "$SSH_HOST" &
SSH_PID=$!
for _ in $(seq 50); do g bash -c "ss -ltnH 'sport = :$PORT' | grep -q ." 2>/dev/null && break; sleep 0.2; done
if g bash -c "ss -ltnH 'sport = :$PORT' | grep -q 127.0.0.1"; then pass "the guest listens on 127.0.0.1:$PORT (ssh -R)"
else fail "no listener on 127.0.0.1:$PORT in the guest"; exit 1; fi
code=$(ga curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/v1/models" | tr -d '\r')
if [[ "$code" == 403 ]]; then pass "the forwarded proxy refuses GET /v1/models (403)"
else fail "GET /v1/models through the bridge gave $code, want 403"; fi

echo ""
echo "--- what the sandbox sees ---"
envout=$(sandboxed sh -c 'printf "BASE=%s\nNOPROXY=%s\nHTTPS_PROXY_SET=%s\nTOKEN=%s\n" "$ANTHROPIC_BASE_URL" "$NO_PROXY" "${HTTPS_PROXY:+yes}" "$CLAUDE_CODE_OAUTH_TOKEN"' | tr -d '\r')
if [[ "$(sed -n 's/^BASE=//p' <<<"$envout")" == "http://127.0.0.1:$PORT" ]]; then pass "ANTHROPIC_BASE_URL is the forwarded port"
else fail "ANTHROPIC_BASE_URL in the sandbox: $(sed -n 's/^BASE=//p' <<<"$envout")"; fi
if [[ "$(sed -n 's/^TOKEN=//p' <<<"$envout")" == sk-ant-oat01-wmf-claude-placeholder ]]; then pass "the sandbox holds only the placeholder token"
else fail "CLAUDE_CODE_OAUTH_TOKEN in the sandbox is not the placeholder"; fi
info "in the sandbox: NO_PROXY=$(sed -n 's/^NOPROXY=//p' <<<"$envout") HTTPS_PROXY set=$(sed -n 's/^HTTPS_PROXY_SET=//p' <<<"$envout")"

echo ""
echo "--- direct egress from the sandbox ---"
code=$(sandboxed curl -s -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models 2>/dev/null | tr -d '\r')
if [[ "$code" != 200 && "$code" != 401 ]]; then pass "direct curl to api.anthropic.com from nono is refused (${code:-no response})"
else fail "direct curl to api.anthropic.com from nono got $code"; fi
code=$(sandboxed curl -s -o /dev/null -w '%{http_code}' https://www.wikipedia.org/ 2>/dev/null | tr -d '\r')
if [[ "$code" != 200 ]]; then pass "curl to an unlisted host from nono is refused (${code:-no response})"
else fail "curl to www.wikipedia.org from nono got 200"; fi

echo ""
echo "--- host proxy log (this run) ---"
tail -n 12 "$LOG"

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
