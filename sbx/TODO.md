# wmf-sbx: to do

The open items, by area. `DESIGN.md` gives the reasons behind the
current code. An item that names "the engineer" needs a person: a host
check, a decision, or an outward-facing step (filing a task, sending a
merge request).

## nono 0.80 on the host

- **Run the host's `nono proxy` on 0.80.** The guest pins nono 0.80; the
  host's egress proxy and auth proxy use the host's nono. Verified so
  far, in sandboxes: a `--sudo` session; a nono session (with the
  credential proxy and the clipboard through nono's proxy); a nested
  sandbox in both kinds of session. On the host with 0.80, check the
  capture-store problem below ("Watch and file upstream": nono).
- **Fewer `--open-port` flags.** `session.local_ports` gives
  `--local-web` the range 49152–53247, and `bin/claude` expands a range
  to one `--open-port` per port: 4098 flags, about 75 KB of `nono run`
  command line, in `ps` and in the status line's flag list. It does no
  harm and does not slow a session. nono 0.80 has a range only in a
  profile (`open_port_range`). Choices: ask nono for a CLI range
  (`--open-port LO-HI`) and let `bin/claude` pass it through; or let
  `bin/claude` write a per-session profile with `open_port_range` (first
  check whether `extends` can name a profile loaded by path). Either
  way, the `bin/claude` change is upstream's. Low priority.

## Firewall

- **The engineer's check of `ct direction reply`.** In a new sandbox:
  `sudo nft list chain inet wmf_sbx_hostblock block | grep 'direction
  reply'` prints a line, `limactl shell` works, and a session reaches
  the model and a Gerrit fetch. Verified so far: the rendered ruleset in
  network namespaces (old connections to private addresses, to a
  blocked port and to a deleted credential proxy rule are cut; SSH from
  the host survives).

## Claude Code memory and sessions

- **Carry memory and sessions with `lima/claude-context.py`.** It
  copies Claude Code's memory and sessions between host and VM, with
  name checks, a three-way merge for memory, and a prefix rule for
  sessions. wmf-sbx resumes inside the VM only, and `rm` loses
  `~/.claude`. Call it from `create` and `resume` (push) and from
  `sbx-pull` and `rm` (pull: stage only, as upstream does; never write
  the host's memory directly). The repos are at the host's paths, so
  only the home prefix differs and no path map is needed. Open: pull at
  `stop` too? How to merge the memories of two sandboxes?

## A contained mode

- **`create --contained`: design first.** A fenced sandbox with a
  smaller allowlist, compatible with upstream's contained mode (the model
  API only). The fence is on the host, so root in the guest cannot
  remove it, and fenced+sudo contained is possible too. Questions to
  settle before code:
  - The list: the model API through the auth proxy only, as upstream?
    Then the MCP servers and `git fetch origin` fail, as in upstream.
    `create` needs the full list (packages, `composer update`, `npm
    ci`): use it for `create` and the small list from the first
    session.
  - `wmf-sbx allow` on a contained sandbox: refuse it, or allow it and
    show the sandbox as no longer contained in `status` and `ls`.
  - The request body: without the checks below, the model API can fetch
    URLs for the agent, which the fence does not see. So `--contained`
    needs them, at least for contained sandboxes.
  - The record: chosen at `create`, kept in the host state, never
    inferred from the guest.

## Ask the engineer first

- **Request-body checks in `authproxy.py`.** It blocks only the
  `/v1/mcp_servers` paths. A `/v1/messages` body can still name MCP
  servers or server tools, and through them Anthropic's servers fetch
  URLs that the egress allowlist does not see. Upstream's model proxy
  refuses server tools, `mcp_servers`, `container` and URL content
  sources. Refusing server tools also stops Claude Code's WebSearch. A
  first step: refuse the `mcp_servers` field only, or make the full
  check a setting.
- **Headless Chromium for the chrome-devtools MCP.** Upstream runs
  Chromium as its own user with a per-uid nftables rule
  (`lima/guest-chrome.sh`). It would give Tier 2 manual testing, which
  `WMF_CLAUDE_SKIP_CHROME=1` turns off. Needs an image user and a rule
  in `sandbox-provision.sh`. Medium effort.

## MCP servers

- **Move the MCP servers out of the guest.** They run anonymous in the
  VM, because a server in the sandbox puts its credential in the
  sandbox. On the host, behind a gateway, a server can use the engineer's
  credentials safely. Plan it for both `--sudo` and nono sessions. This
  also decides in-VM GitLab access with a token (private projects,
  `mr_default_target_self`).
- **Gerrit MCP write-tool denies** (upstream). Gerrit's MCP server has
  20 tools; the skills use 5 read-only ones; 15 write with the
  engineer's credential. Propose denies for the 15.

## Testing and the MediaWiki setup

- **Generate the testing section of the guest's `~/.claude/CLAUDE.md`
  per sandbox**: name each repo's real entry points (composer and npm
  scripts, Jest wiring, `tests/` directories, `QUnitTestModule`,
  `tests/parser/*.txt`) and say which runners do not apply.
- **Phan's `../../extensions/<Name>`, upstream.** The sandbox makes the
  links (`setup.link_phan_paths`). The real fix is in
  mediawiki-phan-config: use `$IP` (`MW_INSTALL_PATH`, else `'../..'`,
  so CI does not change) in the `.phan/config.php` of about 170 repos,
  through a helper. mediawiki-phan-config may already have a helper
  that registers an extension dependency, unused so far: look first.
- **Watch for segfaults on real hardware.** Under TCG, Claude Code
  (Bun), jsdoc, a Jest worker and QEMU itself crashed once each and
  passed on retry; maybe also an unexplained `limactl start` exit 1
  after 24 s.
- **Run the nested acceptance test again**, when the upstream test
  failures below merge. Explicit sizes: outer `wmf-sbx create --no-start
  --sudo --nested --name egsn --memory 14GiB --disk 120GiB <wmf-claude
  checkout>`; inner `--memory 8GiB --disk 60GiB` with Translate, Cite and
  Parsoid. It also checks that Translate's phan (about 2.7 GB at its
  peak) passes in an 8 GiB inner VM. In `-p` mode, allow `Monitor`,
  `TaskOutput` and `TaskStop`, and tell the agent to keep a Monitor
  running while it waits.
- A cheap class of clone for CI-only dependencies (shallow, no host
  remote, no reset) is declined for now, in favour of better reuse of
  clones.

## Git transport and host remotes

- **Verify host `git maintenance` with the alternates**: that the host's
  background maintenance does not prune objects that a `--shared` clone
  borrows while gc is suspended.
- **A full `wmf-sbx ls`**: sandboxes, their clones, and unfetched-commit
  counts per clone.
- **sbx-pull**: completion for zsh and for macOS bash 3.2
  (bash-completion 2 needs bash 4); try the prompts (`git sbx-pull`,
  `create`, `rm`, `resolve`) on a real terminal; decide whether `rm`
  keeps a tip that was only fetched under a ref of its own (`rm` removes
  the remote, and gc can then prune the commit).

## GitLab

- **Token plumbing**, on the host only: keep the anonymous path, and
  never write the token into the plan, the VM or its environment. Needed
  for private projects and a real `mr_default_target_self` (anonymous
  callers see None).
- **A full project listing** with an on-disk cache, and recorded
  responses as test fixtures.

## Images and disks

- **The image key includes the HEAD commit.** Each wmf-claude commit
  gives a new image, so a cold create unless `WMF_SBX_GIT_HASH_OVERRIDE`
  is set. A key on a hash of only the files staged into the image would
  keep images across host-only commits, but adds a list of paths to
  keep correct. Deferred: the override is enough.

## macOS

- **The egress proxy on QEMU with HVF, and an equivalent of `restrict=on`
  on `vz`.** That would remove `--no-egress-proxy` and the guest's own
  network filtering. The host checks are in `MACOS.md`. Not verified:
  virtiofs on `vz` (read-only and fresh), one image on both drivers,
  overlays and the egress proxy on macOS.
- **Copy without the egress proxy** (for someone else). Without the
  proxy, the clipboard shim would connect to `192.168.5.2:PORT`, the
  host's loopback, which other local users can reach too. Moot if
  `--no-egress-proxy` goes.
- Unverified on any host: that the QEMU wrapper refuses `user-v2`
  networks, and the `cloud-init clean` flags on every Debian release.

## Session

- **Broaden `WMF_CLAUDE_STE=0`** to the other places that ask for
  ASD-STE100: the `write-commit-msg` skill, the `mediawiki-dev` agent,
  and the `templates/` starters. Skills and agents are static text:
  they could say "unless the SessionStart context says not to", or the
  hook could add an overriding line.
- **Upstream topic**: Claude Code's hooks tell the agent to offer a
  profile change after any nono denial, even a harmless `ls ~`.
- **Clipboard shim with socat**, only if socat comes into the image for
  another reason (paste, or no egress proxy). It gives a connect timeout
  and a clean half-close (`socat -u - TCP:ADDR,connect-timeout=1`). Not
  nc: its variants differ. Not worth a new package alone: a package
  changes the image key.
- **The dependency walk**: a manifest `name` that is not the directory:
  keep `dependency_overrides`, or fall back to the `name` field? Do
  `dev-requires` repos need another policy (`:ro` by default)?

## Upstream wmf-claude: merge requests

Check their state with:

```bash
P='https://gitlab.wikimedia.org/api/v4/projects/repos%2Fproduct-safety-and-integrity%2Fwmf-claude'
for m in 131 132 134; do curl -s "$P/merge_requests/$m" \
  | jq -r '"!\(.iid) \(.state) \(.merge_commit_sha // .squash_commit_sha // "-") \(.title)"'; done
```

When one merges, rebase onto upstream `main` and take upstream's
version of its files. Keep `hooks/context/lima-sbx/` and
`hooks/context/lima-sbx-sudo/`.

- **!131 (Phabricator username in `config.json`): withdraw.** Upstream's
  `WMF_CLAUDE_PHAB_USER` replaces it, and this branch does not carry
  it.
- **!132 (SessionStart backend seam): amend.** Move upstream's inline
  browser text to `hooks/context/nono/chrome.txt` and `chrome-vm.txt`,
  chosen by `WMF_CLAUDE_CHROME_IN_VM`, as this branch has it. Add the
  test environment fix below. Then watch its status.
- **!134 (manual-test skill's `/w` path): amend, then watch.** A docroot
  install (CI's, `composer serve`, a wmf-sbx sandbox) has its scripts at
  the root. Rebase it on current upstream `main`: upstream changed
  `skills/manual-test/SKILL.md` near lines 28 and 75 (its VM text).

## Upstream wmf-claude: commits to send

Each touches only shared files and stands without sbx: one small merge
request each, or a change to an open one.

- **`lima: Stop guest-install.sh when the Claude Code download
  fails`.** `lima/guest-install.sh` runs `curl … | bash`; without
  `pipefail`, a failed download gives an empty script, bash exits 0,
  and the install goes on with no Claude Code. `tests/test-lima.sh`
  does not catch it: its check matches a substring.
- **`tests: Keep the session-start and status line tests off the
  caller's env`.** The byte-for-byte fixture tests fail when the caller
  exports `WMF_CLAUDE_STE` or one of the VM variables
  (`WMF_CLAUDE_IN_VM`, `WMF_CLAUDE_CHROME_IN_VM`,
  `WMF_CLAUDE_MODEL_PROXY_PORT`). The session-start part goes into !132;
  the status line part (`WMF_CLAUDE_SANDBOX_BACKEND`) tests sbx code and
  stays here.
- **`lima: Fix the shellcheck findings in the VM scripts`.**
  `tests/test-lima.sh` fails where shellcheck 0.11 is installed (SC2016
  in `bin/claude-vm`, SC1091 in `lima/guest-install.sh`). CI has no
  shellcheck.
- **`--local-web=LO-HI` ranges in `bin/claude`.** Split the `bin/claude`
  and `tests/test-templates.sh` hunks into a commit of their own first.
  The case without sbx: karma, chromedriver and Cypress listen on ports
  that a fixed list cannot name.
- **Keep shared-file edits upstreamable.** Outside `sbx/`, this branch
  changes `bin/claude`, `bin/claude-vm`, `bin/session-start.sh`,
  `bin/statusline.sh`, `hooks/context/`, `lima/guest-install.sh`,
  `package.json`, `tests/test-templates.sh`, `tests/fixtures/`,
  `CLAUDE.md` and `SECURITY.md` (`git diff --stat origin/main -- .
  ':!sbx'`). Each needs a self-contained commit that a reviewer can
  accept without sbx. The skill-list fix is upstream as !132: take its
  latest version when the commits are squashed.

## Upstream wmf-claude: tasks to file

- **Read paths in the profile.** Propose `read` entries for
  `/opt/claude-code`, `/etc/gitconfig`, `/etc/bash.bashrc`, `/opt/node`
  and `/etc/fonts` in `profiles/wmf-engineer.json`. The lima-sbx launcher
  grants them per session.
- **`tests/test-profile.sh` fails on Linux**: nono refuses the profile
  ("Landlock deny-overlap is not enforceable on Linux"), because the
  `~/.cargo/credentials(.toml)` deny is under `group:rust_runtime`'s
  `~/.cargo` allow. File on #WMF-Claude if it is not filed.
- **Make `bin/wmf-claude-setup` fully non-interactive** (optional):
  `setup.sh`'s "Press Enter" gate, and a `WMF_CLAUDE_NONINTERACTIVE=1`
  that turns any prompt into an error.
- **The "Linux glob caveat" may be stale.** `docs/security-rationale.md`
  says Claude Code ignores glob patterns in `Read` and `Edit` rules on
  Linux, and `bin/wmf-claude-setup` warns about it on every Linux
  install. On Claude Code 2.1.269 on Linux, `Read(**/*.pem)` and
  `Read(**/*.env)` did deny a Read of a nested `.pem` and of
  `sample.env`. Upstream uses the `//**/` form, which was not
  measured. Re-measure on the current Claude Code with the shipped
  `wiring/settings-merge.json` before filing: does the startup warning
  still print, and are a Read and an Edit of a nested `.pem` and `.env`
  denied? Fix: state the versions as measured, and print the warning
  only if the measurement supports it.

## Other projects: tasks to file

- **MediaWiki-REST-API: header parameters are looked up by exact case.**
  `ParamValidatorCallbacks` finds a `'source' => 'header'` parameter
  with `$params[$name]` on `RequestInterface::getHeaders()`, keyed by the
  name as the client sent it. Header names are case-insensitive (RFC 9110
  §5.1), so `content-type: application/json` to `POST /page` or `PUT
  /page/{title}` gets 400 (`The "Content-Type" parameter must be set.`).
  It shows under `php -S` (`composer serve`, Quickstart), which keeps
  the client's case; the api-testing client sends lowercase, so core's
  `tests/api-testing/REST/Creation.js` and `Update.js` fail 15 of 27.
  It started with core 0ca91a896ad (T412668). Fix: in `hasParam()` and
  `getValue()`, for the header source, use `$this->request->hasHeader(
  $name )` and `getHeader( $name )` (which `HeaderContainer` already
  makes case-insensitive), with `implode( ', ', … )` for the string
  type; add a `ValidatorTest` case with the header sent as `PARAM1` and
  read as `param1`. Verified: `tests/phpunit/unit/includes/Rest/`
  passes (674), the new case fails without the fix, and the two suites
  go to 27 passing.
- **wdio-mediawiki: an empty `MW_SCRIPT_PATH` is rejected.**
  `wdio-defaults.conf.js` throws `MW_SERVER or MW_SCRIPT_PATH not
  defined` for `MW_SCRIPT_PATH=`, which is correct for a docroot install
  (core's `Gruntfile.js` accepts it for that reason). Workaround:
  `MW_SCRIPT_PATH=/`. Fix, in core's `tests/selenium/wdio-mediawiki/`
  (verified: core's `tests/selenium/specs/page.js` passes with
  `MW_SCRIPT_PATH=`):
  ```diff
  -if ( !process.env.MW_SERVER || !process.env.MW_SCRIPT_PATH ) {
  +if ( !process.env.MW_SERVER || process.env.MW_SCRIPT_PATH === undefined ) {
  ```
- **Popups: `npm run test:unit` runs no tests on Node 21 and later, and
  exits 0.** `@wikimedia/mw-node-qunit` 7.0.0 does `global.navigator =
  global.window.navigator`; Node 21+ has a read-only `navigator`, so the
  setup throws, no test runs, and the pipe into `tap-mocha-reporter`
  exits 0. Popups' own `.nvmrc` asks for Node 24. With
  `NODE_OPTIONS=--no-experimental-global-navigator`, 203 tests pass.
  Fix: bump the pin in `package.json` to `7.10.0` (it uses
  `Object.defineProperty`) and regenerate `package-lock.json`; then fix
  `ext.popups/settingsDialogRenderer > #render`
  (`tests/node-qunit/ui/settingsDialogRenderer.test.js:70` expects
  `display: none` after `hide()` and gets `block`). `set -o pipefail` in
  the script also makes a crash before any output fail.
- **nono: the OAuth capture store is read once and written whole**
  (always-further/nono). Check on 0.80 first. Several `nono proxy`
  processes that share one `oauth-capture/providers.json` each read it
  once (`OAuthCaptureStore::load_with_persistence`) and resolve phantoms
  from memory; each capture writes the whole in-memory map back
  (`persist_tokens`). So a phantom that proxy A mints is unknown to an
  older proxy B (Claude Code gets 401, then a refresh 400); B's next
  capture deletes A's newer phantoms from the file; and two writers race
  on `providers.json.tmp`. Use case: one `nono proxy` per sandbox and one
  shared login. Proposal: on a phantom miss, read the file again if its
  mtime changed, and merge; in `persist_tokens`, read and merge before
  the write, under `flock`, with a unique temporary name. When nono
  fixes it, remove `authproxy.refresh_proxy`.
- **Propose to Lima**: a native `restrict`/egress-proxy option for
  QEMU, to drop the wrapper; a shared-base-disk (overlay) option.
- **Propose to nono**: backdate the leaf certificates' `notBefore`
  (mitmproxy does), for the up to 100 ms that the guest clock can be
  behind; a live allowlist change for `nono proxy` (`wmf-sbx allow`
  replaces the proxy); credentials for `--upstream-proxy`
  (`egress.AuthShim` adds them).

## Filed: watch status

- **Claude Code: a setting that names the copy command**
  (https://github.com/anthropics/claude-code/issues/102454). When it
  lands, it replaces the fake `wl-copy`.
- **mediawiki/core, four test failures** that a sandbox shows. When each
  merges, take its entry out of `templates/MEDIAWIKI-TESTING.md` (§0,
  §2, §6 item 7), then run the acceptance test again:
  - Gerrit 1355223: `SchemaGenerator::normalizePath()` leaves the path
    absolute for a symlinked extension; 11 AbuseFilter schema tests fail.
  - Gerrit 1355226 and 1355227 (`$wgUsePathInfo` in
    DevelopmentSettings): api-testing `ReparseDependentPages.js` expects
    the `?title=` red-link URL.
  - Gerrit 1355229: `UndoIntegrationTest::testMcrUndoAction` has no
    title in its context; EventLogging's hook dereferences it.
  - Gerrit 1355230: `WikiBirthdayTest` does not clear the
    `GetHumanTimestamp` hook, which cldr handles.
- **VTE OSC 52** (https://gitlab.gnome.org/GNOME/vte/-/issues/2495). If
  VTE implements it, GNOME Terminal copies with no shim.
