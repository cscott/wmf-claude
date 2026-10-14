#!/usr/bin/env python3
"""Run one command in a sandbox, as the agent, outside nono.

Starts the VM first if it is stopped. The command runs as `agent` (the
host uid, D10), from a fixed PATH, without a login shell, so the
agent's dotfiles do not run (vm.agent_argv). stdin, stdout and stderr
are the caller's, and the exit status is the command's. The command
gets the sandbox's proxy variables (the host's, or the egress proxy's)
(vm.guest_proxy_env): `sudo` drops the ones Lima put in the guest's
environment, and a nested sandbox's inner VM has no other way out. It
also gets $SANDBOX_NAME, as a session does (bin/git-review-check).

`--engineer` runs it as Lima's user instead, which has sudo. It refuses
a working directory that the agent can write (its home, /tmp): the
engineer's tools must not pick up a file the agent put there, such as a
git hook (HANDOFF-LIMA.md §4). In a `create --sudo` sandbox the agent
has sudo too, so that guards nothing; it says so.

Usage:
  wmf-sbx exec [--engineer] [-w DIR] NAME -- CMD [ARGS...]
  wmf-sbx exec [--engineer] [-w DIR] NAME CMD [WORD...] [-- ARGS...]

The second form is a shorthand: the words after NAME, none of which
starts with -, begin the command, and a `--` with ARGS after them stays
in it. So `wmf-sbx exec a wmf-sbx exec b -- ls` runs `wmf-sbx exec b --
ls` in a.
"""

import argparse
import sys

from . import cli as cli_mod
from . import color as color_mod
from . import lima as lima_mod
from . import session as session_mod
from . import state as state_mod
from . import vm as vm_mod

AGENT_WRITABLE = (vm_mod.AGENT_HOME, "/tmp", "/var/tmp", "/dev/shm")


def agent_writable(path):
    path = path.rstrip("/") or "/"
    return any(path == p or path.startswith(p + "/") for p in AGENT_WRITABLE)


def main(argv=None, lima=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engineer", action=argparse.BooleanOptionalAction, default=False,
                        help="run as Lima's user (has sudo) instead of the agent")
    parser.add_argument("-w", "--workdir", help="working directory in the VM")
    parser.add_argument("name", help="the sandbox (or a path shortcut such as .)")
    parser.add_argument("words", nargs="*", metavar="CMD",
                        help="the command, if it starts before the --")
    parser.add_argument("args", nargs=argparse.REMAINDER, metavar="ARGS",
                        help="the command (or the rest of it, after CMD)")
    args, _config = cli_mod.parse_args(parser, argv, "exec")
    cmd = args.words + (["--"] if args.words and args.args else []) + args.args
    if not cmd:
        parser.error("give a command after the sandbox name: NAME -- CMD [ARGS...]")
    try:
        name, state = vm_mod.resolve(args.name, env=env)
        # The proxy, and the sandbox's name, as a session has them.
        agent_env = dict(vm_mod.guest_proxy_env(name, env, forward=True))
        agent_env[session_mod.SANDBOX_NAME_VAR] = name
        if args.engineer:
            workdir = args.workdir or vm_mod.ENGINEER_HOME
            if agent_writable(workdir):
                raise vm_mod.VmError(
                    f"--engineer refuses {workdir}: the agent can write there")
            if state.get("sudo"):
                print(color_mod.warning(
                    f"note: {name} is a sudo sandbox: the agent has root in the VM too, "
                    f"so it can change anything that this command runs or reads"),
                    file=sys.stderr)
            argv_in = cmd
        elif args.workdir:
            argv_in = vm_mod.agent_argv(cmd, workdir=args.workdir, env=agent_env)
            workdir = vm_mod.ENGINEER_HOME
        else:
            # The primary workspace, where the clone is (D8). Before the
            # clone exists (phase 4), the agent's home instead.
            primary = state.get("primaryDir") or vm_mod.AGENT_HOME
            argv_in = vm_mod.agent_argv(
                ["sh", "-c", 'cd -- "$1" 2>/dev/null || true; shift; exec "$@"',
                 "sh", primary] + cmd, workdir=vm_mod.AGENT_HOME, env=agent_env)
            workdir = vm_mod.ENGINEER_HOME
        vm_mod.ensure_running(name, lima=lima,
                              log=lambda m: print(color_mod.dim(m), file=sys.stderr))
        result = vm_mod.shell(name, argv_in, lima=lima, workdir=workdir,
                              check=False, capture=False, interactive=True)
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
