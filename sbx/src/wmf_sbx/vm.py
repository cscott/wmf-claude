#!/usr/bin/env python3
"""The Lima VM of one wmf-sbx sandbox: create, start, stop, delete, run
a command as the agent, and check the security invariants.

The verbs (create, start, stop, exec, cp, rm, status) call this module;
it calls `limactl` through lima.Limactl, so the tests fake it.

- The Lima instance of sandbox NAME is `wmf-sbx-NAME`. The prefix keeps
  wmf-sbx away from other Lima instances (Kosta's `wmf-claude` VM, the
  engineer's own), and every verb also requires NAME's state file
  (HANDOFF-LIMA.md §4).
- The agent runs as `agent`, with the host uid (D10), from a fixed PATH
  and without a login shell, so its dotfiles never run outside nono
  (lima/guest-claude.sh does the same).
"""

import os
import shlex
import tempfile

from . import image as image_mod
from . import lima as lima_mod
from . import template as template_mod

PREFIX = "wmf-sbx-"
AGENT = "agent"
AGENT_HOME = "/home/agent"
ENGINEER_HOME = f"/home/{template_mod.ENGINEER}"
AGENT_PATH = f"/usr/local/bin:/usr/bin:/bin:{AGENT_HOME}/.local/bin"
START_TIMEOUT = "30m"


class VmError(Exception):
    """A user-facing failure in a VM operation."""


def instance_name(name):
    if name.startswith("builder-"):
        raise VmError("sandbox names may not start with 'builder-' (image builders use it)")
    return PREFIX + name


def agent_argv(argv, workdir=AGENT_HOME, env=None):
    """argv to run `argv` as the agent, from the guest's engineer shell."""
    out = ["sudo", "-H", "-u", AGENT, "env", "-C", workdir, f"PATH={AGENT_PATH}"]
    out += [f"{k}={v}" for k, v in sorted((env or {}).items())]
    return out + list(argv)


def write_template(tmpl, directory):
    path = os.path.join(directory, "lima.yaml")
    with open(path, "w", encoding="utf-8") as f:
        image_mod.yaml.safe_dump(tmpl, f, sort_keys=False)
    return path


def create(name, tmpl, lima=None, log=print):
    """Create the instance from `tmpl` (a template.py dict) and boot it.
    `limactl create` copies the golden image (D2)."""
    lima = lima or lima_mod.Limactl()
    inst = instance_name(name)
    if lima.exists(inst):
        raise VmError(f"a Lima instance {inst} exists already; remove it first "
                      f"(`limactl delete {inst}`), or choose another --name")
    with tempfile.TemporaryDirectory(prefix="wmf-sbx-tmpl.") as d:
        path = write_template(tmpl, d)
        lima.validate(path)
        log(f"+ limactl create --name={inst} (copies the golden image)")
        lima.create(inst, path)
    log(f"+ limactl start {inst} (the first boot runs the provisioning)")
    lima.start(inst, timeout=START_TIMEOUT)
    return inst


def status(name, lima=None):
    """Lima's status of the instance ('Running', 'Stopped', ...), or None."""
    lima = lima or lima_mod.Limactl()
    return lima.status(instance_name(name))


def ensure_running(name, lima=None, log=print):
    lima = lima or lima_mod.Limactl()
    st = status(name, lima)
    if st is None:
        raise VmError(f"sandbox {name} has no VM ({instance_name(name)}); "
                      f"remove it with `wmf-sbx rm {name}` and create it again")
    if st != "Running":
        log(f"+ limactl start {instance_name(name)}")
        lima.start(instance_name(name), timeout=START_TIMEOUT)


def stop(name, lima=None):
    lima = lima or lima_mod.Limactl()
    if status(name, lima) == "Running":
        lima.stop(instance_name(name))


def delete(name, lima=None):
    lima = lima or lima_mod.Limactl()
    if status(name, lima) is not None:
        lima.delete(instance_name(name))


def shell(name, argv, lima=None, workdir=ENGINEER_HOME, check=True, capture=True,
          input=None):
    """Run argv in the guest as the engineer (Lima's user)."""
    lima = lima or lima_mod.Limactl()
    args = ["shell", f"--workdir={workdir}", instance_name(name), "--"] + list(argv)
    return lima.call(*args, check=check, capture=capture, input=input)


# The security invariants that `start` and `status` check in a running
# sandbox (HANDOFF-LIMA.md §4, contained mode). Each is a shell test that
# prints nothing and exits 0 when the invariant holds.
INVARIANTS = [
    ("the agent is not in sudo or docker",
     "! id -nG agent | grep -qwE 'sudo|docker'"),
    ("the agent cannot sudo",
     "! sudo -u agent sudo -n true 2>/dev/null"),
    ("the host block (nft) is loaded",
     "sudo -n nft list table inet wmf_sbx_hostblock >/dev/null"),
    ("TIOCSTI is off",
     "test \"$(sysctl -n dev.tty.legacy_tiocsti)\" = 0"),
    ("no mount is writable",
     "! findmnt -rn -o OPTIONS -t 9p,virtiofs | grep -qE '(^|,)rw(,|$)'"),
    ("the agent has the host uid",
     "test \"$(id -u agent)\" = \"$WMF_SBX_HOST_UID\""),
]


def check_invariants(name, host_uid=None, lima=None):
    """[(description, ok)] for each invariant, in one guest call."""
    host_uid = os.getuid() if host_uid is None else host_uid
    script = [f"WMF_SBX_HOST_UID={int(host_uid)}"]
    for i, (_desc, test) in enumerate(INVARIANTS):
        script.append(f"if {test}; then echo {i}:ok; else echo {i}:FAIL; fi")
    result = shell(name, ["bash", "-c", "\n".join(script)], lima=lima, check=False)
    seen = {}
    for line in (result.stdout or "").splitlines():
        idx, _, verdict = line.strip().partition(":")
        if idx.isdigit():
            seen[int(idx)] = verdict == "ok"
    return [(desc, seen.get(i, False)) for i, (desc, _t) in enumerate(INVARIANTS)]


def quote(argv):
    return " ".join(shlex.quote(a) for a in argv)


def resolve(arg, env=None):
    """(name, state) for a NAME or path-shortcut argument. Raises
    state.StateError unless wmf-sbx owns the sandbox."""
    from . import state as state_mod
    name = state_mod.resolve_name_arg(arg, env=env)
    return name, state_mod.require(name, env=env)
