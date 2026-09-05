#!/usr/bin/env python3
"""Run a one-off command in a sandbox, restarting and restoring it first
if it's stopped.

`sbx exec [flags] NAME CMD [ARGS...]` starts a stopped sandbox as a side
effect (there is no `sbx start`), but that's all it does -- unlike
`wmf-sbx-resume` / `wmf-sbx-start`, it does not put the mount layout back
or re-point the `<name>` host remotes at the daemon's new host port
after a restart (sbx/NOTES.md §34.2/§40). Run a bare
`sbx exec NAME git log` against a sandbox that's been stopped for a
while and it lands in a five-commit clone with its `--shared`
alternates pointing nowhere -- §40's exact bug, just reached through
`exec` instead of `run`.

`wmf-sbx-exec` is `wmf-sbx-start`'s restore
(`wmf_sbx.resume.start_and_restore()`, shared with `wmf-sbx-resume` and
`wmf-sbx-start` -- see those modules) followed by the actual `sbx exec`,
so a one-off command against a possibly-stopped sandbox doesn't need a
separate `wmf-sbx-start` run before it.

Flags mirror the real `sbx exec`'s own options (reference:
~/Projects/Wikimedia/docs.docker.com/reference/cli/sbx/exec), so
`wmf-sbx exec -it NAME bash` works the same way `sbx exec -it NAME bash`
does. `--cloud`/`--cloud-api-url` are left out on purpose: this tool
only ever tracks local sandboxes, and the two are meaningless apart
from each other.

Usage:
  wmf-sbx-exec [--no-remotes] [--no-restore] [--dry-run]
               [-d] [--detach-keys KEYS] [-e KEY=VALUE]... [--env-file FILE]...
               [-i] [--privileged] [-t] [-u USER] [-w DIR] [-D]
               NAME CMD [ARGS...]
"""

import argparse
import subprocess
import sys

from . import create as create_mod
from . import resume as resume_mod
from . import state as state_mod


def main(argv=None, run=subprocess.run, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--no-remotes", action="store_true",
        help="Skip the host-remote re-point before running the command",
    )
    parser.add_argument(
        "--no-restore", action="store_true",
        help="Skip re-applying the sandbox's mount layout before running the command",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would run; start nothing, change nothing, run nothing",
    )
    parser.add_argument(
        "-d", "--detach", action="store_true",
        help="sbx exec's own flag: run the command in the background",
    )
    parser.add_argument(
        "--detach-keys", metavar="KEYS",
        help="sbx exec's own flag: override the key sequence for detaching",
    )
    parser.add_argument(
        "-e", "--env", action="append", default=[], metavar="KEY=VALUE",
        help="sbx exec's own flag: set an environment variable (repeatable)",
    )
    parser.add_argument(
        "--env-file", action="append", default=[], metavar="FILE",
        help="sbx exec's own flag: read environment variables from a file (repeatable)",
    )
    parser.add_argument(
        "-i", "--interactive", action="store_true",
        help="sbx exec's own flag: keep STDIN open even if not attached",
    )
    parser.add_argument(
        "--privileged", action="store_true",
        help="sbx exec's own flag: give extended privileges to the command",
    )
    parser.add_argument(
        "-t", "--tty", action="store_true",
        help="sbx exec's own flag: allocate a pseudo-TTY",
    )
    parser.add_argument(
        "-u", "--user", metavar="USER",
        help="sbx exec's own flag: username or UID (name|uid[:group|gid])",
    )
    parser.add_argument(
        "-w", "--workdir", metavar="DIR",
        help="sbx exec's own flag: working directory inside the sandbox",
    )
    parser.add_argument(
        "-D", "--debug", action="store_true",
        help="sbx's own global flag: enable debug logging",
    )
    parser.add_argument("name", help="Sandbox to exec into")
    parser.add_argument(
        "cmd", nargs=argparse.REMAINDER,
        help="Command (and arguments) to run inside the sandbox",
    )
    args = parser.parse_args(argv)

    if not args.cmd:
        parser.error("give a command to run")

    try:
        state_mod.validate_name(args.name)
    except state_mod.StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # These mirror the real `sbx exec`'s own flags one for one, so they're
    # rebuilt here in a fixed order rather than replayed positionally --
    # our own --no-remotes/--no-restore/--dry-run are consumed above and
    # never reach this list.
    exec_flags = []
    if args.detach:
        exec_flags.append("-d")
    if args.detach_keys:
        exec_flags += ["--detach-keys", args.detach_keys]
    for kv in args.env:
        exec_flags += ["-e", kv]
    for f in args.env_file:
        exec_flags += ["--env-file", f]
    if args.interactive:
        exec_flags.append("-i")
    if args.privileged:
        exec_flags.append("--privileged")
    if args.tty:
        exec_flags.append("-t")
    if args.user:
        exec_flags += ["-u", args.user]
    if args.workdir:
        exec_flags += ["-w", args.workdir]

    # -D/--debug is `sbx`'s own global flag, not exec's -- it belongs
    # before the subcommand, the same place `--cloud` goes in the real
    # CLI's own examples.
    #
    # --upstream comes first, ahead of -D: this module IS wmf-sbx-exec, so
    # without it a plain `wmf-sbx exec` here would redirect straight back
    # to this same module and recurse forever (sbx/NOTES.md "wmf-sbx
    # redirects") -- and bin/wmf-sbx only recognizes --upstream as $1.
    cmd = (
        [create_mod.WMF_SBX, "--upstream"] + (["-D"] if args.debug else [])
        + ["exec"] + exec_flags + [args.name] + list(args.cmd)
    )

    if args.dry_run:
        resume_mod.start_and_restore(
            args.name, no_restore=args.no_restore, no_remotes=args.no_remotes,
            dry_run=True, run=run, env=env, claude_md=False,
        )
        print("+ " + " ".join(cmd), file=sys.stderr)
        return 0

    if not resume_mod.start_and_restore(
        args.name, no_restore=args.no_restore, no_remotes=args.no_remotes,
        run=run, env=env, claude_md=False,
    ):
        return 1

    print("+ " + " ".join(cmd), file=sys.stderr)
    # Inherit stdio: the command being run may itself be interactive
    # (or just want its output to reach the terminal directly).
    return run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
