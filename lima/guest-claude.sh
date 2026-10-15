#!/bin/bash
# Start a sandboxed Claude Code session as `agent`, from the engineer side of
# the Lima guest. bin/wmf-claude-vm claude runs this over limactl shell.
#
#   guest-claude.sh [--model-proxy-port PORT] NAME [bin/claude args...]
#
# NAME is a workspace under /home/agent/work (see bin/wmf-claude-vm push).
#
# In a contained VM (root-owned marker /etc/wmf-claude/contained), bin/wmf-claude-vm runs
# the model API proxy on the host and forwards it to 127.0.0.1:PORT here; the
# session then runs in bin/claude's contained mode (WMF_CLAUDE_MODEL_PROXY_PORT). The marker
# and the flag must agree: a contained VM never starts a session without the proxy,
# and a normal VM never starts one in contained mode.
#
# --chrome is handled here too: the browser must run outside nono, and it
# runs as the browser user (lima/guest-chrome.sh), which the engineer can
# start and the agent cannot. The flag then goes on to bin/claude, which
# opens 9222 and loads the chrome-devtools MCP as on the host.
#
# --docker=SERVICE is handled here, not by the agent's bin/claude: the broker
# must run as a user that can reach the Docker socket, and the agent cannot.
# This script starts bin/launch-docker-broker as the engineer for the
# workspace's docker-compose.yml, publishes its handshake in /run/wmf-claude
# (group wmfbroker: engineer and agent), and runs bin/claude as the agent in
# attach mode with WMF_DOCKER_HANDSHAKE set. bin/claude opens the broker's
# localhost port for the session; nono's full network filter stays on.
set -euo pipefail

# The WMF_CLAUDE_GUEST_* overrides exist for tests/test-lima.sh only.
INSTALL="${WMF_CLAUDE_GUEST_INSTALL:-/opt/wmf-claude}"
AGENT="${WMF_CLAUDE_GUEST_AGENT:-agent}"
AGENT_HOME="${WMF_CLAUDE_GUEST_AGENT_HOME:-/home/$AGENT}"
RUN_DIR="${WMF_CLAUDE_GUEST_RUN_DIR:-/run/wmf-claude}"
CHROME_USER="${WMF_CLAUDE_GUEST_CHROME_USER:-wmfchrome}"
CONTAINED_MARKER="${WMF_CLAUDE_GUEST_CONTAINED_MARKER:-/etc/wmf-claude/contained}"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
note() { printf 'note: %s\n' "$1" >&2; }

PROXY_PORT=""
if [[ "${1:-}" == --model-proxy-port ]]; then
  PROXY_PORT="${2:-}"
  [[ "$PROXY_PORT" =~ ^[0-9]+$ ]] || die "--model-proxy-port needs a port"
  shift 2
fi
if [[ -f "$CONTAINED_MARKER" && -z "$PROXY_PORT" ]]; then
  die "this is a contained VM; start sessions with bin/wmf-claude-vm claude, which runs the model API proxy"
fi
if [[ ! -f "$CONTAINED_MARKER" && -n "$PROXY_PORT" ]]; then
  die "--model-proxy-port given, but this VM is not a contained VM ($CONTAINED_MARKER is missing)"
fi

NAME="${1:-}"
[[ -n "$NAME" ]] || die "usage: guest-claude.sh [--model-proxy-port PORT] NAME [bin/claude args...]"
shift
[[ "$NAME" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ && "$NAME" != *..* ]] \
  || die "invalid workspace name '$NAME'"
WORK="$AGENT_HOME/work/$NAME"
[[ -d "$WORK" ]] \
  || die "no workspace $WORK; push it first: bin/wmf-claude-vm push <dir> $NAME"

# Take --docker=SERVICE out of the arguments, note --chrome, and pass
# everything else through. bin/claude reads its own flags before `--`, so
# stop looking there.
DOCKER_SERVICE=""
WANT_CHROME=false
AGENT_ARGS=()
SEEN_SEP=false
for arg in "$@"; do
  if ! $SEEN_SEP; then
    case "$arg" in
      --) SEEN_SEP=true ;;
      --ide)
        die "$arg is not available in the VM: it needs the host, which the VM cannot reach" ;;
      --chrome) WANT_CHROME=true ;;
      --docker=*:*|--docker=auto|--docker|--docker=|--egress=*)
        die "in the VM use --docker=SERVICE (no auto, :WORKDIR or --egress); see docs/lima-vm.md" ;;
      --docker=*)
        DOCKER_SERVICE="${arg#--docker=}"
        continue ;;
    esac
  fi
  AGENT_ARGS+=("$arg")
done

# The helpers stop with the session: the broker, and the browser (a TERM to
# sudo reaches lima/guest-chrome.sh, which removes its data dir).
BROKER_PID=""; HS=""; LOG=""; CHROME_PID=""; CHROME_LOG=""
stop_helpers() {
  if [[ -n "$CHROME_PID" ]]; then kill "$CHROME_PID" 2>/dev/null; wait "$CHROME_PID" 2>/dev/null; fi
  if [[ -n "$BROKER_PID" ]]; then kill "$BROKER_PID" 2>/dev/null; wait "$BROKER_PID" 2>/dev/null; fi
  rm -f "$HS" "$LOG" "$CHROME_LOG"
}
trap stop_helpers EXIT

# WMF_CLAUDE_IN_VM: the session hook describes the VM (no wiki server unless
# the session starts one). nono passes WMF_CLAUDE_* through.
ENV_ARGS=(WMF_CLAUDE_IN_VM=1)
if [[ -n "$PROXY_PORT" ]]; then
  # The host checked this too; check again here, just before the session.
  # The lock also keeps the containers and the broker from reaching out.
  net=$(sudo -n /usr/local/sbin/wmf-claude-contained-net status 2>/dev/null || echo missing)
  [[ "$net" == locked ]] || die "the VM's network is not locked ($net); run: bin/wmf-claude-vm lock"
  ENV_ARGS+=("WMF_CLAUDE_MODEL_PROXY_PORT=$PROXY_PORT")
fi
if [[ -n "$DOCKER_SERVICE" ]]; then
  COMPOSE="$WORK/docker-compose.yml"
  [[ -f "$COMPOSE" ]] || die "--docker=$DOCKER_SERVICE: no $COMPOSE"
  [[ -d "$RUN_DIR" && -w "$RUN_DIR" ]] \
    || die "$RUN_DIR is missing or not writable; the VM provisioning did not finish"

  # The broker writes the handshake 0600, and the log stays private. The
  # umask applies to the broker only: the agent's session keeps the default.
  HS="$RUN_DIR/broker.$$.json"
  LOG="$RUN_DIR/broker.$$.log"
  (umask 077; exec python3 "$INSTALL/bin/launch-docker-broker" \
    --service "$DOCKER_SERVICE" --compose-file "$COMPOSE" --handshake "$HS" >"$LOG" 2>&1) &
  BROKER_PID=$!
  for _ in $(seq 50); do
    [[ -f "$HS" ]] && break
    kill -0 "$BROKER_PID" 2>/dev/null || break
    sleep 0.1
  done
  if [[ ! -f "$HS" ]]; then
    sed 's/^/  /' "$LOG" >&2
    die "the Docker broker did not start"
  fi
  # The agent is in the directory's group (wmfbroker); let it read the file.
  chmod 0640 "$HS"
  ENV_ARGS+=("WMF_DOCKER_HANDSHAKE=$HS")
  AGENT_ARGS=(--docker "${AGENT_ARGS[@]}")
  note "Docker broker for '$DOCKER_SERVICE' runs as $(id -un); it stops with the session."
fi
if $WANT_CHROME; then
  id -u "$CHROME_USER" >/dev/null 2>&1 || die "no user $CHROME_USER; run bin/wmf-claude-vm update"
  [[ -d "$RUN_DIR" && -w "$RUN_DIR" ]] \
    || die "$RUN_DIR is missing or not writable; the VM provisioning did not finish"
  CHROME_LOG="$RUN_DIR/chrome.$$.log"
  (umask 077; exec sudo -n -u "$CHROME_USER" "$INSTALL/lima/guest-chrome.sh" cdp >"$CHROME_LOG" 2>&1) &
  CHROME_PID=$!
  for _ in $(seq 100); do
    curl -s -o /dev/null http://127.0.0.1:9222/json/version && break
    kill -0 "$CHROME_PID" 2>/dev/null || break
    sleep 0.1
  done
  if ! curl -s -o /dev/null http://127.0.0.1:9222/json/version; then
    sed 's/^/  /' "$CHROME_LOG" >&2
    die "headless Chromium did not start"
  fi
  ENV_ARGS+=("WMF_CLAUDE_CHROME_IN_VM=1")
  AGENT_ARGS=(--chrome "${AGENT_ARGS[@]}")
  note "Headless Chromium for the session runs as $CHROME_USER, loopback only; it stops with the session."
fi

# Run as the agent, from the workspace. Not in a login shell: the agent's
# ~/.profile and ~/.bashrc would run outside nono. PATH is fixed and puts
# ~/.local/bin (Claude Code) last, so an agent-written file there cannot
# replace nono, git or jq before bin/claude starts the sandbox.
AGENT_PATH="/usr/local/bin:/usr/bin:/bin:$AGENT_HOME/.local/bin"
sudo -H -u "$AGENT" env -C "$WORK" PATH="$AGENT_PATH" "${ENV_ARGS[@]}" \
  "$INSTALL/bin/claude" "${AGENT_ARGS[@]}"
