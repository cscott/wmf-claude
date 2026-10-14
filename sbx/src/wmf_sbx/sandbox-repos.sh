#!/bin/bash
# The in-VM half of the sandbox's repositories (lima-port/HANDOFF-LIMA.md
# §6.1, D8, D10). repos.py sends this file to `bash -s` in the guest and
# adds one call per repository after it:
#
#   prepare MOUNT PATH OWNER [refresh]
#                                as root, before the clones
#   clone_repo MOUNT PATH BRANCH SHA UPSTREAM RESET
#                                as OWNER (agent, or engineer for ':ro')
#   refresh_repo MOUNT PATH BRANCH SHA UPSTREAM RESET
#                                the same, for a clone that a warm snapshot
#                                has already (track B3)
#
# MOUNT is the read-only mount of the host's git dir. PATH is the host
# path of the repository; the clone has the same path in the VM (D8).
set -euo pipefail

warn() { echo "warning: $*" >&2; }

# -- root ----------------------------------------------------------------

# Add a safe.directory entry to /etc/gitconfig once. The agent has the
# host uid (D10), so the mounted files are its own; this is defence in
# depth, and lets the agent read a ':ro' clone that the engineer owns.
add_safe() {
  git config --system --get-all safe.directory 2>/dev/null | grep -Fxq -- "$1" \
    || git config --system --add safe.directory "$1"
}

# Make the missing parents of PATH, owned by the agent, then PATH itself,
# empty and owned by OWNER. A PATH that exists must be empty.
make_dirs() {
  local p=$1 owner=$2 group
  group=$(id -gn agent)
  if [[ -e "$p" ]]; then
    if [[ -n "$(ls -A -- "$p")" ]]; then
      echo "wmf-sbx: $p exists in the VM and is not empty" >&2
      return 1
    fi
  else
    [[ -e "$(dirname -- "$p")" ]] || make_dirs "$(dirname -- "$p")" agent
    install -d -m 0755 -- "$p"
  fi
  chown "$owner:$group" -- "$p"
}

prepare() {
  local mount=$1 path=$2 owner=$3 refresh=${4:-}
  if [[ ! -d "$mount/objects" ]]; then
    echo "wmf-sbx: $mount is not mounted, or is not a git dir" >&2
    return 1
  fi
  add_safe "$mount"
  # A ':ro' clone is the engineer's. The agent's git opens it as PATH,
  # and upload-pack (git-remote-wmfsbx) as PATH/.git (RAN, phase 4).
  if [[ "$owner" != agent ]]; then
    add_safe "$path"
    add_safe "$path/.git"
  fi
  # A warm snapshot's clone is there already (refresh_repo).
  [[ -n "$refresh" && -d "$path/.git" ]] && return 0
  make_dirs "$path" "$owner"
}

# -- the clone's owner ---------------------------------------------------

# `--shared`: the clone borrows the host's objects through the alternates
# file, so nothing is copied; the host suspends gc for it (remotes.py).
# The clone is on the host's branch, or detached at the host's commit.
# The arguments after the sixth are the mounts of the object dirs that the
# host repo borrows from (repos.py). The clone needs them at once (its
# branch's commit may be there only), then lists them in its alternates.
clone_repo() {
  local mount=$1 path=$2 branch=$3 sha=$4 upstream=$5 reset=$6 remote alt
  local extra=("${@:7}") borrowed
  borrowed=$(IFS=:; echo "${extra[*]}")
  echo "+ git clone --shared $mount $path" >&2
  if [[ -n "$branch" ]]; then
    GIT_ALTERNATE_OBJECT_DIRECTORIES=$borrowed \
      git -c core.hooksPath=/dev/null clone -q --shared -b "$branch" -- "$mount" "$path"
  else
    GIT_ALTERNATE_OBJECT_DIRECTORIES=$borrowed \
      git -c core.hooksPath=/dev/null clone -q --shared --no-checkout -- "$mount" "$path"
  fi
  if [[ ${#extra[@]} -gt 0 && -f "$path/.git/objects/info/alternates" ]]; then
    printf '%s\n' "${extra[@]}" >> "$path/.git/objects/info/alternates"
  fi
  if [[ -z "$branch" ]]; then
    git -C "$path" -c core.hooksPath=/dev/null checkout -q --detach "$sha"
  fi
  # A shallow host repo gives no alternates: git copies the objects
  # instead (RAN: a `--depth` clone on the host).
  if [[ -f "$path/.git/objects/info/alternates" ]]; then
    while IFS= read -r alt; do
      if [[ ! -d "$alt" ]]; then
        echo "wmf-sbx: $path borrows objects from $alt, which does not exist" >&2
        return 1
      fi
    done < "$path/.git/objects/info/alternates"
  fi

  # git does not copy hooks. Seed Gerrit's commit-msg hook from the host
  # repo, so that commits get a Change-Id.
  if [[ -f "$path/.gitreview" && -f "$mount/hooks/commit-msg" ]]; then
    install -m 0755 -- "$mount/hooks/commit-msg" "$path/.git/hooks/commit-msg"
  fi

  remote=$(set_remotes "$path" "$mount" "$upstream" "$reset")
  if [[ "$reset" == 1 ]]; then
    echo "+ git safe-reset --force $remote ($path)" >&2
    git -C "$path" safe-reset --force "$remote" >/dev/null \
      || warn "$path: git safe-reset $remote failed; it stays on the host's commit"
  fi
  init_submodules "$mount" "$path"
}

# `origin` is the real upstream, `local` the host's repository, as in
# sbx (configure_remotes). Without an upstream, `origin` stays the host's
# repository. For a fresh clone (origin is the host's) or a warm one (as
# an earlier create left it). Prints the remote that a reset uses.
set_remotes() {
  local path=$1 mount=$2 upstream=$3 reset=$4
  if [[ -z "$upstream" && "$reset" == 1 ]]; then
    warn "$path: no upstream is known (no gerrit:/gitlab: name, no https origin on the host); it is reset to the host repo's master (or main), which may be behind upstream"
  fi
  if [[ -z "$upstream" ]]; then
    if git -C "$path" remote get-url local >/dev/null 2>&1; then
      git -C "$path" remote remove origin
      git -C "$path" remote rename local origin
    fi
    git -C "$path" remote set-url origin "$mount"
    echo origin
    return
  fi
  if git -C "$path" remote get-url local >/dev/null 2>&1; then
    git -C "$path" remote set-url local "$mount"
    git -C "$path" remote set-url origin "$upstream"
  else
    git -C "$path" remote rename origin local
    git -C "$path" remote add origin "$upstream"
  fi
  local remote=origin
  if ! git -C "$path" fetch -q origin; then
    warn "$path: could not fetch $upstream; git safe-reset uses local"
    remote=local
  fi
  # Two remotes have a `master`; tell `git checkout master` which.
  git -C "$path" config checkout.defaultRemote "$remote"
  echo "$remote"
}

# A clone from a warm snapshot (track B3): made by an earlier create,
# before any session, so it holds no work. It still borrows the host's
# objects (its alternates), so the host's new commits are there; fetch
# its refs, then check out what clone_repo would: the host's branch at
# the host's commit, or detached there, then the same reset. Its other
# local branches came from earlier creates' host branches: removed.
refresh_repo() {
  local mount=$1 path=$2 branch=$3 sha=$4 upstream=$5 reset=$6 remote host b
  if [[ ! -d "$path/.git" ]]; then
    clone_repo "$@"
    return
  fi
  echo "+ git fetch, checkout ${branch:-$sha} ($path, from the snapshot)" >&2
  remote=$(set_remotes "$path" "$mount" "$upstream" "$reset")
  host=origin
  [[ -n "$upstream" ]] && host=local
  git -C "$path" -c core.hooksPath=/dev/null fetch -q --prune "$host" \
    || warn "$path: could not fetch the host repo's branches"
  if [[ -n "$branch" ]]; then
    git -C "$path" -c core.hooksPath=/dev/null checkout -q -f -B "$branch" "$sha"
    git -C "$path" branch -q --set-upstream-to="$host/$branch" "$branch" 2>/dev/null || true
  else
    git -C "$path" -c core.hooksPath=/dev/null checkout -q -f --detach "$sha"
  fi
  git -C "$path" for-each-ref --format='%(refname:short)' refs/heads |
  while read -r b; do
    [[ "$b" == "$branch" ]] || git -C "$path" branch -q -D -- "$b"
  done
  if [[ -f "$path/.gitreview" && -f "$mount/hooks/commit-msg" ]]; then
    install -m 0755 -- "$mount/hooks/commit-msg" "$path/.git/hooks/commit-msg"
  fi
  if [[ "$reset" == 1 ]]; then
    echo "+ git safe-reset --force $remote ($path)" >&2
    git -C "$path" safe-reset --force "$remote" >/dev/null \
      || warn "$path: git safe-reset $remote failed; it stays on the host's commit"
  fi
  init_submodules "$mount" "$path"
}

# Submodules (wmf-claude's MCP servers, VisualEditor's lib/ve): from the
# host's own copies in the mounted git dir (modules/NAME), with no network.
# A commit the host does not have (a repo reset to upstream) is fetched
# from the URL in .gitmodules instead.
init_submodules() {
  local mount=$1 path=$2 key sub name
  [[ -f "$path/.gitmodules" ]] || return 0
  git -C "$path" config -f .gitmodules --get-regexp '^submodule\..*\.path$' |
  while read -r key sub; do
    name=${key#submodule.}; name=${name%.path}
    if [[ -d "$mount/modules/$name" ]]; then
      git -C "$path" config "submodule.$name.url" "$mount/modules/$name"
      if git -C "$path" -c protocol.file.allow=always -c core.hooksPath=/dev/null \
           submodule update --init -q -- "$sub" 2>/dev/null; then
        continue
      fi
      git -C "$path" config --unset "submodule.$name.url"
    fi
    # From its upstream. A try from the host's copy above may have cloned
    # the submodule already, with that copy as its origin, which lacks the
    # commit (RAN: VisualEditor's lib/ve, "did not contain"): point it at
    # the URL in .gitmodules first.
    git -C "$path" submodule init -q -- "$sub"
    git -C "$path" submodule sync -q -- "$sub"
    echo "+ git submodule update --init $sub (from its upstream; $path)" >&2
    git -C "$path" -c core.hooksPath=/dev/null submodule update --init -q -- "$sub" \
      || warn "$path: submodule $sub could not be checked out"
  done
}
