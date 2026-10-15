#!/bin/bash
# Live checks for Lima VM mode against a real VM. tests/test-lima.sh checks
# the files; this script checks what a running VM enforces. Run it on the
# host, outside nono: limactl needs ~/.lima, which the sandbox denies.
#
# The VM must exist and run (bin/wmf-claude-vm create). The script creates a
# throwaway repository, pushes it as workspace smoke-<pid>, and removes the
# workspace at the end. It does not change your other workspaces or repos.
#
# Usage: ./tests/smoke-lima.sh [--docker] [--claude]
#   --docker   also test the Docker broker: a php:8.3-cli container, the
#              broker as the engineer, mwdocker as the agent inside nono
#              (pulls the image on first use)
#   --claude   also run one sandboxed `claude -p` turn in the VM (needs
#              bin/wmf-claude-vm login first; costs one short model call).
#              With --docker, also one turn that runs mwdocker.
#
#   WMF_CLAUDE_VM=<name>   Lima instance name (default: wmf-claude)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VMCMD="$REPO_ROOT/bin/wmf-claude-vm"
VM="${WMF_CLAUDE_VM:-wmf-claude}"
WITH_CLAUDE=false
WITH_DOCKER=false
for a in "$@"; do
  case "$a" in
    --claude) WITH_CLAUDE=true ;;
    --docker) WITH_DOCKER=true ;;
    *) echo "usage: $0 [--docker] [--claude]" >&2; exit 2 ;;
  esac
done

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
# The profile asks for signal_mode: isolated and shared-memory-only IPC. nono
# enforces those through Landlock scoping only on ABI V6 (kernel 6.12+); below
# that it drops them silently, with no error. Assert the guest kernel has it.
kver=$(g uname -r | tr -d '\r'); kmaj=${kver%%.*}; krest=${kver#*.}; kmin=${krest%%.*}
if [[ "$kmaj" =~ ^[0-9]+$ && "$kmin" =~ ^[0-9]+$ ]] && { (( kmaj > 6 )) || { (( kmaj == 6 )) && (( kmin >= 12 )); }; }; then
  pass "guest kernel $kver has Landlock ABI V6 (signal + IPC scoping enforce)"
else fail "guest kernel '${kver:-missing}' is below 6.12; signal_mode/ipc scoping silently does not enforce"; fi

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
echo "--- nono network filter: DNS and localhost ports ---"
# The resolver that the agent's processes use. nono runs as the agent with
# the real profile. A --docker session adds one --open-port for the broker.
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
r=$(nono_probe --open-port 8765)
if [[ "$r" == ANSWERED ]]; then fail "with --open-port (as --docker): UDP DNS from inside nono is answered"
elif [[ "$r" =~ ^(EPERM|EACCES|REFUSED|TIMEOUT)$ ]]; then pass "with --open-port (as --docker): UDP DNS from inside nono blocked ($r)"
else fail "with --open-port: probe did not run: $r"; fi
# A granted localhost port answers; others do not.
ports=$(ga bash -c '
  cd /home/agent/work
  python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 & srv=$!
  sleep 1
  n() { nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json --workdir /home/agent/work "$@"; }
  printf "granted=%s " "$(n --open-port 8765 -- curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/ 2>/dev/null)"
  printf "ungranted=%s\n" "$(n -- curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/ 2>/dev/null)"
  kill "$srv"' | tr -d '\r')
if [[ "$ports" == "granted=200 ungranted=000" ]]; then pass "localhost ports: a granted port answers, an ungranted one does not"
else fail "localhost ports: $ports, want granted=200 ungranted=000"; fi
# The other direction: a process inside nono binds a granted port (the VM
# serves the wiki from the session this way) and the engineer reaches it.
bound=$(g bash -c '
  sudo -H -u agent -- bash -c "cd /home/agent/work && exec nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json --workdir /home/agent/work --open-port 8767 -- python3 -m http.server 8767 --bind 127.0.0.1" >/dev/null 2>&1 & srv=$!
  for _ in $(seq 50); do curl -s -o /dev/null http://127.0.0.1:8767/ && break; sleep 0.1; done
  curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8767/
  sudo -n pkill -u agent -f "http.server 8767" 2>/dev/null; wait "$srv" 2>/dev/null' 2>/dev/null | tr -d '\r' | tail -n1)
if [[ "$bound" == 200 ]]; then pass "in nono: a granted port can be bound and served"
else fail "in nono: binding a granted port failed (curl got '$bound')"; fi
# An open port is for localhost only. With 443 granted, a direct connect to
# an outside host on 443 must still fail: nono checks the destination, not
# only the port. The nft rule lets the agent out on 443, so a deny here comes
# from nono. Resolve outside the sandbox (it has no DNS), and use a raw
# socket, which does not use the proxy.
TCP_PROBE='
import socket, sys, errno
try:
    socket.create_connection((sys.argv[1], 443), timeout=5).close()
    print("CONNECTED")
except socket.timeout:
    print("TIMEOUT")
except OSError as e:
    print(errno.errorcode.get(e.errno, str(e.errno)))
'
ext_ip=$(g getent ahostsv4 deb.debian.org | awk 'NR==1 {print $1}' | tr -d '\r')
r=$(ga env -C /home/agent/work PATH=/usr/local/bin:/usr/bin:/bin \
  nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json --workdir /home/agent/work --open-port 443 \
  -- python3 -c "$TCP_PROBE" "$ext_ip" 2>&1 | tr -d '\r' | tail -n1)
if [[ "$r" == CONNECTED ]]; then fail "with --open-port 443: a direct connect to $ext_ip:443 got through"
elif [[ "$r" =~ ^(EPERM|EACCES|ECONNREFUSED|TIMEOUT)$ ]]; then pass "with --open-port 443: a direct connect to an outside host is blocked ($r)"
else fail "with --open-port 443: probe did not run: $r"; fi

echo ""
echo "--- PHP and MariaDB ---"
phpv=$(ga php -r 'echo PHP_VERSION;' 2>/dev/null | tr -d '\r')
if [[ "$phpv" =~ ^8\.[3-9] ]]; then pass "php $phpv as the agent"
else fail "php as the agent: '${phpv:-missing}', want 8.3 or newer"; fi
if ga composer --version >/dev/null 2>&1; then pass "composer as the agent"
else fail "composer missing for the agent"; fi
listen=$(g ss -H -ltn 'sport = :3306' 2>/dev/null | awk '{print $4}' | tr -d '\r' | sort -u | tr '\n' ' ')
if [[ "$listen" == "127.0.0.1:3306 " ]]; then pass "MariaDB listens on 127.0.0.1:3306 only"
else fail "MariaDB listens on '${listen:-nothing}', want 127.0.0.1:3306 only"; fi
if ga mariadb -h 127.0.0.1 -u wikiuser -pwikipass -e 'SELECT 1' wikidb >/dev/null 2>&1; then pass "agent: wikiuser connects over TCP"
else fail "agent: wikiuser cannot connect over TCP"; fi
if ga mariadb -u root -e 'SELECT 1' >/dev/null 2>&1; then fail "agent can use the MariaDB root account"
else pass "agent cannot use the MariaDB root account"; fi
# In nono, 3306 answers with the grant --local-db makes, and not without.
# A raw connect first, which tells a nono deny from a client problem (the
# server speaks first, so recv proves it is mariadbd); then the client,
# whose error the failure message shows.
DB_PROBE='
import socket, errno
try:
    s = socket.create_connection(("127.0.0.1", 3306), timeout=5)
    s.recv(1)
    s.close()
    print("CONNECTED")
except OSError as e:
    print(errno.errorcode.get(e.errno, str(e)))
'
# From the agent's work directory: with no XDG_RUNTIME_DIR, nono resolves
# the base profile's $XDG_RUNTIME_DIR/cc-socks grant against the cwd, and
# the engineer's home is closed to the agent.
raw=$(ga bash -c '
  cd /home/agent/work
  n() { nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json --workdir /home/agent/work "$@"; }
  printf "granted=%s ungranted=%s\n" \
    "$(n --open-port 3306 -- python3 -c "$1" 2>&1 | tail -n1)" \
    "$(n -- python3 -c "$1" 2>&1 | tail -n1)"' _ "$DB_PROBE" | tr -d '\r')
if [[ "$raw" == "granted=CONNECTED ungranted="* && "$raw" != "granted=CONNECTED ungranted=CONNECTED" ]]; then
  pass "in nono: TCP 3306 connects with the --local-db grant, not without ($raw)"
else fail "in nono: TCP 3306: $raw, want granted=CONNECTED and an ungranted deny"; fi
# --batch: one value per line whether or not a terminal is attached (limactl
# shell may attach one, and the client then draws a table). The exit code
# is information only: nono's own code is not the child's in every mode.
client=$(ga bash -c '
  cd /home/agent/work
  nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json --workdir /home/agent/work --open-port 3306 \
    -- mariadb -h 127.0.0.1 -u wikiuser -pwikipass --batch --skip-column-names -e "SELECT 42" wikidb 2>&1
  echo "rc=$?"' | tr -d '\r' | tail -n2 | tr '\n' ' ')
if [[ "$client" == "42 rc="* ]]; then pass "in nono: the mariadb client reaches wikidb with the grant (nono ${client#42 })"
else fail "in nono: the mariadb client with the grant said: '$client'"; fi

echo ""
echo "--- headless Chromium: loopback only ---"
cv=$(g chromium --version 2>/dev/null | tr -d '\r')
if [[ "$cv" == Chromium* ]]; then pass "$cv"
else fail "chromium missing in the guest: '${cv:-nothing}'"; fi
cg=$(g id -nG wmfchrome 2>/dev/null | tr -d '\r')
if [[ "$cg" == wmfchrome ]]; then pass "browser user wmfchrome is in no other group"
else fail "browser user groups: '${cg:-missing}', want wmfchrome only"; fi
if g sudo -n nft list table inet wmf_claude_chrome >/dev/null 2>&1; then pass "browser firewall table loaded"
else fail "nft table wmf_claude_chrome missing"; fi
# A local server as the agent: the browser must reach it, and only it.
# The firewall check drops the proxy and resolver flags, so the rule
# alone has to refuse the outside address (resolved outside the browser).
# As the engineer (sudo to the browser user); the server serves an empty
# temporary directory.
cd_out=$(g bash -c '
  d=$(mktemp -d) && cd "$d" || exit 1
  python3 -m http.server 8766 --bind 127.0.0.1 >/dev/null 2>&1 & srv=$!
  sleep 1
  sudo -n -u wmfchrome /opt/wmf-claude/lima/guest-chrome.sh dump-dom http://127.0.0.1:8766/ 2>&1
  kill "$srv"; rmdir "$d"' 2>/dev/null | tr -d '\r')
if grep -q "Directory listing" <<<"$cd_out"; then pass "browser reaches a loopback server"
else fail "browser did not render the loopback server; last lines: $(tail -n 3 <<<"$cd_out" | tr '\n' ' ' | head -c 300)"; fi
# A failed navigation prints no DOM at all (Chromium's stderr is kept, so a
# browser that does not start says why). The outside name must not render
# the index. The firewall is proven by its counter: without the proxy and
# resolver flags, the packets to an outside address hit the reject rule.
chrome_counter() { g sudo -n nft list table inet wmf_claude_chrome 2>/dev/null | tr -d '\r' | grep -oE 'counter packets [0-9]+' | awk '{print $3; exit}'; }
ext_dom=$(g sudo -n -u wmfchrome /opt/wmf-claude/lima/guest-chrome.sh dump-dom http://deb.debian.org/debian/ 2>&1 | tr -d '\r')
if ! grep -q 'Index of' <<<"$ext_dom"; then pass "browser: an outside name does not render"
else fail "browser rendered deb.debian.org through the proxy and resolver flags"; fi
if [[ -n "$ext_ip" ]]; then
  before=$(chrome_counter)
  g sudo -n -u wmfchrome /opt/wmf-claude/lima/guest-chrome.sh dump-dom-unproxied "http://$ext_ip/" >/dev/null 2>&1
  after=$(chrome_counter)
  if [[ "$before" =~ ^[0-9]+$ && "$after" =~ ^[0-9]+$ ]] && (( after > before )); then
    pass "browser firewall alone rejected the packets to $ext_ip ($((after - before)) counted)"
  else fail "browser firewall counter did not move for $ext_ip (before '${before:-?}', after '${after:-?}')"; fi
else
  fail "browser firewall check skipped: no outside address resolved"
fi
# CDP from inside nono, with the grant --chrome makes.
cdp=$(g bash -c '
  sudo -n -u wmfchrome /opt/wmf-claude/lima/guest-chrome.sh cdp >/dev/null 2>&1 & b=$!
  for _ in $(seq 100); do curl -s -o /dev/null http://127.0.0.1:9222/json/version && break; sleep 0.1; done
  sudo -H -u agent -- bash -c "cd /home/agent/work && nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json --workdir /home/agent/work --open-port 9222 -- curl -s http://127.0.0.1:9222/json/version" | grep -c Browser
  kill "$b"; wait "$b" 2>/dev/null' 2>/dev/null | tr -d '\r' | tail -n1)
if [[ "$cdp" == 1 ]]; then pass "in nono: CDP answers on 127.0.0.1:9222 with the --chrome grant"
else fail "in nono: CDP did not answer (grep count '$cdp')"; fi

echo ""
echo "--- push / pull round trip ---"
NAME="smoke-$$"
SRC=$(mktemp -d)
WS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/wmf-claude/vm-$VM-workspaces"
DNAME="smoke-docker-$$"
DSRC=""
cleanup() {
  if [[ -n "$DSRC" ]]; then
    g bash -c 'cd "$1" 2>/dev/null && docker compose down -t 1 >/dev/null 2>&1' _ "/home/agent/work/$DNAME"
    ga rm -rf "/home/agent/work/$DNAME" 2>/dev/null
    rm -rf "$DSRC"
  fi
  ga rm -rf "/home/agent/work/$NAME" 2>/dev/null
  if [[ -f "$WS_FILE" ]]; then
    awk -F'\t' -v n="$NAME" -v d="$DNAME" '$1 != n && $1 != d' "$WS_FILE" > "$WS_FILE.new" && mv "$WS_FILE.new" "$WS_FILE"
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
# Until pulled, status reports the branch; a changed file is reported as
# uncommitted, since a bundle would not carry it.
ga bash -c 'echo dirty > "$1/dirty.txt"' _ "/home/agent/work/$NAME"
st=$("$VMCMD" status 2>/dev/null)
if grep -qE "$NAME +smoke-branch +1 commit not pulled" <<<"$st"; then pass "status lists the agent's branch as not pulled"
else fail "status does not list smoke-branch as not pulled"; fi
if grep -qE "$NAME +\(working tree\) +1 uncommitted file" <<<"$st"; then pass "status lists the uncommitted file"
else fail "status does not list the uncommitted file"; fi
ga rm -f "/home/agent/work/$NAME/dirty.txt"
head_before=$(git -C "$SRC" rev-parse HEAD)
if "$VMCMD" pull "$NAME" >/dev/null 2>&1; then pass "pull (destination from the push record)"
else fail "pull failed"; fi
if "$VMCMD" status 2>/dev/null | grep -qE "$NAME +smoke-branch"; then fail "status still lists smoke-branch after pull"
else pass "status drops the branch once pulled"; fi
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

if $WITH_DOCKER; then
  echo ""
  echo "--- Docker broker ---"
  # A throwaway compose project: one PHP container that runs as the agent's
  # UID and mounts the workspace, as MediaWiki-Docker does.
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
  if "$VMCMD" push "$DSRC" "$DNAME" >/dev/null 2>&1; then pass "push the compose project"
  else fail "push the compose project failed"; fi
  ga bash -c 'printf "MW_DOCKER_UID=%s\nMW_DOCKER_GID=%s\n" "$(id -u)" "$(id -g)" > "$1/.env"' _ "$DW"
  if g bash -c 'cd "$1" && docker compose up -d --quiet-pull >/dev/null 2>&1' _ "$DW"; then pass "engineer starts the container"
  else fail "docker compose up failed in $DW"; fi

  # The broker runs as the engineer, as lima/guest-claude.sh starts it.
  # mwdocker runs as the agent inside nono with the real profile and the
  # broker's port open, as bin/claude --docker runs it. Each line of output
  # is KEY=VALUE.
  BROKER_CHECKS='
    W=$1; RUN=/run/wmf-claude; HS=$RUN/smoke.$$.json; LOG=$RUN/smoke.$$.log
    (umask 077; exec python3 /opt/wmf-claude/bin/launch-docker-broker --service mediawiki \
      --compose-file "$W/docker-compose.yml" --handshake "$HS" >"$LOG" 2>&1) &
    bp=$!
    for _ in $(seq 100); do [ -f "$HS" ] && break; sleep 0.1; done
    if [ ! -f "$HS" ]; then echo "BROKER=failed: $(tr "\n" " " < "$LOG")"; kill "$bp" 2>/dev/null; rm -f "$LOG"; exit 0; fi
    echo "BROKER=up"
    port=$(jq -r .port "$HS"); tok=$(jq -r .token "$HS")
    agent() { sudo -H -u agent env -C "$W" PATH=/usr/local/bin:/usr/bin:/bin \
      WMF_DOCKER_BROKER_URL="http://127.0.0.1:$port" WMF_DOCKER_BROKER_TOKEN="$tok" "$@"; }
    sandboxed() { agent nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json \
      --workdir "$W" --read /opt/wmf-claude --open-port "$port" -- "$@"; }
    echo "PHP=$(sandboxed /opt/wmf-claude/bin/mwdocker php -r "echo PHP_VERSION;" 2>&1 | tail -n1)"
    sandboxed /opt/wmf-claude/bin/mwdocker php -r "file_put_contents(\"from-container.txt\", \"ok\");" >/dev/null 2>&1
    echo "OWNER=$(stat -c %U "$W/from-container.txt" 2>/dev/null)"
    echo "BASH=$(sandboxed /opt/wmf-claude/bin/mwdocker bash -c id 2>&1 | tr "\n" " ")"
    sandboxed curl -s --max-time 5 --unix-socket /var/run/docker.sock http://localhost/version >/dev/null 2>&1
    echo "SOCKET=$?"
    echo "NOGRANT=$(agent nono run --silent --profile /opt/wmf-claude/profiles/wmf-engineer.json --workdir "$W" \
      -- curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port/health" 2>/dev/null)"
    kill "$bp" 2>/dev/null; wait "$bp" 2>/dev/null; rm -f "$HS" "$LOG"
  '
  out=$(g bash -c "$BROKER_CHECKS" _ "$DW" | tr -d '\r')
  val() { sed -n "s/^$1=//p" <<<"$out" | head -n1; }
  if [[ "$(val BROKER)" == up ]]; then pass "broker starts as the engineer"
  else fail "broker did not start: $(val BROKER)"; fi
  if [[ "$(val PHP)" =~ ^8\.3\. ]]; then pass "mwdocker php in nono reaches the container (PHP $(val PHP))"
  else fail "mwdocker php in nono: '$(val PHP)'"; fi
  if [[ "$(val OWNER)" == agent ]]; then pass "the container writes as the agent's UID"
  else fail "from-container.txt owner is '$(val OWNER)', want agent"; fi
  if grep -q 'not allowed' <<<"$(val BASH)"; then pass "mwdocker refuses a bin off the allowlist (bash)"
  else fail "mwdocker bash: '$(val BASH)'"; fi
  if [[ "$(val SOCKET)" != 0 ]]; then pass "the agent in nono cannot reach the Docker socket"
  else fail "the agent in nono reached the Docker socket"; fi
  if [[ "$(val NOGRANT)" == 000 ]]; then pass "without --open-port, the sandbox cannot reach the broker"
  else fail "without --open-port, the broker answered: '$(val NOGRANT)'"; fi

  # The broker must refuse a container that mounts the Docker socket.
  ga sed -i 's#      - ./:/var/www/html/w#      - ./:/var/www/html/w\n      - /var/run/docker.sock:/var/run/docker.sock#' "$DW/docker-compose.yml"
  g bash -c 'cd "$1" && docker compose up -d --quiet-pull >/dev/null 2>&1' _ "$DW"
  refusal=$(g bash -c 'timeout 30 python3 /opt/wmf-claude/bin/launch-docker-broker --service mediawiki \
    --compose-file "$1/docker-compose.yml" --handshake "/run/wmf-claude/smoke-refuse.$$.json" 2>&1; rm -f /run/wmf-claude/smoke-refuse.$$.json' _ "$DW" | tr -d '\r')
  if grep -q 'refusing to serve' <<<"$refusal"; then pass "broker refuses a container with the Docker socket mounted"
  else fail "broker did not refuse the socket mount: $(tail -n2 <<<"$refusal")"; fi
  # Back to the safe compose file for the --claude turn below.
  ga git -C "$DW" checkout -q -- docker-compose.yml
  g bash -c 'cd "$1" && docker compose up -d --quiet-pull >/dev/null 2>&1' _ "$DW"
fi

if $WITH_CLAUDE; then
  echo ""
  echo "--- sandboxed Claude Code session ---"
  out=$("$VMCMD" claude "$NAME" -- -p 'Reply with exactly SMOKE-OK and nothing else.' 2>&1)
  if grep -q 'SMOKE-OK' <<<"$out"; then pass "claude -p in the VM"
  else fail "claude -p in the VM: $(tail -n3 <<<"$out")"; fi
  if $WITH_DOCKER; then
    # The full path: lima/guest-claude.sh starts the broker, bin/claude runs
    # as the agent, and Claude calls mwdocker.
    out=$("$VMCMD" claude "$DNAME" --docker=mediawiki -- -p \
      "Run this exact shell command and reply with only its output: mwdocker php -r 'echo \"SMOKE-\", PHP_VERSION;'" \
      --allowedTools 'Bash(mwdocker:*)' 2>&1)
    if grep -qE 'SMOKE-8\.3\.' <<<"$out"; then pass "claude --docker in the VM runs mwdocker"
    else fail "claude --docker in the VM: $(tail -n3 <<<"$out")"; fi
  fi
fi

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
