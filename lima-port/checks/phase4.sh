#!/bin/bash
# Phase 4 checks (HANDOFF-LIMA.md §6, §11): the git-dir mounts and the
# git transport of one running sandbox, on the real VM.
#
# Usage: lima-port/checks/phase4.sh NAME PRIMARY [RO_REPO]
#
#   NAME     a sandbox made by `wmf-sbx create ... PRIMARY [RO_REPO:ro]`
#   PRIMARY  the host path of its primary repository
#   RO_REPO  the host path of a ':ro' repository of the sandbox
#
# wmf-sbx and git-remote-wmfsbx must be on PATH (sbx/bin). The script
# changes only throwaway branches (wmf-sbx-check-*), a temporary host
# worktree, the sandbox's clones, and refs/remotes/NAME/* (`git safe-reset`
# runs `git remote update`). It runs a plain `git gc` in PRIMARY. It also runs, as guest root, a
# `remount,rw` of a mount (the bypass test) and remounts it `ro` after.
# Exit status: the number of failed checks.
set -uo pipefail
NAME=${1:?usage: $0 NAME PRIMARY [RO_REPO]}
PRIMARY=${2:?usage: $0 NAME PRIMARY [RO_REPO]}
RO=${3:-}
INST=wmf-sbx-$NAME
MOUNT=/run/wmf-sbx/host$(git -C "$PRIMARY" rev-parse --path-format=absolute --git-common-dir)
TAG=wmf-sbx-check-$$
FAILS=0

pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
check() { local what=$1; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }
agent() { wmf-sbx exec "$NAME" -- "$@"; }
guest() { limactl shell --workdir / "$INST" -- "$@" 2>/dev/null; }
hgit() { git -C "$PRIMARY" -c user.name=check -c user.email=check@example.org "$@"; }

echo "== mounts"
check "the primary's git dir is mounted read-only" \
  bash -c "limactl shell --workdir / $INST -- findmnt -rn -o OPTIONS $MOUNT 2>/dev/null | grep -qE '(^|,)ro(,|$)'"
check "the agent's clone is at the host path, borrowing from the mount" \
  bash -c "wmf-sbx exec $NAME -- cat $PRIMARY/.git/objects/info/alternates | grep -qxF $MOUNT/objects"
check "the agent cannot write the mount" \
  bash -c "! wmf-sbx exec $NAME -- touch $MOUNT/agent-was-here 2>/dev/null"
guest sudo sh -c "mount -o remount,rw $MOUNT; touch $MOUNT/root-was-here" >/dev/null 2>&1
check "guest root's remount,rw cannot write the host repo either" \
  test ! -e "$(git -C "$PRIMARY" rev-parse --git-common-dir)/root-was-here"
guest sudo mount -o remount,ro "$MOUNT" >/dev/null 2>&1
if [[ -n "$RO" ]]; then
  check "the ':ro' clone is the engineer's" \
    bash -c "[[ \$(wmf-sbx exec $NAME -- stat -c %U $RO) == engineer ]]"
  check "the agent cannot write the ':ro' clone" \
    bash -c "! wmf-sbx exec $NAME -- touch $RO/agent-was-here 2>/dev/null"
fi

echo "== host to VM: a host commit is seen at once (git fetch local)"
hgit branch -q "$TAG-host" HEAD
for step in commit pack-refs repack gc; do
  hgit worktree add -q --detach "/tmp/$TAG-wt" "$TAG-host" 2>/dev/null
  git -C "/tmp/$TAG-wt" -c user.name=check -c user.email=check@example.org \
    commit -q --allow-empty -m "check $step"
  sha=$(git -C "/tmp/$TAG-wt" rev-parse HEAD)
  hgit branch -q -f "$TAG-host" "$sha"
  hgit worktree remove --force "/tmp/$TAG-wt"
  case $step in
    pack-refs) hgit pack-refs --all ;;
    repack) hgit repack -q -a -d ;;
    gc) hgit gc -q ;;  # prunes nothing: gc.pruneExpire is `never` while suspended
  esac
  seen=$(agent git -C "$PRIMARY" fetch -q local "$TAG-host" 2>/dev/null \
    && agent git -C "$PRIMARY" rev-parse FETCH_HEAD)
  check "after a host $step" test "$seen" = "$sha"
done

echo "== VM to host: git-remote-wmfsbx"
agent git -C "$PRIMARY" config uploadpack.packObjectsHook "touch /tmp/$TAG-hook-ran"
agent git -C "$PRIMARY" -c user.name=agent -c user.email=agent@example.org \
  commit -q --allow-empty -m "agent work" >/dev/null
agent git -C "$PRIMARY" branch -q "$TAG-agent"
work=$(agent git -C "$PRIMARY" rev-parse HEAD)
# `rm --dry-run` runs the unfetched-commit guard, and removes nothing.
check "the rm guard refuses while the agent's commit is not fetched" \
  bash -c "! wmf-sbx rm --dry-run $NAME >/dev/null 2>&1"
check "the host fetches the agent's commit" \
  bash -c "git -C $PRIMARY fetch -q $NAME $TAG-agent:refs/remotes/$NAME/$TAG-agent && \
           [[ \$(git -C $PRIMARY rev-parse $NAME/$TAG-agent) == $work ]]"
check "the rm guard passes once it is fetched" \
  bash -c "wmf-sbx rm --dry-run $NAME >/dev/null 2>&1"
check "the agent's uploadpack.packObjectsHook did not run" \
  bash -c "! limactl shell --workdir / $INST -- test -e /tmp/$TAG-hook-ran 2>/dev/null"
check "push to the sandbox is refused" \
  bash -c "! git -C $PRIMARY push $NAME $TAG-host:refs/heads/$TAG-pushed 2>/dev/null && \
           [[ -z \$(wmf-sbx exec $NAME -- git -C $PRIMARY branch --list $TAG-pushed) ]]"
hgit worktree add -q -b "$TAG-reset" "/tmp/$TAG-wt" "$TAG-host" 2>/dev/null
check "git safe-reset $NAME on the host" \
  bash -c "cd /tmp/$TAG-wt && git safe-reset --force $NAME $TAG-agent >/dev/null 2>&1 && \
           [[ \$(git rev-parse HEAD) == $work ]]"
hgit worktree remove --force "/tmp/$TAG-wt"
check "host gc is suspended" \
  bash -c "[[ \$(git -C $PRIMARY config gc.auto) == 0 && \$(git -C $PRIMARY config gc.pruneExpire) == never ]]"
check "the invariants hold (wmf-sbx status)" bash -c "wmf-sbx status $NAME >/dev/null 2>&1"

# The agent's branch and commit stay in the sandbox; the host keeps the
# fetched ref refs/remotes/NAME/..., so `rm` does not refuse for them.
agent git -C "$PRIMARY" config --unset uploadpack.packObjectsHook
hgit branch -q -D "$TAG-host" "$TAG-reset" "$TAG-agent" 2>/dev/null
echo "== $FAILS failed"
exit "$FAILS"
