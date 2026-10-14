#!/usr/bin/env python3
"""Show the log of a sandbox's egress proxy: what it allowed and denied.

`tail` on the sandbox's egress/NAME/proxy.log, with any of tail's
options. NAME comes first or last; every other argument goes to tail as
it is.

Usage:
  wmf-sbx logs NAME [TAIL-OPTIONS...]
  wmf-sbx logs [TAIL-OPTIONS...] NAME
e.g. `wmf-sbx logs egtest -f`, `wmf-sbx logs -50 egtest`.
"""

import os
import sys

from . import color as color_mod
from . import egress as egress_mod
from . import state as state_mod
from . import vm as vm_mod


def split_args(argv):
    """(NAME, tail's arguments): NAME is the first argument, or else the
    last, whichever is not an option."""
    if argv and not argv[0].startswith("-"):
        return argv[0], argv[1:]
    if argv and not argv[-1].startswith("-"):
        return argv[-1], argv[:-1]
    return None, argv


def main(argv=None, env=None, execvp=os.execvp):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv in (["-h"], ["--help"]):
        print(__doc__.split("\n\n", 1)[1].rstrip())
        return 0
    arg, tail_args = split_args(argv)
    if arg is None:
        print("usage: wmf-sbx logs NAME [TAIL-OPTIONS...]", file=sys.stderr)
        return 2
    try:
        name, state = vm_mod.resolve(arg, env=env)
        if not egress_mod.enabled(state):
            raise vm_mod.VmError(f"{name} is not behind the egress proxy "
                                 f"(vz, or create --no-egress-proxy): it has no proxy log")
        path = egress_mod.log_file(name, env)
        if not os.path.isfile(path):
            raise vm_mod.VmError(f"{name} has no proxy log yet ({path}): "
                                 f"its proxy starts with the VM")
    except (state_mod.StateError, vm_mod.VmError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    execvp("tail", ["tail"] + tail_args + ["--", path])
    return 0


if __name__ == "__main__":
    sys.exit(main())
