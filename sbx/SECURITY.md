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
| Credentials | the engineer's files, minus a deny list | only the Claude credential of the session |
| Work leaves by | the agent's own writes to the host | the engineer's `git fetch` of the agent's commits |

## 1. The layers

1. **The VM.** Each sandbox is its own Lima instance with its own disk,
   a copy of the golden image. Nothing of the host is mounted except
   the git dirs of the sandbox's repos (§2).
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
  channel for small amounts of data.
- An allowed host is a channel too: the proxy filters by host name, and
  some allowed hosts accept writes (the package registries, GitHub).
- NAT reaches the host's services on its public addresses on TCP 80 and
  443, through the proxy only if their name is allowed.

## 4. Credentials

- **Claude.** `resume` gives the host's credential (`ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, or `~/.config/wmf-sbx/claude-oauth-token`)
  to one session: on stdin into a 0600 file in the guest's `/dev/shm`,
  which a wrapper reads and deletes before Claude Code starts. It is
  never on a command line or in the image. The session's environment
  holds it, so **the agent can read it and spend it** while the session
  runs. Prefer a short-lived token. A login made in the VM stays in the
  VM until `rm`.
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
- **Attack surface on the host:** the hypervisor; the 9p or virtiofs
  server, which parses guest requests; `limactl shell` (SSH to the guest
  with Lima's key in `~/.lima/_config/user`).
- **The golden image** is read-only in the cache and checked against
  its checksum by `status`. It holds no credential.

## 7. What the agent can do, plainly

- Read and write its clones; commit; run the tests and the wiki.
- Read the host repos' git dirs, read-only.
- Reach the allowed hosts through nono's proxy, and Lima's DNS.
- Spend the Claude credential of the session it runs in.
- Not: read your home directory or working trees, write anything on
  the host, push, reach the host or the LAN, become root, or use your
  Gerrit, Phabricator, GitLab or SSH identity.

## 8. Reference

`reference/AI_Code_Assistants.wt` and `reference/Secure_use_guidelines.wt`
are the WMF guidelines this design is measured against.
