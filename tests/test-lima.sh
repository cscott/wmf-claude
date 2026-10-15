#!/bin/bash
# Static checks for the optional Lima VM mode (bin/claude-vm, lima/):
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
VM="$REPO_ROOT/bin/claude-vm"
GI="$REPO_ROOT/lima/guest-install.sh"
GC="$REPO_ROOT/lima/guest-claude.sh"
VM_CLAUDE="$REPO_ROOT/bin/claude"
GD="$REPO_ROOT/lima/guest-dev-stack.sh"
GB="$REPO_ROOT/lima/guest-chrome.sh"

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
check  "readiness probe: agent not in docker"       "$Y" 'if id -nG agent \| grep -qw docker; then exit 1; fi'
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
for f in "$Y" "$GI" "$GC" "$GD" "$GB" "$VM"; do
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
absent "chrome MCP built in the guest (no SKIP_CHROME)" "$GI" 'WMF_CLAUDE_SKIP_CHROME'
check  "build writes mcp-config.json (the tree owner)" "$REPO_ROOT/bin/wmf-claude-build" "> \"\\\$CDP_DIR/mcp-config.json\""
absent "setup no longer writes mcp-config.json"      "$REPO_ROOT/bin/wmf-claude-setup" "> \"\\\$CDP_DIR/mcp-config.json\""
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
echo "--- guest-dev-stack.sh: PHP and MariaDB ---"
check  "guest-install runs it as root from the new tree" "$GI" 'sudo bash "\$NEW/lima/guest-dev-stack.sh"'
check  "refuses to run as non-root"                      "$GD" '\[\[ "\$\(id -u\)" == 0 \]\]'
check  "Debian packages, no recommends"                  "$GD" 'apt-get install -y -q --no-install-recommends "\$\{MISSING\[@\]\}"'
absent "no third-party apt source, key or download"      "$GD" 'sources\.list|/etc/apt/keyrings|https?://'
check  "MariaDB binds loopback only"                     "$GD" '^bind-address = 127\.0\.0\.1$'
check  "restart only when the config changed"            "$GD" 'cmp -s "\$NEWCNF" "\$CNF"'
check  "rights on the wiki database only"                "$GD" 'GRANT ALL PRIVILEGES ON \\`\$DB_NAME\\`\.\* TO'
absent "no grant on *.*"                                 "$GD" 'ON \*\.\*'
absent "root keeps unix_socket authentication"           "$GD" "'root'|ALTER USER"
absent "the agent user is never touched"                 "$GD" '(usermod|gpasswd|adduser).*(^| )agent( |$)'
check  "docs: PHPUnit in the VM"                         "$REPO_ROOT/docs/lima-vm.md" '^## PHPUnit in the VM$'

echo ""
echo "--- bin/claude-vm: copies, not mounts ---"
absent "no --rosetta"                               "$VM" '--rosetta'
absent "no --mount flag to limactl"                 "$VM" '--mount'
absent "no --port-forward flag to limactl"          "$VM" '--port-forward'
check  "limactl runs non-interactively"             "$VM" '--tty=false'
check  "vm_exists reads the whole list (no grep -q: SIGPIPE under pipefail)" "$VM" '^vm_exists\(\) \{ limactl list [^|]*\| grep -Fx "\$VM" >/dev/null; \}$'
absent "no limactl output piped into grep -q"       "$VM" 'limactl [^|]*\| *grep -q'
check  "push uses git bundle"                       "$VM" 'git -C "\$repo" bundle create'
check  "pull exports with hooks and fsmonitor off"  "$VM" 'core\.hooksPath=/dev/null -c core\.fsmonitor=false'
check  "pull fetches into remote-tracking vm/*"     "$VM" '\+refs/heads/\*:refs/remotes/vm/\*'
check  "pull verifies the bundle first"             "$VM" 'bundle verify "\$bundle"'
absent "pull never checks out, merges or resets"    "$VM" 'git -C "\$dest" (checkout|merge|reset|pull)'
check  "checkout never overwrites a local branch"     "$VM" 'die "\$2 exists in'
check  "push records the workspace's host repo"     "$VM" '\| ws_save_many$'
check  "update ships HEAD via git archive"          "$VM" 'git -C "\$ROOT" archive'
check  "update includes the submodules at HEAD's commits" "$VM" 'done < <\(git -C "\$ROOT" ls-tree -r HEAD\)'
check  "update stops on a missing submodule commit" "$VM" 'cat-file -e "\$sha\^\{commit\}"'
check  "update reuses vendored deps when the lockfile is unchanged" "$GI" 'cmp -s "\$CURRENT/\$sub/\$lock" "\$NEW/\$sub/\$lock"'
check  "update copies them, no hardlinks"            "$GI" 'cp -a "\$CURRENT/\$sub/\$vend" "\$NEW/\$sub/\$vend"'
check  "update passes WMF_CLAUDE_PHAB_USER to the guest" "$VM" 'guest env \$\{WMF_CLAUDE_PHAB_USER:\+WMF_CLAUDE_PHAB_USER="\$WMF_CLAUDE_PHAB_USER"\}'
check  "guest-install passes it to setup"             "$GI" 'PHAB_ENV=\("WMF_CLAUDE_PHAB_USER=\$WMF_CLAUDE_PHAB_USER"\)'
check  "setup keeps the recalled username in the VM"  "$REPO_ROOT/bin/wmf-claude-setup" '-n "\$DEFAULT_PHAB_USER" && \( -n "\$\{WMF_CLAUDE_IN_VM:-\}" \|\| ! -t 0 \)'

echo ""
echo "--- bin/claude-vm: what a session left in the VM ---"
claude_fn=$(awk '/^cmd_claude\(\) \{/,/^\}/' "$VM")
if grep -q 'after_session' <<<"$claude_fn" && ! grep -q 'exec limactl' <<<"$claude_fn"; then
  pass "claude keeps the launcher for the report after the session"
else
  fail "cmd_claude must call after_session and not exec"
fi
check  "a contained session records its exit code (errexit stays on)" "$VM" '--model-proxy-port "\$port" "\$name" "\$@" \|\| SESSION_RC=\$\?'
check  "the report strips control characters from agent-chosen names" "$VM" "tr -d '\\\\000-\\\\010\\\\013-\\\\037\\\\177'"
report_fn=$(awk '/^unpulled_report\(\) \{/,/^\}/' "$VM")
if grep -q 'guest_agent bash -c' <<<"$report_fn" && grep -q 'core\.hooksPath=/dev/null' <<<"$report_fn"; then
  pass "the report runs as the agent, hooks off"
else fail "the report must run as the agent with hooks off"; fi
if ! grep -qE '(g "\$w"|git -C "\$dest") (commit|checkout|merge|reset|push|fetch|add|switch)' <<<"$report_fn"; then
  pass "the report is read-only"
else fail "the report runs a git command that writes"; fi
check  "the report compares with the host's vm/* refs" "$VM" 'refs/remotes/vm/\$branch'
check  "the report counts uncommitted files"            "$VM" 'status --porcelain'
status_fn=$(awk '/^cmd_status\(\) \{/,/^\}/' "$VM")
if grep -q 'unpulled_report' <<<"$status_fn"; then pass "status shows the report"; else fail "status must show the report"; fi
check  "docs: sibling workspaces via --allow"           "$REPO_ROOT/docs/lima-vm.md" 'claude core --allow /home/agent/work --'
check  "docs: uncommitted files do not travel"          "$REPO_ROOT/docs/lima-vm.md" 'Uncommitted files never travel'

echo ""
echo "--- bin/claude: attach-mode handshake override ---"
check  "bin/claude reads WMF_DOCKER_HANDSHAKE once" "$REPO_ROOT/bin/claude" '^DOCKER_HANDSHAKE_OVERRIDE="\$\{WMF_DOCKER_HANDSHAKE:-\}"$'
check  "bin/claude unsets it on every path"         "$REPO_ROOT/bin/claude" '^unset WMF_DOCKER_HANDSHAKE$'
check  "attach mode uses the override"              "$REPO_ROOT/bin/claude" 'HS_FILE="\$\{DOCKER_HANDSHAKE_OVERRIDE:-'

echo ""
echo "--- bin/wmf-claude-model-proxy: request policy ---"
# -B: loading the proxy as a module must not write bin/__pycache__.
if out=$(python3 -B "$REPO_ROOT/tests/test-model-proxy.py" 2>&1); then
  pass "test-model-proxy.py ($(grep -c '^PASS' <<<"$out") checks)"
  MP="$REPO_ROOT/bin/wmf-claude-model-proxy"
  check  "proxy: request and response records, one gzip member each" "$MP" 'member = gzip\.compress\($'
  check  "proxy: request and response share an id"            "$MP" 'id=self\.rid, kind="response"'
  check  "proxy: a request it cannot record is not forwarded" "$MP" 'return self\._session_stopped\(\)'
  check  "proxy: a per-session byte budget stops the session" "$MP" 'self\.written \+ n > self\.budget'
  check  "proxy: a bad transcript path refuses to start"      "$MP" 'cannot open the transcript file'
  check  "vm: the contained session captures the transcript"  "$VM" 'transcript-\$VM-\$\(date -u \+%Y%m%dT%H%M%SZ\)-\$\$\.jsonl\.gz'
  check  "vm: the proxy is given --capture"                   "$VM" 'model-proxy" --socket "\$sock" --log "\$log" --capture "\$capture"'
  check  "vm: delete keeps the host transcripts"              "$VM" 'are on the host and are kept'
  for prof in wmf-engineer wmf-contained; do
    if jq -e '.filesystem.deny | index("~/.local/state/wmf-claude")' "$REPO_ROOT/profiles/$prof.json" >/dev/null; then
      pass "$prof denies ~/.local/state/wmf-claude (the host transcript dir)"
    else fail "$prof does not deny ~/.local/state/wmf-claude"; fi
  done
else
  fail "test-model-proxy.py"; grep -v '^PASS' <<<"$out" | sed 's/^/        /'
fi

echo ""
echo "--- bin/claude-vm logs: reads the host artifacts ---"
# logs reads host files only: it must never need a VM (retention outlives it).
logs_src="$(awk '/^logs_request\(\) \{/,/^\}/' "$VM")
$(awk '/^logs_transcript\(\) \{/,/^\}/' "$VM")
$(awk '/^cmd_logs\(\) \{/,/^\}/' "$VM")"
if grep -qE 'limactl|vm_exists|is_contained' <<<"$logs_src"; then
  fail "logs: must read host files only, never a VM"
else pass "logs reads host files only (never needs a VM)"; fi

LG=$(mktemp -d); LST="$LG/wmf-claude"; mkdir -p "$LST"
# A request log: two sessions, and a torn final line to drop.
{
  printf '%s\n' '{"ts":"2026-01-01T00:00:00Z","decision":"start","pid":1}'
  printf '%s\n' '{"ts":"2026-01-01T00:00:01Z","decision":"deny","status":403,"method":"POST","path":"/v1/x","reason":"OLD_SESSION"}'
  printf '%s\n' '{"ts":"2026-01-01T00:00:02Z","decision":"stop","pid":1}'
  printf '%s\n' '{"ts":"2026-01-01T01:00:00Z","decision":"start","pid":2}'
  printf '%s\n' '{"ts":"2026-01-01T01:00:01Z","decision":"allow","status":200,"method":"POST","path":"/v1/messages","req_bytes":9,"resp_bytes":9}'
  printf '{"torn line with no close\n'
} > "$LST/model-proxy-wmf-claude.log"
# Two transcripts for this VM; a decoy for the contained VM it must not match.
printf '%s\n' '{"id":"aaaa1111","kind":"request","decision":"request","ts":"t","request":"OLDTX"}'   | gzip > "$LST/transcript-wmf-claude-20260101T000000Z-1.jsonl.gz"
printf '%s\n' '{"id":"bbbb2222","kind":"request","decision":"request","ts":"t","request":"NEWTX"}'   | gzip > "$LST/transcript-wmf-claude-20260101T010000Z-2.jsonl.gz"
printf '%s\n' '{"id":"cccc3333","kind":"request","decision":"request","ts":"t","request":"DECOYTX"}' | gzip > "$LST/transcript-wmf-claude-contained-20260101T020000Z-3.jsonl.gz"
# Append half of a valid member to the newest: a truncated final member.
TZ=$(mktemp); printf '%s\n' '{"id":"dddd4444","kind":"response"}' | gzip > "$TZ"
head -c "$(( $(wc -c < "$TZ") / 2 ))" "$TZ" >> "$LST/transcript-wmf-claude-20260101T010000Z-2.jsonl.gz"; rm -f "$TZ"

lg() { XDG_STATE_HOME="$LG" WMF_CLAUDE_VM="$1" bash "$VM" "${@:2}" 2>/dev/null; }

out=$(lg wmf-claude logs)
if grep -q OLD_SESSION <<<"$out"; then fail "logs: default shows only the newest session"; else pass "logs: default shows only the newest session"; fi
if grep -q 'v1/messages' <<<"$out"; then pass "logs: default shows the newest session's records"; else fail "logs: default missing the newest records"; fi
if grep -q "$(printf '\033')" <<<"$out"; then fail "logs: piped output must be plain"; else pass "logs: piped output is plain (no ANSI)"; fi
if grep -q OLD_SESSION <<<"$(lg wmf-claude logs --all)"; then pass "logs --all shows every session"; else fail "logs --all must show every session"; fi

tx=$(lg wmf-claude logs --transcript)
if grep -q DECOYTX <<<"$tx"; then fail "logs --transcript: the VM-name glob matched '*-contained-*'"; else pass "logs --transcript: the VM-name glob is anchored"; fi
if grep -q bbbb2222 <<<"$tx"; then pass "logs --transcript reads the newest transcript"; else fail "logs --transcript did not read the newest"; fi
if grep -q NEWTX <<<"$(lg wmf-claude logs --transcript --raw)"; then pass "logs --transcript --raw prints the decoded JSON"; else fail "logs --transcript --raw did not decode"; fi
if lg wmf-claude logs --transcript >/dev/null; then pass "logs --transcript tolerates a truncated final member"; else fail "logs --transcript aborted on a truncated member"; fi
if lg nope logs >/dev/null 2>&1; then fail "logs: a missing request log must exit non-zero"; else pass "logs: a missing request log exits non-zero"; fi

# --text reconstructs the readable message, and labels the agent's tool calls
# (commands) instead of dumping the raw SSE deltas.
TT="$LST/transcript-wmf-claude-20260101T040000Z-7.jsonl.gz"
python3 - "$TT" <<'PYEOF'
import gzip, json, sys
def ev(t, d): return f"event: {t}\ndata: {json.dumps(d)}\n\n"
sse  = ev("message_start", {"type":"message_start","message":{"model":"m1"}})
sse += ev("content_block_start", {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}})
sse += ev("content_block_delta", {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Listing now."}})
sse += ev("content_block_start", {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","name":"Bash","id":"t1"}})
sse += ev("content_block_delta", {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":'{"command":"ls -la /tmp"}'}})
sse += ev("message_delta", {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}})
rec = {"id":"tool0007","kind":"response","decision":"allow","status":200,"ts":"t","response":sse}
open(sys.argv[1],"wb").write(gzip.compress((json.dumps(rec)+"\n").encode()))
PYEOF
tt=$(lg wmf-claude logs --transcript --text "$TT")
if grep -q 'Listing now.' <<<"$tt"; then pass "logs --text reconstructs the assistant text"; else fail "logs --text missing the assistant text"; fi
if grep -q 'Bash: ls -la /tmp' <<<"$tt"; then pass "logs --text labels the agent's tool calls (commands)"; else fail "logs --text did not label the Bash command"; fi
if grep -q 'partial_json\|input_json_delta\|event: message_start' <<<"$tt"; then fail "logs --text leaked raw SSE instead of reconstructing"; else pass "logs --text reconstructs, not dumps, the stream"; fi
if lg wmf-claude logs --text >/dev/null 2>&1; then fail "logs: --text must require --transcript"; else pass "logs: --text requires --transcript"; fi

# --text display correctness: a non-SSE error body (not a false stream footer),
# a part-captured body flagged, a failed tool_result marked, and a record that
# would throw isolated so later records still render.
EB="$LST/transcript-wmf-claude-20260101T050000Z-8.jsonl.gz"
python3 - "$EB" <<'PYEOF'
import gzip, json, sys
recs = [
  {"id":"e1","kind":"response","decision":"error","status":529,"ts":"t","response_truncated":True,
   "response":json.dumps({"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}})},
  {"id":"ie","kind":"request","decision":"request","ts":"t",
   "request":json.dumps({"model":"m","messages":[{"role":"user","content":[
       {"type":"tool_result","is_error":True,"content":"permission denied"}]}]})},
  {"id":"bad","kind":"response","decision":"allow","status":200,"ts":"t",
   "response":json.dumps({"type":"message","content":"not-an-array"})},   # .content a string -> map throws
  {"id":"ok","kind":"response","decision":"allow","status":200,"ts":"t",
   "response":"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"SURVIVED\"}}\n\nevent: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":1}}\n\n"},
]
with open(sys.argv[1],"wb") as f:
    for r in recs: f.write(gzip.compress((json.dumps(r)+"\n").encode()))
PYEOF
eb=$(lg wmf-claude logs --transcript --text "$EB")
if grep -q 'overloaded_error' <<<"$eb"; then pass "logs --text renders a non-SSE error body"; else fail "logs --text did not render the error body"; fi
if grep -q 'stream ended without message_stop' <<<"$eb"; then fail "logs --text showed a false stream footer on a JSON body"; else pass "logs --text: no false stream footer on a JSON body"; fi
if grep -q 'capture truncated' <<<"$eb"; then pass "logs --text flags a part-captured body"; else fail "logs --text did not flag response_truncated"; fi
if grep -q 'result error' <<<"$eb"; then pass "logs --text marks a failed tool_result"; else fail "logs --text did not mark is_error"; fi
if grep -q 'unrenderable record bad' <<<"$eb" && grep -q 'SURVIVED' <<<"$eb"; then pass "logs --text isolates a bad record; later records still render"; else fail "logs --text: a bad record blanked the rest"; fi
# --vm selects the VM, and wins over WMF_CLAUDE_VM.
out=$(XDG_STATE_HOME="$LG" WMF_CLAUDE_VM=nonesuch bash "$VM" --vm wmf-claude logs 2>/dev/null)
if grep -q 'allow' <<<"$out"; then pass "--vm selects the VM (over WMF_CLAUDE_VM)"; else fail "--vm did not select the VM"; fi
rm -rf "$LG"

echo ""
echo "--- bin/claude-vm: modes, --vm and list ---"
MD=$(mktemp -d); mkdir -p "$MD/bin" "$MD/cfg/wmf-claude" "$MD/lima/old" "$MD/lima/other"
# A fake limactl: it logs each call, lists four instances (none with
# FAKE_LIMA_EMPTY), and fails `create`, so a test that gets that far stops.
cat > "$MD/bin/limactl" <<'FAKE'
#!/bin/bash
echo "$*" >> "$FAKE_LIMA_LOG"
case "$1" in
  list)
    [[ -n "${FAKE_LIMA_EMPTY:-}" ]] && exit 0
    if [[ "$*" == *'{{.Dir}}'* ]]; then
      d="$FAKE_LIMA_DIR"
      printf 'wmf-claude\tRunning\t%s\nold\tStopped\t%s\nother\tRunning\t%s\nwmf-claude-contained\tStopped\t%s\n' \
        "$d/wmf-claude" "$d/old" "$d/other" "$d/c"
    else
      printf 'wmf-claude\nold\nother\nwmf-claude-contained\n'
    fi ;;
  create) exit 1 ;;
esac
exit 0
FAKE
chmod +x "$MD/bin/limactl"
C="$MD/cfg/wmf-claude"
date > "$C/vm-wmf-claude-created"
printf 'core\t/x/core\nwmf-claude\t/x/w\nnotes\t/x/n\nextra\t/x/e\n' > "$C/vm-wmf-claude-workspaces"
date > "$C/vm-wmf-claude-contained-contained"
# "old": made before the created record, found by the template in its config.
echo '    nft add table inet wmf_claude_hostblock' > "$MD/lima/old/lima.yaml"
echo 'images: []' > "$MD/lima/other/lima.yaml"
vmc() { : > "$MD/calls"; env -u WMF_CLAUDE_VM XDG_CONFIG_HOME="$MD/cfg" FAKE_LIMA_LOG="$MD/calls" FAKE_LIMA_DIR="$MD/lima" \
  PATH="$MD/bin:$PATH" bash "$VM" "$@" </dev/null 2>&1; }
called() { grep -qE "^$1( |$)" "$MD/calls"; }

out=$(vmc --vm newvm create)
if grep -q 'choose a mode' <<<"$out" && ! called create; then pass "create: no mode and no terminal refuses before limactl create"
else fail "create: no mode did not refuse first ($out)"; fi
out=$(vmc --vm newvm create --isolated --contained)
if grep -q 'not both' <<<"$out" && ! called create; then pass "create: --isolated with --contained refuses"; else fail "create: both modes accepted"; fi
out=$(FAKE_LIMA_EMPTY=1 vmc create --contained)
if grep -q 'its own name' <<<"$out" && ! called create; then pass "create --contained: the default name refuses"; else fail "create --contained took the default name ($out)"; fi
out=$(vmc --vm newvm create --isolated --cpus 2)
if grep -qE '^create .*--name newvm --cpus 2 ' "$MD/calls" && ! grep -q -- '--isolated' "$MD/calls"; then pass "create: the mode flag never reaches limactl create"
else fail "create: limactl create got: $(grep '^create' "$MD/calls")"; fi
if [[ -f "$C/vm-newvm-created" && ! -f "$C/vm-newvm-contained" ]]; then pass "create --isolated marks the VM, with no contained record"; else fail "create --isolated: wrong host records"; fi
out=$(vmc --vm newc create --contained)
if [[ -f "$C/vm-newc-contained" ]]; then pass "create --contained: a create that fails part way stays contained"; else fail "create --contained: no contained record after a failed create"; fi
out=$(WMF_CLAUDE_VM=wmf-claude FAKE_LIMA_EMPTY=1 vmc create --contained)
if grep -q 'its own name' <<<"$out"; then pass "create --contained: the default name refuses, also when set by name"; else fail "create --contained took WMF_CLAUDE_VM=wmf-claude"; fi
out=$(vmc list)
row() { grep -E "^. $1 " <<<"$out"; }
if row wmf-claude | grep -qE '^\* wmf-claude +Running +isolated +4 \(core, wmf-claude, notes, \.\.\.\)'; then pass "list: the selected VM, its mode and workspaces"
else fail "list: wrong row for wmf-claude: $(row wmf-claude)"; fi
if row wmf-claude-contained | grep -q 'contained (no token)'; then pass "list: a contained VM without a token"; else fail "list: wrong row for the contained VM"; fi
if row old | grep -q isolated; then pass "list: finds an older VM by its template"; else fail "list: missed the older VM"; fi
if row other >/dev/null; then fail "list: shows a VM that is not wmf-claude"; else pass "list: leaves out other Lima VMs"; fi
out=$(vmc --vm old list)
if row old | grep -q '^\*'; then pass "list: --vm moves the selection"; else fail "list: --vm did not move the selection"; fi
out=$(FAKE_LIMA_EMPTY=1 vmc list)
if grep -q 'No wmf-claude VMs' <<<"$out"; then pass "list: says when there is none"; else fail "list: no message for an empty list"; fi
# The reset checks last: the fake still lists a VM after its delete, so
# they leave host records that the list checks above do not expect.
# reset reads the confirmation from stdin; give it, so a delete can happen.
vmr() { : > "$MD/calls"; env -u WMF_CLAUDE_VM XDG_CONFIG_HOME="$MD/cfg" FAKE_LIMA_LOG="$MD/calls" FAKE_LIMA_DIR="$MD/lima" \
  PATH="$MD/bin:$PATH" bash "$VM" "${@:2}" <<<"$1" 2>&1; }
out=$(vmr wmf-claude reset --isolated)
if called delete; then pass "reset: a valid mode reaches the delete (control)"; else fail "reset: the delete is not reachable, so the next checks prove nothing"; fi
out=$(vmr wmf-claude reset --isolated --contained)
if grep -q 'not both' <<<"$out" && ! called delete; then pass "reset: a bad mode refuses before the delete"; else fail "reset deleted before it checked the mode"; fi
out=$(vmr wmf-claude reset --contained)
if grep -q 'its own name' <<<"$out" && ! called delete; then pass "reset --contained: refuses to make the default VM contained"; else fail "reset --contained made the default VM contained"; fi
out=$(vmr wmf-claude-contained --vm wmf-claude-contained reset)
if called delete && ! grep -q 'its own name' <<<"$out"; then pass "reset keeps a contained VM contained"; else fail "reset of a contained VM failed: $out"; fi
out=$(vmr x --vm nonesuch reset)
if grep -q 'does not exist' <<<"$out" && ! called delete && ! called create; then pass "reset: no VM and no mode refuses"; else fail "reset of a missing VM went on"; fi

rm -rf "$MD"

echo ""
echo "--- bin/claude-vm: push and pull, nested repositories (fake guest) ---"
# A fake limactl that runs guest commands here: `shell ... -- CMD` runs CMD,
# with HOME set to a fake agent home for `sudo -H -u agent --`, and `copy`
# is cp. So the git plumbing of push and pull runs for real.
RT=$(mktemp -d); mkdir -p "$RT/bin" "$RT/agent/work" "$RT/cfg"
cat > "$RT/bin/limactl" <<'FAKE'
#!/bin/bash
case "$1" in
  list)
    if [[ "$*" == *Status* ]]; then echo Running; else echo wmf-claude; fi ;;
  copy)
    cp "${2#wmf-claude:}" "${3#wmf-claude:}" ;;
  shell)
    while [[ "$1" != -- ]]; do shift; done; shift
    if [[ "$1 $2 $3 $5" == "sudo -H -u --" ]]; then shift 5; HOME="$FAKE_AGENT_HOME" exec "$@"; fi
    exec "$@" ;;
esac
FAKE
chmod +x "$RT/bin/limactl"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.org GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.org
mkrepo() { git init -q -b main "$1" && git -C "$1" commit -q --allow-empty -m "${2:-init}"; }
H="$RT/host/core"
mkrepo "$H"; printf 'extensions/*\nvendor/\n' > "$H/.gitignore"; git -C "$H" add .gitignore; git -C "$H" commit -q -m ignore
mkrepo "$H/extensions/A"; mkrepo "$H/extensions/B"; mkrepo "$H/vendor/dep"; mkrepo "$H/extensions/bad name"
# A submodule: the clone of core leaves an empty directory at its path.
mkrepo "$RT/subsrc"
git -C "$H" -c protocol.file.allow=always submodule add -q "$RT/subsrc" lib/sub >/dev/null 2>&1
git -C "$H" commit -q -m submodule
rt() { env -u WMF_CLAUDE_VM XDG_CONFIG_HOME="$RT/cfg" WMF_CLAUDE_GUEST_AGENT_HOME="$RT/agent" FAKE_AGENT_HOME="$RT/agent" \
  PATH="$RT/bin:$PATH" bash "$VM" "$@" 2>&1; }
W="$RT/agent/work"
WS="$RT/cfg/wmf-claude/vm-wmf-claude-workspaces"
ag() { git -C "$W/$1" "${@:2}"; }

out=$(rt push --nested "$H")
if [[ -d "$W/core/.git" && -d "$W/core/extensions/A/.git" && -d "$W/core/extensions/B/.git" ]]; then pass "push --nested: the top repo and its nested repos arrive"
else fail "push --nested: missing workspaces: $out"; fi
if [[ ! -e "$W/core/vendor/dep" ]]; then pass "push --nested: dependency trees (vendor) stay out"; else fail "push --nested pushed vendor/dep"; fi
if grep -q 'skipped extensions/bad name' <<<"$out" && [[ ! -e "$W/core/extensions/bad name" ]]; then pass "push --nested: an invalid workspace name is skipped"; else fail "push --nested: the invalid name was not skipped"; fi
if [[ -e "$W/core/lib/sub/.git" ]]; then pass "push --nested: a submodule path (empty in the top clone) gets its repo"
else fail "push --nested: the submodule did not arrive: $out"; fi
if [[ "$(cut -f1 "$WS" | sort | tr '\n' ' ')" == "core core/extensions/A core/extensions/B core/lib/sub " ]]; then pass "push --nested records each workspace"
else fail "push --nested recorded: $(cut -f1 "$WS" | tr '\n' ' ')"; fi

out=$(rt push --nested "$H")
if grep -q 'nothing to send' <<<"$out"; then pass "push --nested again: nothing to send"; else fail "push --nested again sent bundles: $out"; fi
git -C "$H/extensions/A" commit -q --allow-empty -m host-change
out=$(rt push --nested=A "$H")
if grep -q '1 to send' <<<"$out" && grep -q '1 up to date' <<<"$out" \
   && [[ "$(ag core/extensions/A rev-parse host/main 2>/dev/null)" == "$(git -C "$H/extensions/A" rev-parse main)" ]]; then
  pass "push --nested=A: sends only A's new commits; the top repo is up to date"
else fail "push --nested=A: $out"; fi

# One bad entry does not stop the others.
mkrepo "$H/extensions/C"; mkdir -p "$W/core/extensions/C"; echo x > "$W/core/extensions/C/file"
git -C "$H/extensions/B" commit -q --allow-empty -m host-change-b
out=$(rt push --nested "$H"); prc=$?
if [[ "$prc" -ne 0 ]] && grep -q '! core/extensions/C' <<<"$out" \
   && [[ "$(ag core/extensions/B rev-parse host/main 2>/dev/null)" == "$(git -C "$H/extensions/B" rev-parse main)" ]]; then
  pass "push --nested: a failed entry is reported, and the others still arrive"
else fail "push --nested with a bad entry (rc $prc): $out"; fi
rm -rf "$W/core/extensions/C" "$H/extensions/C"

# The agent works in B and in the top repo; pull --nested brings both back.
ag core/extensions/B switch -q -c fix && ag core/extensions/B commit -q --allow-empty -m agent-b
ag core switch -q -c topfix && ag core commit -q --allow-empty -m agent-top
out=$(rt pull --nested core)
got=$(git -C "$H/extensions/B" rev-parse -q --verify vm/fix); want=$(ag core/extensions/B rev-parse fix)
if [[ -n "$want" && "$got" == "$want" ]]; then pass "pull --nested: a nested branch lands in vm/* of its own repo"
else fail "pull --nested: no vm/fix in B: $out"; fi
if git -C "$H" rev-parse -q --verify vm/topfix >/dev/null; then pass "pull --nested: the top repo is pulled too"; else fail "pull --nested: no vm/topfix in core"; fi
if [[ "$(git -C "$H/extensions/A" rev-parse -q --verify vm/main)" == "$(ag core/extensions/A rev-parse main)" ]]; then
  pass "pull --nested: a nested repo with no new commits gets its branches, without a bundle"
else fail "pull --nested: A's vm/main does not match the VM"; fi
# A branch at a commit the other side has travels without new commits, and
# a deleted branch goes, each way.
git -C "$H/extensions/A" branch feat main
out=$(rt push --nested=A "$H")
if [[ "$(ag core/extensions/A rev-parse -q --verify host/feat)" == "$(git -C "$H/extensions/A" rev-parse main)" ]]; then pass "push: a new branch at a commit the VM has reaches host/*"
else fail "push: host/feat missing in the VM: $out"; fi
git -C "$H/extensions/A" branch -q -D feat
out=$(rt push --nested=A "$H")
if ! ag core/extensions/A rev-parse -q --verify host/feat >/dev/null && ag core/extensions/A rev-parse -q --verify host/main >/dev/null; then
  pass "push: a branch deleted on the host leaves host/*, and host/HEAD's branch stays"
else fail "push: host/feat still in the VM, or host/main gone: $out"; fi
ag core/extensions/A branch atold main
out=$(rt pull --nested core)
if git -C "$H/extensions/A" rev-parse -q --verify vm/atold >/dev/null; then pass "pull --nested: a VM branch at a commit the host has comes back"
else fail "pull --nested: no vm/atold: $out"; fi
ag core/extensions/A branch -q -D atold
out=$(rt pull --nested core)
if ! git -C "$H/extensions/A" rev-parse -q --verify vm/atold >/dev/null; then pass "pull --nested: a branch deleted in the VM leaves vm/*"
else fail "pull --nested: vm/atold is still there"; fi
out=$(rt pull --nested core)
if grep -q 'no nested workspace has changed' <<<"$out" && ! ls "$W/.pull" | grep -q nested; then pass "pull --nested again: nothing is sent twice"
else fail "pull --nested sent commits again: $out"; fi
if [[ -d "$W/.pull" ]] && ! ls "$W/.pull" | grep -q nested; then pass "pull --nested removes its bundles in the guest"; else fail "pull --nested left bundles in the guest, or made none"; fi
out=$(rt pull --nested core somewhere)
if grep -q 'takes no DEST' <<<"$out"; then pass "pull --nested refuses a DEST"; else fail "pull --nested took a DEST"; fi

# The end-of-session report suggests one pull --nested, not one per repo.
ag core/extensions/A switch -q -c more && ag core/extensions/A commit -q --allow-empty -m agent-a
ag core commit -q --allow-empty -m agent-top2
ag core/extensions/B commit -q --allow-empty -m agent-b2
rpt=$(env XDG_CONFIG_HOME="$RT/cfg" FAKE_AGENT_HOME="$RT/agent" PATH="$RT/bin:$PATH" bash -c '
  VM=wmf-claude; ENGINEER_HOME=/; AGENT=agent; WS_FILE="$1"
  '"$(grep -E '^guest(_agent)?\(\)' "$VM")"'
  '"$(awk '/^unpulled_report\(\) \{/,/^\}/' "$VM")"'
  unpulled_report' _ "$WS" 2>&1)
if grep -q 'Pull with: claude-vm pull --nested core$' <<<"$rpt"; then pass "the report groups nested workspaces into one pull --nested"
else fail "the report did not group the nested workspaces: $rpt"; fi
# A guest that reports an index out of range, or not a number, gets nothing.
pn=$(awk '/^pull_nested\(\) \{/,/^\}/' "$VM")
if grep -qF 'if [[ "$kind" != B || ! "$i" =~ ^(0|[1-9][0-9]*)$ ]] || (( i >= ${#wss[@]} )); then continue; fi' <<<"$pn"; then pass "pull --nested takes only indices in range from the guest"
else fail "pull --nested does not check the guest's indices"; fi
rm -rf "$RT"
unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL

echo ""
echo "--- bin/wmf-claude-vm: the old name still works ---"
if [[ "$(bash "$REPO_ROOT/bin/wmf-claude-vm" --help 2>&1)" == "$(bash "$VM" --help 2>&1)" && -x "$REPO_ROOT/bin/wmf-claude-vm" ]]; then
  pass "bin/wmf-claude-vm runs bin/claude-vm"
else fail "bin/wmf-claude-vm does not run bin/claude-vm"; fi
check  "setup accepts a claude-vm alias to the old name" "$REPO_ROOT/bin/wmf-claude-setup" 'install_alias claude-vm "\$ROOT/bin/claude-vm" .* "\$ROOT/bin/wmf-claude-vm"$'

echo ""
echo "--- scripts parse ---"
for f in "$VM" "$GI" "$GC" "$GD" "$GB"; do
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
  for f in "$VM" "$GI" "$GC" "$GD" "$GB"; do
    if shellcheck -x "$f" >/dev/null 2>&1; then pass "shellcheck ${f#"$REPO_ROOT"/}"; else fail "shellcheck ${f#"$REPO_ROOT"/}"; shellcheck -x "$f" | sed 's/^/        /'; fi
  done
else
  skip "shellcheck not installed"
fi
if command -v limactl >/dev/null 2>&1; then
  # A throwaway LIMA_HOME: validate needs none of ~/.lima, and a nono
  # session cannot read it.
  LIMA_TMP="$(mktemp -d)"
  if LIMA_HOME="$LIMA_TMP" limactl validate "$Y" >/dev/null 2>&1; then pass "limactl validate lima/wmf-claude.yaml"; else fail "limactl validate lima/wmf-claude.yaml"; LIMA_HOME="$LIMA_TMP" limactl validate "$Y" 2>&1 | sed 's/^/        /'; fi
  rm -rf "$LIMA_TMP"
else
  skip "limactl not installed; template not validated"
fi

echo ""
echo "--- lima/guest-claude.sh: agent launch ---"
check  "--ide is refused"                           "$GC" '^[[:space:]]*--ide\)$'
check  "--chrome starts the browser as the browser user" "$GC" 'exec sudo -n -u "\$CHROME_USER" "\$INSTALL/lima/guest-chrome.sh" cdp'
check  "--chrome waits for CDP before the session"  "$GC" 'curl -s -o /dev/null http://127.0.0.1:9222/json/version'
check  "--chrome tells the session it is in the VM" "$GC" 'WMF_CLAUDE_CHROME_IN_VM=1'
check  "every VM session is told so"                "$GC" '^ENV_ARGS=\(WMF_CLAUDE_IN_VM=1\)$'
check  "hook: how to serve the wiki in the VM"      "$REPO_ROOT/bin/session-start.sh" 'php -S 127\.0\.0\.1:8080 -t \.'
check  "docs: serving the wiki"                     "$REPO_ROOT/docs/lima-vm.md" 'Serving the wiki'
check  "the browser stops with the session"         "$GC" 'if \[\[ -n "\$CHROME_PID" \]\]; then kill "\$CHROME_PID"'
absent "bin/claude: contained mode no longer refuses --chrome" "$VM_CLAUDE" 'contained_refused\+=" --chrome"'

echo ""
echo "--- lima/guest-chrome.sh: headless, loopback only ---"
check  "headless"                                   "$GB" '^  --headless=new$'
check  "CDP pinned to loopback"                     "$GB" '--remote-debugging-address=127\.0\.0\.1'
check  "default context: closed proxy"              "$GB" '^  --proxy-server=127\.0\.0\.1:9$'
check  "no name resolution but loopback"            "$GB" 'MAP \* ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127\.0\.0\.1'
check  "proxy bypass names loopback"                "$GB" '--proxy-bypass-list="127\.0\.0\.1;localhost;\[::1\]"'
check  "fresh data dir per run"                     "$GB" 'DATA_DIR=\$\(mktemp -d /tmp/wmf-claude-chrome\.XXXXXX\)'
check  "data dir removed on exit"                   "$GB" 'rm -rf "\$DATA_DIR"'
check  "HOME is the data dir"                       "$GB" '^export HOME="\$DATA_DIR"$'
absent "Chromium's own sandbox stays on"            "$GB" 'no-sandbox'
check  "dev stack: browser user is a system account, no login" "$GD" 'adduser --system --group --home "/var/lib/\$CHROME_USER" --shell /usr/sbin/nologin'
check  "dev stack: browser user leaves sudo/docker"  "$GD" 'gpasswd -d "\$CHROME_USER" "\$g"'
check  "dev stack: firewall rejects the browser uid off loopback" "$GD" 'meta skuid \$CHROME_UID oifname != "lo" counter reject'
check  "dev stack: firewall unit follows nftables.service" "$GD" '^PartOf=nftables\.service$'
check  "dev stack: Chromium without recommends (no setuid sandbox)" "$GD" ' chromium fonts-liberation\)$'
check  "docs: a browser in the VM"                  "$REPO_ROOT/docs/lima-vm.md" '^## A browser in the VM$'
check  "only --docker=SERVICE is accepted"          "$GC" '--docker=\*:\*\|--docker=auto\|--docker\|--docker=\|--egress=\*\)'
check  "the broker runs as the engineer"            "$GC" 'python3 "\$INSTALL/bin/launch-docker-broker"'
check  "handshake is group-readable (0640)"         "$GC" 'chmod 0640 "\$HS"'
check  "the agent runs with a fixed PATH"           "$GC" 'sudo -H -u "\$AGENT" env -C "\$WORK" PATH="\$AGENT_PATH"'
check  "~/.local/bin is last on the agent PATH"     "$GC" 'AGENT_PATH="/usr/local/bin:/usr/bin:/bin:\$AGENT_HOME/\.local/bin"'

echo ""
echo "--- contained mode: launchers ---"
check  "guest-claude: contained marker requires --model-proxy-port"   "$GC" 'this is a contained VM; start sessions with claude-vm claude'
check  "guest-claude: --model-proxy-port requires the marker"   "$GC" 'this VM is not a contained VM'
check  "guest-claude: passes WMF_CLAUDE_MODEL_PROXY_PORT"        "$GC" 'ENV_ARGS\+=\("WMF_CLAUDE_MODEL_PROXY_PORT=\$PROXY_PORT"\)'
check  "guest-install: no MCP servers in a contained VM"      "$GI" 'CONTAINED_ENV=\(WMF_CLAUDE_CONTAINED=1\)'
check  "vm: create --contained writes the guest marker"        "$VM" 'date -u \+%FT%TZ > \$CONTAINED_GUEST_MARKER'
check  "vm: create --contained writes the host record"         "$VM" 'date -u \+%FT%TZ > "\$CONTAINED_RECORD"'
check  "vm: the host record decides contained mode"            "$VM" '^is_contained\(\) \{ \[\[ -f "\$CONTAINED_RECORD" \]\]; \}'
check  "vm: model proxy bridge off Lima's shared connection"     "$VM" 'ssh -n -F "\$dir/ssh.config" -o ControlMaster=no -o ControlPath=none -o ExitOnForwardFailure=yes'
check  "vm: model proxy bridge binds guest loopback, free port"  "$VM" '-R "127.0.0.1:0:\$sock"'
check  "vm: pull from a shallow workspace bundles past the boundary" "$VM" 'bundle create -q "\$2" --branches HEAD --not \$\(cat "\$shallow"\)'
check  "install: nftables.service stop no longer flushes Docker's rules" "$GI" 'ExecStop=.*/etc/systemd/system/nftables.service.d/wmf-claude.conf'
check  "vm: reset keeps a contained VM a contained VM"               "$VM" 'if is_contained; then CREATE_MODE=contained; else CREATE_MODE=isolated; fi'
# cmd_login_contained must never run claude auth login (that puts the token in the VM).
login_contained=$(awk '/^cmd_login_contained\(\) \{/,/^\}/' "$VM")
if [[ -n "$login_contained" ]] && ! grep -q 'claude auth login' <<<"$login_contained"; then pass "vm: contained login never signs Claude in inside the VM"
else fail "vm: contained login runs claude auth login, or is missing"; fi
# model_proxy_stop (the EXIT trap of a contained session) must stop the proxy and the
# forward under set -u, also on macOS /bin/bash 3.2, where an empty array is
# unbound. Run it in every bash we have, with nothing to clean up and with
# two live processes.
model_proxy_stop_fn=$(awk '/^model_proxy_stop\(\) \{/,/^\}/' "$VM")
for sh in bash /bin/bash; do
  command -v "$sh" >/dev/null 2>&1 || continue
  if out=$("$sh" -c "set -euo pipefail; $model_proxy_stop_fn
    MODEL_TMP=(); MODEL_SSH_PID=''; MODEL_PROXY_PID=''; model_proxy_stop
    sleep 30 & MODEL_SSH_PID=\$!; sleep 30 & MODEL_PROXY_PID=\$!; t=\$(mktemp); MODEL_TMP=(\"\$t\")
    model_proxy_stop
    if kill -0 \$MODEL_SSH_PID 2>/dev/null || kill -0 \$MODEL_PROXY_PID 2>/dev/null || [ -e \"\$t\" ]; then echo LEFT; fi" 2>&1) \
     && [[ "$out" != *LEFT* ]]; then
    pass "vm: model_proxy_stop stops the proxy and forward ($("$sh" -c 'echo $BASH_VERSION'))"
  else
    fail "vm: model_proxy_stop under $sh: $out"
  fi
done

NET="$REPO_ROOT/lima/guest-contained-net.sh"
echo ""
echo "--- contained VM network phase: lima/guest-contained-net.sh ---"
if bash -n "$NET" && [[ -x "$NET" ]]; then pass "guest-contained-net.sh parses and is executable"; else fail "guest-contained-net.sh"; fi
lock_rules=$(awk '/^load_lock\(\) \{/,/^\}/' "$NET")
check  "lock: output policy drop"                        "$NET" 'type filter hook output priority -10; policy drop;'
check  "lock: forward policy drop (containers)"          "$NET" 'type filter hook forward priority -10; policy drop;'
n_accept=$(grep -c ' accept$' <<<"$lock_rules")
if [[ "$n_accept" == 8 ]]; then pass "lock: exactly the 8 expected accept rules"
else fail "lock: $n_accept accept rules, want 8 (lo, established x2, docker0 x2, br-* x2, DHCP)"; fi
for r in 'oifname "lo" accept' 'ct state established,related ct direction reply accept' 'oifname "docker0" accept' 'oifname "br-\*" accept' 'udp sport 68 udp dport 67 accept'; do
  if grep -qE -e "$r" <<<"$lock_rules"; then pass "lock: allows only $r"; else fail "lock: missing $r"; fi
done
check  "install: unit runs before the network"           "$GI" '^Before=network-pre.target$'
check  "install: unit restarts with nftables (its stop flushes the ruleset)" "$GI" '^PartOf=nftables.service$'
check  "install: unit enabled early (sysinit)"           "$GI" '^WantedBy=sysinit.target$'
check  "install: root-owned copy of the script"          "$GI" 'install -m 0755 -o root -g root "\$NEW/lima/guest-contained-net.sh" /usr/local/sbin/wmf-claude-contained-net'
check  "vm: create --contained ends locked"                    "$VM" '"\$\(contained_net lock\)" == locked'
check  "vm: sessions only when locked"                   "$VM" '\[\[ "\$net" == locked \]\] \|\| die'
check  "vm: update refuses a locked VM"                  "$VM" 'is_contained && \[\[ "\$\(contained_phase\)" == locked \]\]'
check  "vm: no lock or unlock during a session"          "$VM" '! contained_session_running \|\| die'
check  "vm: check-host-block restores the lock on exit" "$VM" "trap 'if \\\$\\{RELOCK:-false\\}; then contained_net lock >/dev/null; fi' EXIT"
check  "vm: check-host-block tests by IP, not by name"  "$VM" '--resolve "deb.debian.org:443:\$ip"'
check  "vm: sessions honour WMF_CLAUDE_REQUIRE_HOST_BLOCK" "$VM" 'WMF_CLAUDE_REQUIRE_HOST_BLOCK:-'
check  "vm: an interactive session waits on the warning" "$VM" 'Press Enter to start the session anyway'
check  "vm: unlock clears the host-block verification"  "$VM" 'rm -f "\$HOST_BLOCK_RECORD"'
# host_block_state: verified only for an ok record of the current limactl.
HB=$(mktemp -d); mkdir -p "$HB/bin"; printf '#!/bin/sh\n' > "$HB/bin/limactl"; chmod +x "$HB/bin/limactl"
hb_fns="$(grep '^limactl_binary()' "$VM")
$(awk '/^host_block_state\(\) \{/,/^\}/' "$VM")"
hb() { PATH="$HB/bin:$PATH" bash -c "set -euo pipefail; HOST_BLOCK_RECORD='$HB/rec'; $hb_fns
  host_block_state"; }
real=$(PATH="$HB/bin:$PATH" python3 -c 'import os, shutil; print(os.path.realpath(shutil.which("limactl")))')
[[ "$(hb)" == unchecked ]] && pass "host block: no record is 'unchecked'" || fail "host block: no record gave $(hb)"
echo "ok 2026-01-01T00:00:00Z $real" > "$HB/rec"
[[ "$(hb)" == verified ]] && pass "host block: an ok record for this limactl is 'verified'" || fail "host block: ok record gave $(hb)"
echo "ok 2026-01-01T00:00:00Z /old/lima/2.1.0/bin/limactl" > "$HB/rec"
[[ "$(hb)" == stale ]] && pass "host block: a record for another limactl is 'stale'" || fail "host block: other limactl gave $(hb)"
echo "fail 2026-01-01T00:00:00Z $real" > "$HB/rec"
[[ "$(hb)" == failed ]] && pass "host block: a failed record is 'failed'" || fail "host block: failed record gave $(hb)"
rm -rf "$HB"
# The phase logic, with a fake nft and id, in a scratch directory.
NT=$(mktemp -d); mkdir -p "$NT/bin" "$NT/etc"
printf '#!/bin/sh\necho 0\n' > "$NT/bin/id"
cat > "$NT/bin/nft" <<'FAKE'
#!/bin/bash
# Fake nft: a file stands for the loaded table.
case "$1 $2" in
  "-f -") cat > /dev/null; touch "$NFT_STATE" ;;
  "list table") [[ -e "$NFT_STATE" ]] ;;
  "delete table") rm -f "$NFT_STATE" ;;
esac
FAKE
chmod +x "$NT/bin/id" "$NT/bin/nft"
net() { NFT_STATE="$NT/table" WMF_CLAUDE_GUEST_CONTAINED_DIR="$NT/etc" PATH="$NT/bin:$PATH" bash "$NET" "$@" 2>&1; }
out=$(net status)
if grep -q 'not a contained VM' <<<"$out"; then pass "net: refuses without the contained marker"; else fail "net without marker: $out"; fi
touch "$NT/etc/contained"
if [[ "$(net status)" == broken ]]; then pass "net: no phase file and no table is 'broken' (sessions refused)"; else fail "net: missing phase gave $(net status)"; fi
if [[ "$(net apply)" == locked && "$(net status)" == locked ]]; then pass "net: a missing phase applies as locked"; else fail "net: apply without a phase did not lock"; fi
echo garbage > "$NT/etc/net-phase"
if [[ "$(net apply)" == locked ]]; then pass "net: an unknown phase applies as locked"; else fail "net: unknown phase did not lock"; fi
if [[ "$(net open)" == open && "$(net status)" == open && ! -e "$NT/table" ]]; then pass "net: open removes the table"; else fail "net: open"; fi
if [[ "$(net apply)" == open && ! -e "$NT/table" ]]; then pass "net: apply keeps an open phase open"; else fail "net: apply of open"; fi
if [[ "$(net lock)" == locked && "$(net status)" == locked && -e "$NT/table" ]]; then pass "net: lock loads the table"; else fail "net: lock"; fi
rm -f "$NT/table"
if [[ "$(net status)" == broken ]]; then pass "net: phase locked without the table is 'broken'"; else fail "net: unloaded table not detected"; fi
rm -rf "$NT"
# guest-claude.sh refuses mismatches before it starts anything.
GT=$(mktemp -d); mkdir -p "$GT/home/work/ws"
gc() { WMF_CLAUDE_GUEST_AGENT_HOME="$GT/home" WMF_CLAUDE_GUEST_CONTAINED_MARKER="$GT/contained" bash "$GC" "$@" 2>&1; }
out=$(touch "$GT/contained"; gc ws)
if grep -q 'this is a contained VM' <<<"$out"; then pass "guest-claude: contained VM without --model-proxy-port refused"; else fail "guest-claude: contained VM without --model-proxy-port: $out"; fi
out=$(gc --model-proxy-port 7319 ws --docker=mediawiki)
if grep -q "network is not locked" <<<"$out"; then pass "guest-claude: --docker in a contained VM gets to the lock check"; else fail "guest-claude: --docker in a contained VM: $out"; fi
out=$(gc --model-proxy-port 7319 ws)
if grep -q "network is not locked" <<<"$out"; then pass "guest-claude: a contained session needs the lock"; else fail "guest-claude: contained session without the lock: $out"; fi
out=$(rm -f "$GT/contained"; gc --model-proxy-port 7319 ws)
if grep -q 'not a contained VM' <<<"$out"; then pass "guest-claude: --model-proxy-port refused outside a contained VM"; else fail "guest-claude: --model-proxy-port outside a contained VM: $out"; fi
rm -rf "$GT"

echo ""
echo "========================="
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
