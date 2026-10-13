#!/usr/bin/env bash
# Track B2 acceptance (HANDOFF-LIMA.md track B): copy-on-write disks
# through wmf-sbx itself. Runs INSIDE a nested sandbox (`create --nested`),
# as the agent, from a wmf-claude checkout at the commit under test:
# either in a session (under nono, as the agent works) or piped in from
# the host (outside nono):
#
#   bash lima-port/checks/overlay-b2.sh all              # in the session
#   wmf-sbx exec NAME -- bash -s all < lima-port/checks/overlay-b2.sh
#
# Phases (`all` runs them in order; each can run alone):
#   image    build the golden image (or find it in the cache)
#   full     the baseline: a sandbox with --full-disk (a full copy); its
#            create time and disk use
#   overlay  two sandboxes on overlays: create time and disk use, the
#            backing chain, `status`; both run at once; each has its own
#            machine-id and SSH host key; 200 MiB written in one stays
#            there; the golden checksum does not change; stop and start;
#            `image rm` of the golden image is refused
#   gc       `image gc` keeps the image in use
#   clean    remove the three sandboxes; the golden image stays and
#            still matches its checksum
#
# WMF_SBX: the wmf-sbx to test (default: sbx/bin/wmf-sbx of this checkout).
# B2_REPO: the repo the sandboxes get (default: a small new git repo in
# the nested sandbox's wmf-sbx-src directory). The sandboxes use --no-deps.
#
# Every phase ends with `== RESULT ...` lines. Send all the output.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)
WMF_SBX=${WMF_SBX:-${HERE:+$HERE/../../sbx/bin/wmf-sbx}}
[ -x "${WMF_SBX:-}" ] || WMF_SBX=$(command -v wmf-sbx || true)
[ -x "${WMF_SBX:-}" ] || { echo "== RESULT setup: no wmf-sbx (set WMF_SBX)"; exit 1; }
LIMA_HOME=${LIMA_HOME:-$HOME/.lima}
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/wmf-sbx/images

result() { echo "== RESULT $*"; }
sbx() { echo "+ wmf-sbx $*" >&2; "$WMF_SBX" "$@"; }
kib() { du -k "$1" 2>/dev/null | cut -f1; }
disk_of() { echo "$LIMA_HOME/wmf-sbx-$1/disk"; }

repo() {
  if [ -n "${B2_REPO:-}" ]; then echo "$B2_REPO"; return; fi
  local src
  src=$(sed -n 's|^ *path: "\(.*\)/core"$|\1|p' "$HOME/.config/wmf-sbx/settings.yaml" 2>/dev/null | head -1)
  src=${src:-$HOME/wmf-sbx-src}
  local r=$src/b2-demo
  if [ ! -d "$r/.git" ]; then
    mkdir -p "$r" && git -C "$r" init -q -b main \
      && echo "B2 check" > "$r/README" && git -C "$r" add README \
      && git -C "$r" -c user.name=b2 -c user.email=b2@example.org commit -q -m "B2 check"
  fi
  echo "$r"
}

golden() {
  # The key of the newest image, from `image ls` (newest last).
  local key
  key=$("$WMF_SBX" image ls 2>/dev/null | tail -1 | cut -d' ' -f1)
  [ -n "$key" ] && echo "$CACHE/$key/golden.qcow2"
}

timed_create() {
  local name=$1; shift
  local t0=$SECONDS
  if sbx create --no-deps --name "$name" "$@" "$(repo)" > "/tmp/claude-$(id -u)/b2-$name.log" 2>&1 \
       || { tail -30 "/tmp/claude-$(id -u)/b2-$name.log"; false; }; then
    echo "-- $name: create in $((SECONDS - t0)) s; disk $(kib "$(disk_of "$name")") KiB" \
         "(apparent $(du -k --apparent-size "$(disk_of "$name")" | cut -f1) KiB)"
  else
    result "$name: create FAILED"; return 1
  fi
}

phase_image() {
  local t0=$SECONDS
  sbx image build || { result "image: build FAILED"; return 1; }
  local g; g=$(golden)
  echo "-- golden: $g, $(kib "$g") KiB, built or found in $((SECONDS - t0)) s"
  qemu-img info "$g" | sed 's/^/   | /'
  sha256sum "$g" > "/tmp/claude-$(id -u)/b2-golden.sha256"
  result "image: ok"
}

phase_full() {
  timed_create b2full --full-disk || return 1
  qemu-img info -U "$(disk_of b2full)" | grep -E "backing|format|virtual" | sed 's/^/   | /'
  sbx stop b2full >/dev/null 2>&1
  result "full: ok (baseline)"
}

phase_overlay() {
  local fails=0 g; g=$(golden)
  fail() { echo "-- FAIL: $*"; fails=$((fails + 1)); }
  timed_create b2a || return 1
  timed_create b2b || return 1
  echo "-- chain of b2a:"
  qemu-img info -U --backing-chain "$(disk_of b2a)" | grep -E "^image:|backing file:|file format" | sed 's/^/   | /'
  local backing
  backing=$(qemu-img info -U --output=json "$(disk_of b2a)" | jq -r '.["full-backing-filename"] // empty')
  [ "$backing" = "$g" ] || fail "b2a's backing file is '$backing', not $g"
  for n in b2a b2b; do
    "$WMF_SBX" status "$n" > "/tmp/claude-$(id -u)/b2-status-$n.log" 2>&1 || fail "status $n"
    grep '^disk' "/tmp/claude-$(id -u)/b2-status-$n.log" | sed "s/^/   | $n: /"
  done
  local st; st=$("$WMF_SBX" ls 2>/dev/null | grep -cE "b2[ab].*Running" || true)
  [ "$st" = 2 ] || echo "-- note: \`wmf-sbx ls\` shows $st of 2 running (check by hand)"
  local ida idb ka kb
  ida=$("$WMF_SBX" exec b2a -- cat /etc/machine-id); idb=$("$WMF_SBX" exec b2b -- cat /etc/machine-id)
  ka=$("$WMF_SBX" exec b2a -- cat /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2)
  kb=$("$WMF_SBX" exec b2b -- cat /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2)
  echo "-- machine-id: $ida / $idb; host key: ${ka:0:16}... / ${kb:0:16}..."
  [ -n "$ida" ] && [ "$ida" != "$idb" ] || fail "the machine-ids are the same"
  [ -n "$ka" ] && [ "$ka" != "$kb" ] || fail "the SSH host keys are the same"
  local a0 b0 a1 b1
  a0=$(kib "$(disk_of b2a)"); b0=$(kib "$(disk_of b2b)")
  "$WMF_SBX" exec b2a -- sh -c 'dd if=/dev/urandom of="$HOME/b2-fill" bs=1M count=200 status=none && sync'
  a1=$(kib "$(disk_of b2a)"); b1=$(kib "$(disk_of b2b)")
  echo "-- after 200 MiB in b2a: b2a $a0 -> $a1 KiB, b2b $b0 -> $b1 KiB"
  [ $((a1 - a0)) -ge 150000 ] || fail "b2a's disk did not grow by about 200 MiB"
  [ $((b1 - b0)) -lt 20000 ] || fail "b2b's disk grew too"
  sha256sum -c --quiet "/tmp/claude-$(id -u)/b2-golden.sha256" || fail "the golden image changed"
  sbx stop b2a >/dev/null 2>&1 || fail "stop b2a"
  sbx start b2a >/dev/null 2>&1 || fail "start b2a"
  "$WMF_SBX" exec b2a -- test -f /home/agent/b2-fill || fail "b2a lost its file across stop and start"
  "$WMF_SBX" status b2a >/dev/null 2>&1 || fail "status b2a after the restart"
  local key; key=$(basename "$(dirname "$g")")
  if "$WMF_SBX" image rm "$key" > "/tmp/claude-$(id -u)/b2-rm.log" 2>&1; then
    fail "image rm of the golden image was not refused"
  else
    echo "-- image rm refused: $(cat "/tmp/claude-$(id -u)/b2-rm.log")"
  fi
  [ $fails = 0 ] && result "overlay: ok" || result "overlay: $fails FAILED"
}

phase_gc() {
  local g; g=$(golden)
  sbx image gc
  [ -f "$g" ] && result "gc: ok (the image in use stays)" || result "gc: FAILED (the image is gone)"
}

phase_clean() {
  local g; g=$(golden)
  for n in b2a b2b b2full; do sbx rm --force "$n" >/dev/null 2>&1; done
  for n in b2a b2b b2full; do
    [ -e "$LIMA_HOME/wmf-sbx-$n" ] && echo "-- FAIL: $n is still there"
  done
  if [ -f "$g" ] && sha256sum -c --quiet "/tmp/claude-$(id -u)/b2-golden.sha256"; then
    result "clean: ok (the golden image stays, unchanged)"
  else
    result "clean: FAILED (the golden image is gone or changed)"
  fi
}

[[ $(id -u) != 0 ]] || { echo "run as the agent, not root"; exit 1; }
mkdir -p "/tmp/claude-$(id -u)"
echo "== wmf-sbx: $WMF_SBX ($(git -C "$(dirname "$WMF_SBX")" log -1 --format='%h %s' 2>/dev/null))"
echo "== $(limactl --version 2>&1), $(qemu-img --version | head -1)"
case "${1:-}" in
  image|full|overlay|gc|clean) "phase_$1" ;;
  all) phase_image && phase_full && phase_overlay; phase_gc; phase_clean ;;
  *) sed -n '2,29p' "${BASH_SOURCE[0]:-/dev/stdin}"; exit 2 ;;
esac
