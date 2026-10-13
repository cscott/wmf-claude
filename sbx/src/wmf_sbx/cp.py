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

Usage:
  wmf-sbx cp [-r] SRC DST   # one or both of them NAME:PATH; a directory
                            # is copied whole, -r or not
"""

import argparse
import os
import shutil
import sys
import tempfile

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


def main(argv=None, lima=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # A directory is copied whole either way; -r is accepted for the
    # hands that type `cp -r` for a directory.
    parser.add_argument("-r", "-R", "--recursive", action="store_true",
                        help="accepted and ignored: directories are always copied whole")
    parser.add_argument("src")
    parser.add_argument("dst")
    args = parser.parse_args(argv)
    lima = lima or lima_mod.Limactl()
    try:
        src = resolve_cp_arg(args.src, env=env)
        dst = resolve_cp_arg(args.dst, env=env)
        names = [_sandbox_name_in(a) for a in (src, dst)]
        if names == [None, None]:
            raise vm_mod.VmError("write SRC or DST (or both) as NAME:PATH")
        for name in dict.fromkeys(n for n in names if n):
            vm_mod.resolve(name, env=env)
            vm_mod.ensure_running(name, lima=lima,
                                  log=lambda m: print(color_mod.dim(m), file=sys.stderr))
        if names[0] and names[1]:
            src_guest, dst_guest = _guest_path(src), _guest_path(dst)
            tmp = tempfile.mkdtemp(prefix="wmf-sbx-cp.")
            print(color_mod.dim(f"+ {names[0]}:{src_guest} -> {tmp} -> {names[1]}:{dst_guest}"),
                  file=sys.stderr)
            try:
                staged = os.path.join(tmp, os.path.basename(os.path.normpath(src_guest)))
                copy_out(names[0], src_guest, staged, lima)
                copy_in(names[1], staged, dst_guest, lima)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        elif names[1]:
            copy_in(names[1], os.path.expanduser(src), _guest_path(dst), lima)
        else:
            copy_out(names[0], _guest_path(src), os.path.expanduser(dst), lima)
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
