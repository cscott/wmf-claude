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
import pwd
import re
import shlex
import shutil
import tempfile

from . import disk as disk_mod
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


# Lima runs this in place of QEMU (QEMU_SYSTEM_<ARCH>): it adds free-page
# reporting to every QEMU sandbox (qemu_wrapper.py).
QEMU_WRAPPER = os.path.join(image_mod.REPO_ROOT, "sbx", "bin", "wmf-sbx-qemu")
VMTYPE_RE = re.compile(r"^vmType:\s*['\"]?(\w+)", re.M)


def qemu_env(vm_type, env=None, arch=None):
    """The variables for `limactl start` of a QEMU instance in contained
    mode: the QEMU wrapper, the real QEMU, plain mode (no egress proxy).
    {} for another vmType, or without QEMU or the wrapper."""
    if vm_type != "qemu":
        return {}
    from . import qemu_wrapper as qw
    env = os.environ if env is None else env
    arch = arch or image_mod.host_arch()
    var = f"QEMU_SYSTEM_{arch.upper()}"
    real = env.get(var) or shutil.which(f"qemu-system-{arch}")
    if not real or os.path.realpath(real) == os.path.realpath(QEMU_WRAPPER) \
            or not os.access(QEMU_WRAPPER, os.X_OK):
        return {}
    return {var: QEMU_WRAPPER, qw.REAL_VAR: real, qw.PLAIN_VAR: "1"}


def vm_type_of(name, lima, env=None):
    """The instance's vmType, from its lima.yaml, or None."""
    m = VMTYPE_RE.search(lima.config_text(instance_name(name), env) or "")
    return m.group(1) if m else None


def start_instance(name, lima, env=None, vm_type=None):
    """`limactl start`, through the QEMU wrapper on QEMU."""
    vm_type = vm_type or vm_type_of(name, lima, env)
    lima.start(instance_name(name), timeout=START_TIMEOUT,
               extra_env=qemu_env(vm_type, env) or None)


def create(name, tmpl, lima=None, log=print, backing=None, env=None):
    """Create the instance from `tmpl` (a template.py dict) and boot it.
    Without `backing`, `limactl create` copies the golden image (D2).
    With it, the template's image is disk.py's placeholder, and the
    instance's disk becomes a qcow2 overlay on `backing` (D12), checked
    before and after the first start."""
    lima = lima or lima_mod.Limactl()
    inst = instance_name(name)
    if lima.exists(inst):
        raise VmError(f"a Lima instance {inst} exists already; remove it first "
                      f"(`limactl delete {inst}`), or choose another --name")
    with tempfile.TemporaryDirectory(prefix="wmf-sbx-tmpl.") as d:
        path = write_template(tmpl, d)
        lima.validate(path)
        log(f"+ limactl create --name={inst} "
            f"({'with a placeholder disk' if backing else 'copies the golden image'})")
        lima.create(inst, path)
    if backing:
        log(f"+ qemu-img create -b {backing} (the disk: a copy-on-write overlay)")
        lima.overlay_disk(inst, backing, env=env)
        check_disk(name, lima=lima, env=env)
    log(f"+ limactl start {inst} (the first boot runs the provisioning)")
    start_instance(name, lima, env=env, vm_type=tmpl.get("vmType"))
    if backing:
        check_disk(name, lima=lima, env=env, running=True)
    return inst


def check_disk(name, lima=None, env=None, running=False):
    """The backing files of the sandbox's overlay disk, after
    disk.check_chain; VmError if the chain is wrong."""
    lima = lima or lima_mod.Limactl()
    inst = instance_name(name)
    infos = lima.disk_chain(inst, env=env, force_share=running)
    try:
        return disk_mod.check_chain(infos, lima.disk_path(inst, env),
                                    image_mod.cache_root(env))
    except disk_mod.DiskError as e:
        raise VmError(f"{name}: {e}") from None


def status(name, lima=None):
    """Lima's status of the instance ('Running', 'Stopped', ...), or None."""
    lima = lima or lima_mod.Limactl()
    return lima.status(instance_name(name))


PROXY_PORTS_RE = re.compile(r'PROXY_PORTS=\\?"([0-9 ]*)\\?"')


def configured_proxy_ports(name, lima, env=None):
    """The proxy ports in the instance's provisioning, or None if the
    config cannot be read."""
    m = PROXY_PORTS_RE.search(lima.config_text(instance_name(name), env))
    return [int(p) for p in m.group(1).split()] if m else None


def refresh_proxy_ports(name, lima, env=None, log=print, running=False):
    """The host block lets the agent reach the host's loopback proxy on the
    ports in the provisioning, which create fills in. A proxy whose port
    changed (a restarted cloud container, RAN) is then refused in the
    guest. Put the current ports in, on a stopped instance; on a running
    one, say that a restart is needed. Returns True if it changed."""
    want = template_mod.loopback_proxy_ports(env)
    have = configured_proxy_ports(name, lima, env)
    if have is None or have == want:
        return False
    if running:
        pid = unsignalled_hostagent(name, env)
        how = (f"`wmf-sbx stop {name}` and start it again" if pid is None
               else outside_stop_advice(name, pid))
        log(f"warning: the host's proxy port is now {' '.join(map(str, want)) or 'none'}, "
            f"the VM allows {' '.join(map(str, have)) or 'none'}: {how}")
        return False
    ports = " ".join(str(p) for p in want)
    log(f"+ limactl edit {instance_name(name)} (proxy ports {ports or 'none'})")
    lima.edit(instance_name(name), '.provision[0].script |= sub("PROXY_PORTS=\\"[0-9 ]*\\""; '
                                   f'"PROXY_PORTS=\\"{ports}\\"")')
    return True


def ensure_running(name, lima=None, log=print, env=None):
    lima = lima or lima_mod.Limactl()
    st = status(name, lima)
    if st is None:
        raise VmError(f"sandbox {name} has no VM ({instance_name(name)}); "
                      f"remove it with `wmf-sbx rm {name}` and create it again")
    refresh_proxy_ports(name, lima, env=env, log=log, running=st == "Running")
    if st != "Running":
        # A nested sandbox's inner VMs read nono's CA from a fixed path
        # (image.session_ca_file) when they start; this session's CA
        # replaces the one an earlier session wrote there.
        try:
            image_mod.extra_ca_files(env)
        except (image_mod.ImageError, OSError) as e:
            log(f"warning: could not refresh the extra CA certificates ({e})")
        log(f"+ limactl start {instance_name(name)}")
        start_instance(name, lima, env=env)


def unsignalled_hostagent(name, env=None, kill=None):
    """The pid of the instance's Lima host agent if it runs and this
    process may not signal it, else None. Under nono, Landlock scopes
    signals to the session's sandbox: a VM that an earlier session
    started has a host agent outside it, and `limactl stop` gets EPERM,
    then waits 3 minutes for an exit that never comes (RAN, N3)."""
    try:
        with open(os.path.join(lima_mod.lima_home(env), instance_name(name), "ha.pid"),
                  encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        (kill or os.kill)(pid, 0)
    except PermissionError:
        return pid
    except OSError:
        pass
    return None


def outside_stop_advice(name, pid):
    inst = instance_name(name)
    user = pwd.getpwuid(os.getuid()).pw_name
    return (f"its Lima host agent (pid {pid}) runs outside this session's sandbox "
            f"(an earlier session started it), so it cannot be stopped from here; "
            f"the next `wmf-sbx resume` of the outer sandbox stops it, "
            f"or stop it now from outside nono (as the outer VM's "
            f"engineer: `sudo -u {user} limactl stop {inst}`), then "
            f"`wmf-sbx start {name}`")


def stop(name, lima=None, env=None, kill=None):
    lima = lima or lima_mod.Limactl()
    if status(name, lima) == "Running":
        pid = unsignalled_hostagent(name, env, kill=kill)
        if pid is not None:
            raise VmError(f"cannot stop {name}: {outside_stop_advice(name, pid)}")
        lima.stop(instance_name(name))


def delete(name, lima=None):
    lima = lima or lima_mod.Limactl()
    if status(name, lima) is not None:
        lima.delete(instance_name(name))


def shell(name, argv, lima=None, workdir=ENGINEER_HOME, check=True, capture=True,
          input=None, interactive=False):
    """Run argv in the guest as the engineer (Lima's user). Only an
    `interactive` command gets the caller's stdin (Limactl.call)."""
    lima = lima or lima_mod.Limactl()
    args = ["shell", f"--workdir={workdir}", instance_name(name), "--"] + list(argv)
    if interactive:
        return lima.call(*args, check=check, capture=capture, input=input,
                         interactive=True)
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
    # Lima writes each mount to /etc/fstab (cloud-init). A kernel with no
    # 9p leaves the entries there and mounts nothing (RAN, phase 4).
    ("every git dir is mounted",
     "awk '$3==\"9p\"||$3==\"virtiofs\"{print $2}' /etc/fstab | "
     "{ while read -r m; do mountpoint -q \"$(printf '%b' \"$m\")\" || exit 1; done; }"),
    # Not a security rule, but cheap: the agent's `node` is the image's
    # pinned Node, not another one earlier on its PATH.
    ("the agent's node is the image's",
     f"test \"$(sudo -u agent env PATH={AGENT_PATH} node --version)\" = "
     "\"v$(jq -r .inputs.node.version /etc/wmf-sbx-image.json)\""),
    # Only `create --nested` gives the agent /dev/kvm (nested.py); root
    # writes the marker, which the agent cannot.
    ("the agent is in the kvm group only in a nested sandbox",
     "! id -nG agent | grep -qw kvm || test -e /etc/wmf-sbx/nested"),
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
