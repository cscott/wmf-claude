#!/usr/bin/env python3
"""git-remote-wmfsbx: the host fetches from a sandbox's clones
(lima-port/HANDOFF-LIMA.md §6.2).

git runs `git-remote-wmfsbx REMOTE URL` for a URL of the form
wmfsbx://NAME/PATH, and talks the remote-helper protocol on its stdin
and stdout (gitremote-helpers(7)):

- To `capabilities`, the helper answers `connect` only.
- To `connect git-upload-pack`, it answers an empty line, and then
  becomes `git upload-pack PATH` in the VM: it execs `limactl shell`, so
  git's pipe goes to the VM unchanged (tty-less `limactl shell` is
  binary-safe, RAN, phase 0).
- It refuses `connect git-receive-pack`, so the remote is fetch-only.
  Nothing on the host ever writes into the VM through it.

upload-pack runs as the agent, with hooks off and no fsmonitor, so a
fetch runs no hook from the agent's repository. git does not read
`uploadpack.packObjectsHook` from repository config, only from
protected config (RAN in the guest, phase 4).

The helper does not pass GIT_PROTOCOL to the VM, so upload-pack speaks
protocol v0, which every git client accepts over `connect`.

NAME must have a wmf-sbx state file, so the helper reaches only the VMs
that wmf-sbx owns.
"""

import os
import re
import sys

from . import lima as lima_mod
from . import state as state_mod
from . import vm as vm_mod

URL_RE = re.compile(r"^wmfsbx://([^/]+)(/.*)$")


class HelperError(Exception):
    """A bad URL or a bad request from git."""


def parse_url(url):
    """(name, path) of a wmfsbx:// URL, or HelperError."""
    m = URL_RE.match(url or "")
    if not m:
        raise HelperError(f"{url!r} is not a wmfsbx://NAME/PATH URL")
    name, path = m.group(1), m.group(2)
    if not state_mod.NAME_RE.match(name):
        raise HelperError(f"{name!r} is not a sandbox name")
    # normpath keeps a leading `//` (POSIX allows it a meaning).
    if (os.path.normpath(path) != path or path.startswith("//")
            or any(c in path for c in "\n\0")):
        raise HelperError(f"{path!r} is not a normal absolute path")
    return name, path


def upload_pack_argv(name, path, exe="limactl"):
    git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
           "upload-pack", "--", path]
    return [exe, "shell", "--workdir=/", vm_mod.instance_name(name), "--"] + \
        vm_mod.agent_argv(git)


def serve(url, stdin, stdout, execvp=os.execvp, env=None):
    """Answer git on `stdin`/`stdout` (binary streams). Returns an exit
    status, or does not return (exec)."""
    name, path = parse_url(url)
    while True:
        line = stdin.readline()
        if not line:
            return 0
        cmd = line.rstrip(b"\n").decode("utf-8", "replace")
        if cmd == "capabilities":
            stdout.write(b"connect\n\n")
            stdout.flush()
        elif cmd == "connect git-upload-pack":
            state_mod.require(name, env=env)
            stdout.write(b"\n")
            stdout.flush()
            argv = upload_pack_argv(name, path)
            execvp(argv[0], argv)
            return 0  # only a fake execvp returns
        elif cmd.startswith("connect "):
            raise HelperError(f"wmfsbx remotes are fetch-only; {cmd[8:]} is refused")
        elif cmd == "":
            return 0
        else:
            raise HelperError(f"unsupported request {cmd!r}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: git-remote-wmfsbx REMOTE URL (git runs this)", file=sys.stderr)
        return 2
    try:
        return serve(argv[1], sys.stdin.buffer, sys.stdout.buffer)
    except (HelperError, state_mod.StateError, lima_mod.LimaError) as e:
        print(f"fatal: wmfsbx: {e}", file=sys.stderr)
        return 128


if __name__ == "__main__":
    sys.exit(main())
