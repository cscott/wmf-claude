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

`wmf-sbx image build|ls|rm|prune` calls `main()`.
"""

import argparse
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
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when yaml is absent
    yaml = None

from . import kit
from . import lima as lima_mod
from . import state as state_mod

SBX_ROOT = kit.SBX_ROOT
REPO_ROOT = kit.REPO_ROOT

# Kosta's template pins the base image (a versioned Debian 13 genericcloud
# release, with sha512 digests). Read it from there, so that there is one
# pin for both modes.
BASE_TEMPLATE = os.path.join(REPO_ROOT, "lima", "wmf-claude.yaml")
BUILD_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            "image-build.sh")
NONO_VERSION_FILE = os.path.join(REPO_ROOT, ".nono-version")

CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"
# `stable`, not `latest`: the image is shared by every sandbox that has the
# same key, so it takes the release channel that has had more use.
CLAUDE_CHANNEL = "stable"

# The packages, after kit.BASE_PACKAGES (the PHP side, unchanged from the
# Docker kit):
# - what the wmf-claude build and bin/claude need (lima/wmf-claude.yaml);
# - Node from Debian (Node 22 is what MediaWiki CI uses);
# - nftables, for the host block (§5.3);
# - unzip and zstd, for the helpers;
# - the shared libraries that Chrome for Testing needs on Debian 13
#   (`ldd`, RAN 2026-10-09). Contained sandboxes cannot apt-get, so
#   mw-install-browser must find them already here (§5.2).
IMAGE_PACKAGES = list(kit.BASE_PACKAGES) + [
    "ca-certificates", "curl", "git", "jq", "python3", "python3-venv",
    "nodejs", "npm", "nftables", "unzip", "zstd",
    "libnss3", "libnspr4", "libatk1.0-0t64", "libatk-bridge2.0-0t64",
    "libx11-6", "libxcomposite1", "libxdamage1", "libxext6", "libxfixes3",
    "libxrandr2", "libgbm1", "libxcb1", "libxkbcommon0", "libasound2t64",
    "libatspi2.0-0t64",
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


class ImageError(Exception):
    """A user-facing failure in an image operation."""


# -- inputs and the cache key ---------------------------------------------

def host_arch():
    """Lima's name for this machine's architecture."""
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def claude_platform(arch):
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


def claude_release(arch, fetch=_fetch, channel=CLAUDE_CHANNEL):
    """(version, sha256) of the Claude Code release on `channel` for
    `arch`, from the release manifest."""
    version = fetch(f"{CLAUDE_RELEASES}/{channel}").strip()
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        raise ImageError(f"unexpected Claude Code version {version!r} on {channel}")
    manifest = json.loads(fetch(f"{CLAUDE_RELEASES}/{version}/manifest.json"))
    plat = claude_platform(arch)
    checksum = ((manifest.get("platforms") or {}).get(plat) or {}).get("checksum", "")
    if not re.match(r"^[0-9a-f]{64}$", checksum):
        raise ImageError(f"no checksum for {plat} in the {version} manifest")
    return version, checksum


def tree_revision(root=REPO_ROOT, run=subprocess.run):
    """The commit whose tree goes into the image. The tree comes from
    `git archive HEAD`, so uncommitted changes are not in it."""
    result = run(["git", "-C", root, "rev-parse", "--short=12", "HEAD"],
                 capture_output=True, text=True, check=True)
    return result.stdout.strip()


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


def image_inputs(arch=None, fetch=_fetch, run=subprocess.run, root=REPO_ROOT,
                 uid=None, gid=None):
    """Everything that goes into the image (HANDOFF-LIMA.md §5.1)."""
    arch = arch or host_arch()
    version, checksum = claude_release(arch, fetch=fetch)
    return {
        "agent": agent_ids(uid, gid),
        "schema": 1,
        "arch": arch,
        "base": base_image(arch),
        "packages": list(IMAGE_PACKAGES),
        "nono": nono_version(),
        "claude": {"version": version, "platform": claude_platform(arch),
                   "sha256": checksum},
        "tree": tree_revision(root, run=run),
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
    """{key: [sandbox names]} from the sandbox state files. A state file
    names its image as "image": KEY (written by create, phase 3)."""
    used = {}
    sdir = state_mod.state_dir(env)
    if not os.path.isdir(sdir):
        return used
    for name in sorted(os.listdir(sdir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sdir, name), encoding="utf-8") as f:
                key = json.load(f).get("image")
        except (OSError, ValueError, AttributeError):
            continue
        if key:
            used.setdefault(key, []).append(name[:-len(".json")])
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


def prune_images(keep=(), env=None):
    """Delete every entry that no sandbox uses and that is not in `keep`.
    Returns the keys it deleted."""
    used = images_in_use(env)
    gone = []
    for key, _manifest in list_images(env):
        if key in used or key in keep:
            continue
        remove_image(key, env)
        gone.append(key)
    return gone


# -- the builder ----------------------------------------------------------

def builder_name(key):
    return BUILDER_PREFIX + key[:8]


def extra_ca_files(env=None):
    """The absolute paths in $WMF_SBX_CA_CERTS. Each must be a file."""
    env = os.environ if env is None else env
    files = [os.path.abspath(os.path.expanduser(p))
             for p in (env.get(CA_CERTS_VAR) or "").split(":") if p]
    for f in files:
        if not os.path.isfile(f):
            raise ImageError(f"{CA_CERTS_VAR}: no such file: {f}")
    return files


def builder_template(inputs, ca_files=()):
    """The builder's Lima config, as a dict. Closed like Kosta's template:
    plain mode, no mounts, no port forwards, no agent or X11 forwarding."""
    tmpl = {
        "minimumLimaVersion": "2.0.0",
        "plain": True,
        "images": [dict(inputs["base"])],
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
            out[var] = re.sub(r"//(localhost|127\.0\.0\.1)([:/]|$)",
                              rf"//{SLIRP_GATEWAY}\2", value)
    return out


def make_tree_tarball(dest, rev, root=REPO_ROOT, run=subprocess.run):
    """HEAD of the checkout, with the submodules at their recorded commits,
    as one tarball (no .git), as bin/wmf-claude-vm does it."""
    stage = tempfile.mkdtemp(prefix="wmf-sbx-tree.")
    try:
        archive = run(["git", "-C", root, "archive", "--format=tar", "HEAD"],
                      capture_output=True, check=True)
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
                 capture_output=True, text=True, check=True)
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
    inputs = inputs or image_inputs(run=run)
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
        lima.create(name, tmpl)
        lima.start(name, timeout=BUILDER_START_TIMEOUT)

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

def _human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def main(argv=None, env=None, lima=None, run=subprocess.run, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="wmf-sbx image")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the golden image unless it is cached")
    b.add_argument("--dry-run", action="store_true",
                   help="print the inputs and the key; build nothing")
    b.add_argument("--keep-builder", action="store_true",
                   help="do not delete the builder VM after a good build")
    sub.add_parser("ls", help="list the cached images")
    r = sub.add_parser("rm", help="remove one image (refused if a sandbox uses it)")
    r.add_argument("key")
    sub.add_parser("prune", help="remove every image that no sandbox uses")
    args = parser.parse_args(argv)

    def say(msg):
        print(msg, file=out, flush=True)

    try:
        if args.cmd == "build":
            inputs = image_inputs(run=run)
            if args.dry_run:
                say(json.dumps({"key": cache_key(inputs), "inputs": inputs},
                               indent=2, sort_keys=True))
                return 0
            build(inputs, lima=lima, run=run, env=env, log=say,
                  keep_builder=args.keep_builder)
        elif args.cmd == "ls":
            used = images_in_use(env)
            for key, m in list_images(env):
                size = os.path.getsize(golden_path(key, env))
                inp = m.get("inputs", {})
                users = ", ".join(used.get(key, [])) or "-"
                say(f"{key}  {m.get('created', '?')}  {_human(size):>9}  "
                    f"wmf-claude {inp.get('tree', '?')}  claude {inp.get('claude', {}).get('version', '?')}  "
                    f"nono {inp.get('nono', '?')}  used by: {users}")
        elif args.cmd == "rm":
            remove_image(args.key, env)
            say(f"removed {args.key}")
        elif args.cmd == "prune":
            gone = prune_images(env=env)
            say("removed: " + (", ".join(gone) if gone else "nothing"))
    except (ImageError, lima_mod.LimaError, state_mod.StateError) as e:
        print(f"wmf-sbx image: {e}", file=sys.stderr)
        return 1
    return 0
