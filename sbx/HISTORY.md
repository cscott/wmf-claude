# wmf-sbx: history

The paths that were taken and then abandoned, and the paths that were
not taken, each with its reason. The other files in `sbx/` describe only
the code as it is.

## Docker Sandboxes, replaced by Lima

wmf-sbx started (September 2026) as a wrapper around Docker Sandboxes
(`sbx`): a microVM per sandbox with the agent as root, "kits" that
installed the toolchain, a writable clone mounted over a read-only bind
of each host repo, a restore pass after every container start, a git
daemon on a published port for the host to fetch from, an MCP gateway
on the host, SSH-agent stripping, and an idle stop. Upstream then
started a Lima VM mode (!127). In October 2026 wmf-sbx moved to Lima, on
top of it: the guest runs the same `bin/claude`, profile and tool layer
as upstream, a Lima instance keeps its disk and mounts across boots (so
the restore pass and the mount-over went), read-only git mounts and
`wmfsbx://` remotes replaced the git daemon, and a nested sandbox can
run Lima itself, which the sbx microVM could not.

## The MCP servers on the host

The Docker port ran the MCP servers on the host, and the sandbox reached
them through a gateway. The secrets stayed on the host. Lima has no such
gateway, so the servers now run anonymous in the VM. Moving them out of
the guest again is an open item (`TODO.md`).

## Credential proxy options

The first Lima version put the host's API key or setup-token token in
the session environment, where the agent could read it. Three designs
to keep it out of the VM were compared. **H**, a reverse proxy on the
host that swaps a phantom for the real token, was chosen and is
`credproxy.py`. **N**, nono's own `--credential` phantoms, was simpler
but weaker: the real token is in the VM, in the nono supervisor's memory.
**K**, an `apiKeyHelper` that gets a short-lived token from the host,
needs a short-lived credential to exist, and subscription tokens are not
short-lived; it stayed a fallback and was not built.

For a full `/login` in the sandbox, capture in the host egress proxy
(nono's `oauth_capture`, now `authproxy.py`) was taken, because it works
in `--sudo` mode too. Rejected: capture in the guest's nono session (no
nono in `--sudo` mode, and the real token in the VM); an `apiKeyHelper`
(no gain over capture); and a copy of the credentials file or the
refresh token into the guest (the real refresh token in the VM, and a
rotation invalidates the copies that other sandboxes and the host hold).

A root install broker in the VM, for package installs in a nono session,
was considered and not built: behind the egress proxy, `--sudo` mode
installs packages directly.

## Image builder

Option **A**, a Lima builder VM from the pinned Debian cloud image, was
taken. Option **B**, mmdebstrap inside mkosi, was kept for later and not
taken: the bootloader, the kernel, cloud-init and both architectures
are then ours to get right, and it runs on Linux only.
A per-session nono profile for MediaWiki (`wmf-mediawiki.json`) was
also rejected: `extends` cannot name a profile loaded by path, so
launcher flags add the grants.

## Disks

The first version gave each sandbox a full copy of the golden image
(2.6 GiB each). `limactl clone` was tried and dropped: it copies the
disk too. Overlay method **A** (let Lima make the overlay) was refused:
Lima 2.0 removed qcow2 overlays (lima#4206), and `vz` and krunkit take
raw disks only. Method **B**, a swap of the disk after `limactl create`,
was taken. An APFS clonefile or reflink copy for macOS was not taken: it
saves disk only where the file system supports it, and `vz` still
converts to raw.

## The egress proxy

squid was considered for the host proxy; `nono proxy` was taken, so the
proxy and the in-guest sessions share the profile's endpoint rules. The
`tcp:` form of the QEMU guestfwd was tried and failed: it is one shared
stream for all connections.

## `--landlock-only` and nono 0.78 and 0.79

Under nono 0.78 and 0.79, proxy mode ignored the connect-port grants
(nolabs-ai/nono#1786), so sessions ran with `--landlock-only`, and
other workarounds covered the gaps. nono 0.80 fixed the cause, and the
flag and the workarounds were removed. Only nested sessions still use
`--sandbox-policy=landlock`, for a different reason (Lima's UDP socket).
A sandbox made from a golden image older than the removal still has a
`bin/claude` that wants `--landlock-only` for localhost ports, and its
sessions stop at once: make it again.

## Bundles

Upstream's VM mode moves git data with bundles. wmf-sbx kept read-only
mounts for host to VM and `wmfsbx://` remotes for VM to host: a mount
needs no copy and is always current, and in both designs the host
chooses the names.

## Old names

- fenced+nono was called "contained". That name is upstream's, for a VM
  that reaches the model API only, so ours became "fenced".
- The settings file was `repos.yaml`. It became `settings.yaml`, and its
  keys changed: `rules` became `repos`, `extra_environment` became
  `environment`, and `extra_packages` became `packages`.
- `WMF_CLAUDE_GIT_HASH_OVERRIDE` became `WMF_SBX_GIT_HASH_OVERRIDE`,
  because it is host configuration.
- The Phabricator username was stored in a `config.json` (upstream
  !131). Upstream's `WMF_CLAUDE_PHAB_USER` replaced it.

None of these had a compatibility path at release: they were all on the
development branch only.
