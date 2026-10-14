#!/usr/bin/env python3
"""The wmf-sbx golden image: build it, cache it, list and remove it.

Read lima-port/HANDOFF-LIMA.md §5 and decision D1 (A: a Lima builder VM
from Kosta's pinned Debian image). In short:

- A golden image is a qcow2 file with no backing file. A builder VM makes
  it: install the packages, nono, Claude Code, the built wmf-claude tree
  and the helpers; seal the identity; stop; export the disk.
- The cache key is a hash of everything that goes in (`image_inputs`).
  An entry is `<cache>/images/<key>/` with `golden.qcow2` (mode 0444),
  `golden.sha256` and `manifest.json`. An entry is never written again:
  other inputs make another key. D12's overlays depend on that.
- Nothing per-sandbox goes in: no credentials, no workspace. One
  per-host value does: the `agent` user has the host user's uid and gid
  (D10), so mounted files are the agent's own. The uid and gid are in the
  key, so a host with another uid gets another image. On most machines
  they never change (cananian, 2026-10-09).

`wmf-sbx image build|ls|rm|gc|prune` calls `main()`.
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when yaml is absent
    yaml = None

from . import disk as disk_mod
from . import kit
from . import lima as lima_mod
from . import state as state_mod

SBX_ROOT = kit.SBX_ROOT
REPO_ROOT = kit.REPO_ROOT

# Kosta's template pins the base image (a versioned Debian 13 genericcloud
# release, with sha512 digests). Read it from there, so that there is one
# pin for both modes.
BASE_TEMPLATE = os.path.join(REPO_ROOT, "lima", "wmf-claude.yaml")
# A local copy of the base image, used instead of a download when it is
# there. Only a nested sandbox has one (nested.py): cloud.debian.org
# redirects to mirrors, which the session's nono does not allow (RAN, N3).
# It is not in the cache key: the digest is.
LOCAL_BASE_DIR = "/var/cache/wmf-sbx/base"
BUILD_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            "image-build.sh")
NONO_VERSION_FILE = os.path.join(REPO_ROOT, ".nono-version")

# Node, one version for the whole image, from nodejs.org (cananian,
# 2026-10-09): the version that WMF CI uses, so that `npm ci` and `npm
# test` in a repository behave as in CI, and the MCP servers run on it
# too. CI's images are node24-* at 24.18.0 (integration/config
# zuul/layout.yaml: "our current CI testing targets are Node 24"; Node
# 26 is experimental only), and MobileFrontend pins exactly 24.18.0
# (HANDOFF-LIMA.md §11, phase 5). Not Debian's (Debian 13 has Node 20),
# and not a version manager (nave, fresh-node): a contained agent cannot
# download a Node, and every shell would have to select one. The
# tarball's checksum comes from the release's SHASUMS256.txt and goes
# into the image inputs, so a new version or a changed tarball gives a
# new image. Change NODE_VERSION when the CI images change.
NODE_VERSION = "24.18.0"
NODE_DIST = "https://nodejs.org/dist"

# php-ast, built from source as WMF CI builds it (integration/config,
# dockerfiles/php-scratch/build-extension-ast): Phan 6 needs ast 1.1.3 or
# later, and Debian 13 has 1.1.2 (RAN, phase 7: `composer phan` refused to
# start). A commit, not a tag, so that the source cannot change. Change it
# when CI's does.
PHP_AST = {"version": "1.1.3", "url": "https://github.com/nikic/php-ast",
           "commit": "c2faa5d071454ba3c2d0170611e92846ca606bba"}

CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"
# `stable`, not `latest`: the image is shared by every sandbox that has the
# same key, so it takes the release channel that has had more use.
CLAUDE_CHANNEL = "stable"
# WMF_SBX_CLAUDE_VERSION picks another release: a channel (`stable`,
# `latest`) or an exact version (2.1.296). The version and its checksum go
# into the cache key, so another release gives another image.
CLAUDE_VERSION_VAR = "WMF_SBX_CLAUDE_VERSION"
# Replaces the HEAD commit in the cache key (tree_revision).
TREE_OVERRIDE_VAR = "WMF_SBX_GIT_HASH_OVERRIDE"
# Its first name. Still read, with a note once, when the new one is not
# set (wmf-sbx's own variables are WMF_SBX_*; WMF_CLAUDE_* are upstream's).
OLD_TREE_OVERRIDE_VAR = "WMF_CLAUDE_GIT_HASH_OVERRIDE"
_OLD_TREE_OVERRIDE_NOTED = []
TREE_OVERRIDE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# WMF_SBX_GIT_HASH_OVERRIDE=latest: `create` uses the newest cached image
# (for this architecture and host uid) instead of the one for the current
# inputs, and builds only when there is none (newest_image).
LATEST = "latest"
# A Debian package name (settings.yaml `packages:`).
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
CLAUDE_CHANNELS = ("stable", "latest")

# The packages, after kit.BASE_PACKAGES (the PHP side, unchanged from the
# Docker kit):
# - what the wmf-claude build and bin/claude need (lima/wmf-claude.yaml);
# - no Node: it comes from nodejs.org (NODE_VERSION). Debian's `npm`
#   also brings a few hundred node-* packages;
# - xz-utils, to unpack the Node tarball;
# - nftables, for the host block (§5.3);
# - unzip and zstd, for the helpers;
# - the shared libraries that Chrome for Testing needs on Debian 13
#   (`ldd`, RAN 2026-10-09). Contained sandboxes cannot apt-get, so
#   mw-install-browser must find them already here (§5.2).
IMAGE_PACKAGES = list(kit.BASE_PACKAGES) + [
    "ca-certificates", "curl", "git", "jq", "python3", "python3-venv",
    "nftables", "unzip", "xz-utils", "zstd",
    "libnss3", "libnspr4", "libatk1.0-0t64", "libatk-bridge2.0-0t64",
    "libx11-6", "libxcomposite1", "libxdamage1", "libxext6", "libxfixes3",
    "libxrandr2", "libgbm1", "libxcb1", "libxkbcommon0", "libasound2t64",
    "libatspi2.0-0t64",
    # The rest of what Chrome for Testing (mw-install-browser) links
    # against, and Xvfb and GTK 3 for Cypress's Electron
    # (mw-install-cypress). The agent has no sudo, so they are here.
    "libcups2t64", "libpango-1.0-0", "libcairo2", "libdbus-1-3", "libdrm2",
    "libexpat1", "libudev1", "libgtk-3-0t64", "xvfb", "xauth",
]

# The builder VM. Closed like Kosta's template (no mounts, no forwards),
# and with its own user name, which the build removes again.
BUILDER_PREFIX = "wmf-sbx-builder-"
BUILDER_USER = "wmfbuilder"
# Lima gives its user the host uid unless the template says otherwise.
# The build then creates `agent` with the host uid, so the builder's user
# must have another one. (RAN: without this, wmfbuilder got uid 30033.)
BUILDER_UID = 59999
AGENT_USER = "agent"
BUILDER_DISK = "20GiB"
BUILDER_START_TIMEOUT = "30m"

# Lima rewrites a proxy on the host's loopback to this address in the
# guest (pkg/cidata, `setupEnv`). The build does the same for the
# variables it passes through `sudo`, which drops them otherwise.
SLIRP_GATEWAY = "192.168.5.2"
PROXY_VARS = ("http_proxy", "https_proxy", "no_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")

KEY_RE = re.compile(r"^[0-9a-f]{16}$")

# Extra CA certificates for the build only, for a network whose proxy
# re-signs TLS (a corporate TLS-inspection proxy, or a cloud sandbox: RAN,
# github.com came back signed by the sandbox's interception CA). A list of
# host files, separated by ":". Lima installs them in the builder through
# cloud-init; image-build.sh removes them before it seals, so the image
# does not trust them and they are not an image input.
CA_CERTS_VAR = "WMF_SBX_CA_CERTS"
# In a nested sandbox (nested.py writes this marker, as root), a session's
# nono re-signs TLS for the hosts with endpoint rules (nodejs.org, RAN N3),
# and its CA is in $SSL_CERT_FILE. With no $WMF_SBX_CA_CERTS, the inner
# VMs get that file.
NESTED_MARKER = "/etc/wmf-sbx/nested"
# $SSL_CERT_FILE is the system's bundle plus nono's CA (RAN N3: 151
# certificates). The inner VMs get only what the system bundle lacks.
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
PEM_CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----")


class ImageError(Exception):
    """A user-facing failure in an image operation."""


# -- inputs and the cache key ---------------------------------------------

def host_arch():
    """Lima's name for this machine's architecture."""
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def claude_platform(arch):
    return {"x86_64": "linux-x64", "aarch64": "linux-arm64"}[arch]


def node_platform(arch):
    return {"x86_64": "linux-x64", "aarch64": "linux-arm64"}[arch]


def base_image(arch=None, template=BASE_TEMPLATE):
    """The pinned base image for `arch`, from Kosta's template:
    {"location": ..., "arch": ..., "digest": ...}."""
    arch = arch or host_arch()
    if yaml is None:
        raise ImageError("PyYAML is required (pip install -r sbx/requirements.txt)")
    with open(template, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    for img in doc.get("images") or []:
        if img.get("arch") == arch:
            if not img.get("digest"):
                raise ImageError(f"{template}: the {arch} image has no digest")
            return {"location": img["location"], "arch": arch,
                    "digest": img["digest"]}
    raise ImageError(f"{template} has no image for {arch}")


def nono_version(path=NONO_VERSION_FILE):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def _fetch(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def claude_release(arch, fetch=_fetch, channel=CLAUDE_CHANNEL, env=None):
    """(version, sha256) of the Claude Code release for `arch`, from the
    release manifest: the one on `channel`, or the channel or exact
    version that $WMF_SBX_CLAUDE_VERSION names."""
    requested = ((os.environ if env is None else env).get(CLAUDE_VERSION_VAR) or "").strip()
    if requested and not (requested in CLAUDE_CHANNELS
                          or re.match(r"^\d+\.\d+\.\d+$", requested)):
        raise ImageError(f"{CLAUDE_VERSION_VAR}={requested!r}: want "
                         f"{' or '.join(CLAUDE_CHANNELS)}, or a version such as 2.1.296")
    channel = requested or channel
    if channel in CLAUDE_CHANNELS:
        version = fetch(f"{CLAUDE_RELEASES}/{channel}").strip()
    else:
        version = channel
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        raise ImageError(f"unexpected Claude Code version {version!r} on {channel}")
    try:
        manifest = json.loads(fetch(f"{CLAUDE_RELEASES}/{version}/manifest.json"))
    except urllib.error.HTTPError as e:
        raise ImageError(f"no Claude Code release {version} ({e.code} for its manifest)")
    plat = claude_platform(arch)
    checksum = ((manifest.get("platforms") or {}).get(plat) or {}).get("checksum", "")
    if not re.match(r"^[0-9a-f]{64}$", checksum):
        raise ImageError(f"no checksum for {plat} in the {version} manifest")
    return version, checksum


def node_release(arch, fetch=_fetch, version=NODE_VERSION):
    """{version, file, sha256} of the Node tarball for `arch`, with the
    checksum from the release's SHASUMS256.txt."""
    name = f"node-v{version}-{node_platform(arch)}.tar.xz"
    checksum = ""
    for line in fetch(f"{NODE_DIST}/v{version}/SHASUMS256.txt").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == name:
            checksum = parts[0]
    if not re.match(r"^[0-9a-f]{64}$", checksum):
        raise ImageError(f"no checksum for {name} in Node v{version}'s SHASUMS256.txt")
    return {"version": version, "file": name, "sha256": checksum}


def head_commit(root=REPO_ROOT, run=subprocess.run):
    """The commit whose tree goes into the image. The tree comes from
    `git archive HEAD`, so uncommitted changes are not in it."""
    result = run(["git", "-C", root, "rev-parse", "--short=12", "HEAD"],
                 capture_output=True, text=True, check=True,
                 stdin=subprocess.DEVNULL)
    return result.stdout.strip()


def tree_override(env=None):
    """$WMF_SBX_GIT_HASH_OVERRIDE, stripped, or else its old name's value
    (with a note, once per run)."""
    env = os.environ if env is None else env
    if TREE_OVERRIDE_VAR in env:
        return (env.get(TREE_OVERRIDE_VAR) or "").strip()
    old = (env.get(OLD_TREE_OVERRIDE_VAR) or "").strip()
    if old and not _OLD_TREE_OVERRIDE_NOTED:
        _OLD_TREE_OVERRIDE_NOTED.append(True)
        print(f"note: {OLD_TREE_OVERRIDE_VAR} is now {TREE_OVERRIDE_VAR}; the old name "
              f"still works for now", file=sys.stderr)
    return old


def tree_revision(root=REPO_ROOT, run=subprocess.run, env=None):
    """The wmf-claude part of the cache key: the HEAD commit, or the value
    of $WMF_SBX_GIT_HASH_OVERRIDE. With the override, new commits do not
    give a new image; change its value to rebuild. The image still gets
    the tree of HEAD at the time it is built."""
    override = tree_override(env)
    if not override or override == LATEST:
        return head_commit(root, run=run)
    if not TREE_OVERRIDE_RE.match(override):
        raise ImageError(f"{TREE_OVERRIDE_VAR}={override!r}: want letters, digits, "
                         f"'.', '_' or '-' (at most 64)")
    return override


def helper_files():
    """{name in /usr/local/bin: host path}, the kit's helper scripts."""
    return {name: os.path.join(src_dir, name)
            for name, src_dir in sorted(kit.HELPER_SCRIPTS.items())}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def agent_ids(uid=None, gid=None):
    """The host user's uid and gid, for the agent (D10). Root and the
    builder's uid are refused."""
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    if uid == 0 or gid == 0:
        raise ImageError("run wmf-sbx as your own user, not as root")
    if uid == BUILDER_UID:
        raise ImageError(f"uid {uid} is the builder's uid; change BUILDER_UID")
    return {"name": AGENT_USER, "uid": uid, "gid": gid}


def wants_latest(env=None):
    """Whether WMF_SBX_GIT_HASH_OVERRIDE asks for the newest cached image."""
    return tree_override(env) == LATEST


def image_packages(extra=()):
    """The image's package list: IMAGE_PACKAGES, then the engineer's extra
    packages (settings.yaml `packages:`), sorted, without repeats."""
    bad = [p for p in extra if not isinstance(p, str) or not PACKAGE_RE.match(p)]
    if bad:
        raise ImageError(f"settings.yaml: `packages:` {bad[0]!r} is not a Debian package name")
    return list(IMAGE_PACKAGES) + sorted(set(extra) - set(IMAGE_PACKAGES))


def newest_image(env=None, arch=None, uid=None):
    """(key, manifest) of the newest cached image for `arch` and the host
    uid `uid` (the agent's uid, D10), or None."""
    arch = arch or host_arch()
    uid = os.getuid() if uid is None else uid
    for key, manifest in reversed(list_images(env)):
        inputs = manifest.get("inputs") or {}
        if inputs.get("arch") == arch and (inputs.get("agent") or {}).get("uid") == uid:
            return key, manifest
    return None


def image_inputs(arch=None, fetch=_fetch, run=subprocess.run, root=REPO_ROOT,
                 uid=None, gid=None, env=None, packages=()):
    """Everything that goes into the image (HANDOFF-LIMA.md §5.1)."""
    arch = arch or host_arch()
    version, checksum = claude_release(arch, fetch=fetch, env=env)
    return {
        "agent": agent_ids(uid, gid),
        "schema": 1,
        "arch": arch,
        "base": base_image(arch),
        "packages": image_packages(packages),
        "nono": nono_version(),
        "node": node_release(arch, fetch=fetch),
        "php_ast": dict(PHP_AST),
        "claude": {"version": version, "platform": claude_platform(arch),
                   "sha256": checksum},
        "tree": tree_revision(root, run=run, env=env),
        "helpers": {name: _sha256_file(path)
                    for name, path in helper_files().items()},
        "build_script": _sha256_file(BUILD_SCRIPT),
    }


def cache_key(inputs):
    blob = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# -- the cache ------------------------------------------------------------

def cache_root(env=None):
    """`${XDG_CACHE_HOME:-~/.cache}/wmf-sbx/images`, absolute. Do not move
    it once overlays exist (D12)."""
    env = os.environ if env is None else env
    base = env.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.realpath(os.path.join(base, "wmf-sbx", "images"))


def entry_dir(key, env=None):
    if not KEY_RE.match(key or ""):
        raise ImageError(f"{key!r} is not an image key (16 hex digits)")
    return os.path.join(cache_root(env), key)


def golden_path(key, env=None):
    return os.path.join(entry_dir(key, env), "golden.qcow2")


def list_images(env=None):
    """[(key, manifest)] for every complete cache entry, oldest first."""
    root = cache_root(env)
    found = []
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        if not KEY_RE.match(name):
            continue
        try:
            with open(os.path.join(root, name, "manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        if os.path.isfile(os.path.join(root, name, "golden.qcow2")):
            found.append((name, manifest))
    found.sort(key=lambda item: item[1].get("created", ""))
    return found


def verify_entry(key, env=None):
    """Raise ImageError unless the entry's golden.qcow2 is read-only and
    matches its recorded checksum."""
    path = golden_path(key, env)
    if os.stat(path).st_mode & 0o222:
        raise ImageError(f"{path} is writable; a golden image must be read-only")
    with open(os.path.join(entry_dir(key, env), "golden.sha256"), encoding="utf-8") as f:
        want = f.read().split()[0]
    if _sha256_file(path) != want:
        raise ImageError(f"{path} does not match golden.sha256; do not use it")


def images_in_use(env=None):
    """{key: [sandbox names]}. A state file names its image as "image":
    KEY (written by create, phase 3). And a Lima instance whose disk is
    an overlay uses every cache entry in its backing chain (D12), with or
    without a state file: a write to, or the removal of, that file breaks
    the instance."""
    used = {}

    def add(key, name):
        if name not in used.setdefault(key, []):
            used[key].append(name)

    sdir = state_mod.state_dir(env)
    for name in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sdir, name), encoding="utf-8") as f:
                key = json.load(f).get("image")
        except (OSError, ValueError, AttributeError):
            continue
        if key:
            add(key, name[:-len(".json")])
    root = cache_root(env)
    home = lima_mod.lima_home(env)
    for inst in sorted(os.listdir(home)) if os.path.isdir(home) else []:
        for path in disk_mod.header_chain(os.path.join(home, inst, "disk"))[1:]:
            parent = os.path.dirname(os.path.realpath(path))
            if os.path.dirname(parent) == root and KEY_RE.match(os.path.basename(parent)):
                add(os.path.basename(parent), inst[len("wmf-sbx-"):]
                    if inst.startswith("wmf-sbx-") else inst)
    # A warm snapshot keeps the golden image it is backed by (track B3).
    from . import snapshot as snapshot_mod
    for key, users in snapshot_mod.golden_keys_in_use(env).items():
        for name in users:
            add(key, name)
    return used


def remove_image(key, env=None):
    """Delete a cache entry. Refuse if a sandbox uses it."""
    users = images_in_use(env).get(key)
    if users:
        raise ImageError(f"image {key} is in use by: {', '.join(users)}")
    path = entry_dir(key, env)
    if not os.path.isdir(path):
        raise ImageError(f"no image {key} in {cache_root(env)}")
    for f in os.listdir(path):
        os.chmod(os.path.join(path, f), 0o644)
    shutil.rmtree(path)


def _created(manifest):
    try:
        return datetime.datetime.fromisoformat(manifest.get("created", ""))
    except (TypeError, ValueError):
        return None


def prune_images(keep=(), env=None, min_age=None, now=None):
    """Delete every entry that no sandbox uses and that is not in `keep`
    (`image prune`). With `min_age` (a timedelta), keep entries younger
    than that too (`image gc`; and a concurrent `create` may have built
    one and not yet recorded its sandbox). Returns [(key, bytes freed)]."""
    used = images_in_use(env)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    gone = []
    for key, manifest in list_images(env):
        if key in used or key in keep:
            continue
        if min_age is not None:
            created = _created(manifest)
            if created is None or now - created < min_age:
                continue
        size = sum(os.path.getsize(os.path.join(entry_dir(key, env), f))
                   for f in os.listdir(entry_dir(key, env)))
        remove_image(key, env)
        gone.append((key, size))
    return gone


# `image gc` (and the gc at the end of `create`) removes the unused images
# older than this; settings.yaml `images: {gcAge: ...}` changes it.
GC_AGE = "7d"
DURATION_RE = re.compile(r"^\s*(\d+)\s*([dhm]?)\s*$")


def parse_duration(text, what="a duration"):
    """'14d', '12h', '30m', or a number of days, as a timedelta."""
    m = DURATION_RE.match(str(text))
    if not m:
        raise ImageError(f"{what}: {text!r} is not a duration (14d, 12h, 30m)")
    n, unit = int(m.group(1)), m.group(2) or "d"
    unit = {"d": "days", "h": "hours", "m": "minutes"}[unit]
    return datetime.timedelta(**{unit: n})


def gc_age(config=None):
    """settings.yaml `images.gcAge`, as a timedelta."""
    images = (config or {}).get("images") or {}
    return parse_duration(images.get("gcAge", GC_AGE), "settings.yaml images.gcAge")


# A golden image older than this is rebuilt at the next `create`, with the
# same inputs (track B3): settings.yaml `images: {maxAge: ..., expire: ...}`.
MAX_AGE = "14d"
# A warm create freezes a new snapshot only when the one it started from
# is older than this (settings.yaml `images.snapshotInterval`): within it,
# the next create fetches and updates a little more, and a freeze (a stop,
# a start and a full generation written) is not worth it.
SNAPSHOT_INTERVAL = "1d"


def snapshot_interval(config=None):
    images = (config or {}).get("images") or {}
    return parse_duration(images.get("snapshotInterval", SNAPSHOT_INTERVAL),
                          "settings.yaml images.snapshotInterval")


# The input that tells a rebuilt image of the same inputs from the first
# (a new key, so a new file: a golden file is never rewritten).
EPOCH = "epoch"


def max_age(config=None):
    images = (config or {}).get("images") or {}
    return parse_duration(images.get("maxAge", MAX_AGE), "settings.yaml images.maxAge")


def expires(config=None):
    """settings.yaml `images.expire` (default true)."""
    value = ((config or {}).get("images") or {}).get("expire", True)
    if not isinstance(value, bool):
        raise ImageError(f"settings.yaml images.expire: {value!r} is not true or false")
    return value


def _base(inputs):
    return {k: v for k, v in (inputs or {}).items() if k != EPOCH}


def images_for(inputs, env=None):
    """[(key, manifest)] of the cached images built from `inputs` (any
    epoch), newest first."""
    want = _base(inputs)
    return [(k, m) for k, m in reversed(list_images(env)) if _base(m.get("inputs")) == want]


def choose_inputs(inputs, env=None, config=None, expire=None, renew=False, now=None,
                  min_left=None):
    """(inputs, note): the inputs of the golden image to use. The newest
    cached image of `inputs` while it is younger than images.maxAge (with
    at least `min_left` to go); else new inputs with an EPOCH, which
    build() makes a new image of. With `expire` False (--no-expire,
    images.expire: false) an old image stays, with a warning, unless
    `renew` (--cold). `note` is a line to show, or None."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    found = images_for(inputs, env)
    if not found:
        return inputs, None
    key, manifest = found[0]
    limit = max_age(config)
    created = _created(manifest)
    age = now - created if created else limit
    left = limit - age
    if left > (min_left or datetime.timedelta(0)):
        return manifest["inputs"], None
    days = f"{age.days} days old" if left <= datetime.timedelta(0) else \
        f"{age.days} days old, {left.total_seconds() / 3600:.0f} h before images.maxAge"
    if expire is None:
        expire = expires(config)
    if not expire and not renew:
        return manifest["inputs"], (
            f"warning: golden image {key} is {days} ({limit.days} days); it is kept "
            f"(--no-expire or images.expire: false). When it suits you, `wmf-sbx create "
            f"--cold` builds a new one, with the Debian updates since.")
    new = dict(_base(inputs))
    new[EPOCH] = now.isoformat(timespec="seconds")
    return new, f"+ golden image {key} is {days} ({limit.days} days): building a new one"


def gc_images(env=None, config=None, keep=(), now=None):
    """`image gc`: remove the warm snapshots, then the golden images, that
    no sandbox uses and that are older than gc_age. Returns [(name,
    bytes)]."""
    from . import snapshot as snapshot_mod
    gone = [(f"snapshot {k}/{g}", n) for k, g, n in
            snapshot_mod.prune(env=env, gc_age=gc_age(config), now=now)]
    return gone + prune_images(keep=keep, env=env, min_age=gc_age(config), now=now)


def prune_all(env=None):
    """`image prune`: every unused snapshot, then every unused image."""
    from . import snapshot as snapshot_mod
    gone = [(f"snapshot {k}/{g}", n) for k, g, n in snapshot_mod.prune(env=env)]
    return gone + prune_images(env=env)


# -- the builder ----------------------------------------------------------

def builder_name(key):
    return BUILDER_PREFIX + key[:8]


def extra_ca_files(env=None):
    """The absolute paths in $WMF_SBX_CA_CERTS. Each must be a file. In a
    nested sandbox, with no $WMF_SBX_CA_CERTS: $SSL_CERT_FILE (nono's CA)."""
    env = os.environ if env is None else env
    value = env.get(CA_CERTS_VAR)
    if value is None and os.path.exists(NESTED_MARKER):
        cert = env.get("SSL_CERT_FILE")
        path = session_ca_file(cert, env) if cert and os.path.isfile(cert) else None
        return [path] if path else []
    files = [os.path.abspath(os.path.expanduser(p))
             for p in (value or "").split(":") if p]
    for f in files:
        if not os.path.isfile(f):
            raise ImageError(f"{CA_CERTS_VAR}: no such file: {f}")
    return files


def _pem_certs(path):
    """The certificates in a PEM file, as {DER bytes: PEM text}, in order.
    Bundles wrap their base64 differently; the DER bytes are the same."""
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    certs = {}
    for m in PEM_CERT_RE.finditer(text):
        body = "".join(m.group().split("\n")[1:-1]).replace(" ", "")
        try:
            der = base64.b64decode("".join(body.split()), validate=True)
        except ValueError:
            continue
        certs.setdefault(der, m.group().strip())
    return certs


def session_ca_file(bundle, env=None):
    """A file with the certificates of `bundle` that SYSTEM_CA_BUNDLE does
    not have (nono's CA), or None if there are none. Its path does not
    change, so a later session's CA replaces an earlier one's for the
    inner VMs that read it at their next start."""
    certs = _pem_certs(bundle)
    system = _pem_certs(SYSTEM_CA_BUNDLE)
    extra = [pem for der, pem in certs.items() if der not in system]
    if not extra:
        return None
    path = os.path.join(os.path.dirname(cache_root(env)), "session-ca.pem")
    content = "\n".join(extra) + "\n"
    try:
        with open(path, encoding="ascii") as f:
            if f.read() == content:
                return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(content)
    os.replace(tmp, path)
    return path


def local_base(base, base_dir=None):
    """The path of a local copy of the base image, or None. Lima checks
    its digest as it does a download's."""
    path = os.path.join(base_dir or LOCAL_BASE_DIR, os.path.basename(base["location"]))
    return path if os.path.isfile(path) else None


def builder_template(inputs, ca_files=(), base_dir=None):
    """The builder's Lima config, as a dict. Closed like Kosta's template:
    plain mode, no mounts, no port forwards, no agent or X11 forwarding."""
    image = dict(inputs["base"])
    image["location"] = local_base(image, base_dir) or image["location"]
    tmpl = {
        "minimumLimaVersion": "2.0.0",
        "plain": True,
        "images": [image],
        "cpus": 4,
        "memory": "4GiB",
        "disk": BUILDER_DISK,
        "mounts": [],
        "containerd": {"system": False, "user": False},
        "portForwards": [{"guestIP": "0.0.0.0", "proto": "any", "ignore": True}],
        "ssh": {"loadDotSSHPubKeys": False, "forwardAgent": False,
                "forwardX11": False, "forwardX11Trusted": False},
        "user": {"name": BUILDER_USER, "home": f"/home/{BUILDER_USER}",
                 "uid": BUILDER_UID},
    }
    if ca_files:
        tmpl["caCerts"] = {"files": list(ca_files)}
    return tmpl


def guest_proxy_env(env=None):
    """The host's proxy variables, with a loopback proxy rewritten to the
    host as the guest sees it. Lima does this for the instance's own
    environment; `sudo` drops it, so the build passes it explicitly."""
    env = os.environ if env is None else env
    out = {}
    for var in PROXY_VARS:
        value = env.get(var)
        if value:
            # nono's proxy URL has credentials: //nono:PASSWORD@127.0.0.1:PORT
            out[var] = re.sub(r"//([^/@]*@)?(localhost|127\.0\.0\.1)([:/]|$)",
                              rf"//\g<1>{SLIRP_GATEWAY}\3", value)
    return out


def check_submodules(root=REPO_ROOT, run=subprocess.run):
    """Raise ImageError for a submodule that is not checked out.
    `git submodule foreach` skips one without a word, and the image then
    has an empty MCP server directory (RAN: image d16a1c5496fa83fe has
    none of the three)."""
    result = run(["git", "-C", root, "submodule", "status", "--recursive"],
                 capture_output=True, text=True, check=True,
                 stdin=subprocess.DEVNULL)
    missing = [line.split()[1] for line in result.stdout.splitlines()
               if line.startswith("-") and len(line.split()) > 1]
    if missing:
        raise ImageError(
            f"submodules not checked out in {root}: {', '.join(missing)}. Run "
            f"`git -C {root} submodule update --init --recursive`.")


def make_tree_tarball(dest, rev, root=REPO_ROOT, run=subprocess.run):
    """HEAD of the checkout, with the submodules at their recorded commits,
    as one tarball (no .git), as bin/wmf-claude-vm does it."""
    check_submodules(root, run=run)
    stage = tempfile.mkdtemp(prefix="wmf-sbx-tree.")
    try:
        archive = run(["git", "-C", root, "archive", "--format=tar", "HEAD"],
                      capture_output=True, check=True,
                      stdin=subprocess.DEVNULL)
        run(["tar", "-x", "-C", stage], input=archive.stdout, check=True)
        run(["git", "-C", root, "submodule", "--quiet", "foreach", "--recursive",
             f'mkdir -p "{stage}/$displaypath" && git archive --format=tar $sha1 '
             f'| tar -x -C "{stage}/$displaypath"'], check=True)
        with open(os.path.join(stage, ".wmf-claude-rev"), "w", encoding="utf-8") as f:
            f.write(rev + "\n")
        run(["tar", "--no-xattrs", "-czf", dest, "-C", stage, "."], check=True,
            env=dict(os.environ, COPYFILE_DISABLE="1"))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def stage_dir(dest, inputs, key, run=subprocess.run, root=REPO_ROOT):
    """Fill `dest` with what the build script reads: tree.tgz, helpers/
    and image.json."""
    make_tree_tarball(os.path.join(dest, "tree.tgz"), inputs["tree"], root=root, run=run)
    hdir = os.path.join(dest, "helpers")
    os.makedirs(hdir)
    for name, path in helper_files().items():
        shutil.copy(path, os.path.join(hdir, name))
    with open(os.path.join(dest, "image.json"), "w", encoding="utf-8") as f:
        json.dump({"key": key, "inputs": inputs}, f, indent=2, sort_keys=True)
        f.write("\n")
    shutil.copy(BUILD_SCRIPT, os.path.join(dest, "image-build.sh"))


def build_env(inputs, guest_stage):
    """The variables that image-build.sh reads."""
    return {
        "WMF_SBX_STAGE": guest_stage,
        "WMF_SBX_PACKAGES": " ".join(inputs["packages"]),
        "WMF_SBX_NONO_VERSION": inputs["nono"],
        "WMF_SBX_NODE_VERSION": inputs["node"]["version"],
        "WMF_SBX_NODE_FILE": inputs["node"]["file"],
        "WMF_SBX_NODE_SHA256": inputs["node"]["sha256"],
        "WMF_SBX_PHP_AST_URL": inputs["php_ast"]["url"],
        "WMF_SBX_PHP_AST_COMMIT": inputs["php_ast"]["commit"],
        "WMF_SBX_PHP_AST_VERSION": inputs["php_ast"]["version"],
        "WMF_SBX_CLAUDE_VERSION": inputs["claude"]["version"],
        "WMF_SBX_CLAUDE_PLATFORM": inputs["claude"]["platform"],
        "WMF_SBX_CLAUDE_SHA256": inputs["claude"]["sha256"],
        "WMF_SBX_TREE_REV": inputs["tree"],
        "WMF_SBX_BUILDER_USER": BUILDER_USER,
        "WMF_SBX_AGENT_UID": str(inputs["agent"]["uid"]),
        "WMF_SBX_AGENT_GID": str(inputs["agent"]["gid"]),
    }


def qemu_img_info(path, run=subprocess.run):
    result = run(["qemu-img", "info", "--output=json", path],
                 capture_output=True, text=True, check=True,
                 stdin=subprocess.DEVNULL)
    return json.loads(result.stdout)


def check_exported(info):
    """Raise ImageError unless `info` (qemu-img info JSON) is a qcow2 file
    with no backing file and no external data file."""
    if info.get("format") != "qcow2":
        raise ImageError(f"exported image is {info.get('format')!r}, not qcow2")
    for k in ("backing-filename", "full-backing-filename"):
        if info.get(k):
            raise ImageError(f"exported image has a backing file: {info[k]}")
    data = (((info.get("format-specific") or {}).get("data")) or {}).get("data-file")
    if data:
        raise ImageError(f"exported image has an external data file: {data}")


def build(inputs=None, lima=None, run=subprocess.run, env=None, log=print,
          now=None, keep_builder=False):
    """Build the image for `inputs` unless the cache has it. Returns the
    key. The entry appears all at once (a rename), so a failed build
    leaves nothing that `list_images` shows."""
    env = os.environ if env is None else env
    inputs = inputs or image_inputs(run=run, env=env)
    lima = lima or lima_mod.Limactl(run=run)
    key = cache_key(inputs)
    final = entry_dir(key, env)
    if os.path.isdir(final):
        verify_entry(key, env)
        log(f"image {key} is in the cache: {golden_path(key, env)}")
        return key

    name = builder_name(key)
    ca_files = extra_ca_files(env)
    root = cache_root(env)
    os.makedirs(root, exist_ok=True)
    work = tempfile.mkdtemp(prefix=f".build-{key}.", dir=root)
    try:
        if lima.exists(name):
            log(f"deleting a builder left by an earlier build: {name}")
            lima.delete(name)
        tmpl = os.path.join(work, "builder.yaml")
        with open(tmpl, "w", encoding="utf-8") as f:
            yaml.safe_dump(builder_template(inputs, ca_files), f, sort_keys=False)
        lima.validate(tmpl)

        log(f"==> builder {name}: create and boot (Debian {inputs['arch']})")
        try:
            lima.create(name, tmpl)
            lima.start(name, timeout=BUILDER_START_TIMEOUT)
        except lima_mod.LimaError:
            # Nothing in it to inspect yet (RAN, N3: a refused download
            # left a stopped builder).
            if lima.exists(name):
                lima.delete(name)
            raise

        log("==> staging the tree, helpers and build script")
        stage = os.path.join(work, "stage")
        os.makedirs(stage)
        stage_dir(stage, inputs, key, run=run)
        guest_stage = f"/tmp/wmf-sbx-stage.{key}"
        lima.shell(name, ["mkdir", "-p", guest_stage])
        for f in sorted(os.listdir(stage)):
            src = os.path.join(stage, f)
            lima.copy(src, f"{name}:{guest_stage}/", recursive=os.path.isdir(src))
        variables = dict(guest_proxy_env(env))
        variables.update(build_env(inputs, guest_stage))
        argv = ["sudo", "env"] + [f"{k}={v}" for k, v in variables.items()] + [
            "bash", f"{guest_stage}/image-build.sh"]

        log("==> running image-build.sh in the builder (this is the long part)")
        result = lima.call("shell", name, "--", *argv, check=False, capture=False)
        if result.returncode != 0:
            raise ImageError(f"image-build.sh failed in {name} (exit {result.returncode}); "
                             f"the builder is kept for inspection: limactl shell {name}")

        log("==> stopping the builder")
        lima.stop(name)

        log("==> exporting the disk")
        out_dir = os.path.join(work, "entry")
        os.makedirs(out_dir)
        out = os.path.join(out_dir, "golden.qcow2")
        run(["qemu-img", "convert", "-O", "qcow2", lima.disk_path(name, env), out],
            check=True)
        info = qemu_img_info(out, run=run)
        check_exported(info)
        digest = _sha256_file(out)
        os.chmod(out, 0o444)
        with open(os.path.join(out_dir, "golden.sha256"), "w", encoding="utf-8") as f:
            f.write(f"{digest}  golden.qcow2\n")
        stamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(timespec="seconds")
        with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"key": key, "created": stamp, "inputs": inputs,
                       "commit": head_commit(root=REPO_ROOT, run=run),
                       "virtual_size": info.get("virtual-size"),
                       "sha256": digest}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.rename(out_dir, final)
        log(f"image {key}: {golden_path(key, env)}")
        if not keep_builder:
            lima.delete(name)
        return key
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- the command line -----------------------------------------------------

def _allocated(path):
    """The bytes a file takes on disk (a qcow2 file is sparse)."""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return 0


def sandbox_disks(env=None):
    """The wmf-sbx sandboxes' own disks in the Lima home (an overlay's
    own blocks only; not the image builder's)."""
    home = lima_mod.lima_home(env)
    if not os.path.isdir(home):
        return []
    return [os.path.join(home, d, "disk") for d in sorted(os.listdir(home))
            if d.startswith("wmf-sbx-") and not d.startswith(BUILDER_PREFIX)
            and os.path.isfile(os.path.join(home, d, "disk"))]


def ls_totals(totals, unused):
    """The two summary lines of `image ls`."""
    def part(label, kind):
        n, size = totals[kind]
        return f"{n} {label}{'' if n == 1 else 's'} ({_human(size)})"
    all_bytes = sum(size for _n, size in totals.values())
    line = (f"total: {part('golden image', 'golden')}, {part('snapshot', 'snapshot')}, "
            f"{part('sandbox disk', 'disk')}: {_human(all_bytes)} on disk")
    if unused[0] or unused[1]:
        line += (f"\n       `wmf-sbx image prune` would free {_human(unused[2])} "
                 f"({unused[0]} image{'' if unused[0] == 1 else 's'} and {unused[1]} "
                 f"snapshot{'' if unused[1] == 1 else 's'} that no sandbox needs)")
    return line


def _human(n):
    for unit in ("B", "KiB", "MiB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    if n < 1024:
        return f"{n:.1f} GiB"
    return f"{n / 1024:.1f} TiB"


def main(argv=None, env=None, lima=None, run=subprocess.run, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="wmf-sbx image")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the golden image unless it is cached")
    b.add_argument("--dry-run", action="store_true",
                   help="print the inputs and the key; build nothing")
    b.add_argument("--keep-builder", action="store_true",
                   help="do not delete the builder VM after a good build")
    b.add_argument("--config", default=None,
                   help="the settings file, for its `packages:` (default "
                        "~/.config/wmf-sbx/settings.yaml)")
    sub.add_parser("ls", help="list the cached images")
    r = sub.add_parser("rm", help="remove one image (refused if a sandbox uses it)")
    r.add_argument("key")
    g = sub.add_parser("gc", help="remove the images that no sandbox uses and that are "
                                  f"older than settings.yaml images.gcAge ({GC_AGE})")
    g.add_argument("--config", default=None, help="the settings file")
    sub.add_parser("prune", help="remove every image that no sandbox uses (to free "
                                 "disk space)")
    args = parser.parse_args(argv)

    def say(msg):
        print(msg, file=out, flush=True)

    try:
        if args.cmd == "build":
            from . import resolve as resolve_mod
            config = resolve_mod.load_config(args.config or resolve_mod.DEFAULT_CONFIG)
            inputs = image_inputs(run=run, env=env, packages=config["packages"])
            inputs, note = choose_inputs(inputs, env=env, config=config)
            if note:
                say(note)
            if args.dry_run:
                say(json.dumps({"key": cache_key(inputs), "inputs": inputs},
                               indent=2, sort_keys=True))
                return 0
            build(inputs, lima=lima, run=run, env=env, log=say,
                  keep_builder=args.keep_builder)
        elif args.cmd == "ls":
            from . import snapshot as snapshot_mod
            used = images_in_use(env)
            totals = {"golden": [0, 0], "snapshot": [0, 0], "disk": [0, 0]}
            unused = [0, 0, 0]   # images, snapshots, bytes
            # The snapshots first: a golden image that only unused
            # snapshots keep goes too, after them, at `image prune`.
            snap_used = snapshot_mod.in_use(env)
            snap_rows, kept_keys = [], set()
            for skey in snapshot_mod.list_keys(env):
                for gen, path, meta in snapshot_mod.list_generations(skey, env):
                    users = ", ".join(snap_used.get(os.path.realpath(path), [])) or "-"
                    size = _allocated(path)
                    totals["snapshot"][0] += 1
                    totals["snapshot"][1] += size
                    if users == "-":
                        unused[1] += 1
                        unused[2] += size
                    else:
                        kept_keys.add(snapshot_mod.borrower_name(skey))
                    snap_rows.append(
                        f"snapshot {skey}/{gen}  {_human(size):>9}  "
                        f"on {meta.get('golden', '?')}  {len(meta.get('repos') or [])} "
                        f"repos (made by {meta.get('sandbox', '?')})  used by: {users}")
            snap_names = {snapshot_mod.borrower_name(k) for k in snapshot_mod.list_keys(env)}
            for key, m in list_images(env):
                size = _allocated(golden_path(key, env))
                totals["golden"][0] += 1
                totals["golden"][1] += size
                left = [u for u in used.get(key, []) if u not in snap_names or u in kept_keys]
                if not left:
                    unused[0] += 1
                    unused[2] += size
                inp = m.get("inputs", {})
                users = ", ".join(used.get(key, [])) or "-"
                tree = inp.get("tree", "?")
                if m.get("commit") and m["commit"] != tree:
                    tree += f" ({m['commit']})"
                say(f"{key}  {m.get('created', '?')}  {_human(size):>9}  "
                    f"wmf-claude {tree}  claude {inp.get('claude', {}).get('version', '?')}  "
                    f"nono {inp.get('nono', '?')}  used by: {users}")
            for row in snap_rows:
                say(row)
            for disk in sandbox_disks(env):
                totals["disk"][0] += 1
                totals["disk"][1] += _allocated(disk)
            say(ls_totals(totals, unused))
        elif args.cmd == "rm":
            remove_image(args.key, env)
            say(f"removed {args.key}")
        elif args.cmd in ("gc", "prune"):
            if args.cmd == "gc":
                from . import resolve as resolve_mod
                config = resolve_mod.load_config(args.config or resolve_mod.DEFAULT_CONFIG)
                gone = gc_images(env=env, config=config)
            else:
                gone = prune_all(env=env)
            say("removed: " + (", ".join(f"{k} ({_human(n)})" for k, n in gone)
                               if gone else "nothing"))
    except (ImageError, lima_mod.LimaError, state_mod.StateError) as e:
        print(f"wmf-sbx image: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # resolve.ResolutionError: the settings file
        if type(e).__name__ != "ResolutionError":
            raise
        print(f"wmf-sbx image: {e}", file=sys.stderr)
        return 1
    return 0
