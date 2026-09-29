#!/bin/bash
# Static checks for the optional Lima VM mode (bin/wmf-claude-vm, lima/):
# the template stays closed (no mounts, no port forwards, a pinned image with
# digests), the guest scripts pin and verify what they install, the agent
# user never joins `docker`, and the scripts parse. The real behaviour needs a
# VM: docs/lima-vm.md#verifying-the-boundary. `limactl validate` and
# shellcheck run when they are installed.
#
# Usage: ./tests/test-lima.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }

pass() { green "PASS: $*"; ((PASS++)); }
fail() { red   "FAIL: $*"; ((FAIL++)); }
skip() { printf 'SKIP: %s\n' "$*"; }

Y="$REPO_ROOT/lima/wmf-claude.yaml"
VM="$REPO_ROOT/bin/wmf-claude-vm"
GI="$REPO_ROOT/lima/guest-install.sh"
GC="$REPO_ROOT/lima/guest-claude.sh"

# `-e`: several patterns start with `--`.
check()  { if grep -qE -e "$3" "$2"; then pass "$1"; else fail "$1"; fi; }   # $1 desc  $2 file  $3 must match
absent() { if grep -qE -e "$3" "$2"; then fail "$1"; else pass "$1"; fi; }   # $1 desc  $2 file  $3 must not match

echo "=== Lima VM mode ==="
echo ""
echo "--- template: closed by construction ---"
check  "plain mode on"                              "$Y" '^plain: true$'
check  "no host mounts (mounts: [])"                "$Y" '^mounts: \[\]$'
absent "no rosetta key (it adds a host share)"      "$Y" '^[[:space:]]*rosetta:'
absent "no vmOpts key (rosetta lives there too)"    "$Y" '^[[:space:]]*vmOpts:'
absent "no networks key (user-mode network only)"   "$Y" '^networks:'
absent "no guest socket forwards"                   "$Y" 'guestSocket'
check  "port forwards: an ignore-all rule"          "$Y" '^[[:space:]]+ignore: true$'
n_rules=$(awk '/^portForwards:/{p=1;next} p&&/^[^ -]/{p=0} p&&/^- /{c++} END{print c+0}' "$Y")
if [[ "$n_rules" == 1 ]]; then pass "port forwards: exactly one rule (the ignore rule)"; else fail "port forwards: $n_rules rules, want 1"; fi
check  "containerd: system off"                     "$Y" '^[[:space:]]+system: false$'
check  "containerd: user off"                       "$Y" '^[[:space:]]+user: false$'
check  "ssh: no agent forwarding"                   "$Y" '^[[:space:]]+forwardAgent: false$'
check  "ssh: no X11 forwarding"                     "$Y" '^[[:space:]]+forwardX11: false$'
check  "ssh: host ~/.ssh pubkeys not loaded"        "$Y" '^[[:space:]]+loadDotSSHPubKeys: false$'

echo ""
echo "--- template: image pinning ---"
absent "no latest/daily image URL"                  "$Y" 'location:.*/(latest|daily)/'
n_loc=$(grep -c '^- location:' "$Y")
n_dig=$(grep -cE '^[[:space:]]+digest: "sha512:[0-9a-f]{128}"$' "$Y")
if [[ "$n_loc" -ge 2 && "$n_loc" == "$n_dig" ]]; then
  pass "every image ($n_loc) has a sha512 digest"
else
  fail "images: $n_loc locations, $n_dig sha512 digests"
fi
check  "x86_64 image"                               "$Y" '^[[:space:]]+arch: "x86_64"$'
check  "aarch64 image"                              "$Y" '^[[:space:]]+arch: "aarch64"$'
# The release directory and the file name must name the same release.
BAD=""
while IFS= read -r loc; do
  rel=$(sed -E 's#.*/trixie/([0-9]+-[0-9]+)/.*#\1#' <<<"$loc")
  [[ "$loc" == *"-$rel.qcow2\"" ]] || BAD+="$loc "
done < <(grep '^- location:' "$Y")
if [[ -z "$BAD" ]]; then pass "image URLs name one versioned release in path and file name"; else fail "image URL not versioned: $BAD"; fi

echo ""
echo "--- template: users and Docker ---"
check  "agent user is created"                      "$Y" '--shell /bin/bash "\$AGENT"$'
check  "engineer joins docker"                      "$Y" 'usermod -aG docker "\$ENGINEER"'
absent "agent never joins docker"                   "$Y" 'usermod .*docker.*\$AGENT|usermod .*\$AGENT.*docker'
check  "agent is removed from sudo/docker if found" "$Y" 'gpasswd -d "\$AGENT"'
check  "readiness probe: agent not in docker"       "$Y" '! id -nG agent \| grep -qw docker'
check  "agent home is closed to others"             "$Y" 'chmod 0750 "/home/\$AGENT"'
check  "broker handshake dir is group-shared"       "$Y" 'd /run/wmf-claude 2750 \$ENGINEER \$BROKER_GROUP'

echo ""
echo "--- template: host-loopback block ---"
check  "nft table"                                  "$Y" 'table inet wmf_claude_hostblock \{'
check  "replies to host-opened connections pass"    "$Y" 'ct state established,related accept'
check  "DNS and DHCP to Lima's subnet are allowed"  "$Y" 'ip daddr \$NET udp dport \{ 53, 67 \} accept'
check  "private IPv4 (host LAN, VPN) is refused"    "$Y" 'ip daddr @private4 counter reject'
check  "the block applies only to the uplink"       "$Y" 'oifname "\$DEV" jump block'
check  "TIOCSTI injection is off"                   "$Y" 'dev.tty.legacy_tiocsti = 0'
check  "forward chain covers containers"            "$Y" 'hook forward'
absent "no flush ruleset (would drop Docker rules)" "$Y" '^[[:space:]]*flush ruleset'
check  "nftables enabled at boot"                   "$Y" 'systemctl enable nftables'

echo ""
echo "--- no permissive profile ---"
for f in "$Y" "$GI" "$GC" "$VM"; do
  n="${f#"$REPO_ROOT"/}"
  absent "$n: no api.minimax.io"                    "$f" 'minimax'
  absent "$n: no network_profile"                   "$f" 'network_profile'
  absent "$n: no *.wikimedia.org wildcard"          "$f" '\*\.wikimedia\.org'
  absent "$n: no WMF_CLAUDE_PROFILE override"       "$f" 'WMF_CLAUDE_PROFILE='
done
if ls "$REPO_ROOT/profiles/" | grep -qi lima; then
  fail "a Lima-specific profile exists under profiles/ (the VM reuses wmf-engineer)"
else
  pass "no Lima-specific profile under profiles/"
fi

echo ""
echo "--- guest-install.sh: pinned, verified installs ---"
check  "nono version comes from .nono-version"      "$GI" '\.nono-version'
check  "nono .deb checked against SHA256SUMS.txt"   "$GI" 'SHA256SUMS\.txt'
check  "sha256sum -c is strict"                     "$GI" 'sha256sum -c --strict'
absent "no releases/latest"                         "$GI" 'releases/latest'
n_pipe=$(grep -cE 'curl .*\| *(ba)?sh' "$GI")
n_off=$(grep -cF 'curl -fsSL https://claude.ai/install.sh | bash' "$GI")
if [[ "$n_pipe" == 1 && "$n_off" == 1 ]]; then
  pass "the only curl|bash is Claude Code's official installer"
else
  fail "curl|bash: $n_pipe uses, $n_off of them the official Claude Code installer"
fi
check  "chrome MCP build skipped in the guest"      "$GI" 'WMF_CLAUDE_SKIP_CHROME=1'
check  "per-user setup as agent, build skipped"     "$GI" 'WMF_CLAUDE_SKIP_BUILD=1'
check  "agent runs bin/wmf-claude-setup itself"     "$GI" 'as_agent WMF_CLAUDE_SKIP_BUILD=1 .*wmf-claude-setup'
absent "no agent login shell (dotfiles run outside nono)" "$GI" 'bash -lc'
absent "guest-claude.sh: no agent login shell"      "$GC" 'bash -lc'
absent "root never writes under the agent's home"   "$Y" '(install|chown|chmod)[^#]*/home/\$AGENT/'
check  "the agent gets TCP 80/443 only"             "$Y" 'meta skuid \$AGENT_UID jump agent_egress'
absent "no fixed /tmp upload path"                  "$VM" '/tmp/wmf-claude-(tree|push)\.\$\$'
absent "no username file (setup asks, as on the host)" "$GI" 'phab_user'
check  "tree swapped by symlink after the build"    "$GI" 'mv -T "\$INSTALL.new" "\$INSTALL"'
check  "server.log handed to the agent"             "$GI" 'sudo chown "\$AGENT:\$AGENT" "\$LOGF"'
check  "wmf-claude-setup honors WMF_CLAUDE_SKIP_BUILD" "$REPO_ROOT/bin/wmf-claude-setup" 'WMF_CLAUDE_SKIP_BUILD'

echo ""
echo "--- bin/wmf-claude-vm: copies, not mounts ---"
absent "no --rosetta"                               "$VM" '--rosetta'
absent "no --mount flag to limactl"                 "$VM" '--mount'
absent "no --port-forward flag to limactl"          "$VM" '--port-forward'
check  "limactl runs non-interactively"             "$VM" '--tty=false'
check  "push uses git bundle"                       "$VM" 'git -C "\$top" bundle create'
check  "pull exports with hooks and fsmonitor off"  "$VM" 'core\.hooksPath=/dev/null -c core\.fsmonitor=false'
check  "pull fetches into remote-tracking vm/*"     "$VM" '\+refs/heads/\*:refs/remotes/vm/\*'
check  "pull verifies the bundle first"             "$VM" 'bundle verify "\$bundle"'
absent "pull never checks out, merges or resets"    "$VM" 'git -C "\$dest" (checkout|merge|reset|pull)'
check  "checkout never overwrites a local branch"     "$VM" 'die "\$2 exists in'
check  "push records the workspace's host repo"     "$VM" 'ws_save "\$name" "\$top"'
check  "update ships HEAD via git archive"          "$VM" 'git -C "\$ROOT" archive'
check  "update includes the submodules"             "$VM" 'submodule --quiet foreach --recursive'

echo ""
echo "--- bin/claude: attach-mode handshake override ---"
check  "bin/claude reads WMF_DOCKER_HANDSHAKE in attach mode" "$REPO_ROOT/bin/claude" 'HS_FILE="\$\{WMF_DOCKER_HANDSHAKE:-'
check  "bin/claude unsets it before the sandbox"    "$REPO_ROOT/bin/claude" '^[[:space:]]+unset WMF_DOCKER_HANDSHAKE$'

echo ""
echo "--- scripts parse ---"
for f in "$VM" "$GI" "$GC"; do
  if bash -n "$f" 2>/dev/null; then pass "bash -n ${f#"$REPO_ROOT"/}"; else fail "bash -n ${f#"$REPO_ROOT"/}"; fi
  if [[ -x "$f" ]]; then pass "executable: ${f#"$REPO_ROOT"/}"; else fail "not executable: ${f#"$REPO_ROOT"/}"; fi
done
# The provision script embedded in the template.
PROV="$(mktemp)"
awk '/^- mode: system$/{s=1;next} s&&/^  script: \|$/{p=1;next} p&&/^probes:/{exit} p{sub(/^    /,""); print}' "$Y" > "$PROV"
if [[ -s "$PROV" ]] && bash -n "$PROV" 2>/dev/null; then pass "bash -n embedded provision script"; else fail "embedded provision script does not parse"; fi
if [[ -z "$(grep -oE '\{\{[^}]*\}\}' "$PROV" | grep -vx '{{.User}}')" ]]; then
  pass "provision script uses no Go template besides {{.User}}"
else
  fail "provision script has an unexpected {{...}} template: $(grep -oE '\{\{[^}]*\}\}' "$PROV" | grep -vx '{{.User}}' | tr '\n' ' ')"
fi
rm -f "$PROV"
if command -v shellcheck >/dev/null 2>&1; then
  for f in "$VM" "$GI" "$GC"; do
    if shellcheck -x "$f" >/dev/null 2>&1; then pass "shellcheck ${f#"$REPO_ROOT"/}"; else fail "shellcheck ${f#"$REPO_ROOT"/}"; shellcheck -x "$f" | sed 's/^/        /'; fi
  done
else
  skip "shellcheck not installed"
fi
if command -v limactl >/dev/null 2>&1; then
  if limactl validate "$Y" >/dev/null 2>&1; then pass "limactl validate lima/wmf-claude.yaml"; else fail "limactl validate lima/wmf-claude.yaml"; limactl validate "$Y" 2>&1 | sed 's/^/        /'; fi
else
  skip "limactl not installed; template not validated"
fi

echo ""
echo "--- lima/guest-claude.sh: agent launch ---"
check  "--chrome and --ide are refused"             "$GC" '--chrome\|--ide\)'
check  "only --docker=SERVICE is accepted"          "$GC" '--docker=\*:\*\|--docker=auto\|--docker\|--egress=\*\)'
check  "the broker runs as the engineer"            "$GC" 'python3 "\$INSTALL/bin/launch-docker-broker"'
check  "handshake is group-readable (0640)"         "$GC" 'chmod 0640 "\$HS"'
check  "--docker adds --landlock-only"              "$GC" 'AGENT_ARGS=\(--docker --landlock-only'
check  "the agent runs with a fixed PATH"           "$GC" 'sudo -H -u "\$AGENT" env -C "\$WORK" PATH="\$AGENT_PATH"'
check  "~/.local/bin is last on the agent PATH"     "$GC" 'AGENT_PATH="/usr/local/bin:/usr/bin:/bin:\$AGENT_HOME/\.local/bin"'

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
