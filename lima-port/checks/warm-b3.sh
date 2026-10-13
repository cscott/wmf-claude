#!/usr/bin/env bash
# Track B3/B4 check (HANDOFF-LIMA.md track B): warm snapshots through
# wmf-sbx itself. Runs INSIDE a nested sandbox (`create --nested`), as the
# agent, from a wmf-claude checkout at the commit under test, in a session
# (under nono) or piped in from the host (outside nono):
#
#   bash lima-port/checks/warm-b3.sh all              # in the session
#   wmf-sbx exec NAME -- bash lima-port/checks/warm-b3.sh all
#
# Phases (`all` runs them in order; each can run alone):
#   cold   `create` of B3_REPO and its dependencies with no snapshot yet
#          (--cold): its time, and the snapshot it makes
#   warm   a commit on a new branch in the host repo of B3_REPO, then a
#          second `create` of the same repos: it must start from the
#          snapshot, check out that commit, have a working wiki, its own
#          machine-id, and make the next generation; its time
#   again  a third `create`, with nothing new: the warm time with no work
#   gc     `image gc` keeps the snapshots in use
#   clean  remove the sandboxes (the snapshots stay; `wmf-sbx image prune`
#          removes them)
#
# WMF_SBX: the wmf-sbx to test (default: sbx/bin/wmf-sbx of this checkout).
# B3_REPO: what to create (default gerrit:mediawiki/extensions/Cite).
# B3_MEMORY: each inner sandbox's memory (default 4GiB). A nested
# sandbox has 16 GiB, and two inner sandboxes at the default 8 GiB do not
# fit (the first run was cut off). Each sandbox stops after its checks.
#
# Every phase ends with `== RESULT ...` lines. Send all the output.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)
WMF_SBX=${WMF_SBX:-${HERE:+$HERE/../../sbx/bin/wmf-sbx}}
[ -x "${WMF_SBX:-}" ] || { echo "== RESULT setup: no wmf-sbx (set WMF_SBX)"; exit 1; }
REPO=${B3_REPO:-gerrit:mediawiki/extensions/Cite}
MEMORY=${B3_MEMORY:-4GiB}
LIMA_HOME=${LIMA_HOME:-$HOME/.lima}
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/wmf-sbx/sandboxes
LOGS=/tmp/claude-$(id -u)/warm-b3
mkdir -p "$LOGS"

result() { echo "== RESULT $*"; }
sbx() { echo "+ wmf-sbx $*" >&2; "$WMF_SBX" "$@"; }
st() { jq -r "$2" "$STATE/$1.json" 2>/dev/null; }
in_vm() { "$WMF_SBX" exec "$1" -- sh -c "$2" 2>&1; }

phase() { echo; echo "== $* ($(date '+%H:%M:%S'); free: $(free -m | awk '/^Mem:/ {print $7}') MiB)"; }

timed_create() {
  local name=$1; shift
  local t0=$SECONDS
  echo "-- $name: create (its output: $LOGS/$name.log)"
  if sbx create --name "$name" --memory "$MEMORY" "$@" "$REPO" > "$LOGS/$name.log" 2>&1; then
    echo "-- $name: create in $((SECONDS - t0)) s"
    grep -E '^\+ (warm|cold)|^\+ golden image|npm ci|skipping it|update\.php|composer update' \
      "$LOGS/$name.log" | sed 's/^/   | /' | head -40
    return 0
  fi
  echo "-- $name: create FAILED after $((SECONDS - t0)) s; tail:"
  tail -30 "$LOGS/$name.log" | sed 's/^/   | /'
  return 1
}

wiki_ok() {
  # The wiki's PHP runs and its database is current.
  in_vm "$1" 'cd "$(jq -r .env.MW_INSTALL_PATH ~/.claude/settings.json)" && php maintenance/run.php version' \
    | tail -1
}

primary() { st "$1" .primaryDir; }

# Record what the later phases compare, then stop it: one inner VM at a time.
done_with() {
  in_vm "$1" 'cat /etc/machine-id' > "$LOGS/$1.machine-id"
  sbx stop "$1" >/dev/null 2>&1 || echo "-- could not stop $1"
}

phase_cold() {
  phase cold
  timed_create b3a --cold || { result "cold: create FAILED"; return 1; }
  local snap; snap=$(st b3a .snapshot)
  echo "-- snapshot: $snap ($(du -k "$snap" 2>/dev/null | cut -f1) KiB)"
  "$WMF_SBX" status b3a > "$LOGS/status-b3a.log" 2>&1 || { cat "$LOGS/status-b3a.log"; result "cold: status FAILED"; return 1; }
  grep '^disk' "$LOGS/status-b3a.log" | sed 's/^/   | /'
  echo "-- wiki: $(wiki_ok b3a)"
  echo "-- borrower entries in $(primary b3a): $(git -C "$(primary b3a)" config --get-all wmfSbx.borrower | tr '\n' ' ')"
  done_with b3a
  [ -n "$snap" ] && [ -f "$snap" ] && result "cold: ok" || result "cold: no snapshot FAILED"
}

phase_warm() {
  phase warm
  local fails=0 host sha
  fail() { echo "-- FAIL: $*"; fails=$((fails + 1)); }
  host=$(primary b3a)
  [ -n "$host" ] || { result "warm: run cold first"; return 1; }
  git -C "$host" checkout -q -b "b3-check-$$" \
    && git -C "$host" -c user.name=b3 -c user.email=b3@example.org \
         commit -q --allow-empty -m "B3 check: a host commit after the snapshot"
  sha=$(git -C "$host" rev-parse HEAD)
  timed_create b3b || { result "warm: create FAILED"; return 1; }
  grep -q '^+ warm: from snapshot' "$LOGS/b3b.log" || fail "the create was not warm"
  local got; got=$(in_vm b3b "git -C '$host' rev-parse HEAD")
  [ "$got" = "$sha" ] && echo "-- the host's new commit is checked out" || fail "HEAD is $got, not $sha"
  echo "-- wiki: $(wiki_ok b3b)"
  local a b; a=$(cat "$LOGS/b3a.machine-id" 2>/dev/null); b=$(in_vm b3b 'cat /etc/machine-id')
  echo "-- machine-id: $a / $b"
  [ -n "$a" ] && [ "$a" != "$b" ] || fail "the machine-ids are the same"
  "$WMF_SBX" status b3b > "$LOGS/status-b3b.log" 2>&1 || fail "status b3b"
  grep '^disk' "$LOGS/status-b3b.log" | sed 's/^/   | /'
  [ "$(st b3b .snapshot)" != "$(st b3a .snapshot)" ] || fail "no new generation"
  "$WMF_SBX" image ls 2>/dev/null | grep '^snapshot' | sed 's/^/   | /'
  git -C "$host" checkout -q - 2>/dev/null
  done_with b3b
  [ $fails = 0 ] && result "warm: ok" || result "warm: $fails FAILED"
}

phase_again() {
  phase again
  timed_create b3c || { result "again: create FAILED"; return 1; }
  done_with b3c
  grep -q '^+ warm: from snapshot' "$LOGS/b3c.log" && result "again: ok" || result "again: not warm FAILED"
}

phase_gc() {
  phase gc
  sbx image gc
  local n=0
  for s in b3a b3b b3c; do
    local p; p=$(st "$s" .snapshot)
    [ -z "$p" ] || [ -f "$p" ] || { echo "-- FAIL: $s's snapshot $p is gone"; n=$((n + 1)); }
  done
  [ $n = 0 ] && result "gc: ok (the snapshots in use stay)" || result "gc: $n FAILED"
}

phase_clean() {
  phase clean
  for s in b3a b3b b3c; do sbx rm --force "$s" >/dev/null 2>&1; done
  "$WMF_SBX" image ls 2>/dev/null | grep '^snapshot' | sed 's/^/   | /'
  result "clean: ok (the snapshots stay until \`wmf-sbx image prune\`)"
}

[[ $(id -u) != 0 ]] || { echo "run as the agent, not root"; exit 1; }
echo "== wmf-sbx: $WMF_SBX ($(git -C "$(dirname "$WMF_SBX")" log -1 --format='%h %s' 2>/dev/null))"
echo "== $(limactl --version 2>&1), $(qemu-img --version | head -1)"
case "${1:-}" in
  cold|warm|again|gc|clean) "phase_$1" ;;
  all) phase_cold && phase_warm && phase_again; phase_gc; phase_clean ;;
  *) sed -n '2,29p' "${BASH_SOURCE[0]:-/dev/stdin}"; exit 2 ;;
esac
