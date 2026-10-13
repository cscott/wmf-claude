#!/usr/bin/env bash
# PLAN-SUDO A2.0 item 7: the QEMU wrapper and the host egress proxy
# (sbx/bin/wmf-sbx-qemu, sbx/src/wmf_sbx/egress.py) on a host that is not
# itself a VM: Linux with KVM, or macOS with HVF. Items 2, 6 and 8 again,
# plus the guest's clock. In the nested dev VM they passed (sbx/NOTES.md,
# "`--sudo` egress (track A)").
#
#   lima-port/checks/sudo-egress-m7.sh all     # every step below, in order
#   lima-port/checks/sudo-egress-m7.sh STEP    # or one: preflight create
#                                              # check clock swap lifecycle
#   lima-port/checks/sudo-egress-m7.sh clean   # delete the VM and its files
#
# It makes one throwaway Lima VM, NAME (default egress-m7; not a wmf-sbx
# sandbox, so wmf-sbx does not list it) from Lima's debian-13 template,
# with no mounts, and runs it behind the wrapper: `restrict=on`, the
# guest's only way out the guestfwd to `nono proxy` on the host. Its
# egress files are in ~/.local/state/wmf-sbx/egress/NAME. `clean` removes
# both. Each check prints PASS or FAIL; the output, and copies of the
# proxy and Lima logs, go to ./sudo-egress-m7-logs/.
#
# M7_MEMORY (default 2GiB) is the VM's memory.
# M7_IMAGE=PATH boots a local qcow2 instead of the template's download
# (e.g. a wmf-sbx golden image, `wmf-sbx image ls`).
#
# No check sends a POST: the endpoint rules were measured in item 2 and 9.
set -uo pipefail

NAME=${M7_NAME:-egress-m7}
# NAME goes into paths, a Python string and `rm -rf`: a plain name only.
[[ $NAME =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "M7_NAME: '$NAME' is not a plain name"; exit 2; }
LOGS=${M7_LOGS:-sudo-egress-m7-logs}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
E="${XDG_STATE_HOME:-$HOME/.local/state}/wmf-sbx/egress/$NAME"
LIMA_DIR="${LIMA_HOME:-$HOME/.lima}/$NAME"
# Written by `create` only. Every step that stops, kills or deletes needs
# it, so that the script never acts on a Lima VM it did not make (an
# M7_NAME that names one of yours).
MARK="$E/made-by-sudo-egress-m7"
PROXY=http://192.168.5.100:3128
WIKI=https://en.wikipedia.org/w/api.php
mkdir -p "$LOGS"
exec > >(tee -a "$LOGS/run.log") 2>&1

case "$(uname -m)" in
  x86_64|amd64) QARCH=x86_64 ;;
  arm64|aarch64) QARCH=aarch64 ;;
  *) echo "unknown machine $(uname -m)"; exit 1 ;;
esac
QVAR="QEMU_SYSTEM_$(tr a-z A-Z <<<"$QARCH")"

FAILS=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }
step() { echo; echo "== $* ($(date '+%H:%M:%S'))"; }

egress_py() {
  # $1: python statements, with `e` the egress module.
  python3 -c "import sys; sys.path.insert(0, '$REPO/sbx/src')
import wmf_sbx.egress as e
$1"
}
listening() {
  python3 -c 'import socket, sys; socket.create_connection(("127.0.0.1", int(sys.argv[1])), 1)' "$1" 2>/dev/null
}
alive() { kill -0 "$1" 2>/dev/null; }
# The pids of the wrapper's children: the proxies and QEMU.
children() { ps -A -o pid=,ppid=,command= | awk -v p="$1" '$2 == p {print $1, $3, $4}'; }
gsh() { limactl shell --workdir / "$NAME" -- "$@"; }
# A request from the guest through the egress proxy; prints the status.
gcurl() { gsh curl -sS -m 20 -o /dev/null -w '%{http_code}' -x "$PROXY" "$@" 2>/dev/null; }
start_vm() {
  env "$QVAR=$REPO/sbx/bin/wmf-sbx-qemu" \
      WMF_SBX_QEMU_REAL="$(command -v "qemu-system-$QARCH")" \
      WMF_SBX_EGRESS_CONFIG="$E/proxy.json" \
      limactl start --tty=false "$NAME" 2>&1 | tail -3
}
wrapper_pid() { cat "$E/wrapper.pid" 2>/dev/null; }
ours() {
  [[ -e $MARK ]] && return 0
  echo "refusing: this script did not make a Lima VM '$NAME' (no $MARK)"; exit 1
}

preflight() {
  step "preflight"
  uname -a
  [[ $(uname -s) == Darwin ]] && { sw_vers; sysctl -n machdep.cpu.brand_string; }
  [[ $(uname -s) == Linux ]] && ls -l /dev/kvm
  for t in limactl nono socat openssl python3 "qemu-system-$QARCH"; do
    local v=--version; [[ $t == socat ]] && v=-V; [[ $t == openssl ]] && v=version
    if command -v "$t" >/dev/null; then pass "$t: $("$t" $v 2>&1 | grep -m1 '[0-9]\.[0-9]')"
    else fail "$t is not installed"; fi
  done
  git -C "$REPO" log --oneline -1
  env | grep -i '^https\?_proxy=' | sed 's|//[^@/]*@|//***@|' || echo "no host proxy: the egress proxy goes direct"
}

create() {
  step "create"
  if limactl list -q 2>/dev/null | grep -qx "$NAME"; then
    fail "a Lima VM '$NAME' exists already: \`$0 clean\` if this script made it, or set M7_NAME"
    return 1
  fi
  egress_py "e.make_ca('$NAME'); print(e.write_config('$NAME'))" || { fail "egress config"; return 1; }
  local set=".caCerts.files = [\"$E/ca.pem\"] | .hostResolver.enabled = false | .propagateProxyEnv = false | .memory = \"${M7_MEMORY:-2GiB}\""
  [[ -n ${M7_IMAGE:-} ]] && set+=" | .images = [{\"location\": \"$M7_IMAGE\", \"arch\": \"$QARCH\"}]"
  limactl create --tty=false --name="$NAME" --vm-type=qemu --mount-none --containerd=none \
    --set "$set" template:debian-13 2>&1 | tail -2
  limactl list -q 2>/dev/null | grep -qx "$NAME" || { fail "limactl create"; return 1; }
  : > "$MARK"
  local t0=$SECONDS
  start_vm
  echo "first start took $((SECONDS - t0)) s"
  if gsh true; then pass "the VM booted behind the wrapper"
  else fail "the VM did not boot"; return 1; fi
}

check() {
  step "check (items 2 and 6)"
  ours
  local w; w=$(wrapper_pid)
  if [[ -n $w ]] && alive "$w"; then pass "wrapper pid $w"; else fail "no wrapper (pid file: '$w')"; return 1; fi
  children "$w"
  children "$w" | grep -q "qemu-system" && pass "QEMU is the wrapper's child" || fail "QEMU is not the wrapper's child"
  children "$w" | grep -q "nono proxy" && pass "nono proxy is the wrapper's child" || fail "no proxy under the wrapper"
  listening "$(cat "$E/port")" && pass "proxy on port $(cat "$E/port")" || fail "nothing on port $(cat "$E/port")"
  grep -c 'wmf-sbx-qemu' "$LIMA_DIR/ha.stderr.log" | sed 's/^/wrapper lines in the Lima log: /'
  # This VM's QEMU only: its command line names the instance directory.
  ps -A -o command= | grep -F -- "$LIMA_DIR/" | grep -- "-netdev user" | grep -v grep \
    | tr ' ' '\n' | grep -A1 '^-netdev$' | tail -1
  [[ $(gcurl "$WIKI") == 200 ]] && pass "guest: wiki GET through the proxy is 200" || fail "guest: wiki GET is not 200"
  local off; off=$(gcurl https://example.com/)
  [[ $off != 200 ]] && pass "guest: example.com (off the list) refused ($off)" || fail "guest: example.com got through"
  if gsh curl -sS -m 10 -o /dev/null -k https://140.82.112.3/ 2>/dev/null; then fail "guest: direct 140.82.112.3:443 got through"
  else pass "guest: no direct route (140.82.112.3:443)"; fi
  if gsh timeout 5 getent hosts example.com >/dev/null; then fail "guest: DNS answers"
  else pass "guest: no DNS"; fi
}

clock() {
  step "clock"
  ours
  grep 'Time sync' "$LIMA_DIR/ha.stderr.log" | grep -v 'within threshold' | tail -4
  grep 'Time sync: drift' "$LIMA_DIR/ha.stderr.log" | tail -1
  gsh timedatectl show -p NTPSynchronized -p NTP 2>/dev/null
  if [[ $QARCH == x86_64 ]]; then
    gsh sudo modprobe ptp_kvm 2>&1 && gsh ls -l /dev/ptp0 && pass "ptp_kvm loads" || echo "ptp_kvm does not load"
  fi
  # The skew right after a start is what failed TLS in the nested VM: the
  # first request to each host in the first seconds.
  limactl stop "$NAME" >/dev/null 2>&1
  start_vm >/dev/null
  echo "limactl start returned at $(date '+%H:%M:%S')"
  local h g
  h=$(python3 -c 'import time; print(f"{time.time():.3f}")'); g=$(gsh date +%s.%3N)
  echo "right after start: host $h, guest $g, guest - host = $(python3 -c "print(round($g - $h, 3))") s"
  local s
  for u in https://www.mediawiki.org/w/api.php https://registry.npmjs.org/ https://packagist.org/; do
    s=$(gcurl "$u"); echo "  first request after start: $u $s"
  done
  sleep 10
  # ha.stderr.log is rewritten at each start: this one is the restart's.
  grep 'Time sync' "$LIMA_DIR/ha.stderr.log" | grep -v 'within threshold' | tail -3
  grep 'Time sync: drift' "$LIMA_DIR/ha.stderr.log" | tail -1
}

swap() {
  step "swap (item 8)"
  ours
  # The proxy's own decision, from its log. A proxy upstream of it (in a
  # nested run) may refuse the host too: the log says "deny" then, with
  # another reason, and the guest gets no 200.
  local host=${M7_SWAP_HOST:-example.org}
  last() { grep "host=\"$host\"" "$E/proxy.log" | tail -1 | grep -o 'decision=.*'; }
  listed() { last | grep -q 'not in the allowlist'; }
  local w old new; w=$(wrapper_pid); old=$(cat "$E/port")
  echo "before: $(gcurl "https://$host/")"
  listed && pass "$host denied by the allowlist before" || fail "$host not denied by the allowlist before: $(last)"
  echo "$host" >> "$E/allow"
  egress_py "e.write_config('$NAME')"
  kill -USR1 "$w"
  for _ in $(seq 20); do new=$(cat "$E/port"); [[ $new != "$old" ]] && break; sleep 1; done
  [[ $new != "$old" ]] && pass "port $old -> $new" || { fail "the port did not change"; return 1; }
  sleep 3
  listening "$new" && pass "the new proxy stays up" || fail "the new proxy is gone"
  listening "$old" && pass "the old proxy is still up" || fail "the old proxy is gone"
  echo "after: $(gcurl "https://$host/")"
  if listed; then fail "$host still denied by the allowlist after"
  else pass "$host passes the allowlist after: $(last)"; fi
}

# $1: how to stop (stop, force, kill-wrapper, kill-qemu)
lifecycle_one() {
  limactl list --format '{{.Status}}' "$NAME" | grep -q Running || start_vm >/dev/null
  local w pids; w=$(wrapper_pid)
  pids="$w $(children "$w" | awk '{print $1}' | tr '\n' ' ')"
  echo "-- $1: wrapper and children: $pids"
  case $1 in
    stop) limactl stop "$NAME" >/dev/null 2>&1 ;;
    force) limactl stop --force "$NAME" >/dev/null 2>&1 ;;
    kill-wrapper) kill -9 "$w" ;;
    kill-qemu) local q; q=$(children "$w" | awk '/qemu-system/ {print $1}')
               [[ -n $q ]] && kill -9 $q ;;
  esac
  sleep 5
  local left=""
  for p in $pids; do alive "$p" && left+=" $p"; done
  if [[ -z $left ]]; then pass "$1: the wrapper, the proxy and QEMU are gone"
  else
    fail "$1: still running:$left"
    ps -o pid=,command= -p "${left// /,}" 2>/dev/null | cut -c1-120
    # Only what is still ours: a pid can be reused.
    for p in $left; do
      ps -o command= -p "$p" 2>/dev/null | grep -qE 'qemu-system|nono proxy|wmf-sbx-qemu' \
        && kill -9 "$p" 2>/dev/null
    done
  fi
  [[ -e $E/wrapper.pid ]] && echo "   wrapper.pid is left (expected after kill-wrapper only)"
  limactl stop --force "$NAME" >/dev/null 2>&1
}

lifecycle() {
  step "lifecycle (item 6)"
  ours
  for how in stop force kill-qemu kill-wrapper; do lifecycle_one "$how"; done
  start_vm >/dev/null && gsh true && pass "it starts again"
}

clean() {
  step "clean"
  ours
  limactl delete --force "$NAME" 2>&1 | tail -1
  rm -rf "$E"
}

collect() {
  cp "$E/proxy.log" "$LOGS/" 2>/dev/null
  cp "$LIMA_DIR/ha.stderr.log" "$LOGS/" 2>/dev/null
}

case "${1:-}" in
  all) preflight; create && { check; clock; swap; lifecycle; } ;;
  preflight|create|check|clock|swap|lifecycle|clean) "$1" ;;
  *) sed -n '2,25p' "$0"; exit 2 ;;
esac
[[ ${1:-} == clean ]] || collect
echo; echo "$FAILS failed. Logs in $LOGS/"
exit $((FAILS > 0))
