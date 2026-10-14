#!/usr/bin/env python3
"""Start a sandbox's VM, without starting an agent, and check it.

`limactl start` on the sandbox's instance, then the security invariants
(vm.INVARIANTS: the agent has no sudo and the host uid, the host block is
loaded, the mounts are read-only, TIOCSTI is off; behind the egress proxy,
vm.EGRESS_INVARIANTS too; a `--sudo` sandbox has vm.SUDO_INVARIANTS
instead of "the agent cannot sudo"). A failed invariant
is an error: the sandbox is running, but not as wmf-sbx made it.

Usage:
  wmf-sbx start NAME
"""

import argparse
import sys

from . import color as color_mod
from . import egress as egress_mod
from . import lima as lima_mod
from . import state as state_mod
from . import vm as vm_mod


def check(name, lima=None, out=None, env=None):
    """Print the invariants (to stderr unless `out`); return True if all
    hold. A sandbox behind the egress proxy has more
    (vm.EGRESS_INVARIANTS)."""
    out = out or sys.stderr
    ok = True
    state = state_mod.load(name, env) or {}
    egress = egress_mod.enabled(state)
    port = egress_mod.read_port(name, env) if egress else None
    for desc, good in vm_mod.check_invariants(name, lima=lima, egress=egress,
                                              loopback_port=port or 22,
                                              sudo=bool(state.get("sudo"))):
        mark = color_mod.highlight("ok") if good else color_mod.error("FAIL")
        print(f"  {mark}  {desc}", file=out)
        ok = ok and good
    return ok


def main(argv=None, lima=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="the sandbox (or a path shortcut such as .)")
    args = parser.parse_args(argv)
    try:
        name, _state = vm_mod.resolve(args.name, env=env)
        vm_mod.ensure_running(name, lima=lima,
                              log=lambda m: print(color_mod.dim(m), file=sys.stderr))
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    print(f"{name}: running; checking it", file=sys.stderr)
    if not check(name, lima=lima, env=env):
        print(color_mod.error(f"error: {name} breaks a security invariant; do not "
                              f"use it. `wmf-sbx rm {name}` and create it again."),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
