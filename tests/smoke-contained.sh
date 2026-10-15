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
#   ./tests/smoke-contained.sh --reboot       # first stop and start the VM: the
#     lock must load at boot, before the network
#   ./tests/smoke-contained.sh --docker       # also the Docker broker: prepare a
#     php:8.3-cli container while open (the host firewall rule must be off
#     for the image pull), lock, then check the container has no egress and
#     a session reaches it with mwdocker
#   ./tests/smoke-contained.sh --host-block   # also prove the host-side block
#     (LuLu blocking limactl, docs/lima-vm.md#contained-mode): remove the VM's own
#     lock for a moment, as root, and check that egress still fails
#
#   WMF_CLAUDE_VM=<name>    Lima instance name (default: wmf-claude-contained)
#   WMF_CLAUDE_MODEL_PROXY_PORT=N   guest port for this script's own bridge (default: 7319)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VM="${WMF_CLAUDE_VM:-wmf-claude-contained}"
PORT="${WMF_CLAUDE_MODEL_PROXY_PORT:-7319}"
HOST_BLOCK=false
REBOOT=false
WITH_DOCKER=false
for a in "$@"; do
  case "$a" in
    --host-block) HOST_BLOCK=true ;;
    --reboot) REBOOT=true ;;
    --docker) WITH_DOCKER=true ;;
    *) echo "usage: $0 [--reboot] [--host-block] [--docker]" >&2; exit 2 ;;
  esac
done
VMCMD="$REPO_ROOT/bin/wmf-claude-vm"

PASS=0
FAIL=0
red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
# Not ((PASS++)): it returns false when the count is 0.
pass() { green "PASS: $*"; PASS=$((PASS + 1)); }
fail() { red   "FAIL: $*"; FAIL=$((FAIL + 1)); }
info() { printf 'INFO: %s\n' "$*"; }

P=/opt/wmf-claude/profiles/wmf-contained.json
W=/home/agent/work/smoke-contained
AGENT_PATH="/usr/local/bin:/usr/bin:/bin:/home/agent/.local/bin"
MODEL_TOKEN_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/wmf-claude/vm-$VM-model-token"

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
if $REBOOT; then
  echo "--- boot: stop and start the VM ---"
  if limactl stop "$VM" >/dev/null 2>&1 && limactl start --tty=false "$VM" >/dev/null 2>&1; then
    pass "the VM stopped and started (readiness probe passed)"
  else
    fail "the VM did not restart"; exit 1
  fi
  if [[ "$(g systemctl is-active wmf-claude-contained-net.service | tr -d '\r')" == active ]]; then pass "wmf-claude-contained-net.service ran at boot"
  else fail "wmf-claude-contained-net.service is not active after boot"; fi
  if g sudo -n nft list table inet wmf_claude_contained_lock 2>/dev/null | grep -q 'policy drop'; then pass "the lock table is loaded after boot"
  else fail "no lock table after boot"; fi
  echo ""
fi
echo "--- contained VM and token ---"
CONTAINED_RECORD="${XDG_CONFIG_HOME:-$HOME/.config}/wmf-claude/vm-$VM-contained"
if [[ -f "$CONTAINED_RECORD" ]]; then pass "the host records $VM as a contained VM"
else fail "$VM is not a contained VM on this host. Recreate it: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm reset --contained"; exit 1; fi
if g test -f /etc/wmf-claude/contained && [[ "$(g stat -c %U /etc/wmf-claude/contained | tr -d '\r')" == root ]]; then pass "the guest marker exists and belongs to root"
else fail "no root-owned /etc/wmf-claude/contained in the guest"; fi
if [[ -s "$MODEL_TOKEN_FILE" && "$(stat -f '%Lp' "$MODEL_TOKEN_FILE" 2>/dev/null || stat -c '%a' "$MODEL_TOKEN_FILE" 2>/dev/null)" == 600 ]]; then
  pass "token is in a 0600 host file ($MODEL_TOKEN_FILE), not the keychain"
else fail "no 0600 token file at $MODEL_TOKEN_FILE. Store one: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm login"; exit 1; fi
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
echo "--- the VM's network: locked ---"
NETCMD=/usr/local/sbin/wmf-claude-contained-net
net=$(g sudo -n "$NETCMD" status 2>&1 | tr -d '\r')
if [[ "$net" == locked ]]; then pass "the VM's network is locked"
else fail "the VM's network is '$net', not locked. Run: WMF_CLAUDE_VM=$VM bin/wmf-claude-vm lock"; exit 1; fi
# Egress for each user, outside nono. The lock is root's, so root too. The
# address comes from the host, so a DNS failure in the VM cannot pass this.
EGRESS_IP=$(python3 -c 'import socket; print(socket.gethostbyname("deb.debian.org"))')
# limactl shell gives the command a terminal, so its stderr arrives on stdout.
egress() { "$@" curl -sS -m 8 -o /dev/null --resolve "deb.debian.org:443:$EGRESS_IP" https://deb.debian.org/ >/dev/null 2>&1; }
for who in engineer agent root; do
  case $who in engineer) run=(g) ;; agent) run=(ga) ;; root) run=(g sudo -n) ;; esac
  if egress "${run[@]}"; then fail "$who: HTTPS out of the VM works"
  else pass "$who: no HTTPS out of the VM (by IP, $EGRESS_IP)"; fi
done
g sudo -n resolvectl flush-caches 2>/dev/null
if g getent hosts example.org >/dev/null 2>&1; then fail "DNS lookups leave the VM"
else pass "no DNS out of the VM"; fi

host_block_check() {
  local out
  out=$("$VMCMD" check-host-block 2>&1 | tr -d '\r')
  if grep -q "the host blocks the VM's egress without its lock" <<<"$out"; then
    pass "with the VM's lock off, the host still blocks the VM's egress"
  else
    fail "check-host-block: $(tail -n4 <<<"$out")"
  fi
  info "$(grep -m1 'DNS from the VM' <<<"$out" | sed 's/^ *//')"
  if [[ "$(g sudo -n "$NETCMD" status | tr -d '\r')" == locked ]]; then pass "the VM's lock is back"
  else fail "the VM's lock was not restored"; fi
}
RELOCK=false
if $HOST_BLOCK; then
  echo ""
  echo "--- the host-side block (--host-block: bin/wmf-claude-vm check-host-block) ---"
  host_block_check
else
  info "host-side block not checked (run with --host-block once LuLu blocks limactl)"
fi

echo ""
echo "--- the launcher: bin/wmf-claude-vm claude ---"
before=0; [[ -f "$LOG" ]] && before=$(wc -l < "$LOG")
out=$("$VMCMD" claude smoke-contained -- -p 'Reply with exactly MODEL-OK and nothing else.' 2>&1 | tr -d '\r')
if grep -q 'MODEL-OK' <<<"$out"; then pass "claude -p through bin/wmf-claude-vm (proxy and bridge started and stopped)"
else fail "bin/wmf-claude-vm claude: $(tail -n8 <<<"$out")"; fi
if tail -n +"$((before + 1))" "$LOG" | grep -q '"decision": "allow".*"/v1/messages"'; then pass "the host proxy logged the model call"
else fail "no allowed /v1/messages in the host log"; fi
if ! pgrep -f "wmf-claude-model-proxy.*$VM" >/dev/null && ! ls "$VMDIR"/sock/model-*.sock >/dev/null 2>&1; then
  pass "the session's proxy stopped and removed its socket"
else fail "a model proxy or socket outlived the session"; fi
before=$(wc -l < "$LOG")
out=$("$VMCMD" claude smoke-contained -- -p 'Use your WebSearch tool to search for "wikimedia foundation" and report the first result title.' 2>&1 | tr -d '\r')
if tail -n +"$((before + 1))" "$LOG" | grep -q "server tool 'web_search"; then pass "a web search request is refused by the host proxy"
else info "no web_search request reached the proxy (Claude may not have tried): $(tail -n3 <<<"$out")"; fi

echo ""
echo "--- this script's own proxy and bridge, for the checks below ---"
PROXY_PID=""; SSH_PID=""
DNAME=smoke-contained-docker
DSRC=""
cleanup() {
  if $RELOCK; then g sudo -n "$NETCMD" lock >/dev/null; fi
  if [[ -n "$DSRC" ]]; then
    g bash -c 'cd "$1" 2>/dev/null && docker compose down -t 1 >/dev/null 2>&1' _ "/home/agent/work/$DNAME"
    ga rm -rf "/home/agent/work/$DNAME" 2>/dev/null
    rm -rf "$DSRC"
    WSF="${XDG_CONFIG_HOME:-$HOME/.config}/wmf-claude/vm-$VM-workspaces"
    [[ -f "$WSF" ]] && awk -F'\t' -v d="$DNAME" '$1 != d' "$WSF" > "$WSF.new" && mv "$WSF.new" "$WSF"
  fi
  [[ -n "$SSH_PID" ]] && kill "$SSH_PID" 2>/dev/null
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT
python3 -B "$REPO_ROOT/bin/wmf-claude-model-proxy" --socket "$SOCK" --log "$LOG" \
  --token-file "$MODEL_TOKEN_FILE" </dev/null &
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

if $WITH_DOCKER; then
  echo ""
  echo "--- --docker: prepare while open, test while locked ---"
  DSRC=$(mktemp -d)
  git -C "$DSRC" init -q -b main
  cat > "$DSRC/docker-compose.yml" <<'COMPOSE'
services:
  mediawiki:
    image: php:8.3-cli
    command: sleep infinity
    user: "${MW_DOCKER_UID}:${MW_DOCKER_GID}"
    working_dir: /var/www/html/w
    volumes:
      - ./:/var/www/html/w
COMPOSE
  git -C "$DSRC" add docker-compose.yml
  git -C "$DSRC" -c user.name=smoke -c user.email=smoke@invalid commit -q -m "smoke: compose"
  DW="/home/agent/work/$DNAME"
  "$VMCMD" push "$DSRC" "$DNAME" >/dev/null 2>&1 || fail "push the compose project"
  ga bash -c 'printf "MW_DOCKER_UID=%s\nMW_DOCKER_GID=%s\n" "$(id -u)" "$(id -g)" > "$1/.env"' _ "$DW"
  # This script's own proxy counts as a running session, and unlock and lock
  # refuse during one. Stop it; the launcher checks below start their own.
  [[ -n "$SSH_PID" ]] && kill "$SSH_PID" 2>/dev/null
  [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null
  wait 2>/dev/null; SSH_PID=""; PROXY_PID=""
  # Prepare: the image pull needs egress. The EXIT trap locks again if this
  # stops here.
  RELOCK=true
  if ! out=$("$VMCMD" unlock 2>&1); then fail "unlock: $(tail -n2 <<<"$out")"; fi
  if out=$(g bash -c 'cd "$1" && docker compose up -d --quiet-pull 2>&1' _ "$DW" | tr -d '\r'); then
    pass "prepare: the container started while the VM was open"
  else
    fail "prepare: docker compose up failed while open (is a host firewall rule on limactl still on?): $(tail -n3 <<<"$out")"
  fi
  if ! out=$("$VMCMD" lock 2>&1); then fail "lock: $(tail -n2 <<<"$out")"; fi
  RELOCK=false
  if [[ "$(g sudo -n "$NETCMD" status | tr -d '\r')" == locked ]]; then pass "locked again after the prepare step"
  else fail "the VM did not lock again"; exit 1; fi
  # Locked: a raw TCP connect by IP from inside the container, so DNS cannot
  # decide it.
  cid=$(g bash -c 'cd "$1" && docker compose ps -q mediawiki' _ "$DW" | tr -d '\r')
  r=$(g docker exec "$cid" php -r '$f = @fsockopen($argv[1], 443, $e, $s, 5); echo $f ? "OPEN" : "BLOCKED";' -- "$EGRESS_IP" 2>&1 | tr -d '\r')
  if [[ "$r" == BLOCKED ]]; then pass "locked: the container cannot reach the internet (TCP to $EGRESS_IP:443)"
  else fail "locked: the container reached the internet: $r"; fi
  # The session's path, without the model: a broker as the engineer, and
  # mwdocker as the agent inside nono with the wmf-contained profile.
  BROKER_CHECKS='
    W=$1; ip=$2; HS=/run/wmf-claude/smoke.$$.json; LOG=/run/wmf-claude/smoke.$$.log
    (umask 077; exec python3 /opt/wmf-claude/bin/launch-docker-broker --service mediawiki \
      --compose-file "$W/docker-compose.yml" --handshake "$HS" >"$LOG" 2>&1) &
    bp=$!
    for _ in $(seq 100); do [ -f "$HS" ] && break; sleep 0.1; done
    if [ ! -f "$HS" ]; then echo "BROKER=failed: $(tr "\n" " " < "$LOG")"; kill "$bp" 2>/dev/null; rm -f "$LOG"; exit 0; fi
    port=$(jq -r .port "$HS"); tok=$(jq -r .token "$HS")
    sudo -H -u agent env -C "$W" PATH=/usr/local/bin:/usr/bin:/bin \
      WMF_DOCKER_BROKER_URL="http://127.0.0.1:$port" WMF_DOCKER_BROKER_TOKEN="$tok" \
      nono run --silent --profile /opt/wmf-claude/profiles/wmf-contained.json --workdir "$W" \
      --read /opt/wmf-claude --open-port "$port" -- sh -c '\''
        echo "PHP=$(/opt/wmf-claude/bin/mwdocker php -r "echo PHP_VERSION;" 2>&1 | tail -n1)"
        echo "EGRESS=$(/opt/wmf-claude/bin/mwdocker php -r "\$f = @fsockopen(\$argv[1], 443, \$e, \$s, 5); echo \$f ? \"OPEN\" : \"BLOCKED\";" -- "$1" 2>&1 | tail -n1)"
      '\'' _ "$ip"
    kill "$bp" 2>/dev/null; wait "$bp" 2>/dev/null; rm -f "$HS" "$LOG"
  '
  out=$(g bash -c "$BROKER_CHECKS" _ "$DW" "$EGRESS_IP" | tr -d '\r')
  if [[ "$(sed -n 's/^PHP=//p' <<<"$out")" =~ ^8\.3\. ]]; then pass "mwdocker in the contained sandbox reaches the container"
  else fail "mwdocker in the contained sandbox: $out"; fi
  if [[ "$(sed -n 's/^EGRESS=//p' <<<"$out")" == BLOCKED ]]; then pass "code run through mwdocker cannot reach the internet"
  else fail "code run through mwdocker: $(sed -n 's/^EGRESS=//p' <<<"$out")"; fi
  # The full path: lima/guest-claude.sh starts the broker, bin/claude runs in
  # contained mode, and Claude calls mwdocker.
  out=$("$VMCMD" claude "$DNAME" --docker=mediawiki -- -p \
    "Run this exact shell command and reply with only its output: mwdocker php -r 'echo \"SMOKE-\", PHP_VERSION;'" \
    --allowedTools 'Bash(mwdocker:*)' 2>&1 | tr -d '\r')
  if grep -qE 'SMOKE-8\.3\.' <<<"$out"; then pass "a contained session with --docker runs mwdocker"
  else fail "contained session with --docker: $(tail -n4 <<<"$out")"; fi
fi

echo ""
echo "--- host proxy log (this run) ---"
tail -n 12 "$LOG"

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
