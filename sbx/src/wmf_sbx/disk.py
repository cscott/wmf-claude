#!/usr/bin/env python3
"""Copy-on-write sandbox disks.

On QEMU, a sandbox's disk is a qcow2 overlay on the golden image: it
holds only the blocks that the sandbox changes. `vz` cannot boot qcow2,
so a `vz` sandbox keeps its full copy.

Lima refuses an image with a backing file at `limactl create` (the
CVE-2023-32684 class), so `create` gives Lima a small empty placeholder
image, then puts the overlay in the place of the instance's `disk`. At
start Lima reads only the disk's virtual size, and grows it to the
template's `disk:` (verified on Lima 2.2.1). A later Lima can check at
start too; `create` warns on any other version (TESTED_LIMA).

The chain check is what makes this safe: below the instance's disk,
every file must be a read-only qcow2 file in the image cache, with no
external data file. Never accept an overlay or an image from elsewhere.
"""

import os
import struct
import subprocess

PLACEHOLDER = "placeholder.qcow2"
PLACEHOLDER_SIZE = "1G"
TESTED_LIMA = "2.2.1"
QCOW2_MAGIC = b"QFI\xfb"
# A chain deeper than this is not one that wmf-sbx makes (it makes two:
# golden <- snapshot <- sandbox).
MAX_DEPTH = 8


class DiskError(Exception):
    pass


def uses_overlay(vm_type):
    """Only the QEMU driver boots a qcow2 disk on a backing file."""
    return vm_type == "qemu"


def placeholder_path(cache_root):
    return os.path.join(cache_root, PLACEHOLDER)


def ensure_placeholder(cache_root, run=subprocess.run):
    """The shared placeholder image (an empty qcow2, no backing file),
    made once. Lima copies it into the instance at create (196 KiB)."""
    path = placeholder_path(cache_root)
    if os.path.isfile(path):
        return path
    os.makedirs(cache_root, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        run(["qemu-img", "create", "-q", "-f", "qcow2", tmp, PLACEHOLDER_SIZE],
            check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise DiskError("qemu-img is not on PATH: install QEMU's tools "
                        "(qemu-utils on Debian)") from None
    except subprocess.CalledProcessError as e:
        raise DiskError(f"could not make {path}: {(e.stderr or '').strip()}") from None
    os.chmod(tmp, 0o444)
    os.replace(tmp, path)
    return path


def backing_file(path):
    """The backing file named in a qcow2 file's header, or None (no
    backing file, or not a qcow2 file). Reads the header only: no
    qemu-img, and no lock on a running instance's disk."""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
            if len(head) < 20 or head[:4] != QCOW2_MAGIC:
                return None
            offset, size = struct.unpack(">QI", head[8:20])
            if not offset or not size or size > 1023:
                return None
            f.seek(offset)
            name = f.read(size).decode("utf-8", "replace")
    except OSError:
        return None
    if not os.path.isabs(name):
        name = os.path.join(os.path.dirname(path), name)
    return name


def header_chain(path):
    """[path, its backing file, ...] from the qcow2 headers."""
    chain = [path]
    while len(chain) <= MAX_DEPTH:
        nxt = backing_file(chain[-1])
        if nxt is None:
            break
        chain.append(nxt)
    return chain


def _under(path, root):
    root = os.path.realpath(root)
    return os.path.realpath(path).startswith(root + os.sep)


def check_chain(infos, disk, cache_root):
    """Raise DiskError unless `infos` (`qemu-img info --backing-chain
    --output=json` of `disk`) is the instance's disk on read-only qcow2
    files in the image cache. Returns the backing files, top first."""
    if not isinstance(infos, list) or not infos:
        raise DiskError(f"{disk}: no qemu-img information")
    if os.path.realpath(infos[0].get("filename", "")) != os.path.realpath(disk):
        raise DiskError(f"{disk}: qemu-img reported {infos[0].get('filename')!r}")
    if len(infos) < 2:
        raise DiskError(f"{disk} has no backing file: it is not an overlay on the golden "
                        f"image (a flattened disk is refused)")
    if len(infos) > MAX_DEPTH + 1:
        raise DiskError(f"{disk}: the backing chain is {len(infos) - 1} files deep")
    for i, info in enumerate(infos):
        name = info.get("filename", "")
        if info.get("format") != "qcow2":
            raise DiskError(f"{name} is {info.get('format')!r}, not qcow2")
        data = (((info.get("format-specific") or {}).get("data")) or {}).get("data-file")
        if data:
            raise DiskError(f"{name} has an external data file: {data}")
        below = info.get("full-backing-filename") or info.get("backing-filename")
        if i + 1 < len(infos):
            if not below or os.path.realpath(below) != os.path.realpath(
                    infos[i + 1].get("filename", "")):
                raise DiskError(f"{name}: its backing file {below!r} is not the next file "
                                f"in the chain")
        elif below:
            raise DiskError(f"{name}: its backing file {below!r} is not in the chain")
        if i == 0:
            continue
        if not _under(name, cache_root):
            raise DiskError(f"{disk} is backed by {name}, which is not in the image cache "
                            f"({cache_root})")
        try:
            mode = os.stat(name).st_mode
        except OSError as e:
            raise DiskError(f"{name}: {e}") from None
        if mode & 0o222:
            raise DiskError(f"{name} is writable; a file below an overlay must be read-only")
    return [info["filename"] for info in infos[1:]]


def lima_version_warning(version):
    """A warning if method B is untested on this Lima, else None."""
    if version == TESTED_LIMA:
        return None
    return (f"warning: Lima {version or '(unknown)'}: copy-on-write disks are tested "
            f"with Lima {TESTED_LIMA} only (they rely on Lima not checking a disk's "
            f"backing file at start). If `create` or `start` fails, use --full-disk "
            f"or Lima {TESTED_LIMA}.")
