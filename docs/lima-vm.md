# Lima VM mode

`claude-vm` runs the whole of wmf-claude (nono, Claude Code, the MCP
servers, the Docker exec broker) inside one persistent
[Lima](https://lima-vm.io) VM (Debian 13). It is opt-in: plain `bin/claude`
stays the default, and nothing here changes it. A VM is one of two kinds,
chosen at `create`: **isolated** or **contained**.

## Which mode do I need?

| | `bin/claude` on the host | Isolated VM | Contained VM |
|---|---|---|---|
| Around the agent | nono | nono, in a VM | nono, in a VM |
| The agent's network | the profile's allowlist: Wikimedia sites (mostly read-only), Phabricator, Gerrit, GitLab, docs, the Anthropic API | the same | the model API only, through a proxy on the host |
| The VM's network, outside nono | — | the internet; not the host, the LAN or private ranges | none (locked) during sessions |
| The model credential | on the host (keychain), in reach of the sandbox | in the VM, in reach of the sandbox | on the host only, out of the VM's reach |
| Host files | nono denies credentials and dotfiles; the working directory is writable | none: code moves as git bundles | none |
| MCP servers | yes | yes | no |
| Docker broker | yes | yes | yes; images and dependencies prepared while open |
| Model requests logged | no | no | each one, on the host |
| If the agent gets root in the VM | — | it has the internet and the token | the in-VM lock can go; only the host firewall rule on `limactl`, if on, still blocks the VM (maybe not DNS) |

- **`bin/claude` on the host**: everyday work. Nothing to set up.
- **Isolated VM**: an escape from nono must land somewhere other than your
  machine. It does not limit egress: the work in the VM is not contained. A bypass lands in a throwaway Debian guest that holds the repos you
  pushed, the login token and Docker images, not your home directory,
  keychain, SSH keys or browser profiles.
- **Contained VM**: the agent must reach nothing but the model API and never
  see the model credential. This is what Anthropic's Cyber Verification
  Program (CVP) agent-containment guidance asks for: a VM whose only egress is
  the model API, through a local proxy, with the credential injected from
  outside. The cost: a token from `claude setup-token`, an open/locked cycle
  for setup, a firewall rule on the host, and no MCP servers.

Two layers stop a contained VM's egress. The in-VM lock, a root-owned
nftables table, blocks it in every session; the agent is not root, so it
cannot remove it. The host firewall rule (LuLu, below) blocks the VM's
traffic on the host, and is the layer that still holds if something gets
root in the VM. Without the rule, a contained VM is contained while VM root
holds.

How it works, and why: [`security-rationale.md`](security-rationale.md#lima-vm-mode).

## Requirements

Lima 2.0 or newer (`brew install lima`), git, tar, python3. macOS uses the
`vz` driver; Linux hosts use QEMU with KVM. About 10 GB of disk for the
image, Docker and the tree. nono and Claude Code are installed in the guest;
on the host, a contained VM needs Claude Code once, for `claude setup-token`.

`./setup.sh` offers to add a `claude-vm` alias for `bin/claude-vm`, and the
examples here use it; without the alias, run `bin/claude-vm`. The old name,
`bin/wmf-claude-vm`, still works. `--vm NAME` before the command (or
`WMF_CLAUDE_VM=<name>`) selects the VM; the default is `wmf-claude`. A
contained VM is a separate VM, so `create --contained` refuses the default
name: give it its own, such as `wmf-claude-contained`. `claude-vm
list` shows the VMs on this machine and the mode of each, and `status`
shows what the mode protects.

## Isolated VM: quick start

```bash
claude-vm create --isolated         # once; asks for your Phabricator username
claude-vm login                     # once: open the printed URL on the host, paste the code
claude-vm push ~/src/mediawiki/core
claude-vm claude core               # Claude Code in that workspace, sandboxed
claude-vm pull core                 # the agent's branches land in vm/* of your checkout
claude-vm review core <branch>      # then checkout or discard it
```

`create` needs `--isolated` or `--contained`; on a terminal it asks when
neither is given. It also takes `limactl create` flags (`--cpus 8 --memory 16`; the defaults
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
   claude-vm create --contained   # ends with the network locked
   command claude setup-token             # on the host, outside the sandbox: a long-lived token
   claude-vm login                # paste it; it goes into a 0600 host file, never the VM
   ```
3. **Turn the LuLu rule on and prove it works:**

   ```bash
   claude-vm check-host-block     # removes the VM's lock for one request; it must still fail
   claude-vm status               # network: locked; host firewall: verified (date)
   ```

Then the daily cycle:

| | VM | Host firewall rule | You |
|---|---|---|---|
| **Set up** | `claude-vm unlock` | off | `update`, clone or push repos, pull images, `composer install` |
| **Work** | `claude-vm lock` | on | `claude-vm claude NAME` |

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
It also writes a per-session transcript next to that log,
`transcript-<vm>-<time>-<pid>.jsonl.gz`: a request record before each call
reaches the API and a response record after, linked by an id. Both live on the
host, out of the VM's reach, so a session cannot alter its own record; a
request it cannot record is not forwarded, and the session stops at a write
error or the byte budget (`WMF_CLAUDE_CAPTURE_MAX_BYTES`, 2 GiB by default).
`delete` and `reset` keep the files, so remove them yourself when the audit
trail is no longer needed.

To read these host-side artifacts, run `claude-vm logs`. It reads the
files only, so it needs neither a running VM nor the host that ran the session.
With no argument it prints the most recent session of the request log, one line
per request (colour on a terminal, plain in a pipe); `--all` prints every
session and `-f` follows a live one. `logs --transcript` summarises the newest
transcript, one line per record; `--transcript PATH` reads a specific file.
`--text` reconstructs each turn as readable text: the model's prose, its
extended thinking, and the tool calls it requested (shown as `→ Bash: ...`). A
tool call is a request to run a command; what happened — the output, a denial
or an error — is in the next request's result, which `--text` also shows. An
error body or a part-captured turn is flagged, and `--raw` prints the decoded JSON. Set `WMF_CLAUDE_VM` to the VM whose logs you
want, as with every other subcommand.
`--allow-post`, `--minimax` and `--ide` are refused; the VM has no MCP
servers except chrome-devtools, whose browser runs in the VM.

### Network phases: locked and open

**Locked**, the default and the only phase in which sessions start: nothing
leaves the VM, for any user or container. The forwarded model proxy still
works; so do Docker containers among themselves and with the VM. **Open**:
normal egress, for setup. A VM that boots, or reboots, comes up in the
phase it had, and an unknown phase is locked. `claude-vm status`
shows `locked`, `open`, or `broken` (sessions refused).

## The work loop

```bash
claude-vm push DIR [NAME]        # git bundle of HEAD, branches and tags -> /home/agent/work/NAME
claude-vm push --nested DIR      # the same, and every repo inside DIR (core: extensions/*, skins/*)
claude-vm claude NAME [args]     # bin/claude in that workspace, as agent, sandboxed; on exit: what is not pulled yet
claude-vm pull NAME [DEST]       # agent's branches -> vm/* in the repo NAME came from
claude-vm pull --nested NAME     # the same, and the new commits of the repos pushed with --nested
claude-vm review NAME BRANCH     # git log -p HEAD..vm/BRANCH
claude-vm checkout NAME BRANCH   # git switch -c BRANCH vm/BRANCH
claude-vm discard NAME BRANCH    # git branch -rd vm/BRANCH
```

- `push` moves commits and refs only: never `.git/config`, hooks or
  untracked files. A second push updates `host/*` in the workspace and never
  overwrites the agent's work. A push sends only the commits the workspace
  does not have yet, so the first push of a large repo is the slow one.
- `push --nested` also sends the repos inside DIR, up to three levels down
  (`vendor` and `node_modules` are not searched): for MediaWiki core,
  `extensions/*` and `skins/*`. Each one is a workspace of its own, named
  after its path (`core/extensions/Foo`), and lands inside the checkout, as
  on the host. `--nested=Foo,Bar` sends only those. One copy carries all the
  bundles; a repo that fails to import is reported, and the others still
  arrive. Submodules inside DIR are found too, as nested repos. core with
  about a hundred extensions is roughly 2 GB the first time, and the host
  and the guest each need about twice that free while it moves. A repo more
  than three levels down, such as VisualEditor's `lib/ve` submodule, does
  not travel: push it by hand
  (`push ~/src/mediawiki/core/extensions/VisualEditor/lib/ve core/extensions/VisualEditor/lib/ve`).
- `pull` fetches the agent's branches into remote-tracking `vm/<branch>`
  refs; your branches, HEAD and working tree stay as they are. **Review
  before you take a branch**: the commits are untrusted, and a diff can add
  files that run on build or commit. The workspace remembers which repo it
  came from, so `pull`, `review`, `checkout` and `discard` need only its
  name; `pull NAME DEST` sends it elsewhere and remembers that instead.
  `pull --nested NAME` also brings back the work in the workspaces pushed
  with `--nested`, each into its own repo, in one pass. Like `pull`, it
  keeps each repo's `vm/*` the same as the VM's branches, deletions too,
  and sends only commits the host does not have yet. When the top repo's
  pull fails, the nested ones still run.
- `claude` takes `bin/claude`'s flags, except `--ide` (it needs the host).
  `--chrome` starts a browser in the VM: [A browser in the VM](#a-browser-in-the-vm).
- A session is granted its own workspace. For core plus extensions, push
  with `--nested` and start the session in `core`: it sees them all. For
  sibling workspaces, pass nono's grant through, with guest paths and the
  `--`: `claude-vm claude core --allow /home/agent/work --`
  (`--read DIR --` for read-only). Only what you pushed is in reach, so this
  is a smaller step than `--allow ~/src` on the host.
- Notes, or any directory that is not a repository: `git init` it on the
  host, add a `.gitignore` for anything large, commit, and push it like a
  repo. What the agent commits there comes back with `pull`.
- When a session ends, `claude` lists what is still only in the VM: branches
  not pulled, and changed files not committed (`status` shows the same).
  Uncommitted files never travel, since a bundle carries commits only: ask
  the agent to commit, or commit yourself with `shell --agent`.
- A large repository can be cloned shallow in the VM instead of pushed
  (in a contained VM, while open):
  `claude-vm shell --agent -- git clone --depth 1 URL /home/agent/work/NAME`.
  The first `pull NAME DEST` then sends only the agent's new commits; DEST
  must have the commit the clone starts at (`git fetch` it first).

## PHPUnit in the VM

The guest has PHP, Composer and MariaDB, so tests and maintenance scripts
run as on the host: no Docker, and no emulated images on Apple Silicon. A
project that is not MediaWiki can ignore this section; the packages do no
harm, and `--local-web=PORT` opens, and lets the session bind, whatever port
its own dev server uses.
MariaDB listens on 127.0.0.1 only; `wikiuser` (password `wikipass`) owns the
database `wikidb`.

```bash
claude-vm shell --agent -- composer install -d /home/agent/work/core   # needs the network: a contained VM while open
claude-vm shell --agent -- php /home/agent/work/core/maintenance/install.php \
  --dbserver 127.0.0.1 --dbname wikidb --dbuser wikiuser --dbpass wikipass \
  --server http://127.0.0.1:8080 --scriptpath '' \
  --pass 'change-me-adminpass' DevWiki admin
claude-vm claude core --local-db
```

- `composer install` needs packagist, which nono's allowlist does not
  include, so it runs outside nono as the agent, and `vendor` is the
  agent's. Run it again when a push changes `composer.json`.
- `install.php` writes `LocalSettings.php` once per workspace. For pushed
  extensions add `wfLoadExtension` lines, or pass `--with-extensions`.
- `--local-db` opens the port for the session, as on the host. In the VM the
  database is in the agent's reach anyway: nono does not mediate Unix socket
  connects on Linux, and `LocalSettings.php` holds the password. It is test
  data, in a VM nothing outside reaches.
- No service: `--dbtype sqlite --dbpath /home/agent/work/core/data` gives a
  wiki that needs no `--local-db`. Use MariaDB when a finding depends on
  MySQL behaviour.
- **Serving the wiki.** No web server runs unless Docker provides one. In a
  `--local-web` (or `--chrome`) session, PHP's built-in server runs inside
  the sandbox, since nono lets the session bind a port it opened, and it
  stops with the session. Claude is told how at session start; by hand, from
  the workspace root in the session:
  `PHP_CLI_SERVER_WORKERS=4 php -S 127.0.0.1:8080 -t . &`. The install
  command above points the wiki at that address.
- `update` adds the packages to an existing VM (contained: unlock first).
- Docker stays for the full dev stack: the services, and a wiki in a browser.

## A browser in the VM

`claude-vm claude NAME --chrome` starts a headless Chromium in the
guest for the session, as the `wmfchrome` user, and stops it with the
session; there is nothing to run in another terminal. The chrome-devtools
MCP attaches to it as on the host, and screenshots work headless.

The browser reaches loopback only. An nftables rule rejects everything the
browser user sends out of a non-loopback interface; on top of that, name
resolution is off for every name but `localhost`, and the default context
points at a closed proxy. So it shows what the VM serves on loopback: the
local wiki from `--docker=mediawiki --local-web` or from PHP's built-in
server, or any dev server the session starts on an opened port, and not
Wikimedia sites. The firewall is the layer that holds: a CDP client can open
a browser context with its own proxy, so the flags alone would not.
`update` adds Chromium to an existing VM. If the browser does not start,
`claude-vm claude` prints its log; "No usable sandbox" means user
namespaces are off in the guest. The `dbus` errors in that log are noise:
a headless system has no session bus.

## Docker inside the VM

You drive Docker from an engineer shell; Claude reaches it only through the
broker, with `mwdocker`, as on the host. The checkout belongs to `agent`, so
run the containers as the agent's UID:

```bash
claude-vm unlock                  # contained VM only, with the host firewall rule off
claude-vm shell
cd /home/agent/work/core
printf 'MW_DOCKER_UID=%s\nMW_DOCKER_GID=%s\n' "$(id -u agent)" "$(id -g agent)" | sudo -u agent tee .env
docker compose up -d                      # pulls the images
docker compose exec mediawiki composer install
exit
claude-vm lock                    # contained VM only, with the rule back on
claude-vm claude core --docker=mediawiki --local-web
```

- The compose file in the agent's workspace is the agent's to edit, and
  Docker is VM root: **read it before `docker compose up`**, or keep a
  reviewed copy outside `/home/agent`. The broker refuses privileged and
  socket-mounting containers.
- `--docker` takes a service name only (no `auto`, `:WORKDIR` or
  `--egress`). In an isolated VM, restrict a container's network with an
  override from `/opt/wmf-claude/templates/docker-egress/`, which the agent
  cannot write:
  `docker compose -f docker-compose.yml -f /opt/wmf-claude/templates/docker-egress/egress-none.yml up -d`.
- **Contained VM:** the lock blocks the containers too, so prepare open and
  test locked. While locked, `composer update`, `npm install` and image
  pulls fail; unlock for them, with no session running.

## Updating, stopping, resetting

```bash
claude-vm list      # the wmf-claude VMs on this machine: state, mode, workspaces
claude-vm update    # re-upload this checkout's HEAD, re-pin nono, update Claude Code (contained: unlock first)
claude-vm status    # VM state, mode and what it protects, installed revision vs host HEAD, invariants; contained: phase, host firewall
claude-vm stop      # free the RAM; login state and workspaces persist
claude-vm start
claude-vm reset     # delete (confirmed by name) and create again, same mode: drops the token and every workspace
```

A failed `update` leaves the previous install in place. The VM's copy has no
`.git`, so `bin/claude`'s self-update is off there; `update` is the only
update path. It asks nothing after the first run: it keeps the Phabricator
username it has (`WMF_CLAUDE_PHAB_USER=<name>` sets it, for `create` too) and
reuses the previous revision's vendored dependencies when their lockfiles
are unchanged, so a routine update is mostly the upload and a Claude Code
check.

## Known limitations

- **Isolated VM: the model token is in the VM,** in reach of the sandbox, as on
  the host. `reset` drops it; revoke it at claude.ai if in doubt.
  → Contained mode keeps it on the host.
- **Host files follow `$XDG_*`, the profile denies are literal.** The token
  (`~/.config/wmf-claude`), the request log and the transcripts
  (`~/.local/state/wmf-claude`) honour `XDG_CONFIG_HOME` and `XDG_STATE_HOME`,
  but the profiles deny the literal `~/.config` and `~/.local/state` paths. If
  you set those `XDG_*` variables to a non-default location, the files move out
  from under the denies, so a host `bin/claude` session launched from that
  location could read them. Leave `XDG_CONFIG_HOME`/`XDG_STATE_HOME` at their
  defaults, or add the real paths to both profiles' `deny`.
- **Isolated VM: egress.** Inside the VM, nono's allowlist binds Claude exactly
  as on the host, and containers you start are not sandboxed by nono.
  → Contained mode locks the VM's network.
- **Isolated VM: a localhost-port grant is not race-proof on Linux.** nono's
  per-connection destination check (the seccomp layer that keeps `--open-port`
  to loopback) approves a `connect()` with `SECCOMP_USER_NOTIF_FLAG_CONTINUE`,
  which re-runs the syscall from the child's own memory, so a second thread can
  rewrite the address after the check — a known nono limitation. A
  `--local-web`, `--local-db`, `--docker` or `--chrome` session could in
  principle use this to reach an outside host, and the VM's host-and-LAN block
  still lets the `agent` user out on 80/443. The exposure is bounded to the
  throwaway guest, but in an isolated VM that includes the model token.
  → Contained mode closes it: the lock drops every non-loopback packet, and the
  token is not in the VM.
- **Apple Silicon: the WMF dev images do not run.** The VM is arm64 and the
  images (`docker-registry.wikimedia.org/dev/...`) are amd64 only. QEMU
  emulation crashes their `php-fpm`; Rosetta would run them but needs a host
  share, which this VM does not allow. Use multi-arch images, or a VM on an
  amd64 Linux host. PHPUnit does not need them:
  [PHPUnit in the VM](#phpunit-in-the-vm).
- **Contained: what the host firewall may miss.** Should something with root
  in the VM remove the lock, the block on `limactl` stops the VM's traffic,
  but maybe not DNS (`check-host-block` reports whether it does) and not the
  host's own loopback services. The in-VM lock covers both; closing them on
  the host needs a VM with no network at all, which Lima does not offer yet.
- **Contained: not done yet.** A per-engagement scope statement surfaced to
  the session.
- **Linux hosts: contained mode is untested.** The token store (a 0600 file
  under `~/.config/wmf-claude/`) is the same on Linux and macOS, but the Linux
  path has not run, `tests/smoke-contained.sh` uses some macOS commands, and no
  host firewall setup has been tried; `check-host-block` tells you whether
  yours works.
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
claude-vm shell
findmnt -t virtiofs,9p,fuse.sshfs                # nothing: no host mounts
id agent; sudo -u agent sudo -n true             # no docker group; sudo refused
sudo -u agent curl -sS --unix-socket /var/run/docker.sock http://localhost/version   # Permission denied
curl -sS --max-time 5 http://host.lima.internal:22 ; echo $?                          # refused: 7
sudo nft list table inet wmf_claude_hostblock     # the host-and-LAN block, with counters
```
