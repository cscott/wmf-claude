#!/usr/bin/env bash
# Track L, step L6 (HANDOFF-LIMA.md): measure the shared login of the auth
# proxy (`create --auth-proxy`, authproxy.py). Every auth-proxy sandbox's
# egress proxy uses one nono capture store, and `resume` copies the
# phantoms between the sandboxes. Run it on your host, from this checkout.
#
#   lima-port/checks/oauth-l6.sh create            # l6-a, l6-b, and l6-c (--sudo)
#   lima-port/checks/oauth-l6.sh login l6-a        # interactive: /login, one message, /exit
#   lima-port/checks/oauth-l6.sh use l6-b          # a turn, no /login (the shared login)
#   lima-port/checks/oauth-l6.sh use l6-c          # the same in the sudo sandbox
#   lima-port/checks/oauth-l6.sh rotation l6-a l6-b  # refresh in a, then in b with the old phantoms
#   lima-port/checks/oauth-l6.sh race l6-a l6-b    # both refresh at once
#   lima-port/checks/oauth-l6.sh rc l6-a           # interactive: Remote Control, /status, /usage
#   lima-port/checks/oauth-l6.sh scan l6-a         # real tokens in the guest? (want 0)
#   lima-port/checks/oauth-l6.sh long l6-a l6-b [H]  # two sessions for H hours (default 9)
#   lima-port/checks/oauth-l6.sh rm
#
# Order: create, login l6-a, use l6-b, use l6-c, rotation l6-a l6-b, race
# l6-a l6-b, rc l6-a, scan l6-a, scan l6-b, scan l6-c; then, when you can
# leave it, long l6-a l6-b (an access token lasts 8 hours: two sessions
# that cross a refresh). Send the output of every phase, and the logs in
# ./oauth-l6-logs/. The script prints no token: lengths, prefixes of
# phantoms, and counts.
set -uo pipefail

WMF_SBX=${WMF_SBX:-wmf-sbx}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)
LOGS=${WMF_SBX_CHECK_LOGS:-oauth-l6-logs}
mkdir -p "$LOGS"
LOGS=$(cd "$LOGS" && pwd)
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/wmf-sbx
STORE=$STATE/auth/state/nono/oauth-capture
HOST_COPY=$STATE/auth/credentials.json
CREDS=/home/agent/.claude/.credentials.json
PROMPT="Reply with the single word ok."

result() { echo "== RESULT $*"; }
proxy_log() { echo "$STATE/egress/$1/proxy.log"; }

phantom() {  # NAME: the guest's access phantom prefix and expiry
  "$WMF_SBX" exec "$1" -- sh -c "test -f $CREDS && jq -r '.claudeAiOauth |
    \"\(.accessToken[0:20]) \(.refreshToken[0:10]) expires \(.expiresAt / 1000 | floor | todate)\"' $CREDS \
    || echo 'no credentials file'"
}

host_copy() {
  if [ -f "$HOST_COPY" ]; then
    jq -r '.claudeAiOauth | "\(.accessToken[0:20]) \(.refreshToken[0:10]) expires \(.expiresAt / 1000 | floor | todate)"' "$HOST_COPY"
  else
    echo "none"
  fi
}

# A turn outside `resume`: no sync of the shared login.
exec_turn() {  # NAME LOG
  "$WMF_SBX" exec "$1" -- claude -p "$PROMPT" --model haiku < /dev/null > "$2" 2>&1
}

expire() {  # NAME: make Claude Code refresh at its next request
  "$WMF_SBX" exec "$1" -- sh -c "jq '.claudeAiOauth.expiresAt = 0' $CREDS > $CREDS.l6 \
    && mv $CREDS.l6 $CREDS && chmod 600 $CREDS"
}

token_lines() {  # NAME: the proxy's token-endpoint lines since the mark
  grep -E 'oauth/token|status=40[13]' "$(proxy_log "$1")" 2>/dev/null | tail -6 | sed 's/^/   | /'
}

phase_create() {
  for n in l6-a l6-b; do
    "$WMF_SBX" create --no-start --no-deps --no-remotes --auth-proxy --name "$n" "$REPO" \
      > "$LOGS/create-$n.log" 2>&1 && echo "-- $n: created" || {
      tail -20 "$LOGS/create-$n.log"; result "create: $n FAILED"; return 1; }
  done
  "$WMF_SBX" create --no-start --no-deps --no-remotes --sudo --auth-proxy --name l6-c "$REPO" \
    > "$LOGS/create-l6-c.log" 2>&1 && echo "-- l6-c: created (sudo)" || {
    tail -20 "$LOGS/create-l6-c.log"; result "create: l6-c FAILED"; return 1; }
  grep -h "auth proxy" "$LOGS"/create-l6-*.log | sort -u | sed 's/^/   | /'
  result "create: ok"
}

phase_login() {  # NAME
  echo "-- In the session: /login, open the URL on this machine, paste the code,"
  echo "   send one message (\"hi\"), then /exit."
  "$WMF_SBX" resume --no-continue "$1"
  phase_inspect "$1"
  [ -f "$HOST_COPY" ] && result "login $1: the host has the shared login" \
    || result "login $1: no host copy FAILED"
}

phase_inspect() {  # NAME
  echo "-- $1: the guest's login: $(phantom "$1")"
  echo "-- the host's copy:      $(host_copy)"
  echo "-- the capture store: $(find "$STORE" -type f 2>/dev/null | wc -l) file(s) in $STORE"
  echo "-- $1: the proxy's Anthropic decisions (counts):"
  grep -oE 'target="[a-z.]*(anthropic|claude)[a-z.]*" method="[A-Z]+" path="[^"?]*|host="[a-z.]*claude[a-z.]*" port=443 decision="deny"' \
    "$(proxy_log "$1")" 2>/dev/null | sort | uniq -c | sort -rn | head -30 | sed 's/^/   | /'
  echo "-- $1: denied (by path):"
  grep -E 'decision=Deny|decision="deny"' "$(proxy_log "$1")" 2>/dev/null \
    | grep -oE '(target|host)="[^"]*"( method="[A-Z]+" path="[^"?]*)?' | sort | uniq -c \
    | sed 's/^/   | /'
}

phase_use() {  # NAME
  "$WMF_SBX" resume --no-continue "$1" -- -p "$PROMPT" --model haiku < /dev/null \
    > "$LOGS/use-$1.log" 2>&1
  local rc=$?
  sed 's/^/   | /' "$LOGS/use-$1.log" | tail -5
  echo "-- $1 now: $(phantom "$1")"
  [ $rc = 0 ] && grep -qi '\bok\b' "$LOGS/use-$1.log" && result "use $1: ok" \
    || result "use $1: exit $rc FAILED"
}

phase_rotation() {  # A B
  # Both start from the same phantoms (a resume of each syncs them).
  phase_use "$1" >/dev/null; phase_use "$2" >/dev/null
  echo "-- before: $1 $(phantom "$1")"
  echo "--         $2 $(phantom "$2")"
  expire "$1"; exec_turn "$1" "$LOGS/rotation-$1.log"; local ra=$?
  echo "-- $1 refreshed (exec, no sync): rc $ra; now $(phantom "$1")"
  token_lines "$1"
  # B still holds the refresh phantom that A just used.
  expire "$2"; exec_turn "$2" "$LOGS/rotation-$2.log"; local rb=$?
  echo "-- $2 refreshed with the old phantoms: rc $rb; now $(phantom "$2")"
  sed 's/^/   | /' "$LOGS/rotation-$2.log" | tail -3
  token_lines "$2"
  if [ $rb = 0 ]; then
    result "rotation: the old refresh token still worked after $1's refresh (no rotation, or a grace period)"
  else
    result "rotation: the old refresh token failed after $1's refresh (it rotates); sync must win the race"
  fi
  phase_use "$2"
}

phase_race() {  # A B
  phase_use "$1" >/dev/null; phase_use "$2" >/dev/null
  expire "$1"; expire "$2"
  exec_turn "$1" "$LOGS/race-$1.log" & local pa=$!
  exec_turn "$2" "$LOGS/race-$2.log" & local pb=$!
  wait $pa; local ra=$?; wait $pb; local rb=$?
  echo "-- $1: rc $ra, now $(phantom "$1")"; tail -2 "$LOGS/race-$1.log" | sed 's/^/   | /'
  echo "-- $2: rc $rb, now $(phantom "$2")"; tail -2 "$LOGS/race-$2.log" | sed 's/^/   | /'
  token_lines "$1"; token_lines "$2"
  [ $ra = 0 ] && [ $rb = 0 ] && result "race: both refreshed" \
    || result "race: a=$ra b=$rb FAILED"
  echo "-- after a resume of each (sync):"
  phase_use "$1"; phase_use "$2"
}

phase_rc() {  # NAME
  local before; before=$(wc -l < "$(proxy_log "$1")" 2>/dev/null || echo 0)
  echo "-- In the session: /status (note Login method, Organization policy), /usage,"
  echo "   then Remote Control (/remote-control, or however your account offers it);"
  echo "   connect from the Claude app or claude.ai, send it one message, then /exit."
  "$WMF_SBX" resume --no-continue "$1"
  echo "-- $1: what the proxy decided during the session:"
  tail -n +"$((before + 1))" "$(proxy_log "$1")" > "$LOGS/rc-$1.log" 2>/dev/null
  grep -oE '(target|host)="[^"]*"( method="[A-Z]+" path="[^"?]*)?.*(decision=[A-Za-z]+|decision="[a-z]+")' \
    "$LOGS/rc-$1.log" | sed -E 's/ endpoint_policy.*//; s/ port=443 method="CONNECT"//' \
    | sort | uniq -c | sort -rn | head -40 | sed 's/^/   | /'
  echo "-- (the whole log of the session: $LOGS/rc-$1.log)"
}

phase_scan() {  # NAME
  python3 - "$1" "$STORE" "$WMF_SBX" <<'EOF'
import json, os, subprocess, sys
name, store, wmf_sbx = sys.argv[1:4]
secrets = set()
def walk(v):
    if isinstance(v, dict):
        for x in v.values(): walk(x)
    elif isinstance(v, list):
        for x in v: walk(x)
    elif isinstance(v, str) and len(v) >= 20 and not v.startswith("nono_") and (
            v.startswith("sk-ant-") or v.count(".") == 2 or len(v) >= 40):
        secrets.add(v)
for d, _dirs, files in os.walk(store):
    for f in files:
        try:
            walk(json.load(open(os.path.join(d, f))))
        except (OSError, ValueError):
            pass
if not secrets:
    print(f"== RESULT scan {name}: no tokens found in the host store {store}; nothing to look for")
    sys.exit(0)
dump = subprocess.run([wmf_sbx, "exec", "--engineer", "-w", "/", name, "--", "sudo", "sh", "-c",
                       "cat /home/agent/.claude/.credentials.json /proc/*/environ 2>/dev/null;"
                       " grep -rah . /home/agent/.claude 2>/dev/null | head -c 200000000"],
                      capture_output=True).stdout
hits = sum(dump.count(s.encode()) for s in secrets)
print(f"-- {len(secrets)} token-like values in the host store, {len(dump)} bytes scanned")
print(f"== RESULT scan {name}: {hits} hits" + ("" if hits == 0 else " FAILED"))
EOF
}

long_session() {  # NAME N INTERVAL
  for i in $(seq "$2"); do
    printf '{"type":"user","message":{"role":"user","content":"Reply with exactly: tick-%s"}}\n' "$i"
    echo "$(date -u +%FT%TZ) message $i; host copy: $(host_copy)" >> "$LOGS/long-$1.log"
    if [[ $i -lt $2 ]]; then sleep "$3"; fi
  done | "$WMF_SBX" resume --no-continue "$1" -- -p --input-format stream-json \
    --output-format stream-json --verbose --model haiku > "$LOGS/long-$1.jsonl" 2>&1
  echo "-- $1: session rc $?, replies $(grep -o '"result":"tick-[0-9]*' "$LOGS/long-$1.jsonl" | wc -l) of $2"
}

phase_long() {  # A B [HOURS]
  local hours=${3:-9} interval=${WMF_SBX_L6_INTERVAL:-600}
  local n=$(( hours * 3600 / interval ))
  : > "$LOGS/long-$1.log"; : > "$LOGS/long-$2.log"
  long_session "$1" "$n" "$interval" & local pa=$!
  sleep 30
  long_session "$2" "$n" "$interval" & local pb=$!
  wait $pa $pb
  for s in "$1" "$2"; do
    echo "-- $s: token endpoint and errors:"
    grep -E 'oauth/token|status=40[13]' "$(proxy_log "$s")" | tail -10 | sed 's/^/   | /'
  done
  echo "-- the host copy over time ($1):"; sed 's/^/   | /' "$LOGS/long-$1.log" | awk 'NR%6==1'
}

phase_rm() {
  for n in l6-a l6-b l6-c; do "$WMF_SBX" rm --force "$n" >/dev/null 2>&1 && echo "-- $n removed"; done
  echo "-- the shared login stays in $STATE/auth (rm -r it to sign out every sandbox)"
}

case "${1:-}" in
  create|rm) "phase_$1" ;;
  login|inspect|use|rc|scan) [ -n "${2:-}" ] || { echo "usage: $0 $1 NAME"; exit 2; }
                             "phase_$1" "$2" ;;
  rotation|race) [ -n "${3:-}" ] || { echo "usage: $0 $1 A B"; exit 2; }; "phase_$1" "$2" "$3" ;;
  long) [ -n "${3:-}" ] || { echo "usage: $0 long A B [HOURS]"; exit 2; }
        phase_long "$2" "$3" "${4:-9}" ;;
  *) sed -n '2,23p' "$0"; exit 2 ;;
esac
