#!/usr/bin/env python3
"""List wmf-sbx sandbox remotes registered against the current repo.

Walks up from the current directory (or --path) looking for the
enclosing git repository, then scans every sandbox's host state file
(~/.local/state/wmf-sbx/sandboxes/*.json, respecting $XDG_STATE_HOME --
see wmf_sbx/state.py) for a `remotes[]` entry whose `hostDir` is that
same repo: a remote `wmf-sbx-create` actually wired up here, via
wmf_sbx/remotes.py's sync_remotes(), not just one that happens to be
named like a sandbox remote.

Written so `git-review-check` (sbx/bin/git-review-check) has a real
answer to "is REMOTE actually a sandbox remote for this repo", replacing
the name-prefix guess (`sandbox-*`) it used to make -- see sbx/NOTES.md
§48/§48.1. Also useful stand-alone: run in any repo to list the sandbox
remotes wmf-sbx thinks it registered there.
"""

import argparse
import json
import os
import sys

from . import state as state_mod


def find_repo_root(start=None):
    """Walk `start` (default: the current directory) upward looking for
    a `.git` entry -- a directory for a normal clone, a file for a
    worktree or submodule. Returns the realpath'd directory that holds
    it, or None if the filesystem root is reached first."""
    here = os.path.realpath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def find_remotes(repo_root, env=None):
    """Every {sandbox, hostDir, remote, url} entry, across all sandbox
    state files, whose hostDir realpath's to `repo_root` (already
    realpath'd by the caller, e.g. find_repo_root's return value).
    Sorted by sandbox name then remote name, for stable output."""
    matches = []
    for name in state_mod.list_names(env):
        saved = state_mod.load(name, env)
        if not saved:
            continue
        for entry in saved.get("remotes", []):
            host_dir = entry.get("hostDir") or ""
            if os.path.realpath(host_dir) != repo_root:
                continue
            matches.append({
                "sandbox": name,
                "hostDir": entry.get("hostDir"),
                "remote": entry.get("remote"),
                "url": entry.get("url"),
            })
    matches.sort(key=lambda m: (m["sandbox"], m["remote"] or ""))
    return matches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--path", default=None,
        help="Directory to start looking for the enclosing repo from "
        "(default: the current directory)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the full matching state entry for each remote "
        "(sandbox/hostDir/remote/url), one JSON object per line, instead "
        "of just the remote name",
    )
    args = parser.parse_args(argv)

    repo_root = find_repo_root(args.path)
    if repo_root is None:
        print("error: not inside a git repository.", file=sys.stderr)
        return 1

    for entry in find_remotes(repo_root):
        if args.json:
            print(json.dumps(entry))
        else:
            print(entry["remote"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
