#!/usr/bin/env python3
"""List the sandboxes that wmf-sbx owns, with their VM status and image.

Usage:
  wmf-sbx ls
"""

import argparse
import sys

from . import lima as lima_mod
from . import state as state_mod
from . import vm as vm_mod


def main(argv=None, lima=None, env=None):
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
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
        if state.get("backend") != "lima":
            continue
        st = instances.get(vm_mod.PREFIX + name, "MISSING")
        print(f"{name:24} {st:10} image {state.get('image') or '?'}  "
              f"{state.get('primaryDir') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
