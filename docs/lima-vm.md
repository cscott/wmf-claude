# Lima VM mode

Opt-in. For SREs and security engineers whose threat model includes a rogue or
prompt-injected agent that must have **no path to the host**, even through a
sandbox bug. Plain `bin/claude` stays the default; nothing here changes it.

`bin/wmf-claude-vm` runs the whole of wmf-claude — nono, Claude Code, the MCP
servers, the Docker exec broker — inside one persistent [Lima](https://lima-vm.io)
VM (Debian 13). The VM has **no host mounts and no port forwards**; code moves
in and out by explicit copy. Claude's login state lives only in the VM.

## What the VM adds, and what it does not

| | |
|---|---|
| **Same sandbox inside** | Claude still runs under `bin/claude` with `profiles/wmf-engineer.json` and the tool layer. The VM is a second boundary around that, not a replacement. |
| **A separate kernel** | A nono bypass lands in a throwaway Debian guest. Its contents: the repos you pushed, the Anthropic login token, Docker images. Not your home directory, keychain, SSH keys or browser profiles. |
| **No host filesystem** | `mounts: []`. Lima's default is to mount `~` read-only; this template mounts nothing. `bin/wmf-claude-vm push`/`pull` move git bundles: commits and refs only, never `.git/config`, hooks or untracked files. |
| **No inbound ports** | `plain: true` plus an ignore-all `portForwards` rule: Lima forwards nothing but its own SSH into the guest. No SSH agent or X11 forwarding, no Rosetta (it adds a host share). |
| **Host and LAN blocked** | A root-owned nftables rule in the guest refuses new connections to private and link-local addresses: the host (`host.lima.internal`, and its LAN address), your LAN, and a VPN's internal ranges. DNS and DHCP to Lima's own subnet pass. Applies to the guest and to its containers. |
| **Agent egress: HTTPS only** | The same rule gives the `agent` user (Claude, nono's proxy, the MCP servers) TCP 80 and 443 only. This also binds `--landlock-only`, which `--docker` needs, and which on its own lets nono allow UDP to every host. |
| **Two users** | The Lima default user `engineer` has sudo and is in `docker`; Claude runs as `agent`, which has neither. nono does not mediate `connect()` to pathname AF_UNIX sockets on Linux, so this is what keeps the Docker socket (VM root) out of reach.The engineer never runs git in, or code from, a path the agent can write, and `agent` sessions start without a login shell, so the agent's dotfiles never run outside nono. |

What it does **not** protect against:

- **The Anthropic token is in the VM.** A compromised agent can use it, as on
  the host. `bin/wmf-claude-vm reset` drops it; revoke it at claude.ai if in
  doubt.
- **Egress.** nono's allowlist binds Claude inside the VM exactly as on the
  host. Engineer-driven containers are not nono-sandboxed, as on the host; use
  the egress overrides (below).
- **User-mode networking is NAT through the host.** The VM reaches the
  public internet as the host does. The nft rule covers private address ranges
  only, so a host service on a public address stays reachable. DNS is answered
  by Lima's resolver on the host.
- **`docker compose up` in the agent's workspace runs agent-written config.**
  The compose file, overrides and `.env` there are the agent's to edit, and
  `docker` is VM root. Read them before you start containers, or keep a
  reviewed copy outside `/home/agent`. This matches the host, where it would be
  host root.
- **Docker inside the VM is rootful.** `docker` group equals VM root; only
  `engineer` is in it. A Docker compromise is VM root, still inside the VM.
  Why not rootless: the containers write into the agent-owned checkout
  (`composer install`, PHPUnit, `maintenance/run.php`), and MediaWiki-Docker
  does that by running as the checkout owner's UID. Under rootless Docker the
  container's UIDs map to the *engineer's* subordinate range, so those writes
  fail or leave files the agent cannot touch. Mapping the agent's UID into
  another user's subuid range works but is fragile. Rootful Docker with the
  agent excluded keeps the socket unreachable and the workflow unchanged.
- **Lima itself** runs on the host with your privileges, and the SSH key in
  `~/.lima/_config/user` opens both guest users.
- **Claude Code is installed by the official installer** as `agent`: neither
  pinned nor checksummed. nono is (`.nono-version`, checked against the
  release's `SHA256SUMS.txt`, which comes from the same GitHub release).

## Requirements

Lima 2.0 or newer (`brew install lima`), git, tar. macOS uses the `vz`
driver; Linux hosts use QEMU with KVM. About 10 GB of disk for the image,
Docker and the tree. nono and Claude Code are **not** needed on the host for
this mode; they are installed in the guest.

`./setup.sh` offers to add a `claude-vm` alias for `bin/wmf-claude-vm`; answer
`y`, or re-run it later. The examples here use the full path.

## Create

```bash
bin/wmf-claude-vm create                          # defaults: 4 CPUs, 8 GiB, 60 GiB disk
bin/wmf-claude-vm create --cpus 8 --memory 16     # flags pass to `limactl create`
```

The first boot downloads the Debian image (digest-checked), creates the two
users, installs Docker, nono (pinned) and the base packages, and writes the
nft rule. Then the host checkout's **HEAD**, submodules included and without
`.git`, is uploaded and installed at `/opt/wmf-claude` (owned by `engineer`,
readable by `agent`); Claude Code is installed for `agent`, and
`bin/wmf-claude-setup` runs as `agent`, which asks for your Phabricator
username the same way `./setup.sh` does. Uncommitted changes never enter the
VM.

## First login

```bash
bin/wmf-claude-vm login
```

This runs `claude auth login` as `agent`. Open the printed URL on the host and
paste the code back. The credential stays in the agent's `~/.claude` inside
the VM, and later sessions reuse it. `login` also marks the first-run
onboarding as done: otherwise its login step starts the OAuth flow again,
inside the sandbox, where it fails. Do this once per VM, and again after a
`reset`.

`login` runs outside nono, because `/login` cannot run inside it on Linux:
Claude Code binds an ephemeral port for the OAuth callback, and nono allows no
bind without a port grant. `claude auth login` has no model and no tools, so
no agent runs unsandboxed. It uses the same fixed `PATH` as the sessions, and
no login shell.

## The work loop

```bash
bin/wmf-claude-vm push DIR [NAME]        # git bundle of HEAD, branches and tags -> /home/agent/work/NAME
bin/wmf-claude-vm claude NAME [args]     # bin/claude in that workspace, as agent, sandboxed
bin/wmf-claude-vm pull NAME [DEST]       # agent's branches -> vm/* in the repo NAME came from; nothing checked out
```

- `push` clones the bundle as `agent` on first use (remote `host`) and fetches
  into `host/*` after that, so it never overwrites the agent's work. Nested
  repos (`extensions/Foo` inside `core`) are separate bundles:
  `push ~/src/mediawiki/extensions/Foo core/extensions/Foo`.
- `claude` takes every `bin/claude` flag except `--chrome` and `--ide`, which
  need the host. `--docker=SERVICE` is handled specially (below). Flags that
  open a localhost port need `--landlock-only` on Linux, as on any Linux host
  ([nolabs-ai/nono#1786](https://github.com/nolabs-ai/nono/issues/1786)).
- `pull` runs `git bundle create` **as `agent`** with `core.hooksPath=/dev/null`
  and `core.fsmonitor=false`; git never runs in the agent's repository as
  `engineer`, so a planted hook or config cannot run with sudo. On the host the
  bundle is verified and fetched into remote-tracking branches `vm/<branch>`,
  as if from a remote named `vm` (none is added to `.git/config`). Your
  branches, HEAD and working tree are untouched. Review before you take a
  branch — the commits are untrusted content, and a diff can add files that
  run on build or commit:

  ```bash
  bin/wmf-claude-vm review core fix-foo   # git log -p HEAD..vm/fix-foo
  bin/wmf-claude-vm checkout core fix-foo # git switch -c fix-foo vm/fix-foo
  bin/wmf-claude-vm discard core fix-foo  # git branch -rd vm/fix-foo
  ```

  `push` records which host repository each workspace came from (in
  `~/.config/wmf-claude/vm-<instance>-workspaces`), so `pull`, `review`,
  `checkout` and `discard` need only the workspace name, from any directory. `pull NAME
  DEST` sends it elsewhere and records DEST instead. They are plain git
  underneath; use git directly for anything else (`git branch -r`, a merge).

  The next `pull` drops `vm/*` branches that no longer exist in the VM.

## Docker inside the VM

You drive Docker from an engineer shell; Claude reaches it only through the
broker, as on the host. The checkout belongs to `agent`, so write into it as
`agent`, and run the containers as the agent's UID so their writes are the
agent's:

```bash
bin/wmf-claude-vm shell
cd /home/agent/work/core
printf 'MW_DOCKER_UID=%s\nMW_DOCKER_GID=%s\n' "$(id -u agent)" "$(id -g agent)" | sudo -u agent tee .env
docker compose up -d
exit

bin/wmf-claude-vm claude core --docker=mediawiki
```

`bin/wmf-claude-vm claude NAME --docker=SERVICE` starts
`bin/launch-docker-broker` **as `engineer`** for the workspace's
`docker-compose.yml`, publishes the
`{port, token}` handshake in `/run/wmf-claude/` (mode 0640, group `wmfbroker`:
engineer and agent), and launches `bin/claude --docker --landlock-only` as
`agent` with `WMF_DOCKER_HANDSHAKE` pointing at it — the launcher's attach
mode. The broker stops with the session. Everything the broker enforces on the
host (pinned compose file and service, allowlisted binaries, refusal of
privileged or socket-mounting containers) applies unchanged.

To keep the VM launcher short, it takes `--docker=SERVICE` only: no `auto`, no
`:WORKDIR` and no `--egress`. You can still start the containers with an
egress override, from `/opt/wmf-claude/templates/docker-egress/`, which
`agent` cannot write; the broker just does not verify it:

```bash
docker compose -f docker-compose.yml -f /opt/wmf-claude/templates/docker-egress/egress-none.yml up -d
```

## Updating, stopping, resetting

```bash
bin/wmf-claude-vm update    # re-upload this checkout's HEAD; re-pin nono; `claude update` as agent
bin/wmf-claude-vm status    # VM state, installed revision vs host HEAD, invariants
bin/wmf-claude-vm stop      # free the RAM; login state and workspaces persist
bin/wmf-claude-vm start
bin/wmf-claude-vm reset     # delete (confirmed by name) and create again: drops the token and every workspace
```

`update` installs into a new `/opt/wmf-claude.<rev>` and swaps the symlink
after a successful build, so a failed update leaves the previous install in
place. The in-VM copy has no `.git`, so `bin/claude`'s self-update prompt is
off there; `update` is the only update path.

## Verifying the boundary

The static checks are in `tests/test-lima.sh`. What a real VM should show:

```bash
bin/wmf-claude-vm shell
findmnt -t virtiofs,9p,fuse.sshfs                # nothing: no host mounts
id agent; sudo -u agent sudo -n true             # no docker group; sudo refused
sudo -u agent curl -sS --unix-socket /var/run/docker.sock http://localhost/version   # Permission denied
curl -sS --max-time 5 http://host.lima.internal:22 ; echo $?                          # refused: 7
getent hosts deb.debian.org                       # DNS still works
sudo nft list table inet wmf_claude_hostblock     # the rule, with counters
```

On the host, `lsof -nP -iTCP -sTCP:LISTEN | grep -i lima` shows only Lima's
SSH port for the instance.
