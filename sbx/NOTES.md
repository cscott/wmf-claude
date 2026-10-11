# wmf-sbx notes

Open to-do items, and the decisions and findings that the code depends on
or that future work needs. No history: `git log` has it. The design is in
`README.md` (Architecture) and `../lima-port/HANDOFF-LIMA.md` (decisions D1
to D12, and tracks A and B, which are not repeated here).

## To do

### Testing and the MediaWiki setup

- [ ] **Use WMF CI's dependency set, not only the manifests**.
  `deps.py` walks the `requires` field in `extension.json`/`skin.json`. CI (quibble) checks
  out `integration/config`'s `zuul/dependencies.yaml` (test jobs, honour each entry's
  `recurse`) and `zuul/phan_dependencies.yaml` (phan). For Translate that is 1 vs 5 vs 7 repos.
  Union the sets (the manifest walk still decides what the wiki loads). Clone CI-only
  dependencies as a new, cheap class of clone (shallow, no host remote, no reset). Decide
  whether to fetch the YAML over gitiles at create time, vendor a snapshot, or clone
  `integration/config`.
- [ ] **Make `.phan/config.php`'s `../../extensions/<Name>` paths resolve**. Symlinks into `core/extensions/` resolve `..` from the real directory, so the paths
  fail in the engineer's layout. Options: bind-mount each repo into `core/extensions/<Name>`
  (only option that matches CI), lowercase `extensions/`/`skins/` symlink dirs, real clones of
  CI dependencies under `core/extensions/`, or a sandbox-only phan config. When fixed, remove
  the "sibling" text from MEDIAWIKI-TESTING.md §3 and trap 4 of its §6, and session.py's
  "Running tests" paragraph.
- [ ] **Generate the testing section of `~/.claude/CLAUDE.md` per sandbox**: name each repo's real entry points (composer/npm scripts,
  Jest wiring, `tests/` dirs, `QUnitTestModule`, `tests/parser/*.txt`) and say which runners do
  not apply.
- [ ] **VisualEditor's git submodule is not cloned or initialised** in the sandbox clone. `sandbox-repos.sh` has no submodule step.
- [ ] **`--reset-all` semantics**. It must reset to the *upstream* branch
  (Gerrit/GitLab), never to the host's branch. Today, a repo with no known upstream keeps the
  host repo as `origin` and `safe-reset` goes there silently (`sandbox-repos.sh`, `clone_repo`). Refuse `--reset-all` (or warn per repo) when the upstream is unknown, and test it.
- [ ] **Re-check on real hardware what failed under TCG**: core's QUnit
  (`--qunit-component=MediaWiki`) does not complete (0 tests, ping timeout, or "no message in
  60000 ms"; karma also warns that "/" is proxied); the full core wdio suite
  (`npm run selenium-test`) did not finish; Cypress was not tested (the cloud proxy gave 502
  for `download.cypress.io`/`cdn.cypress.io`). `lima-port/checks/real-hardware.sh` runs
  these checks; not on Apple Silicon (no Chrome for Testing for Linux arm64).
- [ ] **Watch for the TCG segfaults on real hardware**: Claude Code (Bun),
  jsdoc, a Jest worker, and QEMU itself once at boot; each passed on retry. Maybe the same as
  an unexplained `limactl start` exit 1 after 24 s, seen once.
- [ ] **Optional: say `time env VAR=x cmd`** in MEDIAWIKI-TESTING.md's §6: the shell
  `time` keyword does not take a `VAR=x` prefix.

### Git transport and host remotes

- [ ] **`git safe-reset local` inside the VM falls through to the Gerrit check**. `git-review-check` trusts `local` only when `$SANDBOX_NAME`
  is set, which was a Docker Sandboxes variable. Lima sets nothing like it, and
  `wmf-sbx-ls-remotes` is not in the image. Give the VM a marker that the script can trust
  (an env var in the launcher/settings `env`, or a file the image owns) and update the
  comment at `bin/git-review-check` line 35.
- [ ] **Verify host `git maintenance` and the alternates**:
  that the host's background maintenance does not prune objects that a `--shared` clone in
  the VM borrows while gc is suspended.
- [ ] **Known gap: gc stays enabled for a repo that is cloned but gets no host remote**. Suspend gc for every repo whose objects a clone borrows.
- [ ] **A full `wmf-sbx ls`**: sandboxes, their clones, and unfetched-commit
  counts per clone; `wmf-sbx-ls-remotes` lists only the host-side remotes.
- [ ] **Sandbox-to-sandbox `wmf-sbx cp`**. `cp.py` still refuses it ("write
  exactly one of SRC and DST as NAME:PATH"). Copy through a host temp file (clean up
  on failure) and say so in the `+ resolved ...` trace.
- [ ] **Ambiguous path shortcut on a TTY: prompt**. When `.` matches more than
  one sandbox's `primaryDir`, prompt to choose instead of failing.

### GitLab

- [ ] **GitLab token plumbing**:
  host side only, keep the anonymous path working, and never write the token into the plan,
  the VM or its environment. Needed for private projects and for a real
  `mr_default_target_self` (anonymous callers see `None`).
- [ ] **Full GitLab project listing with an on-disk cache, plus recorded-response fixtures**.

### Session, credentials, Claude Code

- [ ] **Store the user's model preference persistently**.
- [ ] **Check the "update installed, restart to update" message on the first resume** (Still
  to do). `bin/launch-claude.sh` exports `DISABLE_AUTOUPDATER=1` , so it may be gone
  under Lima; confirm in a new sandbox, then close or fix.
- [ ] **Upstream: grant the Linux system paths the tools read**. Propose `read`
  entries for `/opt/claude-code`, `/etc/php`, `/etc/gitconfig`, `/etc/bash.bashrc`,
  `/opt/node`, `/etc/fonts` in `profiles/wmf-engineer.json` (today the lima-sbx launcher
  grants them per session).
- [ ] **Upstream: Gerrit MCP write-tool denies** (HANDOFF §12). Gerrit's MCP server has
  20 tools; the skills use 5 read-only ones; 15 write under the engineer's credential.
- [ ] **Upstream topic: Claude Code's hooks tell the agent to offer profile changes after any
  nono denial, even a harmless `ls ~`**.
- [ ] **Audit `capture_output=True` calls for `stdin=subprocess.DEVNULL`**. 8 of 11 lack it today (checked: `image.py` ×4, `remotes.py` 75 and 394
  — `git ls-remote` can prompt for a host key or credentials —, `repos.py` 44,
  `session.py` 312), and `lima.Limactl.call` captures output with `input=None`, so a
  `limactl` prompt would hang invisibly. Make the check routine (one grep).

### From the design documents

- [ ] **Wire up or drop the unused `repos.yaml` keys** `extra_dependencies`,
  `extra_environment`, `extra_packages`: `resolve.load_config` gives them defaults and
  `reference/example-repos.yaml` documents them, but no code reads them. `extra_packages`
  would have to go into `image.image_inputs` (the cache key).
- [ ] **Dependency walk, open questions:** a manifest `name` that is not the directory: an
  override table (`dependency_overrides`, today) or a fallback to the `name` field? Do
  `dev-requires` repos need another policy (`:ro` by default)?
- [ ] **A lost state file:** `wmf-sbx rm --scan DIR` to find and remove host remotes
  (`remote.<r>.wmfSbxSandbox`) of sandboxes that wmf-sbx no longer knows.
- [ ] **`state.new_state` still writes `daemonPort`/`hostPort`**, which nothing reads now.
- [ ] **Claude memory across sandboxes:** `~/.claude` (memory, projects) lives on the VM
  disk and goes with `rm`. Design: a host store (`global/` and `repos/<canonical>/`), seeded
  at create with `wmf-sbx cp`, extracted and merged at `rm`. Open: extract at `stop` too?
  How to merge two sandboxes' memories?
- [ ] **In-VM GitLab access with a token**, if wanted: needs a credential design (nono
  `--credential`, or the host proxy of track A); the old sbx secret-swap design does not
  apply.

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
  https and `git@gitlab-ssh.wikimedia.org` forms); then exact `repos.yaml` rules only — a
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
  cannot be a path are refused; `dependency_overrides:` in `repos.yaml` maps them by hand.
- Link names (canonical's last segment, or the manifest `name` outside extensions/skins, may
  contain spaces) are decided on the host and shipped in the plan JSON; setup never clobbers a
  real directory at a link destination.

### Clones, remotes, gc (host ↔ VM)

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
- Default VM memory is 8 GiB: phan on Translate was OOM-killed at 4 GiB.
- Timings under TCG are 5–35× slower than a 16-core host (image build 26–41 min, create
  ~250–310 s, setup for Translate ~40 min); only compare them with each other.

### nono, Landlock and the session

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
  arrives (track A).
- Browsers without sudo: the image has the Chrome/Cypress libraries, Xvfb and xauth;
  `/usr/bin/chromium`/`chromedriver` wrap links in `~/.cache/wmf-sbx-browser`; the wrapper
  adds `--no-sandbox`, a temporary `--user-data-dir` (headless Chrome will not start without
  one under nono) and `XDG_CONFIG_HOME` under the cache (crash handler). `/etc/fonts` is
  granted; helpers must not read the dpkg database.
- Credential: the host's `$ANTHROPIC_API_KEY`, `$CLAUDE_CODE_OAUTH_TOKEN` or
  `~/.config/wmf-sbx/claude-oauth-token` (0600) goes on stdin into a 0600 file in the guest's
  `/dev/shm`, which the launcher reads and deletes; never on a command line, never in the
  image.
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

