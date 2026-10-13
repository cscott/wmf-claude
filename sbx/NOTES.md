# wmf-sbx notes

Open to-do items, and the decisions and findings that the code depends on
or that future work needs. No history: `git log` has it. The design is in
`README.md` (Architecture) and `../lima-port/HANDOFF-LIMA.md` (decisions D1
to D12, and tracks A and B, which are not repeated here).

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

Docker sbx leftovers:
- [ ] **`state.new_state` still writes `daemonPort`/`hostPort`**, which nothing reads.
- [ ] **`bin/wmf-sbx`'s `RETIRED` verbs** and their "belonged to the Docker sbx backend"
  message.
- [ ] **Docstrings and comments that name Docker sbx or `sbx create`/`sbx run`**:
  `__init__.py` ("host-side tooling for Docker Sandboxes"), color.py (the palette "the
  real `sbx create` uses"), create.py (`sbx create` requires ...), repos.py ("as with
  Docker sbx"), ls_remotes.py ("the name-prefix guess it used to make", "NOTES §48").
  `git grep -n -i -E 'docker sbx|sbx (create|run)|Docker Sandboxes' sbx`.
- [ ] **`sbx/reference/Secure_use_guidelines.wt`** recommends `docker sbx`: it is a copy of
  a wiki page; refresh the copy (or the page) or drop it.

Development scaffolding:
- [ ] **Process references in code comments**: `lima-port/HANDOFF-LIMA.md`, phase
  numbers, D-numbers, N1–N3, and "(RAN ...)" in about 100 comments in 24 files
  (`git grep -c -E 'HANDOFF-LIMA|RAN\b|phase [0-9]' -- sbx/src sbx/bin sbx/helpers
  sbx/templates`). Keep the reason each one gives; drop the pointer to the process.
- [ ] **`lima-port/`**: the handoff documents, `TESTING-MACOS.md`, `acceptance/` and
  `checks/` are for this port. Decide what ships: move the checks that stay useful
  (`real-hardware.sh`, `cypress-nono.sh`, `credential-scan.py`) to `sbx/tests/` or
  `sbx/checks/`, the decisions that the code depends on to README.md or SECURITY.md, and
  drop the rest. Then fix every link to `lima-port/`.
- [ ] **Personal names and paths**: "cananian" (about 15 places in `sbx/`, not counting the
  tests), `~/Projects/Wikimedia`, cananian's host in the findings. Write "the engineer" and
  example paths.
- [ ] **This file**: trim it to what a maintainer needs (the decisions and findings that
  the code depends on); move the to-dos to Phabricator (#WMF-Claude).
- [ ] **`sbx/upstream/`**: drop what upstream took (the `ste-switch` topic and !130 are
  merged), file the rest, and remove the directory when it is empty.

### Testing and the MediaWiki setup

- [ ] **A cheap class of clone for CI-only dependencies** (shallow, no host remote, no
  reset): `create` now adds CI's dependencies of the named repos (below), which are full
  clones like the others.
- [ ] **Phan's `../../extensions/<Name>`, upstream**: the sandbox links `../../extensions` and
  `../../skins` beside each clone to core's (`setup.link_phan_paths`, where the name is
  free). The real fix is upstream: mediawiki-phan-config's `$IP` (`MW_INSTALL_PATH`, else
  `'../..'`, so CI is unchanged) in the ~170 Gerrit repos' `.phan/config.php`, ideally
  through a helper there. Not yet RAN on real hardware: Cite's phan clean with CI's phan
  dependencies and the links.
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
  nono pull` did). nono also POSTs an update check to `update.nono.sh`; not granted.
- `limactl` logs proxy URLs with their password: `lima.redact` removes them from LimaError.
- Inner sandboxes' repos go to `wmf-sbx-src` in the engineer's home path (state `nestedSrc`,
  e.g. `/home/cananian/wmf-sbx-src`; settings written by the install, if none), the one
  writable directory the session gives them. The outer VM has the clones at their host paths
  already, so this looks as it does on the host. Not under `/home/agent`: the inner VM has
  the clones at the same paths (D8), and that is its own agent's home (N3: create refused
  `~/wmf-sbx-src`). A sandbox installed before this keeps its old settings.
- nono's CA: `$SSL_CERT_FILE` is the system bundle plus nono's CA (151 certificates);
  `image.session_ca_file` keeps the ones the system bundle lacks (compared as DER), at a
  fixed path (`~/.cache/wmf-sbx/session-ca.pem`), so a resume in a later session refreshes
  it. Whether an inner VM reads the new file at its next start (Lima's cidata) is not RAN.
  An inner VM that runs across an outer session restart has the old proxy password and CA
  until it restarts.
- `exec` gives the agent's command `image.guest_proxy_env`: `sudo` drops the proxy that
  Lima writes to the inner VM's `/etc/environment`, and the inner VM has no other way out
  (N3: `mw-install-browser` got ENETUNREACH to googlechromelabs.github.io).
- `TMPDIR` must be the session's (`/tmp/claude-UID`, launch-claude.sh): `/tmp` is
  write-only under nono, and Lima builds its cloud-init ISO in TMPDIR.
- In a nested session a new pty is not a terminal; `test_real_pty_*` skips.

### Testing in the sandbox

- The manual-test skill (upstream) takes the base URL from a "Manual testing" section in
  CLAUDE.md: the sandbox's `~/.claude/CLAUDE.md` has one (`session.RUNNING_TESTS_MD`;
  docroot install, no `/w`). The skill's own `/w` wording: branch `manual-test-script-path`,
  for an upstream MR.

### Session, credentials, Claude Code

- [ ] **Check the "update installed, restart to update" message on the first resume** in a
  new sandbox. Its likely cause, `claude update` in `wmf-claude-setup` at create time (an
  agent-owned 2.1.296 beside the image's pinned version), is now skipped
  (`WMF_CLAUDE_SKIP_UPDATE=1`); close this if the message is gone.
- [ ] **Upstream: grant the Linux system paths the tools read**. Propose `read`
  entries for `/opt/claude-code`, `/etc/php`, `/etc/gitconfig`, `/etc/bash.bashrc`,
  `/opt/node`, `/etc/fonts` in `profiles/wmf-engineer.json` (today the lima-sbx launcher
  grants them per session).
- [ ] **Upstream: Gerrit MCP write-tool denies** (HANDOFF §12). Gerrit's MCP server has
  20 tools; the skills use 5 read-only ones; 15 write under the engineer's credential.
- [ ] **Upstream topic: Claude Code's hooks tell the agent to offer profile changes after any
  nono denial, even a harmless `ls ~`**.

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
  apply.

- [ ] **Broaden `WMF_CLAUDE_STE=0`** to the other places that ask for ASD-STE100: the
  `write-commit-msg` skill, the `mediawiki-dev` agent, and the `templates/CLAUDE.md` and
  `templates/mediawiki/CLAUDE.md` starters. Today it drops only the SessionStart bullet
  (enough for now, cananian 2026-10-11). Skills and agents are static text: they could say
  "unless the SessionStart context says not to", or the hook could add a line that overrides
  them.

### Upstream work and repo hygiene

- [ ] **Follow MRs !130, !131, !132**: the branch's base (`lima-port-base`)
  already carries their commits. Check their state with:
  ```bash
  P='https://gitlab.wikimedia.org/api/v4/projects/repos%2Fproduct-safety-and-integrity%2Fwmf-claude'
  for m in 130 131 132; do curl -s "$P/merge_requests/$m" \
    | jq -r '"!\(.iid) \(.state) \(.merge_commit_sha // .squash_commit_sha // "-") \(.title)"'; done
  ```
  When one merges, rebase onto upstream `main` and take upstream's version of
  its files (!132: `bin/session-start.sh`, `hooks/context/nono/`, the seam
  tests; !131: `bin/wmf-claude-setup`; !130: `skills/run-tests`). Keep
  `hooks/context/lima-sbx/`. A reviewer's change goes to the MR branch.
- [ ] **Shared-file edits must be upstreamable**. Outside `sbx/` and
  `lima-port/`, the branch now changes `bin/claude` (`--local-web=LO-HI` ranges),
  `hooks/context/lima-sbx/*`, `package.json`, `tests/test-templates.sh`, `SECURITY.md`,
  `.gitignore` (checked: `git diff --stat lima-port-base..HEAD`). Split them into
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
- [ ] **Seam 2 (optional, upstream): make `bin/wmf-claude-setup` fully non-interactive**. It still `read -rp`s the Phabricator username (line 208); `setup.sh`
  has a "Press Enter" gate. Proposed: `WMF_CLAUDE_PHAB_USER`, `WMF_CLAUDE_NONINTERACTIVE=1`
  that turns any prompt into an error. Lima feeds the answer through `config.json` today, so
  this is a nicety, not a blocker.

## Decisions and findings

### Project rules

- Do not fork `skills/` or `agents/`; fix the environment or propose an upstream change. Do not
  edit `profiles/wmf-engineer.json` from this project. Do not weaken
  `bin/wmf-claude-build`'s supply-chain checks (`npm ci --ignore-scripts`,
  `--require-hashes`, chrome-devtools-mcp one-package rule).
- Never commit `responses*.txt` (host transcripts, gitignored); distil what matters into NOTES.
- Python code: `sbx/src/wmf_sbx/`, on `sys.path` only (no pip packaging); thin wrappers in
  `sbx/bin/`; tests in `sbx/tests/`; `python3 -m wmf_sbx <cmd>` via `__main__.COMMANDS`.
  PyYAML is the only runtime dependency. Verb lists in `bin/wmf-sbx`, `COMMANDS` and
  `bin/wmf-sbx-<verb>` must agree (a test checks).
- `setup.py` must stay import-free: it runs in the VM as `python3 - --lima PLAN` from stdin, so
  a change to it needs no new image.

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
  After a build, `create` prunes unused images older than one hour (`PRUNE_MIN_AGE`: a
  concurrent `create` records its sandbox only after its build).
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
- In a session the proxy port needs `--landlock-only` (already on) and a per-session
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
- Still to run: macOS by someone with a Mac
  (`lima-port/TESTING-MACOS.md`, a guide for a human tester). The check scripts avoid
  bash 4 features and empty-array expansions under `set -u` (macOS has bash 3.2).
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
- A shallow host repo (`git clone --depth`): `git clone --shared` copies the objects and
  writes no alternates file; `sandbox-repos.sh` accepts that (RAN).

### nono, Landlock and the session

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
  the VM's nftables limits off-VM TCP to 80/443. Bookmark: revisit when external egress control
  arrives (track A). With it, the guest needs no DNS (the host proxy resolves names), so the
  same change closes contained mode's DNS residual (lookups through Lima's resolver can
  tunnel data). If the unification slips, the separate fix is a filtering DNS forwarder in
  the VM (dnsmasq or unbound, as root) that answers only for nono's allowlist (the profile's
  domains plus `session.REGISTRY_DOMAINS`): only nono's proxy needs DNS, because nft and
  Landlock keep the agent's other traffic off the network. DNS over HTTPS does not help:
  any recursive resolver forwards `<data>.evil.example` to its name server.
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
  ~$18); allow `Monitor`, `TaskOutput`, `TaskStop` with `--allowedTools`. Sonnet-class agents
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

