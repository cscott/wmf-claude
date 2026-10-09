#!/usr/bin/env python3
"""Dispatch `python3 -m wmf_sbx <command> [args...]` to the matching
submodule's main() -- the same commands sbx/bin/wmf-sbx-<command> exposes
on PATH, for use anywhere sbx/bin isn't (e.g. `PYTHONPATH=sbx/src python3
-m wmf_sbx resolve ...`). The bin/ wrappers remain the primary, PATH-
visible entry points; this is a second way in, not a replacement.
"""

import importlib
import sys

COMMANDS = {
    "cp": "wmf_sbx.cp",
    "create": "wmf_sbx.create",
    "exec": "wmf_sbx.exec",
    "image": "wmf_sbx.image",
    "ls-remotes": "wmf_sbx.ls_remotes",
    "refresh-claude-md": "wmf_sbx.refresh_claude_md",
    "resolve": "wmf_sbx.resolve",
    "resume": "wmf_sbx.resume",
    "rm": "wmf_sbx.rm",
    "run": "wmf_sbx.run",
    "start": "wmf_sbx.start",
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python3 -m wmf_sbx <command> [args...]", file=sys.stderr)
        print("commands: " + ", ".join(sorted(COMMANDS)), file=sys.stderr)
        return 0 if argv else 1

    command, rest = argv[0], argv[1:]
    modname = COMMANDS.get(command)
    if modname is None:
        print(f"wmf_sbx: unknown command '{command}'", file=sys.stderr)
        print("commands: " + ", ".join(sorted(COMMANDS)), file=sys.stderr)
        return 1

    module = importlib.import_module(modname)
    return module.main(rest)


if __name__ == "__main__":
    sys.exit(main())
