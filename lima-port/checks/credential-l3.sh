#!/usr/bin/env bash
# Track L, step L3 (HANDOFF-LIMA.md): acceptance of the host credential
# proxy. Run it on your host, with your usual credential (the checks are
# about the host's Claude Code login most of all).
#
#   lima-port/checks/credential-l3.sh create      # two small sandboxes, l3-a and l3-b
#   lima-port/checks/credential-l3.sh source      # which credential resume will use
#   lima-port/checks/credential-l3.sh concurrent  # a session in each, at the same time
#   lima-port/checks/credential-l3.sh scan        # look for the credential in both VMs
#   lima-port/checks/credential-l3.sh long [H]    # one session for H hours (default 9)
#   lima-port/checks/credential-l3.sh rm          # remove both sandboxes
#
# Then, by hand: `wmf-sbx resume l3-a` (want: no login prompt), and on a
# Mac the same commands (the keychain source). The blind run is
# lima-port/acceptance/README.md, without ANTHROPIC_API_KEY set.
#
# The sandboxes hold only this checkout (--no-deps --no-remotes), so
# `create` is quick once the golden image is built. WMF_SBX_CHECK_IMAGE=KEY
# reuses a cached image (`wmf-sbx image ls`).
#
# No check prints a credential or sends one into a VM: credential-scan.py
# reads them on the host and counts matches in what it is given.
# Logs go to ./credential-l3-logs/.
set -uo pipefail

WMF_SBX=${WMF_SBX:-wmf-sbx}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)
LOGS=${WMF_SBX_CHECK_LOGS:-credential-l3-logs}
SANDBOXES=(${WMF_SBX_L3_NAMES:-l3-a l3-b})   # WMF_SBX_L3_NAMES="x y" to use others
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/wmf-sbx/sandboxes
LIMA_DIR=${LIMA_HOME:-$HOME/.lima}
IMAGE_ARGS=(${WMF_SBX_CHECK_IMAGE:+--image "$WMF_SBX_CHECK_IMAGE"})
mkdir -p "$LOGS"

proxy_log() { echo "$STATE/$1.credproxy.log"; }

# One `claude -p` turn with a tool call, in a session. Output to LOG.
session() {  # NAME LOG PROMPT
  "$WMF_SBX" resume "$1" -- -p "$3" --model haiku --allowedTools Bash \
    < /dev/null > "$2" 2>&1
}

case "${1:-}" in
  create)
    for n in "${SANDBOXES[@]}"; do
      "$WMF_SBX" create "${IMAGE_ARGS[@]}" --no-deps --no-remotes --name "$n" "$REPO" ||
        exit 1
    done
    ;;

  source)
    # What resume will use, as host_source sees it; prints no credential.
    PYTHONPATH="$REPO/sbx/src" python3 -c '
import wmf_sbx.credproxy as cp
s = cp.host_source()
print("source:", s.describe if s else "none (Claude Code will ask for a login in the VM)")
if s:
    print("kind:", s.kind)
    print("problem:", cp.check_source(s) or "none")'
    ;;

  concurrent)
    # Want: both rc 0, both outputs "l3-ok-<name>", and in each proxy log
    # its own POST /v1/messages 200 lines, no 401 or 403.
    pids=()
    for n in "${SANDBOXES[@]}"; do
      : > "$(proxy_log "$n")"
      session "$n" "$LOGS/concurrent-$n.log" \
        "Run: sleep 20; echo l3-ok-$n. Then reply with its output." &
      pids+=($!)
    done
    for i in "${!SANDBOXES[@]}"; do
      wait "${pids[$i]}"; rc=$?
      n=${SANDBOXES[$i]}
      echo "== $n: rc $rc; output: $(grep -o "l3-ok-$n" "$LOGS/concurrent-$n.log" | head -1)"
      sed 's/^/   | /' "$(proxy_log "$n")"
    done
    ;;

  scan)
    # During a session (the proxy, the wrapper, nono and Claude Code are
    # running): every process's environment and command line, /dev/shm,
    # /tmp and the agent's Claude Code files, streamed to the host and
    # counted there. Then each VM's disk and Lima files, and the host's
    # wmf-sbx state. Want: 0 hits everywhere.
    rc=0
    for n in "${SANDBOXES[@]}"; do
      session "$n" "$LOGS/scan-session-$n.log" \
        "Run: sleep 90; echo done. Then reply with its output." &
      pid=$!
      sleep 45
      limactl shell "wmf-sbx-$n" sudo sh -c '
        for p in /proc/[0-9]*; do cat "$p/environ" "$p/cmdline" 2>/dev/null; echo; done
        tar cf - /dev/shm /tmp /run/wmf-sbx /home/agent/.claude /home/agent/.claude.json \
          /home/agent/.config /home/agent/.local 2>/dev/null' |
        python3 "$HERE/credential-scan.py" "$n processes and files" || rc=1
      wait $pid
      echo "   (session rc $?)"
      limactl shell "wmf-sbx-$n" sync
      files=()
      while IFS= read -r -d '' f; do files+=("$f"); done \
        < <(find "$LIMA_DIR/wmf-sbx-$n" -maxdepth 1 -type f -print0)
      python3 "$HERE/credential-scan.py" "$n VM disk and Lima files" "${files[@]}" || rc=1
    done
    find "$STATE" -type f -print0 | xargs -0 python3 "$HERE/credential-scan.py" \
      "host wmf-sbx state" || rc=1
    echo "== scan: $([[ $rc == 0 ]] && echo PASS || echo FAIL)"
    exit $rc
    ;;

  long)
    # One session for H hours: the proxy reads the host login for each
    # request, so it must follow the host's refreshes. The session is one
    # `claude -p` reading stream-json from stdin; this script sends it a
    # message every INTERVAL seconds (WMF_SBX_L3_INTERVAL, default 600), so
    # one proxy serves every request. (Asking the model to sleep in a loop
    # does not work: RAN, the session ended within 10 minutes.) Each message
    # also logs the login's expiry and a hash prefix of its access token
    # (not the token). Want: a reply to every message, no 401 in the proxy
    # log, and, if the run crosses the expiry, a new hash. Only Claude Code
    # running on the host refreshes the host login.
    hours=${2:-9}
    interval=${WMF_SBX_L3_INTERVAL:-600}
    n=${WMF_SBX_L3_MESSAGES:-$(( hours * 3600 / interval ))}
    first=${SANDBOXES[0]}
    log=$LOGS/long.log
    : > "$(proxy_log "$first")"
    : > "$log"
    for i in $(seq "$n"); do
      python3 "$HERE/credential-scan.py" --login-state "message $i" >> "$log"
      printf '{"type":"user","message":{"role":"user","content":"Reply with exactly: tick-%s"}}\n' "$i"
      if [[ $i -lt $n ]]; then sleep "$interval"; fi
    done | "$WMF_SBX" resume "$first" -- -p --input-format stream-json \
      --output-format stream-json --verbose --model haiku > "$LOGS/long-session.jsonl" 2>&1
    echo "== session rc $?"
    echo "== replies: $(grep -o '"result":"tick-[0-9]*' "$LOGS/long-session.jsonl" | wc -l) of $n"
    echo "== login over time:"; sed 's/^/   | /' "$log"
    echo "== proxy log (status counts):"
    awk '{print $5}' "$(proxy_log "$first")" | sort | uniq -c | sed 's/^/   | /'
    grep -E ' (401|403|502) ' "$(proxy_log "$first")" | sed 's/^/   ! /'
    ;;

  rm)
    for n in "${SANDBOXES[@]}"; do "$WMF_SBX" rm --force "$n"; done
    ;;

  *)
    sed -n '2,24p' "$0" >&2
    exit 2
    ;;
esac
