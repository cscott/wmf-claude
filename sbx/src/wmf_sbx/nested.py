#!/usr/bin/env python3
"""Nested sandboxes (`wmf-sbx create --nested`, HANDOFF-LIMA.md track C):
a sandbox whose agent can run Lima VMs of its own, under nono, to work on
wmf-sbx itself.

Decided (cananian, 2026-10-12): the golden image stays without QEMU and
Lima, because work on wmf-claude is a small share of its use. A nested
sandbox installs them at `create`, as root, and only it gives the agent
the `kvm` group and the session grants below (N1, sbx/NOTES.md, "Nested
Lima").
"""

import shlex

from . import vm as vm_mod

LIMA_VERSION = "2.2.1"
LIMA_RELEASES = "https://github.com/lima-vm/lima/releases/download"
# Written by the install, as root: the invariant "the agent is not in the
# kvm group" holds unless it exists, and the agent cannot create it.
MARKER = "/etc/wmf-sbx/nested"

# Debian packages for QEMU and its firmware, by guest architecture, and
# PyYAML, which wmf-sbx itself needs.
PACKAGES = {
    "x86_64": ["qemu-system-x86", "qemu-utils", "ovmf", "python3-yaml"],
    "aarch64": ["qemu-system-arm", "qemu-utils", "qemu-efi-aarch64", "python3-yaml"],
}
LIMA_ARCH = {"x86_64": "x86_64", "aarch64": "aarch64"}

# What a nested Lima reads (N1). The install creates each directory, so
# that nono's grant has a path to apply to on either architecture.
READ_DIRS = ("/usr/share/qemu", "/usr/share/seabios", "/usr/share/OVMF", "/usr/share/ovmf",
             "/usr/share/AAVMF", "/usr/share/qemu-efi-aarch64",
             "/usr/local/share/lima", "/usr/local/libexec/lima")
LIMA_HOME = f"{vm_mod.AGENT_HOME}/.lima"

# The hosts an image build and a `create` in the sandbox reach, through
# the session's nono proxy (Lima points the inner VMs at it), besides the
# profile's and session.REGISTRY_DOMAINS.
DOMAINS = (
    "cloud.debian.org", "deb.debian.org", "security.debian.org",   # base image, apt
    "nodejs.org",                                                  # Node
    "downloads.claude.ai",                                         # Claude Code
    "release-assets.githubusercontent.com",                        # nono, Lima releases
)

# Default resources: an image build and one inner sandbox (D2 copies).
MEMORY = "16GiB"
DISK = "120GiB"


def session_grants():
    """bin/claude flags for a nested sandbox's session (N1). The inner VM's
    SSH port needs none: Lima binds a port from the kernel's ephemeral
    range, the window the session opens already (--open-port is connect
    and listen)."""
    grants = ["--allow", LIMA_HOME, "--allow-unix-socket-subtree-bind", LIMA_HOME,
              "--allow-file", "/dev/kvm"]
    for d in READ_DIRS:
        grants += ["--read", d]
    for d in DOMAINS:
        grants += ["--allow-domain", d]
    return grants


def install_script(arch):
    """A bash script, run as root in the sandbox: QEMU, firmware, Lima
    LIMA_VERSION (checked against its release's SHA256SUMS), the agent in
    the kvm group, and the marker."""
    if arch not in PACKAGES:
        raise ValueError(f"no nested support for {arch}")
    tarball = f"lima-{LIMA_VERSION}-Linux-{LIMA_ARCH[arch]}.tar.gz"
    base = f"{LIMA_RELEASES}/v{LIMA_VERSION}"
    q = shlex.quote
    return f"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "+ apt-get install {' '.join(PACKAGES[arch])}" >&2
apt-get update -q >/dev/null
apt-get install -y -q {' '.join(PACKAGES[arch])} >/dev/null
if ! /usr/local/bin/limactl --version 2>/dev/null | grep -q {q(LIMA_VERSION)}; then
  echo "+ Lima {LIMA_VERSION}" >&2
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/{tarball}" {q(base + '/' + tarball)}
  curl -fsSL -o "$tmp/SHA256SUMS" {q(base + '/SHA256SUMS')}
  (cd "$tmp" && grep ' {tarball}$' SHA256SUMS | sha256sum -c --quiet)
  tar -xzf "$tmp/{tarball}" -C /usr/local
  rm -rf "$tmp"
fi
install -d -m 0755 {' '.join(q(d) for d in READ_DIRS)}
usermod -aG kvm {q(vm_mod.AGENT)}
install -d -m 0755 /etc/wmf-sbx
touch {q(MARKER)}
if [ ! -c /dev/kvm ]; then
  echo "warning: no /dev/kvm in the sandbox: the host gives it no nested virtualisation, so inner VMs run without KVM (slowly)" >&2
fi
echo "+ nested: $(/usr/local/bin/limactl --version), $(id -nG {q(vm_mod.AGENT)})" >&2
"""
