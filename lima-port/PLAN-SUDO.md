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
Proposed instead: `helpers/wmf-sbx-qemu` (the `QEMU_SYSTEM_<ARCH>`
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
- Contained mode (nono inside too) gets the same only if the inner nono
  stops filtering domains and chains everything to the outer proxy
  (A2.0, item 9). Then the outer proxy is the single policy point for
  both modes. Today, a contained session's allowlist is fixed when the
  session starts.

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
   HVF (`TESTING-MACOS.md`; ask Kosta).

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

## A2.1 The wrapper: `sbx/helpers/wmf-sbx-qemu` (Python)

- Probe calls (no `-pidfile`/`-qmp`): exec the real QEMU unchanged.
- The boot call: exactly one `-netdev user,…`, no other `-netdev` or
  network `-device`, else refuse (exit non-zero, a message on stderr
  that Lima shows). Append `restrict=on,guestfwd=…`.
- Starts the proxy (its argv from a file wmf-sbx wrote, see A2.2), and
  `egress.AuthShim` in its own process when the upstream needs
  credentials (one process tree: the engineer's decision, 2026-10-13),
  waits for it to listen, runs QEMU, forwards signals, stops the proxy.
  The proxy's stdout goes to /dev/null: without `--silent` (which also
  stops the decision log) it prints the password.
- Finds the real QEMU from `WMF_SBX_QEMU_REAL` or PATH, never itself.
- Tests: argv rewriting and refusals as pure functions; the lifetime
  with a fake QEMU and a fake proxy (two small scripts).

## A2.2 The proxy config: `sbx/src/wmf_sbx/egress.py`

Landed (2026-10-13), with the shim, the per-sandbox password and `rm`
removing the directory; `--proxy-ca-cert` with its CA and the shim ran
against the session proxy (wiki GET 200, POST 403, off-list 403).


- `proxy_argv(state, env)`: `nono proxy --silent --profile
  <this checkout>/profiles/wmf-engineer.json`, `--allow-domain`
  for `REGISTRY_DOMAINS`, the Debian mirror and security hosts,
  `repos.yaml` extras; `--proxy-ca-cert/--proxy-ca-key` of the sandbox;
  `--upstream-proxy` when the host has one (as `session.upstream_proxy`);
  `--log-file` under `~/.local/state/wmf-sbx/egress/<name>/`.
  The password goes in by `NONO_PROXY_PASS`, not argv.
- Written at each start to `egress/<name>/proxy.json` (0600), which the
  wrapper reads, so a start picks up a new allowlist or upstream.
- `make_ca(name)`: an EC P-256 CA, 0600 key, at create; `rm` removes the
  directory.
- `guest_files(password)`: `/etc/environment` proxy variables with
  `no_proxy=localhost,127.0.0.1`, `/etc/apt/apt.conf.d/90wmf-sbx-proxy`,
  for the provisioning script.
- Tests: the argv and files from a sample state and env, no nono run.

## A2.3 The template and start

- `--sudo` template: `vmType: qemu` (refuse `vz`), `hostResolver.enabled:
  false`, `propagateProxyEnv: false`, the sandbox CA in `caCerts`, the
  guest proxy files in the provisioning. The `PROXY_PORTS` host block is
  not needed (nothing in the guest reaches 192.168.5.2) but stays,
  harmless.
- `vm.ensure_running`: for a `sudo` sandbox, write `proxy.json` and give
  `limactl start` `QEMU_SYSTEM_<ARCH>=…/wmf-sbx-qemu`.
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

- `create --sudo` / `--no-sudo`, `repos.yaml` `agentSudo:`; recorded in
  state; `exec`, `status`, `resume` read it.
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
  shared login. Decide after A2.0.
- `exec --engineer` is meaningless with `--sudo`: say so.
- `sbx/SECURITY.md`: the residuals of D9 (the guest side of every
  channel is hostile; the two-user split is not a boundary).

Exit (as HANDOFF §11): `mw-install-browser` runs on demand through the
proxy; the blind run passes in a `--sudo` sandbox on Linux.

## Later, not in this track

- Contained mode on the same proxy (NOTES "Local ports" bookmark): the
  local-port window, `--landlock-only` and the DNS residual could go.
  The QEMU half only; `vz` sandboxes keep nft + nono.
- A Lima feature request for a native `restrict`/egress-proxy option, to
  drop the wrapper.
