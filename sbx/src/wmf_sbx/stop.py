#!/usr/bin/env python3
"""Stop a sandbox's VM. Nothing in it is lost; `wmf-sbx start` boots it
again.

Usage:
  wmf-sbx stop NAME
"""

import argparse
import sys

from . import color as color_mod
from . import lima as lima_mod
from . import state as state_mod
from . import vm as vm_mod


def main(argv=None, lima=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="the sandbox (or a path shortcut such as .)")
    args = parser.parse_args(argv)
    try:
        name, _state = vm_mod.resolve(args.name, env=env)
        vm_mod.stop(name, lima=lima)
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    print(f"{name}: stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
