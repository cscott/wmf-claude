#!/usr/bin/env python3
"""Warm snapshots (lima-port/HANDOFF-LIMA.md track B3).

A snapshot is the disk of a sandbox at the end of its `create`, before
any session ran in it: the clones, composer's vendor/ and node_modules,
the installed wiki. The next `create` of the same set of repos starts
from it, fetches and updates only what changed, and freezes the result
as the key's next generation.

Key (snapshot_key): the golden image, `--nested`, and the set of
resolved directories, each as (canonical, host path, read-only or not).
Not the flags that only change the dependency walk: the set shows what
they did, and a repo is the same full clone whether it was named or came
in as a dependency.

In the image cache, `snapshots/KEY/`:
  GEN.qcow2  the frozen disk, read-only, backed by the golden image
             directly (depth 2: golden <- snapshot <- sandbox)
  GEN.json   its metadata, written last: a generation without it is not
             one (list_generations)
  .lock      flock()ed around a freeze

The clones in a snapshot borrow objects from the host repos (`clone
--shared`), as a sandbox's do (the engineer's decision, 2026-10-13), so
every host repo lists the key as a gc borrower (`snapshot-KEY`) while
the key has a snapshot, and its gc stays off. A host repo that lacks the
entry was replaced (deleted, cloned again): a snapshot of it may name
objects that are gone, so a warm create checks the entries first
(borrowers_intact) and goes cold if one is missing.
"""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time

from . import disk as disk_mod
from . import image as image_mod
from . import lima as lima_mod
from . import remotes as remotes_mod
from . import vm as vm_mod

SUBDIR = "snapshots"
BORROWER_PREFIX = "snapshot-"
# A superseded generation that no sandbox uses is removed after this: a
# `create` that has just chosen it as its base is still booting on it.
SUPERSEDED_MIN_AGE = datetime.timedelta(hours=1)


class SnapshotError(Exception):
    pass


def snapshot_key(golden_key, repos, nested=False):
    """The key of a set of repos: `repos` is [(canonical, host path,
    read_only)]. 16 hex digits, like an image key."""
    blob = json.dumps({"golden": golden_key, "nested": bool(nested),
                       "repos": sorted([c or "", p, bool(ro)] for c, p, ro in repos)},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def root(env=None):
    return os.path.join(image_mod.cache_root(env), SUBDIR)


def key_dir(key, env=None):
    if not image_mod.KEY_RE.match(key or ""):
        raise SnapshotError(f"{key!r} is not a snapshot key (16 hex digits)")
    return os.path.join(root(env), key)


def borrower_name(key):
    return BORROWER_PREFIX + key


def new_generation(now=None):
    """A name that sorts by time: 20261013T120000Z-1a2b3c."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def list_generations(key, env=None):
    """[(gen, path, meta)] of the key's complete generations, oldest
    first."""
    d = key_dir(key, env)
    found = []
    if not os.path.isdir(d):
        return found
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        gen = name[:-len(".json")]
        path = os.path.join(d, gen + ".qcow2")
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if os.path.isfile(path):
            found.append((gen, path, meta))
    return found


def newest(key, env=None):
    """(gen, path, meta) of the key's newest generation, or None."""
    gens = list_generations(key, env)
    return gens[-1] if gens else None


def list_keys(env=None):
    r = root(env)
    if not os.path.isdir(r):
        return []
    return sorted(k for k in os.listdir(r) if image_mod.KEY_RE.match(k))


@contextlib.contextmanager
def locked(key, env=None):
    """An exclusive flock on the key, around a freeze and a removal."""
    d = key_dir(key, env)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".lock"), "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write_meta(path, meta):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# -- the host repos' gc ----------------------------------------------------

def host_dirs(meta):
    """The host repos a generation's clones borrow from."""
    return [r["path"] for r in meta.get("repos") or []]


def register_borrowers(key, dirs, run=subprocess.run, warn=None):
    """List the key as a borrower in every host repo and turn its gc off.
    Raises SnapshotError if one cannot be listed: a snapshot that a host
    gc may break must not be published."""
    for d in dirs:
        if not (remotes_mod.add_borrower(d, borrower_name(key), run=run)
                and remotes_mod.suspend_gc(d, run=run, warn=warn)):
            raise SnapshotError(f"{d}: could not list snapshot {key} as a gc borrower")


def unregister_borrowers(key, dirs, run=subprocess.run, warn=None):
    for d in dirs:
        if not os.path.isdir(d):
            continue
        remotes_mod.remove_borrower(d, borrower_name(key), run=run)
        remotes_mod.resume_gc(d, run=run, warn=warn)


def borrowers_intact(key, dirs, run=subprocess.run):
    """The host repos that do not list the key (replaced since the
    freeze, or gone): [] if the snapshot is safe to use."""
    return [d for d in dirs
            if not os.path.isdir(d)
            or borrower_name(key) not in remotes_mod.borrowers(d, run=run)]


# -- what is in use --------------------------------------------------------

def in_use(env=None):
    """{snapshot file: [sandbox names]}, from the backing chains of the
    Lima instances' disks (as image.images_in_use)."""
    used = {}
    r = os.path.realpath(root(env))
    home = lima_mod.lima_home(env)
    for inst in sorted(os.listdir(home)) if os.path.isdir(home) else []:
        for path in disk_mod.header_chain(os.path.join(home, inst, "disk"))[1:]:
            real = os.path.realpath(path)
            if real.startswith(r + os.sep):
                name = inst[len("wmf-sbx-"):] if inst.startswith("wmf-sbx-") else inst
                used.setdefault(real, []).append(name)
    return used


def golden_keys_in_use(env=None):
    """{golden key: [snapshot-KEY, ...]}: a snapshot keeps its golden
    image, whether or not a sandbox runs on it."""
    used = {}
    croot = image_mod.cache_root(env)
    for key in list_keys(env):
        for _gen, path, _meta in list_generations(key, env):
            for below in disk_mod.header_chain(path)[1:]:
                parent = os.path.dirname(os.path.realpath(below))
                if os.path.dirname(parent) == croot:
                    users = used.setdefault(os.path.basename(parent), [])
                    if borrower_name(key) not in users:
                        users.append(borrower_name(key))
    return used


# -- removal ---------------------------------------------------------------

def created(meta):
    """When the generation was frozen, or None."""
    try:
        return datetime.datetime.fromisoformat(meta.get("created", ""))
    except (TypeError, ValueError):
        return None


def _remove_generation(path):
    for p in (path[:-len(".qcow2")] + ".json", path):
        with contextlib.suppress(FileNotFoundError):
            os.chmod(p, 0o644)
            os.unlink(p)


def prune(env=None, gc_age=None, now=None, run=subprocess.run, warn=None):
    """Remove the snapshots that no sandbox uses. With `gc_age` (`image
    gc`): a key's newest generation only when it is older than that, and
    a superseded one after SUPERSEDED_MIN_AGE. Without (`image prune`):
    every unused one. A key with no generation left is unlisted as a
    borrower. Returns [(key, gen, bytes)]."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    used = in_use(env)
    gone = []
    for key in list_keys(env):
        with locked(key, env):
            gens = list_generations(key, env)
            dirs = sorted({d for _g, _p, m in gens for d in host_dirs(m)})
            for i, (gen, path, meta) in enumerate(gens):
                if os.path.realpath(path) in used:
                    continue
                if gc_age is not None:
                    made = created(meta)
                    age = now - made if made else None
                    limit = gc_age if i == len(gens) - 1 else SUPERSEDED_MIN_AGE
                    if age is None or age < limit:
                        continue
                size = os.path.getsize(path)
                _remove_generation(path)
                gone.append((key, gen, size))
            if not list_generations(key, env):
                unregister_borrowers(key, dirs, run=run, warn=warn)
                with contextlib.suppress(OSError):
                    for leftover in os.listdir(key_dir(key, env)):
                        os.unlink(os.path.join(key_dir(key, env), leftover))
                    os.rmdir(key_dir(key, env))
    return gone


# -- the freeze ------------------------------------------------------------

def seal_script():
    """Run as root in the sandbox before its disk is frozen: what makes it
    this sandbox goes, as image-build.sh seals the golden image
    (HANDOFF-LIMA.md §5.2). Each sandbox gets its machine-id and SSH host
    keys again at its next boot, from Lima's per-instance cloud-init data;
    the next `create` writes the plan, the setup log and the session files
    again."""
    home = vm_mod.AGENT_HOME
    return f"""set -eu
cd /
rm -f {home}/wmf-sbx-plan.json {home}/.wmf-sbx/wmf-sbx-setup.log \\
      {home}/.wmf-sbx/wmf-sbx-setup.status
rm -rf {home}/.wmf-sbx/timings
rm -f /etc/ssh/ssh_host_*
rm -f /etc/sudoers.d/90-cloud-init-users
rm -f /usr/local/share/ca-certificates/cloud-init-ca-cert-*.crt
cloud-init clean --logs --seed --machine-id
sync
"""


def _kept_paths():
    """What the seal removes and this sandbox still needs after its freeze:
    its plan, setup log and status, and timings (the session's CLAUDE.md
    sends the agent to them). Relative to /."""
    home = vm_mod.AGENT_HOME.lstrip("/")
    return [f"{home}/wmf-sbx-plan.json", f"{home}/.wmf-sbx"]


def keep_script():
    """Root, before the seal: the kept files as base64 of a tar.gz on
    stdout. A missing one is left out."""
    return ("cd / && tar --numeric-owner --ignore-failed-read -czf - "
            + " ".join(_kept_paths()) + " 2>/dev/null | base64 -w0\n")


RESTORE_SCRIPT = "set -eu\nbase64 -d | tar -C / --numeric-owner -xzpf -\n"


def _keep(name, lima, warn):
    res = vm_mod.shell(name, ["sudo", "bash", "-s"], lima=lima, input=keep_script(),
                       check=False)
    if res.returncode != 0 or not (res.stdout or "").strip():
        warn(f"could not save {name}'s setup log before its snapshot; "
             f"the sandbox will not have it")
        return None
    return res.stdout.strip()


def _restore(name, lima, kept, warn):
    if kept is None:
        return
    res = vm_mod.shell(name, ["sudo", "bash", "-c", RESTORE_SCRIPT], lima=lima,
                       input=kept + "\n", check=False)
    if res.returncode != 0:
        warn(f"could not put {name}'s setup log back after its snapshot")


def freeze(name, key, golden, meta, lima=None, env=None, run=subprocess.run, log=print,
           warn=None):
    """Freeze the sandbox `name`'s disk as the key's next generation, and
    put the sandbox on a new overlay on it (track B3). At the end of
    `create` only, before any session (D1). Returns the generation's path.
    On a failure the sandbox gets its own disk back, and SnapshotError."""
    lima = lima or lima_mod.Limactl()
    inst = vm_mod.instance_name(name)
    disk = lima.disk_path(inst, env)
    t0 = time.monotonic()
    log(f"+ snapshot {key}: seal and stop {name}")
    note = warn or (lambda m: print(f"warning: {m}", file=sys.stderr))
    kept = _keep(name, lima, note)
    res = vm_mod.shell(name, ["sudo", "bash", "-s"], lima=lima, input=seal_script(),
                       check=False)
    if res.returncode != 0:
        _restore(name, lima, kept, note)
        raise SnapshotError(f"sealing {name} failed (exit {res.returncode})")
    lima.stop(inst)
    moved = None
    try:
        with locked(key, env):
            register_borrowers(key, host_dirs(meta), run=run, warn=warn)
            gen = new_generation()
            dest = os.path.join(key_dir(key, env), gen + ".qcow2")
            tmp = os.path.join(key_dir(key, env), "." + gen + ".qcow2")
            shutil.move(disk, tmp)
            moved = tmp
            if os.path.realpath(disk_mod.backing_file(tmp) or "") != os.path.realpath(golden):
                log(f"+ qemu-img rebase -b {golden} (the snapshot sits on the golden "
                    f"image directly)")
                lima.rebase_disk(tmp, golden)
            os.chmod(tmp, 0o444)
            os.replace(tmp, dest)
            moved = dest
            write_meta(os.path.join(key_dir(key, env), gen + ".json"),
                       dict(meta, key=key, golden_path=golden, sandbox=name,
                            created=datetime.datetime.now(
                                datetime.timezone.utc).isoformat(timespec="seconds")))
        lima.overlay_disk(inst, dest, env=env)
    except (OSError, lima_mod.LimaError, SnapshotError) as e:
        if moved is not None and not os.path.exists(disk):
            os.chmod(moved, 0o644)
            shutil.move(moved, disk)
            with contextlib.suppress(OSError):
                os.unlink(moved[:-len(".qcow2")] + ".json")
        log(f"+ limactl start {inst} (on its own disk: the snapshot failed)")
        vm_mod.start_instance(name, lima, env=env, vm_type="qemu")
        _restore(name, lima, kept, note)
        raise SnapshotError(f"snapshot of {name} failed: {e}") from None
    log(f"+ limactl start {inst} (on a new overlay on the snapshot)")
    vm_mod.start_instance(name, lima, env=env, vm_type="qemu")
    vm_mod.check_disk(name, lima=lima, env=env, running=True)
    _restore(name, lima, kept, note)
    log(f"+ snapshot {key}: frozen in {time.monotonic() - t0:.0f} s "
        f"({os.path.getsize(dest) / 2**30:.1f} GiB)")
    return dest
