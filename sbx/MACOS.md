# wmf-sbx on macOS

wmf-sbx is checked on Linux only. This file has the tests that check it
on a Mac (part 1, for a person with a Mac), and what is known about the
architecture that macOS lacks (part 2, for whoever works on it).

On macOS, Lima's default driver is `vz` (Apple's Virtualization
framework); `create --vm-type qemu` gives QEMU with HVF. They get:

| | `vz` (the default) | QEMU (`--vm-type qemu`) |
|---|---|---|
| Disk | a full copy of the golden image, converted to raw | a qcow2 overlay on the golden image |
| Network | the guest's own policy (nftables and nono); `create` warns | the host egress proxy |
| `--sudo` | refused (`--sudo` alone picks QEMU) | yes |
| `wmf-sbx allow`, `logs` | no | yes |
| Repo mounts | virtiofs | 9p |
| `--nested` | M3 or later with macOS 15 (`nestedVirtualization`) | not checked |

So the faster `vz` is the weaker of the two in every way that wmf-sbx
adds: part 2 is what it would take to close that.

# Part 1: tests to run on a Mac

## For the tester

wmf-sbx makes disposable MediaWiki development sandboxes: Lima VMs in which
Claude Code runs as an unprivileged user. This part asks a person with a
Mac to check that it works there, and to send the results back. It takes
an afternoon, mostly waiting: about 2 hours of machine time and 30
minutes of yours.

Nothing here changes your Mac outside these places: Homebrew packages,
`~/wmf-claude`, `~/wmf-sbx-src` (the repos the sandbox works on),
`~/.lima/wmf-sbx-*`, `~/.cache/wmf-sbx`, `~/.config/wmf-sbx`,
`~/.local/state/wmf-sbx`, and `~/wmf-sbx-macos` for the results. Step 11
removes everything wmf-sbx made.

### What you need

- A Mac with macOS 13 or later, and 40 GB of free disk.
  - Apple Silicon (M1 to M4): everything except the browser tests, which
    cannot run there (there is no Chrome for Testing for Linux on arm64).
  - Intel: everything.
- [Homebrew](https://brew.sh/).
- A Claude subscription (Pro or Max) to sign in with. You can use an API
  key instead (`export ANTHROPIC_API_KEY=...`); then skip step 4.
- A Gerrit account is not needed: the sandbox clones anonymously.

### Recording the results

Run every command in this page from one Terminal window, in the results
directory, and keep its output:

```bash
mkdir -p ~/wmf-sbx-macos && cd ~/wmf-sbx-macos
script -a session.log        # records everything you see from here on; `exit` stops it
```

If a step fails, do not fix it: note the step number, keep going where you
can, and say so in `RESULTS.md` (step 12). A failure on macOS is the
result we are looking for.

### 1. Install

```bash
brew install lima jq git python@3 pyyaml
git clone --recurse-submodules https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude.git ~/wmf-claude
export PATH="$HOME/wmf-claude/sbx/bin:$PATH"
python3 -c 'import yaml' && echo pyyaml-ok
sw_vers; uname -m; sysctl -n machdep.cpu.brand_string; limactl --version
```

If `import yaml` fails, `python3 -m pip install --user pyyaml` (add
`--break-system-packages` if pip refuses).

Tell wmf-sbx where to clone the repos on the Mac (if you already have a
`~/.config/wmf-sbx/settings.yaml`, keep yours and skip this):

```bash
mkdir -p ~/.config/wmf-sbx
cat > ~/.config/wmf-sbx/settings.yaml <<'YAML'
repos:
  - match: "gerrit:mediawiki/core"
    path: "~/wmf-sbx-src/core"
  - match: "gerrit:mediawiki/extensions/{name}"
    path: "~/wmf-sbx-src/extensions/{name}"
  - match: "gerrit:mediawiki/skins/{name}"
    path: "~/wmf-sbx-src/skins/{name}"
  - match: "gerrit:**/{name}"
    path: "~/wmf-sbx-src/{name}"
YAML
```

### 2. Unit tests (5 minutes)

```bash
cd ~/wmf-claude/sbx && python3 -m unittest discover -s tests 2>&1 | tail -30; cd -
```

Want: `OK` at the end. Failures here are macOS differences in paths,
commands or Python; they are useful on their own.

### 3. Make a sandbox (30 to 60 minutes the first time)

```bash
time wmf-sbx create --no-start --name mac1 gerrit:mediawiki/extensions/Cite
wmf-sbx status mac1
wmf-sbx ls
```

The first `create` builds the golden image (a VM that installs PHP, Node,
Claude Code and more, then is saved), then makes the sandbox: clones of Cite
and MediaWiki core, a working wiki. Want: every `ok` line in `status`, and
`create` ending with the commands to resume.

### 4. Sign in (5 minutes)

```bash
wmf-sbx login
```

It starts a small VM from the golden image that step 3 built. It prints a
link: open it, sign in to Claude, copy the code it shows, paste it
into the Terminal. Want: "Saved the token in ...". Note anything odd on the
screen (garbled text, a link you could not open or copy).

### 5. Use it (15 minutes)

```bash
wmf-sbx resume mac1
```

Claude Code starts inside the sandbox. Want: no login question, no theme
question. Ask it, one after the other:

1. "Run Cite's PHP unit tests and tell me the summary line."
2. "Start the wiki and fetch its main page with curl; tell me the HTTP status."
3. "Make a branch named mac-test, add a file MAC-TEST.md with today's date,
   and commit it."

Then `/exit`. On the Mac, fetch the commit:

```bash
git -C ~/wmf-sbx-src/extensions/Cite fetch mac1
git -C ~/wmf-sbx-src/extensions/Cite log -1 --stat mac1/mac-test
```

Want: the commit with MAC-TEST.md.

### 6. The repo mounts (10 minutes)

```bash
~/wmf-claude/sbx/checks/mounts.sh mac1 ~/wmf-sbx-src/extensions/Cite
```

It checks the read-only mount of Cite's git directory (virtiofs on a
Mac): a commit, a repack and a `git gc` on the Mac are seen at once in
the sandbox, and nothing in the sandbox can write the mount, not even
root after `mount -o remount,rw`. It changes only throwaway branches
named `wmf-sbx-check-*`, and runs `git gc` in your Cite clone. Want: a
PASS line for each check and exit status 0 (`echo $?`).

### 7. Browser tests (Intel Macs only; 15 minutes)

```bash
WMF_SBX_CHECK_NAME=mac1 ~/wmf-claude/sbx/checks/real-hardware.sh browser
WMF_SBX_CHECK_NAME=mac1 ~/wmf-claude/sbx/checks/real-hardware.sh qunit
```

On Apple Silicon, run only the first line and expect it to fail (record
the message).

### 8. The credential stays on the Mac (20 minutes)

```bash
cd ~/wmf-claude
sbx/checks/credential-host.sh create
sbx/checks/credential-host.sh source
sbx/checks/credential-host.sh concurrent
sbx/checks/credential-host.sh scan
sbx/checks/credential-host.sh rm
cd ~/wmf-sbx-macos
```

Want: `source` names the token file; `concurrent` shows `rc 0` and
`cred-ok-...` for both; `scan` ends with `== scan: PASS`. The scan searches
both VMs for your Claude token without ever printing it.

If you use Claude Code on this Mac, also run `source` once with the token
file moved away, to check the keychain is read:

```bash
mv ~/.config/wmf-sbx/claude-oauth-token{,.away}
~/wmf-claude/sbx/checks/credential-host.sh source
mv ~/.config/wmf-sbx/claude-oauth-token{.away,}
```

### 9. Optional: QEMU instead of Apple's virtualisation (30 minutes)

```bash
brew install qemu
time wmf-sbx create --no-start --vm-type qemu --name mac-qemu gerrit:mediawiki/extensions/Cite
du -sh ~/.lima/wmf-sbx-mac-qemu
wmf-sbx status mac-qemu
wmf-sbx exec mac-qemu -- curl -sS -o /dev/null -w '%{http_code}\n' https://www.kernel.org/
wmf-sbx allow mac-qemu www.kernel.org
wmf-sbx exec mac-qemu -- curl -sS -o /dev/null -w '%{http_code}\n' https://www.kernel.org/
wmf-sbx logs mac-qemu
wmf-sbx rm --force mac-qemu
```

On QEMU the sandbox is behind the host egress proxy. Want: `create`
with no warning about the egress proxy; `du` in MB, not GB; the first
`curl` fails (`CONNECT tunnel failed, response 403`), the second prints
`200`; `logs` shows the denied and the allowed request.

### 10. Optional: QEMU behind the host egress proxy (20 minutes)

This checks the network mode that wmf-sbx uses by default on Linux, and
that `--sudo` needs. It does not use the sandbox you made above. A
script makes one throwaway VM, `egress-check`, from Lima's Debian 13
template. It runs the VM through a wrapper
(`sbx/bin/wmf-sbx-qemu`) that cuts off the VM's network
(QEMU `restrict=on`). The only way out is a `nono proxy` on the Mac,
which the wrapper starts and stops. The script checks that the proxy
lets the VM reach allowed hosts and nothing else, that a host can be
allowed while the VM runs, what happens to the VM's clock, and that
the proxy and QEMU stop with the VM.

```bash
brew install qemu socat nono
~/wmf-claude/sbx/checks/egress-host.sh all
~/wmf-claude/sbx/checks/egress-host.sh clean
```

It prints a PASS or FAIL line for each check and puts its logs in
`~/wmf-sbx-macos/egress-host-logs/`. One FAIL is expected on macOS:
"kill-wrapper: still running". When the wrapper is killed, macOS does
not stop its children the way Linux does, so QEMU and the proxy keep
running. The script then kills them itself. Record any other FAIL.

### 11. Clean up

First check the recording for secrets, while the token is still there:

```bash
python3 ~/wmf-claude/sbx/checks/credential-scan.py results ~/wmf-sbx-macos/*
```

Want: `0 hits` for each credential found. (With an API key, keep
`ANTHROPIC_API_KEY` set for this.) Then:

```bash
wmf-sbx rm --force mac1
~/wmf-claude/sbx/checks/egress-host.sh clean   # if you did step 10
wmf-sbx rm --prune
wmf-sbx image prune
limactl list                               # want: no wmf-sbx-* left
rm ~/.config/wmf-sbx/claude-oauth-token    # unless you keep using wmf-sbx
```

To remove everything: also `rm -rf ~/.cache/wmf-sbx ~/.local/state/wmf-sbx
~/.config/wmf-sbx ~/wmf-claude ~/wmf-sbx-src`, and `brew uninstall lima` if you do not
use it otherwise. The token can be revoked at claude.ai (Settings, Claude
Code) if you want.

### 12. Send the results

Stop the recording (`exit`), then write `~/wmf-sbx-macos/RESULTS.md`: the
Mac model and chip, the macOS version, and for each step 1 to 11 "pass",
"fail" or "skipped", with a line about anything that surprised you.

Then pack the directory (step 11 checked it for secrets):

```bash
tar czf ~/wmf-sbx-macos.tgz -C ~ wmf-sbx-macos
```

Send `~/wmf-sbx-macos.tgz` to the person who asked you to test.

## For the person who gets the results

The results go to the Claude Code session that works on wmf-sbx (for
example, in a sandbox made with `--nested`). Unpack the tarball and attach
`RESULTS.md` and `session.log` to the conversation; in a wmf-sbx sandbox,
copy them in with `wmf-sbx cp`. `session.log` is a terminal
recording, with escape sequences; Claude reads it as is.

## What each step checks

For the session that reads the results. Record the Lima, QEMU and macOS
versions, and the chip, with them.

- **3:** the golden image builds on a Mac and boots under `vz`. The
  `time` of `create` (and `du -sh ~/.lima/wmf-sbx-mac1`) is the cost of
  a full-copy create, the baseline for copy-on-write on `vz` (part 2).
- **5:** a session under nono on `vz`: the grants in `session.py` are
  enough for the agent's paths on a Mac, the MCP servers connect, the
  wiki answers, and the host fetches the agent's commit.
- **6:** virtiofs keeps the git dirs read-only from the host side, and
  shows host changes at once (9p on QEMU needed `cache: none` for that).
  Read-only from the host side is what any future `--sudo` on `vz` would
  rest on.
- **7:** the browser tests (Intel only).
- **8:** the credential proxy reads the token from the keychain
  (`credproxy.py`, `security`), and no VM holds the token.
- **9:** QEMU with HVF: the qcow2 overlay, 9p mounts,
  and the egress proxy in a real sandbox, with `allow` and `logs`.
- **10:** the QEMU wrapper and the egress proxy with HVF, through every
  kind of stop.

Not covered:

- two sandboxes from one golden image on `vz` have different
  machine-ids and SSH host keys;
- `--nested` on `vz` (needs an M3 or later with macOS 15);
- `stop`, `start` and `cp` on `vz` (step 11's `rm` is the only other
  lifecycle command here);
- the qcow2 overlay's own checks with HVF, with real times; step 9
  checks only the size and that the sandbox works;
- a `--sudo` sandbox (QEMU) on a Mac.

# Part 2: what macOS lacks

## Copy-on-write disks for `vz`

On QEMU, a sandbox's disk is a qcow2 overlay on the read-only golden
image (`disk.py`; `DESIGN.md`, "Disks and warm snapshots"): about 20 MiB after the first boot.
`vz` (and `krunkit`) boot raw disks only, and Lima converts the qcow2
golden image to raw at create (`driverutil.EnsureDisk`), so every `vz`
sandbox gets a full copy: gigabytes, and the time to write them. Lima
2.0 removed its own overlays (`basedisk`/`diffdisk`, PR #4206), so there
is nothing in Lima to turn on.

What is known about a solution:

- **APFS clones.** `cp -c` makes a clonefile copy: instant, and it
  shares unchanged blocks until either side writes them. A stopped,
  sealed raw golden disk cloned into each new instance's `disk` gives a
  `vz` sandbox most of what an overlay gives a QEMU one. The place to do
  it is the same as for the qcow2 overlay: after `limactl create`, before the first
  `limactl start` (`vm.create`).
- **What it needs:** a raw copy of the golden image in the cache, made
  once per image (`qemu-img convert -O raw`), on the same APFS volume as
  `~/.lima`: clonefile does not work across volumes, so check it and
  fall back to a full copy with a warning. Lima's issue #5533 proposes
  clonefile for krunkit snapshots, and fails rather than fall back.
- **Maybe already half there:** Lima copies a local image into the
  instance with `continuity/fs.CopyFile`. If that makes a clonefile copy on APFS, and the golden image is
  already raw, so Lima has nothing to convert, then Lima's own copy is
  already a clone. Neither is checked; issue #1411 (the downloader and
  clonefile) is the history. Check first: if it holds, the change is a
  raw cache entry and nothing more.
- **Safer than the qcow2 overlay:** a clone is a file of its own with no backing
  file, so there is no Lima check to go around, and the guest cannot
  reach the golden file through it.
- **What changes in wmf-sbx:** `image.images_in_use` finds a QEMU
  sandbox's golden image from its disk's backing file; a clone has none,
  so `vz` sandboxes must record their image in state for `image prune`.
  `du` counts a clone at its full size: `status` would need
  `df`-style accounting to show what a sandbox really costs.
- The same works on Linux with `cp --reflink=always` on Btrfs or XFS,
  if anyone wants it for `--full-disk`.

## An equivalent of `restrict=on` for `vz`

The egress proxy rests on QEMU's user network: `restrict=on` takes away
every route, and `guestfwd`s on 192.168.5.100 connect the guest to the
proxy, the fake-address resolver and the credential proxy on the host
(README.md, Architecture). The QEMU wrapper owns that network, and the
proxy's lifetime with it. `vz` has no QEMU to wrap, so a `vz` sandbox
falls back to the guest's own policy: nftables, the session's nono with
its own proxy, a narrowed
ephemeral port range for the local ports, and DNS through Lima's
resolver (a residual: lookups can carry data out). It gets no `--sudo`,
no `wmf-sbx allow` and no `wmf-sbx logs`.

**If `vz` gets an equivalent**, then every sandbox can be behind the
egress proxy, and a lot goes:

- `--no-egress-proxy` and `create`'s fallback;
- the guest's network filtering: the agent's rules in nftables, nono's
  network half (`--upstream-proxy`, the port window,
  the narrowed ephemeral range), and the DNS residual;
- the egress/no-egress branches in the provisioning, the session's
  launch line, the invariants and the tests;
- and `--sudo` works on `vz` too.

The main cost is in the launcher: nono with `--allow-net` (no network
sandbox) removes the proxy variables from the child's environment
(`DESIGN.md`, "nono and Landlock"), so `bin/claude` or
`bin/launch-claude.sh`, which wmf-claude also uses outside wmf-sbx, must
set them again inside the sandbox. Until `vz` has an equivalent, doing
this for QEMU alone adds a branch and removes no code (`DESIGN.md`,
"Local ports"; `TODO.md`).

Leads, none checked:

- Lima's `vz` driver gives the guest the same 192.168.5.0/24 network as
  QEMU, through gvisor-tap-vsock (a user-mode network stack in the host
  agent). A no-route mode with forwards to host sockets there would be
  the equivalent. Read `pkg/networks/usernet` and the `vz` driver in
  Lima's source to see how far it is.
- A Lima feature request for a native egress option on the QEMU user
  network and on `vz`'s would remove the wrapper on
  QEMU too.
- Something must own the proxy's lifetime on `vz` in the wrapper's
  place: Lima's host agent, or a process that `wmf-sbx start` leaves
  behind.

## Smaller differences

- **No `PR_SET_PDEATHSIG`.** When the QEMU wrapper is killed, macOS
  does not stop its children: QEMU and the proxy keep running (step 10
  expects that FAIL). A `stop` or a crash of QEMU still stops the proxy;
  only a killed wrapper leaks. A fix could watch the parent with
  kqueue (`EVFILT_PROC`, `NOTE_EXIT`) in each child.
- **The guest's clock.** Behind `restrict=on` the guest has no NTP;
  Lima's own time sync (the guest agent) sets the clock, on macOS too.
  `ptp_kvm` is Linux only.
- **Apple Silicon** has no Chrome for Testing for Linux on arm64: the
  browser tests cannot run in an arm64 guest.
- **The credential** comes from the keychain (`security`), not from
  `~/.claude/.credentials.json` (`credproxy.py`).
- **The check scripts** run under macOS's bash 3.2: no bash 4 features,
  no empty-array expansion under `set -u`.
