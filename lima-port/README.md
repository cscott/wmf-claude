# lima-port: start here

For: the Claude session that ports `wmf-sbx` from Docker Sandboxes (sbx) to
Lima VMs. Written 2026-10-08 by the session in sandbox `wmf-claude-sbx`,
on branch `work/cscott/sbx` (this directory was added on top of `ae82db0`).

This directory is temporary. It carries the porting brief into your
sandbox. Delete it, or move its content into `sbx/`, when the port lands.

## The task documents

- [`HANDOFF-LIMA.md`](HANDOFF-LIMA.md) — the port itself: decisions
  D1–D12, the new modules (`git-remote-wmfsbx`, `image.py`, `template.py`,
  `lima.py`, `helpers/wmf-sbx-qemu`, `proxy.py`, `disk.py`), and the order
  of work (§11: phases 0–7 are the `--no-sudo` MVP; tracks A (`--sudo` on
  QEMU) and B (qcow2 copy-on-write disks) come after it). Read this first.
- [`HANDOFF-LIMA-QCOW2-OVERLAYS.md`](HANDOFF-LIMA-QCOW2-OVERLAYS.md) —
  the evidence and test procedure behind D12 (qcow2 overlays on an
  immutable golden image). Read it before track B.

Both were copied unchanged from untracked files in the engineer's host
checkout. They refer to each other by bare file name, which still works
here.

Phase 1 rebases onto Kosta's upstream MR !127
(<https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/127>),
which adds `lima/wmf-claude.yaml`, `lima/guest-install.sh`,
`lima/guest-claude.sh`, `bin/wmf-claude-vm`, `tests/test-lima.sh` and
`docs/lima-vm.md`.

Other open upstream work that a rebase must respect is in
`sbx/NOTES.md` §104 (MRs !130, !131, !132, and what to change when each
merges).

## How your sandbox was made

The previous sandbox bind-mounted the engineer's whole host layout
(`core`, `Skins`, `Extensions`, `Parsoid`, `mediawiki-config`,
`integration-config`, `docs.docker.com`). Core's `skins/MinervaNeue`,
`skins/MonoBook` and `skins/Vector` there were symlinks to host paths
that were not mounted, so `composer phpunit:entrypoint` failed before
PHPUnit started. The port needs none of that layout.

The suggested command for this sandbox was:

```bash
wmf-sbx create --dry-run --name wmf-lima \
  ~/Projects/Wikimedia/wmf-claude \
  gerrit:mediawiki/extensions/Cite \
  gerrit:mediawiki/extensions/Translate
# then the same without --dry-run
```

- wmf-claude is the primary workspace and keeps the host branch.
- The dependency walk adds core, Parsoid, Vector, and Translate's
  dependencies (UniversalLanguageSelector and its suggests) as real
  clones, not host symlinks. `--no-suggests` was the fallback if that
  list grew too long.
- Cite and Translate give a working wiki, so the `run-tests` path
  (and upstream MR !130) can run live, and give real repos to design
  `create Translate` against (HANDOFF-LIMA.md phase 5).

The engineer may have changed the command. `~/.claude/CLAUDE.md` and
`ls "$MW_INSTALL_PATH"/extensions "$MW_INSTALL_PATH"/skins` show what you
really have.

## Steps the engineer was asked to do on the host first

1. **The handoff docs.** Done by committing this directory on
   `work/cscott/sbx`. If `lima-port/` is missing in your clone, ask the
   engineer to `wmf-sbx cp` it in.
2. **Kosta's !127.** Optional. Fetching it on the host makes it reachable
   through the `local` remote:
   ```bash
   git -C ~/Projects/Wikimedia/wmf-claude fetch origin refs/merge-requests/127/head:kosta/lima-127
   ```
   Check with `git fetch local` and `git branch -r | grep lima`. If it is
   not there, `origin` in this repo is GitLab (not Gerrit), and GET is
   allowed, so fetch it yourself:
   `git fetch origin refs/merge-requests/127/head:kosta/lima-127`.
3. **MCP servers.** The host `/usr/bin/node` was v18.19.1, below the
   v20.18.1 that the Phabricator MCP server needs, so `wmf-sbx create`
   refuses. The engineer either put Node 22 on PATH or passed `--no-mcp`.
   If the Phabricator/Gerrit tools are absent, that is why; use the web
   UI.
4. **Docs access (optional).** For the Lima and QEMU docs, the engineer
   may have run:
   ```bash
   sbx policy allow network lima-vm.io,github.com,raw.githubusercontent.com,www.qemu.org
   ```
   A 403 from those hosts means this was not done. Ask; do not work
   around it.

## What this sandbox cannot do

It cannot run Lima or QEMU: that needs nested virtualisation inside the
sbx microVM. So phases 0, A1 and B1 (the measurements on real VMs) run on
the host. Write the exact commands for the engineer to run, and ask them
to paste the output back.

Measured in `wmf-claude-sbx` on 2026-10-08 (Ubuntu 26.04 guest, kernel
7.0.12, 16 vCPU):

- No `/dev/kvm`, and no `vmx`/`svm` CPU flag (only `hypervisor`). The sbx
  microVM does not expose nested virtualisation, so KVM is not possible.
  `qemu-system-x86_64 -accel kvm` fails with "Could not access KVM kernel
  module".
- QEMU 10.2.1 (apt) with `-accel tcg` works. An Ubuntu 7.0 kernel with a
  busybox initramfs reaches userspace in about 4 s of wall-clock time.
  CPU-bound work is about 21 times slower than native (a 2 GB sha256: 30 s
  in the guest, 1.4 s native). Expect a full Lima Ubuntu boot (systemd,
  cloud-init) to take minutes, not seconds.
- Lima 2.2.1 (GitHub release tarball, unpacked in the scratchpad) runs
  a full Ubuntu 26.04 VM here under TCG. Lima logs "/dev/kvm is not
  available. Disabling KVM. Expect very poor performance" and starts
  QEMU with `-accel tcg,thread=multi`. Measured with `--cpus=2
  --memory=2 --disk=20 --vm-type=qemu template:default`:
  - first `limactl start`: about 6 min (guest boot 4 min 37 s, almost all
    in userspace: systemd and cloud-init; plus about 70 s to download
    nerdctl);
  - `limactl stop`: 14 s; the next `limactl start`: 98 s (guest boot
    1 min 32 s);
  - `limactl shell probe -- CMD`: less than 1 s; sudo works in the guest;
  - QEMU RSS: 2 to 2.7 GB.
- Guest egress goes through the sbx proxy (a guest `curl -sI https://...`
  shows `HTTP/1.0 200 Connection established`), so the sbx network
  policy also applies to the nested VM.
- Things to know:
  - Set `LIMA_HOME` to a short path. The socket path must be less than
    108 characters, and the scratchpad path is too long. `/tmp/lh` works.
  - The engineer allowed `cloud-images.ubuntu.com` in the network
    policy. Without it, the image download fails with 403.
  - QEMU came from apt (`sudo apt-get install qemu-system-x86
    qemu-utils`). Only the sandbox system has it.
  - The first try, with `--cpus=4 --memory=4`, killed the whole sbx
    microVM: probably the host OOM killer, after the 865 MB image
    download completed. The cause is not confirmed. Use small VMs, stop
    them when you do not use them, and keep one VM at a time.

So the write-test-fix cycle can include real `limactl` and real VMs
here, slowly. Timing and performance measurements (phases 0, A1, B1) must
still run on the host: TCG numbers say nothing about KVM.

It can do the code work: the new modules with a fake `limactl` in the
unit tests, the rebase onto !127, and the sbx test suite
(`python3 -m unittest discover -s sbx/tests`). Keep the tests hermetic:
they must pass on the host as well as here (see the `patch_host_probes`
helper in `sbx/tests/test_wmf_sbx_create.py`, and commits `8c76233` and
`dcf86c4`, for the failures that came from probing the real machine).

## Conventions carried over

- Invoke sbx only as `wmf-sbx`, never bare `sbx`, never `--cloud`.
- Do not push anywhere unless the engineer asks.
- Nothing under `sbx/` changes shared plugin files; the default nono
  SessionStart output stays byte-identical to upstream's.
- Comments and commit messages in ASD-STE100, with an
  `Assisted-by: Claude Opus 5.5` trailer and no Co-Authored-By.
- Commit on a branch before you end each turn, and report the branch,
  the commit, and your sandbox name (`$SANDBOX_NAME`).
