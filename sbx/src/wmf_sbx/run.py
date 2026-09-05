#!/usr/bin/env python3
"""Handle the one shape of `sbx run` that overlaps with wmf-sbx-resume.

`sbx run --name NAME [-- AGENT_ARGS...]` re-attaches to an existing
sandbox -- the same thing `wmf-sbx-resume NAME` does, only without the
host-side repair resume does first (see resume.py's docstring). Since
sbx/NOTES.md "wmf-sbx redirects", `wmf-sbx run --name NAME` reaches this
module rather than the real `sbx run`, so engineers whose muscle memory
still reaches for the upstream re-attach spelling land on the wrapper
that actually does the repair, not the raw one that skips it.

Every other shape of `sbx run` -- `sbx run AGENT [PATH...]`, which
creates a *new* sandbox -- is refused rather than forwarded anywhere.
`wmf-sbx-create` always clones and does its own dependency walk and MCP
registration first; none of that runs on a bare `sbx run`, so silently
falling through to it would leave the engineer in a sandbox missing all
of that, discovered only once they hit a missing dependency or tool
mid-session. Erring here and pointing at `wmf-sbx create` is cheap; a
half-set-up sandbox is not.

Usage:
  wmf-sbx-run --name NAME [-- AGENT_ARGS...]
"""

import subprocess
import sys

from . import resume as resume_mod


def extract_name(ours):
    """(name, remaining) -- pull `--name NAME` / `--name=NAME` out of
    `ours` (the non-agent-args portion of argv, see
    resume_mod.split_agent_args). name is None if `--name` wasn't given;
    the last occurrence wins, matching argparse's own `store` behavior."""
    remaining = []
    name = None
    i = 0
    while i < len(ours):
        arg = ours[i]
        if arg == "--name":
            if i + 1 >= len(ours):
                remaining.append(arg)
                i += 1
                continue
            name = ours[i + 1]
            i += 2
            continue
        if arg.startswith("--name="):
            name = arg[len("--name="):]
            i += 1
            continue
        remaining.append(arg)
        i += 1
    return name, remaining


def _refuse_and_point_at_create():
    print(
        "error: wmf-sbx run only understands the --name form of `sbx "
        "run`, to re-attach to an existing sandbox -- it forwards to "
        "wmf-sbx-resume, which does the mount-restore and remote-"
        "repoint steps a bare `sbx run --name` skips. To create a new "
        "sandbox, use `wmf-sbx create` instead.",
        file=sys.stderr,
    )
    return 1


def main(argv=None, run=subprocess.run, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ours, agent_args = resume_mod.split_agent_args(argv)
    name, rest = extract_name(ours)

    if name is None:
        return _refuse_and_point_at_create()

    # `--name NAME` alongside a positional (`sbx run --name X claude
    # /path`) is still the create shape -- upstream's --name there just
    # sets the new sandbox's name. Don't forward that to resume, which
    # would treat `claude`/`/path` as unrecognized arguments and print a
    # confusing wmf-sbx-resume usage block instead of pointing at create.
    if any(not arg.startswith("-") for arg in rest):
        return _refuse_and_point_at_create()

    resume_argv = [name] + rest
    if agent_args is not None:
        resume_argv += ["--"] + agent_args
    return resume_mod.main(resume_argv, run=run, env=env)


if __name__ == "__main__":
    sys.exit(main())
