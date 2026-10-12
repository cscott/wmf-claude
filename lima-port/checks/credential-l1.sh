#!/usr/bin/env bash
# Track L, step L1 (HANDOFF-LIMA.md): send a sandbox's Claude Code through
# the host credential proxy (credential-proxy.py), and log which requests
# it makes. The real credential stays on the host; the VM gets a phantom.
#
#   lima-port/checks/credential-l1.sh MODE [NAME]
#
# MODE is where the proxy gets the real credential:
#   apikey  ANTHROPIC_API_KEY in this shell's environment
#   token   ~/.config/wmf-sbx/claude-oauth-token (from `claude setup-token`)
#   live    ~/.claude/.credentials.json, the host's own Claude Code login
#           (Linux; on macOS the login is in the keychain, so not this mode)
# NAME is a sandbox (default: hw-check, from real-hardware.sh create).
#
# For each MODE it runs, as the agent in the VM:
#   1. outside nono: claude -p, one turn with no tools;
#   2. outside nono: claude -p, one turn that uses a tool (Bash);
#   3. in a session (`wmf-sbx resume`, so bin/claude and nono): the same;
#   4. apikey only: option N, nono's `--credential anthropic` in the VM,
#      without the host proxy, for comparison.
# The proxy log (one line per request: method, path, status, bytes,
# seconds, auth headers in) goes to ./credential-l1-logs/MODE.log. Send
# me the log files. They hold no credential and no request body.
#
# The script opens the proxy port in the VM's nftables host block until
# the VM reboots (`wmf-sbx stop` and `start` close it again).
set -uo pipefail

MODE=${1:?usage: credential-l1.sh apikey|token|live [NAME]}
NAME=${2:-hw-check}
WMF_SBX=${WMF_SBX:-wmf-sbx}
HERE=$(cd "$(dirname "$0")" && pwd)
LOGS=${WMF_SBX_CHECK_LOGS:-credential-l1-logs}
PORT=${WMF_SBX_L1_PORT:-18765}
PHANTOM=wmfsbx-phantom-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
mkdir -p "$LOGS"
LOG=$LOGS/$MODE.log
: > "$LOG"

case $MODE in
  apikey) [[ -n ${ANTHROPIC_API_KEY:-} ]] || { echo "ANTHROPIC_API_KEY is not set" >&2; exit 2; }
          PROXY_ARGS=(); PHANTOM_ENV=ANTHROPIC_API_KEY ;;
  token)  PROXY_ARGS=(--oauth-file "$HOME/.config/wmf-sbx/claude-oauth-token")
          unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN; PHANTOM_ENV=CLAUDE_CODE_OAUTH_TOKEN ;;
  live)   PROXY_ARGS=(--claude-credentials "$HOME/.claude/.credentials.json")
          unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN; PHANTOM_ENV=CLAUDE_CODE_OAUTH_TOKEN ;;
  *) echo "unknown MODE $MODE" >&2; exit 2 ;;
esac

python3 "$HERE/credential-proxy.py" --port "$PORT" --phantom "$PHANTOM" \
  ${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"} > /dev/null 2>> "$LOG" &
PROXY=$!
trap 'kill $PROXY 2>/dev/null' EXIT
sleep 1
kill -0 $PROXY 2>/dev/null || { echo "the proxy did not start:" >&2; cat "$LOG" >&2; exit 1; }

# The guest reaches the host's loopback at 192.168.5.2 (Lima's user network).
limactl shell "wmf-sbx-$NAME" sudo sh -c "
  for c in block agent_egress; do
    nft insert rule inet wmf_sbx_hostblock \$c ip daddr 192.168.5.2 tcp dport $PORT accept
  done"

BASE=http://192.168.5.2:$PORT
ENVS=(ANTHROPIC_BASE_URL=$BASE "$PHANTOM_ENV=$PHANTOM"
      NO_PROXY=192.168.5.2 no_proxy=192.168.5.2)

step() {  # LABEL CMD...
  echo "== $1" | tee -a "$LOG"; shift
  local t0=$SECONDS
  "$@" < /dev/null 2>&1 | grep -v '^time=' | tail -4 | sed 's/^/   | /'
  echo "   rc ${PIPESTATUS[0]}, $((SECONDS - t0)) s" | tee -a "$LOG"
}
PING=(-p "Reply with the word pong." --model haiku)
TOOL=(-p "Run: echo l1-tool-ok. Then reply with its output." --model haiku --allowedTools Bash)

step "1 no tools, outside nono" "$WMF_SBX" exec "$NAME" -- env "${ENVS[@]}" claude "${PING[@]}"
step "2 one tool call, outside nono" "$WMF_SBX" exec "$NAME" -- env "${ENVS[@]}" claude "${TOOL[@]}"

# 3: a real session (bin/claude, nono --landlock-only). The base URL goes in
# the agent's settings env for the run; resume passes the phantom as the
# credential; --open-port lets the session reach the proxy port.
"$WMF_SBX" exec "$NAME" -- bash -c '
  cp ~/.claude/settings.json ~/.claude/settings.json.l1bak &&
  jq --arg u "$1" ".env += {ANTHROPIC_BASE_URL: \$u, NO_PROXY: \"192.168.5.2\", no_proxy: \"192.168.5.2\"}" \
    ~/.claude/settings.json.l1bak > ~/.claude/settings.json' _ "$BASE"
step "3 one tool call, in a session (nono)" \
  env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN "$PHANTOM_ENV=$PHANTOM" \
  "$WMF_SBX" resume "$NAME" --open-port="$PORT" -- "${TOOL[@]}"
"$WMF_SBX" exec "$NAME" -- bash -c 'mv ~/.claude/settings.json.l1bak ~/.claude/settings.json'

# 4 (apikey only): option N, nono's own credential proxy in the VM. The
# real key goes into the VM, as with resume today; nono should hide it
# from the agent. Want: base= a localhost URL, and a phantom key (not
# sk-ant-...). This step does not use the host proxy.
if [[ $MODE == apikey ]]; then
  step "4 option N: nono --credential anthropic (in the VM)" \
    "$WMF_SBX" resume "$NAME" --credential=anthropic -- -p \
    'Run this exact command and reply with its output verbatim: echo "base=$ANTHROPIC_BASE_URL key-is-real=$(case $ANTHROPIC_API_KEY in sk-ant-*) echo yes;; *) echo no;; esac) len=${#ANTHROPIC_API_KEY}"' \
    --model haiku --allowedTools Bash
fi

kill $PROXY 2>/dev/null
echo "== proxy log: $LOG"
cat "$LOG"
