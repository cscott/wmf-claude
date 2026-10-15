#!/usr/bin/env bash
# Track L, step L5 (HANDOFF-LIMA.md): measure a full Claude login through
# the host egress proxy, with nono's `oauth_capture` provider in the
# proxy's profile. Run it on your host, from this checkout. No change to
# the default: only sandboxes created with WMF_SBX_EGRESS_PROFILE set (this
# script does that) run the capture profile.
#
#   lima-port/checks/oauth-l5.sh profile        # write and validate the L5 profile
#   lima-port/checks/oauth-l5.sh create         # two small sandboxes, l5-a and l5-b
#   lima-port/checks/oauth-l5.sh login l5-a     # interactive: /login in the sandbox
#   lima-port/checks/oauth-l5.sh inspect l5-a   # what the guest holds, what the proxy saw
#   lima-port/checks/oauth-l5.sh use l5-a       # one `claude -p` turn on the login
#   lima-port/checks/oauth-l5.sh refresh l5-a   # force a token refresh, then a turn
#   lima-port/checks/oauth-l5.sh share l5-a l5-b  # the login's file in a second sandbox
#   lima-port/checks/oauth-l5.sh scan l5-a      # real tokens in the guest? (want 0)
#   lima-port/checks/oauth-l5.sh rm             # remove both sandboxes
#
# Order: profile, create, login l5-a, inspect l5-a, use l5-a, refresh l5-a,
# inspect l5-a, share l5-a l5-b, scan l5-a, scan l5-b. Then, by hand, in
# `wmf-sbx exec l5-a -- env -u ANTHROPIC_BASE_URL ... claude` (the `login`
# phase prints the line): `/status`, `/usage`, and Remote Control if your
# account has it; note what works and what the proxy log says. Send the
# output of every phase, and logs in ./oauth-l5-logs/.
#
# The `claude` in these phases runs outside nono (wmf-sbx exec, as the
# agent), with the egress proxy's variables and without the session's
# credential proxy, so it uses the login it holds. The proxy never prints a
# token; neither does this script (it prints lengths, prefixes of phantoms,
# and counts).
set -uo pipefail

WMF_SBX=${WMF_SBX:-wmf-sbx}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)
LOGS=${WMF_SBX_CHECK_LOGS:-oauth-l5-logs}
mkdir -p "$LOGS"
LOGS=$(cd "$LOGS" && pwd)
PROFILE_OUT="$LOGS/egress-l5.json"
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/wmf-sbx
NAMES=(l5-a l5-b)
CREDS=/home/agent/.claude/.credentials.json
# The session's credential proxy variables would win over the login.
NOCRED=(env -u ANTHROPIC_BASE_URL -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN
        -u CLAUDE_CODE_OAUTH_TOKEN)

result() { echo "== RESULT $*"; }
egress_log() { echo "$STATE/egress/$1/proxy.log"; }
egress_state() { echo "$STATE/egress/$1/state"; }

phase_profile() {
  # profiles/wmf-engineer.json, plus nono's claude_code provider as its
  # profile guide gives it (`nono profile guide`), and a route that lets
  # the phantom through only for POST /v1/messages (the narrow build).
  jq '. + {
    credential_providers: {claude_code: {type: "oauth_capture",
      token_endpoints: [{host: "https://platform.claude.com", path: "/v1/oauth/token",
        response_fields: [{path: "access_token", kind: "opaque", format: "sk-ant-oat01-{}"},
                          {path: "refresh_token", kind: "opaque"},
                          {path: "id_token", kind: "jwt"}],
        request_body: "auto", request_nonce_fields: ["access_token", "refresh_token"]}],
      api_hosts: ["https://api.anthropic.com"]}},
    credential_routes: [{name: "anthropic_oauth", provider: "claude_code",
      endpoint_policy: {default: {decision: "deny"},
                        allow: [{method: "POST", path: "/v1/messages"}]}}]}' \
    "$REPO/profiles/wmf-engineer.json" > "$PROFILE_OUT" || { result "profile: jq FAILED"; return 1; }
  nono --version
  nono profile validate "$PROFILE_OUT" || { result "profile: invalid"; return 1; }
  # What the proxy makes of it, without a VM.
  local log="$LOGS/proxy-dryrun.log"
  nono proxy --profile "$PROFILE_OUT" --port 0 --no-auth -v > "$log" 2>&1 & local p=$!
  sleep 3; kill "$p" 2>/dev/null; wait "$p" 2>/dev/null
  grep -E "anthropic|claude|oauth|capture|creds:" "$log" | grep -v wikimedia | sed 's/^/   | /'
  result "profile: $PROFILE_OUT (export WMF_SBX_EGRESS_PROFILE=$PROFILE_OUT for the other phases)"
}

need_profile() {
  [ -f "$PROFILE_OUT" ] || { echo "run the profile phase first"; exit 1; }
  export WMF_SBX_EGRESS_PROFILE="$PROFILE_OUT"
}

phase_create() {
  need_profile
  for n in "${NAMES[@]}"; do
    "$WMF_SBX" create --no-start --no-deps --no-remotes --egress-proxy --name "$n" "$REPO" \
      > "$LOGS/create-$n.log" 2>&1 && echo "-- $n: created" || {
      tail -20 "$LOGS/create-$n.log"; result "create: $n FAILED"; return 1; }
  done
  result "create: ok"
}

phase_login() {  # NAME
  need_profile
  echo "-- In the sandbox's claude: /login, open the URL on this machine, paste the code,"
  echo "   then /exit. (The command, to run again by hand for /status, /usage, Remote Control:)"
  echo "   WMF_SBX_EGRESS_PROFILE=$PROFILE_OUT $WMF_SBX exec $1 -- ${NOCRED[*]} claude"
  "$WMF_SBX" exec "$1" -- "${NOCRED[@]}" claude
  phase_inspect "$1"
}

phase_inspect() {  # NAME
  echo "-- $1: the guest's credentials file (shape only)"
  "$WMF_SBX" exec "$1" -- sh -c "test -f $CREDS && jq -c '.claudeAiOauth | {
      access: ((.accessToken // \"\") | {prefix: .[0:16], length: length}),
      refresh: ((.refreshToken // \"\") | {prefix: .[0:8], length: length}),
      expiresAt, scopes, subscriptionType}' $CREDS || echo 'no credentials file'"
  echo "-- $1: the capture store on the host (files only)"
  find "$(egress_state "$1")" -type f 2>/dev/null | sed 's/^/   | /'
  echo "-- $1: the proxy's log for Anthropic hosts (last 40)"
  grep -E "anthropic|claude\.(ai|com)|oauth|capture|phantom" "$(egress_log "$1")" 2>/dev/null \
    | tail -40 | sed 's/^/   | /'
}

phase_use() {  # NAME
  need_profile
  "$WMF_SBX" exec "$1" -- "${NOCRED[@]}" claude -p "Reply with the single word ok." \
    --model haiku > "$LOGS/use-$1.log" 2>&1
  local rc=$?
  sed 's/^/   | /' "$LOGS/use-$1.log" | tail -5
  [ $rc = 0 ] && result "use $1: ok" || result "use $1: exit $rc FAILED"
}

phase_refresh() {  # NAME
  need_profile
  local before; before=$("$WMF_SBX" exec "$1" -- jq -r '.claudeAiOauth.accessToken[0:24]' "$CREDS")
  # Claude Code refreshes a token whose expiresAt has passed.
  "$WMF_SBX" exec "$1" -- sh -c "jq '.claudeAiOauth.expiresAt = 0' $CREDS > $CREDS.l5 && mv $CREDS.l5 $CREDS && chmod 600 $CREDS"
  phase_use "$1"
  local after; after=$("$WMF_SBX" exec "$1" -- jq -r '.claudeAiOauth.accessToken[0:24]' "$CREDS")
  echo "-- access token prefix before: $before, after: $after"
  [ "$before" != "$after" ] && result "refresh $1: a new phantom" || result "refresh $1: no new token"
}

phase_share() {  # FROM TO
  need_profile
  local tmp; tmp=$(mktemp -d)
  "$WMF_SBX" cp "$1:$CREDS" "$tmp/" && "$WMF_SBX" cp "$tmp/.credentials.json" "$2:/home/agent/.claude/"
  rm -rf "$tmp"
  "$WMF_SBX" exec "$2" -- chmod 600 "$CREDS"
  phase_use "$2"
  echo "-- (each sandbox's proxy keeps its own capture store under $(egress_state "$2"):"
  echo "   a failure here means a login is per sandbox unless the stores are shared)"
}

phase_scan() {  # NAME
  # The real tokens are in the host's capture store; count them in the
  # guest's credentials, its agent's files and every process's environment.
  python3 - "$1" "$(egress_state "$1")" "$WMF_SBX" <<'EOF'
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

phase_rm() {
  for n in "${NAMES[@]}"; do "$WMF_SBX" rm --force "$n" >/dev/null 2>&1 && echo "-- $n removed"; done
}

case "${1:-}" in
  profile|create|rm) "phase_$1" ;;
  login|inspect|use|refresh|scan) [ -n "${2:-}" ] || { echo "usage: $0 $1 NAME"; exit 2; }
                                  "phase_$1" "$2" ;;
  share) [ -n "${3:-}" ] || { echo "usage: $0 share FROM TO"; exit 2; }; phase_share "$2" "$3" ;;
  *) sed -n '2,29p' "$0"; exit 2 ;;
esac
