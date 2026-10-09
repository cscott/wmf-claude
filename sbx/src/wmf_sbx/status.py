#!/usr/bin/env python3
"""Show one sandbox: its VM, its image, and (when it runs) its security
invariants.

The golden image is checked too: it must still exist, be read-only and
match its recorded checksum (HANDOFF-LIMA.md D12: a changed golden image
breaks every sandbox made from it). Exit 0 only if everything holds.

Usage:
  wmf-sbx status NAME
"""

import argparse
import sys

from . import color as color_mod
from . import image as image_mod
from . import lima as lima_mod
from . import start as start_mod
from . import state as state_mod
from . import vm as vm_mod


def main(argv=None, lima=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="the sandbox (or a path shortcut such as .)")
    args = parser.parse_args(argv)
    try:
        name, state = vm_mod.resolve(args.name, env=env)
        vm_status = vm_mod.status(name, lima=lima)
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    ok = True
    print(f"sandbox   {name}")
    print(f"vm        {vm_mod.instance_name(name)}: {vm_status or 'MISSING'} "
          f"({state.get('vmType') or '?'})")
    print(f"primary   {state.get('primaryDir') or '-'}")
    key = state.get("image")
    try:
        image_mod.verify_entry(key, env)
        print(f"image     {key}: ok (read-only, checksum matches)")
    except (image_mod.ImageError, OSError, TypeError) as e:
        print(f"image     {key}: {color_mod.error('PROBLEM')} {e}")
        ok = False
    if vm_status is None:
        ok = False
    elif vm_status == "Running":
        print("checks")
        ok = start_mod.check(name, lima=lima, out=sys.stdout) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
