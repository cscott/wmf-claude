# PLAN: `--sudo` mode (track A), egress proxy first

Written 2026-10-13. Builds on HANDOFF-LIMA.md D9 (two whole modes) and D11
(QEMU `restrict=on` + `guestfwd` + a host proxy), whose A1 measurements
are mostly RAN. This plan changes one thing in D11: **the host proxy is
`nono proxy`**, not squid or a new Python proxy, if A2.0 shows it works.

## Why `nono proxy`

nono 0.78 runs its proxy standalone: `nono proxy --profile FILE`,
`--allow-domain HOST|URL-glob`, `--upstream-proxy HOST:PORT`,
`--proxy-ca-cert/--proxy-ca-key` (a fixed CA), `--pass`, `--log-file`,
`--credential`. Compared with D11's "hostname allowlist on CONNECT":

- **The same policy as contained mode.** It reads
  `profiles/wmf-engineer.json`, endpoint rules included (GET/HEAD-only
  wikis, the POST paths phabricator and gitlab need). D11 deferred these
  as "needs TLS interception"; nono already intercepts. So `--sudo` does
  not get a weaker egress policy than contained mode, and there is one
  allowlist to keep, not two.
- **A fixed CA per sandbox** (`--proxy-ca-cert`): made at create, put in
  the guest by Lima's `caCerts` once. No 7-day session CA to refresh
  (today's nested-sandbox problem).
- **Later, the shared login (track L / sbx-style `/login`):** nono's
  `--credential` and its `oauth_capture` sit in the same process.
- **Cost:** nono becomes a host dependency (accepted by the engineer,
  2026-10-13) (today it is only in the
  image). Pin the same version the image pins; macOS gets it from
  Homebrew or nono's release. Fallback, if A2.0 fails: a small Python
  CONNECT proxy with a hostname allowlist (credproxy.py's style), as D11
  first planned.

## The proxy's lifetime: owned by the QEMU wrapper

D11 has `start`/`stop`/`rm` manage a proxy daemon, with a port in state.
Proposed instead: `bin/wmf-sbx-qemu` (the `QEMU_SYSTEM_<ARCH>`
wrapper) **starts the proxy, runs QEMU as its child, and stops the proxy
when QEMU exits**.

- The proxy lives exactly as long as the VM, whoever stops it
  (`wmf-sbx stop`, `limactl stop`, a crash). No pidfile, no stale port,
  no "proxy died but VM runs" state to detect.
- It is in the same process tree (and nono sandbox, when nested) as
  Lima's host agent, so the cross-session stop problem of 5e5f466 does
  not get a second instance.
- The wrapper picks the port, then puts it in the `guestfwd`:
  `guestfwd=tcp:192.168.5.100:3128-cmd:socat - TCP:127.0.0.1:<port>`.
- To check: Lima waits on the process it spawned and signals the pid in
  QEMU's `-pidfile`; a wrapper in between must pass SIGTERM/SIGINT on
  and exit with QEMU's status (A2.0, item 6).

## Opening a host while a sandbox runs (`wmf-sbx allow`)

The engineer often wants to let one unusual host through for the task at
hand, without ending the session or rebuilding the sandbox. `nono proxy`
cannot change its allowlist while it runs (no reload flag, signal or
control socket in 0.78). So the proxy is **replaced**, not edited, and
the VM does not notice:

- The `guestfwd` command reads the port at each new guest connection:
  `cmd:sh -c 'exec socat - TCP:127.0.0.1:$(cat <egress dir>/port)'`
  (a fixed string the wrapper writes; no guest data in it).
- `wmf-sbx allow NAME HOST|URL-glob [--for-session]`, on the host: adds
  the entry to the sandbox's extra list, has the wrapper start a new
  proxy with it, and rewrites `port`. New connections go to the new
  proxy; the old one keeps its open tunnels and stops when the VM does
  (or when idle). `wmf-sbx allow --rm`, `allow --list` the same way.
  (A2.0 item 8 RAN: a host went 000 → 200 under a running VM, and a
  download through the old proxy finished.)
- It runs on the host, as the engineer. Nothing in the guest can call
  it, so root in the guest still cannot widen its own egress.
- Contained mode (nono inside too) gets the same when the inner nono
  runs `--upstream-proxy 192.168.5.100:3128 --allow-domain '*'`: the
  inner nono keeps the endpoint rules, and the outer proxy is the only
  allowlist (A2.0 item 9 RAN; NOTES). The guest needs name lookups that
  succeed (see A2.3).

## A2.0 Measure (throwaway scripts, `lima-port/checks/sudo-egress.sh`)

In this nested `dev` VM (KVM, Lima 2.2.1), with one inner QEMU VM started
with a hand-written wrapper (`restrict=on`, the `guestfwd` above) and
`nono proxy --profile profiles/wmf-engineer.json` plus `REGISTRY_DOMAINS`
and the Debian mirror:

1. `nono proxy` starts on the host side. Nested: it runs inside the
   session's nono, chains with `--upstream-proxy` to the session's proxy
   (which needs a password: does `--upstream-proxy` take credentials, or
   does `NONO_PROXY_PASS`/`https_proxy` apply?), and trusts the session
   CA for the second interception.
2. From the guest, with `https_proxy=http://nono:PASS@192.168.5.100:3128`
   and the sandbox CA installed: GET to an allowed wiki is 200, POST to
   it is 403, a host off the list is 403, `host.lima.internal` and a
   direct `140.82.112.3:443` fail, also after `nft flush ruleset` as
   guest root.
3. With no guest DNS (`hostResolver.enabled: false`, `restrict=on`):
   `apt-get update && apt-get install`, `npm ci`, `composer install`,
   `git clone https://…`, `pip install`, `mw-install-browser`. Which of
   them resolve names themselves and fail?
4. **Claude Code through `HTTPS_PROXY`** (the open A1 item): a turn with
   a tool call, and which hosts it reaches.
5. Throughput: `npm ci` in Cite and core, `socat` per connection, against
   the same in a contained sandbox.
6. The wrapper as parent: `limactl start`, `stop`, `stop --force`, a
   killed QEMU; the proxy goes with the VM each time; Lima's probe calls
   (`--version`, `-M none -accel help`, `-netdev help`) pass through.
7. The same, unnested, on the engineer's host (KVM), and on macOS with
   HVF (`TESTING-MACOS.md` step 9; ask Kosta). Both run
   `checks/sudo-egress-m7.sh all` (items 2, 6 and 8 and the clock, on a
   throwaway Lima VM; it passed nested, NOTES).

8. Replacing the proxy under a running VM: a new proxy with one more
   host, `port` rewritten; a new guest connection to that host works, a
   download that was running through the old proxy finishes.
9. Contained mode behind the outer proxy: the session's nono with
   `--upstream-proxy` to it and no domain filter of its own (is there an
   allow-all, or does it need every host listed?), filesystem rules
   unchanged. Does the outer proxy then see the hostnames (CONNECT) and
   do its endpoint rules still apply (it must intercept TLS behind
   nono's interception, or nono must not intercept)?

Exit: results in `sbx/NOTES.md` (a new "`--sudo` egress (track A)"
section), and a go/no-go on `nono proxy`.

**Found (2026-10-13, NOTES "`--sudo` egress"):** the policy part works
(endpoint rules through `nono proxy`); `--upstream-proxy` cannot send
credentials, so a host behind an authenticated proxy, and every nested
sandbox, needs an auth-adding shim between `nono proxy` and the upstream
(a small Python CONNECT forwarder; it can live in `egress.py`;
`checks/upstream-auth-shim.py` is the throwaway). Items 2, 3, 4 and 8
pass, with these for A2.1–A2.3: the guest's nft host block needs a rule
for 192.168.5.100:3128; `cmd:` needs `sh -c`; `/etc/gitconfig`
`http.proxyAuthMethod=basic` and `NODE_USE_ENV_PROXY=1` in the guest.
Go on `nono proxy`; 5, 6, 7 and 9 remain.

## A2.1 The wrapper: `sbx/bin/wmf-sbx-qemu` (Python)

Landed (2026-10-13) as `bin/wmf-sbx-qemu` + `src/wmf_sbx/qemu_wrapper.py`
(it runs on the host, so `bin/`, not `helpers/`). The shim is a thread
in the wrapper; SIGUSR1 starts a new proxy (for `wmf-sbx allow`); the
proxy and QEMU get `PR_SET_PDEATHSIG` on Linux. Items 5 and 6 RAN
(NOTES); the guest-clock skew they found goes to A2.3.


- Probe calls (no `-pidfile`/`-qmp`): exec the real QEMU unchanged.
- The boot call: exactly one `-netdev user,…`, no other `-netdev` or
  network `-device`, else refuse (exit non-zero, a message on stderr
  that Lima shows). Append `restrict=on,guestfwd=…`.
- Starts the proxy (its argv from a file wmf-sbx wrote, see A2.2), and
  `egress.AuthShim` (a thread of the wrapper) when the upstream needs
  credentials (one process tree: the engineer's decision, 2026-10-13),
  waits for it to listen, runs QEMU, forwards signals, stops the proxy.
  The proxy's stdout goes to /dev/null: without `--silent` (which also
  stops the decision log) it prints a banner.
- Finds the real QEMU from `WMF_SBX_QEMU_REAL` or PATH, never itself.
- Tests: argv rewriting and refusals as pure functions; the lifetime
  with a fake QEMU and a fake proxy (two small scripts).

## A2.2 The proxy config: `sbx/src/wmf_sbx/egress.py`

Landed (2026-10-13), with the shim, the per-sandbox password and `rm`
removing the directory; `--proxy-ca-cert` with its CA and the shim ran
against the session proxy (wiki GET 200, POST 403, off-list 403). The
password went out after item 9: the proxy runs `--no-auth`.


- `proxy_argv(state, env)`: `nono proxy --silent --profile
  <this checkout>/profiles/wmf-engineer.json`, `--allow-domain`
  for `REGISTRY_DOMAINS`, the Debian mirror and security hosts,
  `repos.yaml` extras; `--proxy-ca-cert/--proxy-ca-key` of the sandbox;
  `--upstream-proxy` when the host has one (as `session.upstream_proxy`);
  `--log-file` under `~/.local/state/wmf-sbx/egress/<name>/`.
- Written at each start to `egress/<name>/proxy.json` (0600), which the
  wrapper reads, so a start picks up a new allowlist or upstream.
- `make_ca(name)`: an EC P-256 CA, 0600 key, at create; `rm` removes the
  directory.
- `guest_files(name)`: `/etc/environment` proxy variables with
  `no_proxy=localhost,127.0.0.1`, `/etc/apt/apt.conf.d/90wmf-sbx-proxy`,
  for the provisioning script.
- Tests: the argv and files from a sample state and env, no nono run.

## A2.3 The template and start

Landed (2026-10-13), as `create --egress-proxy` (QEMU only; refused
with `vz`; with `--nested` the egress list has `nested.DOMAINS` too, not
yet run), not yet `--sudo`: the agent still has no sudo,
which is A3. The A2 exit RAN in a nested sandbox (NOTES): the invariants,
the A2.0 item 3 installs, a contained nono behind the proxy, and `wmf-sbx
allow` opening a host under a running contained process (000 → 200). How
it differs from the plan below:

- One guest address for the three guestfwds: 192.168.5.100, port 3128
  the proxy, 53 the resolver, 3129 the session's credential proxy (on a
  Unix socket, `egress/<name>/cred.sock`). So no nft rule per session,
  and one session per egress sandbox at a time (a second is refused
  while the socket answers).
- The resolver: glibc straight at it (`nameserver 192.168.5.100`,
  `options use-vc`), systemd-resolved masked.
- `wmf-sbx allow NAME HOST... | --rm HOST... | --list` (no
  `--for-session`, no URL globs): hosts only, as `--allow-domain`.
- The egress list also has `registry.nono.sh`, which create's
  `wmf-claude-setup` pulls the base pack from.
- Found at the first real create: behind `restrict=on` the guest has no
  default route (the host block now takes the interface toward
  192.168.5.2), and Lima's first-boot step installs rsync before the
  provisioning gives apt the proxy (the image now has rsync).
- Not run end to end: a Claude session in an egress sandbox (the
  credential proxy on its socket is covered by unit tests only).

- `--sudo` template: `vmType: qemu` (refuse `vz`), `hostResolver.enabled:
  false`, `propagateProxyEnv: false`, the sandbox CA in `caCerts`, the
  guest proxy files in the provisioning. The `PROXY_PORTS` host block is
  not needed (nothing in the guest reaches 192.168.5.2) but stays,
  harmless.
- `vm.ensure_running`: for a `sudo` sandbox, write `proxy.json` and give
  `limactl start` `QEMU_SYSTEM_<ARCH>=…/wmf-sbx-qemu`,
  `WMF_SBX_QEMU_REAL` and `WMF_SBX_EGRESS_CONFIG`.
- Where this meets track B (B2 landed on `lima-port` before A2.3):
  - `vm.create(name, tmpl, lima, log, backing, env)` makes the QEMU disk a
    qcow2 overlay after `limactl create` and before the first `limactl
    start`. The wrapper's environment goes on that first start (and on
    every later one), so the order stays: create, overlay, check_disk,
    start behind the wrapper, check_disk (`qemu-img info -U`).
  - QEMU templates use `disk.placeholder_path` as the image. The egress CA
    goes in `caCerts` beside the extra CA files (`template.py`).
  - `image.images_in_use` reads each Lima instance's disk header and `rm`
    removes the overlay with the instance; `rm` removes `egress/<name>/`
    too.
  - B3 stops and restarts the VM at the end of `create` to freeze a
    snapshot. Each start runs a new wrapper with a new proxy (on a new
    port) from the same config; item 7's lifecycle checks show a stopped
    VM starts again behind it.
- Name lookups in the guest: the inner nono of a contained session
  looks up each name before it CONNECTs upstream, and behind
  `restrict=on` with `hostResolver.enabled: false` nothing answers
  (A2.0 item 9). It does not use the address. Decided (the engineer,
  2026-10-13): a **fake-address resolver**. It never asks a real
  resolver: a real one through a guestfwd would be a DNS tunnel past
  the allowlist.
  - Each new name gets the next address in one private block, and the
    resolver keeps the mapping for the sandbox's life: the same name
    gets the same address, and no address is used twice. A record only.
  - So a blocked request for an address can be mapped back to the name
    it was for (diagnosis, in the proxy log or `wmf-sbx status`). Room
    for later, nothing needs it now: the proxy could turn a CONNECT to a
    mapped address back into a CONNECT to its name, then apply the
    allowlist to the name.
  - The block: 198.18.0.0/15 (reserved for benchmarking, RFC 2544; the
    usual block for fake-address DNS) does not clash with Docker's
    172.17/16, Lima's 192.168.5/24 or a LAN, and has 131,072 addresses.
    It is not in the guest's `private4` set, so a direct connection to
    one is dropped by `restrict=on`, not rejected by the nft rule: add
    the block to that set, so a direct connection fails at once.
  - A records only; AAAA answers NODATA, so clients use IPv4. Names in
    /etc/hosts (localhost, the guest's own name) are answered from that
    file first (nsswitch), never by the resolver.
  - It runs **on the host**, next to the egress proxy (the engineer,
    2026-10-13), as a thread of the wrapper like AuthShim, so the two can
    share the mapping later. Root in the guest (`--sudo`) cannot change
    the mapping. The mapping is an append-only file in `egress/<name>/`.
  - The guest reaches it through a second guestfwd (e.g.
    `192.168.5.101:53`), which is TCP only. guestfwd TCP is QEMU's
    slirp: the guest's TCP ends inside QEMU, and the host side is a
    local socket, so TCP costs little. To measure: the guest's resolver
    must go to TCP at once. glibc does with `options use-vc`;
    systemd-resolved tries UDP first and goes to TCP only after
    timeouts (seen in item 9's journal). So point glibc straight at it,
    or measure resolved's first lookup.
- A contained session in a `sudo` sandbox: the session's nono gets
  `--upstream-proxy 192.168.5.100:3128 --allow-domain '*'` instead of
  the domain list, so `wmf-sbx allow` reaches it.
- The guest's clock: nono's leaf certificates start at "now", and the
  guest has no NTP behind `restrict=on`. Lima's own time sync (guest
  agent, virtio-serial) keeps the guest within 100 ms after its first
  sync, about 2 s after READY (NOTES). So: keep `plain: false` (no guest
  agent, no sync); `start` waits for the first "Time sync:" line in
  `ha.stderr.log` before it returns; ask nono upstream to backdate
  `notBefore` (mitmproxy backdates its leaves) for the 100 ms that is
  left. `ptp_kvm` + chrony only if item 7 shows the sync is not enough.
- The `sudo` invariants (replace "the agent is not in sudo"): an
  allowlisted host through the proxy answers; a host off the list, a
  POST to a read-only wiki, `host.lima.internal:22` and a direct
  `140.82.112.3:443` do not; the guest has no route.
- `status`: the proxy answers (a `CONNECT` from the guest), the CA's
  expiry.

Exit for the proxy work (A2): a contained sandbox on QEMU behind the
host proxy (`restrict=on`, nono still inside; the engineer's decision,
2026-10-13: a valuable step in itself) passes the invariants and the
A2.0 item 3 installs, and `wmf-sbx allow` opens a host in it while a
session runs. The proxy lands before the trusted-VM session.

## A3 `--sudo` end to end (after A2)

- `create --sudo` / `--no-sudo`, `settings.yaml` `create: sudo:` (once
  `session: sudo:`); recorded in state; `exec`, `status`, `resume` read it.
- The image grants nothing new; provisioning adds `agent` to `sudo`
  NOPASSWD only in a `sudo` sandbox, with a marker like `nested`'s, so
  the invariants know which mode they check.
- The session: `claude --plugin-dir /opt/wmf-claude --settings
  …/settings-merge.json`, no nono; a new `hooks/context/lima-sbx-sudo/`
  text (sudo, apt through the proxy, no DNS).
- **The credential proxy:** `restrict=on` cuts 192.168.5.2, so
  credproxy's `ANTHROPIC_BASE_URL=http://192.168.5.2:PORT` stops
  working. Options: (a) a second `guestfwd` at a fixed guest address to
  a per-sandbox Unix socket that each `resume` binds; (b) nono proxy's
  `--credential` with the host token, which also starts track L's
  shared login. Decide after A2.0. **Decided by A2.3:** (a), on
  192.168.5.100:3129 (`cred.sock`); nothing more to do here.
- The session's network is the egress proxy's variables (as `exec` has
  them, `vm.guest_proxy_env`), with no nono proxy in between. apt's
  proxy file, the fake DNS and the egress CA are guest configuration and
  need no change. Root can undo any of them; that only cuts root off,
  since `restrict=on` and the host proxy decide what leaves. The
  invariants that matter here exist already ("no route out but the
  egress proxy", "names resolve only to the fake block").
- **`--sudo --nested`: a loopback forwarder.** An inner VM's only way out
  is 192.168.5.2, the outer VM's loopback. In contained mode the
  session's nono proxy listens there; with `--sudo` nothing does (the
  same gap `wmf-sbx exec` has today for an inner `create`). Add a
  forwarder in the outer VM, a root-owned systemd unit on
  `127.0.0.1:PORT` to 192.168.5.100:3128, and point the session's proxy
  variables at it, so `image.guest_proxy_env`'s rewrite gives inner VMs
  `192.168.5.2:PORT`. Do it for every egress sandbox with `nested`
  (contained mode too): `exec`'s inner creates then work as well.
- **`--sudo --nested`: the CA for inner VMs.** In contained mode they
  trust nono's session CA (`image.extra_ca_files` takes
  `$SSL_CERT_FILE`); without nono that variable is unset. Inner VMs need
  the egress CA the outer VM trusts (from its `caCerts`), or every TLS
  request through the forwarder fails.
- The nested warning's wording: under `--sudo` there are no nono grants
  to warn about (the agent has root, which is more); say that instead.
- `exec --engineer` is meaningless with `--sudo`: say so.
- `sbx/SECURITY.md`: the residuals of D9 (the guest side of every
  channel is hostile; the two-user split is not a boundary).

Exit (as HANDOFF §11): `mw-install-browser` runs on demand through the
proxy; the blind run passes in a `--sudo` sandbox on Linux.

### A3 steps (written 2026-10-14, after the egress default ran)

The egress proxy is now the default on QEMU and RAN end to end (NOTES), so
`--sudo` is mostly the guest grant, a second kind of session, and texts.
In order, each a commit with its unit tests:

1. **create and state.** `--sudo/--no-sudo` (BooleanOptionalAction, default
   None) takes its default from `settings.yaml` `session: sudo:` (a bool; the
   built-in default is false, D9). `--sudo` needs the egress proxy:
   `egress_choice` treats it as an explicit `--egress-proxy` (vz and missing
   host tools are errors), and `--sudo --no-egress-proxy` is an error. Until
   step 6, `--sudo --nested` is an error. The state gets `"sudo": true`;
   `status`, `ls` and `--dry-run` show the mode. create says in a dim line
   that the agent has root in the VM and no nono, and the host proxy is the
   network boundary. The old "not ported yet" error goes.
2. **The guest grant.** `sandbox-provision.sh` gets `@AGENT_SUDO@`
   (template.py). With it: `/etc/sudoers.d/90-wmf-sbx-agent` (`agent
   ALL=(ALL) NOPASSWD: ALL`, plus `Defaults:agent env_keep` for the proxy
   variables, so `sudo curl` and the helpers' `sudo apt-get` find the
   proxy; checked with `visudo -cf`) and the marker `/etc/wmf-sbx/sudo`.
   Without it, both are removed, as is the group removal now. The agent is
   never put in the `sudo` or `docker` group. The provisioning runs at every
   boot from the instance config, so no new image is needed.
   `vm.invariants(egress, sudo)`: a `sudo` sandbox swaps "the agent is not
   in sudo or docker" and "the agent cannot sudo" for "the agent is not in
   docker" and "the agent can sudo"; the egress ones are required. A
   contained sandbox still fails if the sudoers file is there. The guest
   checks are only diagnostics in a `sudo` sandbox (root can make them
   pass); SECURITY.md says so.
3. **The session.** `session.launcher_argv` for a `sudo` sandbox: the
   credential wrapper, then `/opt/wmf-claude/bin/launch-claude.sh
   --plugin-dir /opt/wmf-claude --settings
   /opt/wmf-claude/wiring/settings-merge.json` + Claude's args. No bin/claude,
   no nono. The env: `WMF_CLAUDE_SANDBOX_BACKEND=lima-sbx-sudo`,
   `WMF_CLAUDE_HOME=/opt/wmf-claude` (the status line),
   `WMF_CLAUDE_SESSION` (the mode, for the status line),
   `WMF_CLAUDE_SBX_EGRESS`, and the egress proxy's variables
   (`vm.guest_proxy_env`: `sudo -u agent` drops those of
   `/etc/environment`, and nono set them before). To check: Node finds the
   egress CA (WebFetch through the proxy). `resume` refuses bin/claude flags
   (`--allow-post` etc.) for a `sudo` sandbox, with why, and does not add
   `--open-port`. The credential proxy is unchanged (`cred.sock`).
4. **What Claude is told.** `hooks/context/lima-sbx-sudo/{sandbox,
   environment}.txt`:
   - no nono; the agent has passwordless sudo in a disposable VM;
   - the network is the host proxy's: the allowlist and the profile's
     endpoint rules (a wiki POST gets 403), `wmf-sbx allow`;
   - `sudo apt-get install` works through it;
   - do not change the VM's network setup (nft, `/etc/environment`, the
     resolver): it cuts only this VM off.
   `session.home_claude_md` and `RUNNING_TESTS_MD` get `sudo` wording ("with
   passwordless sudo, no nono", no `--local-web`). The new backend joins
   test-templates.sh's backend checks.
5. **Docs.**
   - sbx/README.md: `create --sudo`, `session: sudo:`, the two modes.
   - sbx/SECURITY.md: the mode column and §6 residuals. The guest side of
     every channel is hostile: `git-remote-wmfsbx` fetches, `cp` out,
     `limactl shell` output, the Lima guest agent, the phantom credential.
     The two-user split is not a boundary.
   - lima-port/README.md.
   - NOTES.
   - `exec --engineer` on a `sudo` sandbox says the agent has sudo too.
6. **`--sudo --nested`** (as A3 above: the loopback forwarder for inner
   VMs, the egress CA for them, the warning's wording). Last, as its own
   step; step 1 refuses it until then. Written 2026-10-14 (NOTES): a root
   socket unit on 127.0.0.1:3128 with `systemd-socket-proxyd` to the host
   proxy, state `egressForward`, Lima's `cloud-init-ca-cert-*.crt` for the
   inner VMs, and no inner-VM stop at a `sudo` resume.

RAN on the engineer's host, after step 4:
- `create --sudo Cite`, its invariants, `status`;
- in a session: `sudo apt-get install` (one small package),
  `mw-install-browser`, the egress bullet and `allow`, a WebFetch (Node
  trusts the egress CA through `NODE_EXTRA_CA_CERTS`), and the status line
  (`WMF sandbox VM · sudo · host egress proxy`);
- as agent root:
  - `sudo nft flush ruleset`, then a direct `140.82.112.3:443`, a host off
    the list and a wiki POST all fail;
  - `mount -o remount,rw` on a mount, then a write: the host is unchanged.
- The HANDOFF §11 blind run.

To RUN on the engineer's host, after step 6:
- `create --sudo --nested wmf-claude`: the warning, the forwarder line,
  `status`;
- in the session: `systemctl status wmf-sbx-egress-forward.socket`, then
  an inner `wmf-sbx create` (an image build and an inner VM, through the
  forwarder; TLS with the egress CA);
- `wmf-sbx exec` with an inner `wmf-sbx create` or `limactl start`;
- `resume` after the session ends, with the inner VM still up.
- **The acceptance test.** In a `--sudo --nested` sandbox of wmf-claude,
  the agent changes its own clone of wmf-claude, then uses that clone's
  `sbx/bin/wmf-sbx` to create an inner sandbox and runs the acceptance
  test in it, against the changed copy. Step 6 is done when this passes.

## Later, not in this track

- Contained mode on the same proxy (NOTES "Local ports" bookmark): the
  local-port window, `--landlock-only` and the DNS residual could go.
  The QEMU half only; `vz` sandboxes keep nft + nono.
- A Lima feature request for a native `restrict`/egress-proxy option, to
  drop the wrapper.
