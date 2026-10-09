#!/usr/bin/env python3
"""Run one command in a sandbox, as the agent, outside nono.

Starts the VM first if it is stopped. The command runs as `agent` (the
host uid, D10), from a fixed PATH, without a login shell, so the
agent's dotfiles do not run (vm.agent_argv). stdin, stdout and stderr
are the caller's, and the exit status is the command's.

`--engineer` runs it as Lima's user instead, which has sudo. It refuses
a working directory that the agent can write (its home, /tmp): the
engineer's tools must not pick up a file the agent put there, such as a
git hook (HANDOFF-LIMA.md §4).

Usage:
  wmf-sbx exec [--engineer] [-w DIR] NAME -- CMD [ARGS...]
"""

import argparse
import sys

from . import color as color_mod
from . import lima as lima_mod
from . import state as state_mod
from . import vm as vm_mod

AGENT_WRITABLE = (vm_mod.AGENT_HOME, "/tmp", "/var/tmp", "/dev/shm")


def agent_writable(path):
    path = path.rstrip("/") or "/"
    return any(path == p or path.startswith(p + "/") for p in AGENT_WRITABLE)


def main(argv=None, lima=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engineer", action="store_true",
                        help="run as Lima's user (has sudo) instead of the agent")
    parser.add_argument("-w", "--workdir", help="working directory in the VM")
    parser.add_argument("name", help="the sandbox (or a path shortcut such as .)")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="-- CMD [ARGS...]")
    args = parser.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("give a command after the sandbox name: NAME -- CMD [ARGS...]")
    try:
        name, state = vm_mod.resolve(args.name, env=env)
        if args.engineer:
            workdir = args.workdir or vm_mod.ENGINEER_HOME
            if agent_writable(workdir):
                raise vm_mod.VmError(
                    f"--engineer refuses {workdir}: the agent can write there")
            argv_in = cmd
        elif args.workdir:
            argv_in = vm_mod.agent_argv(cmd, workdir=args.workdir)
            workdir = vm_mod.ENGINEER_HOME
        else:
            # The primary workspace, where the clone is (D8). Before the
            # clone exists (phase 4), the agent's home instead.
            primary = state.get("primaryDir") or vm_mod.AGENT_HOME
            argv_in = vm_mod.agent_argv(
                ["sh", "-c", 'cd -- "$1" 2>/dev/null || true; shift; exec "$@"',
                 "sh", primary] + cmd, workdir=vm_mod.AGENT_HOME)
            workdir = vm_mod.ENGINEER_HOME
        vm_mod.ensure_running(name, lima=lima,
                              log=lambda m: print(color_mod.dim(m), file=sys.stderr))
        result = vm_mod.shell(name, argv_in, lima=lima, workdir=workdir,
                              check=False, capture=False)
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
