# Handoff: Lima VMs on qcow2 overlays with an absolute backing file

Oct 8, 2026 · @C. Scott Ananian

## Goal and context

Goal: run many Lima VM sandboxes that share one read-only golden disk, each instance storing only its own writes in a qcow2 overlay.

- **Why:** Lima v2.0 stopped using qcow2 backing chains, so every instance now holds a full, standalone disk. Overlays would bring per-sandbox cost down to the blocks each VM actually changes.
- **Scope:** QEMU driver only. That is the default on Linux hosts; on macOS it means setting `vmType: qemu`.
- **Status (updated 2026-10-08):** tested on Linux. **Method A is impossible on Lima v2.2.1; method B works** and passes all seven success checks. The source questions are answered from the Lima `v2.2.1` tree (commit `27bc4c4b`). Still to do: macOS with `vmType: qemu`, and real times with KVM or HVF.

**Test environment.** Lima 2.2.1, QEMU 8.2.2, Ubuntu 24.04 x86_64 host on ext4, in a cloud sandbox with **no KVM**, so QEMU used TCG (software emulation, about 20 times slower; compare the times only with each other). Golden image built from the Ubuntu 24.04 minimal cloud image (cloud-init 26.1, kernel 6.8), not `template:default`; the disk behaviour does not depend on the distro.

## Verified facts about Lima disk handling

Since v2.0, Lima builds each instance's disk by renaming or converting the downloaded image into a standalone disk. It never creates an overlay itself.

**File names changed after the PR below.** In v2.2.1 the downloaded image is `~/.lima/<name>/image` until `EnsureDisk` renames it to `disk` (or to `iso` for an ISO). `diffdisk` and `basedisk` are legacy names that Lima migrates at start. This note uses the new names, except where it quotes the PR.

| Fact | Source |
| --- | --- |
| Before v2.0, `diffdisk` was a qcow2 overlay with `basedisk` as its backing file; a fresh instance used about 196 KB. | [PR #4206 discussion](https://github.com/lima-vm/lima/pull/4206) |
| v2.0 (PR #4206) makes the driver rename or convert `basedisk` to `diffdisk` immediately, except when `basedisk` is an ISO9660 image. | [PR #4206](https://github.com/lima-vm/lima/pull/4206) |
| The maintainer's stated reason: remove differencing-I/O overhead. Sharing one basedisk across instances was never implemented. | [PR #4206](https://github.com/lima-vm/lima/pull/4206) |
| Other code paths convert to raw; the vz driver converts qcow2 to raw. | [PR #4206 files](https://github.com/lima-vm/lima/pull/4206/files), [issue #2579](https://github.com/lima-vm/lima/issues/2579) |
| v2.2.1 names: `image` (downloaded), `disk` (boot disk), `iso`; `basedisk`/`diffdisk` are legacy. A legacy `basedisk` "may remain as qcow2 backing file". | `pkg/limatype/filenames/filenames.go:39-44` (v2.2.1) |
| v2.2.1 QEMU `EnsureDisk` renames `image` to `disk` and never converts. It first calls `AcceptableAsBaseDisk`, which **rejects a backing file**, a qcow2 external data file, or VMDK extents. | `pkg/driver/qemu/qemu.go:94`, `pkg/qemuimgutil/qemuimgutil.go:246-295` (v2.2.1) |
| The start path checks nothing about backing files: `prepareDisk` reads only the virtual size, then resizes with `qemu-img resize` if it differs. | `pkg/instance/start.go:511-548` (v2.2.1) |
| Downloaded images are cached under `~/Library/Caches/lima` on macOS, then copied into the instance directory. | [Lima docs commit](https://github.com/lima-vm/lima/commit/c803fe76da6982528a508f887e6bee7a2926c165), [issue #1411](https://github.com/lima-vm/lima/issues/1411) |
| Lima's docs say plain macOS `cp` copies a disk's full virtual size; `cp -c` makes an instant APFS clonefile clone. | [Lima docs commit](https://github.com/lima-vm/lima/commit/c803fe76da6982528a508f887e6bee7a2926c165) |
| A recent proposal implements krunkit snapshots as clonefile copies of the stopped raw disk, failing rather than falling back to a full copy off-volume. | [issue #5533](https://github.com/lima-vm/lima/issues/5533) |

Instance layout (v2.2.1): `~/.lima/<name>/` holds `disk` (the boot disk), plus `iso` only for an ISO image.

## Driver constraints

Only the QEMU driver can boot a qcow2 overlay; every other Lima driver needs a raw disk.

| Driver | Default on | Disk format | Overlay possible? |
| --- | --- | --- | --- |
| `qemu` | Linux hosts (with or without KVM; v2.2.1 falls back to TCG) | qcow2 or raw | **Yes, by method B** (tested) |
| `vz` | macOS (Apple Silicon and Intel, recent macOS) | raw only; qcow2 converted on create | No |
| `krunkit` | macOS, opt-in for GPU | raw | No |
| `wsl2` | Windows | no Lima-managed disk image | No |

On macOS, set `vmType: qemu` to use overlays. Acceleration still comes from HVF, but you lose what only vz provides: Rosetta for x86 binaries, virtiofs mounts (QEMU on macOS falls back to slower mounts) and some boot and I/O speed.

## Hypotheses and results

1. **H1, local images are accepted: held.** An `images:` entry can be a bare absolute path or an absolute `file://` URL. Lima copies it into `image` with `continuity/fs.CopyFile` (decompressing first if the extension or magic says so) and never converts or symlinks it. A digest is optional. (READ `pkg/downloader/downloader.go:625-680`; RAN.)
2. **H2, the copy keeps the header: held.** The copied `image` (196,928 bytes) still named `/srv/lima-golden/golden.qcow2`. (RAN.)
3. **H3, QEMU renames rather than converts: held**, but moot: the check in H5 runs first. (READ.)
4. **H4, resize is harmless: held.** Lima logged "Resize instance sbx1's disk from 20GiB to 30GiB", ran `qemu-img resize -f qcow2`, and the backing file stayed. The guest root grew to 29 G at first boot. Lima refuses to shrink. (READ, RAN.)
5. **H5, no check blocks it: failed for method A.** `create` and `start` both fail with ``file `…/image` is not acceptable as a disk image: image (`…/image`) must not have a backing file (`/srv/lima-golden/golden.qcow2`)``. The check is deliberate: it closes the CVE-2023-32684 class (a disk image that reads host files through its backing path). It does not apply to method B, because it runs only in `EnsureDisk`. (READ, RAN.)
6. **H6, the boot is clean: held.** Both overlays booted, and each got its own machine-id and SSH host keys from a sealed golden image. (RAN.)

## Procedure (as tested)

Build one golden qcow2, freeze it read-only, then give each sandbox an overlay by method B. Method A is recorded for reference only.

**1. Build the golden image**

```bash
limactl create --name golden --set '.vmType="qemu"' template:default
limactl start golden   # provision: packages, tools, agent CLIs
# seal identity, inside the guest (RAN, cloud-init 26.1):
limactl shell golden -- sudo rm -f /etc/ssh/ssh_host_*
limactl shell golden -- sudo cloud-init clean --logs --seed --machine-id
limactl stop golden
qemu-img info ~/.lima/golden/disk           # expect: file format: qcow2, no backing file
mkdir -p /srv/lima-golden
cp --sparse=always ~/.lima/golden/disk /srv/lima-golden/golden.qcow2
chmod a-w /srv/lima-golden/golden.qcow2
sha256sum /srv/lima-golden/golden.qcow2 > /srv/lima-golden/golden.sha256
```

- `cloud-init clean --machine-id` writes `uninitialized` to `/etc/machine-id`; systemd makes a new one at next boot. `cloud-init clean` does **not** remove SSH host keys, so remove them yourself (`--configs ssh_config` removes the sshd config drop-in, not the keys).
- On QEMU, `disk` stays qcow2 when the base image is qcow2 (RAN: 20 GiB virtual, 611 MiB on disk). If it comes back raw, convert it with `qemu-img convert -O qcow2`.

**2. Method A: hand the overlay to Lima as its image. Does not work.**

```yaml
# sbxA.yaml
vmType: qemu
images:
  - location: /srv/lima-overlays/sbxA.qcow2   # overlay on the golden image
    arch: x86_64
```

`limactl create --name sbxA ./sbxA.yaml` copies the overlay (header intact), then fails in `EnsureDisk` with "must not have a backing file" (H5). Do not use it.

**3. Method B: swap the disk after create, with a placeholder image**

`limactl create` copies the `images:` file into the instance. Give it a tiny empty placeholder instead of the golden image, so that `create` does not copy the golden file only for us to delete it:

```bash
qemu-img create -q -f qcow2 ~/placeholder.qcow2 1G     # once; 196 KiB, no backing file
limactl create --tty=false --name sbx1 ./sbx1.yaml     # images: location: ~/placeholder.qcow2
rm ~/.lima/sbx1/disk
qemu-img create -q -f qcow2 -F qcow2 \
  -b /srv/lima-golden/golden.qcow2 ~/.lima/sbx1/disk  # optional: final size as last arg
limactl start sbx1                                     # Lima resizes disk to disk: (H4)
```

Recent `qemu-img` requires `-F` (the backing format), and `-b` must be absolute. Method B depends only on H4 and H6, and on Lima checking backing files at create time only. A Lima upgrade could add the check to the start path.

## Verification and success criteria

The approach works if a running instance's `disk` still names the golden image as its backing file and stays small.

```bash
qemu-img info -U --backing-chain ~/.lima/sbx1/disk   # -U: the running QEMU holds the lock
du -h ~/.lima/sbx1/disk /srv/lima-golden/golden.qcow2
limactl shell sbx1 -- sh -c 'cat /etc/machine-id; hostname; df -h /'
ps aux | grep qemu-system   # the -drive argument names disk, with no format=
```

Method B results (RAN, two instances `sbx1` and `sbx2`, each 2 vCPU, 2 GiB, `disk: 30GiB`, one read-only 9p mount):

| Step | sbx1 | sbx2 |
| --- | --- | --- |
| `limactl create` | 0.07 s | 0.06 s |
| overlay swap | 0.013 s | 0.012 s |
| `disk` after swap | 196 KiB | 196 KiB |
| start, both at once (TCG) | 148 s | 148 s |
| `disk` after first boot | 20 MiB | 20 MiB |
| `disk` after a 200 MiB write | 227 MiB | 220 MiB |

- [x] `qemu-img info` shows `backing file: /srv/lima-golden/golden.qcow2` after create, after start, and after a stop and start. (Method A: fails at create; see H5.)
- [x] `du` on a freshly started `disk` is in the MB range (20 MiB), not GB.
- [x] The guest boots, `limactl shell` works, a read-only 9p mount works, and port forwarding works.
- [x] Two overlays on the same golden image run at the same time without errors.
- [x] Each instance has its own machine-id and SSH host keys (`158febdb…`, `N+fknkOC…` and `cb560f5f…`, `gGR1wP/z…`).
- [x] Writes in one instance don't appear in the other or in the golden image: `/etc/written-by` differs after both wrote it, and the golden SHA-256 did not change after all the tests.
- [x] `limactl stop`, `start` and `delete` work, and `delete` leaves the golden image in place.

Recorded versions: Lima 2.2.1, QEMU 8.2.2 (Ubuntu `1:8.2.2+ds-0ubuntu1.18`), Ubuntu 24.04.5 host, x86_64, no KVM.

## Risks, guest identity, and fallbacks

The biggest risk is silent corruption of every overlay if the golden image changes, so keep it read-only and never boot it again.

- **Golden image changes:** writing to `golden.qcow2` after overlays exist corrupts them all. Keep it `chmod a-w`, and to update it, build a new file (`golden-v2.qcow2`) for new overlays.
- **Moving the golden image:** the absolute path is baked into each overlay. To relocate it, use `qemu-img rebase -u -b <new path> -F qcow2`.
- **Duplicate identity:** clones inherit `/etc/machine-id`, SSH host keys and any random seed. Before copying the golden disk, seal it as in step 1. The [lima-ai](https://github.com/joepreludian/lima-ai) project seals its base the same way, and its clones get their own identity at first boot.
- **Lima upgrades:** the behaviour relies on undocumented internals: the backing-file check is in `EnsureDisk` (create) and not in the start path. Retest after each Lima upgrade.
- **Going around a security check:** method B skips the check that Lima added against malicious backing paths. That is safe only while our code makes the overlay and our builder makes the golden image. Never accept an overlay or a golden image from outside.

**Fallbacks if overlays fail:**

- **macOS, APFS clones:** stop a provisioned VM and `cp -c` its raw `disk` into each new instance. The clones share unchanged blocks, so this works with vz.
- **Linux, reflinks:** on Btrfs or XFS, `cp --reflink=always` gives the same block sharing.
- **One VM, many containers:** run one Lima VM and give each sandbox an overlayfs-backed container. This uses the least space, but isolation drops from one kernel per sandbox to namespaces.

## Source questions, answered (Lima v2.2.1)

- [x] **Does the QEMU driver call `ConvertToRaw` in any case, or only rename?** Only rename. For an ISO it makes a new empty qcow2 `disk` with `qemu-img create`. Conversion is only in the generic `driverutil.EnsureDisk` that the other drivers use, and only when the format differs from the driver's. (`pkg/driver/qemu/qemu.go:94`, `pkg/driverutil/disk.go:63`.)
- [x] **Does the downloader accept a bare local path or `file://` URL in `images:`, and does it copy, clonefile or symlink it?** Both are accepted (`file://` must be absolute). It copies with `continuity/fs.CopyFile`, after decompressing if needed. It never symlinks. (`pkg/downloader/downloader.go:625-680`.)
- [x] **Does Lima inspect the qcow2 header (backing file, format) and reject or flatten it?** It rejects, never flattens, and only at create (`EnsureDisk`). An unknown format only gets a warning. (`pkg/qemuimgutil/qemuimgutil.go:246-295`.)
- [x] **Is the `disk:` resize done with `qemu-img resize`, and is it safe on an overlay?** Yes, `qemu-img resize -f <format> disk <bytes>` at start when the size differs, falling back to `os.Truncate` only if `qemu-img` is missing. Safe on an overlay (RAN). (`pkg/qemuimgutil/qemuimgutil.go:54`, `pkg/instance/start.go:546`.)
- [x] **Does `limactl clone` copy `disk` with clonefile or reflink, and does it preserve an overlay header?** It copies every instance file with `continuity/fs.CopyFile`, which "attempts copy-on-write when supported by the filesystem". The header stays: a clone of a stopped overlay instance still named the golden file (RAN). On ext4 it was a full byte copy of the overlay (220 MiB). clonefile and reflink are not tested. (`pkg/instance/clone.go:90`.)
- [x] **Does `limactl snapshot` on QEMU use internal qcow2 snapshots, and do they behave on an overlay?** Yes: stopped, `qemu-img snapshot -c/-a/-d`; running, HMP `savevm`. Both are stored in the overlay, not the golden file. RAN: an offline snapshot, and an online one that saved 648 MiB of VM state into the overlay. `snapshot apply` is not tested. The command is marked experimental. (`pkg/driver/qemu/qemu.go:175-230`.)

**Sources**

- [lima-vm/lima PR #4206: vz removes basedisk, renames diffdisk](https://github.com/lima-vm/lima/pull/4206)
- [PR #4206 changed files (EnsureDisk, ConvertToRaw)](https://github.com/lima-vm/lima/pull/4206/files)
- [Issue #2579: qcow2-to-raw conversion on create](https://github.com/lima-vm/lima/issues/2579)
- [Issue #1411: downloader should use clonefile](https://github.com/lima-vm/lima/issues/1411)
- [Issue #5533: krunkit snapshots via clonefile](https://github.com/lima-vm/lima/issues/5533)
- [Lima docs commit: disks, cp -c, cache directory](https://github.com/lima-vm/lima/commit/c803fe76da6982528a508f887e6bee7a2926c165)
- [lima-ai: golden base, sealing, limactl clone](https://github.com/joepreludian/lima-ai)
- Lima `v2.2.1` source, commit `27bc4c4b`: `pkg/driver/qemu`, `pkg/driverutil`, `pkg/qemuimgutil`, `pkg/downloader`, `pkg/instance`, `pkg/limatype/filenames`.
