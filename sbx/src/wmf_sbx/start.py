#!/usr/bin/env python3
"""Bring a stopped sandbox back to 'running', without attaching an agent.

The bug this exists to fix: exiting the claude session inside a sandbox
does not stop the sandbox -- it stays 'running', and `git fetch
<name>` on the host keeps working, for a while. But sbx itself
eventually stops an idle sandbox on its own, moving it to 'stopped', and
a stopped sandbox publishes no port at all -- the `<name>`
remotes' git daemon becomes unreachable, and there is no longer a running
Claude session to ask "did you actually finish?" before it happens.

`wmf-sbx-resume` already does the fix -- start the container back up,
put its mount layout back (sbx/NOTES.md §40, without which the clones'
borrowed objects are unreachable), and re-point the `<name>`
remotes at wherever the daemon's host port landed this time (§34.2) --
but it goes on to attach an agent, via `wmf-sbx run --name`. That is
wrong for exactly this recovery case: the engineer isn't trying to talk
to Claude, just to get `git fetch <name>` working again long
enough to pull out whatever it left behind. `wmf-sbx-start` is that
same restore, factored out of `wmf_sbx/resume.py`'s
`start_and_restore()`, stopping right where the attach would begin.

Safe to run against a sandbox that's already running -- every step it
takes is idempotent (`start_sandbox` no-ops if it's already up, the
mount restore checks each mount before making it, `refresh` is a
best-effort re-point either way).

Usage:
  wmf-sbx-start [--no-remotes] [--no-restore] [--dry-run] NAME
"""

import argparse
import subprocess
import sys

from . import resume as resume_mod
from . import state as state_mod


def main(argv=None, run=subprocess.run, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="Sandbox to start")
    parser.add_argument(
        "--no-remotes", action="store_true",
        help="Skip the host-remote re-point; just start the container",
    )
    parser.add_argument(
        "--no-restore", action="store_true",
        help="Skip re-applying the sandbox's mount layout after the start",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would run; start nothing, change nothing",
    )
    args = parser.parse_args(argv)

    try:
        state_mod.validate_name(args.name)
    except state_mod.StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    ok = resume_mod.start_and_restore(
        args.name, no_restore=args.no_restore, no_remotes=args.no_remotes,
        dry_run=args.dry_run, run=run, env=env,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
