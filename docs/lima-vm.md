# Lima VM mode

`bin/wmf-claude-vm` runs the whole of wmf-claude (nono, Claude Code, the MCP
servers, the Docker exec broker) inside one persistent
[Lima](https://lima-vm.io) VM (Debian 13). It is opt-in: plain `bin/claude`
stays the default, and nothing here changes it.

## Which mode do I need?

| | `bin/claude` on the host | Lima VM | Contained Lima VM |
|---|---|---|---|
| Around the agent | nono | nono, in a VM | nono, in a VM |
| The agent's network | the profile's allowlist: Wikimedia sites (mostly read-only), Phabricator, Gerrit, GitLab, docs, the Anthropic API | the same | the model API only, through a proxy on the host |
| The VM's network, outside nono | — | the internet; not the host, the LAN or private ranges | none (locked) during sessions |
| The model credential | on the host (keychain), in reach of the sandbox | in the VM, in reach of the sandbox | on the host only, out of the VM's reach |
| Host files | nono denies credentials and dotfiles; the working directory is writable | none: code moves as git bundles | none |
| MCP servers | yes | yes | no |
| Docker broker | yes | yes | yes; images and dependencies prepared while open |
| Model requests logged | no | no | each one, on the host |

- **`bin/claude` on the host**: everyday work. Nothing to set up.
- **Lima VM**: an escape from nono must land somewhere other than your
  machine. A bypass lands in a throwaway Debian guest that holds the repos you
  pushed, the login token and Docker images, not your home directory,
  keychain, SSH keys or browser profiles.
- **Contained VM**: the agent must reach nothing but the model API and never
  see the model credential. This is what Anthropic's Cyber Verification
  Program (CVP) agent-containment guidance asks for: a VM whose only egress is
  the model API, through a local proxy, with the credential injected from
  outside. The cost: a token from `claude setup-token`, an open/locked cycle
  for setup, and a firewall rule on the host.

How it works, and why: [`security-rationale.md`](security-rationale.md#lima-vm-mode).

## Requirements

Lima 2.0 or newer (`brew install lima`), git, tar, python3. macOS uses the
`vz` driver; Linux hosts use QEMU with KVM. About 10 GB of disk for the
image, Docker and the tree. nono and Claude Code are installed in the guest;
on the host, a contained VM needs Claude Code once, for `claude setup-token`.

`./setup.sh` offers to add a `claude-vm` alias for `bin/wmf-claude-vm`. The
examples here use the full path. `WMF_CLAUDE_VM=<name>` selects the VM
(default `wmf-claude`); a contained VM is a separate VM, so give it its own
name.

## Lima VM: quick start

```bash
bin/wmf-claude-vm create                    # once; asks for your Phabricator username
bin/wmf-claude-vm login                     # once: open the printed URL on the host, paste the code
bin/wmf-claude-vm push ~/src/mediawiki/core
bin/wmf-claude-vm claude core               # Claude Code in that workspace, sandboxed
bin/wmf-claude-vm pull core                 # the agent's branches land in vm/* of your checkout
bin/wmf-claude-vm review core <branch>      # then checkout or discard it
```

`create` takes `limactl create` flags (`--cpus 8 --memory 16`; the defaults
are 4 CPUs, 8 GiB and a 60 GiB disk). It installs this checkout's **HEAD**
(submodules included, uncommitted changes not) into the VM, so commit first.
`login` signs Claude Code in as the VM's `agent` user; do it once per VM,
and again after a `reset`.

## Contained mode

A contained VM is created contained and stays so. Set it up once:

1. **Block the VM on the host.** On macOS, install
   [LuLu](https://objective-see.org/products/lulu.html), set it to **passive
   mode, allow** (its default mode blocks or asks about every connection,
   which breaks Claude Code on the host), and add one rule: **block**, any
   address and port, for the real `limactl` binary:

   ```bash
   readlink -f "$(command -v limactl)"   # e.g. /opt/homebrew/Cellar/lima/2.2.0/bin/limactl
   ```

   Leave the rule **off** for now: the next steps need the VM online. On
   Linux, any firewall that can block the `limactl` process will do
   (OpenSnitch, for example); see [Known limitations](#known-limitations).
2. **Create the VM and store a token on the host:**

   ```bash
   export WMF_CLAUDE_VM=wmf-claude-contained
   bin/wmf-claude-vm create --contained   # ends with the network locked
   command claude setup-token             # on the host, outside the sandbox: a long-lived token
   bin/wmf-claude-vm login                # paste it; it goes into the host keychain, never the VM
   ```
3. **Turn the LuLu rule on and prove it works:**

   ```bash
   bin/wmf-claude-vm check-host-block     # removes the VM's lock for one request; it must still fail
   bin/wmf-claude-vm status               # network: locked; host firewall: verified (date)
   ```

Then the daily cycle:

| | VM | Host firewall rule | You |
|---|---|---|---|
| **Set up** | `bin/wmf-claude-vm unlock` | off | `update`, clone or push repos, pull images, `composer install` |
| **Work** | `bin/wmf-claude-vm lock` | on | `bin/wmf-claude-vm claude NAME` |

`claude` starts only when the VM is locked; `lock` and `unlock` refuse while
a session runs; `update` refuses while locked. After a Lima upgrade the
`limactl` path changes: update the LuLu rule and run `check-host-block`
again (`status` reminds you). Sessions show a red warning, and wait for
Enter, while the host firewall is not verified; with
`WMF_CLAUDE_REQUIRE_HOST_BLOCK=1` they refuse.

In a contained session the agent reaches the model through a proxy on your
host that holds the token, allows only Messages API requests, refuses
anything that would make the API fetch from the internet for the agent
(server tools such as web search, the MCP connector, URL content sources),
and logs each request to `~/.local/state/wmf-claude/model-proxy-<vm>.log`.
`--allow-post`, `--minimax`, `--chrome` and `--ide` are refused; the VM has
no MCP servers.

### Network phases: locked and open

**Locked**, the default and the only phase in which sessions start: nothing
leaves the VM, for any user or container. The forwarded model proxy still
works; so do Docker containers among themselves and with the VM. **Open**:
normal egress, for setup. A VM that boots, or reboots, comes up in the
phase it had, and an unknown phase is locked. `bin/wmf-claude-vm status`
shows `locked`, `open`, or `broken` (sessions refused).

## The work loop

```bash
bin/wmf-claude-vm push DIR [NAME]        # git bundle of HEAD, branches and tags -> /home/agent/work/NAME
bin/wmf-claude-vm claude NAME [args]     # bin/claude in that workspace, as agent, sandboxed
bin/wmf-claude-vm pull NAME [DEST]       # agent's branches -> vm/* in the repo NAME came from
bin/wmf-claude-vm review NAME BRANCH     # git log -p HEAD..vm/BRANCH
bin/wmf-claude-vm checkout NAME BRANCH   # git switch -c BRANCH vm/BRANCH
bin/wmf-claude-vm discard NAME BRANCH    # git branch -rd vm/BRANCH
```

- `push` moves commits and refs only: never `.git/config`, hooks or
  untracked files. A second push updates `host/*` in the workspace and never
  overwrites the agent's work. Nested repos are separate workspaces:
  `push ~/src/mediawiki/extensions/Foo core/extensions/Foo`.
- `pull` fetches the agent's branches into remote-tracking `vm/<branch>`
  refs; your branches, HEAD and working tree stay as they are. **Review
  before you take a branch**: the commits are untrusted, and a diff can add
  files that run on build or commit. The workspace remembers which repo it
  came from, so `pull`, `review`, `checkout` and `discard` need only its
  name; `pull NAME DEST` sends it elsewhere and remembers that instead.
- `claude` takes `bin/claude`'s flags, except `--chrome` and `--ide` (they
  need the host).
- A large repository can be cloned shallow in the VM instead of pushed
  (in a contained VM, while open):
  `bin/wmf-claude-vm shell --agent -- git clone --depth 1 URL /home/agent/work/NAME`.
  The first `pull NAME DEST` then sends only the agent's new commits; DEST
  must have the commit the clone starts at (`git fetch` it first).

## Docker inside the VM

You drive Docker from an engineer shell; Claude reaches it only through the
broker, with `mwdocker`, as on the host. The checkout belongs to `agent`, so
run the containers as the agent's UID:

```bash
bin/wmf-claude-vm unlock                  # contained VM only, with the host firewall rule off
bin/wmf-claude-vm shell
cd /home/agent/work/core
printf 'MW_DOCKER_UID=%s\nMW_DOCKER_GID=%s\n' "$(id -u agent)" "$(id -g agent)" | sudo -u agent tee .env
docker compose up -d                      # pulls the images
docker compose exec mediawiki composer install
exit
bin/wmf-claude-vm lock                    # contained VM only, with the rule back on
bin/wmf-claude-vm claude core --docker=mediawiki --local-web
```

- The compose file in the agent's workspace is the agent's to edit, and
  Docker is VM root: **read it before `docker compose up`**, or keep a
  reviewed copy outside `/home/agent`. The broker refuses privileged and
  socket-mounting containers.
- `--docker` takes a service name only (no `auto`, `:WORKDIR` or
  `--egress`). In a normal VM, restrict a container's network with an
  override from `/opt/wmf-claude/templates/docker-egress/`, which the agent
  cannot write:
  `docker compose -f docker-compose.yml -f /opt/wmf-claude/templates/docker-egress/egress-none.yml up -d`.
- **Contained VM:** the lock blocks the containers too, so prepare open and
  test locked. While locked, `composer update`, `npm install` and image
  pulls fail; unlock for them, with no session running.

## Updating, stopping, resetting

```bash
bin/wmf-claude-vm update    # re-upload this checkout's HEAD, re-pin nono, update Claude Code (contained: unlock first)
bin/wmf-claude-vm status    # VM state, installed revision vs host HEAD, invariants; contained: phase, host firewall
bin/wmf-claude-vm stop      # free the RAM; login state and workspaces persist
bin/wmf-claude-vm start
bin/wmf-claude-vm reset     # delete (confirmed by name) and create again: drops the token and every workspace
```

A failed `update` leaves the previous install in place. The VM's copy has no
`.git`, so `bin/claude`'s self-update is off there; `update` is the only
update path.

## Known limitations

- **Lima VM: the model token is in the VM,** in reach of the sandbox, as on
  the host. `reset` drops it; revoke it at claude.ai if in doubt.
  → Contained mode keeps it on the host.
- **Lima VM: egress.** Inside the VM, nono's allowlist binds Claude exactly
  as on the host, and containers you start are not sandboxed by nono.
  → Contained mode locks the VM's network.
- **Apple Silicon: the WMF dev images do not run.** The VM is arm64 and the
  images (`docker-registry.wikimedia.org/dev/...`) are amd64 only. QEMU
  emulation crashes their `php-fpm`; Rosetta would run them but needs a host
  share, which this VM does not allow. Use multi-arch images, or a VM on an
  amd64 Linux host.
- **Contained: what the host firewall may miss.** Should something with root
  in the VM remove the lock, the block on `limactl` stops the VM's traffic,
  but maybe not DNS (`check-host-block` reports whether it does) and not the
  host's own loopback services. The in-VM lock covers both; closing them on
  the host needs a VM with no network at all, which Lima does not offer yet.
- **Contained: not done yet.** Transcript and log retention outside the
  agent's reach, and a per-engagement scope prompt.
- **Linux hosts: contained mode is untested.** The code has a Linux path
  (the token in a 0600 file instead of the keychain), but it has not run,
  `tests/smoke-contained.sh` uses macOS commands, and no host firewall setup
  has been tried; `check-host-block` tells you whether yours works.
- **Claude Code** is installed in the VM by the official installer, neither
  pinned nor checksummed. nono is pinned and checked.
- **Lima itself** runs on the host with your privileges, and the SSH key in
  `~/.lima/_config/user` opens both guest users.

## Verifying the boundary

The static checks run in CI: `tests/test-lima.sh`. Against a running VM, from
the host and outside nono:

```bash
./tests/smoke-lima.sh [--docker] [--claude]            # a Lima VM
./tests/smoke-contained.sh [--reboot] [--host-block] [--docker]   # a contained VM
```

By hand, in a Lima VM:

```bash
bin/wmf-claude-vm shell
findmnt -t virtiofs,9p,fuse.sshfs                # nothing: no host mounts
id agent; sudo -u agent sudo -n true             # no docker group; sudo refused
sudo -u agent curl -sS --unix-socket /var/run/docker.sock http://localhost/version   # Permission denied
curl -sS --max-time 5 http://host.lima.internal:22 ; echo $?                          # refused: 7
sudo nft list table inet wmf_claude_hostblock     # the host-and-LAN block, with counters
```
