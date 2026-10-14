#!/bin/bash
# Live checks for Lima VM mode against a real VM. tests/test-lima.sh checks
# the files; this script checks what a running VM enforces. Run it on the
# host, outside nono: limactl needs ~/.lima, which the sandbox denies.
#
# The VM must exist and run (bin/wmf-claude-vm create). The script creates a
# throwaway repository, pushes it as workspace smoke-<pid>, and removes the
# workspace at the end. It does not change your other workspaces or repos.
#
# Usage: ./tests/smoke-lima.sh [--claude]
#   --claude   also run one sandboxed `claude -p` turn in the VM (needs
#              bin/wmf-claude-vm login first; costs one short model call)
#
#   WMF_CLAUDE_VM=<name>   Lima instance name (default: wmf-claude)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VMCMD="$REPO_ROOT/bin/wmf-claude-vm"
VM="${WMF_CLAUDE_VM:-wmf-claude}"
WITH_CLAUDE=false
[[ "${1:-}" == "--claude" ]] && WITH_CLAUDE=true

PASS=0
FAIL=0
red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
pass() { green "PASS: $*"; ((PASS++)); }
fail() { red   "FAIL: $*"; ((FAIL++)); }
info() { printf 'INFO: %s\n' "$*"; }

# A command in the guest as the engineer, and as the agent.
g()  { limactl shell --workdir /home/engineer "$VM" -- "$@"; }
ga() { g sudo -H -u agent -- "$@"; }

if ! limactl list >/dev/null 2>&1; then
  red "limactl cannot list instances. Run this outside the nono sandbox."
  exit 1
fi
if [[ "$(limactl list --format '{{.Status}}' "$VM" 2>/dev/null)" != Running ]]; then
  red "VM '$VM' is not running. Create or start it: bin/wmf-claude-vm create | start"
  exit 1
fi

# A UDP DNS query for example.com to $1:$2. It prints ANSWERED, REFUSED
# (an ICMP reject), TIMEOUT, or EPERM/EACCES (a seccomp or Landlock deny).
UDP_PROBE='
import socket, sys, errno
q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    s.connect((sys.argv[1], int(sys.argv[2])))
    s.send(q)
    s.recv(512)
    print("ANSWERED")
except ConnectionRefusedError:
    print("REFUSED")
except socket.timeout:
    print("TIMEOUT")
except OSError as e:
    print(errno.errorcode.get(e.errno, str(e.errno)))
'

echo "=== Lima VM smoke test: $VM ==="
echo ""
echo "--- install ---"
want_rev=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
have_rev=$(g cat /opt/wmf-claude/.wmf-claude-rev 2>/dev/null | tr -d '\r')
if [[ "$have_rev" == "$want_rev" ]]; then pass "VM runs host HEAD ($want_rev)"
else fail "VM runs '${have_rev:-nothing}', host HEAD is $want_rev (bin/wmf-claude-vm update)"; fi
want_nono=$(tr -d '[:space:]' < "$REPO_ROOT/.nono-version")
have_nono=$(g nono --version 2>/dev/null | awk '{print $2}' | tr -d '\r')
if [[ "$have_nono" == "$want_nono" ]]; then pass "nono $want_nono (pinned)"
else fail "nono is '${have_nono:-missing}', .nono-version says $want_nono"; fi
if ga test -w /opt/wmf-claude/bin/claude; then fail "agent can write the install tree"
else pass "agent cannot write the install tree"; fi
if g sudo -n nft list table inet wmf_claude_hostblock >/dev/null 2>&1; then pass "nft host block loaded"
else fail "nft host block missing"; fi
tiocsti=$(g cat /proc/sys/dev/tty/legacy_tiocsti 2>/dev/null | tr -d '\r')
if [[ "$tiocsti" == 0 ]]; then pass "TIOCSTI off"
else fail "dev.tty.legacy_tiocsti is '${tiocsti:-missing}', want 0"; fi

echo ""
echo "--- host boundary ---"
mounts=$(g findmnt -rn -t virtiofs,9p,fuse.sshfs 2>/dev/null | tr -d '\r')
if [[ -z "$mounts" ]]; then pass "no host mounts"; else fail "host mounts: $mounts"; fi
# This VM's hostagent may listen on its SSH port and on its DNS server for
# the guest (logged at start). Any other port is a forward into the guest.
# Other Lima homes (Colima) have their own processes; they are not checked.
ha_pid=$(limactl list --format '{{.HostAgentPID}}' "$VM" 2>/dev/null)
ha_dir=$(limactl list --format '{{.Dir}}' "$VM" 2>/dev/null)
ok_ports=$(limactl list --format '{{.SSHLocalPort}}' "$VM" 2>/dev/null
  grep -oE 'DNS listening on: [0-9.]+:[0-9]+' "$ha_dir/ha.stderr.log" 2>/dev/null | sed 's/.*://')
listening=$(lsof -nP -a -p "$ha_pid" -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $9}')
extra=$(sed 's/.*://' <<<"$listening" | sort -u | grep -vxF -f <(printf '%s\n' "$ok_ports") || true)
if [[ -z "$listening" ]]; then fail "found no listener for the hostagent (pid '${ha_pid}')"
elif [[ -z "$extra" ]]; then pass "hostagent listens only on SSH and its DNS server ($(tr '\n' ' ' <<<"$listening"))"
else fail "hostagent listens on other host ports: $(tr '\n' ' ' <<<"$extra")"; fi
for who in engineer agent; do
  run=g; [[ $who == agent ]] && run=ga
  $run curl -sS -o /dev/null --max-time 5 http://host.lima.internal:22 2>/dev/null
  rc=$?
  if [[ $rc == 7 ]]; then pass "$who: host.lima.internal refused"
  else fail "$who: host.lima.internal:22 gave curl exit $rc, want 7 (refused)"; fi
done
g curl -sS -o /dev/null --max-time 5 http://192.168.0.1/ 2>/dev/null
rc=$?
if [[ $rc == 7 ]]; then pass "LAN address (192.168.0.1) refused"
else fail "192.168.0.1 gave curl exit $rc, want 7 (refused)"; fi
if g getent hosts deb.debian.org >/dev/null 2>&1; then pass "DNS resolves"
else fail "DNS does not resolve"; fi

echo ""
echo "--- agent: no Docker, no sudo, HTTPS only ---"
groups=$(ga id -nG | tr -d '\r')
if grep -qwE 'docker|sudo' <<<"$groups"; then fail "agent groups: $groups"
else pass "agent is in neither docker nor sudo ($groups)"; fi
if ga sudo -n true >/dev/null 2>&1; then fail "agent can sudo"; else pass "agent cannot sudo"; fi
if ga curl -sS --max-time 5 --unix-socket /var/run/docker.sock http://localhost/version >/dev/null 2>&1; then
  fail "agent reaches the Docker socket"
else pass "agent cannot reach the Docker socket"; fi
if g docker version >/dev/null 2>&1; then pass "engineer can use Docker"
else fail "engineer cannot use Docker"; fi
if ga curl -sS -o /dev/null --max-time 10 https://deb.debian.org/ 2>/dev/null; then pass "agent: HTTPS out works"
else fail "agent: HTTPS to deb.debian.org failed"; fi
ga curl -sS -o /dev/null --max-time 5 http://deb.debian.org:8080/ 2>/dev/null
rc=$?
if [[ $rc == 7 ]]; then pass "agent: TCP 8080 out refused"
else fail "agent: TCP 8080 out gave curl exit $rc, want 7 (refused)"; fi
# An nft reject in the output hook drops the packet before it leaves, so
# send() gets EPERM.
r=$(ga python3 -c "$UDP_PROBE" 1.1.1.1 53 | tr -d '\r')
if [[ "$r" =~ ^(EPERM|REFUSED)$ ]]; then pass "agent: UDP to a public resolver blocked ($r)"
else fail "agent: UDP to 1.1.1.1:53 gave $r, want EPERM or REFUSED"; fi

echo ""
echo "--- DNS channel in the nono sandbox (docs/lima-vm.md) ---"
# The resolver that the agent's processes use. nono runs as the agent with
# the real profile; only --sandbox-policy differs.
resolver=$(g awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf | tr -d '\r')
info "resolver in /etc/resolv.conf: $resolver"
nono_probe() {  # $@ extra nono flags
  # Not /home/agent: nono refuses a grant that holds its state root
  # (~/.local/state/nono).
  ga env -C /home/agent/work PATH=/usr/local/bin:/usr/bin:/bin \
    nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json --workdir /home/agent/work "$@" \
    -- python3 -c "$UDP_PROBE" "$resolver" 53 2>&1 | tr -d '\r' | tail -n1
}
r=$(nono_probe)
if [[ "$r" == ANSWERED ]]; then fail "default sandbox: UDP DNS from inside nono is answered"
elif [[ "$r" =~ ^(EPERM|EACCES|REFUSED|TIMEOUT)$ ]]; then pass "default sandbox: UDP DNS from inside nono blocked ($r)"
else fail "default sandbox: probe did not run: $r"; fi
r=$(nono_probe --sandbox-policy landlock)
if [[ "$r" == ANSWERED ]]; then info "--landlock-only (as --docker uses): UDP DNS answered. This is the documented DNS channel."
elif [[ "$r" =~ ^(EPERM|EACCES|REFUSED|TIMEOUT)$ ]]; then info "--landlock-only: UDP DNS blocked ($r). The documented DNS channel did not reproduce; update docs/lima-vm.md."
else fail "--landlock-only: probe did not run: $r"; fi

echo ""
echo "--- push / pull round trip ---"
NAME="smoke-$$"
SRC=$(mktemp -d)
WS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/wmf-claude/vm-$VM-workspaces"
cleanup() {
  ga rm -rf "/home/agent/work/$NAME" 2>/dev/null
  if [[ -f "$WS_FILE" ]]; then
    awk -F'\t' -v n="$NAME" '$1 != n' "$WS_FILE" > "$WS_FILE.new" && mv "$WS_FILE.new" "$WS_FILE"
  fi
  rm -rf "$SRC"
}
trap cleanup EXIT
git -C "$SRC" init -q -b main
git -C "$SRC" -c user.name=smoke -c user.email=smoke@invalid commit -q --allow-empty -m "smoke: base"
printf '#!/bin/sh\necho planted\n' > "$SRC/.git/hooks/post-checkout"
chmod +x "$SRC/.git/hooks/post-checkout"
echo untracked > "$SRC/untracked.txt"
if "$VMCMD" push "$SRC" "$NAME" >/dev/null 2>&1; then pass "push"
else fail "push failed"; fi
if ga test -e "/home/agent/work/$NAME/untracked.txt"; then fail "push carried an untracked file"
else pass "push carried no untracked file"; fi
if ga test -e "/home/agent/work/$NAME/.git/hooks/post-checkout"; then fail "push carried a hook"
else pass "push carried no hook"; fi
rm -f "$SRC/.git/hooks/post-checkout"
# The agent commits on a new branch, as a session would.
ga bash -c 'cd "$1" && git switch -q -c smoke-branch && echo agent > agent.txt && git add agent.txt &&
  git -c user.name=agent -c user.email=agent@invalid commit -q -m "smoke: agent commit"' _ "/home/agent/work/$NAME"
head_before=$(git -C "$SRC" rev-parse HEAD)
if "$VMCMD" pull "$NAME" >/dev/null 2>&1; then pass "pull (destination from the push record)"
else fail "pull failed"; fi
if [[ "$(git -C "$SRC" log -1 --format=%s vm/smoke-branch 2>/dev/null)" == "smoke: agent commit" ]]; then
  pass "pull fetched vm/smoke-branch"
else fail "vm/smoke-branch missing or wrong"; fi
if [[ "$(git -C "$SRC" rev-parse HEAD)" == "$head_before" && ! -e "$SRC/agent.txt" ]]; then
  pass "pull left HEAD and the working tree alone"
else fail "pull changed HEAD or the working tree"; fi
if GIT_PAGER=cat "$VMCMD" review "$NAME" smoke-branch 2>/dev/null | grep -q 'smoke: agent commit'; then pass "review"
else fail "review did not show the agent commit"; fi
if "$VMCMD" checkout "$NAME" smoke-branch >/dev/null 2>&1 && [[ -f "$SRC/agent.txt" ]]; then pass "checkout"
else fail "checkout failed"; fi
git -C "$SRC" switch -q main
if "$VMCMD" discard "$NAME" smoke-branch >/dev/null 2>&1 \
   && ! git -C "$SRC" rev-parse -q --verify refs/remotes/vm/smoke-branch >/dev/null; then pass "discard"
else fail "discard failed"; fi
# A second push to a workspace must not overwrite the agent's branch.
git -C "$SRC" -c user.name=smoke -c user.email=smoke@invalid commit -q --allow-empty -m "smoke: host 2"
"$VMCMD" push "$SRC" "$NAME" >/dev/null 2>&1
if [[ "$(ga git -C "/home/agent/work/$NAME" log -1 --format=%s smoke-branch | tr -d '\r')" == "smoke: agent commit" \
   && "$(ga git -C "/home/agent/work/$NAME" log -1 --format=%s host/main | tr -d '\r')" == "smoke: host 2" ]]; then
  pass "second push updates host/* and keeps the agent's branch"
else fail "second push did not update host/main, or changed the agent's branch"; fi

if $WITH_CLAUDE; then
  echo ""
  echo "--- sandboxed Claude Code session ---"
  out=$("$VMCMD" claude "$NAME" -- -p 'Reply with exactly SMOKE-OK and nothing else.' 2>&1)
  if grep -q 'SMOKE-OK' <<<"$out"; then pass "claude -p in the VM"
  else fail "claude -p in the VM: $(tail -n3 <<<"$out")"; fi
fi

echo ""
echo "--- not automated ---"
echo "  --docker: bin/wmf-claude-vm shell; start a compose project in /home/agent/work/<repo>"
echo "  (docs/lima-vm.md#docker-inside-the-vm), then: bin/wmf-claude-vm claude <repo> --docker=<service>"
echo "  and ask Claude to run mwdocker php --version."

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
