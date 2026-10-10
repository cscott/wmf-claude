# HANDOFF: port `wmf-sbx` from Docker Sandboxes to Lima VM mode

For: the next Claude session that works on cananian's `wmf-sbx` tooling.
Written 2026-10-08 and revised three times the same day to record four
decisions and an order of work:

- the agent may have sudo (D9);
- host repositories are mounted read-only instead of being pushed as
  bundles (D10);
- `--sudo` sandboxes run on QEMU, with egress forced through a filtering
  proxy on the host (D11);
- copy-on-write sandbox disks are qcow2 overlays on an immutable golden
  image, which only the QEMU driver can boot (D12);
- **the MVP is `--no-sudo` with a plain copy of the golden image. `--sudo`
  and copy-on-write disks are two independent tracks after the MVP**
  (§11).

A companion research note, `HANDOFF-LIMA-QCOW2-OVERLAYS.md` ("Handoff:
Lima VMs on qcow2 overlays with an absolute backing file", cananian,
2026-10-08), holds the evidence and the test procedure for D12. Keep it
next to this file, and read it before working on that track.

The author compared four snapshots: the fork base, `work/cscott/sbx`,
upstream `main`, and Kosta Harlan's Lima VM MR
(`repos/product-safety-and-integrity/wmf-claude!127`, patch dated
2026-09-29). The author had no network access to gitlab.wikimedia.org,
and changed and pushed nothing. Claims are labelled:

- **READ**: read in the source or in Lima's documentation.
- **RAN**: run on a scratch copy.
- **VERIFY**: inferred. Measure it before you rely on it.

**Measurement run, 2026-10-08.** A later session ran most of the D11
and D12 items on real VMs, and read the Lima `v2.2.1` source (commit
`27bc4c4b`). Those items are now labelled **RAN** or **READ** in place.
Environment: Lima 2.2.1, QEMU 8.2.2, Ubuntu 24.04 x86_64 host on ext4,
Ubuntu 24.04 minimal cloud guest (cloud-init 26.1, kernel 6.8). The
host had **no KVM**, so QEMU used TCG (software emulation, about 20
times slower). Use those times only to compare with each other. Not run
there: macOS, `vz`, HVF, APFS clonefile, Btrfs or XFS reflinks, and
anything that needs the guest to reach the internet. Record these
results in `sbx/NOTES.md` when the port lands.

**Lima v2.2.1 renamed the instance files.** The boot disk is
`~/.lima/<name>/disk`, and the downloaded image is `image` until
`EnsureDisk` renames it. `diffdisk` and `basedisk` are legacy names
that Lima migrates at start (READ, `pkg/limatype/filenames`). This
document uses the new names.

## 0. Goal, starting point, scope

**Starting point.** `work/cscott/sbx` is already rebased onto current
upstream `main` (see `HANDOFF.md`). The sbx unit suite is green.

**Goal.** Rebase further onto Kosta's Lima VM MR. Then make every
`wmf-sbx` command work on Lima instead of Docker's `sbx`, keeping as much
current behaviour as possible:

- one command makes a sandbox: it resolves repos, walks MediaWiki
  dependencies, clones, and gives you a working wiki;
- PHP, composer and npm run **natively in the VM**;
- each sandbox boots from a **golden disk image**, built once,
  customized like the old `mediawiki-kit`, and cached. In the MVP each
  sandbox gets a full copy of it. After the MVP, QEMU sandboxes get a
  **copy-on-write qcow2 overlay** instead, which makes startup faster
  and stores only each sandbox's own writes (D12);
- the VM sees the host's repositories **read-only**, so any commit the
  host has fetched is reachable inside without a push step (D10);
- the agent **may be root in its VM**, chosen per sandbox (D9). A root
  agent's egress is filtered **outside** the guest, by a proxy on the
  host (D11). That needs the QEMU driver, so the user trades speed for
  control: `--no-sudo` can use the faster `vz` on macOS, and `--sudo`
  gives the stronger, guest-proof firewall. QEMU also gets the cheap
  copy-on-write disks (D12);
- the host gets a git remote per sandbox, and `git safe-reset <name>`
  pulls the agent's work back;
- the same verbs: `create`, `start`, `resume`, `run`, `exec`, `cp`, `rm`,
  `ls-remotes`, `resolve`.

**What goes away.** Upstream `sbx`, its daemon, kits, MCP gateway and
policy engine. Everything that exists only to work around them goes too:
the SSH-agent stripping wrapper, the mount restore pass, port
re-pointing, the host-side MCP proxy, editing sbx's own CLAUDE.md, and
the settings preflight.

**Names stay.** The CLI keeps the names `wmf-sbx` and `wmf_sbx`. "sbx"
now just means "sandbox".

**Out of scope:** changing Kosta's single-VM mode
(`bin/wmf-claude-vm`). It stays, next to ours. D9, D10 and D11 are
deliberate departures from his design, so they live in **our own
template**, and we do not propose them upstream (§12).

## 1. What Kosta's MR provides, and which of its rules we keep (READ)

| Piece | What it gives us |
|---|---|
| `lima/wmf-claude.yaml` | `plain: true`, `mounts: []`, an ignore-all `portForwards` rule, no agent or X11 forwarding, a Debian 13 image pinned by sha512. Two users: `engineer` (sudo, docker) and `agent` (neither). A root-owned nftables table blocks the host, the LAN and private ranges, and gives the agent's uid TCP 80/443 only. TIOCSTI is off. Its `provision` script runs at every boot and is idempotent. |
| `lima/guest-install.sh` | nono pinned by `.nono-version` and checked against `SHA256SUMS.txt`. The tree goes into `/opt/wmf-claude.<rev>` with an atomic symlink swap. Claude Code for `agent`. `bin/wmf-claude-setup` as `agent`. |
| `lima/guest-claude.sh` | Starts `bin/claude` as `agent`: no login shell, a fixed `PATH` with `~/.local/bin` last, `env -C <workspace>`. |
| `bin/wmf-claude-vm` | Patterns to copy: `guest`/`guest_agent`, `login` (`claude auth login` outside nono, then `hasCompletedOnboarding`), `status` invariant checks, `pull` run as `agent` with hooks and fsmonitor off. |
| `bin/claude` | `WMF_DOCKER_HANDSHAKE`, and pre-creating `/tmp/claude-$UID`. |
| `tests/test-lima.sh` | Static grep checks on his template and scripts. |

**Kept in our mode, always:**

- no port forwards: keep the explicit ignore-all rule;
- no SSH agent or X11 forwarding, no Rosetta;
- a versioned base image with a digest;
- no Docker socket reachable by the agent in contained mode;
- the engineer never runs git in, or code from, a path the agent can
  write;
- the agent's dotfiles never run outside nono in contained mode;
- inside the guest, the same `bin/claude`, the same profile and the
  same tool layer, whenever nono is used.

**Changed in our mode, deliberately:**

- `plain: false` with explicit read-only mounts (D10). Plain mode
  ignores `mounts` entirely (READ, Lima docs).
- An optional sudo-capable agent that runs without nono (D9).
- For `--sudo` sandboxes: the QEMU driver, run through a wrapper that
  adds `restrict=on` and a single `guestfwd` (`cmd:` form) to a host
  proxy (D11).

**Mode shape.** His mode is one persistent VM with many workspaces. Ours
is one VM per sandbox, cloned from a cached image, as sbx was.

## 2. Target architecture

```
host                                     Lima instance <name> (one per sandbox)
────                                     ──────────────────────────────────────
wmf-sbx create Cite [--sudo]             disk: copy of golden <hash> (MVP);
                                           qcow2 overlay on it (QEMU, D12)
 ├ resolve + dependency walk (unchanged)   engineer: sudo; drives setup
 ├ ensure golden image (build on miss)     agent: sudo only with --sudo (D9)
 ├ clone instance; mounts = repos' .git ──► /run/wmf-sbx/host/<host path>/.git  (read-only,
 ├ suspend host gc (alternates)              host-enforced: virtiofs or 9p)
 ├ in-VM setup                             /home/cananian/…/core  clone --shared → alternates
 └ add host remote `<name>`                   remote `local` = the read-only mount
                                           composer serve on 127.0.0.1:4000
wmf-sbx resume <name>  ──limactl shell──►  claude as agent: in nono (contained),
                                             or bare (--sudo)
git fetch <name>  ◄──git-remote-wmfsbx──   git upload-pack, run as agent, fetch-only

--sudo only (D11):
host proxy 127.0.0.1:<port> ◄─guestfwd─   192.168.5.100:3128 = the guest's only
  (allowlist; per sandbox)                  way out (QEMU slirp restrict=on)
```

**How each sbx piece is replaced:**

| sbx today | Lima replacement |
|---|---|
| sbx microVM per sandbox | Lima instance per sandbox: `vz` on macOS, QEMU on Linux |
| kit `spec.yaml` | **golden image** built once per kit content (§5) |
| kit `commands.install` as root | image build as root, then create-time steps (§7) |
| read-only host repo bind + writable clone mounted over the host path | read-only Lima mount of each repo's git dir at `/run/wmf-sbx/host/…`; an ordinary `--shared` clone **at the host's absolute path** (D8, D10). No mount-over dance |
| `:ro` extra | the mount alone. No clone, or an engineer-owned clone |
| `--shared` alternates into `.sbx-originals` | `--shared` alternates into the read-only mount. **Host gc suspension stays** |
| git daemon + published port | `git-remote-wmfsbx` over `limactl shell` (§6.2) |
| in-sandbox `git safe-reset local` from the host mirror | unchanged: `local` = the mount |
| restore pass after every container start | gone. Lima re-mounts from the instance config at every boot, and clones live on the persistent disk |
| idle auto-stop | gone |
| host-side MCP behind sbx's gateway | upstream's anonymous MCP servers, in the VM |
| agent is root | per sandbox: `--sudo` (sbx-like) or contained (Kosta-like) (D9) |
| egress allowlist enforced **outside** the VM by sbx's proxy | contained: nono's proxy and nft, both **inside** the VM. `--sudo`: a host proxy that QEMU makes the guest's only route (D11), so it is **outside** the VM again |

## 3. Decisions

Each decision is marked **DECIDED** or **OPEN** (needs cananian's
answer).

**D1. How the golden image is built. DECIDED 2026-10-09: A now, B later.**

- **A:** boot a builder instance from Kosta's pinned Debian
  genericcloud image, run the image provisioning (§5.2), seal its
  identity (§5.2; flags RAN), stop, and export the disk. It
  works with Lima by construction and keeps a digest-pinned base. The
  MR !127 baseline (§11, phase 1) booted and provisioned this base
  under Lima 2.2.1 (RAN). **Chosen for the MVP.**
- **B (later, as a reproducibility upgrade):** mmdebstrap (the modern debootstrap: rootless,
  reproducible) inside mkosi for a bootable image. The bootloader,
  kernel, cloud-init and both arches are then ours to get right, and it
  must run on Linux (a Lima builder on macOS).

Both produce the same artifact, an image plus a digest.

**Never build a golden image from a sandbox that ran an agent,** only
from a builder VM. A guest controls its own disk. CVE-2023-32684
(READ) was a malicious disk image reading a host file through a qcow2
backing path.

**D2. How a sandbox gets its disk in the MVP. DECIDED: a full copy.**
Superseded for QEMU after the MVP by D12.

- Since v2.0, Lima gives every instance a full standalone disk: it
  renames or converts `image` into `disk`, and `vz` converts qcow2 to
  raw (READ, the companion note citing Lima PR #4206 and issue #2579;
  the QEMU driver only renames, READ in v2.2.1).
- The MVP accepts that. `create` points `images:` at the golden image
  file and lets Lima copy it. A local path works as `location` (H1,
  RAN on QEMU): a bare absolute path or an absolute `file://` URL. Lima
  copies it with `continuity/fs.CopyFile`, decompresses it if the
  extension or magic says so, and never symlinks it. A digest is
  optional (READ, `pkg/downloader/downloader.go:625-680`).
- Record the time and bytes of a create on `vz` and on QEMU. That is
  the baseline D12 has to beat.
- `limactl clone` of a stopped template instance is no longer the plan.

**D3. Package-registry egress in a contained session. DECIDED 2026-10-09: per-session `--allow-domain`, no new profile.**

This applies only to contained sandboxes (D9). With `--sudo` the agent
has no nono, and egress is open. The first decision was a
purpose-named `profiles/wmf-mediawiki.json` with **GET/HEAD-only** rules
for packagist, the npm registry, the GitHub hosts composer uses, and the
Cypress and Chrome-for-testing hosts. npm needs `--no-audit`.
**`extends` cannot name a profile by path** (RAN, nono 0.78.0: "invalid
base profile name '/opt/wmf-claude/profiles/wmf-engineer.json'"). So
`wmf-mediawiki.json` cannot extend `wmf-engineer.json` in place. Options:
extend `claude-code` (a pack name) and carry `wmf-engineer`'s rules,
generated from it by a script with a test that they stay in sync; or
pass the extra hosts per session with `--allow-domain`, as
`bin/claude --minimax` does. Do not install profiles into
`~/.config/nono/profiles/` (CLAUDE.md forbids it).
**Decision (cananian, 2026-10-09): the second option.** The contained
launcher adds the registry hosts with `--allow-domain` per session, from
one list in the sbx code with a test. No new profile file. The hosts are
then visible at launch, and nothing has to stay in sync with
`wmf-engineer.json`.

**D4. Environment variables. DECIDED (recommendation stands).** Put
`MW_SERVER`, `MW_SCRIPT_PATH`, `MW_INSTALL_PATH`, `CHROME_BIN`,
`MEDIAWIKI_*`, `API_TESTING_CONFIG_FILE`,
`MEDIAWIKI_HAS_INTEGRATION_TESTS` and the other kit variables in the
`env` key of the agent's `~/.claude/settings.json`. In contained mode
this gets past nono's `allow_vars` filter (READ). In `--sudo` mode it is
harmless.

**D5. Reaching the local wiki.**

- **Contained mode: `--local-web=4000 --landlock-only`. DECIDED by
  measurement (RAN, nono 0.78.0, the real `wmf-engineer` profile, as
  the agent in the MR !127 VM).** A server on 127.0.0.1:4000 and a
  client, both in the sandbox:

  | nono flags | bind | connect |
  | --- | --- | --- |
  | none, or `--open-port 4000` | EACCES | — |
  | `--listen-port 4000` (± `--open-port`) | yes | **no** (nono#1786) |
  | `--open-port 4000` + landlock-only | yes | yes |

  No separate bind grant is needed: under `--sandbox-policy landlock`,
  `--open-port` allows both. `bin/claude --local-web=4000
  --landlock-only` gives exactly that. Use it as the default for
  contained `resume`, and drop it when nono#1786 is fixed.
- **`--sudo` mode:** no nono, so no flag is needed.

**D6. Signing in. DECIDED for the MVP (cananian, 2026-10-10): a host
credential per session; a shared login is the first item after the MVP.**

A sandbox is made per task and must be quick to make, so a login per VM
is not acceptable.

- **What Docker sbx did** (`sbx/NOTES.md` §81.4, READ): the Claude login
  was **on the host**. sbx's egress proxy put it into Anthropic requests,
  and the sandbox held only a placeholder (`Bearer proxy-managed`). A
  `/login` started in any sandbox was kept on the host and shared by all
  sandboxes; when it expired (after days), the next session asked again.
- **What Claude Code supports** (READ, code.claude.com authentication,
  env-vars, settings, LLM-gateway pages, 2026-10-10):
  - `claude setup-token`: a **one-year** OAuth token for
    `CLAUDE_CODE_OAUTH_TOKEN`; no option for a shorter life; model
    requests only. Precedence: `ANTHROPIC_AUTH_TOKEN` >
    `ANTHROPIC_API_KEY` > `apiKeyHelper` > `CLAUDE_CODE_OAUTH_TOKEN` >
    `/login`.
  - `apiKeyHelper`: a command whose output is sent as `X-Api-Key` and
    `Authorization: Bearer`, run again after 5 minutes
    (`CLAUDE_CODE_API_KEY_HELPER_TTL_MS`) and on a 401/403. Whether it can
    return a subscription token is not documented.
  - `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`: `claude auth login` without a
    browser. Refresh-token rotation is not documented, so copies of one
    login in many VMs may break each other; the same holds for copying
    `~/.claude/.credentials.json`.
  - A gateway that injects the credential: documented for the cloud
    providers only; for Anthropic's own API "a reachable base URL isn't"
    a credential, and a subscription login needs an OAuth header the
    gateway must keep.
  - A Console API key can have an expiry (3 hours to 30 days, or custom).
- **The MVP (built, phase 6):** `wmf-sbx resume` takes the host's
  `$ANTHROPIC_API_KEY`, `$CLAUDE_CODE_OAUTH_TOKEN` or
  `~/.config/wmf-sbx/claude-oauth-token` (mode 0600, from `claude
  setup-token`) and gives it to that session only: on stdin into a 0600
  file in the guest's `/dev/shm`, which the launcher reads and deletes.
  Never in a command line, never on the VM's disk. The Claude process
  has it in its environment while it runs. Without one, Claude Code asks
  for a login, which stays in that VM.
- **After the MVP (first item, before tracks A and B):** the VM holds no
  long-lived credential. Options, to measure:
  - **nono `--credential SERVICE`** ("Inject credentials via reverse
    proxy", nono 0.78.0 `run --help`): nono's proxy, outside the
    sandboxed process, adds the credential. Closest to what sbx did, and
    it is in the VM already;
  - an `apiKeyHelper` in the VM that gets a short-lived token from the
    host;
  - the host proxy of track A injecting it (as sbx did), if Anthropic's
    API accepts that for a subscription login.
- Never bake a credential into the image.

**D7. Repo code outside nono. DECIDED for contained mode; moot with
`--sudo`.**

- Create-time `composer update`, `npm ci` and `mw-install` run as agent
  outside nono, before the first session only.
- After `state["attached"]`, wmf-sbx never runs repo code outside nono
  again.

**D8. Paths in the VM. DECIDED.** Clones live at the host's absolute
paths. Claude Code's project key, the seeded memory and `link_plan()`
rely on it. Mounts live under `/run/wmf-sbx/host/`, so nothing has to be
mounted over a clone.

**D9. Agent privilege. DECIDED: configurable per sandbox.**

- Interface: `wmf-sbx create --sudo` / `--no-sudo`. The default comes
  from `repos.yaml` `agentSudo:`. The built-in default is `false`, which
  matches upstream and anyone else running this; cananian sets `true`.
- It is recorded in the sandbox's state, and `resume`, `exec` and
  `status` read it from there. Changing it later means a new sandbox.
- **`--sudo` requires the QEMU driver and the D11 host proxy.** `create
  --sudo` sets `vmType: qemu` on every host, and refuses `vz`. On
  macOS the user chooses between the faster `vz` (`--no-sudo`) and
  QEMU, which brings the guest-proof firewall (`--sudo`) and, after D12,
  copy-on-write disks. On Linux QEMU is the only driver, so `--sudo`
  costs nothing there.
- **Order of work:** the MVP ships `--no-sudo` only. Until `--sudo`
  lands, `create --sudo` fails with a clear message (§11).

**Sudo and nono do not combine.** nono sets `PR_SET_NO_NEW_PRIVS`
(READ, as recorded in the old `sbx/SECURITY.md` comparison table), so
`sudo` cannot gain privilege inside a nono session even when sudoers
allows it (VERIFY in the guest). `bin/claude` also refuses
`--capability-elevation`. So the option selects one of **two whole
modes**:

| | contained (`--no-sudo`) | trusted VM (`--sudo`) |
|---|---|---|
| agent groups | none | `sudo`, NOPASSWD |
| Claude session | `bin/claude` → nono, as in Kosta's mode | `claude` with `--plugin-dir /opt/wmf-claude` and the tool-layer `--settings`, **no nono** |
| Lima driver | `vz` (macOS default) or QEMU | **QEMU only**, through the D11 wrapper |
| on-demand installs (`mw-install-browser`, apt) | no, unless baked into the image (or a future install broker, below) | yes: the helpers' `sudo apt-get` work unchanged, through the proxy |
| egress | nono's allowlist, inside nft's agent rules, **in the guest** | the host proxy's allowlist, **outside the guest** (D11). Root cannot route around it |
| host loopback services | blocked by nft | cut by QEMU `restrict=on` (D11) |
| read-only mounts | only as strong as their host-side enforcement | the same, and the agent *will* be able to try a remount |

**Is trusting Lima with root reasonable?** For *execution*, largely
yes. A guest-root escape needs a hypervisor bug in Virtualization.framework
or QEMU/KVM; such bugs are rare, and Lima's design assumes an untrusted
guest. Keep macOS, Lima and QEMU patched. Root does widen the attack
surface (virtio devices, kernel modules, the guest side of every
host↔guest protocol), but that is not the main cost.

**The main cost was the network, and D11 is the answer.** Lima has no
network controls of its own (READ: its network modes choose topology,
DNS and inbound forwards; none filter egress). Under sbx, egress policy
was enforced **outside** the VM, so a root agent could not bypass it. In
a Lima guest, nft and nono are **inside**, and root can turn them off.
Without D11, a `--sudo` agent would reach:

- **Host loopback.** Lima's docs (READ): "The loopback addresses of the
  host is `192.168.5.2` and is accessible from the guest as
  `host.lima.internal`."
- **Any host, any port, UDP.**

D11 closes both from outside the guest: QEMU's `restrict=on` stops all
routing, and the only path out is a `guestfwd` to the host proxy. That
is why `--sudo` requires QEMU.

**Still true with D11:**

- **The guest side of every channel is hostile.** `limactl
  shell`/`copy` (ssh/scp) and `git-remote-wmfsbx` (git) talk to a server
  the agent controls. Keep host OpenSSH and git current. Prefer git over
  `limactl copy` for bringing anything out.
- The two-user split, TIOCSTI, and engineer-owned files stop being
  boundaries. Keep them anyway: they are cheap and they protect
  contained sandboxes built from the same image.

This is acceptable for cananian's use, **if** the D11 measurements
(phase A1) pass and these residuals are written down in
`sbx/SECURITY.md`.

**Later option, if on-demand installs are wanted in contained mode:** a
root **install broker**, modelled on the Docker broker. A small root
service in the VM installs from an allowlist (`mw-install-browser`,
`mw-install-cypress`, a fixed apt list) on request, over a Unix socket
in group `wmfbroker`. nono does not mediate AF_UNIX `connect()` (READ),
so a contained session can reach it without a nono grant. This gets the
"install the browser when needed" behaviour while keeping nono.

**D10. Host repositories: read-only mounts, not bundles. DECIDED.**

**Why Kosta pushes bundles** (READ, `docs/lima-vm.md` and the template).
His threat model is "a rogue or prompt-injected agent that must have no
path to the host, even through a sandbox bug". Bundles serve that in
four ways:

1. **No host-side file server.** A mount is a host process (or a
   framework service) that parses requests from the guest. Lima's own
   docs (READ) warn that for reverse-sshfs "a compromised sshfs process
   in the guest may have access to unexposed host directories".
2. **No data beyond commits.** A bundle carries commits and refs only,
   "never `.git/config`, hooks or untracked files". A mounted repository
   exposes untracked files (`.env`, local secrets), `.git/config` (which
   can hold credentialed URLs; upstream's tool layer denies
   `Read(.git/config)` for that reason), stashes, reflogs and every other
   branch.
3. **No dependence on read-only enforcement.** Lima documents no
   mechanism for `writable: false` (READ). The sbx project measured the
   same trap: a read-only mount made inside the guest is lifted by a
   root `remount` (old `sbx/SECURITY.md` §3).
4. **One switch.** `plain: true` removes mounts, dynamic port forwards
   and the guest agent together, and that is easy to audit and test.

**What our model costs, and what has to change:**

- **Template:** `plain: false`, which plain mode requires for mounts.
  Keep the explicit ignore-all `portForwards` rule,
  `containerd: {system: false, user: false}`, and the `ssh` forwards off.
  Kosta wrote those explicitly "so a change to `plain` alone does not
  open them", which is the case here. The Lima guest agent comes back
  (port-forward watcher). It adds no forward past the ignore rule (RAN,
  QEMU, Lima 2.2.1): with the rule, the host agent logs "TCP (except for
  SSH) and UDP port forwarding is disabled", and a guest server on 4000
  is not reachable from the host. Without the rule, the same server was
  forwarded to the host's 127.0.0.1:4000 at once. Keep the rule.
- **Mount type pinned, never reverse-sshfs:** `virtiofs` on `vz`, `9p`
  (Lima's QEMU default) on Linux. Our tests assert both. A 9p mount has
  `cache: none`, and the guest has Debian's generic kernel, not the
  cloud kernel (§6.1, RAN in phase 4).
- **What to mount:** each repo's **git dir only**, not the worktree.
  Resolve it on the host with `git rev-parse --git-common-dir`, so
  worktrees and submodule gitdirs resolve. Mount it read-only at
  `/run/wmf-sbx/host/<host path>/.git` (`mountPoint`, READ). This
  leaves out untracked files. It still exposes `.git/config`, hooks,
  stashes and reflogs; write that residual down.
- **Read-only must be host-enforced, and proven by bypass.** In the
  guest, as root: `mount -o remount,rw` on each mount, then try to write.
  Check on the host that nothing changed. Do it for virtiofs on `vz` and
  for 9p on QEMU before trusting either. With `--sudo` this test is
  **the** control; if a mount type fails it, that type is unusable in
  `--sudo` mode.
  - **9p on QEMU passes (RAN).** Lima starts QEMU with
    `-virtfs local,…,security_model=none,readonly=on` for a
    `writable: false` mount. Guest root's `mount -o remount,rw`
    succeeds and the mount shows `rw`, but a write still fails with
    `Read-only file system`, and the host file is unchanged.
  - virtiofs on `vz`: not yet run.
- **Clones:** `git clone --shared` from the mount, so alternates point
  into the read-only objects, as sbx did. This brings back the host-gc
  hazard, so **`suspend_gc`/`resume_gc` stay** (reversing the first
  draft of this handoff). Also check `git maintenance` schedules on the
  host: an incremental repack that deletes packs could still remove
  objects (VERIFY).
- **Ownership:** mounted files show host uids. git refuses a repository
  owned by another user (the old `NOTES.md` §21 "dubious ownership"
  incident). Set `safe.directory` for each mount path in
  `/etc/gitconfig` at create (RAN: the agent, uid 2001, gets "dubious
  ownership" on the 9p mount without it; `git -c safe.directory=*` on
  the command line did not help `clone --shared`; the system entry
  does).
- **Readability: a host file that is not world-readable is invisible to
  the agent** (RAN, 9p, `security_model=none`). Files show the host uid;
  the guest's default user has that uid, the agent does not. A blob in a
  `0700` object directory made `clone --shared` fail at checkout. Git
  makes `0755` object directories and `0444` packs under a `022` umask,
  so normal repositories work; a repository made under umask `077` does
  not.
  **Decision (cananian, 2026-10-09): the agent runs with the host
  user's uid** (and gid). Then mounted files are the agent's own, and
  files that leave the VM (a tar from the guest, `wmf-sbx cp`) carry the
  host uid when unpacked on the host.
  **The agent is in the golden image** (cananian, 2026-10-09: the uid
  changes very rarely, and on most single-user machines never). The host
  uid and gid are image inputs, so they are in the cache key, and a host
  with another uid gets another image. `image-build.sh` creates `agent`
  with them after it removes the builder's user; a group with the gid
  can exist already (macOS gid 20 is `dialout` on Debian), and is then
  reused. Lima gives its own user the host uid by default, so every
  template must set `user.uid` to something else: the builder uses
  59999 (RAN: without it, the builder's user got 30033, the host uid).
  `safe.directory` in `/etc/gitconfig` is still set, as defence in depth.
- **nono grant (contained mode):** the launcher adds
  `--read /run/wmf-sbx/host/…` for each mount.
- **The mount set is fixed at create.** Adding a repo later means
  `limactl edit` plus a restart. VERIFY the flags; sbx had the same
  limit.
- **What it buys:** no `wmf-sbx push`. In-VM `git safe-reset local` sees
  every commit the host has fetched, as today.
- **VM→host is unchanged:** `git-remote-wmfsbx` (§6.2). There is no
  writable mount, ever.

**D11. Host-enforced egress for `--sudo`: QEMU `restrict=on` plus a
host filtering proxy. DECIDED; after the MVP.**

**Why.** A root agent can remove every control inside its guest (D9).
The filter has to sit where root cannot reach it: in the QEMU process
and in a proxy on the host. That gets back the property sbx had, root
inside the VM with egress allowlisted from outside, and on Linux it is
stronger than contained mode, whose controls live in the guest.

**The mechanism.** QEMU's user-mode (slirp) network has two options:

- `restrict=on`: "the guest will be isolated, i.e. it will not be able
  to contact the host and no guest IP packets will be routed over the
  host to the outside", and it "does not affect any explicitly set
  forwarding rules". The second quote is READ, as cited in QEMU bug
  #1696746. The behaviour in the first is RAN (table below).
- `guestfwd=tcp:<guest-ip>:<port>-cmd:<command>`: QEMU starts
  `<command>` once for **each** guest connection to that virtual
  address, with the connection on its stdin and stdout.

**Use the `cmd:` form, not `tcp:` (RAN).** An earlier draft used
`guestfwd=…-tcp:127.0.0.1:<port>`. That form connects one QEMU chardev
to the target, once, and does not reconnect. The first guest CONNECT
worked. Then a second CONNECT (to a host the proxy denies) reached QEMU
but never reached the proxy as a new request, three more got no answer,
and the host had zero connections to the proxy port. A proxy needs one
host connection per guest connection. A single shared stream is also a
risk: a later request can go into an earlier, allowed tunnel. With
`cmd:socat - TCP:127.0.0.1:<port>`, three CONNECTs in sequence each
reached the proxy (200), a denied host got 403, and five at the same
time all got 200. `cmd:` needs `socat` (or `nc`) on the host. Keep the
command a fixed string that the wrapper writes; no guest data goes into
it.

With `restrict=on,guestfwd=tcp:192.168.5.100:3128-cmd:socat - TCP:127.0.0.1:<proxy port>`
(all RAN, after guest root ran `iptables -t nat -F; iptables -F;
nft flush ruleset`):

| Probe from the guest | Without `restrict` | With `restrict=on` |
| --- | --- | --- |
| host loopback service via `host.lima.internal` | **reached** | refused |
| `192.168.5.2:22` | — | refused |
| external TCP `140.82.112.3:443` | — | `Network is unreachable` |
| slirp DNS `192.168.5.3:53`, UDP and TCP | — | no answer / refused |
| host's nameserver (`8.8.8.8:53` UDP) | — | unreachable |
| `guestfwd` address `192.168.5.100:3128` | — | connects |
| proxy stopped, then a CONNECT | — | fails (curl exit 56): fail-closed |
| `limactl shell`, SSH, ControlMaster | works | works |
| 9p mount | works | works |
| boot scripts and all Lima readiness checks | pass | pass |
| guest port 4000 forwarded to host `127.0.0.1:4000` | — | works |

So:

- the proxy is the guest's only way out;
- `host.lima.internal` (host loopback) is cut;
- a proxy that is down means no network: this fails closed;
- Lima's SSH is a host-to-guest forward, so `limactl shell`, the git
  helper, port forwarding and the 9p mounts keep working. Nothing Lima
  needs depends on outbound routing.

**Injecting it.** Lima has no setting for `-netdev` options. Lima does
document `QEMU_SYSTEM_X86_64` / `QEMU_SYSTEM_AARCH64` (READ) for the
QEMU binary path, and v2.2.1 honours it (RAN). Lima's command line has
exactly one user-mode network (RAN):

```
-netdev user,id=net0,net=192.168.5.0/24,dhcpstart=192.168.5.15,hostfwd=tcp:127.0.0.1:<port>-:22
-device virtio-net-pci,netdev=net0,mac=…
```

If `networks:` names a Lima `user-v2` network, Lima uses
`-netdev socket,…` instead (READ, `pkg/driver/qemu/qemu.go:789-796`).

`sbx/helpers/wmf-sbx-qemu`:

- finds Lima's `-netdev user,…` argument, appends `restrict=on` and the
  `guestfwd`, and execs the real QEMU;
- refuses to start (exit non-zero) if it finds no `-netdev user`, or
  more than one, or any second network device. A silent pass-through
  would leave the guest unfiltered;
- **passes Lima's probe calls through unchanged.** Before the boot,
  Lima runs the same binary as `--version`, `-M none -accel help`,
  `-M none -netdev help` and `-cpu help -machine …` (READ,
  `qemu.go:296-350, 1256`). Apply the one-`-netdev user` rule only to
  the call that boots a VM (for example, one with `-pidfile` or
  `-qmp`). A test wrapper that applied it to every call refused
  `-netdev help`; Lima only warned, because it uses that output only for
  `socket_vmnet` (`qemu.go:837`). A refused `-accel help` stops the
  start. `--version` passed through (RAN);
- `wmf-sbx start`/`resume` set the variable for `limactl start` on
  `--sudo` sandboxes only.

A host without KVM needs no wrapper for acceleration: Lima v2.2.1
falls back to TCG itself (READ, `qemu.go:1191`; RAN). Lima 1.2.1 did
not.

Longer term, file a Lima feature request for a native egress-proxy or
`restrict` option, and drop the wrapper when it lands.

**DNS.** The guest does not need DNS: the proxy resolves hostnames for
`CONNECT`. With `restrict=on`, slirp's DNS at `192.168.5.3` does not
answer on UDP or TCP (RAN), so DNS tunnelling at the slirp level is
closed. Set Lima's `hostResolver.enabled: false` anyway. The host
resolver works by a guest iptables DNAT from `192.168.5.3:53` to
`192.168.5.2:<port>` (host loopback; RAN), which `restrict=on` cuts,
so with it on every lookup waits for a timeout.

**The proxy.**

- **Per sandbox,** started by `wmf-sbx start`/`resume`, stopped by
  `stop`/`rm`, on a free host loopback port recorded in state. Each has
  its own allowlist file and log under
  `~/.local/state/wmf-sbx/proxy/<name>/`.
- **First version: hostname allowlist on `CONNECT`, no TLS
  interception.** That is sbx's level of control. Use squid with a
  `dstdomain` ACL (the repo already has an allowlist squid in
  `templates/docker-egress/`), or a small Python CONNECT proxy if squid
  is too heavy a host dependency.
- **The allowlist** is the union of: nono's profile hosts (read with
  dict entries handled; see `HANDOFF.md` §2.2), the Anthropic hosts, the
  package registries (D3's list), and the Debian mirror for `apt`.
  `repos.yaml` may add more.
- **Guest config** (written at create, as root): `http_proxy`,
  `https_proxy` and `no_proxy=localhost,127.0.0.1` in
  `/etc/environment`; an apt proxy file; git `http.proxy` in
  `/etc/gitconfig`. npm, composer and curl read the environment. VERIFY
  that Claude Code honours `HTTPS_PROXY`. Root can change these
  settings, but cannot route around the proxy, so a changed setting only
  breaks networking. That is fail-closed.
- **Later, and in our control:** method and path rules like nono's
  (GET/HEAD-only wikis, POST only where reads need it). This needs TLS
  interception (squid ssl-bump or mitmproxy) and a CA trusted in the
  guest. It is the fix for the remaining exfiltration path, uploads to
  an allowed host. Not in the first `--sudo` release.

**Contained mode does not use D11.** nono's own proxy would have to
chain to the host proxy, and whether nono supports an upstream proxy is
unknown. Contained sandboxes keep nft + nono, on `vz` or QEMU.

**D12. Copy-on-write disks: qcow2 overlays on an immutable golden image,
QEMU only. DECIDED; after the MVP.**

**Why.** A full copy per sandbox costs gigabytes of disk and the time to
write them, on every `create`. An overlay stores only the blocks one
sandbox changes. Before Lima v2.0, `diffdisk` was a qcow2 overlay on
`basedisk`, and a fresh instance used about 196 KB. Measured on Lima
2.2.1 with method B: 196 KiB after create, 20 MiB after first boot,
and about 220 MiB after the guest wrote 200 MiB (RAN).

**Why QEMU only.** Only the QEMU driver boots qcow2. `vz` and `krunkit`
need raw disks, and Lima converts qcow2 to raw for them (READ, the
companion note). So on QEMU a sandbox gets an overlay, and on `vz` it
keeps the MVP's full copy. QEMU now carries two advantages, D11 and
D12, and the user's choice on macOS is: `vz` for speed and virtiofs,
QEMU for the host-side firewall and cheap disks.

**The mechanism** (the companion note's procedure, method B; RAN on
Lima 2.2.1, all seven success checks pass):

1. The golden image is a qcow2 file with no backing file, written once
   by `image build` from a builder VM, then made read-only
   (`chmod a-w`) and **never booted again**.
2. Each sandbox gets
   `qemu-img create -f qcow2 -F qcow2 -b <absolute golden path> <overlay>`.
   The backing path must be absolute, so that the overlay resolves
   from `~/.lima/<name>/`.
3. Attach it by **method B**: `limactl create` with a **placeholder**
   image, then replace `~/.lima/<name>/disk` with the overlay. The
   placeholder is one shared empty qcow2 with no backing file
   (`qemu-img create -f qcow2 placeholder.qcow2 1G`, 196 KiB), so
   `create` does not copy the golden file only for us to delete it.
   Create plus swap took about 80 ms, and the new `disk` was 196 KiB
   (RAN). At `limactl start`, Lima resizes `disk` to the `disk:` size
   with `qemu-img resize`, and the backing file stays (RAN). You can
   also give the overlay its final size at `qemu-img create`.
   - **Method A is not possible** on Lima v2.2.1. The QEMU driver's
     `EnsureDisk` calls `AcceptableAsBaseDisk`, which refuses any image
     with a backing file, a qcow2 external data file, or VMDK extents
     (READ, `pkg/qemuimgutil/qemuimgutil.go:246-295`). `create` fails
     with "must not have a backing file" (RAN). The check is
     deliberate: it closes the CVE-2023-32684 class.
   - Method B works because Lima checks only at create time. At start,
     `prepareDisk` reads the virtual size and nothing else (READ,
     `pkg/instance/start.go:511`). Lima still expects overlays on
     migrated instances: a legacy `basedisk` "may remain as qcow2
     backing file" (READ, `filenames.go:43`).
4. `create` verifies the result:
   `qemu-img info --backing-chain ~/.lima/<name>/disk` must name the
   golden file, after create and again after the first start. If it
   does not, fail the create; do not fall back silently to a flattened
   disk. On a running instance, add `-U` (`--force-share`), or
   `qemu-img` cannot get the lock.

**What this changes in our design:**

- **The image cache becomes immutable and referenced.** Each cache
  entry (§5.1) is one golden file at a fixed absolute path, keyed by
  content hash. A rebuild with different content makes a new file. A
  file is never rewritten in place. A write to a golden image
  **corrupts every overlay on it**.
- **`image rm`/`prune` refuse an image that any sandbox's overlay
  names.** Check the state files, or `qemu-img info` on each instance's
  `disk`.
- **The cache must not move.** The absolute path is baked into every
  overlay. If it ever has to move, use
  `qemu-img rebase -u -b <new path> -F qcow2` on each overlay.
- **Identity sealing in the golden image is now required, not tidy:**
  see §5.2 for the commands (RAN). Each overlay then gets its own
  machine-id and SSH host keys at first boot from Lima's per-instance
  cidata (the note's H6, RAN). D12 makes this a tested requirement.
- **`status` checks** that a QEMU sandbox's `disk` still names its
  golden image, and that the golden file is read-only and unchanged
  (record its checksum at build).
- **Lima upgrades:** this relies on undocumented Lima internals: the
  backing-file check runs at create but not at start. A later Lima can
  add it to the start path, and method B then fails. Tested with Lima
  2.2.1. `create` warns on a different Lima version until the D12 tests
  are rerun on it.
- **Never accept an overlay or a golden image from outside.** Method B
  goes around Lima's backing-file check. That is safe only because our
  code makes the overlay and our builder makes the golden image.
- **The source questions are answered** in the companion note (READ,
  Lima v2.2.1): the QEMU driver never converts to raw; the downloader
  copies a local path; Lima rejects a backing file at create and never
  flattens it.

**Optional, for `vz`:** cheaper full copies. On APFS, `cp -c` makes an
instant clonefile copy that shares unchanged blocks. On Btrfs or XFS,
`cp --reflink=always` does the same on Linux. Copying a stopped raw
golden disk this way into a new instance's `disk` (method B) would
give `vz` sandboxes most of D12's benefit. Not planned; listed for when
`vz` create time matters.

**Security.** The golden image is a host file that every QEMU sandbox
reads through. Guests cannot write it: QEMU opens a backing file
read-only, and no mount exposes the cache (D10 mounts only git dirs).
The CVE-2023-32684 rule in D1 still applies: never build a golden image
from a sandbox's disk.

## 4. The command map

`sbx/bin/wmf-sbx` becomes a plain dispatcher. Drop `--upstream`, the
real-`sbx` lookup and the `--cloud` refusal; keep the harmless
`unset SSH_AUTH_SOCK`. Every verb checks that `<name>` is a sandbox
**wmf-sbx owns**: there must be a state file in
`~/.local/state/wmf-sbx/sandboxes/`. That stops `wmf-sbx rm` from
touching Kosta's `wmf-claude` VM.

| Verb | Today (READ) | On Lima |
|---|---|---|
| `create PRIMARY [EXTRA…]` | resolve, deps, kit, `sbx create`, port, remotes | 1. resolve and walk deps (unchanged). 2. Find or build the golden image. 3. Generate the instance config: mounts (D10), resources, `agentSudo`. 4. Give the instance its disk (a full copy, D2; a qcow2 overlay on QEMU after D12), and boot. 5. Suspend host gc. 6. In-VM setup (§7). 7. Login (D6). 8. Add host remotes. Keep `--name`, `--no-deps`, `--no-dev`, `--no-suggests`, `--reset-all`, `--no-remotes`, `--dry-run`, `--config`. New: `--sudo`/`--no-sudo` (`--sudo` forces QEMU; refused until after the MVP) and `--vm-type vz\|qemu` (contained mode only; default: Lima's default for the host). `--kit`/`--kit-out` become `--image`/`--image-out`. |
| `start NAME` | restart, restore, re-point | `--sudo`: start the host proxy first, then `limactl start` with `QEMU_SYSTEM_*` pointing at the wrapper (D11). Then invariant checks: mount types and read-only flags as configured. Contained: agent not in `sudo`/`docker`, nft table loaded. `--sudo`: a probe from the guest to a non-allowlisted host and to `host.lima.internal` fails, and one to an allowlisted host through the proxy succeeds. |
| **new** `stop NAME` | (sbx idle-stopped on its own) | `limactl stop`, then stop the sandbox's proxy. |
| `resume NAME [-- ARGS]` | start + restore + `sbx run` | `start` if needed, then the launcher for the sandbox's mode (§8). Keep `default_agent_args`, `--continue` and `set_attached`. |
| `run --name NAME` | alias | unchanged |
| `exec NAME -- CMD` | start + restore + `sbx exec` | runs as agent, outside nono. `--engineer` refuses an agent-writable cwd in contained mode, and is meaningless with `--sudo`; say so. |
| `cp [-L] SRC DST` | `sbx cp` + `NAME:PATH` shortcuts | keep `resolve_cp_arg`; use `limactl copy` through an engineer-owned `mktemp -d`. |
| `rm NAME` | guard, `sbx rm`, remotes | the same guard (through the helper), remove remotes, **resume host gc**, `limactl delete --force`, stop the proxy and delete its state, delete state. |
| `ls-remotes`, `resolve` | — | unchanged |
| `refresh-claude-md`, `settings`, `--upstream` | sbx-specific | **retire** |
| **new** `login NAME` | — | D6 |
| **new** `image build\|ls\|rm\|prune` | — | §5. After D12, `rm` and `prune` refuse an image that any sandbox's overlay names, and `ls` shows which sandboxes use each image. |
| **new** `status NAME`, `shell NAME [--agent]` | — | thin wrappers |

There is no `push` verb: D10 makes it unnecessary.

## 5. The image pipeline

### 5.1 Cache key

Cache on kit content, not the repo list (`DESIGN-template-caching.md`
§3). The key is a hash of:

- the base image URL and digest;
- the package list;
- `.nono-version`;
- the bytes of the helper scripts;
- the wmf-claude tree revision, after the overlay and patches;
- the Claude Code version (the installer is unpinned).

The cache lives at
`${XDG_CACHE_HOME:-~/.cache}/wmf-sbx/images/<hash>/golden.qcow2`, with
its manifest and a recorded checksum.

- Store it as **qcow2 with no backing file**, read-only (`chmod a-w`).
  The QEMU driver boots qcow2, and D12's overlays need a qcow2 backing
  file. For `vz`, Lima converts it to raw at create (READ, the companion
  note).
- **Immutable from the start, even in the MVP.** A cache entry is never
  rewritten. A changed key makes a new directory. This costs nothing
  now, and D12 depends on it.
- Resolve the path to an absolute path once, and never move the cache
  (D12).

The same image serves both D9 modes and both drivers. Sudo and proxy
settings are applied at create time, not baked in. VERIFY that one image
file boots under both `vz` and QEMU. If not, the driver joins the cache
key.

### 5.2 What goes in the image

- `kit.BASE_PACKAGES` (php plus extensions, composer, imagemagick,
  librsvg2-bin, git-review, php-wikidiff2, …), Node per
  `DESIGN-setup-steps.md` §7.2, git, jq, curl, python3-venv, nftables.
- No Docker unless decided.
- nono and Claude Code, as `guest-install.sh` installs them.
- `/opt/wmf-claude.<rev>`, built, with the overlay and patches applied.
- The helpers in `/usr/local/bin`. `mw-install-browser` and
  `mw-install-cypress` call `sudo apt-get`, and `mw-install-browser`
  links `/usr/bin/chromium` with sudo (READ). They work unchanged in
  `--sudo` sandboxes. For contained sandboxes, bake their apt
  dependencies in, and pre-install Chrome for Testing (about 420 MB), or
  use the install broker (D9).
- **Identity sealed, as the last step before the builder VM stops**
  (RAN, cloud-init 26.1):
  ```bash
  sudo rm -f /etc/ssh/ssh_host_*
  sudo cloud-init clean --logs --seed --machine-id
  ```
  `--machine-id` writes `uninitialized` to `/etc/machine-id`, and
  systemd makes a new one at next boot. `cloud-init clean` does **not**
  remove SSH host keys, so remove them yourself (`--configs
  ssh_config` removes the sshd config drop-in, not the keys). Copies
  and D12 overlays then get their own machine-id and host keys at first
  boot. The tests check that two sandboxes differ. Check the flags
  again on Debian's cloud-init version.
- Export: `qemu-img convert -O qcow2` from the builder's `disk` if it
  is raw. Then `qemu-img info` must show no backing file. On QEMU the
  builder's `disk` is already qcow2 if the base image was (RAN).

### 5.3 The per-sandbox Lima template

Our template (`lima/wmf-sbx.yaml`, generated per sandbox for mounts):

- `plain: false`, `mounts` read-only with a pinned `mountType`,
  `images:` pointing at the golden image, smaller defaults (4 CPUs,
  4 GiB), and the explicit no-forward keys.
- `vmType`: `vz` or `qemu` for contained sandboxes; always `qemu` for
  `--sudo`. `mountType` follows: `virtiofs` on `vz`, `9p` on QEMU.
  Debian 13's 6.12 kernel avoids the 9p breakage in Linux 6.9–6.11
  (READ, Lima `default.yaml`).
- The **same** security provisioning as Kosta's (users, nft, tmpfiles,
  TIOCSTI), shared through `lima/provision-common.sh` (§12).
- `agentSudo` adds `agent` to `sudo` with NOPASSWD, writes the D11 proxy
  settings, and sets `hostResolver.enabled: false`. Kosta's nft rules
  stay on in `--sudo` sandboxes too: they cost nothing, and they are the
  only control until root removes them.
- Our own static test, beside `tests/test-lima.sh`. It asserts:
  - every mount has `writable: false`;
  - `mountType` is `virtiofs` or `9p`, never `reverse-sshfs`;
  - no mount of `~` or of any host path that is not a git dir;
  - mount points under `/run/wmf-sbx/host/`;
  - the ignore-all `portForwards` rule is present;
  - `containerd` is off;
  - the `ssh` forwards are off;
  - every `--sudo` template has `vmType: qemu` and `hostResolver`
    disabled;
  - the wrapper refuses a command line without exactly one
    `-netdev user` (a unit test with sample Lima command lines).
- Avoid Lima's EXPERIMENTAL `base:` composition (READ); generate the
  YAML with `yq` or Python.

## 6. Git transport

### 6.1 Host → VM: the read-only mount (D10)

At create, as agent:

1. `git clone --shared /run/wmf-sbx/host/<path>/.git <host path>`, with
   `core.hooksPath=/dev/null`.
2. `configure_remotes`: `origin` = Gerrit/GitLab, `local` = the mount.
3. The branch rules stay as they are (`DESIGN-setup-steps.md` §8.2,
   `--reset-all`).
4. The verify half of the old `setup.py` (`alternates_of`, `verify_repo`
   minus the mount checks) still applies: it asserts the alternates
   resolve.

After the host fetches, the agent runs `git safe-reset local` (or
`git fetch local`) and sees it at once. Nothing to push.

**As built (phase 4, `repos.py`, `sandbox-repos.sh`):**

- The clone is on the branch the host checkout is on, or detached at
  its commit. The mount is the repository's git dir (`git rev-parse
  --git-common-dir`), so a worktree's branch is cloned from the main
  repository's git dir.
- One root script makes the clone directories (missing parents owned
  by the agent) and the `/etc/gitconfig` `safe.directory` entries; one
  script per owner clones. The clone at a host path under `/tmp`,
  `/run`, `/home/agent`, ... is refused (D8: the VM uses those paths,
  and `/tmp` is a tmpfs).
- A `:ro` repo is cloned by `engineer`. The agent can read it, and
  cannot write it. It gets a host remote too, because its clone also
  borrows the host's objects (so gc stays off for it).
- **The guest needs the generic Debian kernel.** The genericcloud
  image's cloud kernel has no 9p and no virtiofs: Lima writes the
  fstab entries, and nothing mounts (RAN). The golden image now has
  `linux-image-<arch>` and no cloud kernel; the build checks the
  modules, and an invariant checks that every fstab mount is mounted.
- **9p needs `cache: none`.** With Lima's default for a read-only 9p
  mount (`fscache`), the guest still read a loose ref after the host
  moved it into `packed-refs`, so `git fetch local` missed the host's
  commit (RAN). With `cache: none`, a plain commit, `pack-refs`, a full
  repack and `gc --prune=now` on the host were each seen at once, and
  `git log --all` over the mount took 0.5 s. The template sets it for
  every 9p mount, and `check_template` requires it. Lima writes the
  option to fstab at boot, so `limactl edit` takes effect only at the
  boot after the next one.

### 6.2 VM → host: `git-remote-wmfsbx`

URLs have the form `wmfsbx://<name>/<absolute path>`.

- The helper advertises only `connect` for `git-upload-pack`, and
  refuses `git-receive-pack`, so the remote is fetch-only.
- It runs
  `limactl shell --workdir / <name> -- sudo -H -u agent git -c core.hooksPath=/dev/null -c core.fsmonitor=false upload-pack <path>`.
- git ignores `uploadpack.packObjectsHook` from repository config (RAN,
  git 2.47 in the guest: the agent set it in the clone's config, the
  host fetched, and the hook did not run).
- The helper does not pass `GIT_PROTOCOL` to the VM, so upload-pack
  speaks protocol v0, which git accepts over `connect`. A fetch of one
  commit took 1.5 s (TCG).
- In a `--sudo` sandbox, treat the server as hostile (D9) and keep the
  host's git current.
- A tty-less `limactl shell` passes binary data cleanly (RAN, phase 0).
- A `:ro` clone is the engineer's: upload-pack opens it as `PATH/.git`,
  so `/etc/gitconfig` has `safe.directory` for that path too (RAN:
  without it, "dubious ownership", and the `rm` guard refused).

`git fetch <name>`, `git safe-reset <name>`, `git-review-check` and the
`rm` guard work as today. Port code goes: `refresh_host_port`,
`publish_daemon_port`, `ipv4_mapping`, `wait_for_daemon`,
`ensure_git_daemon`.

### 6.3 Host gc: keep it, and migration is easy

`suspend_gc`/`resume_gc` and the `wmfSbx.*` markers stay, because the
alternates are back. Old Docker-backend sandboxes' markers mean the same
thing, so no conversion is needed. Old `git://127.0.0.1:<port>` remotes
must be removed:

- run old-code `wmf-sbx rm` on each old sandbox before deleting the
  Docker backend, or
- add a one-shot `wmf-sbx migrate` that drops `git://` remotes carrying
  our ownership marker.

Tag the last Docker-backend commit `sbx-docker-final`.

## 7. In-VM setup: where each `setup.py` step goes

| Step | Runs as | When |
|---|---|---|
| packages, helpers, wmf-claude tree, nono, Claude Code | root | image build |
| `agentSudo` sudoers entry, `/etc/gitconfig` `safe.directory` for the mounts, mirrored parent directories (root-owned) | engineer (sudo) | create |
| `--shared` clone, `configure_remotes`, `git_safe_reset`, alternates check | agent | create |
| `link_into_core`, `link_parsoid_checkout`, `write_composer_local` | agent | create |
| `composer_update`, `npm_install`, `phpunit_config`, `write_env_file`, `install_mediawiki`, `write_api_testing_config` | agent (outside nono: D7 in contained mode) | create |
| settings merge: `enabledPlugins`, the ported denies, D4 `env` | agent | create, `update` |
| `~/.claude/CLAUDE.md` (Lima text, mode-aware), memory seed | agent | create |
| MCP registration | already done in the image by `bin/wmf-claude-setup` | — |

**Delete from `setup.py`:**

- the mount-over dance: `move_original`, `bind_over`,
  `bind_into_parallel_tree`, `remount_readonly`;
- `.sbx-originals`, the layout, restore and remount-verify code;
- `lock_shared_skills`;
- the git daemon;
- the CLAUDE.md edits;
- the exec-bits work.

The MediaWiki steps and the alternates verification survive.

**As built (phase 5):** the alternates check is in `sandbox-repos.sh`
(phase 4). `setup.py` keeps the MediaWiki steps (`--lima`), plus the
Docker kit's `--settings` and `--mcp`, which phase 6 keeps or deletes.
`create` sends `setup.py` into the VM on stdin (`mediawiki.py`), so it is
not in the image.

## 8. The session

**Contained (`--no-sudo`).** Start from Kosta's `guest-claude.sh`. As
agent, with no login shell and a fixed `PATH`, run:

```
bin/claude [--local-web=4000 --landlock-only] \
  --allow <each writable extra clone> --read <each :ro clone> \
  --read /run/wmf-sbx/host \
  -- <agent args>
```

nono grants only the launch directory. Every other clone, and the
mounts, need grants or the agent cannot reach them. Add
`WMF_CLAUDE_PROFILE=wmf-mediawiki` if D3 lands.

**Trusted VM (`--sudo`).** As agent, in the primary path, run:

```
claude --plugin-dir /opt/wmf-claude --settings /opt/wmf-claude/wiring/settings-merge.json <agent args>
```

Do this the way `bin/claude` assembles its Claude-side arguments, but
with no nono and no grants. Factor that assembly out of `bin/claude`, or
call `bin/launch-claude.sh` directly, rather than duplicating it.
The tool layer still applies; it is defense in depth, not a boundary,
which is the old sbx position. The proxy settings from D11 are in
`/etc/environment`, so Claude Code, the MCP servers and every tool reach
the network only through the host proxy. The launcher refuses to start
if the sandbox's proxy is not running.

**Context text.** Add a backend directory to the SessionStart seam,
e.g. `hooks/context/lima-sbx/`, with **two variants selected by mode**:

- contained: nono's sandbox text, kept byte-identical to
  `hooks/context/nono/sandbox.txt` by a test, plus one VM paragraph;
- trusted VM: "You are root in a disposable VM; install what you need
  with sudo. All network traffic goes through an allowlisting proxy on
  the engineer's host; a refused host gets a 403 from the proxy. Report
  it to the engineer, who can add it; do not look for a way around it".

`environment.txt` carries:

- the wiki paragraph (`composer serve &`, `MW_SERVER`, retry on `000`);
- the git paragraph: `local` is the engineer's checkout, read-only,
  current as of the host's last fetch; `git safe-reset local`;
- upstream's MCP bullet.

Edit `MEDIAWIKI-TESTING.md` and `HOME_CLAUDE_MD` to be mode-aware: the
sudo steps apply only with `--sudo`. The `run-tests` plugin patch still
applies (RAN).

**As built (phase 6, contained mode):** `hooks/context/lima-sbx/` is in
the upstream tree (cananian, 2026-10-10: edit shared files directly, no
overlay). The launcher is `session.launcher_argv`; besides the grants
above it needs `--read` on `/opt/claude-code`, `/opt/node` and
`/etc/php`, `--read-file` on `/etc/gitconfig` and `/etc/bash.bashrc`,
`--allow` on `~/.cache` and `~/.npm` (not `~/.config/composer`: the
profile denies its `auth.json`, and Landlock cannot deny a path under an
allowed one), the registry `--allow-domain`s (D3), and
`--upstream-proxy` when the host has a proxy. `COMPOSER_HOME` is
`~/.cache/composer-home`, in the settings `env`. The agent's
`~/.claude/CLAUDE.md` is `session.home_claude_md` (no sudo steps). The
`run-tests` patch is not applied: the plugin overlay and patches are
gone; check in phase 7 whether that skill needs a change upstream. The
memory seed (`sbx/reference/claude-memory-seed/`) is not used: it holds
memories about developing wmf-claude, not general ones.

**MCP.** Upstream's anonymous servers run in the VM. Drop the proxy, the
host registration and the node ≥ 20.18.1 host check. Propose tool-layer
denies for Gerrit's write tools upstream.

## 9. Security summary for the new `sbx/SECURITY.md`

**Closed compared with sbx:**

- SSH-agent forwarding;
- the unauthenticated git daemon on loopback;
- host-side MCP processes, the gateway meta-tools, credential spending;
- the shared skills store;
- the host-browser endpoint;
- the writable primary workspace and the post-restart writable window:
  nothing writable is mounted, and nothing is re-mounted by our code.

**Contained mode adds:**

- deny-by-default egress (nono) inside nft;
- no agent root.

**Residual in both modes:**

- a read-only mount of each repo's git dir: `.git/config`, hooks,
  stashes and reflogs are readable;
- read-only depends on host-side enforcement, proven by the §D10 bypass
  test per mount type;
- a host file server parses guest requests (virtiofs/9p);
- the Claude login in each VM (D6);
- DNS to Lima's resolver is open (the MR's residual too);
- NAT reaches host services on public addresses;
- Lima's SSH key in `~/.lima/_config/user`.

**`--sudo` mode (D9, D11):**

- egress is filtered **outside** the guest: QEMU `restrict=on` leaves
  only the `guestfwd` to the host proxy, so root cannot route around the
  allowlist, and host loopback is cut. On Linux this makes `--sudo` the
  stronger network mode;
- the proxy filters by hostname only, so uploads to an allowed host
  remain an exfiltration path. Method and path rules in the proxy are
  the planned fix (D11);
- every in-guest control is advisory: nft, the two-user split, file
  ownership;
- the guest side of ssh, scp and git is hostile;
- a larger hypervisor attack surface, and a QEMU wrapper of our own in
  the path.

**Contained mode adds:**

- create-time repo code outside nono (D7);
- `--landlock-only` for the wiki (D5).

Move the old document to `sbx/history/SECURITY-docker-sbx.md`.

## 10. Code disposition

Test counts are READ from `sbx/tests/`.

| Module / file | Fate | Tests |
|---|---|---|
| `resolve.py`, `deps.py`, `color.py`, `ls_remotes.py`, `git-safe-reset`, `git-review-check` | keep as they are | 78 + 56 + 11 + 14 + 9 + 11 |
| `state.py`, `remotes.py` | adapt: no ports, `wmfsbx://` URLs, record `agentSudo`; **gc suspension stays** | 27 + 47 |
| `create.py` | rewrite the sbx half (23 `WMF_SBX` call sites, MCP host registration, ports). Keep resolve, deps, `link_plan`, `canonicals_for_kit`, the plan, and the nested-mount conflict check (it now guards mount points) | 196, about half survive |
| `setup.py` | §7: keep the MediaWiki steps and the alternates verification; delete the mount dance | 245, most survive |
| `kit.py` | becomes `image.py` (packages, helpers, plugin staging, settings patch, CLAUDE.md text, cache key) plus `template.py` (instance YAML, mounts). **Fix `wiki_family_domains()` first** if the Docker backend must keep working meanwhile (`HANDOFF.md` §2.2); it is not needed after the port | 103, about half survive |
| `resume.py`, `start.py`, `exec.py`, `cp.py`, `rm.py`, `run.py`, `__main__.py` | adapt to `limactl` | 43 + 11 + 15 + 15 + 21 + 15 + 8 |
| `settings.py`, `refresh_claude_md.py`, `helpers/wmf-sbx-mcp-proxy`, `helpers/wmf-sbx-gateway-tools`, `patches/sbx-claude-md/` | delete | 17 + 12 + 43 |
| new: `git-remote-wmfsbx`, `image.py`, `template.py`, `lima.py` (a fakeable `limactl` wrapper) | new, MVP | — |
| new: `helpers/wmf-sbx-qemu` (the D11 wrapper), `proxy.py` (proxy lifecycle, allowlist assembly, guest proxy config) | new, track A | — |
| new: `disk.py` (overlay create, attach by method B with a placeholder image, backing-chain verification, in-use checks for `image rm`/`prune`) | new, track B | — |

## 11. Phases and exit criteria

### The MVP: contained mode (`--no-sudo`) only

`create --sudo` fails with "not yet; see HANDOFF-LIMA.md D11" until
phase A3. Every sandbox gets a full copy of the golden image (D2).

0. **Measure** (no code). Exit: results in `sbx/NOTES.md`, and D1, D3–D6
   answered.
   - that a local golden file works as `images: location` (H1 in the
     companion note): **done**, works on QEMU (RAN). Still to do: the
     time and bytes of a full-copy create on `vz` and on QEMU with KVM
     or HVF, the baseline for track B;
   - **the read-only bypass test** for virtiofs on `vz` and 9p on QEMU:
     **9p on QEMU passes** (RAN, D10). virtiofs on `vz` is still to do;
   - that `sudo` fails inside a nono session: **done** (RAN). nono sets
     `NoNewPrivs: 1`; sudo in a child process says "The 'no new
     privileges' flag is set, which prevents sudo from running as root"
     and exits 1. (nono also refuses `sudo` as the start command, but
     its own message says that check is deprecated and children
     bypass it; the flag is the control.);
   - `safe.directory` and uid readability on mounts: **done** (RAN; D10);
   - tty-less binary `limactl shell`: **done** (RAN). 8 MB of random
     bytes plus CR, NUL and ^Z went host to guest and back with the same
     SHA-256 and byte count. Lima's warnings go to stderr only;
   - port 4000 bind and connect under nono: **done** (RAN; D5);
   - headless Chrome under nono: **done** (RAN, Chrome for Testing
     `chrome-headless-shell` 155.0.8059.39, in the MR !127 Debian VM, as
     the agent, with the real `wmf-engineer` profile, `--open-port 4000`,
     landlock-only and `--read` on the Chrome directory). It loads a page
     from a server on 127.0.0.1:4000 in the same sandbox and runs its
     JavaScript, **only with `--no-sandbox`**. With Chrome's own sandbox
     it stops: "No usable sandbox": the setuid helper cannot work under
     nono's NoNewPrivs, and it finds no usable user namespaces. So nono
     is the only sandbox around the browser; the karma and wdio configs
     must pass `--no-sandbox`. The download needs
     `googlechromelabs.github.io` (version list) and
     `storage.googleapis.com` (the files), as in the sbx kit. Debian 13
     needs 16 shared libraries for it (`ldd`; the `t64` names for
     `libatk1.0-0`, `libatk-bridge2.0-0`, `libasound2`, `libatspi2.0-0`).
     Also found: the profile denies reading `/etc/mime.types`, so
     Python's `http.server` fails to serve files in the sandbox (a test
     harness problem, not a MediaWiki one);
   - whether the guest agent adds port forwards: **done** (RAN; D10).
1. **Rebase onto !127** (latest revision). RAN on the 2026-09-29 patch:
   it conflicts with main only in `CLAUDE.md`, and our rebased files
   merge onto main + !127 exactly as onto main. Exit: all three suites
   pass. Tag `sbx-docker-final`.
   **Done 2026-10-09** (`sbx/NOTES.md` §105). The base is the
   `lima-port-base` branch: main 10aefc1, !127 (rebased, clean), !132,
   !130, !131, and a lint fix for !127 kept as its own commit. The work
   is on the `lima-port` branch; the `sbx-docker-final` branch marks its last sbx
   commit. The sbx unit suite, `test-templates.sh` and `test-lima.sh`
   pass. `test-profile.sh` fails on Linux for the upstream reason in
   `sbx/NOTES.md` §104, on `main` too.
   **Baseline: MR !127 booted, unchanged (RAN, 2026-10-09).**
   `bin/wmf-claude-vm create` from `1a2c0f5`, Lima 2.2.1, QEMU TCG (no
   KVM). Two test-only changes to a local copy of the template, not
   committed: `caCerts` for the cloud sandbox's egress CAs, and one
   nft accept for the sandbox's HTTP proxy at `192.168.5.2`.
   - Times (TCG): image download and first boot with provisioning
     11.5 min; the planned restart 2 min; the whole `create` 18 min.
   - Boundary checks from `docs/lima-vm.md` pass: no host mounts; the
     agent has no sudo and cannot open the Docker socket; the engineer
     runs Docker 29.9.0; `192.168.5.2` is refused; the host has only
     the SSH forward.
   - **Bug, fixed on `lima-port-base`** (`de6eedb`, its own commit, for
     Kosta): `guest-install.sh` ran the Claude Code installer as
     `bash -c 'curl … | bash'` without pipefail, so a failed download
     printed "installed".
   - **Proxies do not reach the agent.** The agent steps run through
     `sudo -u agent env …`, which drops `https_proxy`. On a network
     that needs a proxy, the Claude Code install and `nono pull` fail.
     Here they were finished by hand with the proxy variables passed.
   - **Debian 13 genericcloud has no `iptables`,** so Lima's host
     resolver (an iptables DNAT) is not set up. DNS still works through
     slirp, but `host.lima.internal` does not resolve:
     `docs/lima-vm.md` expects curl exit 7 there, and gets 6.
2. **Image builder** and `image …`. Exit: a sealed, read-only qcow2
   golden image with no backing file boots under `vz` and QEMU; two
   sandboxes from it have different machine-ids and host keys; `status`
   passes.
   **Done on Linux/QEMU, 2026-10-09** (`sbx/NOTES.md` §106): `wmf-sbx
   image build|ls|rm|prune` (`image.py`, `lima.py`, `image-build.sh`).
   Image `d16a1c5496fa83fe`: qcow2, no backing file, mode 0444, 2.3 GiB
   (20 GiB virtual). Two full-copy sandboxes from it have different
   machine-ids and host keys; `agent` has the host uid. A second `image
   build` hits the cache in 2 s. Still to do: `vz` (the Mac list), and
   `status` (it comes with phase 3).
3. **Lifecycle:** `create --no-deps`, `start`, `stop`, `exec`, `cp`,
   `rm`. Exit: round trip on `vz` and on QEMU.
   **Decision (cananian, 2026-10-09): Lima only, from phase 3.** The
   verbs move to Lima now; they do not keep a Docker path beside it.
   The `sbx-docker-final` branch keeps the Docker backend for anyone
   who needs a root agent before track A. Until phase 6, `resume` and
   `run` refuse with "not ported yet" rather than call Docker `sbx`.
   **Done on Linux/QEMU, 2026-10-09** (`sbx/NOTES.md` §107): `create`,
   `start`, `stop`, `status`, `ls`, `exec`, `cp`, `rm` on Lima
   (`vm.py`, `template.py`, the verbs, a plain `bin/wmf-sbx`). The real
   round trip passed; `vz` is on the Mac list.
4. **Git transport and mounts.** Exit:
   - after a host `git fetch`, the agent sees the new commit with no
     host action;
   - `git safe-reset <name>` on the host works;
   - the `rm` guard works;
   - host gc is suspended while the sandbox lives and restored after;
   - the static template test passes.
   **Done on Linux/QEMU, 2026-10-09** (`sbx/NOTES.md` §108): the
   read-only git-dir mounts, the clones at the host paths, the `:ro`
   clones, `git-remote-wmfsbx`, the host remotes and gc suspension
   (`repos.py`, `sandbox-repos.sh`, `remote_helper.py`). From a clean
   image: `create` 247 s, then `lima-port/checks/phase4.sh` 18 of 18
   ok, and `rm` left the host config clean. Found and fixed: the cloud
   kernel has no 9p (the image now has the generic kernel); 9p needs
   `cache: none`; a `:ro` clone needs `safe.directory` for `PATH/.git`;
   an image built from a checkout without its submodules had no MCP
   servers. `vz` is on the Mac list.
5. **MediaWiki setup.** Exit: `wmf-sbx create Translate` serves
   `Special:Version`.
   **Node: DECIDED 2026-10-09, one global Node 24.18.0** (cananian
   agreed to the recommendation; `sbx/NOTES.md` §109). WMF CI uses
   Node 24, not 22:
   - **WMF CI** (integration/config, READ 2026-10-09): `zuul/layout.yaml`
     says "our current CI testing targets are Node 24". The
     `mediawiki-node24` jobs run on the `node24-*` images (194 references,
     at 24.18.0); `mediawiki-node26` is in `experimental` only ("Not-yet-
     supported language runtimes", 26.8.2); nothing names Node 22.
   - **Deployed repositories** (codesearch `deployed`, `"node":` in
     `package.json`, 24 lines in 19 repositories): MobileFrontend pins
     exactly `24.18.0` (`engines` and `.nvmrc`); citoid `24`; cxserver,
     Function Orchestrator and function-schemata `>=24`; Popups
     `>=20.19.5`; CirrusSearch, Wikibase parts, change-propagation and
     iPoid `>=20` or `>=18`; old services (restbase, parsoid,
     mobileapps, push-notifications) name Node 6 to 12, which nothing
     runs. MediaWiki core names no Node. In wmf-claude, mcp-phabricator
     wants `>=20`, and chrome-devtools-mcp 0.23.0 wants
     `^20.19 || ^22.12 || >=23`.
   - **Options** (from cananian): nave (a per-version Node tree,
     selected per shell), fresh-node (a container-like Node
     environment), a newer Debian or Ubuntu package pinned with apt
     preferences, or one global Node.
   - **Why one global Node:** CI has one Node, so one Node is the right
     model, and `npm ci`/`npm test` then work in every repository and
     every non-interactive shell with nothing for the agent to learn. A
     contained agent cannot download a Node (egress is TCP 80/443
     through nono), so a version manager would need every version in
     the image anyway. fresh-node needs a container runtime, which the
     sandbox does not have. A Debian `experimental` package can bring
     newer libraries and is not a stable pin; NodeSource adds an apt
     source and a signing key for nothing the tarball does not give.
   - **As built:** the nodejs.org tarball of `image.NODE_VERSION`,
     checked against the checksum from the release's `SHASUMS256.txt`
     (version and checksum are image inputs, so in the cache key), at
     `/opt/node/v<version>`, with `node`, `npm`, `npx`, `corepack` in
     `/usr/local/bin`. Debian's `nodejs` and `npm` are not installed
     (Debian's `npm` brought a few hundred `node-*` packages). The build
     and an invariant check that the agent's `node` is the pinned one.
     24.18.0, not the newest 24.x (24.21.0): CI and MobileFrontend use
     24.18.0. **Change `NODE_VERSION` when the CI images change.**
   - **If a repository ever needs another Node:** a second pinned tree
     under `/opt/node/`, selected by that repository's `.nvmrc` through
     one wrapper. Not needed now.
   **Done on Linux/QEMU, 2026-10-09** (`sbx/NOTES.md` §110): `wmf-sbx
   create Translate` gives a wiki that serves `Special:Version` (200),
   with Translate, UniversalLanguageSelector and Vector loaded.
   `setup.py --lima` runs in the VM as the agent (`mediawiki.py`); the
   Docker-only parts of `setup.py` are deleted (§7). The global Node is
   24.18.0, as CI (`sbx/NOTES.md` §109). Found: this cloud sandbox's
   proxy blocks GitHub archives, so composer was set to
   `preferred-install: source` for the test.
6. **The session.** Exit: MCP calls answer; the SessionStart text is
   the contained variant.
   **Done on Linux/QEMU, 2026-10-10** (`sbx/NOTES.md` §111): `wmf-sbx
   resume` and `run --name` (`resume.py`, `session.py`);
   `hooks/context/lima-sbx/`; at create, `wmf-claude-setup` and the
   session files. In a real session in the VM, under nono, with a test
   API key: the lima-sbx SessionStart text and `~/.claude/CLAUDE.md` are
   in the context; `mcp list` shows the three servers connected; a
   Phabricator tool call returns the task; Claude starts `composer serve`
   and gets 200 from `Special:Version`; git works; the D4 variables are
   set; the agent commits as the engineer (`user.name`/`user.email` of
   the host, in the settings `env`). From a new image, `create` then
   `resume` passed the same checks, and the host fetched the agent's
   commit. Found and fixed: the grants that the wmf-engineer profile lacks
   on Linux (`/etc/php`, `/etc/gitconfig`, `/opt/claude-code`,
   `/opt/node`, the agent's caches); nono's `--upstream-proxy` for a host
   proxy; the proxy port baked into the VM. To do in phase 7:
   `mw-install-browser` and `mw-install-cypress` use sudo (apt, links in
   `/usr/bin`), which a contained agent does not have.
7. **MVP acceptance:** the blind run of
   `DESIGN-testing-instructions.md` §9, in a contained sandbox, on `vz`
   and on QEMU.

### Tests that need a Mac (ask Kosta)

cananian works on Linux, so every macOS item is collected here, to ask
Kosta Harlan to run. Each one is also named in its phase. Record Lima,
QEMU and macOS versions with the results.

- **Phase 0:** the time and bytes of a full-copy `create` on `vz` (the
  baseline for track B); the read-only bypass test for virtiofs on `vz`
  (D10: guest root `mount -o remount,rw`, then a write; the host file
  must not change).
- **Phase 2:** the same sealed golden image boots under `vz`, and two
  sandboxes from it on `vz` have different machine-ids and host keys.
- **Phase 3:** the lifecycle round trip (`create`, `start`, `stop`,
  `exec`, `cp`, `rm`) on `vz`.
- **Phase 4:** the git-dir mounts on `vz` (virtiofs): they mount; a
  host commit, `pack-refs`, a full repack and `gc --prune=now` are each
  seen at once by `git fetch local` in the guest (9p needed
  `cache: none` for this; virtiofs may have its own cache setting);
  `lima-port/checks/phase4.sh` does these checks: the agent cannot write the
  mount, guest root's `remount,rw` cannot either, `git fetch NAME` and
  the `rm` guard work.
- **Phase 6:** `wmf-sbx resume` on `vz`: the session starts under nono,
  `mcp list` shows the servers connected, and Claude reaches the wiki
  (`composer serve`, then `curl $MW_SERVER`). On macOS the agent's
  paths differ, so check that the grants in `session.py` are enough.
- **Phase 7:** the blind run of `DESIGN-testing-instructions.md` §9 in
  a contained sandbox on `vz`.
- **Track A (A1):** the D11 measurements on macOS QEMU with HVF:
  `restrict=on`, the `cmd:` guestfwd, slirp DNS, and that virtiofs is
  not available (so the mounts are 9p there too).
- **Track B (B1):** method B on macOS with `vmType: qemu` and HVF, the
  seven success checks, and real times.
- **D12, optional:** APFS `cp -c` (clonefile) copies of a stopped raw
  golden disk into a `vz` instance's `disk`.

### After the MVP: two independent tracks

Both tracks touch only QEMU sandboxes, and neither depends on the other.
Do them in either order, or in parallel.

**Track A: `--sudo` (D9 + D11)**

- **A1. Measure D11** (no code). Exit: results in `sbx/NOTES.md`.
  Mostly **done** on Linux with TCG (RAN; results in D11):
  - Lima's QEMU command line (`ps`): one `-netdev user`, SSH by
    `hostfwd`. **Done**: as believed;
  - with `restrict=on` added: SSH, `limactl shell` and 9p still work;
    nothing Lima needs breaks. **Done**: all work, and port forwarding
    too;
  - **DNS with `restrict=on`**: does slirp still answer? **Done**: no,
    on UDP or TCP;
  - `guestfwd` to a host port carries a `CONNECT` through. **Done**:
    only with the `cmd:` form; the `tcp:` form carries one connection
    only;
  - `host.lima.internal` and a non-allowlisted host are unreachable,
    even after `sudo nft flush ruleset` in the guest. **Done**:
    unreachable;
  - Claude Code works through `HTTPS_PROXY`. **Still to do** (needs a
    host where the guest may reach the internet);
  - still to do: the same on macOS with HVF, and a check that the
    wrapper refuses a `user-v2` network.
- **A2. Wrapper and proxy:** `helpers/wmf-sbx-qemu`, `proxy.py`, the
  allowlist assembly, the guest proxy config, and `start`/`stop`/`rm`
  managing the proxy. Exit: the `start` probes (§4) pass; the wrapper's
  refusal tests pass.
- **A3. `--sudo` end to end:** `create --sudo` enabled, the trusted-VM
  launcher, the sudo context text. Exit: `mw-install-browser` runs on
  demand through the proxy; the blind run passes in a `--sudo` sandbox
  on Linux.

**Track B: copy-on-write disks (D12)**

- **B1. Research and measure** (no code). Answer the companion note's
  source questions from Lima's v2 tree. Run its procedure on Linux and
  on macOS with `vmType: qemu`. **Done on Linux with TCG** (READ and
  RAN; results in the companion note): method A is refused by Lima,
  method B passes all seven checks, and H1–H4 and H6 held. Still to do:
  macOS with `vmType: qemu`. Exit: its seven success checks recorded in
  `sbx/NOTES.md`, with the Lima, QEMU and host versions and which of
  H1–H6 held:
  - the backing file survives create and start;
  - a fresh `disk` is MB, not GB;
  - the guest boots, and the shell, mounts and SSH work;
  - two overlays run at once;
  - each has its own machine-id and host keys;
  - writes stay in their own overlay, and the golden checksum is
    unchanged;
  - stop, start and delete work, and delete leaves the golden image.
- **B2. Implement:** `disk.py` with method B and a placeholder image; `create`
  verifies the backing chain and fails rather than flatten; the
  in-use checks for `image rm`/`prune`; the `status` checks; the
  Lima-version warning. Exit: unit tests with sample `qemu-img info`
  output; a create on QEMU is faster and smaller than the phase-0
  baseline.
- **B3. Acceptance:** the blind run in an overlay-backed sandbox. Then
  rebuild the golden image (a new hash) and confirm that existing
  sandboxes still boot on the old file, and that `prune` keeps it until
  they are gone.

**After both tracks**

- **C1. Delete** the Docker-backend code. **Split and moved earlier
  (cananian, 2026-10-09).** The first reason to wait (the Docker backend
  is the only way to get a root agent until A3) no longer holds on this
  branch: since phase 3 wmf-sbx cannot reach the Docker code, and the
  `sbx-docker-final` branch keeps it.
  - **C1a, the code: before phase 4.** Delete the Docker-only modules,
    functions and their tests (the kit and ports in `kit.py` and
    `create.py`, `settings.py`, `refresh_claude_md.py`, the MCP proxy
    and gateway helpers, `resume.py` and `run.py`; phase 6 writes the
    Lima `resume`).
  - **C1b, the documents: at phase 7,** when the design stops moving.
    Rewrite `sbx/README.md` and `sbx/SECURITY.md`. The rules for the
    notes and design documents (cananian, 2026-10-09):
    - `sbx/NOTES.md` keeps only actionable decisions and findings, and
      the to-do items that still apply. No history: remove entries for
      decisions that were changed later, and the Docker-sbx entries that
      no longer apply.
    - Remove each `DESIGN-*` document whose design is implemented. Put
      a short description in a "Design" or "Architecture" section of
      `sbx/README.md`, where it helps a reader understand the structure
      of the code. (`DESIGN-template-caching.md` is superseded by §5 and
      D12.)
    - Until phase 7, keep the sbx history that the port still uses to
      understand earlier decisions. Remove what the port no longer
      needs, as it becomes unnecessary.
- **C2. Later:** method and path rules in the proxy (TLS interception, a
  guest CA), to close the upload-to-allowed-host path (D11). Optionally,
  clonefile or reflink copies for `vz` sandboxes (D12).

## 12. Upstreaming

Propose upstream (they benefit Kosta's mode as well):

- `lima/provision-common.sh`: one copy of the security provisioning,
  tested once;
- `profiles/wmf-mediawiki.json` (D3), if agreed;
- Gerrit write-tool denies;
- the SessionStart seam's backend directories, so his mode gets a
  `lima` context.

**Do not propose** D9 (`--sudo`), D10 (mounts) or D11 (the QEMU wrapper)
for his template. D9 and D10 contradict his threat model by design, and
D11 exists to serve D9. They live in `lima/wmf-sbx.yaml`, our helpers,
and their own tests, and the docs say why.

**Upstream to Lima:**

- a feature request for a native egress option on the QEMU user network
  (`restrict` plus `guestfwd`, or a proxy setting), and if possible on
  `vz`'s gvisor-tap-vsock network too. It would remove the wrapper, and
  make D11 available on `vz`;
- a feature request for a supported shared-base-disk option on QEMU (an
  overlay on a named read-only image). Lima v2.0 removed overlays to cut
  differencing I/O, and sharing one basedisk was never implemented
  (READ, PR #4206 via the companion note). Lima v2.2.1 also refuses a
  backing file at create, on purpose (READ), so the request must keep
  that protection: only an image that the user names in the instance
  config, never one that a downloaded image names. A supported option
  would replace D12's reliance on the create-only check (method B).

## 13. Still unverified

These items are listed above; this collects them.

- **Lima:** read-only enforcement and freshness for virtiofs on `vz`
  (9p on QEMU passes both with `cache: none`, RAN); one image booting
  under both drivers. Done: the guest agent adds no port forward past
  the ignore rule; tty-less binary `limactl shell`; `limactl edit
  --set '.mounts[]…'` changes the mounts (it takes effect at the second
  boot, RAN).
- **Lima + QEMU disks (D12, phase B1):** method B on macOS with
  `vmType: qemu`, and on KVM or HVF for real times. On Linux with TCG,
  H1–H4 and H6 held, H5 failed (method A refused), and the source
  questions are answered (RAN, READ).
- **Lima + QEMU network (D11, phase A1):** the same tests on macOS with
  HVF; the wrapper's refusal of a `user-v2` network. On Linux with TCG
  the rest is done (RAN).
- **Claude Code:** works through `HTTPS_PROXY`.
- **nono:** done: `--upstream-proxy HOST:PORT` chains nono's proxy to a
  host proxy (RAN, phase 6, nono 0.78.0).
- **nono:** that `sudo` fails inside a session; `extends` by path; bind
  for `composer serve`.
- **Claude Code:** refresh-token rotation (D6). Done: a session in the
  VM with a credential passed by `resume` (RAN, phase 6, an API key).
- **cloud-init:** the `clean` flags on Debian's version (RAN on
  Ubuntu's cloud-init 26.1, §5.2).
- **Other:** host `git maintenance` and alternates. Done: Chrome
  headless under nono (only with `--no-sandbox`);
  `uploadpack.packObjectsHook` is ignored by the guest's git (RAN).

Sources consulted:

- Lima: `limactl clone` reference; internals; `templates/default.yaml`
  (`base:` EXPERIMENTAL; `mountPoint`; `writable`; provisioning per
  boot); user-mode network page (`host.lima.internal`); mount page
  (types; the reverse-sshfs warning); plain-mode page (mounts ignored);
  issue #2580.
- Lima: network overview, user-v2 and VMNet pages (no egress
  controls); environment-variables page (`QEMU_SYSTEM_*`).
- Lima v2.2.1 source (commit `27bc4c4b`): `pkg/driver/qemu`,
  `pkg/driverutil`, `pkg/qemuimgutil`, `pkg/downloader`,
  `pkg/instance`, `pkg/limatype/filenames`.
- QEMU: bug #1696746 (quotes the `-netdev user,restrict=on`
  documentation); the system invocation page.
- CVE-2023-32684 (osv.dev).
- For D12, the companion note and its sources: Lima PR #4206 (overlays
  removed in v2.0), issues #2579, #1411 and #5533, the Lima docs commit
  on `cp -c` and the cache directory, and lima-ai (golden-image
  sealing).
