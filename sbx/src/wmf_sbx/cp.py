#!/usr/bin/env python3
"""Copy a file or directory between the host and a sandbox.

Exactly one side is written NAME:PATH. A relative PATH is anchored at
the sandbox's primary workspace, which is at the host's absolute path in
the VM too (D8). In the VM the copy is done by the agent, in its own
tree, through a staging directory that the engineer makes in /tmp:

  host -> VM   `limactl copy` into the staging directory (the engineer's,
               mode 0755), then the agent copies it to PATH;
  VM -> host   the agent copies PATH into a staging directory of its own
               (mode 0755), then `limactl copy` brings it out.

Root never writes a path the agent controls: the agent could have made
it a symlink (lima/guest-install.sh follows the same rule).

With both sides NAME:PATH, the copy goes out of the first sandbox into a
temporary directory on the host, and from there into the second; the
temporary directory is removed afterwards, also on failure.

More than one SRC, as with cp: DST must then be a directory that exists.
The sources are all on one side: the host, or one sandbox. A wildcard
(`*`, `?`, `[...]`) in a NAME:PATH source is expanded in the sandbox, as
the agent, so quote it on the host (`'NAME:logs/*.txt'`); a host wildcard
is the host shell's (and is expanded here if it reaches us quoted). A
pattern that matches nothing is an error.

Usage:
  wmf-sbx cp [-r] SRC... DST   # NAME:PATH on one side or both; a
                               # directory is copied whole, -r or not
"""

import argparse
import glob
import os
import shutil
import sys
import tempfile

from . import cli as cli_mod
from . import color as color_mod
from . import lima as lima_mod
from . import state as state_mod
from . import vm as vm_mod


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


def _guest_mktemp(name, lima, as_agent):
    """A new directory in /tmp, mode 0755, made by the engineer or by the
    agent. Refuse any output that is not such a path."""
    argv = ["bash", "-c", 'd=$(mktemp -d /tmp/wmf-sbx-cp.XXXXXX) && chmod 0755 "$d" && echo "$d"']
    if as_agent:
        argv = vm_mod.agent_argv(argv, workdir="/tmp")
    out = vm_mod.shell(name, argv, lima=lima).stdout.strip()
    if not out.startswith("/tmp/wmf-sbx-cp.") or any(c.isspace() for c in out):
        raise vm_mod.VmError(f"mktemp in the guest failed: {out!r}")
    return out


def copy_in(name, src, dest, lima):
    if not os.path.exists(src):
        raise vm_mod.VmError(f"no such file or directory: {src}")
    stage = _guest_mktemp(name, lima, as_agent=False)
    base = os.path.basename(os.path.normpath(src))
    try:
        lima.copy(src, f"{vm_mod.instance_name(name)}:{stage}/", recursive=os.path.isdir(src))
        vm_mod.shell(name, vm_mod.agent_argv(
            ["cp", "-a", "--no-preserve=ownership", f"{stage}/{base}", dest], workdir="/tmp"),
            lima=lima)
    finally:
        vm_mod.shell(name, ["rm", "-rf", stage], lima=lima, check=False)


def copy_out(name, src, dest, lima):
    stage = _guest_mktemp(name, lima, as_agent=True)
    base = os.path.basename(os.path.normpath(src))
    try:
        vm_mod.shell(name, vm_mod.agent_argv(
            ["bash", "-c", 'cp -a -- "$1" "$2/" && chmod -R a+rX "$2"', "_", src, stage],
            workdir="/tmp"), lima=lima)
        is_dir = vm_mod.shell(name, ["test", "-d", f"{stage}/{base}"], lima=lima,
                              check=False).returncode == 0
        lima.copy(f"{vm_mod.instance_name(name)}:{stage}/{base}", dest, recursive=is_dir)
    finally:
        vm_mod.shell(name, vm_mod.agent_argv(["rm", "-rf", stage], workdir="/tmp"),
                     lima=lima, check=False)


def _guest_path(resolved_arg):
    """The PATH half of a resolved NAME:PATH, absolute: with no primary
    workspace recorded, a relative PATH is anchored at the agent's home."""
    guest = resolved_arg.partition(":")[2]
    if not guest.startswith("/"):
        guest = os.path.join(vm_mod.AGENT_HOME, guest)
    return guest


GLOB_CHARS = "*?["


def is_pattern(path):
    return any(c in path for c in GLOB_CHARS)


# Expands $1 as the agent: IFS empty, so the unquoted $1 is globbed but not
# split; nullglob, so no match gives nothing. NUL-separated.
GUEST_GLOB = 'shopt -s nullglob; IFS=; set -- $1; [ $# -gt 0 ] && printf "%s\\0" "$@"'


def expand_guest(name, pattern, lima):
    """The agent's paths that `pattern` (absolute) matches in the sandbox,
    sorted; VmError when none does."""
    res = vm_mod.shell(name, vm_mod.agent_argv(["bash", "-c", GUEST_GLOB, "_", pattern],
                                               workdir="/tmp"), lima=lima, check=False)
    found = sorted(p for p in (res.stdout or "").split("\0") if p)
    if not found:
        raise vm_mod.VmError(f"no match in {name}: {pattern}")
    return found


def expand_host(path):
    if not is_pattern(path) or os.path.exists(path):
        return [path]
    found = sorted(glob.glob(path))
    if not found:
        raise vm_mod.VmError(f"no match: {path}")
    return found


def guest_is_dir(name, path, lima):
    return vm_mod.shell(name, vm_mod.agent_argv(["test", "-d", path], workdir="/tmp"),
                        lima=lima, check=False).returncode == 0


def main(argv=None, lima=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # A directory is copied whole either way; -r is accepted for the
    # hands that type `cp -r` for a directory.
    parser.add_argument("-r", "-R", "--recursive", action=argparse.BooleanOptionalAction,
                        default=False,
                        help="accepted and ignored: directories are always copied whole")
    parser.add_argument("paths", nargs="+", metavar="SRC... DST",
                        help="the sources, then the destination")
    args, _config = cli_mod.parse_args(parser, argv, "cp")
    if len(args.paths) < 2:
        parser.error("give at least one SRC and a DST")
    lima = lima or lima_mod.Limactl()
    try:
        srcs = [resolve_cp_arg(a, env=env) for a in args.paths[:-1]]
        dst = resolve_cp_arg(args.paths[-1], env=env)
        src_names = {_sandbox_name_in(s) for s in srcs}
        if len(src_names) > 1:
            raise vm_mod.VmError("the sources must all be on the host, or all in one "
                                 "sandbox: run one cp for each")
        src_name, dst_name = src_names.pop(), _sandbox_name_in(dst)
        if src_name is None and dst_name is None:
            raise vm_mod.VmError("write SRC or DST (or both) as NAME:PATH")
        for name in dict.fromkeys(n for n in (src_name, dst_name) if n):
            vm_mod.resolve(name, env=env)
            vm_mod.ensure_running(name, lima=lima,
                                  log=lambda m: print(color_mod.dim(m), file=sys.stderr))
        # The sources, with their wildcards expanded on their own side.
        if src_name:
            sources = []
            for s in srcs:
                path = _guest_path(s)
                sources += expand_guest(src_name, path, lima) if is_pattern(path) else [path]
        else:
            sources = [p for s in srcs for p in expand_host(os.path.expanduser(s))]
        dst_path = _guest_path(dst) if dst_name else os.path.expanduser(dst)
        if len(sources) > 1 and not (guest_is_dir(dst_name, dst_path, lima) if dst_name
                                     else os.path.isdir(dst_path)):
            raise vm_mod.VmError(f"{len(sources)} sources, so {args.paths[-1]} must be a "
                                 f"directory that exists")
        for source in sources:
            if src_name and dst_name:
                tmp = tempfile.mkdtemp(prefix="wmf-sbx-cp.")
                print(color_mod.dim(f"+ {src_name}:{source} -> {tmp} -> "
                                    f"{dst_name}:{dst_path}"), file=sys.stderr)
                try:
                    staged = os.path.join(tmp, os.path.basename(os.path.normpath(source)))
                    copy_out(src_name, source, staged, lima)
                    copy_in(dst_name, staged, dst_path, lima)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            elif dst_name:
                copy_in(dst_name, source, dst_path, lima)
            else:
                copy_out(src_name, source, dst_path, lima)
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
