# wmf-sbx notes

Open to-do items, and the decisions and findings that the code depends on
or that future work needs. No history: `git log` has it. The design is in
`README.md` (Architecture) and `../lima-port/HANDOFF-LIMA.md` (decisions D1
to D12, and tracks A, B, C and L, which are not repeated here; A and B are done). What is open on macOS, and the
tests for a Mac, are in `MACOS.md`.

## To do

### Before a wider release: remove the development leftovers

Decided (cananian, 2026-10-13): while cananian is the only user, these stay, as reminders.
Just before others use wmf-sbx, remove them; to others they are only cruft.

Shims for old names and old local state:
- [ ] **`WMF_CLAUDE_GIT_HASH_OVERRIDE`** (now `WMF_SBX_GIT_HASH_OVERRIDE`): remove
  `image.OLD_TREE_OVERRIDE_VAR`, the note in `image.tree_override`, its test, and the
  README's "first name" sentence.
- [ ] **`repos.yaml` and its old keys** (`rules`, `extra_environment`, `extra_packages`):
  remove `resolve.LEGACY_CONFIG`, `LEGACY_KEYS`, `_LEGACY_NOTED` and `load_config`'s
  `legacy` argument, their tests, the README's "Before `settings.yaml`" sentence, and the
  `settings.yaml` finding's "(was ...)" notes in this file.
- [ ] **Sandboxes from an older image**: the "sandbox is older than that" sentence in
  MEDIAWIKI-TESTING.md §5.6 (Cypress).
- [ ] **State files from older code**: the "made before this key existed" fallbacks and
  comments: `attached` and `primaryDir` (state.py, resume.py, exec.py), `nestedSrc`
  (`nested.session_grants` with no src; the "keeps its old settings" note under "Nested
  Lima"), and a manifest with no `commit` (`image.py`, `newest_image`'s caller). Check
  each with `git log -S` first: keep a fallback that other users can hit.
- [ ] **The Phabricator username from `claude mcp get phabricator`** (create.py,
  `phabricator_username`, step 3, "for a host whose last install predates" config.json):
  remove it if upstream's installer has written config.json for long enough.

Docker sbx leftovers (cananian removed the last Docker sandboxes, 2026-10-16):
- [x] **`state.new_state` wrote `daemonPort`/`hostPort`** and `"backend": "lima"`: gone,
  with the Docker-state refusal in `state.require` (`ls` now skips only an empty state).
- [x] **`bin/wmf-sbx`'s `RETIRED` verbs**: gone; such a verb is now an unknown verb.
- [x] **`color.run_with_color`** (the pty wrapper for `sbx`) and its tests: gone.
- [x] **Docstrings and comments that named Docker sbx or `sbx create`/`sbx rm`**, and the
  unused `patch_host_probes` in the create tests.
- [ ] **`sbx/reference/Secure_use_guidelines.wt`** recommends `docker sbx`: it is a copy of
  a wiki page; refresh the copy (or the page) or drop it.

Development scaffolding:
- [ ] **Process references in code comments**: `lima-port/HANDOFF-LIMA.md`, phase
  numbers, D-numbers, N1–N3, and "(RAN ...)" in about 100 comments in 24 files
  (`git grep -c -E 'HANDOFF-LIMA|RAN\b|phase [0-9]' -- sbx/src sbx/bin sbx/helpers
  sbx/templates`). Keep the reason each one gives; drop the pointer to the process.
- [ ] **`lima-port/`**: the handoff documents, `acceptance/` and
  `checks/` are for this port. Decide what ships: move the checks that stay useful
  (`real-hardware.sh`, `cypress-nono.sh`, `credential-scan.py`) to `sbx/tests/` or
  `sbx/checks/`, the decisions that the code depends on to README.md or SECURITY.md, and
  drop the rest. Then fix every link to `lima-port/`.
- [ ] **Personal names and paths**: "cananian" (about 15 places in `sbx/`, not counting the
  tests), `~/Projects/Wikimedia`, cananian's host in the findings. Write "the engineer" and
  example paths.
- [ ] **This file**: trim it to what a maintainer needs (the decisions and findings that
  the code depends on); move the to-dos to Phabricator (#WMF-Claude).
- [ ] **`sbx/upstream/`**: drop what upstream took (!130 and !133, the STE switch, are
  merged), file the rest, and remove the directory when it is empty.

### Testing and the MediaWiki setup

- [ ] **A cheap class of clone for CI-only dependencies** (shallow, no host remote, no
  reset): `create` now adds CI's dependencies of the named repos (below), which are full
  clones like the others. (note from the engineer: declined for now in favor of
  better reuse of cloned repos)
- [ ] **Phan's `../../extensions/<Name>`, upstream**: the sandbox links `../../extensions` and
  `../../skins` beside each clone to core's (`setup.link_phan_paths`, where the name is
  free). The real fix is upstream: mediawiki-phan-config's `$IP` (`MW_INSTALL_PATH`, else
  `'../..'`, so CI is unchanged) in the ~170 Gerrit repos' `.phan/config.php`, ideally
  through a helper there. The links work (RAN 2026-10-13, engineer's host,
  `lima-port/checks/phan-links.sh`): Cite's phan is clean with CI's phan dependencies
  (cldr, CommunityConfiguration, Gadgets) outside nono (21 s) and under the session's nono
  (19 s), about 2.7 GB at its peak; with the links moved aside, 42 undeclared-symbol
  findings, all in those three.
  (engineer's note: my recollection is that is already a helper
  function registered by mediawiki-phan to add an extension as a
  dependency, but no extension is using it yet.)
- [ ] **Four core test failures, in review** (B4 blind run; handoff
  `sbx/upstream/HANDOFF-CORE.md`): (1) 11 AbuseFilter schema tests on the symlinks,
  Gerrit 1355223; (2) api-testing `ReparseDependentPages.js`, Gerrit 1355226 (the test)
  and 1355227 (`$wgUsePathInfo` in DevelopmentSettings); (3)
  `UndoIntegrationTest::testMcrUndoAction` with EventLogging, Gerrit 1355229; (4)
  `WikiBirthdayTest` with cldr, Gerrit 1355230. MEDIAWIKI-TESTING.md names each one and its
  change (§0, §2, §6 item 7). **Deferred until they merge** (cananian, 2026-10-15): then
  take each failure out of MEDIAWIKI-TESTING.md, run the blind acceptance run again, and
  delete HANDOFF-CORE.md.
- [ ] **Generate the testing section of `~/.claude/CLAUDE.md` per sandbox**: name each repo's real entry points (composer/npm scripts,
  Jest wiring, `tests/` dirs, `QUnitTestModule`, `tests/parser/*.txt`) and say which runners do
  not apply.
- [ ] **Watch for the TCG segfaults on real hardware**: Claude Code (Bun),
  jsdoc, a Jest worker, and QEMU itself once at boot; each passed on retry. Maybe the same as
  an unexplained `limactl start` exit 1 after 24 s, seen once.

### Git transport and host remotes

- [ ] **Verify host `git maintenance` and the alternates**:
  that the host's background maintenance does not prune objects that a `--shared` clone in
  the VM borrows while gc is suspended.
- [ ] **A full `wmf-sbx ls`**: sandboxes, their clones, and unfetched-commit
  counts per clone; `wmf-sbx-ls-remotes` lists only the host-side remotes.

### GitLab

- [ ] **GitLab token plumbing**:
  host side only, keep the anonymous path working, and never write the token into the plan,
  the VM or its environment. Needed for private projects and for a real
  `mr_default_target_self` (anonymous callers see `None`).
- [ ] **Full GitLab project listing with an on-disk cache, plus recorded-response fixtures**.

### Lima, QEMU and the disks

Copy-on-write disks and warm snapshots (HANDOFF-LIMA.md D12, B1 to B4) are done (closed
2026-10-15). Copy-on-write for `vz` is in `MACOS.md`, Part 2.

- [ ] **The golden image key includes the HEAD commit** (deferred, low priority; cananian,
  2026-10-15): every wmf-claude commit gives a new image, so a cold create, unless
  `WMF_SBX_GIT_HASH_OVERRIDE` is set. A key on a hash of only the files staged into the
  image would keep images and snapshots across host-only commits, but it adds a list of
  paths to maintain and a way to get the key wrong; the override is enough for now.

### Nested Lima (track C)

- N1 (engineer's host, KVM, a sandbox from `lima-port/checks/n1-spike.sh`): the agent runs
  a nested Lima VM under the session's nono with, besides the session's flags:
  `--allow ~/.lima`, `--allow-unix-socket-subtree-bind ~/.lima`, `--allow-file /dev/kvm`,
  `--read` of `/usr/share/qemu`, `/usr/share/seabios`, `/usr/share/OVMF`, `/usr/share/ovmf`,
  `/usr/local/share/lima`, `/usr/local/libexec/lima`, and `--listen-port` for the inner
  VM's SSH port (pinned in its template, 50022, inside the ephemeral window). The agent
  needs the `kvm` group; root installs QEMU, OVMF and Lima. Nested KVM works with no host
  change (`accel=kvm`; start in 22 s under nono, 31 s outside).
- N2: `create --nested` (state `nested`), `nested.py` (install script, grants, domains),
  the invariant "kvm group only in a nested sandbox". No `--listen-port` grant for the inner
  SSH port: `--open-port` is connect and listen, and Lima's port comes from the ephemeral
  window the session opens.
- `limactl` refuses root (`must not run as the root user`): the install runs it as the
  agent.
- `cloud.debian.org` redirects to geo-picked mirrors (`gemmei.ftp.acc.umu.se`), which no
  allowlist can list: the install downloads the base image (checked against its digest) to
  `/var/cache/wmf-sbx/base`, and the builder boots that copy (`image.local_base`; not in
  the cache key). `cloud.debian.org` is also the HEAD that refreshes a cached base image.
  Behind the egress proxy (`--egress-proxy --nested`) the guest cannot follow that redirect
  (RAN on the engineer's host: curl 403): the host downloads the base image once to
  `~/.cache/wmf-sbx/base/` (`nested.host_base`, digest checked) and `create` copies it in;
  the install checks the digest again.
- Inner egress works through nono's proxy. Lima and `image.guest_proxy_env` rewrite the
  session's proxy URL (`http://nono:PASSWORD@127.0.0.1:PORT`) to 192.168.5.2; the rewrite
  must keep the credentials. nono re-signs TLS for hosts with endpoint rules (nodejs.org;
  not deb.debian.org), so the inner VMs need nono's CA: in a nested sandbox, with no
  `WMF_SBX_CA_CERTS`, `image.extra_ca_files` gives them `$SSL_CERT_FILE`. The CA is the
  session's (7 days): an inner sandbox made in an earlier session has an old one.
- An inner image build reaches: deb.debian.org, security.debian.org, github.com (php-ast),
  nodejs.org, release-assets.githubusercontent.com, downloads.claude.ai, pypi.org and
  files.pythonhosted.org (MCP venvs). An inner create's `wmf-claude-setup` reaches the nono
  pack registry, `registry.nono.sh` (N3: a 403 that did not name the host; `RUST_LOG=debug
  nono pull` did).
- nono in the sandbox POSTs an update check to `update.nono.sh`; the session refuses it.
  This does no harm (nono goes on, and the image pins nono's version), so it gets no grant.
- `limactl` logs proxy URLs with their password: `lima.redact` removes them from LimaError.
- Inner sandboxes' repos go to `wmf-sbx-src` in the engineer's home path (state `nestedSrc`,
  e.g. `/home/cananian/wmf-sbx-src`; settings written by the install, if none), the one
  writable directory the session gives them. The outer VM has the clones at their host paths
  already, so this looks as it does on the host. Not under `/home/agent`: the inner VM has
  the clones at the same paths (D8), and that is its own agent's home (N3: create refused
  `~/wmf-sbx-src`). A sandbox installed before this keeps its old settings.
- `create` in a sandbox of the sandbox's own clone (`create --name inner .` in egnest) failed
  with "unable to read tree" (RAN): the clone's alternates name
  `/run/wmf-sbx/host/<path>/.git/objects`, which in the inner VM is the mount of that clone's
  own git dir, so the host's objects were not there. Earlier inner sandboxes cloned
  `wmf-sbx-src` repos, which borrow nothing. Now any repo's alternates (also a host
  `--reference` clone) are mounted too, and the clone in the VM lists their mounts
  (`repos.alternates`, `clone_repo`'s extra arguments; `template.is_mountable` lets the
  objects dir through the check). RAN in egnest (egress proxy): the inner clone has both
  alternates, status and log read every object, `git fsck --connectivity-only` exits 0.
  QEMU's 9p shares on the outer VM's own 9p mount of the host's objects without trouble.
- nono's CA: `$SSL_CERT_FILE` is the system bundle plus nono's CA (151 certificates);
  `image.session_ca_file` keeps the ones the system bundle lacks (compared as DER), at a
  fixed path (`~/.cache/wmf-sbx/session-ca.pem`), so a resume in a later session refreshes
  it, and so does every start of a stopped sandbox (`vm.ensure_running`). An inner VM
  reads the new file at its next start (Lima's cidata; RAN 2026-10-13: after a session
  restart, `start inner` gave the new CA in `session-ca.pem` and in the inner VM's
  `cloud-init-ca-cert-1.crt`, and `curl https://nodejs.org/dist/index.json` from inner got
  200, `issuer: CN=nono-session-ca`).
  An inner VM that runs across an outer session restart has the old proxy password, port
  and CA until it restarts, and the later session cannot restart it: its Lima host agent
  is in the earlier session's sandbox, and Landlock scopes signals to the sandbox, so
  `limactl stop` gets EPERM ("operation not permitted"), then waits 3 minutes for an exit
  that never comes (RAN). So `resume` of a nested sandbox first stops, as root outside
  nono, the inner VMs that still run (`nested.stop_inner`), unless a session runs in the
  sandbox (an agent `nono` process: its inner VMs are in use). In a session, `vm.stop`
  checks the host agent first (`kill -0` of `ha.pid`) and refuses with where to stop it
  from; the proxy-port warning says the same. Not RAN: the stop at `resume`.
- `exec` gives the agent's command `image.guest_proxy_env`: `sudo` drops the proxy that
  Lima writes to the inner VM's `/etc/environment`, and the inner VM has no other way out
  (N3: `mw-install-browser` got ENETUNREACH to googlechromelabs.github.io).
- `TMPDIR` must be the session's (`/tmp/claude-UID`, launch-claude.sh): `/tmp` is
  write-only under nono, and Lima builds its cloud-init ISO in TMPDIR.

### The egress proxy and `--sudo` (after track A)

Track A is done (2026-10-14); its findings are under "Decisions and findings".

- [ ] **Run the nested acceptance test again**, after the upstream bugs that the egsn run
  found are fixed (its report: `lima-port/acceptance/runs/2026-10-14-nested-egsn/REPORT.md`
  on the egsn branch, not on `lima-port`). Use the commands in `lima-port/acceptance/
  README.md`, which give the outer and the inner sandbox explicit sizes. It checks too that
  Translate's phan passes in an 8 GiB inner VM.
- [ ] **macOS**: the egress proxy on QEMU with HVF, and an equivalent of `restrict=on` on
  `vz`, which would remove `--no-egress-proxy` and the guest's network filtering:
  `MACOS.md`.
- [ ] **Propose upstream:**
  - Lima: a native `restrict`/egress-proxy option for QEMU, to drop the wrapper;
  - nono: backdate the leaf certificates' `notBefore` (mitmproxy does), for the up to
    100 ms that the guest's clock can be behind after Lima's sync ("The guest's clock");
  - nono: a live allowlist change for `nono proxy` (today `wmf-sbx allow` replaces the
    proxy), and credentials for `--upstream-proxy` (today `egress.AuthShim` adds them).

### Testing in the sandbox

- The manual-test skill (upstream) takes the base URL from a "Manual testing" section in
  CLAUDE.md: the sandbox's `~/.claude/CLAUDE.md` has one (`session.RUNNING_TESTS_MD`;
  docroot install, no `/w`). The skill's own `/w` wording: upstream !134.

### Session, credentials, Claude Code

- [ ] **Track L7: measure the one refresher again** (HANDOFF-LIMA, track L, L7): `oauth-l6.sh
  l7 l6-a l6-b` with the shorter gap, then `long l6-a l6-b` (9 hours, the real margin).
- [ ] **Upstream: file `sbx/upstream/NONO-ISSUE-1.md`** (nono's capture store is read
  once and written whole). When nono reloads and merges, remove
  `authproxy.refresh_proxy`.
- [x] **The "update installed, restart to update" message on the first resume**: gone
  since `wmf-claude-setup` skips `claude update` (`WMF_CLAUDE_SKIP_UPDATE=1`) and the
  version is pinned (cananian, 2026-10-16).
- [ ] **Upstream: grant the Linux system paths the tools read**. Propose `read`
  entries for `/opt/claude-code`, `/etc/php`, `/etc/gitconfig`, `/etc/bash.bashrc`,
  `/opt/node`, `/etc/fonts` in `profiles/wmf-engineer.json` (today the lima-sbx launcher
  grants them per session).
- [ ] **Upstream: Gerrit MCP write-tool denies** (HANDOFF §12). Gerrit's MCP server has
  20 tools; the skills use 5 read-only ones; 15 write under the engineer's credential.
- [ ] **Upstream topic: Claude Code's hooks tell the agent to offer profile changes after any
  nono denial, even a harmless `ls ~`**.
- [ ] To investigate: the original 'sbx' port ran the MCP on the *host
      side* of the sandbox, which allowed MCP services to have safe
      access to user secrets.  I think we've reverted to *guest side*
      MCP in the lima port.  If so, we should create a plan to
      transition back to host-side MCP in both sudo and nono modes.

- [ ] **Clipboard shim: use socat in place of bash `/dev/tcp`** if socat is added to the
  golden image later. `clipboard.GUEST_SHIM` opens the guestfwd with bash's `/dev/tcp`,
  as the image has neither socat nor nc. socat gives a connect timeout and a clean
  half-close (`socat -u - TCP:ADDR,connect-timeout=1`). Do not use nc: its variants
  differ (OpenBSD nc needs `-N` to close after stdin ends). Not a reason to add socat
  alone: it saves one line, slirp accepts at once, Claude Code kills the shim after 2 s,
  and a new package changes the image's cache key, so every host rebuilds the image. It
  may be worth it for paste (`session.native_paste`) or for the no-egress case.
  Upstream, a setting that names the copy command would replace the fake `wl-copy`:
  https://github.com/anthropics/claude-code/issues/102454 (filed 2026-10-15).

- [ ] **Copy without the egress proxy** (a task for someone else; cananian 2026-10-15
  will not work on it). Today copy needs the egress proxy's guestfwd to `clip.sock`.
  Without the proxy the shim would connect to `192.168.5.2:PORT`, the host's loopback,
  so the listener would need a TCP port there, which other local users and processes
  can reach too. The plan is to remove `--no-egress-proxy` once the macOS question is
  settled (the egress proxy is the more secure design), which would make this moot.
- **Paste: not built, on purpose** (cananian 2026-10-15). A paste channel lets the VM
  read the host's clipboard, and a step to approve each read would be more intrusive
  than the workaround: the terminal's own paste (Ctrl+Shift+V, Shift+middle click in
  GNOME Terminal) works; a plain middle click does not (README "Copy").

### From the design documents

- [ ] **Dependency walk, open questions:** a manifest `name` that is not the directory: an
  override table (`dependency_overrides`, today) or a fallback to the `name` field? Do
  `dev-requires` repos need another policy (`:ro` by default)?
- [ ] **Claude memory across sandboxes:** `~/.claude` (memory, projects) lives on the VM
  disk and goes with `rm`. Design: a host store (`global/` and `repos/<canonical>/`), seeded
  at create with `wmf-sbx cp`, extracted and merged at `rm`. Open: extract at `stop` too?
  How to merge two sandboxes' memories?
- [ ] **In-VM GitLab access with a token**, if wanted: needs a credential design (nono
  `--credential`, or the host proxy of track A); the old sbx secret-swap design does not
  apply. (note from the engineer: probably requires host-side MCP, see above.)

- [ ] **Broaden `WMF_CLAUDE_STE=0`** to the other places that ask for ASD-STE100: the
  `write-commit-msg` skill, the `mediawiki-dev` agent, and the `templates/CLAUDE.md` and
  `templates/mediawiki/CLAUDE.md` starters. Today it drops only the SessionStart bullet
  (enough for now, cananian 2026-10-11). Skills and agents are static text: they could say
  "unless the SessionStart context says not to", or the hook could add a line that overrides
  them.

### Quality-of-life issues
- [x] **Copy commits between the sandbox and the host** (cananian: `git safe-reset` then
  `git reset --hard NAME/BRANCH` was too many steps, and a branch that tracks the
  sandbox remote breaks `git review`). Done: `git sbx-pull`, on both sides; RAN
  2026-10-16 (`TESTPLAN-sbx-pull.md`). The findings, the plan and what was built:
  READ 2026-10-15 (git-review 2.4.0, `git_review/cmd.py`): git-review follows the
  current branch's upstream (remote *and* branch) only when `track` is on, and the
  `.gitreview` of mediawiki/core, Cite and Parsoid all set `track=1`. So on a branch
  that tracks `sbx-parsoid/foo`, `git review` checks for `remotes/sbx-parsoid/foo`,
  finds it, and pushes to `sbx-parsoid` `refs/for/foo`. `git config gitreview.track
  false` in the repo wins over `.gitreview` (git config is read first), but then the
  remote is `gerrit` (no `defaultremote` in these files) and the branch `master`, for
  every branch of that repo. Also: `git safe-reset NAME/BRANCH` runs `git checkout
  BRANCH`, which git's DWIM turns into a new branch that tracks `NAME/BRANCH` when only
  one remote has it: the setup that breaks `git review`. And the agent amends, so a pull
  from a sandbox is often not a fast-forward.
  `track=1` is there for release branches (cananian 2026-10-15): a local branch that
  tracks `origin/REL1_46` makes `git review` send to `REL1_46`. So turning `track` off
  is not an option, and a branch fetched from a sandbox must track the Gerrit branch
  the work is for, which the host cannot read from the sandbox. Proposed: the `origin/*`
  branch with the fewest commits to the sandbox branch (`git rev-list --count
  origin/X..NAME/BRANCH`), master on a tie, said aloud, with an option (`--onto
  REL1_46`) to name it.

  **Plan (cananian 2026-10-15, from three use cases; supersedes the first sketch).**
  Use cases, Parsoid, sandbox `sbx-parsoid`:
  1. `wmf-sbx create --reset-all .`, "work on gerrit 1354096"; the agent leaves branch
     `fix-thing`. Host: `git sbx pull` (or `... fix-thing`, `... sbx-parsoid/fix-thing`).
     The engineer edits, amends, then hands it back; the agent works on; host `git sbx
     pull` again, also from another branch; at the end `git review` sends to the change's
     branch.
  2. As 1, but the host starts with `git review -d 1354096` (branch
     `review/c_scott_ananian/1354096`, upstream `origin/master`), then a new sandbox
     (no `--reset-all`) or a hand-off to an existing one (after `/clear` there).
  3. As 2, but from a WIP commit on the host's `master` (after `git safe-reset`), not on
     Gerrit.

  Design: one command, `git sbx-pull` (`sbx/bin/git-sbx-pull`), on both sides, and the
  skill `/wmf-claude:sbx-pull` in the sandbox: one name to remember (cananian
  2026-10-15).
  - Upstreams stay on Gerrit (`git pull`, `git review`, release branches). Host branch
    config `branch.B.wmfsbx = NAME`, `branch.B.wmfsbxBranch` (the sandbox's name for
    it), `branch.B.wmfsbxSha` (the tip at the last pull), which no other git command reads.
  - `-f`/`--force` means "I know this is not upstream yet, do it anyway", on both sides
    and as in `git safe-reset` (a better long name is welcome; `-f` stays).
  - **Host `git sbx-pull [[NAME/]BRANCH] [--onto X] [-f]`.**
    - Which: `NAME/BRANCH`; `BRANCH`, if one sandbox of this repo has it (it fetches each
      running sandbox first); none: the current branch's record, else the only sandbox's
      only *new* branch: not named like a branch of `origin` (master, main, REL1_*), and
      not at a tip that the host gave it (a hand-off it has not changed). The skill keeps
      the agent on the hand-off branch, so this is for case 1's first pull, and for an
      agent that made a branch anyway.
    - Leaving the current branch (when it is not the target): a dirty tree stops it; so
      does a `git-review-check` failure, unless `-f`. With the check below, case 3's WIP
      commit on `master` passes once the agent has fetched it.
    - The target's local branch L: the one recorded for that sandbox branch, else one of
      the same name, else a new one. Switch to it (`git checkout L`), then move it to the
      sandbox's tip (`git reset --keep`) when nothing is lost: L's tip is in the new tip,
      or is L's `wmfsbxSha`, or is on Gerrit (case 2, first pull), or the agent fetched it
      (seen-ref, below: the agent amended the engineer's edits). Else stop, unless `-f`
      (the old tip stays in L's reflog, and is printed).
    - A new L's upstream, the first that applies (said aloud): the sandbox branch's
      upstream when it is `origin/X` (read with `wmf-sbx exec`, checked to exist on the
      host); when it is `local/Y`, Y's upstream on the host; the upstream of the host
      branch the work sits on (an agent branch made from `review/...` anyway, with no
      upstream: the review branch's `origin/master`); the guess (fewest commits);
      `--onto`.
  - **`git-review-check` learns "fetched by the agent".** Today it checks the sandbox side
    only when its argument is a sandbox remote. Added: in a repo with sandbox remotes, HEAD
    is safe when the agent has it: in `refs/remotes/NAME/*` (an agent branch) or
    `refs/remotes/NAME-seen/*` (a hand-off the agent took), for a sandbox of this repo
    (`wmf-sbx-ls-remotes`). So `git safe-reset` after a hand-off does not complain, and
    leaving the branch before the hand-off still does: the explicit hand-off is wanted.
  - **`git safe-reset NAME/BRANCH`** where NAME is a sandbox remote: prints that
    `git sbx-pull` is the command now, and execs `git sbx-pull NAME/BRANCH`.
  - **In the sandbox, `/wmf-claude:sbx-pull`** (or `!git sbx-pull`): runs `git sbx-pull`
    in guest mode (`$SANDBOX_NAME` set), then reads `git range-diff OLD...NEW` to learn
    what the engineer changed, and goes on from there. The skill also says: keep working
    on the hand-off branch, under its name (cananian 2026-10-15: a new branch at each
    hand-off leaves the engineer to remember which branch is the latest). Only a hand-off
    from `master`/`main` (case 3) gets a new branch, named by the agent with `--as`.
    - Which: the argument; else the host's current branch (`git ls-remote --symref local
      HEAD`; the agent cannot read the host's `.git/config`, and need not), when it is
      not named like an `origin` branch; else, with the host on such a branch and commits
      on it that are not on `origin` (case 3), it stops and asks for `--as NAME`. The
      skill then has the agent read those commits and messages (`git log -p
      origin/master..local/master`) and choose a short branch name for the work, and run
      `git sbx-pull --as NAME`. Else stop.
    - Gates: the guest tree is clean; the guest branch's commits are on the host (in
      `refs/remotes/$SANDBOX_NAME/*` there, which `git fetch local` brings with an extra
      refspec), or on Gerrit; else stop and report: the engineer has not pulled them
      (`-f` exists, but the skill does not use it without the engineer's say).
    - Then `git checkout -B B local/B`, upstream `local/B`, and a seen-ref
      `refs/sbx/seen/B` at the tip it took, which the host's fetch brings to
      `refs/remotes/NAME-seen/` for the host's gates.
  - **No push command** (cananian 2026-10-15): the hand-off is "stay on the branch, type
    `/sbx-pull` in the sandbox". A new sandbox (no `--reset-all`) starts on the host's
    branch already. Leaving the branch before that makes `git safe-reset` complain, which
    is wanted: an explicit hand-off.
  - `wmf-sbx rm NAME` unsets the `branch.*.wmfsbx*` entries that name it.
  - **Bash completion** (READ 2026-10-15, Debian's `/usr/share/bash-completion/
    completions/git`): `git sbx<TAB>` completes already (git lists the `git-*` commands
    on PATH). For the arguments, git's completion calls `_git_sbx_pull` and, when that is
    not defined, has bash-completion lazy-load a file named `git-sbx-pull`, which
    bash-completion 2 also looks for in `~/.local/share/bash-completion/completions/`. So:
    ship `sbx/completion/git-sbx-pull` (defines `_git_sbx_pull`: sandbox names from
    `wmf-sbx-ls-remotes`, then `NAME/` branches from `refs/remotes/NAME/*`) and link it
    there at install; no `.bashrc` change. README "Getting started" says how. To check:
    macOS (Homebrew `bash-completion@2`; zsh is the default shell there, and zsh's git
    completion is another mechanism).
  - Open: whether Claude Code takes `/sbx-pull` for the plugin's `/wmf-claude:sbx-pull`.
    Moot: the skill is a user skill (below).
  - Built (2026-10-15): the host side, `sbx/src/wmf_sbx/sbx_pull.py` (tests:
    `test_wmf_sbx_sbx_pull.py`, real repos), and `wmf-sbx rm`'s clean-up
    (`remotes.forget_pulls`: the records and `refs/remotes/NAME-seen/*`). Differences
    from the plan: `--onto` comes first, not last, and on a branch that exists it
    re-points the upstream; a bare pull takes the only new branch of *all* the
    sandboxes, not only of a sole sandbox; a branch at its `wmfsbxSha` may be left
    (it is what was pulled). The Gerrit check is in Python (`gerrit_has`, the same
    query as `git-review-check`). A host branch's upstream on a sandbox remote is
    never passed on. Then `git-review-check` (HEAD in `refs/remotes/NAME/*` or
    `NAME-seen/*`) and `git safe-reset NAME/BRANCH` (or `NAME BRANCH`; a bare `NAME`
    still resets to its master). Then guest mode, `sbx/helpers/git-sbx-pull`: one
    stdlib-only file, which the image copies to `/usr/local/bin` (a new image; a
    sandbox made before it has neither the command nor the skill). The guest reads
    the host's copy of its branches (`refs/remotes/$SANDBOX_NAME/*` there) into
    `refs/sbx/host-has/*`. The skill is a *user* skill, written at create to
    `~/.claude/skills/sbx-pull/SKILL.md` (source `sbx/templates/skills/sbx-pull/`),
    not the plugin's: it is wmf-sbx's alone, it stays out of the shared `skills/`,
    and a user skill's command is `/sbx-pull` (to RAN in a session). The agent's
    CLAUDE.md names it. Last, `sbx/completion/git-sbx-pull` (bash): git's completion
    calls `_git_sbx_pull`, and when it is not defined has bash-completion 2 load
    `git-sbx-pull` from the user's completion directory
    (`$BASH_COMPLETION_USER_DIR`, else `$XDG_DATA_HOME/bash-completion`), so the
    README's "Getting started" links it there and nothing edits `~/.bashrc`. One
    file does both modes, by `$SANDBOX_NAME`; the guest image does not install it
    (the agent has no TTY). The README has a "Commit flow" section, which the host
    command's `--help` names. RAN 2026-10-15 on the engineer's host: the completion
    works, but lists only branches that were fetched before; so it now fetches the
    sandbox remotes (`local` in a sandbox) first, in parallel, in a subshell (no job
    lines; `wait` leaves the user's jobs alone), at most once a minute per remote
    (a stamp in `.git/wmf-sbx-completion/`); a failed fetch is silent. No time limit:
    macOS has no `timeout`, and Ctrl-C ends a slow TAB.
  - RAN 2026-10-16, `TESTPLAN-sbx-pull.md` with MultiTitle, every step passed: the new
    image has `git-sbx-pull` and `/sbx-pull`; Gerrit's user agent in a new sandbox
    (`git fetch origin`, `git safe-reset origin`: no 403); the completion's fetch
    (a TAB found a branch that no fetch had seen); a bare host pull of the only new
    branch, with the sandbox branch's upstream; the next pull with no argument;
    `review-check` on agent and seen refs; `safe-reset NAME/BRANCH`; the hand-off on
    a named branch (the agent read the change, and saw that a host amend with `-m`
    dropped its Change-Id); both stops and `-f`; the leave gate; `--as` from master,
    and its upstream through host master; the REL1_46 guess and `--onto`; `rm`. Seen
    then: the completion offers the sandbox's `master` (bare and qualified), which
    a bare pull never takes; the `wmfsbx.*` keys left after `rm` are the snapshot's gc
    borrower, not the pull records (the plan said "nothing": its error). Fixed
    after: the host completion leaves out origin-named branches, in both forms
    (the sandbox completion keeps the host's master, a hand-off with `--as`);
    `git sbx-pull BRANCH` in two sandboxes takes the one that the host's branch
    BRANCH records, and stops only when no record decides. Open: zsh (its own
    `_git` and git's `git-completion.zsh` find subcommands in different ways), and
    macOS bash 3.2 (bash-completion 2 needs bash 4).
  - Built after, on the host only (cananian: "build all four list prompts and y/n on
    the two safety stops"): on a terminal, `git sbx-pull` asks instead of stopping,
    with a numbered list, when BRANCH is in more than one sandbox and no record
    decides; when no sandbox branch is new (every branch that is not origin-named);
    when more than one is new; and when more than one host branch records the
    sandbox branch. y/N before it leaves or moves unsaved commits (the two `-f`
    stops). A dirty tree and the rename conflict stay stops: they need another
    command, not a choice. The guest helper never asks. The questions are
    `wmf_sbx/prompt.py` (`Prompt.pick`, `Prompt.confirm`), now also used by the
    path shortcut in `state.py` (a directory with more than one sandbox), `create`'s
    clone question and `rm`'s y/N. Prompt checks for a terminal itself (stdin and
    stderr both TTYs; cananian: so that no caller forgets and a script hangs): with
    none, it asks nothing, `pick()` is None and `confirm()` is False, and the caller
    keeps its old stop. A caller that passes its own `ask` gives the answers itself.
    `rm` without `-f` and with no terminal now refuses at once (it used to call
    `input()`, which read EOF). Not yet RAN on a terminal.
  - Built after, with the same Prompt: `wmf-sbx create NAME` (and `wmf-sbx resolve`)
    lets you pick when a bare name matches more than one project (`resolve._pick_exact`;
    dependencies are full paths, so they never ask); `rm`'s unfetched-commits stop
    offers "Fetch them now?" (`git fetch REMOTE` in each repo, then the check again).
    Not yet RAN on a terminal. Found then, not changed: `rm` removes the remote, and
    with it `refs/remotes/NAME/*`, so a commit that was only fetched (as the stop's
    own `git fetch REMOTE SHA` advice does, too) is left unreferenced, for gc to
    prune. A local branch keeps it (`git sbx-pull`). Open: should `rm` keep such
    tips under a ref of their own?
  - Built after: Prompt defaults (cananian: useful for prompts in general). Enter takes
    `pick(..., default=I)` or `confirm(..., default=True)` ("[Y/n]"); Ctrl-D and no
    terminal never do. `pick` also takes the start of one label ("w" for "wait ...").
    The golden image's "another build is running" question is now a `pick` (Enter
    waits, as before; no terminal aborts). Defaults chosen: `rm`'s "Fetch them now?"
    is yes (a fetch only adds to the host); `git sbx-pull`'s "update which?" is the
    branch with the sandbox branch's own name, when it is one of them. A destructive
    question takes the safe answer on Enter: remove a sandbox, leave or move unsaved
    work are [y/N] (`default=False`, said at each call); the busy image waits. No
    default: which sandbox, project or new branch (nothing makes one the obvious
    choice). `create`'s "Clone them there now?" stays no: it writes to the engineer's
    disk.
  - READ 2026-10-15, why upstreams to a sandbox seemed to vanish: `wmf-sbx rm` runs
    `git remote remove NAME` (remotes.py), which deletes `refs/remotes/NAME/*` and
    unsets `branch.*.remote`/`merge` for every branch that tracked NAME. The local
    branches stay (RAN in a scratch repo). With the upstream on Gerrit this is moot.

### Upstream work and repo hygiene

- [ ] **Adopt what upstream added (`sbx/FROM-UPSTREAM.md`)**: more to-dos, from the
  review of upstream 70712f6..21bc986. In order: a session on nono 0.80, rename our
  "contained" mode (upstream's contained mode is a different thing), the long project
  directory names in `resume.py`, `WMF_CLAUDE_PHAB_USER`, the terminal variables, the
  uncommitted-work report, memory and session carry, `ct direction reply`; then, after
  asking the engineer, headless Chromium and request-body checks.
- [ ] **Send the new upstreamable commits upstream** (`sbx/upstream/README.md`, "To send"):
  pipefail on the Claude Code install (c34e8b1), the test environment fix (b525f78, into !132),
  the `bin/claude-vm` shellcheck fix (2cedd08), and `--local-web=LO-HI` (part of 4dbf3e2).
- [ ] **Follow MRs !131, !132 and !134**: `lima-port` is rebased on upstream `main` 21bc986
  (2026-10-16; !127, !130 and !133 merged) and carries their commits. The rebase
  changed !131 and !132 to fit upstream's new code: !131 now keeps upstream's
  `WMF_CLAUDE_PHAB_USER` and contained-VM branches, and !132 moves upstream's new
  inline browser text to `hooks/context/nono/{chrome,chrome-vm}.txt`. Update the MR
  branches to match, or withdraw !131 (upstream's `WMF_CLAUDE_PHAB_USER` does most of it). Check their state with:
  ```bash
  P='https://gitlab.wikimedia.org/api/v4/projects/repos%2Fproduct-safety-and-integrity%2Fwmf-claude'
  for m in 131 132 134; do curl -s "$P/merge_requests/$m" \
    | jq -r '"!\(.iid) \(.state) \(.merge_commit_sha // .squash_commit_sha // "-") \(.title)"'; done
  ```
  When one merges, rebase onto upstream `main` (`git rebase wmf/main`; on a conflict
  in a file the MR changed, take the branch commit's own version, then check that the
  tip's tree did not change) and take upstream's version of its files (!132:
  `bin/session-start.sh`, `hooks/context/nono/`, the seam tests; !131:
  `bin/wmf-claude-setup`). Keep `hooks/context/lima-sbx/` and `hooks/context/lima-sbx-sudo/`. A reviewer's change goes to
  the MR branch.
- [ ] **Shared-file edits must be upstreamable**. Outside `sbx/` and
  `lima-port/`, the branch now changes `bin/claude` (`--local-web=LO-HI` ranges),
  `hooks/context/lima-sbx/*`, `package.json`, `tests/test-templates.sh`, `SECURITY.md`,
  `.gitignore` (checked: `git diff --stat wmf/main..HEAD -- ':!sbx' ':!lima-port'`). Split them into
  self-contained commits justified without sbx, or patch them at build time.
- [ ] **File the tasks in `sbx/upstream/`**: (1) core REST
  header-case lookup; (2) `wdio-mediawiki` empty `MW_SCRIPT_PATH`; (3) Popups
  `mw-node-qunit` false green on Node ≥21; (4) wmf-claude "Linux glob caveat" — re-measure
  first. cananian files; add T-numbers to the
  README; delete a pair when upstream takes the fix.
- [ ] **Upstream: `tests/test-profile.sh` fails on Linux**: nono refuses the
  profile ("Landlock deny-overlap is not enforceable on Linux") because the
  `~/.cargo/credentials(.toml)` deny sits under `group:rust_runtime`'s `~/.cargo` allow.
  A #WMF-Claude task, if not filed yet.
- [ ] **Seam 2 (optional, upstream): make `bin/wmf-claude-setup` fully non-interactive**.
  Upstream 21bc986 has `WMF_CLAUDE_PHAB_USER`, and keeps the recalled username with no
  prompt in a VM or with no terminal. Left: `setup.sh`'s "Press Enter" gate, and a
  `WMF_CLAUDE_NONINTERACTIVE=1` that turns any prompt into an error. A nicety, not a
  blocker.

## Decisions and findings

### Project rules

- Do not fork `skills/` or `agents/`; fix the environment or propose an upstream change. Do not
  edit `profiles/wmf-engineer.json` from this project. Do not weaken
  `bin/wmf-claude-build`'s supply-chain checks (`npm ci --ignore-scripts`,
  `--require-hashes`, chrome-devtools-mcp one-package rule).
- Never commit `responses*.txt` (host transcripts, gitignored); distil what matters into NOTES.
- Variable names: the reader decides the prefix. `WMF_SBX_*` is read by wmf-sbx itself: the
  engineer's configuration on the host (`WMF_SBX_CA_CERTS`, `WMF_SBX_GIT_HASH_OVERRIDE`),
  and the QEMU wrapper's (`WMF_SBX_EGRESS_CONFIG`). `WMF_CLAUDE_*` is read by wmf-claude
  (bin/claude, the hooks), also when wmf-sbx sets it for a session in the VM
  (`WMF_CLAUDE_SANDBOX_BACKEND`, `WMF_CLAUDE_NO_PAUSE`, `WMF_CLAUDE_SBX_EGRESS`). nono gives
  the session only the profile's `allow_vars`, which has `WMF_CLAUDE_*` and not
  `WMF_SBX_*`; a `WMF_SBX_*` variable for the session is dropped with no error (RAN, egdef,
  2026-10-14; a test now checks `launcher_argv`'s variables against `allow_vars`).
- Python code: `sbx/src/wmf_sbx/`, on `sys.path` only (no pip packaging); thin wrappers in
  `sbx/bin/`; tests in `sbx/tests/`; `python3 -m wmf_sbx <cmd>` via `__main__.COMMANDS`.
  PyYAML is the only runtime dependency. Verb lists in `bin/wmf-sbx`, `COMMANDS` and
  `bin/wmf-sbx-<verb>` must agree (a test checks).
- `git-safe-reset` and `git-review-check` are intended to be
  used/useful on the host as well as the guest; don't hardcode any
  assumption that is only true on the sandbox or behavior that only
  makes sense inside the sandbox unless guarded by a check that
  $SANDBOX_NAME exists.
- Tests never see the machine's own state: every test module calls `isolation.isolate()`
  from its setUpModule (a test checks), which gives it an empty HOME and XDG directories,
  no credential or `WMF_SBX_*` variables, no nested marker, and failing `claude` and
  `security` stubs on PATH. A failing assertion must not print a real credential (one in
  egsn printed the session's phantom), and a test must not start the real Claude Code
  (create's `claude mcp get phabricator` did, 70 times a run, two thirds of its time).
  Code that reads a path the tests patch reads the module attribute at call time, not a
  copy made at import (`nested.inside()`).
- `setup.py` must stay import-free: it runs in the VM as `python3 - --lima PLAN` from stdin, so
  a change to it needs no new image.

### The egress proxy and `--sudo` (track A)

The design is in `README.md` (Architecture: the egress proxy, `--sudo` sessions) and
`SECURITY.md` (§3, §6b). The labels below, and in code comments, are the plan's steps:

- A2.0, measurements with throwaway scripts (`lima-port/checks/`): (1) `nono proxy` on the
  host side, nested and chained; (2) from the guest: an allowed GET passes, a POST, a host
  off the list, the host and a direct address fail, also after guest root flushes nft;
  (3) package installs with no guest DNS; (4) Claude Code through `HTTPS_PROXY`;
  (5) throughput; (6) the wrapper as QEMU's parent, through every kind of stop; (7) items
  2, 6 and 8 unnested on a real host (`checks/sudo-egress-m7.sh`); (8) a new proxy under a
  running VM; (9) a contained session's nono behind the host proxy.
- A2.1 the QEMU wrapper, A2.2 the proxy's config (`egress.py`), A2.3 the template, start,
  the fake-address resolver and `wmf-sbx allow` (`create --egress-proxy`).
- A3 `--sudo`: (1) create and state, (2) the guest's sudo grant, (3) the session, (4) what
  Claude is told, (5) the docs, (6) `--sudo --nested`.

A2.0 results (in the nested `dev` VM, nono 0.78.0, 2026-10-13):

- `nono proxy --profile profiles/wmf-engineer.json` (password by `NONO_PROXY_PASS`) loads the
  profile and intercepts TLS for its 48 endpoint routes: through it, GET
  `en.wikipedia.org/w/api.php` passes the endpoint policy and POST gets 403 ("no rule
  matched"); a host off the list gets 403 at CONNECT. So the host proxy can enforce the same
  policy as contained mode (RAN).
- Its TLS-intercept CA goes under `$XDG_STATE_HOME/nono/sessions/`; under a session's nono
  `~/.local/state/nono` is not writable and it skips interception with only a warning, so
  set `XDG_STATE_HOME` (or pass `--proxy-ca-cert`, the plan's fixed CA). It also writes
  `nono/update-check.json` there.
- `--upstream-proxy HOST:PORT` sends no credentials: chained to the session's proxy it got
  `407` (logged as "enterprise proxy rejected CONNECT"), and the client a 502. Neither
  `user:pass@HOST:PORT` nor a URL helps, and the binary has no other upstream-auth setting.
  A direct connection from inside the session is EPERM (Landlock). So in a nested sandbox,
  and on a host behind an authenticated proxy, the host proxy needs a small shim that adds
  `Proxy-Authorization` on the way to the upstream. (Inner contained sessions pass
  `--upstream-proxy` the same way, `session.upstream_proxy`, without the password: check
  whether their egress works at all when nested.)
- No live allowlist change: no reload flag, signal or control socket in 0.78. `wmf-sbx
  allow` replaces the proxy (README.md, Architecture).
- With `lima-port/checks/upstream-auth-shim.py` between them, the chain guest → `nono proxy`
  (fixed EC P-256 CA, `--proxy-ca-cert/--proxy-ca-key`) → shim → session proxy works: two
  TLS interceptions in a row, each trusting the next CA (RAN).
- Inner VM `inner` booted through a throwaway `QEMU_SYSTEM_X86_64` wrapper that appends
  `restrict=on,guestfwd=tcp:192.168.5.100:3128-cmd:sh -c '…socat…'` to the one `-netdev
  user` (boot 14–16 s). From the guest, with `https_proxy=http://nono:PASS@
  192.168.5.100:3128`: wiki GET 200, POST 403, off-list host 403, no password 407; direct
  `140.82.112.3:443`, `192.168.5.2:22` and the session's proxy port fail; no DNS (getent,
  `@192.168.5.3`), UDP unreachable, no default route. All after `nft flush ruleset` as guest
  root (RAN).
  - The image's nft host block drops 192.168.5.100:3128 too: a `sudo` template needs a rule
    for it.
  - libslirp splits the `cmd:` string into argv itself (no shell): `cmd:exec socat …`
    aborts; wrap in `sh -c '…'`. A wrapper in bash must keep `$(cat port)` out of its own
    expansion (`\$(…)`), or the port is fixed at boot.
  - Lima writes the host's proxy variables (here the session proxy, with its password) to
    the guest's `/etc/environment` unless `propagateProxyEnv: false`; the plan sets it.
- With no guest DNS, every client that honours the proxy variables works, as they send the
  name in CONNECT: `apt-get update/install` (with an apt proxy conf), `npm ci`, `composer
  install`, `pip install`, `git clone https://…` (RAN). Two need settings: git aborts the
  CONNECT until `http.proxyAuthMethod=basic` (put it in `/etc/gitconfig`); Node's `fetch`
  ignores the variables unless `NODE_USE_ENV_PROXY=1`. `mw-install-browser`: not run.
  - RAN 2026-10-15, git to Gerrit in a contained session failed (a new sbx-parsoid):
    `cb6fc0c` put the egress proxy in `/etc/gitconfig` (`http.proxy`), which wins over
    the variables, and since `cfdb74d` a contained session's variables name nono's
    proxy, with nono refusing a direct connection to 192.168.5.100:3128 ("Failed to
    connect"). Through nono's proxy, git aborted the CONNECT ("Proxy CONNECT aborted":
    its URL has credentials; `f066dba` had dropped `proxyAuthMethod=basic` with the
    egress proxy's password). And Gerrit's edge now refuses git's own user agent (403,
    "Please set a user-agent"), also with no nono. Reproduced with `nono run
    --sandbox-policy landlock --upstream-proxy 192.168.5.100:3128` and fixed: the
    provisioning removes `http.proxy` (every git in the guest has the variables), and
    the session's `env` gives git `http.proxyAuthMethod=basic` and, with GERRITUA,
    `http.https://gerrit.wikimedia.org/.userAgent` (GIT_CONFIG_COUNT/KEY/VALUE, after
    any from settings.yaml; `wmf-sbx exec` gets them too). Both are written at create:
    a sandbox made before needs a re-create. Not yet RAN in a new sandbox. The same
    403 hit create: each clone's `git fetch origin` in the VM (sandbox-repos.sh; it
    then warns and falls back to `local`), and `--clone-missing`'s `git clone` on the
    host. Both now get GERRITUA for Gerrit's URL (`deps.gerrit_git_config`): in the
    VM as `export` lines at the top of the clone script, on stdin, and on the host in
    git's environment, never in an argv (`ps` shows an argv). The image build clones
    only php-ast, from GitHub. And `git safe-reset origin` said "Could not reach Gerrit"
    (RAN): `git-review-check`'s `curl -sf` got the same 403, with stderr discarded.
    It now sends GERRITUA to gerrit.wikimedia.org only, as a header on curl's stdin
    (`-H @-`), and on a status other than 200 says the status (and, for a 403 with no
    GERRITUA, names it). `git sbx-pull`'s Gerrit query (host and guest) sends it to
    gerrit.wikimedia.org only, not to another `.gitreview` host or a redirect. Both
    helpers are in the image: a new image and a re-create.
- Claude Code through `HTTPS_PROXY` reaches `api.anthropic.com` (a turn fails only with
  "Invalid API key": no credentials given); its telemetry host is denied, harmlessly (RAN).
- **Opening a host under a running VM works** (item 8, RAN): the guestfwd reads
  `port` at each guest connection (`sh -c 'exec socat - TCP:127.0.0.1:$(cat …/port)'`).
  A second `nono proxy` with `download.cypress.io` added, `port` rewritten (write + `mv`):
  the next guest request to it went 000 → 200, with no restart; a 4 MB download started
  through the old proxy finished (200, full size) after the swap. Nested, a host still needs
  the outer session's allowlist too: proxy 3 allowed `example.com` and the session proxy
  then refused it (403, "enterprise proxy rejected CONNECT").
- **The wrapper as the proxy's parent** (item 6, RAN with `bin/wmf-sbx-qemu` and
  `egress.py`, inner VM, boot 13 s): Lima runs wrapper → `nono proxy` + QEMU; its probe
  calls pass through. After `limactl stop`, `limactl stop --force` and `kill -9` of QEMU,
  the wrapper and the proxy are gone and `wrapper.pid` removed; after the kill Lima shows
  `Stopped`. A killed wrapper takes both children with it (`PR_SET_PDEATHSIG`, Linux only;
  macOS has no equivalent: item 7).
- **Throughput** (item 5, RAN): `npm ci --ignore-scripts` in Cite (708 packages, 37 MB,
  empty cache): 7.6 s through the egress proxy (16 proxy connections), 9.4 s through Lima's
  usual path to the same session proxy. No cost from the extra hop or `socat`.
  - npm with no usable proxy variable (`HTTPS_PROXY=` set empty, so it went direct) hung
    for over 10 minutes with no DNS, rather than failing: the guest's variables must be
    complete.
- **The guest's clock** (found in item 6): nono signs each leaf certificate with
  `notBefore` = now, and the guest's clock ran a fraction of a second behind: the first
  request after boot failed "certificate is not yet valid", the next passed. With
  `restrict=on` the guest has no NTP (`System clock synchronized: no`). `ptp_kvm` does
  not load in this nested guest ("Operation not supported").
  - **Lima sets the guest's clock itself**, not over the network, so `restrict=on` does
    not stop it (Lima 2.2.1, `pkg/hostagent/timesync.go`, `pkg/guestagent/api/server/
    server.go`). 5 s after the guest agent is up, then every 10 s, the host agent sends
    its time over the guest agent's virtio-serial channel; when the guest is more than
    100 ms off, the guest agent steps the clock (`settimeofday`). In the inner VM: "guest
    clock adjusted (was -908ms off)" 2 s after READY, then drift 0 to 17 ms.
  - What is left: (a) from READY to the first sync (about 2 s here), the guest can be a
    second behind; (b) after it, up to 100 ms behind, so the first request to a new host
    can still fail while its new leaf is younger than the skew. The failure in item 6 was
    (a).
  - In the m7 script's runs (golden image, nested), the first sync came about 5 s after
    "guest agent is alive" ("was -1530ms off", then "-1529ms"), and both times before
    `limactl start` returned: right after it, guest - host was 0.042 s and the first
    requests to three hosts all got 200.
  - On the engineer's Linux host (unnested, Lima 2.3.0-beta.1, QEMU 8.2.2, Debian's cloud
    image): the guest was -534 and -229 ms off at the first sync. At the restarts the sync
    landed 1 s before `limactl start` returned; at the first boot (the provisioning) READY
    came in the same second as "guest agent is alive", so the sync came about 5 s after
    `limactl start` returned. So (a) is a race, both ways: `start` waits for the first
    "Time sync:" line (`egress.TIME_SYNC_DONE`). `ptp_kvm` loads there (`/dev/ptp0`), so chrony with a PHC
    refclock is possible unnested on Linux (not nested, not on macOS).
  - No guest agent, no sync: Lima runs none in `plain: true` mode
    (`hostagent.go`: "Guest agent will not be running"). wmf-sbx's template is not plain;
    `lima/wmf-claude.yaml` is.
- **Item 9, contained mode behind the egress proxy** (a session's nono inside the guest,
  `--upstream-proxy 192.168.5.100:3128`):
  - The inner nono sends no credentials upstream, so it gets nothing from a proxy with a
    password. The egress proxy runs `--no-auth` now (egress.py): only the guest reaches the
    guestfwd, so the password protected nothing. `nono proxy` exits 2 on `--no-auth` with
    `NONO_PROXY_PASS` set.
  - **The inner nono resolves each name itself**, and behind `restrict=on` the guest has no
    DNS (Lima's resolver is the host agent, at 192.168.5.2 and a random port; `getent
    hosts` hung). Every request timed out (curl exit 28) and nothing got to the egress
    proxy. With a line in /etc/hosts it went through, and the address in that line is not
    used: 10.9.9.9 did as well as 1.1.1.1, because nono sends the name in the upstream
    CONNECT. So the guest needs a lookup that succeeds, not a real resolver (A2.3: the
    fake-address resolver, `fakedns.py`). Plain tools given the proxy variables
    (curl, apt, git, npm) do not look up names.
  - Two interceptions in a row work: the inner nono trusts the egress CA from the guest's
    store, and the outer proxy still applies the endpoint rules (its log has the `l7`
    decision for the wiki GET). A wiki POST gets 403.
  - `--allow-net` is no use: the inner nono then removes the proxy variables from the
    child's environment and does not proxy, so every connection goes direct and fails.
  - **Inner `--allow-domain '*'`**: the inner nono lets every host through to the outer
    proxy and still applies the profile's endpoint rules (POST 403). The outer proxy
    is the only allowlist: example.com went to the outer proxy and was denied there,
    and download.cypress.io, which only the outer list has, answered 200. So
    `wmf-sbx allow` covers a contained session too.
  - Found here: `PR_SET_PDEATHSIG` is tied to the thread that forked the child. The wrapper
    started each replacement proxy from a short-lived thread, so the new proxy died
    when that thread ended. Fixed in the wrapper (one long-lived thread).
- **Item 7, nested, with `lima-port/checks/sudo-egress-m7.sh all`** (a plain Lima VM
  behind the wrapper; `M7_IMAGE` = the golden image, as the sandbox cannot download
  Debian's): every check passed. The guest reaches a wiki (200), not an unlisted host, not
  an address direct, no DNS; the swap moves the guestfwd to a new proxy and both stay up;
  `limactl stop`, `stop -f`, killing QEMU, and killing the wrapper each leave no wrapper,
  proxy or QEMU, and the VM starts again. In the swap, the session's own proxy refuses
  example.org upstream of the egress proxy, so the script checks the egress proxy's
  reason ("not in the allowlist" before, not after), not the HTTP status.
- **Item 7 on the engineer's Linux host** (unnested; nono 0.79.0, Lima 2.3.0-beta.1, QEMU
  8.2.2, Ubuntu 24.04, kernel 7.0): every check passed, the four stops included. One
  thing open: after the swap, the new proxy let example.org through its allowlist, but the
  request failed: "DNS resolution returned no addresses" (the host's own lookup, by nono
  proxy), so the guest got 000. The script now asks for a 200 after the swap when no
  proxy is upstream. The cause is the host's resolver: `resolvectl query example.org`
  there says "Name 'example.org' not found", and a later request got the same reason (502).
  nono proxy looks up the name itself and does the right thing. The swap host is now
  www.kernel.org (`M7_SWAP_HOST`): rerun on the same VM, it was denied by the allowlist
  before, the port moved, both proxies stayed up, and after it the guest got 200
  (decision="allow"). Item 7 is done on Linux.
- **A2.3, `create --egress-proxy`, nested** (cached golden image, `--no-deps`, 2026-10-13):
  - First create: the provisioning stopped at `Cannot find device ""`: behind `restrict=on`
    the guest's only route is 192.168.5.0/24 on eth0, no default. Lima's
    `30-install-packages.sh` also failed (rsync, "Temporary failure resolving"): it runs
    before the provisioning writes the apt proxy. Both fixed (interface toward 192.168.5.2;
    rsync in the image). A `limactl validate` test failed once in a full suite run while the
    VM booted, and passed alone and on the rerun.
  - Second create: every invariant passed, the CONNECT 403 one included (`%{http_connect}`
    is 403). `wmf-claude-setup` then failed: `nono pull` got 403, "registry.nono.sh:443 is
    not in the allowlist". `wmf-sbx allow egtest registry.nono.sh`, setup again: done, and
    no other host denied. Now built in (`egress.SETUP_DOMAINS`).
  - Guest: resolv.conf `nameserver 192.168.5.100` + `use-vc`, resolved inactive;
    `getent ahostsv4 en.wikipedia.org` gives 198.18.0.7, the same after a restart (the
    `names` file keeps it). As root: `apt-get update` and an install through the proxy. As
    the agent: `npm install`, `composer require`, `git clone` from GitHub. A nono run as
    the agent with `--allow-domain '*' --upstream-proxy 192.168.5.100:3128`: wiki GET 200,
    POST 403, example.com 502 (the outer proxy's 403), registry.npmjs.org 200.
  - `wmf-sbx allow` under a running contained process: pypi.org 000 before, `allow` 8 s in,
    200 after (decision="allow"), no restart. www.kernel.org was allowed by the egress
    proxy too, but the outer session's proxy (nested) refused it, as in item 8.
  - `allow --rm`, `--list`, a bad host (exit 1), and `status` (proxy port, CA expiry, the
    list) as designed. `stop` left no wrapper, proxy or QEMU and removed `wrapper.pid`;
    `start` passed every check, and the first requests after it got 200.
  - systemd-timesyncd still looks up `2.debian.pool.ntp.org` (it gets a fake address, and
    its NTP is dropped); harmless, Lima sets the clock.
- **A Claude session in an egress sandbox** (RAN, 2026-10-13): the `egnest` sandbox
  (`--egress-proxy --nested`) served as that test. A whole working session ran in it; its
  proxy.log has 88 CONNECTs to api.anthropic.com allowed, so the credential proxy on
  `cred.sock` works. The denials in that log are not ours to chase:
  `http-intake.logs.us5.datadoghq.com` (Claude Code's telemetry, 64) and
  `raw.githubusercontent.com` (1, at Claude Code's startup) are not in
  `profiles/wmf-engineer.json` either, so nono on the host denies them too;
  `wmf-sbx-check.invalid` (2) is the `status`/create invariant that a host off the list
  gets 403.
- **The default is flipped** (the engineer, 2026-10-13): `create` makes egress-proxy
  sandboxes by default, and `--no-egress-proxy` gives the earlier kind (the guest's own
  route, the per-port credential proxy). Where the proxy cannot run, the default falls
  back to the earlier kind with a warning that the guest, not the host, enforces the
  network policy: on `vz`, and on a host without `qemu-system-ARCH`, `nono` or `openssl`
  on its PATH (`egress.missing_tools`). An explicit `--egress-proxy` makes both an error
  (`create.egress_choice`). The session gets `WMF_CLAUDE_SBX_EGRESS=NAME`, and
  `bin/session-start.sh` then tells Claude to ask the engineer for `wmf-sbx allow NAME
  HOST` when a host is denied.
  RAN on the engineer's host (2026-10-14, after c31fa1b): the dry runs of the default,
  `--vm-type vz` (the warning), `--vm-type vz --egress-proxy` (error, exit 1) and
  `--no-egress-proxy` (warm snapshots again) as designed. A real default `create --no-deps
  Cite` (a new golden image, then cold): every invariant passed, and setup
  (`wmf-claude-setup`, the plugin, the MCP registration) went through the proxy with no
  denial but the two `wmf-sbx-check.invalid` checks. A session in it: no session-start
  bullet (`WMF_SBX_EGRESS` was dropped by nono; now `WMF_CLAUDE_SBX_EGRESS`, 1dfd453), yet
  the agent refused to work around the 403 by itself. example.com: denied by the egress
  proxy ("not in the allowlist"); after `allow`, "DNS resolution returned no addresses",
  as for example.org in item 7: the engineer's resolver has no example.com. Test `allow`
  with www.kernel.org. On the host, `curl -x` to the proxies: `allow egdef pypi.org`, then
  the old proxy's port still gave 403 (it keeps the old list, for open connections) and
  the new port 200.
  RAN (2026-10-14, a new golden image 161df08b for 1dfd453): a default `create Cite` with
  its 11 dependencies (core, VisualEditor, Vector...) through the proxy: composer for all 12
  repos, `npm ci`, the wiki install, all as without the proxy, and `Sandbox egcite is
  ready`. Its log denies only `wmf-sbx-check.invalid` and, 3 times during `npm ci`,
  `scarf.sh`: the `@scarf/scarf` postinstall's download analytics (npm does not run that
  script, by its allow-scripts warning; nothing failed). Leave it denied.
  A session in it: `WMF_CLAUDE_SBX_EGRESS` is `egcite`, and the bullet is there. Asked to
  fetch www.kernel.org, Claude got the 502, did not work around it, and asked for `wmf-sbx
  allow egcite www.kernel.org`; after the engineer ran it, the same fetch gave the page
  ("The Linux Kernel Archives") with no restart. The default path is done on Linux.
- **`--nested` behind the egress proxy** (RAN on the engineer's host, 2026-10-13: `create
  --egress-proxy --nested` passes every invariant and finishes, after 388cbb3 and d6b8628;
  an inner `create .` of wmf-claude in it ran too, after 24c67e9 and 3ab7488 (the clone
  mounts the object dirs that the outer clone borrows)) needs
  no proxy of its own: Lima points the inner VMs at the session's nono proxy, as now, and that
  proxy's upstream becomes the egress proxy; the inner wrapper's own proxy chains the same
  way, through AuthShim (item 7 ran exactly that chain). So a nested sandbox's egress
  list adds `nested.DOMAINS` (cloud.debian.org, nodejs.org, downloads.claude.ai, ...).
  On macOS `--nested` means vz, so it falls back.
- **`--sudo` (A3 steps 1 to 5, written and RAN 2026-10-14).** `create --sudo` (or
  `settings.yaml` `create: sudo: true`) needs the egress proxy. The provisioning writes `/etc/sudoers.d/90-wmf-sbx-agent` (visudo-checked;
  `env_keep` for the proxy variables) and `/etc/wmf-sbx/sudo`, and a contained sandbox
  removes both at each boot. A session runs `bin/launch-claude.sh` with no nono, with the
  egress proxy's variables (`sudo -u agent` drops `/etc/environment`'s) and
  `NODE_EXTRA_CA_CERTS` set to the system bundle, which holds the egress CA. Without nono in
  between, the session-start bullet gives the proxy's own `403` at CONNECT, not the 502 of
  a contained session (RAN above, `nono proxy --profile`: a host off the list gets 403 at CONNECT).
  First host create (egsudo, 2026-10-14): every invariant passed but "the agent can sudo,
  from its sudoers file". The invariants run as the engineer, and `/etc/sudoers.d` is
  0750 root:root, so its `test -e` failed with the file there. The test now runs under
  `sudo -n`, as a check of its own.
  RAN on the host after that (egsudo, 2026-10-14): `status` all ok; `resume --allow-post`
  refused (exit 2); in the session, `sudo apt-get install`, `mw-install-browser`, WebFetch
  (403 at CONNECT for www.kernel.org, then 200 after `wmf-sbx allow`, with no restart); the
  status line; `sudo nft flush ruleset`, then curl `--noproxy '*'` to an IP failed at once
  and the proxy still refused a host off its list; a wiki POST got 403 (`endpoint rules
  denied POST`); `sudo mount -o remount,rw` of a `.git` share, then a write got
  `Read-only file system` from the host side. Some 30 denied CONNECTs to
  `metadata.ftp-master.debian.org` (backoff 1, 2, 4 s) were `apt-listchanges.timer`
  ("Try hourly to initialize apt-listchanges database": `apt-get changelog`, on the first
  hour mark after boot); the image now masks it and apt's daily timers. The blind run
  (HANDOFF-LIMA §11) passed in a `--sudo` sandbox.
- **`--sudo --nested` (A3 step 6, written and RAN 2026-10-14).** An inner VM's only
  way out is 192.168.5.2, the outer guest's loopback. A contained session's nono proxy
  listens there; a `sudo` session and `exec` have none. So every nested egress sandbox
  gets a root forwarder at create (`nested.forward_script`): a systemd socket on
  127.0.0.1:3128 and `systemd-socket-proxyd` to 192.168.5.100:3128, as a `DynamicUser`
  (not the agent's uid, so the nft block lets it through). The state key `egressForward`
  says it is there; `exec` and the `sudo` session point the proxy variables at it only
  then, so older nested sandboxes keep the direct URL for `exec`. The create-time install
  itself uses the direct URL (the forwarder is not up yet). The inner VMs' CAs: with no
  session nono there is no `SSL_CERT_FILE`, so `image.extra_ca_files` gives them Lima's
  `/usr/local/share/ca-certificates/cloud-init-ca-cert-*.crt` (the egress CA and the
  host's extra ones). `resume` does not stop the inner VMs of a `sudo` sandbox: its
  `pgrep nono` cannot see a session there, and no Landlock scope separates sessions.
  RAN on the host (egsn, 2026-10-14): the forwarder, an inner create (Translate) and the
  acceptance run in it, `exec` (its proxy variables, an inner stop/start and an inner curl
  through the forwarder), a resume that left the inner VM running, and an inner `resume`
  that took its credential from the nested marker alone. The acceptance run passed but for
  Translate's `composer phan`, OOM-killed twice (exit 137, 2.65 GB in a 3.9 GB guest) at
  `nested.INNER_MEMORY` 4 GiB. Decided (the engineer, 2026-10-14): the nested defaults are
  what work on wmf-claude needs, 14 GiB (as this `dev` sandbox) and 8 GiB inside; the
  acceptance run gives its sizes explicitly (`--memory 8GiB --disk 60GiB`, the outer
  `--memory 14GiB --disk 120GiB`), so a smaller default does not break it.
- **An inner sandbox's credential (2026-10-14, after egsn).** The inner `resume` found
  none ("Not logged in"): the VM has only its session's phantom. The credential proxy now
  chains in a nested sandbox: `resume` of a nested sandbox writes the session's
  variables (phantom, ANTHROPIC_BASE_URL) into `/etc/wmf-sbx/nested` (root, 0640 group
  agent, replaced with mv), the nested grants give nono `--read-file` of it (without it,
  `nono why`: path_not_granted), and the inner credproxy takes them as its source and
  upstream. The environment variables override them. Outside a nested sandbox
  ANTHROPIC_BASE_URL is ignored, so the host's real credential never follows it. The
  marker keeps a dead phantom after the session; it is worth nothing then.

### Repo resolution and the dependency walk

- WMF CI's dependencies (`ci_deps.py`): CI does not read extension.json; Zuul's
  `set_mw_dependencies` reads integration/config's `zuul/dependencies.yaml` (test jobs,
  closed as `get_dependencies` does, honouring `recurse:`) and `zuul/phan_dependencies.yaml`
  (phan, direct entries). `create` fetches both from gitiles once per run (two requests,
  about 90 KB, GERRITUA, the manifest fetch's retries; no cache) and adds, for each named
  repo, what CI checks out when it is under test, with settings.yaml
  `extra_dependencies:`. Roots only: CI's lists are closures for the repo under test, and
  applying them at every node takes a BlueSpice extension from 3 repos to 84. Defaults
  include the phan map's (so the agent's phan run matches CI); `--no-phan-deps` drops them,
  `--no-ci-deps` the lookup. Unreachable: a warning, and the manifests' dependencies only.
  Without CI's, Parsoid lacks ParserFunctions and 8 `ParserTest_Parsoid_*` tests fail.
- `realpath` every host path that is mounted, stored or compared, including paths the code
  computes about itself (`_SBX_ROOT`, `kit.SBX_ROOT`, the `bin/` shims' `__file__`).
  `abspath` does not resolve symlinks. Do not realpath a host command (e.g. `node`).
  The bind-mounts used inside the sandbox are to real paths, and
  symlinks on the host do not generally exist in the guest, so if we
  use a path containing symlinks it will probably break/refer to a
  different file if/when that same path is used inside the guest.
- Raw-path arguments (`/`, `~`, `./`, `../`, `.`, `..`) skip name resolution.
- Nested-mount guard: refuse two repo directories where one contains the other
  (`find_nested_mount_conflict`; now guards Lima mount points).
- Default sandbox name `sbx-<slug>` (slug from the primary repo); a collision with a default
  name prompts for `name-2` etc.; an explicit `--name` is not re-prompted. The host remote's
  name is the sandbox name itself.
- Gerrit REST: strip the `)]}'` XSSI line; `?m=` matches a substring anywhere in the path, so
  filter on the final path segment yourself; `?p=` is an exact prefix; `?d` adds descriptions.
- Archived Gerrit projects are marked by `[ARCHIVED]` in the description, not by `state`. Warn,
  never exclude; match the final segment case-insensitively and prefer a non-archived match.
- A rule's existing directory is trusted only if its `.gitreview` `project=` matches; a
  directory that exists with the wrong checkout is an error, not `needs_clone`.
- `reverse_resolve`: `.gitreview` is authoritative; then the checkout's `origin` URL (GitLab
  https and `git@gitlab-ssh.wikimedia.org` forms); then exact `repos:` rules only — a
  wildcard rule cannot be inverted.
- GitLab: consulted only when Gerrit has no match; search groups `repos` then
  `toolforge-repos`, never personal namespaces (about half of 4,427 projects are forks).
  `gitlab_upstream` follows `forked_from_project` (depth 5) so `origin` is the real upstream,
  not the engineer's fork. Anonymous: `/projects`, `?search=`, group
  search, path lookup, `archived=true` work; `/search?scope=projects` and `/api/v4/mcp` are
  401; `simple=true` drops `archived` and `forked_from_project`.
- If no canonical is known, `origin` falls back to the host checkout's `origin` URL when it is
  http(s) (`host_upstream_url`; never ssh — the VM has no SSH agent).
- Dependency walk: manifests come from gitiles when a repo is not cloned; a local checkout
  always wins; a checkout with no manifest is a leaf. Gitiles bursts get HTTP 429 with
  `retry-after: 60` (Varnish); retry once. "Couldn't ask" is `UNREACHABLE`, not "no
  dependencies": the create refuses. Unit tests must stub `fetch_manifest` (`patch_offline`).
- A dependency key is a credits `name`, not a directory (44 of 298 manifests differ); keys that
  cannot be a path are refused; `dependency_overrides:` in settings.yaml maps them by hand.
- Link names (canonical's last segment, or the manifest `name` outside extensions/skins, may
  contain spaces) are decided on the host and shipped in the plan JSON; setup never clobbers a
  real directory at a link destination.

### Clones, remotes, gc (host ↔ VM)

- gc: every host repo that a sandbox clones from lists the sandbox in `wmfSbx.borrower`
  (multi-valued) and has gc suspended, with or without a host remote (a "name-taken" skip
  gets none); `rm` unlists it, and gc comes back when no borrower and no marked remote is
  left (`remotes.resume_gc`).
- Submodules (VisualEditor's `lib/ve`, wmf-claude's MCP servers): `init_submodules` in
  `sandbox-repos.sh` takes each from the host's copy in the mounted git dir
  (`modules/NAME`), and else from the URL in `.gitmodules`. When the host's copy lacks the
  commit (a host that pulled without `git submodule update`, or a repo reset to upstream),
  the first try has cloned the submodule with the host's copy as its origin: `submodule
  init` + `sync` point it at upstream before the retry, or the retry fails with "did not
  contain" and `lib/ve` stays empty (RAN, tests in `CloneRepoTests`).
- `git-review-check` (host and sandbox alike) treats `local` as the sandbox's mirror (a
  local-only "is HEAD there" check, not Gerrit) only when `$SANDBOX_NAME` is set. The
  sandbox sets it, not the tool: `session.SANDBOX_NAME_VAR` in the session's settings `env`
  and in `wmf-sbx exec` (not overridable in settings.yaml `environment:`). On the host it is
  unset, so a host repo's own remote named `local` gets the Gerrit check.

- Lima transport: each repo's git dir is a read-only Lima mount under `/run/wmf-sbx/host/`;
  the VM has a `git clone --shared` at the host's absolute path; `origin` = upstream, `local` =
  the mount; VM → host through `wmfsbx://NAME/PATH` (`git-remote-wmfsbx`, upload-pack only, as
  the agent via `limactl shell`, hooks off, push refused).
- Clones at the host path keep Claude's cwd, its `~/.claude.json` `projects` key and memory
  dir, and every baked path (`.env`, `LocalSettings.php`, `vendor/composer/*`, symlinks) the
  same as on the host. The agent cannot `/cd`, so the start path is load-bearing.
- Use `--shared`, not `--reference` (which copied a 685 MB object store). `--shared` needs the
  host's objects to stay: `suspend_gc` sets `gc.auto=0`/`gc.pruneExpire=never` with `--local`
  only, stashes old values with an `(unset)` sentinel, marks the repo
  `wmfSbx.gcSuspended`, and restores only when the last marked remote is gone.
- Two remotes with a `master` make `git checkout master` ambiguous: set
  `checkout.defaultRemote` to the reset remote (in `sandbox-repos.sh`).
- Repos named on the command line keep the host's branch; discovered dependencies are reset to
  upstream with `git safe-reset --force` (no unpushed work exists in a fresh clone);
  `--reset-all` resets everything.
- A host remote without our marker is a human's: never steal or re-point it.
- `unfetched_tips` (the `rm` guard) ignores `refs/remotes/*` and refs equal to a
  remote-tracking tip, so upstream commits fetched in the VM are not "agent work".
- `git safe-reset` accepts `R`, `R master`, `R/master` (split on the first slash, only if `R`
  is a real remote) and names unknown remotes clearly. `git-review-check REMOTE` answers a
  sandbox remote or `local` with a local `for-each-ref --contains=HEAD refs/remotes/R/*`, and
  trusts the remote only when wmf-sbx's state (via `wmf-sbx-ls-remotes`) registered it for
  this repo. Its tests drive real repos.
- Path shortcuts: a NAME shaped like a path resolves through the state file's `primaryDir`
  (one match, else an error). `cp` anchors a relative guest path at the sandbox's
  `primaryDir`, else the agent's home.
- A `:ro` repo is cloned by `engineer` so the agent can read but not write it; upload-pack then
  needs `safe.directory` for `PATH/.git` too.
- Test a read-only mount by attempting the bypass (`sudo mount -o remount,rw` then write), not
  by `touch` or `/proc/mounts`. On Lima 9p, guest root's remount shows `rw` but writes fail and
  the host is unchanged.

### Lima, QEMU and the golden image

- Lima's DNS: the guest asks 192.168.5.3:53, and Lima's iptables NAT sends it to the host
  agent's resolver on 192.168.5.2 at a random port. The host block's filter sees the address
  after NAT, so it accepts by the original destination (`ct original ... proto-dst 53`).
  A host proxy hides a DNS failure: the proxy resolves names itself.
- Debian 13's cloud kernel has no 9p and no virtiofs: the image installs `linux-image-amd64`
  and purges the cloud kernels; the build checks the modules.
- 9p `fscache` serves stale refs after a host `pack-refs`; use `9p.cache: none` (template line
  69). Lima writes the option to fstab at boot, so `limactl edit` takes effect one
  boot later.
- Lima gives its user the host uid unless the template sets `user.uid`: the builder uses 59999,
  sandboxes' `engineer` 59998, and `agent` has the host uid (D10).
- TLS-inspecting networks re-sign github.com: pass extra CAs with `WMF_SBX_CA_CERTS` (Lima
  `caCerts`); the seal removes them. `sudo` drops proxy variables, so the build passes them
  explicitly, with a loopback proxy rewritten to 192.168.5.2.
- A failed image build leaves nothing (export renamed into place); a checkout without its
  submodules is refused, because `git submodule foreach` skips them silently.
- The proxy port is baked into the VM's host block: `start`/`resume` update the provisioning
  (`limactl edit`) before booting a stopped VM. nono needs `--upstream-proxy 192.168.5.2:PORT`
  behind a host proxy.
- `exec` without `-w` falls back to the agent's home when the primary clone does not exist.
- On real hardware (Linux, KVM; `lima-port/checks/real-hardware.sh`, outside nono) every
  browser suite passes: core QUnit 897 tests in 7 s, core wdio 13/13 spec files in 22 s,
  Popups wdio 1/1, Cypress install 12 s and Cite's specs 3/3. What failed under TCG (core QUnit
  not completing, wdio specs failing) was slowness, not the sandbox.
- The settings file is `~/.config/wmf-sbx/settings.yaml` (the engineer's decision; it was
  `repos.yaml`): `repos:` (was `rules:`), `session:`, `environment:` (was
  `extra_environment:`; into the session env at create, not `ANTHROPIC_*`/`NO_PROXY`),
  `packages:` (was `extra_packages:`; into the image inputs, so the key), plus
  `dependency_overrides:`, `link_overrides:`, `extra_dependencies:`. `load_config` reads
  the old file and keys, with one note per run.
- `--reset-all` (checked against the code and in tests): every clone is reset, each to
  `origin`, the upstream URL from its canonical (Gerrit, or GitLab followed to the fork's
  root) or else the host's https `origin`. Only when upstream does not fetch or no upstream
  is known does it reset to the host repo's `master` (or `main`; `git safe-reset` with no
  branch), never to the host's current branch, and it warns for both
  (test_wmf_sbx_repos.py, `CloneRepoTests`).
- `WMF_SBX_GIT_HASH_OVERRIDE=latest`: `create` takes the newest cached image for this
  arch and uid (`image.newest_image`, also used by `login`); it builds, keyed on HEAD,
  only when there is none.
- Golden image key: `WMF_SBX_GIT_HASH_OVERRIDE` replaces the HEAD commit (the engineer's
  choice over keying on tree hashes); the manifest's `commit` is the HEAD that was built.
  At its end, `create` runs `image gc`: unused images older than `imageCache.gcAge` (7 days).
  `image prune` removes every unused image. An image is in use if a state file names it
  or an instance's disk has it in its backing chain (the qcow2 headers, read without
  `qemu-img`, so no lock on a running disk).
- Copy-on-write disks (HANDOFF-LIMA.md B2, `disk.py`): on QEMU, `limactl create` with a shared
  placeholder image, then the instance's `disk` is replaced by `qemu-img create -b
  GOLDEN` (method B), and `disk.check_chain` runs before and after the first start.
  `--full-disk` and `vz` keep the full copy. RAN 2026-10-13 (`lima-port/checks/overlay-b2.sh`
  in a nested sandbox, Lima 2.2.1, KVM; every check passed): golden image 2.57 GiB; a
  `--full-disk` create 32 s and 2.6 GiB, an overlay create 29–30 s and 31–54 MiB; 200 MiB
  written in one overlay grew only that one; two overlays ran at once with their own
  machine-id and SSH host key; stop and start kept the data; `image rm` was refused
  ("in use by" both overlays and the full copy, by state file); `image gc` kept the image;
  after `rm` the golden image was unchanged. With KVM and a local SSD the copy costs little
  time, so B2 saves disk, not create time (B3 is for time). The same check under the
  session's nono (the agent ran it) passed too: full copy 44 s and 2.6 GiB, overlays 31 s
  and 33 MiB each. The outer sandbox for the run
  was made with Lima 2.3.0-beta.1 on the host: the overlay worked there too (`create`
  warned).
- Warm snapshots (HANDOFF-LIMA.md B3, `snapshot.py`): the key is the golden image, `--nested`, the
  egress proxy (only when on, so the plain keys did not change) and the set of (canonical,
  host path, read-only). Behind the egress proxy (2026-10-14, after the merge that made it
  the default): the guest side of the proxy is the same in every egress sandbox and is
  written again at each boot; only the CA differs, so the seal also runs
  `update-ca-certificates --fresh` (as image-build.sh does), and cloud-init adds the next
  sandbox's CA. RAN 2026-10-14 (engineer's host, Cite and 11 dependencies, egress proxy):
  the second sandbox started warm from the first one's snapshot in 80 s; it had one
  `cloud-init-ca-cert` (its own), the first one's CA was not in the trust bundle, and
  HTTPS through the proxy gave 200. The freeze runs at the end of every QEMU
  `create`: the seal (identity, setup logs) in the guest, stop, the disk moved into
  `snapshots/KEY/GEN.qcow2`, `qemu-img rebase` onto the golden image (depth 2), read-only,
  a new overlay for the sandbox, start. A warm create refreshes the clones
  (`refresh_repo`), runs `composer update`, `npm ci` where the lock file changed (a stamp in
  node_modules) and `update.php`. Snapshots borrow the host repos' objects (the engineer's
  choice over dissociating them): the key is a gc borrower, `snapshot-KEY`, and a host repo
  without the entry makes the create cold. Golden images age (`imageCache.maxAge`, an `epoch`
  input); snapshots do not. RAN 2026-10-13, the freeze only: the host's `create --nested
  --no-deps` of wmf-claude (Lima 2.3.0-beta.1) froze its snapshot and restarted on an
  overlay on it, and `status` showed the chain sandbox → snapshot → golden. RAN 2026-10-13
  (`lima-port/checks/warm-b3.sh` in a nested sandbox, Cite and its 11 dependencies, inner
  sandboxes at 4 GiB; every check passed): cold 721 s (with a 6-minute image build),
  snapshot 4.8 GiB; warm 92 s and 94 s, with `composer update` about 15 s in all and every
  `npm ci` skipped; the warm sandbox had the host's new commit, a working wiki
  (`showSiteStats`) and its own machine-id. Each freeze writes a full generation (the
  rebase onto the golden image copies the snapshot's data: 5 GiB per create), so a warm
  create freezes only when its snapshot is older than `imageCache.snapshotInterval` (1 day; the
  engineer's choice over a fingerprint of what changed); a superseded one goes at the next
  `image gc` after an hour. B4 RAN 2026-10-14 (`lima-port/checks/b4.sh`, nested, inner
  sandboxes at 3 GiB, every phase passed): two warm creates at once, 106 s each, froze one
  after the other (34 s and 40 s, 4.7 GiB each) on their own generations; a warm create with
  no freeze (snapshot minutes old) took 58 s, so the freeze costs about 45 s; a golden
  image rebuilt with `maxAge: 0m` made the next create cold on it (636 s with the build),
  the sandbox on the old image still started and passed `status`, `image gc` kept its
  files, and `image prune` removed the old image and its 3 snapshots once nothing used
  them. RAN 2026-10-14 on the engineer's host, not nested (Translate, Cite and Parsoid,
  24 repos): the cold create froze in 19 s (8.4 GiB); a warm create from it, with no
  freeze, took 113 s. The golden image is keyed on the
  wmf-claude HEAD, so in development every new commit makes the next create cold.
- Host memory (RAN 2026-10-13): QEMU keeps every page a guest touched (Lima's QEMU has no
  balloon), so a 16 GiB nested sandbox that built an image held 15 GB of the host, and the
  host's OOM killer took it, twice, on a 60 GiB laptop with other sandboxes up. Now every
  QEMU start goes through the QEMU wrapper in plain mode (`WMF_SBX_QEMU_PLAIN=1`, set by
  `vm.start_instance`), which adds `virtio-balloon-pci,free-page-reporting=on`; the nested
  default is 14 GiB and a sandbox inside one defaults to 8 GiB (was 10 and 4, until phan
  was OOM-killed at 4 inside). RAN 2026-10-14
  (`lima-port/checks/balloon.sh`, the engineer's host, Lima 2.3.0-beta.1): the guest bound
  the balloon; the QEMU's RSS went 809 MiB, 3873 MiB with 3 GiB touched in the guest, and
  751 MiB 30 s after the guest freed it. Page cache is not free memory: a guest that read a lot keeps it until it needs
  the memory.
- PCI slots (RAN 2026-10-14): Lima gives each git-dir mount a `-virtfs`, which takes a
  slot of q35's root bus. With Lima's 6 devices the bus held 24 mounts, or 23 and the
  balloon (QEMU 8.2.2, locally). Translate, Cite and Parsoid with their dependencies are
  24 repos, and the engineer's sandbox did not start ("no slot/function available for
  virtio-balloon-pci"; a plain `limactl start`, without the balloon, booted it). A
  `pcie-pci-bridge` behind a root port took only 31 more. The QEMU wrapper now makes each
  `-virtfs` an `-fsdev` plus a `virtio-9p-pci` at an explicit `addr=SLOT.FN`, 8 functions
  to a slot from 0x1e down (`qemu_wrapper.spread_virtfs`); `template.MAX_QEMU_MOUNTS` is
  96 (12 slots). Local QEMU booted Lima's devices with 1, 25 and 96 such mounts and the
  balloon. On the engineer's host (Lima 2.3.0-beta.1, `21382a1`) that 24-repo sandbox
  started with the mounts at 00:1c.0-00:1e.7 and the balloon at 00:07.0; all 24 mounted,
  and `lima-port/checks/balloon.sh` gave back 3060 MiB.
- Default VM memory is 8 GiB: phan on Translate was OOM-killed at 4 GiB.
- Timings under TCG are 5–35× slower than a 16-core host (image build 26–41 min, create
  ~250–310 s, setup for Translate ~40 min); only compare them with each other.

### Shared login (track L)

- L1 (option H, RAN in the cloud test VM with a Console API key; `lima-port/checks/
  credential-proxy.py`): Claude Code 2.1.287 works through a host reverse proxy with
  `ANTHROPIC_BASE_URL=http://192.168.5.2:PORT` and a phantom `ANTHROPIC_API_KEY`, outside
  nono and in a session (`resume --open-port=PORT`, the base URL and `NO_PROXY=192.168.5.2`
  in the settings env). One turn with a tool: two `POST /v1/messages`, nothing else on the
  base URL. Besides those: `HEAD /api/hello` without a credential (a connectivity probe;
  the proxy forwards it without one). The real key never entered the VM.
- In a session the proxy port needed `--landlock-only` (removed 2026-10-16 with nono
  0.80, upstream eb4961d) and a per-session
  `--open-port`; nftables needs an accept for 192.168.5.2:PORT in `block` and
  `agent_egress` (`PROXY_PORTS`).
- On the engineer's host (Linux, KVM; `credential-l1.sh`) all three sources work through the
  proxy, outside nono and in a session: a `setup-token` token and the live login
  (`~/.claude/.credentials.json`) as `Authorization: Bearer`, a key as `x-api-key`. In
  OAuth mode too, the base URL gets only `POST /v1/messages` and the open `/api/hello`;
  nothing failed for want of other endpoints.
- Option N (nono `--credential anthropic`): the agent sees `ANTHROPIC_BASE_URL=
  http://127.0.0.1:PORT/anthropic` and a 64-character phantom, not the key. But the key is
  in the VM (the `/dev/shm` hand-over, the environment of the wrapper and of nono's
  supervisor), and the built-in service is API-key only. **Chosen: H** (L1 done).
- L2 RAN in the cloud test VM: a session with a tool call through the proxy, with the key
  on the host only; the rule is removed after the session.
- L3 in the cloud VM (`lima-port/checks/credential-l3.sh`, an API key): sessions in two
  sandboxes, both through their own proxy; `scan` found the key nowhere: not in any
  process's environment or command line in either VM during a session, not in `/dev/shm`,
  `/tmp` or the agent's Claude Code files, not on either VM's disk (3 and 23 GB), not in the
  host's wmf-sbx state.
- L3 on the engineer's host (Linux, KVM): `concurrent` passes with the live login (both
  sandboxes' requests in the same second) and `scan` passes with the live login and with
  the token file (access and refresh tokens: 0 hits in processes, files and both disks).
  `create` removed 4 unused images (10 GiB) after its build.
- An idle host's login expires: with no host Claude Code running, the access token had
  expired, and the sessions got the proxy's 401 ("run `claude` on the host once"). One
  host `claude -p` refreshed it. If `long` shows this mid-session, consider letting the
  proxy have the host's Claude Code refresh the login (not refreshing it itself: the
  refresh token rotates, and the host's copy would break).
- `long`, first version (the model asked to `sleep` in a loop): the session ended within 10
  minutes (209 requests, all 200); Claude Code does not block on long commands. Now the
  script feeds one `claude -p --input-format stream-json` session a message every 10
  minutes (RAN: 3 of 3 replies, one proxy).
- `limactl shell` passes stdin to SSH, which reads it even when the command does not:
  piped input to `resume` was eaten by the checks before the session. `Limactl.call` now
  gives limactl `/dev/null` unless the call has `input` or is `interactive` (the session,
  `exec`); the other capturing subprocess calls also get `/dev/null`.
- `long 9` on the engineer's host, live login, host idle: one session and one proxy for 9
  hours, a message every 10 minutes. 45 of 54 replies; every request until the login's
  expiry (7.5 hours after its last refresh) got a 200, every one after it the proxy's 401
  (two per message: Claude Code retries once). The token never changed: nothing refreshes
  an idle host's login. So a long session works through the proxy, and the host login is
  not a source to rely on; `wmf-sbx login` is.
- `wmf-sbx login` on the engineer's host (Linux, KVM): the token was read from the
  transcript and saved; `resume` then used it with no login prompt. With the pty at 2000
  columns, Claude Code's text was garbled on the engineer's terminal; the pty now has the
  terminal's own width (the engineer's decision), and `find_token` joins a token that
  wraps (a line filled to the width, ending in token characters, goes on in the next).
- `--continue` does not take up a `-p` run ("No conversation found to continue"): only an
  interactive session marks a sandbox attached.
- Blind run with a `wmf-sbx login` token (engineer's host, KVM, Translate + Cite + Parsoid,
  2026-10-12): 18 minutes; first green suite after 25 s; every suite passed or failed as the
  guide says, except Cypress (does not start under nono) and the guide's errors it found
  (parsertests scope, `--testsuite=extensions`, core's other suites, Parsoid needing
  ParserFunctions), now fixed in MEDIAWIKI-TESTING.md.
- Still to run: macOS by someone with a Mac (`MACOS.md`, a guide for a human tester).
  The check scripts avoid bash 4 features and empty-array expansions under `set -u`
  (macOS has bash 3.2).
- Docker sbx's sign-in (Docker's docs, and our pre-deletion SECURITY.md §7.5): `/login` ran
  in the sandbox; the host's egress proxy completed the OAuth exchange, kept the token on
  the host (one per host, shared by every sandbox), refreshed it, and swapped a
  `proxy-managed` placeholder for it on the way out. It needs TLS interception, so here it
  waits for track A.
- `setup-token` tokens (Anthropic's docs and issues): one year, scope `user:inference`. Not
  possible with one: Remote Control, claude.ai connectors and synced skills and plugins,
  `/usage` (scope error), cloud sessions and `/schedule`; no expiry warning. Local MCP
  servers work. `wmf-sbx login` makes one in a throwaway VM (RAN in the cloud VM up to
  the code prompt: `script` is in the image, and the URL is an OSC 8 hyperlink). Without a
  terminal setup-token waits for ever, so `login` refuses to start; a SIGTERM left the VM
  running (Python runs no `finally` for it), so SIGTERM and SIGHUP now stop it cleanly.
- L5 (`lima-port/checks/oauth-l5.sh`, engineer's host, Linux, KVM, nono 0.79, Claude Code
  2.1.292, 2026-10-15): nono's `claude_code` `oauth_capture` provider works in the host's
  standalone `nono proxy`, although the proxy's route list shows only
  `api.anthropic.com | creds: none`.
  - `/login` in the guest succeeded. The guest's `.credentials.json` has a phantom access
    token (`sk-ant-oat01-`, 77 characters) and a phantom refresh token (`nono_`, 69), the
    full login's scopes (`user:inference`, `user:profile`, `user:sessions:claude_code`,
    `user:mcp_servers`, `user:file_upload`, `user:plugins`), and `subscriptionType: null`.
    The real tokens are in the host's per-sandbox store
    (`egress/NAME/state/nono/oauth-capture/providers.json`).
  - `POST /v1/messages?beta=true` with the phantom: 200. A forced refresh (`expiresAt` 0)
    went through `POST platform.claude.com/v1/oauth/token` (200) and gave the guest a new
    phantom; the next turn worked.
  - `scan`: 0 real tokens in the guest's credentials file, `~/.claude` and every process's
    environment, in both sandboxes.
  - The login does not move to another sandbox: the file copied to l5-b gave "OAuth
    session expired and could not be refreshed" (l5-b's proxy has no capture for those
    phantoms). A login is per sandbox, as the per-sandbox stores predict.
  - The narrow policy (`POST /v1/messages` only) denies (403) everything else Claude Code
    calls on `api.anthropic.com`: `GET /api/oauth/profile`, `/api/claude_cli/roles`,
    `/api/claude_cli/bootstrap`, `/api/claude_code/settings`,
    `/api/claude_code/policy_limits`, `/api/claude_code/organizations/metrics_enabled`,
    `/api/organization/claude_code_first_token_date`, `/api/claude_code_penguin_mode`,
    `/v1/mcp_servers`, `/mcp-registry/v0/servers`; `POST /api/eval/sdk-*` (feature flags)
    and `/api/event_logging/v2/batch`. `downloads.claude.ai` is not in the profile's
    allowlist. Effects: `/status` shows "Login method: Claude API account" and
    "Organization policy: Couldn't load", so remote managed settings are not applied (they
    fail open) and Remote Control, `/feedback` and web fetch stay off; `/usage` shows the
    session and week limits (from the message responses) but "Could not refresh usage
    data". Turns work.
- L5 `persist`: the login survives a stop and start of the sandbox (the store is on the
  host's disk; the proxy is restarted with the VM).
- The wider policy (default allow, `deny` with method `*`), RAN in the cloud container
  through a standalone `nono proxy` 0.78: `/v1/mcp_servers?limit=1`, `/v1/mcp_servers/x`,
  `/mcp-registry/v0/servers` and `/api/event_logging/v2/batch` (GET and POST) get nono's
  403; `/api/hello` and `/api/oauth/profile` reach Anthropic. The query string is not
  part of the match.
- L6, first run (engineer's host, 2026-10-15): `/login` in l6-a worked in a session under
  nono (the banner then says "Claude Pro"; profile, roles, bootstrap, metrics, ultrareview
  quota and `/api/frame/contract/latest` reach Anthropic; the four denied paths are
  refused). The host copy and the store were written. But l6-b and l6-c, with the same
  unexpired phantoms, failed: "OAuth session expired and could not be refreshed" (401,
  then 400 from the token endpoint). Cause, from nono's source (`main` 45fcc24,
  `oauth_capture/mod.rs`, `persist.rs`): `nono proxy` reads the capture store once, at
  start, and writes its whole in-memory map at each capture. l6-b's and l6-c's proxies
  had started before the login. Fix: `authproxy.refresh_proxy` restarts a proxy older
  than the store file (`allow.swap`) before the sandbox gets phantoms;
  `sbx/upstream/NONO-ISSUE-1.md` for nono. The `rotation` result of that run does not
  count: l6-b's proxy could not resolve even the old phantoms.
- L6, second run (engineer's host, 2026-10-16, with the proxy restart): l6-b and l6-c
  (sudo) used l6-a's login with no `/login`. Then:
  - **A refresh revokes the old refresh token and the old access token.** After l6-a
    refreshed (200), l6-b's refresh with the old refresh phantom got 400; and the old,
    unexpired access phantom, put back into l6-b by the sync (its proxy restarted, so
    it knew the phantom), got 401.
  - **Two refreshes at once: one wins.** l6-a and l6-b, both expired, refreshed with the
    same refresh phantom in the same second: one 200, one 400. The loser's Claude Code
    emptied its credentials file ("OAuth session expired and could not be refreshed").
  - So a sync can push a dead login: after the race the host copy was the login that
    the winner's refresh had revoked (the refreshes ran by `exec`, outside a resume's
    sync), and l6-a failed with it until the winner's login was pulled.
  - Consequence: every sandbox refreshing for itself does not work for a shared login.
    Two active sessions reach the same `expiresAt` and refresh together every 8 hours;
    and any refresh breaks the other sandboxes until their files have the new phantoms.
- L6 `live`: a session in l6-b sent a message every 20 s while l6-a refreshed (exec) and
  was synced to the host. The message right after the refresh failed (401, then the
  token endpoint's 400 for the old refresh phantom); after the next sync, the session
  went on with the new phantoms (one reply "tick-4\ntick-5", then all). So a running
  Claude Code re-reads its credentials file.
- L6 `trigger`: in a throwaway `CLAUDE_CONFIG_DIR` with `expiresAt` 0, `claude auth
  status --json` does not refresh; `claude -p ok --model haiku` does.
- L6 `rc`: Remote Control works with the auth proxy's policy (the telemetry stream
  denied): `POST /v1/code/sessions`, then `.../bridge`, `.../worker` (PUT, GET),
  `.../worker/events` (and `/delivery`, `/stream`), `.../worker/heartbeat`,
  `.../client/presence`, `.../archive`. `/status`: "Claude Pro account", the
  organization, "Cloud sessions: GitHub connected"; `/usage` shows the limits. Also
  allowed: `/v1/messages/count_tokens`, `/api/oauth/usage`, the organization's skills,
  plugins and marketplaces, `/api/oauth/account/settings`. `scan`: 0 hits in l6-a, l6-b,
  l6-c (14 token-like values in the store).
- L7 `l7` (engineer's host, 2026-10-16, refresh every ~8 minutes, 25 minutes): four
  refreshes, each one 200 from one sandbox, no double refresh, no 400 but one; l6-a's
  resume refreshed at its start (18:00) and at 18:08, then its session ended and l6-b's
  resume refreshed at 18:16 and 18:24. l6-b: 50 of 50 replies, across all four. l6-a: 24
  of 25: its message right after its own 18:08 refresh got 401, then 400 for the old
  refresh phantom. The gap was longer than it had to be: the refresh script waited for
  the rest of the Haiku turn after the refresh, and the refresher restarted its own
  proxy (which had captured the new login) before the push. Now the script stops
  `claude` once the file has the new login, and the refresher marks its own proxy
  current.
- Other denials in the login session, not ours to allow: `downloads.claude.ai` (Claude
  Code's update and plugin downloads), `http-intake.logs.us5.datadoghq.com` (telemetry),
  `raw.githubusercontent.com`.
- A shallow host repo (`git clone --depth`): `git clone --shared` copies the objects and
  writes no alternates file; `sandbox-repos.sh` accepts that (RAN).

### nono, Landlock and the session

- **nono 0.80 opens ports on the loopback only** (2026-10-16). RAN (this VM, the 0.80.0
  .deb extracted to scratch, a listener on the guest's own address): with `--open-port P`,
  a connect to 127.0.0.1:P gets 200, to 192.168.5.15:P "Could not connect"
  (`--allow-connect-port P` does not change it; 0.78 refused both, nono#1786). Through
  nono's proxy (`--allow-domain '*'` or the IP; with a dead `--upstream-proxy`, and
  `--upstream-bypass IP`) it works, and `Authorization` and `x-api-key` pass unchanged. So
  the first nono-mode session on 0.80 failed: Claude Code reached the credential proxy at
  192.168.5.100:3129 directly ("Unable to connect to API (EACCES)", RAN by the engineer in
  sandbox `nono80`). Now: nono sessions get `NO_PROXY=localhost,127.0.0.1` in settings (a
  sandbox made before needs a new `create`), and `--upstream-bypass` for the address (plus
  `--allow-domain 192.168.5.2` off the egress proxy); the clipboard shim sends a CONNECT to
  nono's proxy when `NONO_PROXY_TOKEN` is set (RAN with 0.80: the text arrives; a 502 is
  reported). `--sudo` sessions have no nono and keep the direct path. Not RAN yet: a nono
  session with this change; a nested sandbox's inner credential proxy, which connects to
  the outer one at 192.168.5.100 from under the outer session's nono (likely refused too).
- **nono 0.80 image, `--sudo` session** (RAN by the engineer, sandbox `nono80-sudo`, wmf-claude
  2e819af, 2026-10-16): guest nono 0.80.0; wiki on 4000; bind/connect on 0, 49152, 53247,
  9876 (and 5000: no nono); Gerrit fetch and ls-remote; wiki POST 403, GET 200; kernel.org
  "CONNECT tunnel failed, response 403"; `%2F` path 403; karma started Chrome and
  connected on 9876 (after `mw-install-browser`: `/usr/bin/chromium` is a stub); php has
  ast, intl, mbstring; core's `tests/phpunit/unit/includes/Json` OK (244 tests).

- **Cypress under nono**: the session grants `~/.config/Cypress` (Electron; without it
  `cypress verify` dies with SIGTRAP) and `/var/lib/xkb` (Xvfb writes its compiled keymaps
  there, else to `/tmp`, which the session cannot read back, and Xvfb stops: "Failed to
  activate virtual core keyboard"). image-build.sh makes both for the agent: nono grants
  only paths that exist. Xvfb's socket and lock in `/tmp` need no grant. Cypress's DevTools
  port is OS-assigned, so it needs the VM's narrowed ephemeral range (sandbox-provision.sh):
  outside it, `connect EACCES`. RAN (cloud container, an `agent` user set up as
  image-build.sh does, the session's flags from `session.launcher_argv`,
  `lima-port/checks/cypress-nono.sh`): Cypress 15.21.1 `verify` and a spec pass under nono;
  without the two grants `verify` fails. RAN (engineer's host, a sandbox from a new image,
  2026-10-13): Cite's `npm run selenium-test` gives the same result under the session's nono
  as outside it (referencePreviews 2 of 3; the failure is Cite's, under Parsoid without
  `ExtTagPFragment`, T440934), and an agent that follows §5.6 installs and runs it unaided.
- The profile grants no read of the agent's home, only its dot-directories: the session grants
  `~/MEDIAWIKI-TESTING.md` and `~/.wmf-sbx` by name; an engineer's file needs
  `wmf-sbx resume --read-file=PATH`.
- The profile denies `~/.bashrc`, so create removes it (also after `wmf-claude-setup`, which
  writes its alias there).
- Caches: grant `~/.cache` and `~/.npm`; not `~/.config/composer` (the profile denies its
  `auth.json`, and a deny under an allow makes nono refuse to start: "Landlock deny-overlap
  is not enforceable on Linux"). So `COMPOSER_HOME=~/.cache/composer-home`.
- `/tmp` is write-only in a session; use `$TMPDIR`.
- Local ports (DECIDED, engineer): Landlock grants bind/connect per port and cannot tell
  loopback from other hosts, so the guest's ephemeral range is narrowed to 49152–53247
  (`tcp_tw_reuse`) and each session opens 4000, 9876 and that window plus `--listen-port 0`;
  the VM's nftables limits off-VM TCP to 80/443. Bookmark (reviewed 2026-10-14, after track
  A): a sandbox behind the egress proxy has no DNS residual (the guest's resolver is
  `fakedns.py`, which asks no real resolver). There, the guest's network filtering is a
  second layer: `restrict=on` gives the guest no route, the session's nono passes every host
  (`--allow-domain '*'`) to the host proxy, which is the only allowlist and also applies
  the endpoint rules. Contained mode on that path could keep nono for the filesystem and
  drop its network half: the port window (`--landlock-only` went with nono 0.80),
  the narrowed ephemeral range, `--upstream-proxy`, and nft's agent 80/443 rule. The cost:
  nono with `--allow-net` removes the proxy variables from the child (A2.0 item 9), so the
  launcher (`bin/claude` or `launch-claude.sh`, shared with wmf-claude outside wmf-sbx)
  must export them again inside the sandbox. It is not worth doing while `vz` and
  `--no-egress-proxy` sandboxes exist: they keep all of the guest filtering, so the change
  adds an egress/no-egress branch to provisioning, the session launch, the invariants and
  the tests, and removes no code. It pays only if `vz` gets an equivalent of `restrict=on`
  (`MACOS.md`). For a sandbox not behind the proxy, the DNS residual
  stays (lookups through Lima's resolver can tunnel data); its separate fix is a filtering
  DNS forwarder in the VM (dnsmasq or unbound, as root) that answers only for nono's
  allowlist (the profile's domains plus `session.REGISTRY_DOMAINS`): only nono's proxy
  needs DNS, because nft and Landlock keep the agent's other traffic off the network. DNS
  over HTTPS does not help: any recursive resolver forwards `<data>.evil.example` to its
  name server.
- Browsers without sudo: the image has the Chrome/Cypress libraries, Xvfb and xauth;
  `/usr/bin/chromium`/`chromedriver` wrap links in `~/.cache/wmf-sbx-browser`; the wrapper
  adds `--no-sandbox`, a temporary `--user-data-dir` (headless Chrome will not start without
  one under nono) and `XDG_CONFIG_HOME` under the cache (crash handler). `/etc/fonts` is
  granted; helpers must not read the dpkg database.
- Credential (track L): `credproxy.py`, run by `resume` for the session; the VM gets the
  proxy's address and a phantom, on stdin into a 0600 file in `/dev/shm`. The proxy's port
  is opened per session with an nftables rule tagged `wmf-sbx-cred-PORT` (a per-session
  port, not one per sandbox, so that two sessions of one sandbox work).
- nono sets `NO_PROXY=localhost,127.0.0.1` and drops the caller's; the proxy's address goes
  into `NO_PROXY` through the settings `env` (`session.CREDENTIAL_PROXY_ENV`), or the API
  call goes to nono's proxy and gets a 403 (RAN). `ANTHROPIC_*` variables pass through nono.
- With a base URL that is not Anthropic's, Claude Code turns tool search off unless
  `ENABLE_TOOL_SEARCH=true` (its debug log says so); the session sets it.
- Claude Code also calls api.anthropic.com directly (bootstrap, fast-mode status, the MCP
  registry, telemetry), through nono's proxy and without the base URL; these are not
  needed for a session.
- `resume` adds `--continue` only once `attached` is set, and sets it only after a clean run
  whose arguments could start a conversation (`starts_a_conversation`: not `--version`, not a
  subcommand such as `mcp`).
- Git identity: create puts the host's `user.name`/`user.email` into the settings `env` as
  `GIT_AUTHOR_*`/`GIT_COMMITTER_*`.
- `~/.claude/CLAUDE.md` tells the agent to end each turn with a commit on a branch (undo with
  `--amend` or `reset --soft HEAD^`) and names the host remote; "waiting for instructions to
  commit" leaves work unreachable.
- MCP: upstream's anonymous MCP servers run in the VM. Standing constraint: a server that runs
  in the sandbox puts its credential in the sandbox (Conduit `PHABRICATOR_API_TOKEN`, Gerrit
  `gitcookies`); authenticated servers need another design.
- Register MCP servers with `claude mcp add --scope user`: a plugin-root `.mcp.json` renames
  tools to `mcp__plugin_<plugin>_<server>__<tool>` and breaks the skills' literal names.
  `claude mcp add` refuses a taken name (remove then add); do not hand-edit `~/.claude.json`.
- mcp-phabricator needs Node ≥ 20.18.1 (cheerio → undici `File`), above its own `engines`;
  `npm` does not enforce `engines`. The image's Node 24 satisfies it.
- Permission rules: `permissions.deny` is honoured under `bypassPermissions`; an absolute path
  needs `//` (single `/` matches nothing); `:*` is a prefix match and only at the end
  (`Bash(find *-exec*)`); `Write(path)` is never consulted — use `Edit(path)`. A rejected rule
  is skipped silently: validate with `claude --settings FILE doctor`. A deny list is a speed
  bump, not a boundary (the agent reached for `Bash(cat …)` when `Read` was denied).
- `sudo` replaces PATH with `secure_path`, so tools in `~/.local/bin` are not found under
  sudo.

### MediaWiki facts and the setup

- `composer mw-install:sqlite` loads no extension without `-- --with-extensions` (skins are
  found anyway).
- `.env` is read only by docker compose; core's Gruntfile and `wdio-defaults.conf.js` read
  `process.env.MW_SERVER`/`MW_SCRIPT_PATH`; api-testing reads `.api-testing.config.json`.
  So the values go into the session environment, parsed from core's own
  `mw-install:sqlite` script (`parse_install_params`).
- `MW_SCRIPT_PATH` is empty/`/` with `composer serve` (docroot = core); `/w` is the
  docker-compose layout.
- `composer.lock` is gitignored in core, `package-lock.json` is tracked: use `npm ci`, which
  reinstalls `node_modules` on every run.
- Failure policy: per-repo steps (safe-reset, `composer update`) warn and continue;
  `.env`/npm/install are fatal; the whole MediaWiki phase needs a writable core. A failed
  setup fails the create but keeps the VM; the steps are idempotent.
- Extension Composer libraries are merged into core's `vendor/` as quibble does:
  `composer.local.json` with `extensions/*/composer.json`, `skins/*/composer.json`,
  tab-indented, written before core's update, links made first; each repo keeps its own
  `composer update`; an engineer's own `composer.local.json` is left alone. When Parsoid is a
  workspace clone, exclude `vendor/wikimedia/parsoid/` from the classmap (359 ambiguity
  warnings otherwise).
- Parsoid clone: the `LocalSettings.php` block uses the literal path, has no
  `vendor/wikimedia/parsoid` guard, goes above `// Enabled skins.`, and replaces
  `wfLoadExtension( 'Parsoid' )`. Set `MEDIAWIKI_HAS_INTEGRATION_TESTS=1` with a writable
  Parsoid clone, or unit-only PHPUnit runs skip `LocalSettings.php` and use the vendor
  Parsoid.
- The generated `.api-testing.config.json`: `base_uri` = server root with a trailing slash;
  tab indents (core's eslint lints JSON); `API_TESTING_CONFIG_FILE` (absolute) works from an
  extension.
- Run PHPUnit suites one at a time per core: parallel runs share SQLite `unittest_` tables
  (`no such table: unittest_searchindex`).
- `composer serve`: poll up to 30 s (cold start returns 000); check for an existing server on
  4000 first, log to a file, stop polling if the pid died.
- Known upstream failures: 17 core api-testing REST failures from an exact-case header lookup
  (`ParamValidatorCallbacks::getValue`, T412668 change), Flow's api-testing needs
  `npm install --no-save mocha`; Popups' `mw-node-qunit` crashes on Node ≥ 21/22
  (`navigator` getter) and the pipe reports success — use
  `NODE_OPTIONS=--no-experimental-global-navigator` and read the test count.
- The sibling gap also fails core `--testsuite=structure` (`ResourcesTest`) and makes selenium
  and Cypress specs skip (0 specs is not a pass).
- Browsers: karma's default is FirefoxHeadless; `npm ci` downloads no browser. Chrome runs need
  `CHROME_BIN`, wdio needs `CI=true` and `MEDIAWIKI_USER`/`MEDIAWIKI_PASSWORD`.
- Cypress: binary hosts are only `download.cypress.io` and `cdn.cypress.io` (run-time
  `api`/`cloud.cypress.io` are not needed); `cypress install` exits 0 when the download is
  refused, so check for the binary; honour a repo's `CYPRESS_CACHE_FOLDER`; setup keeps
  `CYPRESS_INSTALL_BINARY=0`; `cypress-parallel` rewrites tracked `parallel-weights.json`;
  use `cypress:worker --spec` for error text; give Chrome by path; Popups needs TextExtracts
  and PageImages.
- Shell traps for helpers: `set -o pipefail` + `| grep -q` fails on SIGPIPE; send progress to
  stderr when stdout carries a result.
- php-ast is built from source at the commit WMF CI pins (`image.PHP_AST`; Debian 13's 1.1.2 is
  too old for phan 6). Change it when CI does.
- WMF CI targets Node 24 (24.18.0); the image has one global Node from the nodejs.org tarball.
- `wmf-sbx exec` has the session's variables (RAN 2026-10-14: `exec NAME bash` had no
  `$MW_INSTALL_PATH`, and an extension's `composer phan` failed; a session gets them from
  `~/.claude/settings.json` `env`, which exec never read). `create` records them in the host
  state (`sessionEnv`, less the credential proxy's); exec passes them. Not from the guest's
  settings.json: the agent can write it, and exec runs outside nono (an `LD_PRELOAD` there
  would run in an engineer's exec). A sandbox made before this has none: recreate it.
- One create per sandbox name: `create` holds an flock on `sandboxes/.NAME.creating`
  (`state.reserve`) from the moment it has the name to its end, before the golden image;
  `reserved_names` counts as taken. Before, a second `create --name X` started while the
  first one built the image went on, and would have failed only at `limactl create`.
- One build per golden image key (RAN 2026-10-14: a second `create` of the same repos,
  started while the first built the image, deleted the first one's running builder as
  "left by an earlier build"). `image.build` holds an flock on
  `images/.build-KEY.lock`; a builder with the lock free is a crashed build's, and goes.
  With the lock held, `busy_prompt` asks: wait (default), delete the other builder, or
  abort; not on a terminal, abort. The lock files stay (removing one races a waiter).
- Dependency repos get no `composer update` and no `npm ci` (2026-10-14, decided by cananian
  after the B4 blind run): setup runs them only in core and in the repos named on the
  command line (the plan's `requested`). No extension or skin of the test sandboxes loads a
  ResourceLoader file from `node_modules/`, so the wiki needs none. Setup removes an untracked `vendor/` that a warm snapshot brought into
  a dependency. With every repo updated, each dependency had a `vendor/` beside core's
  merged one, which CI does not: phan reported `PhanRedefined*` (Translate: all 243 of its
  issues, from `AbuseFilter/vendor/psr/log` and `Elastica/vendor/ruflin/elastica`; 383
  across 24 repos), and EventLogging's `load_composer_autoloader` loaded its json-schema
  6.13.1 over core's 5.3.4 (2 `ArrayDefTest` failures in `core:unit`). The session's
  CLAUDE.md and MEDIAWIKI-TESTING.md §0 tell the agent to run `composer update` and `npm
  ci` in a dependency it changes, before that repo's own scripts. RAN 2026-10-14 for
  composer (engineer's host, Cite): the setup log named the 10 dependencies, only Cite had
  a `vendor/` of its own, and Cite's phan had no `PhanRedefined*`. The `npm ci` half is not
  yet RAN.
- Setup timings on real hardware (Linux, KVM; Cite, Popups, TextExtracts, PageImages, core,
  Vector, cold caches): every `composer update` together 27 s (core 20 s: resolve 2,
  install 12, autoload 6), every `npm ci` together 19 s. So no composer cache in the golden
  image: it would save seconds. Look elsewhere when a create is slow.
- `composer test` in core took 22 min under TCG: the session sets
  `COMPOSER_PROCESS_TIMEOUT=0`.
- On networks that block GitHub archive downloads, run
  `wmf-sbx exec NAME -- composer config --global preferred-install source` before setup;
  source installs are large (core 5.9 GB, composer cache 4.9 GB).
- Acceptance runs: in `-p` mode each Monitor event is a new full-context turn (run 2 cost
  ~$18); allow `Monitor`, `TaskOutput`, `TaskStop` with `--allowedTools`. A `-p` session
  ends at the first turn that ends with no Monitor running, even with background Bash jobs
  still running (RAN 2026-10-14, KVM: the run ended after 9 min, before its report); the
  prompt in `lima-port/acceptance/README.md` tells the agent so. Run 3 (RAN 2026-10-14,
  KVM, Translate, Cite and Parsoid, 24 repos, on the overlay that its own create froze):
  28 min, 149 suite runs; first green run after 1.5 min; no red result in Translate's code;
  findings under "Testing and the MediaWiki setup". It found `~/.wmf-sbx/` empty: the
  freeze's seal removed the setup log and status from the sandbox that froze (now saved on
  the host and put back, `snapshot.keep_script`). Sonnet-class agents
  can follow the guide.

### Method (lessons that still apply)

- A negative finding is only as strong as its search: name the surface searched (`--help`,
  rendered page vs API comments, `ls` vs `ls -la`). "We don't use it" ≠ "it isn't there".
- Probe a binary's capability (`--help` contains the flag), not its version number.
- A test that greps the command it tests checks spelling, not behaviour: run the script
  against stubs. Find steps by content, not by list position or count.
- A step that verifies its artifact by a path the product does not use verifies nothing.
- Tests must isolate `HOME`/`XDG_CONFIG_HOME`: adding a config file adds a way for the suite to
  read the developer's machine.
- Capture exit codes in the same compound command; make each probe depend on as little as the
  question allows; check measurement designs for confounds.
- Error text a model will read is interface: say what happened and what did not.
- Any advice to remove something should hand back the exact argv to restore it.
- Code baked into a VM at image/create time reaches only new sandboxes; changing it is a
  rebuild or recreate.

