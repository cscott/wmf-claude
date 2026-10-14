#!/usr/bin/env python3
"""Let a sandbox behind the egress proxy reach more hosts
(lima-port/PLAN-SUDO.md A2.3).

The egress proxy allows the hosts of the wmf-engineer profile, the
package registries and Debian's mirror. `allow` adds hosts for one
sandbox; it does not change the profile's endpoint rules (a wiki stays
read-only). The list is the sandbox's egress/NAME/allow file.

A running VM takes the change at once: `allow` writes the proxy's config
again and signals the sandbox's QEMU wrapper (SIGUSR1), which starts a
new proxy and points new guest connections at it. Connections already
open stay on the old proxy. A session's own nono lets every host through
to the egress proxy (session.launcher_argv), so a session that runs now
gets the host too.

Usage:
  wmf-sbx allow NAME HOST...        add hosts (as nono's --allow-domain:
                                    `example.org`, `*.example.org`)
  wmf-sbx allow NAME --rm HOST...   remove hosts
  wmf-sbx allow NAME --list         show the hosts `allow` added
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time

from . import cli as cli_mod
from . import color as color_mod
from . import egress as egress_mod
from . import lima as lima_mod
from . import state as state_mod
from . import vm as vm_mod

# A host, or a `*.` wildcard under one; no scheme, path or port.
HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
# How long `allow` waits for the wrapper's new proxy (its own wait, plus
# the time to start).
SWAP_TIMEOUT = egress_mod.LISTEN_TIMEOUT + 5


class AllowError(Exception):
    """A user-facing problem with `allow`."""


def check_host(host):
    """The host, lower-cased; AllowError for anything else, `*` too: it
    would open every host to the sandbox."""
    h = host.strip().lower().rstrip(".")
    if not HOST_RE.match(h):
        raise AllowError(f"{host!r} is not a host name (example.org, or *.example.org)")
    return h


def wrapper_pid(name, env=None, ps=subprocess.run):
    """The pid of this sandbox's running QEMU wrapper, or None. Checked
    with `ps`: a stale pid file may name another process, which SIGUSR1
    would kill."""
    config = egress_mod.read_config(egress_mod.config_file(name, env))
    try:
        with open(config["pidFile"], encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    res = ps(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
             stdin=subprocess.DEVNULL)
    command = (res.stdout or "").strip()
    inst_dir = os.path.join(lima_mod.lima_home(env), vm_mod.instance_name(name)) + "/"
    if res.returncode != 0 or "wmf-sbx-qemu" not in command or inst_dir not in command:
        return None
    return pid


def swap(name, env=None, kill=os.kill, ps=subprocess.run, clock=time.monotonic,
         sleep=time.sleep, timeout=SWAP_TIMEOUT):
    """Write the config again and have a running wrapper start a new proxy.
    Returns the new port, or None if no wrapper runs (the next start
    reads the config). AllowError if the new proxy does not come up."""
    egress_mod.write_config(name, env)
    pid = wrapper_pid(name, env, ps=ps)
    if pid is None:
        return None
    before = egress_mod.read_port(name, env)
    kill(pid, signal.SIGUSR1)
    end = clock() + timeout
    while clock() < end:
        port = egress_mod.read_port(name, env)
        if port and port != before:
            return port
        sleep(0.2)
    raise AllowError(f"the QEMU wrapper (pid {pid}) started no new proxy in {timeout}s; "
                     f"the old one still runs. `wmf-sbx stop {name}` and start it again "
                     f"to apply the list")


def main(argv=None, env=None, kill=os.kill, ps=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="the sandbox (or a path shortcut such as .)")
    parser.add_argument("hosts", nargs="*", metavar="HOST")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--rm", action=argparse.BooleanOptionalAction, default=False,
                       help="remove the HOSTs")
    group.add_argument("--list", action=argparse.BooleanOptionalAction, default=False,
                       help="show the list")
    args, _config = cli_mod.parse_args(parser, argv, "allow")
    env = os.environ if env is None else env
    if args.list == bool(args.hosts):
        parser.error("give HOSTs, or --list")
    try:
        name, state = vm_mod.resolve(args.name, env=env)
        if not egress_mod.enabled(state):
            raise AllowError(f"{name} is not behind the egress proxy (vz, or create --no-egress-proxy); "
                             f"its guest reaches the network itself")
        have = egress_mod.extra_domains(name, env)
        if args.list:
            for host in have:
                print(host)
            return 0
        hosts = [check_host(h) for h in args.hosts]
        if args.rm:
            missing = [h for h in hosts if h not in have]
            if missing:
                raise AllowError(f"not in {name}'s list: {', '.join(missing)}")
            new = [h for h in have if h not in hosts]
        else:
            new = have + [h for h in dict.fromkeys(hosts) if h not in have]
        if new == have:
            print(f"{name}: no change", file=sys.stderr)
            return 0
        egress_mod.set_extra_domains(name, new, env)
        port = swap(name, env, kill=kill, ps=ps)
    except (state_mod.StateError, vm_mod.VmError, AllowError, egress_mod.EgressError,
            OSError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    done = "removed" if args.rm else "allowed"
    if port is None:
        print(f"{name}: {done} {', '.join(hosts)}; the VM is not running, so its next "
              f"start applies it", file=sys.stderr)
    else:
        print(f"{name}: {done} {', '.join(hosts)}; new connections go to the new proxy "
              f"(port {port})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
