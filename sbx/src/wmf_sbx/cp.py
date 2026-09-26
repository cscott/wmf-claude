#!/usr/bin/env python3
"""Copy files between a sandbox and the host, resolving a filesystem-path
shortcut embedded in a NAME:PATH argument first, and waiting for the
sandbox's mount layout to be back before trusting the copy.

`sbx cp [flags] SRC DST` requires exactly one of SRC/DST to be written
SANDBOX:PATH (reference:
~/Projects/Wikimedia/docs.docker.com/reference/cli/sbx/cp) -- the other
is an ordinary host path. Plain `sbx cp` knows nothing of the
`.`/`..`/absolute-path shortcuts `wmf_sbx.state.resolve_name_arg()` adds
elsewhere (sbx/NOTES.md "Allow shortcut sandbox names"), so
`wmf-sbx cp foo .:bar` would otherwise reach `sbx` with a literal `.` as
the sandbox name and fail. This module finds whichever of SRC/DST is
written NAME:PATH, resolves a path-shaped NAME to the sandbox it names,
and anchors a relative PATH at that sandbox's primary workspace --
upstream `sbx cp` requires an absolute container path, and a relative
one otherwise means nothing to it. The other argument is left entirely
untouched.

A sandbox `sbx cp` hasn't accessed yet is not running (MEASURED,
cananian, host, 2026-10-06 -- sbx/NOTES.md #103, and the pre-existing
#45.1): the first command to touch it starts its container as a side
effect, same as `sbx exec`, and races the startup dispatcher's restore
of the mount layout (sbx/NOTES.md #40/#46). `wmf-sbx exec`
(wmf_sbx.resume.start_and_restore) already waits that race out before
running its command; plain `sbx cp` does not, so a `cp` run immediately
after `wmf-sbx create` can report success while the mount underneath it
is still the stale one `sbx create`'s own setup left behind -- the
file lands nowhere the next mount swap keeps. This module now runs
start_and_restore for every sandbox named on either side before handing
off to upstream `cp`, exactly as `wmf-sbx exec` does for its own target.

Usage:
  wmf-sbx-cp [-L] [-D] SRC DST
"""

import argparse
import os
import subprocess
import sys

from . import create as create_mod
from . import resume as resume_mod
from . import state as state_mod


def resolve_cp_arg(arg, env=None):
    """If `arg` is written NAME:PATH, resolve NAME -- a path shortcut
    (state.is_path_shortcut) to the sandbox it names, or an already-valid
    sandbox name as itself -- and anchor a relative PATH at that
    sandbox's primary workspace. Otherwise return `arg` unchanged: a
    plain host path has no colon at all.

    Upstream `sbx cp` requires an absolute container path (MEASURED,
    cananian, 2026-09-29 -- sbx/NOTES.md #99: "container path must be
    absolute" for a bare relative PATH). ~/.claude/CLAUDE.md's repo
    layout mounts a workspace at the same absolute path inside the
    sandbox as on the host, so a relative PATH is anchored at the
    primary workspace's host directory -- the same directory
    `cd NAME && sbx cp ... PATH` would mean on the host. For a path
    shortcut that directory is the shortcut itself; for a real NAME it is
    that sandbox's recorded `primaryDir` (state.primary_dir_for) --
    which is unknown for a sandbox created before that field existed
    (MEASURED, cananian, 2026-10-06), so its PATH is left alone in that
    case; write an absolute container path there."""
    name, sep, path = arg.partition(":")
    if not sep:
        return arg
    if state_mod.is_path_shortcut(name):
        primary_dir = os.path.realpath(os.path.expanduser(name))
        resolved = state_mod.resolve_name_arg(name, env=env)
        state_mod.validate_name(resolved)
    else:
        resolved = name
        primary_dir = state_mod.primary_dir_for(name, env=env)
    if primary_dir is not None and not path.startswith("/"):
        path = f"{primary_dir}/{path}" if path else primary_dir
    return f"{resolved}{sep}{path}"


def _sandbox_name_in(resolved_arg):
    """The sandbox NAME half of an *already-resolved* `resolve_cp_arg`
    result (never a path shortcut by this point -- resolve_cp_arg always
    turns one into a real NAME first), or None if `resolved_arg` isn't
    written NAME:PATH at all, or its NAME half doesn't parse as one.
    Used to decide which sandbox(es) need start_and_restore before the
    real copy runs; see the module docstring."""
    name, sep, _ = resolved_arg.partition(":")
    if not sep:
        return None
    try:
        return state_mod.validate_name(name)
    except state_mod.StateError:
        return None


def main(argv=None, run=subprocess.run, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-L", "--follow-link", action="store_true",
        help="sbx cp's own flag: follow symbolic links in the source path",
    )
    parser.add_argument(
        "-D", "--debug", action="store_true",
        help="sbx's own global flag: enable debug logging",
    )
    parser.add_argument("src", help="Source: a host path, or SANDBOX:PATH")
    parser.add_argument("dst", help="Destination: a host path, or SANDBOX:PATH")
    args = parser.parse_args(argv)

    try:
        src = resolve_cp_arg(args.src, env=env)
        dst = resolve_cp_arg(args.dst, env=env)
    except state_mod.StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    for original, resolved in ((args.src, src), (args.dst, dst)):
        if resolved != original:
            print(f"+ resolved {original!r} to {resolved!r}", file=sys.stderr)

    # A sandbox `cp` hasn't touched yet is not running -- starting it and
    # waiting for its mount layout is exactly what `wmf-sbx exec` already
    # does for its own target (sbx/NOTES.md #45.1/#97); plain `sbx cp`
    # does neither, and races the restore instead (sbx/NOTES.md #103).
    # Do it for every sandbox named on either side, in order, before
    # trusting the copy to either one.
    names = []
    for resolved in (src, dst):
        name = _sandbox_name_in(resolved)
        if name and name not in names:
            names.append(name)
    for name in names:
        if not resume_mod.start_and_restore(name, run=run, env=env, claude_md=False):
            print(f"error: could not start sandbox {name!r}", file=sys.stderr)
            return 1

    cp_flags = []
    if args.follow_link:
        cp_flags.append("-L")

    # -D/--debug is `sbx`'s own global flag, not cp's -- it belongs before
    # the subcommand.
    #
    # --upstream comes first, ahead of -D: this module IS wmf-sbx-cp, so
    # without it a plain `wmf-sbx cp` here would redirect straight back to
    # this same module and recurse forever (sbx/NOTES.md "wmf-sbx
    # redirects") -- and bin/wmf-sbx only recognizes --upstream as $1.
    cmd = (
        [create_mod.WMF_SBX, "--upstream"] + (["-D"] if args.debug else [])
        + ["cp"] + cp_flags + [src, dst]
    )

    print("+ " + " ".join(cmd), file=sys.stderr)
    return run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
