#!/usr/bin/env bash
# Track C, N1 spike (HANDOFF-LIMA.md): can the agent in a wmf-sbx sandbox
# run a nested Lima VM, first outside nono and then under the session's
# nono? This script runs INSIDE the sandbox VM. The host only pipes it in
# (`bash -s`), so the agent cannot change what the engineer runs:
#
#   wmf-sbx exec --engineer n1 -- sudo bash -s setup   < lima-port/checks/n1-spike.sh
#   wmf-sbx exec n1 -- bash -s outside                 < lima-port/checks/n1-spike.sh
#   wmf-sbx exec n1 -- bash -s nono                    < lima-port/checks/n1-spike.sh
#
# setup    (root) install QEMU, OVMF and Lima LIMA_VERSION; add the agent to
#          the kvm group.
# outside  (agent, outside nono) fetch a Debian cloud image into Lima's
#          cache, then create, start, use, stop and delete a small inner VM.
# nono     (agent) the same inner-VM test, under nono with the session's
#          own flags (upstream bin/claude, as `wmf-sbx resume` runs it),
#          plus the candidate grants below. A `nono` shim on PATH keeps
#          bin/claude's nono arguments and replaces the command after `--`
#          with the test, so no Claude Code and no tokens are involved.
#          Set N1_GRANTS to try other grants (one flag per word).
#
# Every phase prints a summary at the end: `== RESULT ...` lines. Send all
# the output.
set -uo pipefail

LIMA_VERSION=2.2.1
INNER=n1-inner
SSH_PORT=50022          # in the session's ephemeral window (49152-53247)
WORK=$HOME/.cache/n1-spike
IMAGE_URL=https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2

# The grants a nested Lima probably needs under nono (HANDOFF-LIMA.md, N1).
DEFAULT_GRANTS="--allow $HOME/.lima
  --allow-unix-socket-subtree-bind $HOME/.lima
  --allow-file /dev/kvm
  --read /usr/share/qemu --read /usr/share/seabios --read /usr/share/OVMF
  --read /usr/share/ovmf --read /usr/local/share/lima --read /usr/local/libexec/lima
  --listen-port $SSH_PORT"

result() { echo "== RESULT $*"; }

# -- setup (root) ---------------------------------------------------------

setup() {
  [[ $(id -u) == 0 ]] || { echo "setup runs as root (sudo bash -s setup)"; exit 1; }
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q && apt-get install -y -q qemu-system-x86 qemu-utils ovmf \
    || { result "setup: apt failed"; exit 1; }
  if ! /usr/local/bin/limactl --version 2>/dev/null | grep -q "$LIMA_VERSION"; then
    curl -fsSL "https://github.com/lima-vm/lima/releases/download/v$LIMA_VERSION/lima-$LIMA_VERSION-Linux-x86_64.tar.gz" \
      | tar -xz -C /usr/local || { result "setup: Lima download failed"; exit 1; }
  fi
  usermod -aG kvm agent
  ls -l /dev/kvm
  id agent
  result "setup: ok: $(/usr/local/bin/limactl --version), $(qemu-system-x86_64 --version | head -1)"
}

# -- the inner VM test (agent) ----------------------------------------------

# Written to a file for the nono phase, so it must not use anything from
# outside the function but its arguments and the environment.
inner_test() {
  local label=$1 work=$2 port=$3 name=$4
  local tmpl=$work/inner.yaml log=$work/$label.log t0 rc=0
  : > "$log"
  say() { echo "$*" | tee -a "$log"; }
  say "== $label: $(id), kvm: $(ls -l /dev/kvm 2>&1)"
  limactl delete --force "$name" >/dev/null 2>&1
  t0=$SECONDS
  if ! limactl create --tty=false --name "$name" "$tmpl" >> "$log" 2>&1; then
    say "== RESULT $label: create FAILED"; tail -20 "$log"; return 1
  fi
  if ! limactl start --tty=false --timeout 10m "$name" >> "$log" 2>&1; then
    say "== RESULT $label: start FAILED after $((SECONDS - t0)) s"
    tail -25 "$log"
    for f in ha.stderr.log serial.log; do
      [[ -f $HOME/.lima/$name/$f ]] && { echo "-- tail $f"; tail -15 "$HOME/.lima/$name/$f"; }
    done
    limactl delete --force "$name" >/dev/null 2>&1
    return 1
  fi
  say "== $label: started in $((SECONDS - t0)) s"
  say "-- accel: $(ps -o args= -C qemu-system-x86_64 | grep -o -- '-accel [a-z]*\|-enable-kvm\|accel=[a-z]*' | sort -u | tr '\n' ' ')"
  say "-- uname: $(limactl shell --workdir / "$name" uname -sr 2>&1)"
  say "-- inner cpus: $(limactl shell --workdir / "$name" nproc 2>&1)"
  # Egress of the inner VM: QEMU's user network connects from this process.
  say "-- inner egress (https://deb.debian.org): $(limactl shell --workdir / "$name" \
       curl -s -o /dev/null -m 20 -w '%{http_code}' https://deb.debian.org/ 2>&1 || true)"
  limactl stop "$name" >> "$log" 2>&1 || { say "-- stop failed"; rc=1; }
  limactl delete --force "$name" >> "$log" 2>&1 || { say "-- delete failed"; rc=1; }
  say "== RESULT $label: ok: start, shell, stop, delete in $((SECONDS - t0)) s (rc $rc)"
  return $rc
}

prepare() {
  mkdir -p "$WORK"
  cat > "$WORK/inner.yaml" <<YAML
# The N1 spike's inner VM: no mounts, no port forwards, no containerd.
images:
- location: "$IMAGE_URL"
  arch: x86_64
vmType: qemu
cpus: 2
memory: 2GiB
disk: 10GiB
plain: true
ssh:
  localPort: $SSH_PORT
containerd:
  system: false
  user: false
YAML
}

outside() {
  [[ $(id -un) == agent ]] || { echo "run as the agent (wmf-sbx exec NAME -- ...)"; exit 1; }
  export PATH=/usr/local/bin:$PATH
  id -nG | grep -qw kvm || { result "outside: the agent is not in the kvm group; run setup"; exit 1; }
  prepare
  # The first start downloads the image into ~/.cache/lima; the nono
  # phase then needs no network for it.
  inner_test outside "$WORK" "$SSH_PORT" "$INNER"
}

# -- under nono ---------------------------------------------------------------

nono_phase() {
  [[ $(id -un) == agent ]] || { echo "run as the agent (wmf-sbx exec NAME -- ...)"; exit 1; }
  export PATH=/usr/local/bin:$PATH
  prepare
  ls "$HOME/.cache/lima" >/dev/null 2>&1 || echo "note: no Lima image cache; run the outside phase first"
  local grants=${N1_GRANTS:-$DEFAULT_GRANTS} shim=$WORK/shim
  mkdir -p "$shim"
  # The test, as a file that nono runs.
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    declare -f inner_test
    echo "export PATH=/usr/local/bin:\$PATH"
    # As upstream's launch-claude.sh does for a session: /tmp is write-only
    # under nono, and Lima builds its cloud-init ISO in TMPDIR (RAN: "open
    # /tmp/diskfs_iso.../05-lima-mounts.sh: permission denied").
    echo 'export TMPDIR="/tmp/claude-$(id -u)"; mkdir -p "$TMPDIR"'
    printf 'inner_test nono %q %q %q\n' "$WORK" "$SSH_PORT" "$INNER"
  } > "$WORK/inner-test.sh"
  # The nono shim: bin/claude's nono arguments, our command.
  cat > "$shim/nono" <<SHIM
#!/usr/bin/env bash
# Only \`nono run ... -- CMD\` is changed; anything else (--version, ...) passes.
[[ \${1:-} == run && " \$* " == *" -- "* ]] || exec /usr/bin/nono "\$@"
args=()
for a in "\$@"; do [[ \$a == -- ]] && break; args+=("\$a"); done
printf '%q ' /usr/bin/nono "\${args[@]}" > "$WORK/nono-argv.txt"
exec /usr/bin/nono "\${args[@]}" -- bash "$WORK/inner-test.sh"
SHIM
  chmod +x "$shim/nono"
  # The session's flags, as `wmf-sbx resume` builds them (session.py,
  # launcher_argv), from the image's own tree.
  local primary=$PWD flags
  flags=$(PYTHONPATH=/opt/wmf-claude/sbx/src python3 - "$primary" <<'PY'
import shlex, sys
import wmf_sbx.session as s
argv = s.launcher_argv({"primaryDir": sys.argv[1], "repos": []}, proxy=s.upstream_proxy())
i = argv.index(s.LAUNCHER)
j = argv.index("--", i)
print(shlex.join(argv[i + 1:j]))
PY
) || { result "nono: could not compute the session's flags"; exit 1; }
  echo "-- session flags: $flags"
  echo "-- candidate grants: $(echo $grants)"
  # shellcheck disable=SC2086
  eval "set -- $flags"
  WMF_CLAUDE_NO_PAUSE=1 WMF_CLAUDE_NONO_VERBOSE=1 PATH="$shim:$PATH" \
    /opt/wmf-claude/bin/claude "$@" $grants -- 2>&1 | tee "$WORK/nono-run.log"
  local rc=${PIPESTATUS[0]}
  echo "-- nono argv (without the --open-port list): $(sed 's/--open-port [0-9]* //g' "$WORK/nono-argv.txt" 2>/dev/null | cut -c1-4000)"
  grep -q '== RESULT nono: ok' "$WORK/nono-run.log" \
    && result "nono: ok with the grants above" \
    || result "nono: FAILED (rc $rc); see the denials above"
}

case "${1:-}" in
  setup) setup ;;
  outside) outside ;;
  nono) nono_phase ;;
  *) sed -n '2,22p' "${BASH_SOURCE[0]:-/dev/stdin}"; exit 2 ;;
esac
