#!/bin/bash
# The in-VM half of the sandbox's repositories (lima-port/HANDOFF-LIMA.md
# §6.1, D8, D10). repos.py sends this file to `bash -s` in the guest and
# adds one call per repository after it:
#
#   prepare MOUNT PATH OWNER     as root, before the clones
#   clone_repo MOUNT PATH BRANCH SHA UPSTREAM RESET
#                                as OWNER (agent, or engineer for ':ro')
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
  local mount=$1 path=$2 owner=$3
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
  make_dirs "$path" "$owner"
}

# -- the clone's owner ---------------------------------------------------

# `--shared`: the clone borrows the host's objects through the alternates
# file, so nothing is copied; the host suspends gc for it (remotes.py).
# The clone is on the host's branch, or detached at the host's commit.
clone_repo() {
  local mount=$1 path=$2 branch=$3 sha=$4 upstream=$5 reset=$6 remote alt
  echo "+ git clone --shared $mount $path" >&2
  if [[ -n "$branch" ]]; then
    git -c core.hooksPath=/dev/null clone -q --shared -b "$branch" -- "$mount" "$path"
  else
    git -c core.hooksPath=/dev/null clone -q --shared --no-checkout -- "$mount" "$path"
    git -C "$path" -c core.hooksPath=/dev/null checkout -q --detach "$sha"
  fi
  while IFS= read -r alt; do
    if [[ ! -d "$alt" ]]; then
      echo "wmf-sbx: $path borrows objects from $alt, which does not exist" >&2
      return 1
    fi
  done < "$path/.git/objects/info/alternates"

  # git does not copy hooks. Seed Gerrit's commit-msg hook from the host
  # repo, so that commits get a Change-Id.
  if [[ -f "$path/.gitreview" && -f "$mount/hooks/commit-msg" ]]; then
    install -m 0755 -- "$mount/hooks/commit-msg" "$path/.git/hooks/commit-msg"
  fi

  # `origin` is the real upstream, `local` the host's repository, as in
  # sbx (configure_remotes, DESIGN-setup-steps.md §8.1). Without an
  # upstream, `origin` stays the host's repository.
  remote=origin
  if [[ -n "$upstream" ]]; then
    git -C "$path" remote rename origin local
    git -C "$path" remote add origin "$upstream"
    if git -C "$path" fetch -q origin; then
      remote=origin
    else
      warn "$path: could not fetch $upstream; git safe-reset uses local"
      remote=local
    fi
    # Two remotes have a `master`; tell `git checkout master` which.
    git -C "$path" config checkout.defaultRemote "$remote"
  fi

  if [[ "$reset" == 1 ]]; then
    echo "+ git safe-reset --force $remote ($path)" >&2
    git -C "$path" safe-reset --force "$remote" >/dev/null \
      || warn "$path: git safe-reset $remote failed; it stays on the host's commit"
  fi
}
