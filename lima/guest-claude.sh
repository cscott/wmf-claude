#!/bin/bash
# Start a sandboxed Claude Code session as `agent`, from the engineer side of
# the Lima guest. bin/wmf-claude-vm claude runs this over limactl shell.
#
#   guest-claude.sh NAME [bin/claude args...]
#
# NAME is a workspace under /home/agent/work (see bin/wmf-claude-vm push).
#
# --docker=SERVICE is handled here, not by the agent's bin/claude: the broker
# must run as a user that can reach the Docker socket, and the agent cannot.
# This script starts bin/launch-docker-broker as the engineer for the
# workspace's docker-compose.yml, publishes its handshake in /run/wmf-claude
# (group wmfbroker: engineer and agent), and runs bin/claude as the agent in
# attach mode with WMF_DOCKER_HANDSHAKE set. The broker's port is a localhost
# port, which on Linux needs --landlock-only (nolabs-ai/nono#1786), so that
# flag is added.
set -euo pipefail

# The WMF_CLAUDE_GUEST_* overrides exist for tests/test-lima.sh only.
INSTALL="${WMF_CLAUDE_GUEST_INSTALL:-/opt/wmf-claude}"
AGENT="${WMF_CLAUDE_GUEST_AGENT:-agent}"
AGENT_HOME="${WMF_CLAUDE_GUEST_AGENT_HOME:-/home/$AGENT}"
RUN_DIR="${WMF_CLAUDE_GUEST_RUN_DIR:-/run/wmf-claude}"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
note() { printf 'note: %s\n' "$1" >&2; }

NAME="${1:-}"
[[ -n "$NAME" ]] || die "usage: guest-claude.sh NAME [bin/claude args...]"
shift
[[ "$NAME" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ && "$NAME" != *..* ]] \
  || die "invalid workspace name '$NAME'"
WORK="$AGENT_HOME/work/$NAME"
[[ -d "$WORK" ]] \
  || die "no workspace $WORK; push it first: bin/wmf-claude-vm push <dir> $NAME"

# Take --docker=SERVICE out of the arguments; pass everything else through.
# bin/claude reads its own flags before `--`, so stop looking there.
DOCKER_SERVICE=""
AGENT_ARGS=()
SEEN_SEP=false
for arg in "$@"; do
  if ! $SEEN_SEP; then
    case "$arg" in
      --) SEEN_SEP=true ;;
      --chrome|--ide)
        die "$arg is not available in the VM: it needs the host, which the VM cannot reach" ;;
      --docker=*:*|--docker=auto|--docker|--egress=*)
        die "in the VM use --docker=SERVICE (no auto, :WORKDIR or --egress); see docs/lima-vm.md" ;;
      --docker=*)
        DOCKER_SERVICE="${arg#--docker=}"
        continue ;;
    esac
  fi
  AGENT_ARGS+=("$arg")
done

ENV_ARGS=()
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
  trap 'kill "$BROKER_PID" 2>/dev/null; wait "$BROKER_PID" 2>/dev/null; rm -f "$HS" "$LOG"' EXIT
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
  AGENT_ARGS=(--docker --landlock-only "${AGENT_ARGS[@]}")
  note "Docker broker for '$DOCKER_SERVICE' runs as $(id -un); it stops with the session."
  note "--docker needs a localhost port, so --landlock-only is added (nolabs-ai/nono#1786)."
fi

# Run as the agent, from the workspace. Not in a login shell: the agent's
# ~/.profile and ~/.bashrc would run outside nono. PATH is fixed and puts
# ~/.local/bin (Claude Code) last, so an agent-written file there cannot
# replace nono, git or jq before bin/claude starts the sandbox.
AGENT_PATH="/usr/local/bin:/usr/bin:/bin:$AGENT_HOME/.local/bin"
sudo -H -u "$AGENT" env -C "$WORK" PATH="$AGENT_PATH" "${ENV_ARGS[@]}" \
  "$INSTALL/bin/claude" "${AGENT_ARGS[@]}"
