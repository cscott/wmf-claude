# Handoff: Lima VMs on qcow2 overlays with an absolute backing file

Oct 8, 2026 · @C. Scott Ananian

## Goal and context

Goal: run many Lima VM sandboxes that share one read-only golden disk, each instance storing only its own writes in a qcow2 overlay. This is unverified on Lima v2.x; your job is to test it and report what works.

- **Why:** Lima v2.0 stopped using qcow2 backing chains, so every instance now holds a full, standalone disk. Overlays would bring per-sandbox cost down to the blocks each VM actually changes.
- **Scope:** QEMU driver only. That is the default on Linux hosts; on macOS it means setting `vmType: qemu`.
- **Status:** the facts below come from Lima's source, PRs and docs. The overlay procedure is a hypothesis assembled from them and has not been run.

## Verified facts about Lima disk handling

Since v2.0, Lima builds each instance's disk by renaming or converting `basedisk` into a standalone `diffdisk`. It never creates an overlay itself.

| Fact | Source |
| --- | --- |
| Before v2.0, `diffdisk` was a qcow2 overlay with `basedisk` as its backing file; a fresh instance used about 196 KB. | [PR #4206 discussion](https://github.com/lima-vm/lima/pull/4206) |
| v2.0 (PR #4206) makes the driver rename or convert `basedisk` to `diffdisk` immediately, except when `basedisk` is an ISO9660 image. | [PR #4206](https://github.com/lima-vm/lima/pull/4206) |
| The maintainer's stated reason: remove differencing-I/O overhead. Sharing one basedisk across instances was never implemented. | [PR #4206](https://github.com/lima-vm/lima/pull/4206) |
| Source comment: `EnsureDisk` just renames baseDisk to diffDisk unless it is ISO9660; "diffDisk" is a misnomer, a full disk since v2.0. | [PR #4206 files](https://github.com/lima-vm/lima/pull/4206/files) |
| Other code paths call `diskUtil.ConvertToRaw(baseDisk, diffDisk, ...)`; the vz driver converts qcow2 to raw. | [PR #4206 files](https://github.com/lima-vm/lima/pull/4206/files), [issue #2579](https://github.com/lima-vm/lima/issues/2579) |
| Downloaded images are cached under `~/Library/Caches/lima` on macOS, then copied into the instance directory as `basedisk`. | [Lima docs commit](https://github.com/lima-vm/lima/commit/c803fe76da6982528a508f887e6bee7a2926c165), [issue #1411](https://github.com/lima-vm/lima/issues/1411) |
| Lima's docs say plain macOS `cp` copies a disk's full virtual size; `cp -c` makes an instant APFS clonefile clone. | [Lima docs commit](https://github.com/lima-vm/lima/commit/c803fe76da6982528a508f887e6bee7a2926c165) |
| A recent proposal implements krunkit snapshots as clonefile copies of the stopped raw disk, failing rather than falling back to a full copy off-volume. | [issue #5533](https://github.com/lima-vm/lima/issues/5533) |

Instance layout: `~/.lima/<name>/` holds `diffdisk` (the boot disk), plus `basedisk` only while it is an ISO.

## Driver constraints

Only the QEMU driver can boot a qcow2 overlay; every other Lima driver needs a raw disk.

| Driver | Default on | Disk format | Overlay possible? |
| --- | --- | --- | --- |
| `qemu` | Linux hosts (with KVM) | qcow2 or raw | Yes, in principle |
| `vz` | macOS (Apple Silicon and Intel, recent macOS) | raw only; qcow2 converted on create | No |
| `krunkit` | macOS, opt-in for GPU | raw | No |
| `wsl2` | Windows | no Lima-managed disk image | No |

On macOS, set `vmType: qemu` to use overlays. Acceleration still comes from HVF, but you lose what only vz provides: Rosetta for x86 binaries, virtiofs mounts (QEMU on macOS falls back to slower mounts) and some boot and I/O speed.

## Unverified hypotheses to test

The overlay should survive Lima only if nothing in its pipeline converts the image; each step below is inferred, not observed.

1. **H1, local images are accepted:** an `images:` entry can point at a local file path, and Lima copies it into `basedisk` without converting it.
2. **H2, the copy keeps the header:** a byte copy of a qcow2 file keeps its backing-file reference. That only resolves if the path is absolute, because a relative path would be resolved against the new location in `~/.lima/<name>/`.
3. **H3, QEMU renames rather than converts:** on the QEMU driver, `EnsureDisk` renames a qcow2 `basedisk` to `diffdisk` with the header intact. The `ConvertToRaw` paths seen in the source must not apply to QEMU with qcow2 input.
4. **H4, resize is harmless:** Lima resizes `diffdisk` to the instance's `disk:` size. `qemu-img resize` on an overlay grows its virtual size without touching the backing file.
5. **H5, no digest or format check blocks it:** Lima does not reject a qcow2 with a backing file (no digest was given, and no format validation fails).
6. **H6, the boot is clean:** the guest boots, and cloud-init reruns per-instance setup because Lima's per-instance cidata supplies a new instance-id.

## Proposed procedure

Build one golden qcow2, freeze it read-only, then give each sandbox an overlay. Try attach method A first; method B is the fallback if Lima mangles the image.

**1. Build the golden image**

```bash
limactl create --name golden --set '.vmType="qemu"' template:default
limactl start golden   # provision: packages, tools, agent CLIs
# optional: seal identity (see Risks)
limactl stop golden
qemu-img info ~/.lima/golden/diffdisk        # expect: file format: qcow2, no backing file
mkdir -p /srv/lima-golden
cp ~/.lima/golden/diffdisk /srv/lima-golden/golden.qcow2
chmod a-w /srv/lima-golden/golden.qcow2
```

If `diffdisk` comes back raw, convert it with `qemu-img convert -O qcow2`.

**2. Create an overlay**

```bash
qemu-img create -f qcow2 -F qcow2 \
  -b /srv/lima-golden/golden.qcow2 /srv/lima-overlays/sbx1.qcow2
```

Recent `qemu-img` requires `-F` (the backing format), and `-b` must be absolute.

**Method A: hand the overlay to Lima as its image**

```yaml
# sbx1.yaml
vmType: qemu
images:
  - location: /srv/lima-overlays/sbx1.qcow2
    arch: aarch64   # or x86_64
mounts:
  - location: "~/work/project"
    writable: true
```

Then `limactl create --name sbx1 ./sbx1.yaml`. This tests H1 to H5 together.

**Method B: swap the disk after create**

```bash
limactl create --name sbx1 --set '.vmType="qemu"' template:default
rm ~/.lima/sbx1/diffdisk
qemu-img create -f qcow2 -F qcow2 \
  -b /srv/lima-golden/golden.qcow2 ~/.lima/sbx1/diffdisk
qemu-img resize ~/.lima/sbx1/diffdisk 100G   # match the disk: size
limactl start sbx1
```

Method B skips Lima's image download and rename, so it only depends on H4 to H6. A Lima upgrade could still change how an existing `diffdisk` is handled at start.

## Verification and success criteria

The approach works if a running instance's `diffdisk` still names the golden image as its backing file and stays small.

```bash
qemu-img info --backing-chain ~/.lima/sbx1/diffdisk
du -h ~/.lima/sbx1/diffdisk /srv/lima-golden/golden.qcow2
limactl shell sbx1 -- sh -c 'cat /etc/machine-id; hostname; df -h /'
ps aux | grep qemu-system   # check the -drive argument names diffdisk
```

- [ ] `qemu-img info` shows `backing file: /srv/lima-golden/golden.qcow2` after `limactl create` (method A) and after `limactl start`.
- [ ] `du` on a freshly started `diffdisk` is in the MB range, not GB.
- [ ] The guest boots, `limactl shell` works, and mounts and port forwarding behave as usual.
- [ ] Two overlays on the same golden image run at the same time without errors.
- [ ] Each instance has its own machine-id and SSH host keys.
- [ ] Writes in one instance don't appear in the other or in the golden image (check its checksum is unchanged).
- [ ] `limactl stop`, `start` and `delete` work, and `delete` leaves the golden image in place.

Record the Lima, QEMU and host OS versions, and which hypotheses held or failed.

## Risks, guest identity, and fallbacks

The biggest risk is silent corruption of every overlay if the golden image changes, so keep it read-only and never boot it again.

- **Golden image changes:** writing to `golden.qcow2` after overlays exist corrupts them all. Keep it `chmod a-w`, and to update it, build a new file (`golden-v2.qcow2`) for new overlays.
- **Moving the golden image:** the absolute path is baked into each overlay. To relocate it, use `qemu-img rebase -u -b <new path> -F qcow2`.
- **Duplicate identity:** clones inherit `/etc/machine-id`, SSH host keys and any random seed. Before copying the golden disk, clear them inside the guest: `truncate -s0 /etc/machine-id`, `rm /etc/ssh/ssh_host_*`, and `cloud-init clean`. The [lima-ai](https://github.com/joepreludian/lima-ai) project seals its base this way, and its clones get their own identity at first boot.
- **Lima upgrades:** the behavior relies on undocumented internals (`EnsureDisk`), so retest after each Lima upgrade.
- **Lima rewrites the disk:** if Lima converts or flattens the image under method A, use method B.

**Fallbacks if overlays fail:**

- **macOS, APFS clones:** stop a provisioned VM and `cp -c` its raw `diffdisk` into each new instance. The clones share unchanged blocks, so this works with vz.
- **Linux, reflinks:** on Btrfs or XFS, `cp --reflink=always` gives the same block sharing.
- **One VM, many containers:** run one Lima VM and give each sandbox an overlayfs-backed container. This uses the least space, but isolation drops from one kernel per sandbox to namespaces.

## Open questions and sources

Answer these from Lima's source (`pkg/driverutil`, `pkg/qemu`, `pkg/downloader`, `pkg/imgutil` in the v2 tree) before or alongside the test runs.

- [ ] Does the QEMU driver call `ConvertToRaw` in any case, or only rename? Under what conditions?
- [ ] Does the downloader accept a bare local path or `file://` URL in `images:`, and does it copy, clonefile or symlink it?
- [ ] Does Lima inspect the qcow2 header (backing file, format) and reject or flatten it?
- [ ] Is the `disk:` resize done with `qemu-img resize`, and is it safe on an overlay?
- [ ] Does `limactl clone` copy `diffdisk` with clonefile or reflink, and does it preserve an overlay header?
- [ ] Does `limactl snapshot` on QEMU use internal qcow2 snapshots, and do they behave on an overlay?

**Sources**

- [lima-vm/lima PR #4206: vz removes basedisk, renames diffdisk](https://github.com/lima-vm/lima/pull/4206)
- [PR #4206 changed files (EnsureDisk, ConvertToRaw)](https://github.com/lima-vm/lima/pull/4206/files)
- [Issue #2579: qcow2-to-raw conversion on create](https://github.com/lima-vm/lima/issues/2579)
- [Issue #1411: downloader should use clonefile](https://github.com/lima-vm/lima/issues/1411)
- [Issue #5533: krunkit snapshots via clonefile](https://github.com/lima-vm/lima/issues/5533)
- [Lima docs commit: disks, cp -c, cache directory](https://github.com/lima-vm/lima/commit/c803fe76da6982528a508f887e6bee7a2926c165)
- [lima-ai: golden base, sealing, limactl clone](https://github.com/joepreludian/lima-ai)
