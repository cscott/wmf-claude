# Track A (`--sudo`): handoff, 2026-10-14

For: the next Claude session on track A, in a sandbox made with the latest
code. Written by the session in the contained nested sandbox `dev`, whose
history the rebuild erases. Read this, then `PLAN-SUDO.md` (A3 steps), then
`sbx/NOTES.md`.

## Where track A stands

The A3 steps 1 to 6 of `PLAN-SUDO.md` are done and RAN on the engineer's
host. The last commits (by subject; Track B cherry-picks them onto GitHub
`lima-port`, so the IDs change):

- `wmf-sbx: --sudo --nested (A3 step 6)`: the loopback forwarder
  (`wmf-sbx-egress-forward.socket`, 127.0.0.1:3128 to 192.168.5.100:3128,
  state `egressForward`), the egress CA for inner VMs
  (`image.GUEST_CA_GLOB`), and no inner-VM stop at a `sudo` resume.
- `credproxy: Chain to $ANTHROPIC_BASE_URL, for a nested sandbox`: only
  when `/etc/wmf-sbx/nested` exists.
- `wmf-sbx: Pass the phantom to inner sandboxes in the nested marker`:
  `resume` of a nested sandbox writes the session's phantom and base URL
  to `/etc/wmf-sbx/nested` (root:agent 0640); `credproxy.host_source` and
  `upstream_url` read it, and the session gets `--read-file` on it.
- `wmf-sbx tests: Hide the nested marker from the credential tests`.

RAN on the engineer's host, 2026-10-14, in `create --sudo --nested --name
egsn ~/Projects/Wikimedia/wmf-claude` (defaults: 10 GiB, 120 GiB):

- create: every check, the nested warning, the forwarder line, `status`.
- In the session: the forwarder is up; an inner `create` of Translate
  (`sbx-testverify`) built its image and booted through the forwarder,
  with TLS on the egress CA.
- `wmf-sbx exec egsn -- …`: the proxy variables are the forwarder's; an
  inner `stop`/`start` and an inner `exec … curl` (301) worked.
- `resume egsn` with the inner VM up: the inner VM kept running.
- The marker: `640 root:agent`, the base URL and the phantom. An inner
  `env -u CLAUDE_CODE_OAUTH_TOKEN …/wmf-sbx resume sbx-testverify -- -p
  "say hi"` answered: the credential came from the marker alone.
- **The acceptance test**: the agent in egsn ran the blind run in
  `sbx-testverify` against its own clone of wmf-claude. Report:
  `lima-port/acceptance/runs/2026-10-14-nested-egsn/REPORT.md` on the
  egsn branch (commit "DO NOT MERGE: The nested acceptance run's report",
  not on `lima-port`). Everything passed except `composer phan` in
  Translate (below).

So step 6 is done by its own definition. What is left is below.

## Open, in order

1. **Phan in an inner sandbox is OOM-killed.** `nested.INNER_MEMORY` is
   4 GiB; Translate's phan reached 2.65 GB in a 3.9 GB guest and was killed,
   twice (exit 137). NOTES already says phan needs 8 GiB. Decide with the
   engineer: raise `INNER_MEMORY` (and `nested.MEMORY` to fit it with the
   outer session), or keep 4 GiB and give the acceptance run
   `--memory 8GiB`. The 10/4 GiB defaults predate the balloon (NOTES "Host
   memory"); with free-page reporting a larger outer VM costs the host
   little while idle.
2. Done: **the intermittent ERROR in the sbx unit suite.** Two causes, both
   in tests: `limactl validate` refused a tempfile name ending in `_` or
   holding `__` (the two "valid for limactl" tests), and the QEMU wrapper
   tests' `alive()` raised ESRCH when a process was reaped while it read
   `/proc/PID/stat`.
3. Done: **tests never read the machine's real state.** Every test module
   calls `isolation.isolate()` (empty HOME, no credential variables, no
   nested marker, `claude`/`security` stubs); a test checks it. NOTES
   "Project rules".
4. Done: **the RAN is recorded** in `PLAN-SUDO.md` and `sbx/NOTES.md`.
5. Then `PLAN-SUDO.md` "Later, not in this track", or what the engineer
   asks for.

## How to work here

- **Branch:** commit on `lima-port` in the sandbox clone. The engineer
  fetches it as `dev-lima-port` and cherry-picks onto GitHub
  `https://github.com/cscott/wmf-claude.git` `lima-port`, which is the
  rebase base (not `origin`, which is GitLab and has no `lima-port`). The
  engineer's `github/*` refs in `local` can be stale; to move onto it:
  `git fetch https://github.com/cscott/wmf-claude.git
  lima-port:refs/remotes/github/lima-port`, then `git switch -C lima-port
  github/lima-port` (never `reset --hard`) and rebase newer commits.
- **Commit trailer:** `Assisted-by: Claude Opus 5.5
  <noreply@anthropic.com>` (the engineer's choice for this repo).
- **Untracked files in the checkout are the engineer's** (session exports,
  `responses*.txt`,
  `eg*-*.txt`, `sudo-egress-m7-logs/`): never commit them; stage explicit
  paths only.
- **Names:** `WMF_SBX_*` is host configuration only; `WMF_CLAUDE_*` is for
  variables the sandbox sees.
- **Wording:** say what the code does, not what it leaves out, when an
  equivalent positive wording exists.
- Do not edit `profiles/wmf-engineer.json`. Never print a phantom or a real
  credential. Tests never POST to a production host. On the engineer's host
  `example.com` does not resolve: use `www.kernel.org`.
- Host checks are the engineer's: write the exact commands for them to run,
  and record what they report as RAN in NOTES.
- In a `--sudo` sandbox there is no nono and the agent has sudo; the VM and
  the host egress proxy are the boundary. Do not change the VM's network
  setup (nft, `/etc/environment`, the resolver).
