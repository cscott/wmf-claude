#!/usr/bin/env python3
"""List the sandboxes that wmf-sbx owns, with their VM status and image
(and `sudo` for a `create --sudo` sandbox).

Usage:
  wmf-sbx ls
"""

import argparse
import sys

from . import cli as cli_mod
from . import lima as lima_mod
from . import state as state_mod
from . import vm as vm_mod


def main(argv=None, lima=None, env=None):
    cli_mod.parse_args(argparse.ArgumentParser(description=__doc__.splitlines()[0]), argv, "ls")
    lima = lima or lima_mod.Limactl()
    try:
        instances = lima.instances()
    except lima_mod.LimaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for name in state_mod.list_names(env):
        try:
            state = state_mod.load(name, env) or {}
        except state_mod.StateError:
            state = {}
        if not state:
            continue
        st = instances.get(vm_mod.PREFIX + name, "MISSING")
        print(f"{name:24} {st:10} {'sudo' if state.get('sudo') else '':4} "
              f"image {state.get('image') or '?'}  {state.get('primaryDir') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
