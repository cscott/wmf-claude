#!/usr/bin/env bash
# Track B4 acceptance (HANDOFF-LIMA.md track B), the parts a script can
# do: two creates of the same repos at once, and a golden image rebuilt
# under sandboxes that still sit on the old one. (The blind run in a
# sandbox from a warm snapshot is a session, not a script.) Runs INSIDE a
# nested sandbox, as the agent, from a wmf-claude checkout at the commit
# under test, in a session (under nono) or from the host (outside nono):
#
#   bash lima-port/checks/b4.sh all              # in the session
#   wmf-sbx exec NAME -- bash lima-port/checks/b4.sh all
#
# Phases (`all` runs them in order; each can run alone):
#   base        make sure B4_REPO has a snapshot (a cold create if not)
#   concurrent  two warm creates of B4_REPO at once, with
#               images.snapshotInterval 0: both work, both freeze (one
#               after the other: the key's lock), each on its own new
#               generation
#   rebuild     a sandbox on the current golden image, stopped; then a
#               create with images.maxAge 0: it builds a new golden image
#               and is cold on it. The old sandbox still starts and
#               passes `status`; `image gc` keeps its files; after it is
#               removed, `image prune` removes the old image and its
#               snapshots, and the new sandbox still passes `status`
#   clean       remove this script's sandboxes (`image prune` removes
#               the rest of the snapshots)
#
# WMF_SBX: the wmf-sbx to test (default: sbx/bin/wmf-sbx of this checkout).
# B4_REPO: what to create (default gerrit:mediawiki/extensions/Cite).
# B4_MEMORY: each inner sandbox's memory (default 3GiB: `concurrent` runs
# two at once, in a nested sandbox of 10 GiB).
#
# Every phase ends with `== RESULT ...` lines. Send all the output.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)
WMF_SBX=${WMF_SBX:-${HERE:+$HERE/../../sbx/bin/wmf-sbx}}
[ -x "${WMF_SBX:-}" ] || { echo "== RESULT setup: no wmf-sbx (set WMF_SBX)"; exit 1; }
REPO=${B4_REPO:-gerrit:mediawiki/extensions/Cite}
MEMORY=${B4_MEMORY:-3GiB}
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/wmf-sbx/sandboxes
SETTINGS=${XDG_CONFIG_HOME:-$HOME/.config}/wmf-sbx/settings.yaml
LOGS=/tmp/claude-$(id -u)/b4
mkdir -p "$LOGS"
NAMES="b4base b4a b4b b4old b4new"

result() { echo "== RESULT $*"; }
phase() { echo; echo "== $* ($(date '+%H:%M:%S'); free: $(free -m | awk '/^Mem:/ {print $7}') MiB)"; }
sbx() { echo "+ wmf-sbx $*" >&2; "$WMF_SBX" "$@"; }
st() { jq -r "$2 // empty" "$STATE/$1.json" 2>/dev/null; }
exists() { [ -f "$STATE/$1.json" ]; }
golden_of() { st "$1" .image; }

# create NAME [FLAGS...]: in the background with &, or not; output in
# $LOGS/NAME.log.
create() {
  local name=$1; shift
  "$WMF_SBX" create --name "$name" --memory "$MEMORY" "$@" "$REPO" > "$LOGS/$name.log" 2>&1
}

report() {
  local name=$1 rc=$2 secs=$3
  if [ "$rc" = 0 ]; then
    echo "-- $name: create in $secs s; $(grep -m1 -E '^\+ (warm|cold)' "$LOGS/$name.log")"
  else
    echo "-- $name: create FAILED (exit $rc) after $secs s; tail:"
    tail -25 "$LOGS/$name.log" | sed 's/^/   | /'
  fi
}

status_ok() {
  "$WMF_SBX" status "$1" > "$LOGS/status-$1.log" 2>&1 && return 0
  sed 's/^/   | /' "$LOGS/status-$1.log"
  return 1
}

phase_base() {
  phase base
  if "$WMF_SBX" image ls 2>/dev/null | grep -q '^snapshot'; then
    echo "-- there is a snapshot already:"
    "$WMF_SBX" image ls 2>/dev/null | grep '^snapshot' | sed 's/^/   | /'
  fi
  local t0=$SECONDS
  echo "-- b4base: create (its output: $LOGS/b4base.log)"
  create b4base; report b4base $? $((SECONDS - t0))
  exists b4base || { result "base: FAILED"; return 1; }
  sbx stop b4base >/dev/null 2>&1
  result "base: ok (snapshot $(st b4base .snapshot))"
}

phase_concurrent() {
  phase concurrent
  local t0=$SECONDS pa pb ra rb fails=0
  echo "-- b4a and b4b: two creates at once ($LOGS/b4a.log, $LOGS/b4b.log)"
  local cfg=$LOGS/settings-interval0.yaml
  { [ -f "$SETTINGS" ] && grep -v '^images:' "$SETTINGS"; printf 'images:\n  snapshotInterval: 0m\n'; } > "$cfg"
  create b4a --config "$cfg" & pa=$!
  sleep 2
  create b4b --config "$cfg" & pb=$!
  wait "$pa"; ra=$?
  wait "$pb"; rb=$?
  report b4a "$ra" $((SECONDS - t0))
  report b4b "$rb" $((SECONDS - t0))
  [ "$ra" = 0 ] && [ "$rb" = 0 ] || fails=$((fails + 1))
  for n in b4a b4b; do
    grep -E '^\+ (snapshot|limactl start .*on a new overlay|qemu-img rebase)' "$LOGS/$n.log" \
      | sed "s/^/   | $n: /"
    grep -q 'snapshot of .* failed' "$LOGS/$n.log" && { echo "-- FAIL: $n's freeze failed"; fails=$((fails + 1)); }
    status_ok "$n" || { echo "-- FAIL: status $n"; fails=$((fails + 1)); }
  done
  local sa sb; sa=$(st b4a .snapshot); sb=$(st b4b .snapshot)
  echo "-- b4a on $sa"
  echo "-- b4b on $sb"
  [ -n "$sa" ] && [ -n "$sb" ] && [ "$sa" != "$sb" ] || { echo "-- FAIL: not two generations"; fails=$((fails + 1)); }
  "$WMF_SBX" image ls 2>/dev/null | grep '^snapshot' | sed 's/^/   | /'
  sbx stop b4a >/dev/null 2>&1; sbx stop b4b >/dev/null 2>&1
  [ $fails = 0 ] && result "concurrent: ok" || result "concurrent: $fails FAILED"
}

phase_rebuild() {
  phase rebuild
  local fails=0 t0 old new cfg
  fail() { echo "-- FAIL: $*"; fails=$((fails + 1)); }
  t0=$SECONDS
  echo "-- b4old: on the current golden image ($LOGS/b4old.log)"
  create b4old; report b4old $? $((SECONDS - t0))
  exists b4old || { result "rebuild: FAILED"; return 1; }
  old=$(golden_of b4old)
  sbx stop b4old >/dev/null 2>&1
  # The settings, with images.maxAge 0: the next create builds anew.
  cfg=$LOGS/settings-maxage0.yaml
  { [ -f "$SETTINGS" ] && grep -v '^images:' "$SETTINGS"; printf 'images:\n  maxAge: 0m\n'; } > "$cfg"
  t0=$SECONDS
  echo "-- b4new: with images.maxAge 0 ($LOGS/b4new.log; it builds an image)"
  create b4new --config "$cfg"; report b4new $? $((SECONDS - t0))
  exists b4new || { result "rebuild: FAILED"; return 1; }
  new=$(golden_of b4new)
  grep -E '^\+ golden image' "$LOGS/b4new.log" | sed 's/^/   | /'
  echo "-- golden images: old $old, new $new"
  [ -n "$new" ] && [ "$new" != "$old" ] || fail "no new golden image"
  grep -q '^+ cold' "$LOGS/b4new.log" || fail "b4new was not cold"
  sbx stop b4new >/dev/null 2>&1
  # The old sandbox still starts, on the old image and its snapshot.
  sbx start b4old >/dev/null 2>&1 || fail "start b4old"
  status_ok b4old || fail "status b4old"
  grep '^disk' "$LOGS/status-b4old.log" | sed 's/^/   | /'
  sbx stop b4old >/dev/null 2>&1
  sbx image gc
  status_ok b4old || fail "image gc took b4old's files"
  "$WMF_SBX" image ls 2>/dev/null | sed 's/^/   | /'
  # Without the old sandbox (and the earlier phases' ones), prune
  # removes the old image and its snapshots; the new sandbox stays sound.
  for n in b4old b4base b4a b4b; do sbx rm --force "$n" >/dev/null 2>&1; done
  sbx image prune
  "$WMF_SBX" image ls 2>/dev/null | sed 's/^/   | /'
  "$WMF_SBX" image ls 2>/dev/null | grep -q "^$old " && fail "the old image $old is still there"
  status_ok b4new || fail "status b4new after the prune"
  [ $fails = 0 ] && result "rebuild: ok" || result "rebuild: $fails FAILED"
}

phase_clean() {
  phase clean
  for n in $NAMES; do exists "$n" && sbx rm --force "$n" >/dev/null 2>&1; done
  "$WMF_SBX" image ls 2>/dev/null | sed 's/^/   | /'
  result "clean: ok (\`wmf-sbx image prune\` removes the rest)"
}

[[ $(id -u) != 0 ]] || { echo "run as the agent, not root"; exit 1; }
echo "== wmf-sbx: $WMF_SBX ($(git -C "$(dirname "$WMF_SBX")" log -1 --format='%h %s' 2>/dev/null))"
echo "== $(limactl --version 2>&1), $(qemu-img --version | head -1)"
case "${1:-}" in
  base|concurrent|rebuild|clean) "phase_$1" ;;
  all) phase_base && phase_concurrent; phase_rebuild; phase_clean ;;
  *) sed -n '2,32p' "${BASH_SOURCE[0]:-/dev/stdin}"; exit 2 ;;
esac
