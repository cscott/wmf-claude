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

import hashlib
import os
import shlex
import urllib.request

from . import egress as egress_mod
from . import image as image_mod
from . import lima as lima_mod
from . import vm as vm_mod

LIMA_VERSION = "2.2.1"
LIMA_RELEASES = "https://github.com/lima-vm/lima/releases/download"
# Written by the install, as root: the invariant "the agent is not in the
# kvm group" holds unless it exists, and the agent cannot create it.
MARKER = image_mod.NESTED_MARKER

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
             "/usr/local/share/lima", "/usr/local/libexec/lima",
             image_mod.LOCAL_BASE_DIR)
LIMA_HOME = f"{vm_mod.AGENT_HOME}/.lima"
# The inner wmf-sbx's own state and settings. Its images go in ~/.cache,
# which every session has.
WMF_SBX_DIRS = (f"{vm_mod.AGENT_HOME}/.local/state/wmf-sbx",
                f"{vm_mod.AGENT_HOME}/.config/wmf-sbx")
# The repos of inner sandboxes: SRC_NAME in the engineer's home path, as
# on the host (the outer VM has the clones at their host paths, D8). Not
# under /home/agent: the inner VM has the clones at the same paths, and
# that is its own agent's home (RAN, N3: create refused
# ~/wmf-sbx-src/extensions/Cite).
SRC_NAME = "wmf-sbx-src"
# Where `create` copies the host's base image in, for a sandbox behind the
# egress proxy: cloud.debian.org redirects to a geo-picked mirror, which
# its list cannot name. The install moves it to image.LOCAL_BASE_DIR.
STAGED_BASE = "/tmp/wmf-sbx-base.qcow2"


class NestedError(Exception):
    pass


def base_cache_dir(env=None):
    """The host's copies of base images, beside the image cache."""
    return os.path.join(os.path.dirname(image_mod.cache_root(env)), "base")


def _digest_ok(path, digest):
    algo, _, want = digest.partition(":")
    if algo not in ("sha256", "sha512") or not want:
        raise NestedError(f"unknown base image digest {digest!r}")
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest() == want


def host_base(base, env=None, log=print, urlopen=urllib.request.urlopen):
    """The path of a host copy of the base image `base` (image.base_image()),
    downloaded once and checked against its digest. Copies of other base
    images are removed."""
    d = base_cache_dir(env)
    os.makedirs(d, exist_ok=True)
    name = os.path.basename(base["location"])
    path = os.path.join(d, name)
    if not (os.path.isfile(path) and _digest_ok(path, base["digest"])):
        log(f"+ base image {name} (downloaded on the host, for the sandbox)")
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            with urlopen(base["location"], timeout=60) as resp, open(tmp, "wb") as f:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    f.write(chunk)
            if not _digest_ok(tmp, base["digest"]):
                raise NestedError(f"{base['location']}: the digest does not match")
            os.replace(tmp, path)
        except OSError as e:
            raise NestedError(f"downloading {base['location']} failed: {e}") from None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    for other in os.listdir(d):
        if other != name:
            os.unlink(os.path.join(d, other))
    return path


def src_dir(host_home):
    """The directory for the inner sandboxes' repos (state `nestedSrc`)."""
    return os.path.join(os.path.normpath(host_home), SRC_NAME)

# The hosts an image build and a `create` in the sandbox reach, through
# the session's nono proxy (Lima points the inner VMs at it), besides the
# profile's and session.REGISTRY_DOMAINS.
DOMAINS = (
    "cloud.debian.org", "deb.debian.org", "security.debian.org",   # base image, apt
    "nodejs.org",                                                  # Node
    "downloads.claude.ai",                                         # Claude Code
    "release-assets.githubusercontent.com",                        # nono, Lima releases
    "pypi.org", "files.pythonhosted.org",                          # MCP servers' venvs
    "registry.nono.sh",                                            # wmf-claude-setup: nono pull
)



def settings(src):
    """The inner wmf-sbx's settings, if the engineer has none there: the
    repos of inner sandboxes go to `src` (RAN, N3: with no settings,
    `create gerrit:...` has no rule), the one writable directory the
    session gives them."""
    return f"""\
# Written by `wmf-sbx create --nested` (sbx/reference/example-settings.yaml).
repos:
  - match: "gerrit:mediawiki/core"
    path: "{src}/core"
  - match: "gerrit:mediawiki/extensions/{{name}}"
    path: "{src}/extensions/{{name}}"
  - match: "gerrit:mediawiki/skins/{{name}}"
    path: "{src}/skins/{{name}}"
  - match: "gerrit:**/{{name}}"
    path: "{src}/{{name}}"
  - match: "gitlab:**/{{name}}"
    path: "{src}/{{name}}"
"""


# Default resources: an image build and one inner sandbox (D2 copies).
# 10 GiB, not 16 (RAN 2026-10-13): QEMU keeps what a guest has touched,
# and a 16 GiB nested sandbox that built an image held 15 GB of the host
# until the host's OOM killer took it. The inner work runs one VM at a
# time: the 4 GiB image builder, or an inner sandbox at INNER_MEMORY.
MEMORY = "10GiB"
# The default memory of a sandbox made inside a nested sandbox.
INNER_MEMORY = "4GiB"


def inside():
    """Whether this runs in a nested sandbox (the install's marker)."""
    return os.path.exists(MARKER)
DISK = "120GiB"


def session_grants(src):
    """bin/claude flags for a nested sandbox's session (N1). The inner VM's
    SSH port needs none: Lima binds a port from the kernel's ephemeral
    range, the window the session opens already (--open-port is connect
    and listen)."""
    grants = ["--allow", LIMA_HOME, "--allow-unix-socket-subtree-bind", LIMA_HOME,
              "--allow-file", "/dev/kvm"]
    # A nested sandbox made before `nestedSrc` existed has no src.
    for d in WMF_SBX_DIRS + ((src,) if src else ()):
        grants += ["--allow", d]
    for d in READ_DIRS:
        grants += ["--read", d]
    for d in DOMAINS:
        grants += ["--allow-domain", d]
    return grants


# The loopback forwarder to the egress proxy (egress.FORWARD_PORT), a
# root unit. systemd-socket-proxyd is in Debian's systemd package.
FORWARD_UNIT = "wmf-sbx-egress-forward"


def forward_script(target):
    """Bash, as root: the units of the loopback forwarder to `target`
    (host:port, the egress proxy), enabled and started. Root's, so a
    contained agent cannot change it; nono gives a contained session no
    connect to its port, and `exec` reaches the proxy itself anyway."""
    unit = f"/etc/systemd/system/{FORWARD_UNIT}"
    desc = "wmf-sbx: the egress proxy on the loopback, for inner VMs"
    return f"""cat > {unit}.socket <<'WMF_SBX_UNIT'
[Unit]
Description={desc}

[Socket]
ListenStream=127.0.0.1:{egress_mod.FORWARD_PORT}

[Install]
WantedBy=sockets.target
WMF_SBX_UNIT
cat > {unit}.service <<'WMF_SBX_UNIT'
[Unit]
Description={desc}
Requires={FORWARD_UNIT}.socket
After={FORWARD_UNIT}.socket

[Service]
ExecStart=/usr/lib/systemd/systemd-socket-proxyd {target}
DynamicUser=yes
WMF_SBX_UNIT
systemctl daemon-reload
systemctl enable --now {FORWARD_UNIT}.socket >/dev/null
echo "+ forwarder 127.0.0.1:{egress_mod.FORWARD_PORT} -> {target}, for inner VMs" >&2
"""


# stop_inner_script's exit status while a session runs in the sandbox.
SESSION_RUNNING = 3


def stop_inner_script(limactl_path="/usr/local/bin/limactl"):
    """A bash script, run as root in a nested sandbox before a session
    starts: stop the inner VMs that still run. An inner VM's Lima host agent stays in the sandbox of the session
    that started it; Landlock scopes signals to the sandbox, so a later
    session cannot stop it (RAN; vm.unsignalled_hostagent), and it keeps
    its old proxy password, port and CA. Root, outside nono, can. Not
    while a session runs (an agent's nono process): its inner VMs are
    in use. Exits SESSION_RUNNING then. Prints `stopped INSTANCE` or
    `failed INSTANCE` for each."""
    limactl = (f"runuser -u {shlex.quote(vm_mod.AGENT)} -- env HOME={vm_mod.AGENT_HOME} "
               f"{shlex.quote(limactl_path)}")
    return f"""set -uo pipefail
if pgrep -u {shlex.quote(vm_mod.AGENT)} -x nono >/dev/null; then exit {SESSION_RUNNING}; fi
[ -x {shlex.quote(limactl_path)} ] || exit 0
{limactl} list --format '{{{{.Name}}}} {{{{.Status}}}}' 2>/dev/null |
while read -r inst status; do
  [ "$status" = Running ] || continue
  # Not the list on stdin.
  if {limactl} stop "$inst" </dev/null >/dev/null 2>&1; then
    echo "stopped $inst"
  else
    echo "failed $inst"
  fi
done
"""


def stop_inner(name, lima=None, log=print):
    """Run stop_inner_script in sandbox `name`; log what it stopped, or
    that a session runs."""
    try:
        res = vm_mod.shell(name, ["sudo", "bash", "-s"], lima=lima,
                           input=stop_inner_script(), check=False)
    except lima_mod.LimaError as e:
        log(f"warning: could not stop the inner VMs that an earlier session started ({e})")
        return []
    if res.returncode == SESSION_RUNNING:
        log(f"note: a session runs in {name}; its inner VMs stay as they are")
        return []
    words = [line.split(None, 1) for line in (res.stdout or "").splitlines()]
    shown = {"stopped": [], "failed": []}
    for w in words:
        if len(w) == 2 and w[0] in shown:
            inst = w[1]
            shown[w[0]].append(inst[len(vm_mod.PREFIX):] if inst.startswith(vm_mod.PREFIX)
                               else inst)
    stopped, failed = shown["stopped"], shown["failed"]
    if stopped:
        log(f"+ stopped the inner VMs that an earlier session started: {' '.join(stopped)} "
            f"(`wmf-sbx start NAME` in the session starts one again, with its proxy and CA)")
    if failed:
        log(f"warning: could not stop the inner VMs {' '.join(failed)}; the session cannot "
            f"stop them either")
    return stopped


def install_script(arch, base, src, staged=None, forward=None):
    """A bash script, run as root in the sandbox: QEMU, firmware, Lima
    LIMA_VERSION (checked against its release's SHA256SUMS), the base
    image `base` (image.base_image(), checked against its digest) in
    image.LOCAL_BASE_DIR, `src` for the inner sandboxes' repos, settings
    for the inner wmf-sbx if it has none,
    the agent in the kvm group, and the marker. With `staged`, the base
    image is that file in the guest (STAGED_BASE), not a download. With
    `forward` (host:port, the egress proxy), its loopback forwarder
    (forward_script)."""
    if arch not in PACKAGES:
        raise ValueError(f"no nested support for {arch}")
    tarball = f"lima-{LIMA_VERSION}-Linux-{LIMA_ARCH[arch]}.tar.gz"
    lima_url = f"{LIMA_RELEASES}/v{LIMA_VERSION}"
    algo, _, digest = base["digest"].partition(":")
    if algo not in ("sha256", "sha512") or not digest:
        raise ValueError(f"unknown base image digest {base['digest']!r}")
    base_file = os.path.join(image_mod.LOCAL_BASE_DIR, os.path.basename(base["location"]))
    settings_file = f"{vm_mod.AGENT_HOME}/.config/wmf-sbx/settings.yaml"
    limactl = (f"runuser -u {shlex.quote(vm_mod.AGENT)} -- env HOME={vm_mod.AGENT_HOME} "
               "/usr/local/bin/limactl")
    q = shlex.quote
    fetch_base = (f"install -m 0644 {q(staged)} {q(base_file + '.tmp')}" if staged else
                  f"curl -fsSL -o {q(base_file + '.tmp')} {q(base['location'])}")
    cleanup = f"rm -f {q(staged)}\n" if staged else ""
    return f"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "+ apt-get install {' '.join(PACKAGES[arch])}" >&2
apt-get update -q >/dev/null
apt-get install -y -q {' '.join(PACKAGES[arch])} >/dev/null
# limactl refuses to run as root (RAN, N3).
if ! {limactl} --version 2>/dev/null | grep -q {q(LIMA_VERSION)}; then
  echo "+ Lima {LIMA_VERSION}" >&2
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/{tarball}" {q(lima_url + '/' + tarball)}
  curl -fsSL -o "$tmp/SHA256SUMS" {q(lima_url + '/SHA256SUMS')}
  (cd "$tmp" && grep ' {tarball}$' SHA256SUMS | sha256sum -c --quiet)
  tar -xzf "$tmp/{tarball}" -C /usr/local
  rm -rf "$tmp"
fi
install -d -m 0755 {' '.join(q(d) for d in READ_DIRS)}
# The base image, for image builds in the sandbox (image.local_base()).
if ! echo {q(digest + '  ' + base_file)} | {algo}sum -c --quiet >/dev/null 2>&1; then
  echo "+ base image {os.path.basename(base_file)}" >&2
  {fetch_base}
  echo {q(digest + '  ' + base_file + '.tmp')} | {algo}sum -c --quiet
  chmod 0644 {q(base_file + '.tmp')}
  mv {q(base_file + '.tmp')} {q(base_file)}
fi
{cleanup}# Root makes it: the agent may not write its parent.
install -d -m 0700 -o {q(vm_mod.AGENT)} -g "$(id -g {q(vm_mod.AGENT)})" {q(src)}
runuser -u {q(vm_mod.AGENT)} -- mkdir -p -m 0700 {' '.join(q(d) for d in (LIMA_HOME,) + WMF_SBX_DIRS)}
runuser -u {q(vm_mod.AGENT)} -- sh -c {q('test -e "$1" || cat > "$1"')} sh {q(settings_file)} <<'WMF_SBX_SETTINGS'
{settings(src)}WMF_SBX_SETTINGS
{forward_script(forward) if forward else ""}usermod -aG kvm {q(vm_mod.AGENT)}
install -d -m 0755 /etc/wmf-sbx
touch {q(MARKER)}
if [ ! -c /dev/kvm ]; then
  echo "warning: no /dev/kvm in the sandbox: the host gives it no nested virtualisation, so inner VMs run without KVM (slowly)" >&2
fi
echo "+ nested: $({limactl} --version), $(id -nG {q(vm_mod.AGENT)})" >&2
"""
