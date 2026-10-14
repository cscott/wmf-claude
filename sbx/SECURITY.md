# Security model: wmf-sbx on Lima

What the agent in a wmf-sbx sandbox can and cannot reach. The root
`SECURITY.md` covers wmf-claude under nono on the engineer's own
machine; this file covers the sandbox VM that `wmf-sbx create` makes.

Scope: **contained mode**, the only mode so far. The agent has no root,
and Claude Code runs under nono inside the VM. `--sudo` mode (a root
agent, with egress enforced outside the VM) is not ported yet; its
design and threat model are D9 and D11 in
`../lima-port/HANDOFF-LIMA.md`.

## The short version

| | nono on the host | wmf-sbx, contained |
|---|---|---|
| Kernel | the host's | a guest kernel, behind a hypervisor (Lima: `vz` or QEMU) |
| Filesystem | the host's, minus a deny list | the VM's; host repos only as read-only mounts of their git dirs |
| Root | none (`NO_NEW_PRIVS`) | none: `agent` has no sudo, and nono runs inside the VM too |
| Network | nono's allowlist proxy | the same nono proxy, inside an nftables host block |
| Credentials | the engineer's files, minus a deny list | none: the Claude credential stays on the host, behind a proxy |
| Work leaves by | the agent's own writes to the host | the engineer's `git fetch` of the agent's commits |

## 1. The layers

1. **The VM.** Each sandbox is its own Lima instance with its own disk:
   on QEMU a copy-on-write overlay on the golden image, on `vz` a copy
   of it. Nothing of the host is mounted except the git dirs of the
   sandbox's repos (§2). Every QEMU sandbox reads the same golden file,
   but none can write it: QEMU opens a backing file read-only, the file
   is read-only on the host, and no mount shows the image cache. `create`
   and `status` check that each file under the overlay is a read-only
   qcow2 file in the cache (`disk.check_chain`), because the way the
   overlay is attached goes around Lima's own backing-file check.
2. **Two users.** `engineer` (Lima's user, with sudo) runs the setup.
   `agent` has no sudo and is not in the `sudo` or `docker` group. It
   has the host user's uid, so the mounted files are its own and the
   files it makes have the host's owner.
3. **nono.** Every Claude Code session runs upstream's `bin/claude`, so
   nono with the `wmf-engineer` profile, inside the VM. The session adds
   grants for the sandbox: the other clones (read-only for a `:ro`
   repo), the agent's caches, the system paths the tools read, the
   package registries, and local ports (§3).
4. **nftables in the VM** (`sandbox-provision.sh`, loaded at every
   boot): no connection to the host, the LAN, private or link-local
   addresses, except DNS to Lima's resolver and a host proxy on its
   exact port. Off the VM, the agent's uid gets TCP 80 and 443 only.
   TIOCSTI is off, so the agent cannot push input into the engineer's
   terminal.

`wmf-sbx start`, `resume` and `status` check the security invariants
(`vm.INVARIANTS`): the agent has no sudo and the host uid, the host block
is loaded, no mount is writable, every git dir is mounted, TIOCSTI is
off. A sandbox that fails one gets no session.

## 2. Files

**Host files in the VM.** Each repo's git dir is mounted read-only
(9p on QEMU, virtiofs on `vz`) under `/run/wmf-sbx/host/`. The host
enforces read-only: RAN as root in the guest, `mount -o remount,rw`
then a write fails, and the host file does not change. The agent works
in a `git clone --shared` of the mount at the host's path, on the VM's
disk. What the agent can read through the mount: everything in the git
dir, including `.git/config` (and any credentials in its remote URLs),
hooks, stashes and reflogs. Nothing outside the git dirs: not the
working trees, not the home directory.

**Writes.** The agent writes its clones, its home, its caches and
`$TMPDIR`. Nothing it writes reaches the host except through the
engineer's fetch (§5). A `:ro` repo is a clone owned by `engineer`: the
agent can read it but not write it.

**Permission mode.** If the engineer sets `permission_mode:
bypassPermissions` (`README.md`, "Preferences"), the agent's tool calls
are not confirmed, and the `ask` rules of `wiring/settings-merge.json`
are approved without asking. The deny rules still apply. Upstream
`bin/claude` refuses that mode on the command line; in a sandbox the
layers above are the boundary.

**Folder trust.** After the engineer's first sandbox, in which Claude
Code asks, `create` marks each repo of a new sandbox trusted in Claude
Code, so the repo's own Claude Code configuration (`.claude/`,
hooks, MCP servers) is used without a question. These are the
engineer's repos, named on `create`, and the session runs them under
nono in the VM.

**Under nono** the agent's home is not readable, only the directories
the profile and the session grant; `~/.bashrc` and other credential
paths in the profile stay denied.

## 3. Network

**Egress from a session** goes through nono's proxy, which allows the
`wmf-engineer` profile's domains (read-only GET/HEAD endpoints for the
wikis and docs) plus, per session, the package registries and the
browser and Cypress download hosts (`session.REGISTRY_DOMAINS`). Behind
a host proxy, nono chains to it (`--upstream-proxy`).

**`--landlock-only`.** On Linux, nono's seccomp layer denies every
localhost connect except to its own proxy (nolabs-ai/nono#1786), so
`bin/claude --landlock-only` runs nono with Landlock alone. That allows
UDP to any host, and TCP to any host on an opened port. In the VM the
nftables rules close most of that again: off the VM, the agent gets
TCP 80 and 443 only, and nono does not open those ports, so HTTP and
HTTPS still go through the allowlist proxy. UDP leaves the VM only as
DNS to Lima's resolver.

**Local ports.** A session opens 4000 (the wiki), 9876 (karma), and
the guest's ephemeral port window, 49152-53247, with bind-to-port-0:
karma, chromedriver, Chrome's DevTools and Cypress listen on a random
port. Landlock grants ports one at a time and cannot tell loopback
from another host; the nftables rule above is what keeps those ports
on loopback. (Decided by the engineer; to revisit when external egress
control comes with `--sudo` mode.)

**Residuals:**

- DNS queries go to Lima's resolver and from there to the host's: a
  channel for small amounts of data. Planned: external egress control,
  with no DNS in the guest, or else a filtering DNS forwarder
  (sbx/NOTES.md, "Local ports").
- An allowed host is a channel too: the proxy filters by host name, and
  some allowed hosts accept writes (the package registries, GitHub).
- NAT reaches the host's services on its public addresses on TCP 80 and
  443, through the proxy only if their name is allowed.

## 4. Credentials

- **Claude.** The credential stays on the host (`credproxy.py`). For
  each session, `resume` runs a reverse proxy on the host's loopback at
  a port the OS picks, and the session gets its address
  (`ANTHROPIC_BASE_URL=http://192.168.5.2:PORT`) and a random phantom
  credential, on stdin into a 0600 file in the guest's `/dev/shm`. The
  proxy refuses a request without the phantom, replaces the phantom
  with the real credential, and forwards only `/v1/...` to
  api.anthropic.com (plus the credential-free `/api/hello`). So **the
  agent can spend the credential while the session runs, but cannot
  read it or take it out**; the phantom is worthless once the session
  ends. The host's own Claude Code login is read for each request and
  never written. `wmf-sbx login` makes the usual credential, a
  one-year `setup-token` token for model requests only, in a throwaway
  VM with no repo, no mount and no session, and deletes the VM. A login made in the VM (no host credential) stays in
  the VM until `rm`.
- **The proxy's port.** For the session, `resume` adds an nftables rule
  that lets the agent reach 192.168.5.2 on that one port, tagged with
  the port, and removes it when the session ends. A rule left by a
  `resume` that did not end cleanly stays until the next `resume`
  (which removes rules whose port has no listener on the host) or the
  next boot. Other processes on the host can reach the proxy's port
  too; without the phantom they get a 403.
- **No other credential is in the VM.** The Phabricator, Gerrit and
  GitLab MCP servers run in the VM anonymously: they can read public
  data, and their write tools fail. No SSH agent is forwarded; the VM
  has no SSH key for Gerrit. The engineer's git identity (name and
  e-mail) is in the session environment, for commits.

## 5. Code that runs outside nono

- **At create time**, as the agent but outside nono, before the first
  session: `composer update`, `npm ci` and the MediaWiki install, with
  the repos' own scripts (D7). They run in the VM, as `agent`, under the
  nftables rules, before any agent session has written to the repos.
  After the first session wmf-sbx runs no repo code outside nono.
- **`wmf-sbx exec`** runs the engineer's command as `agent`, outside
  nono. That is the engineer's choice; do not run code the agent wrote
  that way without reading it. `exec --engineer` refuses a working
  directory that the agent can write.
- **`wmf-sbx cp`** never lets root write a path the agent controls: the
  agent copies into its own tree from a staging directory.

## 6. The host side

- **Fetching the agent's work.** The host remote `wmfsbx://NAME/PATH`
  runs `git upload-pack` in the VM, as the agent, with hooks and
  fsmonitor off. It supports fetch only. The host's git parses what it
  receives as with any remote; review before you merge.
- **Host gc** is suspended in each repo while a sandbox borrows its
  objects, and restored by `rm`. `rm` refuses to remove a sandbox with
  commits that the host has not fetched, unless `--force`.
- **QEMU runs through `bin/wmf-sbx-qemu`** (on QEMU): Lima starts the
  wrapper in place of QEMU. It runs QEMU's probes unchanged, and adds a
  virtio balloon with free-page reporting to the boot, so that a guest
  gives the memory it frees back to the host. For a `--sudo` sandbox it
  also starts the egress proxy (track A); it refuses a boot that has
  neither that proxy's config nor the plain mode that `wmf-sbx` sets.
- **Attack surface on the host:** the hypervisor; the 9p or virtiofs
  server, which parses guest requests; `limactl shell` (SSH to the guest
  with Lima's key in `~/.lima/_config/user`).
- **The golden image** is read-only in the cache and checked against
  its checksum by `status`. It holds no credential.
- **Warm snapshots** (QEMU) carry a sandbox's disk to the next sandbox
  of the same repos. A snapshot is frozen only at the end of `create`,
  before any session ran (D1), so no agent wrote to it; but what
  `create` ran (npm and composer install scripts, which also run in
  every cold create) now lives on in later sandboxes of the same repos,
  until the golden image is 14 days old (`images.maxAge`) and every
  snapshot on it goes cold. `--cold` starts over at once. The freeze
  removes the sandbox's identity (machine-id, SSH host keys, cloud-init
  state) and its setup logs. The guest writes its filesystem, not the
  qcow2 headers, and `create` checks the backing chain of every disk.
  A snapshot keeps host gc off in the repos it borrows from, as a
  sandbox does (above), for as long as the snapshot exists.

## 6a. Nested sandboxes (`create --nested`)

**A nested sandbox is less secure than a normal one. Use it only to work
on wmf-claude or wmf-sbx itself; for all other work, do not pass
`--nested`.** Inside the sandbox VM, nono gives the agent more: the
attack surface of the outer VM's kernel through `/dev/kvm`, Lima's and
QEMU's sockets and data, more writable directories, and more download
hosts. The host boundary (section 1) is the same, but the nono layer in
the sandbox is weaker.

For work on wmf-sbx itself, a nested sandbox lets the agent run Lima VMs
of its own. Root installs QEMU, OVMF and Lima at `create` and adds the
agent to the `kvm` group; the sessions get `/dev/kvm`, `~/.lima` (with
its sockets), QEMU's and Lima's data, `wmf-sbx-src` in the engineer's home path, wmf-sbx's
own directories, and the download hosts of an image build
(`nested.py`). The install also downloads the Debian base image to
`/var/cache/wmf-sbx/base`, checked against the digest that wmf-sbx
pins. What changes:

- `/dev/kvm` is a large kernel interface, but it is the sandbox VM's
  kernel, which the agent already shares; the host's hypervisor is not
  exposed beyond what nested virtualisation already is.
- The inner VMs' network goes through the session's nono proxy (Lima
  points them at it), and the outer VM's nftables still bound it. The
  inner VMs trust the session's nono CA (it re-signs TLS for hosts with
  endpoint rules); an image build removes it before it seals.
- Before a session in a nested sandbox (never in an ordinary one),
  `resume` runs `limactl list` and `limactl stop` as the agent, outside
  nono (an earlier session's inner VMs are out of the new session's
  reach). `limactl` is root's; what it reads in `~/.lima` is the
  agent's, and it runs with the agent's uid, so it can signal and write
  only what the agent owns.
- Every sandbox's invariants check that the agent is in the `kvm` group
  only in a nested sandbox (root writes `/etc/wmf-sbx/nested`, which the
  agent cannot).

## 7. What the agent can do, plainly

- Read and write its clones; commit; run the tests and the wiki.
- Read the host repos' git dirs, read-only.
- Reach the allowed hosts through nono's proxy, and Lima's DNS.
- Spend the Claude credential of the session it runs in, through the
  host's proxy; not read it.
- Not: read your home directory or working trees, write anything on
  the host, push, reach the host or the LAN, become root, or use your
  Gerrit, Phabricator, GitLab or SSH identity.

## 8. Reference

`reference/AI_Code_Assistants.wt` and `reference/Secure_use_guidelines.wt`
are the WMF guidelines this design is measured against.
