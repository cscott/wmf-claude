#!/usr/bin/env python3
"""The Lima config of one wmf-sbx sandbox (HANDOFF-LIMA.md §5.3).

`sandbox_template()` returns the config as a dict; create.py writes it
as YAML and gives it to `limactl create`. It is generated, not composed
with Lima's EXPERIMENTAL `base:`.

The rules, which `check_template()` asserts, and the tests too:

- closed like Kosta's lima/wmf-claude.yaml: the ignore-all
  `portForwards` rule (RAN: without it the guest agent forwards guest
  ports to the host), no containerd, no SSH agent or X11 forwarding, no
  rosetta, no extra networks;
- `plain: false`, because plain mode ignores `mounts` (D10);
- every mount is read-only, under /run/wmf-sbx/host/, of a git dir, with
  `mountType` 9p on QEMU and virtiofs on vz, never reverse-sshfs;
- Lima's user (`engineer`) has a uid that is neither the host's (the
  agent has that, D10) nor the image builder's.
"""

import os
import platform
import re

from . import image as image_mod

PROVISION_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "sandbox-provision.sh")
ENGINEER = "engineer"
ENGINEER_UID = 59998
HOST_MOUNT_ROOT = "/run/wmf-sbx/host"

DEFAULT_CPUS = 4
DEFAULT_MEMORY = "4GiB"
DEFAULT_DISK = "60GiB"

MOUNT_TYPES = {"qemu": "9p", "vz": "virtiofs"}


class TemplateError(Exception):
    """A config that breaks one of the rules in this module's docstring."""


def default_vm_type(system=None):
    """Lima's default driver for this host: vz on macOS, QEMU elsewhere."""
    system = system or platform.system()
    return "vz" if system == "Darwin" else "qemu"


def mount_point(host_gitdir):
    """Where a host git dir appears in the guest: the same path, under
    /run/wmf-sbx/host/ (D10)."""
    return HOST_MOUNT_ROOT + os.path.abspath(host_gitdir)


def loopback_proxy_ports(env=None):
    """The ports of the host's proxy variables that name a loopback proxy.
    Lima gives the guest such a proxy as 192.168.5.2:<port>, and the
    provisioning must let that port through the host block."""
    env = os.environ if env is None else env
    ports = set()
    for var in image_mod.PROXY_VARS:
        m = re.match(r"^\w+://(?:[^@/]*@)?(?:localhost|127\.0\.0\.1):(\d+)(?:/|$)",
                     env.get(var) or "")
        if m:
            ports.add(int(m.group(1)))
    return sorted(ports)


def provision_script(proxy_ports=()):
    with open(PROVISION_SCRIPT, encoding="utf-8") as f:
        script = f.read()
    return (script.replace("@ENGINEER@", ENGINEER)
                  .replace("@PROXY_PORTS@", " ".join(str(p) for p in proxy_ports)))


def sandbox_template(golden_path, arch, gitdirs=(), vm_type=None, cpus=DEFAULT_CPUS,
                     memory=DEFAULT_MEMORY, disk=DEFAULT_DISK, proxy_ports=(),
                     ca_files=(), host_uid=None):
    """The instance config, as a dict.

    `golden_path` is the cached golden image; `limactl create` copies it
    (D2). `gitdirs` are host git dirs to mount read-only (phase 4)."""
    vm_type = vm_type or default_vm_type()
    if vm_type not in MOUNT_TYPES:
        raise TemplateError(f"unknown vmType {vm_type!r} (want qemu or vz)")
    host_uid = os.getuid() if host_uid is None else host_uid
    if host_uid == ENGINEER_UID:
        raise TemplateError(f"the host uid {host_uid} is the engineer's uid in the guest")
    tmpl = {
        "minimumLimaVersion": "2.0.0",
        "vmType": vm_type,
        "plain": False,
        "images": [{"location": os.path.abspath(golden_path), "arch": arch}],
        "cpus": cpus,
        "memory": memory,
        "disk": disk,
        "mountType": MOUNT_TYPES[vm_type],
        "mounts": [{"location": os.path.abspath(g), "mountPoint": mount_point(g),
                    "writable": False} for g in gitdirs],
        "containerd": {"system": False, "user": False},
        "portForwards": [{"guestIP": "0.0.0.0", "proto": "any", "ignore": True}],
        "ssh": {"loadDotSSHPubKeys": False, "forwardAgent": False,
                "forwardX11": False, "forwardX11Trusted": False},
        "user": {"name": ENGINEER, "home": f"/home/{ENGINEER}", "uid": ENGINEER_UID},
        "provision": [{"mode": "system", "script": provision_script(proxy_ports)}],
        "probes": [{
            "mode": "readiness",
            "description": "wmf-sbx sandbox provisioning",
            "script": "#!/bin/bash\nset -eu\n"
                      "test -f /run/wmf-sbx/provisioned\n"
                      "! id -nG agent | grep -qwE 'sudo|docker'\n"
                      "sudo -n nft list table inet wmf_sbx_hostblock >/dev/null\n",
            "hint": "Provisioning did not finish. In the VM: sudo journalctl -b | grep -i provision",
        }],
    }
    if ca_files:
        tmpl["caCerts"] = {"files": [os.path.abspath(f) for f in ca_files]}
    check_template(tmpl, host_uid=host_uid)
    return tmpl


def check_template(tmpl, host_uid=None):
    """Raise TemplateError if `tmpl` breaks a rule in the module docstring."""
    def bad(msg):
        raise TemplateError(msg)

    for key in ("rosetta", "vmOpts", "networks", "base"):
        if key in tmpl:
            bad(f"`{key}` is not allowed in a wmf-sbx template")
    if tmpl.get("plain") is not False:
        bad("plain must be false (plain mode ignores mounts)")
    if {"guestIP": "0.0.0.0", "proto": "any", "ignore": True} not in (tmpl.get("portForwards") or []):
        bad("the ignore-all portForwards rule is missing")
    if tmpl.get("containerd") != {"system": False, "user": False}:
        bad("containerd must be off")
    if any((tmpl.get("ssh") or {}).get(k) for k in
           ("loadDotSSHPubKeys", "forwardAgent", "forwardX11", "forwardX11Trusted")):
        bad("SSH agent and X11 forwarding must be off")
    want = MOUNT_TYPES.get(tmpl.get("vmType"))
    if tmpl.get("mountType") != want:
        bad(f"mountType must be {want!r} for vmType {tmpl.get('vmType')!r}")
    home = os.path.expanduser("~")
    for m in tmpl.get("mounts") or []:
        loc = m.get("location", "")
        if m.get("writable") is not False:
            bad(f"mount {loc} must have writable: false")
        if not (m.get("mountPoint") or "").startswith(HOST_MOUNT_ROOT + "/"):
            bad(f"mount {loc} must be under {HOST_MOUNT_ROOT}/")
        if os.path.basename(loc.rstrip("/")) != ".git" and not loc.endswith(".git"):
            bad(f"mount {loc} is not a git dir")
        if os.path.abspath(loc) in (home, "/"):
            bad(f"mount {loc} is a home or root directory")
    user = tmpl.get("user") or {}
    if user.get("uid") in (None, host_uid, image_mod.BUILDER_UID):
        bad("Lima's user needs its own uid: not the host's (the agent's) nor the builder's")
