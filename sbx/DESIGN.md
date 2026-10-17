# wmf-sbx: design

The reasons the code depends on. `README.md` tells how to use wmf-sbx,
and `SECURITY.md` tells what a sandbox guards. This file tells why the
code is as it is. Where a fact comes from a measurement, it says
"verified" and what was checked. `HISTORY.md` has the paths that were
abandoned.

## Project rules

- Do not fork `skills/` or `agents/`. Do not edit
  `profiles/wmf-engineer.json`: it is upstream's. A session adds grants
  with launcher flags (see "The session").
- Do not weaken the supply-chain checks in `bin/wmf-claude-build`:
  `npm ci --ignore-scripts`, `pip --require-hashes`, and the one-package
  rule for chrome-devtools-mcp.
- Never commit a host transcript, a session export, a credential, or
  `$GERRITUA`.
- Variable prefixes. `WMF_SBX_*` is host configuration that wmf-sbx
  reads: `WMF_SBX_CA_CERTS`, `WMF_SBX_GIT_HASH_OVERRIDE`,
  `WMF_SBX_EGRESS_CONFIG`, `WMF_SBX_QEMU_PLAIN`, and two for
  measurements: `WMF_SBX_EGRESS_PROFILE` (a profile that replaces the
  egress proxy's, a security change) and `WMF_SBX_AUTH_MARGIN_MINUTES`
  (the auth proxy's refresh margin). `WMF_CLAUDE_*` is what
  wmf-claude reads in the sandbox: `WMF_CLAUDE_SANDBOX_BACKEND`,
  `WMF_CLAUDE_NO_PAUSE`, `WMF_CLAUDE_SBX_EGRESS`, `WMF_CLAUDE_PHAB_USER`.
  nono passes only the profile's `allow_vars` to the session, and a test
  checks `launcher_argv` against that list.
- Python layout. The package is `sbx/src/wmf_sbx/`. It goes on
  `sys.path` only; nothing installs it. Each `sbx/bin/wmf-sbx-<verb>` is
  a thin shim. `python3 -m wmf_sbx` dispatches through
  `__main__.COMMANDS`. PyYAML is the only dependency. The verb lists in
  `bin/wmf-sbx`, `COMMANDS` and `bin/wmf-sbx-<verb>` agree, and a test
  checks this.
- `git-safe-reset` and `git-review-check` are useful on the host too.
  Guard each sandbox-only behaviour on `$SANDBOX_NAME`.
- Test isolation. Each test module calls `isolation.isolate()` in
  `setUpModule`: an empty `HOME` and XDG directories, no credentials, no
  `WMF_SBX_*` variables, no nested marker, and `claude` and `security`
  stubs that fail. A test failure must not show a real credential. A
  module attribute that a test patches is read at call time, not bound
  at import.
- `setup.py` imports nothing from the package. It runs in the guest as
  `python3 - PLAN`, from stdin. So a change to it needs no new
  image.

## Architecture

Each sandbox is one Lima instance. Its disk is a copy-on-write overlay on
a cached golden image. The driver is QEMU on Linux; on macOS it can be
`vz`. The guest runs the same `bin/claude`, the same profile and the
same tool layer as upstream's plain nono session.

The guest has two users:

- `engineer` (uid 59998), with sudo. Lima's own user. wmf-sbx runs its
  setup steps as this user.
- `agent`, with the host's uid and gid (so the read-only mounts and the
  host's file owners agree). Claude Code runs as this user.

The Lima template keeps upstream's rules: no port forwards (the
ignore-all rule), no SSH agent and no X11 forwarding, no Rosetta, a
versioned base image with a digest, the nftables host block, TIOCSTI
off, and the engineer never runs code from a path the agent can write.
The template sets `plain: false`, because plain mode turns off mounts,
and then turns off `containerd` and the SSH port forwards by hand.

Every verb first checks that the instance is wmf-sbx's own: a state
file in `~/.local/state/wmf-sbx/sandboxes/` names it.

**Modes.** A sandbox is **fenced** when the host's egress proxy stands
between the VM and the internet, and **unfenced** when it does not. The
agent's side is **+nono** or **+sudo**:

| Mode | How | The agent's side |
| --- | --- | --- |
| fenced+nono | the default | nono, as upstream |
| fenced+sudo | `--sudo` | no nono; passwordless sudo |
| unfenced+nono | `vz`, `--no-egress-proxy` | nono; the guest's nftables |

Upstream's isolated mode is approximately unfenced+nono with the token
in the VM. Upstream's contained mode allows the model API only. The
flags name mechanisms (`--egress-proxy`, `--no-egress-proxy`, `--sudo`).
Do not add `--isolated` as a synonym for `--no-egress-proxy`: that flag
only removes the fence.

**Where wmf-sbx and upstream's VM mode overlap, keep ours:**

- Do not set `WMF_CLAUDE_IN_VM` in a session. With it, upstream's
  `session-start.sh` advises `php -S 127.0.0.1:8080` and MariaDB (we
  use `composer serve` and SQLite), and the status line says "isolated
  VM". wmf-sbx sets it only for the `wmf-claude-setup` call, where it
  stops the username prompt and the `claude-vm` alias.
- The credential proxy and the auth proxy, not upstream's model proxy.
  Both keep the token on the host.
- `wmfsbx://` remotes, not bundles. In both, the host chooses the names.
- The golden image (php-ast as CI builds it, a SQLite wiki), not
  upstream's PHP and MariaDB in the guest.
- Our `ls`, `status` and other verbs are a CLI parallel to upstream's
  `list` and `--vm`.

**Settings defaults.** Each option `--some-flag` of a verb has the
settings key `VERB.someFlag` (the camelCase argparse dest; subcommands
nest, as `image.build.keepBuilder`). The command line wins over the
settings, and the settings win over the code default. `null` means
"choose for me". Every flag is a `BooleanOptionalAction` pair, never
`store_true`, so that the command line can undo a setting; a test walks
`COMMANDS` to check this. An unknown key gives a warning; a wrong type
is an error. `--no-settings`, or `session.settings: false`, turns off the
option defaults only: `repos:` and the other sections are still read. A
value in `settings.yaml` is an explicit request, so wmf-sbx applies no
floor to it. The sudo default is `create.sudo`, because `--sudo` is a
`create` flag. There are no compatibility keys. The helper is
`cli.parse_args(parser, argv, section)`: one parse with
`argparse.SUPPRESS` defaults, then `set_defaults` from the settings; the
help shows ` [settings: VERB.someFlag]`.

The settings file is `~/.config/wmf-sbx/settings.yaml`. Its sections
are `repos`, `session`, `environment`, `packages`,
`dependency_overrides`, `link_overrides`, `extra_dependencies`,
`imageCache`, and one section per verb. `reference/example-settings.yaml`
shows each one.

## The golden image

A builder VM makes the golden image from the digest-pinned Debian
genericcloud image. The builder sets `user.uid` 59999; without it, Lima
gives the builder's user the host uid. wmf-sbx never makes an image from
a sandbox that ran an agent (CVE-2023-32684: a guest can change its own
disk).

The image contains:

- `linux-image-amd64`. The cloud kernel has no 9p or virtiofs modules,
  and the build checks that the modules load.
- The CAs in `WMF_SBX_CA_CERTS`, through Lima's `caCerts`. The seal
  removes them again. `sudo` drops the proxy variables, so the build
  passes them explicitly, and a loopback proxy URL becomes
  `192.168.5.2`.
- php-ast at CI's pin (`image.PHP_AST`), Node 24 from nodejs.org,
  rsync, the libraries that Chrome and Cypress need, and Xvfb.
- The guest helper `git-sbx-pull`, in `/usr/local/bin`.
- apt-listchanges and apt's daily timers, masked.

The seal removes the machine's identity: `rm -f /etc/ssh/ssh_host_*`,
then `cloud-init clean --logs --seed --machine-id`.

The export is atomic. The build requires the submodules:
`git submodule foreach` skips a missing one with no error.

The image is a qcow2 file with no backing file. It is read-only, it is
never rewritten, and its absolute path never changes, because each
overlay names it by that path.

**The image key** is the HEAD commit (or `WMF_SBX_GIT_HASH_OVERRIDE`;
the value `latest` takes `image.newest_image`, which `login` also uses),
the nono version, `packages`, the uid and gid, and the epoch, which
`imageCache.maxAge` (default 14 days) sets.

**Garbage collection.** `image gc` removes images that are not in use
and older than `imageCache.gcAge` (default 7 days); `create` runs it at
the end. `image prune` removes every image not in use. An image is in
use when a state file names it, or when it is in the backing chain of
an instance disk. wmf-sbx reads the qcow2 headers itself, without
`qemu-img`.

**Locks.** The build lock is `images/.build-KEY.lock`. When it is
held, `busy_prompt` asks: wait (the default), delete the lock, or stop.
With no terminal, it stops. The create lock is
`sandboxes/.NAME.creating` (`state.reserve`).

cloud.debian.org redirects to geographic mirrors, so a nested sandbox
uses a local copy of the base image (see "Nested sandboxes").

## Repositories and git transport

**Mounts.** wmf-sbx mounts only the git directory of each repo
(`git rev-parse --git-common-dir`), read-only, at
`/run/wmf-sbx/host/<host path>/.git`. On QEMU the mount is 9p with
`cache: none`: with fscache, the guest saw stale refs after a
`pack-refs`. Lima writes fstab at boot, so a `limactl edit` of the
mounts takes effect one boot later. On `vz` the mount is virtiofs.
Never use reverse-sshfs.

The host enforces read-only. Verified: guest root ran
`mount -o remount,rw` and then a write, and got "Read-only file system";
the host file did not change. Test this by an attempt to go around it,
not by `touch` or `/proc/mounts`.

What is left: the agent can read `.git/config`, the hooks, the stashes
and the reflogs of each mounted repo.

The mount set is fixed at create. `Limactl.call` gives `limactl` a
`/dev/null` stdin.

**Clones.** Each repo is a `git clone --shared` from its mount (not
`--reference`, which copied 685 MB). The clone is at the host's absolute
path: Claude Code's project key, its memory directory and the paths in
generated files depend on it, and the agent cannot `/cd`. A path under
`/tmp`, `/run` or `/home/agent` is refused.

In the clone, `origin` is the upstream and `local` is the mount.
`checkout.defaultRemote` is set to `origin`. A repo the engineer named
keeps the host's branch (or detached commit). A dependency is reset with
`git safe-reset --force`. `--reset-all` resets every repo to `origin`
(or, if that fails, the host's master or main, with a warning).

Submodules come from the host's `modules/NAME`, else from `.gitmodules`
(`submodule init` and `sync`, then the update again).

`safe.directory` is in `/etc/gitconfig`. A `:ro` repo is cloned by
`engineer`; then `upload-pack` needs `PATH/.git` to be safe too. A
shallow host repo is accepted: a shared clone needs no alternates file
for it.

**Host gc.** A shared clone needs every object in the host repo. So
wmf-sbx records each borrower in the host repo
(`wmfSbx.borrower`, multi-valued) and suspends gc there (`suspend_gc`:
`gc.auto=0` and `gc.pruneExpire=never`, in `--local` config; the old
values are kept, with an `(unset)` sentinel, and `wmfSbx.gcSuspended`
marks it). `resume_gc` restores them when no borrower and no marked
remote is left. wmf-sbx never touches a remote that a person added
without the marker.

**VM to host.** The host fetches from a sandbox with a
`wmfsbx://NAME/PATH` remote, through `git-remote-wmfsbx`. The helper
supports `connect` for `upload-pack` only and refuses `receive-pack`.
It runs `limactl shell --workdir / NAME -- sudo -H -u agent git -c
core.hooksPath=/dev/null -c core.fsmonitor=false upload-pack PATH`, with
protocol v0. git ignores `uploadpack.packObjectsHook` from repo config
(verified). The guest side is hostile: keep the host's git current.

`unfetched_tips` leaves out `refs/remotes` and remote-tracking tips.
`vm.uncommitted` runs `git status --porcelain` as the agent (no optional
locks, hooks or fsmonitor) and strips control characters; `rm` and the
end of a session use it. `remotes.unfetched_refs` lists the branches
the host does not have, each with its `git -C HOST_DIR sbx-pull
SANDBOX/BRANCH`.

**In the guest.** `git safe-reset` takes `R`, `R master` and
`R/master`. `git-review-check` answers for a sandbox remote and for
`local` without the network, and trusts a remote only if
`wmf-sbx-ls-remotes` registered it. `SANDBOX_NAME`
(`session.SANDBOX_NAME_VAR`; settings cannot change it) turns on the
`local` handling.

Path shortcuts resolve against `primaryDir`, and `cp` anchors a relative
guest path there.

## The commit flow (sbx-pull)

git-review follows the branch's upstream only when `track=1` (core, Cite
and Parsoid set it, for release branches). So a branch that tracks the
sandbox remote breaks `git review`. Upstreams therefore stay on Gerrit,
and the host records the sandbox link in its own branch config:
`branch.B.wmfsbx`, `branch.B.wmfsbxBranch`, `branch.B.wmfsbxSha`.

**On the host: `git sbx-pull [--onto X] [-f] [[NAME/]BRANCH]`.**

- Which branch: `NAME/BRANCH`; or a `BRANCH` whose host record names the
  sandbox; or else the only new branch over all sandboxes (not named as
  an origin branch, and not a hand-off tip).
- Gates: a dirty tree stops it. A `git-review-check` failure stops it,
  unless `-f`.
- The target: the recorded branch, else a branch of the same name, else
  a new one. `reset --keep` moves it when nothing is lost: the old tip
  is in the new tip, or is `wmfsbxSha`, or is on Gerrit, or the agent
  fetched it (the seen ref). Otherwise it stops, unless `-f`; the old
  tip stays in the reflog, and sbx-pull prints it.
- The new upstream, first match: the sandbox branch's `origin/X`;
  `local/Y`, which gives Y's host upstream; the upstream of the host
  branch the work sits on; a guess (the origin branch with the fewest
  commits to the tip, master on a tie, said aloud); `--onto` (which also
  re-points the upstream of an existing branch). sbx-pull never passes
  on an upstream on a sandbox remote.
- The Gerrit check is in Python (`gerrit_has`).

**In the guest: `git sbx-pull`** (`sbx/helpers/git-sbx-pull`, stdlib
only, at `/usr/local/bin`).

- Which branch: the argument; else the host's current branch (`git
  ls-remote --symref local HEAD`) if it is not named as an origin
  branch; else, if host master has commits that origin does not, it
  stops and asks for `--as NAME`.
- Gates: a clean tree, and the commits are on the host (the host's
  `refs/remotes/$SANDBOX_NAME/*`, seen as `refs/sbx/host-has/*`) or on
  Gerrit.
- Then `checkout -B B local/B`, and the seen ref `refs/sbx/seen/B`,
  which the host fetches into `refs/remotes/NAME-seen/`.
- The guest side never asks a question.

`git-review-check` treats a HEAD in `refs/remotes/NAME/*` or
`NAME-seen/*` as safe. `git safe-reset NAME/BRANCH` runs sbx-pull.

The user skill `/sbx-pull` comes from `sbx/templates/skills/sbx-pull/`
and goes to `~/.claude/skills/sbx-pull/SKILL.md` in the guest. It keeps
the agent on the hand-off branch, reads `git range-diff OLD...NEW`, and
uses `--as` only for a hand-off on master. There is no push command:
the hand-off is "type /sbx-pull".

`rm` runs `remotes.forget_pulls`. `git remote remove` deletes
`refs/remotes/NAME/*` and the upstreams that point there.

**Completion.** `sbx/completion/git-sbx-pull` defines `_git_sbx_pull`.
bash-completion loads it lazily from `$BASH_COMPLETION_USER_DIR` or
`$XDG_DATA_HOME/bash-completion`, so no `.bashrc` edit is needed. It
fetches the sandbox remotes in parallel, in a subshell, quietly, at most
once a minute (a stamp in `.git/wmf-sbx-completion/`), with no timeout
(macOS has no `timeout`). The host side leaves out branches named as
origin branches.

**Prompts.** `prompt.Prompt.pick` and `confirm` check that both stdin
and stderr are terminals; with no terminal, `pick` gives None and
`confirm` gives False. A destructive question defaults to no. `rm`'s
"Fetch them now?" defaults to yes. The busy image lock defaults to wait.
The questions "which sandbox", "which project" and "which new branch"
have no default. `create`'s clone question defaults to no. `pick`
accepts a prefix of a label. `rm` with no `-f` and no terminal refuses at once, before
its checks start the VM.

## Repo resolution and the dependency walk

**CI's dependencies.** `ci_deps.py` reads integration/config's
`zuul/dependencies.yaml` (closed over, with `recurse:`) and
`phan_dependencies.yaml` (direct only). It fetches both from gitiles once
per run, with `$GERRITUA`. Only the roots that the engineer named are
looked up: a lookup at every node took one BlueSpice extension from 3
repos to 84. `--no-phan-deps` and `--no-ci-deps` turn them off; when
gitiles is unreachable, a warning. Without them, a Parsoid sandbox has
no ParserFunctions.

**Manifests.** The walk reads each repo's `extension.json` or
`skin.json`: the local checkout if there is one, else gitiles. A 429
with `retry-after` 60 gets one retry. An unreachable manifest stops the
create. Tests stub `fetch_manifest`. A manifest's `requires` name is its
credits name, which differs from the repo name for 44 of 298 repos;
`dependency_overrides` maps the rest. The host decides the link names;
setup never replaces a real directory with a link.

**Names.**

- Every host path goes through `realpath` (host commands do not).
- An argument that is a raw path skips resolution.
- `find_nested_mount_conflict` refuses a repo inside another mounted
  repo.
- The default sandbox name is `sbx-<slug>`; on a clash it offers
  `NAME-2`. The host remote has the sandbox's name.
- Gerrit REST: strip the `)]}'` XSSI line. `?m=` matches a substring,
  `?p=` a prefix, `?d` adds descriptions. `[ARCHIVED]` in the
  description gives a warning, and a non-archived match wins.
- A rule's directory is trusted only when its `.gitreview` `project=`
  matches.
- `reverse_resolve` tries `.gitreview`, then the origin URL (GitLab
  https or ssh), then the exact rules.
- GitLab: the groups `repos`, then `toolforge-repos`;
  `forked_from_project` to depth 5. Anonymous API limits apply:
  `/search?scope=projects` gives 401, and `simple=true` drops fields.
- `host_upstream_url` accepts http(s) URLs only.

## The MediaWiki setup

| Step | Runs as | What |
| --- | --- | --- |
| image build | root | packages, Node, php-ast, helpers |
| create | engineer (sudo) | sudoers, `safe.directory`, mounts |
| create | agent | clones, links, `composer.local.json`, `composer update`, `npm ci`, PHPUnit config, `.env`, install, api-testing config, settings merge, CLAUDE.md |

`--with-extensions` adds extensions to the wiki. The install parameters
come from core's `mw-install:sqlite` script (`parse_install_params`).
`MW_SCRIPT_PATH` is `/`, as in CI. composer.lock is not tracked and
package-lock.json is, so setup runs `npm ci`.

**Failure policy.** A per-repo step gives a warning. `.env`, `npm ci` and
the install are fatal. A failed setup keeps the VM, so the engineer can
look. Each step is idempotent.

- `composer.local.json` is merged as quibble does: tab-indented, links
  first, and the engineer's own entries left as they are. With a Parsoid
  clone, `vendor/wikimedia/parsoid/` is excluded from the classmap.
- The Parsoid block in LocalSettings: a literal path, put above
  `// Enabled skins.`, in place of the `wfLoadExtension`.
  `MEDIAWIKI_HAS_INTEGRATION_TESTS=1` makes core's unit tests use it.
- The api-testing config: `base_uri` with a trailing slash, tabs, and an
  absolute `API_TESTING_CONFIG_FILE`.
- One PHPUnit run at a time per core.
- `composer serve`: poll for 30 s, and check first for a server already
  on port 4000.
- Only core and the repos the engineer named get `composer update` and
  `npm ci`; dependency repos do not, and an untracked `vendor/` left by
  a snapshot is removed. Otherwise phan reports `PhanRedefined*`, and
  EventLogging loads its json-schema copy over core's.
- No composer cache in the image: it saves seconds only.
  `COMPOSER_PROCESS_TIMEOUT=0`. MEDIAWIKI-TESTING.md gives the
  `preferred-install source` tip.
- `setup.link_phan_paths` makes the `../../extensions` links that phan
  configs expect.
- `exec` takes `sessionEnv` from the host state, not from the guest's
  `settings.json`: the agent can write that file, and `exec` runs
  outside nono.
- Shell scripts: `pipefail` with `grep -q` gives a false failure (SIGPIPE);
  progress goes to stderr.

**Browsers.** `CHROME_BIN`, `CI=true`, `MEDIAWIKI_USER` and
`MEDIAWIKI_PASSWORD` are set. Cypress needs download.cypress.io and
cdn.cypress.io; `cypress install` exits 0 when the download is refused.
`CYPRESS_CACHE_FOLDER` is set; `CYPRESS_INSTALL_BINARY=0` skips it during
`npm ci`. Run a spec with `cypress:worker --spec` (the parallel-weights
runner needs the full set). Popups' Cypress tests need TextExtracts and
PageImages. Verified on real hardware: every browser suite passes. The
failures seen under TCG (QEMU without KVM, 5 to 35 times slower) were
timeouts.

**Known upstream failures**, which MEDIAWIKI-TESTING.md lists: the REST
header case, Flow's mocha tests, and Popups on Node 21 or later
(`NODE_OPTIONS=--no-experimental-global-navigator`); and the sibling
gap (a repo's tests that need a repo the sandbox does not have).

**Unattended runs.** In `claude -p`, a Monitor event is a full turn, and
the run ends at the first turn with no Monitor running; a background
Bash job does not keep it open. Allow `Monitor`, `TaskOutput` and
`TaskStop`.

## The session

The profile grants no read of the home directory, other than the
dot-directories. A session grants `~/MEDIAWIKI-TESTING.md` and
`~/.wmf-sbx` by name (`--read-file`, `--read`). `~/.bashrc` is removed:
the profile denies it, and bash would complain at each shell.

- `~/.cache` and `~/.npm` are granted. `COMPOSER_HOME` is
  `~/.cache/composer-home`: a deny under an allow makes nono refuse to
  start, so it cannot be a deny in `~/.config`.
- `/tmp` is write-only under the profile: use `$TMPDIR`.
- Launcher grants: `--read` `/opt/claude-code` and `/opt/node`;
  `--read-file` `/etc/gitconfig` and `/etc/bash.bashrc`; and
  `--allow-domain` for the package registries in
  `session.REGISTRY_DOMAINS`. These are flags, not a new profile:
  `extends` cannot name a profile loaded by path.
- Environment variables go in the `env` of the agent's
  `~/.claude/settings.json`, which nono's `allow_vars` does not filter.
- Cypress grants: `~/.config/Cypress` and `/var/lib/xkb`. nono grants
  only paths that exist, so `image-build.sh` makes `/var/lib/xkb`.
- Browser wrappers in `~/.cache/wmf-sbx-browser` add `--no-sandbox`, a
  temporary user-data-dir and `XDG_CONFIG_HOME`. `/etc/fonts` is
  granted. The helpers do not read dpkg.
- `ENABLE_TOOL_SEARCH=true`.
- `--continue` is added only after a session that was attached
  (`starts_a_conversation`; a `-p` run does not count).
- The git identity goes in `GIT_AUTHOR_*` and `GIT_COMMITTER_*`: from
  the same variables in the host's environment, else from `git config`
  (as git does). A nested sandbox has only the variables.
- The guest's `~/.claude/CLAUDE.md` (`session.home_claude_md`) has the
  commit rule and `RUNNING_TESTS_MD` (a docroot install, no `/w`).
- SessionStart text: `hooks/context/lima-sbx/` and `lima-sbx-sudo/`.
- Terminal variables (`TERM_PROGRAM`, `TERM_PROGRAM_VERSION`,
  `COLORTERM`) come from the host (`session.terminal_env`).
- `resume.project_dir` uses `lima/claude-context.py enc`, so a long path
  gives the same directory name as Claude Code.
- `session.wmf_claude_setup` passes `WMF_CLAUDE_PHAB_USER`.
- `WMF_CLAUDE_SKIP_UPDATE=1`: the image pins the Claude Code version.
- `sudo`'s `secure_path` applies to the setup commands.
- Repo code that `create` runs (composer, npm, the installer) runs
  outside nono only before the first session. After the state records
  `attached`, wmf-sbx never runs repo code outside nono.

**A `--sudo` session** runs `bin/launch-claude.sh` without nono. It sets
the proxy variables itself (`sudo -u` drops `/etc/environment`), and
`NODE_EXTRA_CA_CERTS` is the system bundle.

**MCP servers** run in the VM, anonymous, added with `claude mcp add
--scope user` (a plugin `.mcp.json` would rename the tools).
mcp-phabricator needs Node 20.18.1 or later. A server in the sandbox
puts its credential in the sandbox; that is why the servers are
anonymous.

**Permission rules.** A deny holds under bypass mode. Absolute paths
start with `//`. `:*` is a prefix match only at the end. Use `Edit`, not
`Write`. Validate a file with `claude --settings FILE doctor`. A deny
list is a speed bump, not a boundary.

**Git to Gerrit.** Provisioning removes `http.proxy` from the git
config. The session environment sets `http.proxyAuthMethod=basic`, and
`$GERRITUA` as `http.https://gerrit.wikimedia.org/.userAgent`, through
`GIT_CONFIG_COUNT` (also for `exec`). For clones at create,
`deps.gerrit_git_config` gives the same: as `export` lines on stdin in
the VM, as the environment on the host, never in argv.
`git-review-check` sends `$GERRITUA` with `-H @-` to gerrit.wikimedia.org
only, and says which status it got. sbx-pull's query goes to Gerrit only.

## nono and Landlock

- Under nono 0.80, `--open-port` allows connections on the loopback
  only. So a nono session gets `NO_PROXY=localhost,127.0.0.1`, and
  `--upstream-bypass` and `--allow-domain` for `192.168.5.2`. The
  inner credential proxy connects with CONNECT through nono's proxy
  when `NONO_PROXY_TOKEN` is set.
- The credential proxy sends `connection: close`. nono's forward path
  copies until the upstream closes, so a kept-alive connection held each
  request open. Verified: one task took 47 minutes before the change and
  33 seconds after it.
- A host that is not on the allowlist gets 502 from nono's proxy in a
  nono session, and 403 from the egress proxy in a `--sudo` session.
- A nested sandbox's sessions use `--sandbox-policy=landlock`: Lima's
  hostagent opens a UDP socket, and the proxy-mode seccomp filter
  refuses it.
- The 4098 `--open-port` flags (one per port of the window) are not
  the cause of a slow session.
- nono's update check to update.nono.sh is refused; that does no harm.
- `PR_SET_NO_NEW_PRIVS`: `sudo` does not work under nono. That is why
  `--sudo` is a mode of its own.

## The egress proxy and `--sudo`

**The QEMU wrapper.** `bin/wmf-sbx-qemu` (`qemu_wrapper.py`) adds
`restrict=on,guestfwd=tcp:192.168.5.100:3128-cmd:sh -c '…socat…$(cat
port)'` to the one `-netdev user`. On the boot call (`-pidfile` or
`-qmp`), it refuses zero netdevs or more than one. It passes probe
calls through. libslirp splits `cmd:` itself, so the command is
`sh -c`, and `$(cat port)` stays unexpanded until a connection comes.
Use `cmd:`, not `tcp:`: `tcp:` is one shared stream for all
connections, which can mix requests.

`restrict=on` does this (verified): the host loopback is cut, there is
no route out, slirp's DNS does not answer, and it fails closed. SSH, 9p
and the guestfwds still work. The template sets `hostResolver` off and
`propagateProxyEnv: false`.

**The proxy** is `nono proxy --profile` with a fixed CA, `--no-auth`
(only the guest reaches the guestfwd), and its own `XDG_STATE_HOME`. A
long-lived thread starts each proxy: `PR_SET_PDEATHSIG` is per thread
(Linux only). The proxy enforces the profile's endpoint rules: a POST to
a read-only host gets 403.

`wmf-sbx allow` starts a new proxy and replaces `port` (write, then
`mv`). The old proxy keeps its open connections.

**DNS.** `fakedns.py` answers each name with a 198.18.x address; the
inner nono resolves names itself, so the address only has to reach the
proxy. `resolv.conf` has `nameserver 192.168.5.100` and `use-vc`. The
names file stays across restarts.

**In the guest:** apt's proxy config, git's
`http.proxyAuthMethod=basic`, `NODE_USE_ENV_PROXY=1`, and the full set of
proxy variables (npm hangs with a partial set).

**The clock.** Lima's guest agent steps the clock when it is more than
100 ms off (over virtio-serial; not in plain mode). `start` waits for
"Time sync:" in the log (`egress.TIME_SYNC_DONE`), because nono's CA
certificate is not valid before its start time. Up to 100 ms of skew is
left.

**Lists.** `egress.SETUP_DOMAINS` (the setup hosts, with
registry.nono.sh) and `nested.DOMAINS`. scarf.sh, datadog and
raw.githubusercontent.com stay denied.

The proxy port is in the VM's host block, so `start` and `resume` run
`limactl edit` before boot.

**Without the proxy** (`vz`, or a missing qemu-system, nono or openssl:
`egress.missing_tools`) wmf-sbx goes on with a warning. An explicit
`--egress-proxy` makes it an error (`create.egress_choice`).
`WMF_CLAUDE_SBX_EGRESS=NAME` adds a SessionStart bullet.

**The inner nono** gets `--allow-domain '*'` and `--upstream-proxy`: the
outer proxy is the one allowlist, so `wmf-sbx allow` covers nono
sessions too. `--allow-net` strips the proxy variables. `AuthShim` adds
`Proxy-Authorization` on the upstream side.

**`--sudo`.** `/etc/sudoers.d/90-wmf-sbx-agent` (checked by `visudo`,
with `env_keep`) and `/etc/wmf-sbx/sudo`. A boot without `--sudo`
removes both. The invariant check runs under `sudo -n`. The agent can
remount a mount read-write, and writes still fail.

**`--sudo --nested`.** `nested.forward_script` makes a systemd socket on
`127.0.0.1:3128` with `systemd-socket-proxyd` to `192.168.5.100:3128`
(`DynamicUser`). The state records `egressForward`. The inner CAs come
from `cloud-init-ca-cert-*.crt`. `resume` does not stop the inner VMs of
a sudo-mode sandbox.

What is left: an upload to an allowed host (the method and path rules
apply through nono's TLS interception). With sudo, the in-guest controls
are advice only. A VM has a larger hypervisor surface than a container.
The proxy costs no throughput (measured).

## The credential proxy and shared login

**The credential proxy** (`credproxy.py`) starts with each `resume`, as
a thread on a port the OS chooses. The VM gets `ANTHROPIC_BASE_URL` and
a phantom token, on stdin, into a 0600 file in `/dev/shm`. A per-session
nft rule `wmf-sbx-cred-PORT` lets the agent reach it, so two sessions
work at once. `CREDENTIAL_PROXY_ENV` sets `NO_PROXY` for it. Behind the
egress proxy, it listens on `cred.sock`, and a guestfwd puts it at
`192.168.5.100:3129`.

Claude Code calls only `POST /v1/messages`, and the open
`HEAD /api/hello`, on the base URL. The token source is a setup-token
token, the live host login, or an API key.

**`wmf-sbx login`.** The idle host login expires (verified: 7.5 hours
after its last refresh). So `login` runs `claude setup-token` in a
throwaway VM from the golden image. The token lasts one year, with the
scope `user:inference`: no Remote Control, connectors, `/usage` or
cloud sessions. `login` uses a pty at the terminal's width;
`find_token` joins wrapped lines. It refuses with no terminal, and
SIGTERM or SIGHUP stops it cleanly. The token goes to
`~/.config/wmf-sbx/claude-oauth-token`, mode 0600.

**The auth proxy** (`create --auth-proxy`, `authproxy.py`) uses nono's
`claude_code` oauth_capture provider in the host's egress proxy (the
token endpoint `platform.claude.com/v1/oauth/token`; phantoms
`sk-ant-oat01-…` and `nono_…`). Each sandbox has a store, and all
sandboxes share one login.

- A refresh revokes the old tokens, and of two refreshes at once one
  loses. So there is one refresher: it stops `claude` when the file has
  the new login, and marks its own proxy current. Verified with two
  sandboxes on one login: four refreshes in 25 minutes, each one 200
  from one sandbox, the second sandbox the refresher after the first
  one's session ended, and no message lost; then 9 hours of messages
  in both, across the real refresh 10 minutes before the expiry.
- A running Claude Code reads its credentials file again. `claude -p`
  refreshes the token; `claude auth status` does not.
- nono's capture store is read once per process. `authproxy.refresh_proxy`
  restarts a proxy older than the store file before it gives out
  phantoms, and before the refresher refreshes: a stale proxy does not
  know the refresh phantom, sends it on unresolved, and gets 400.
- Policy: allow by default; deny `/v1/mcp_servers`, `/mcp-registry` and
  `/api/event_logging` (the rule does not match on the query). Remote
  Control works. The login survives a stop and a start.

Verified: a scan of the VM disks and memory found no real credential.

**Nested.** `/etc/wmf-sbx/nested` (root, mode 0640, group agent,
replaced with `mv`, granted with `--read-file`) is the source and
upstream of the inner credential proxy. The guest honours
`ANTHROPIC_BASE_URL` only in a nested sandbox. After the session the
marker keeps a phantom that no longer works.

## Disks and warm snapshots

**Overlays.** `limactl create` gets a shared 196 KiB placeholder disk.
wmf-sbx then replaces it with `qemu-img create -f qcow2 -F qcow2 -b
GOLDEN disk SIZE`, where GOLDEN is an absolute path and SIZE is the
larger of `--disk` and the golden image. Lima checks backing files only
at create (`AcceptableAsBaseDisk`), not at start (`prepareDisk`).
`disk.check_chain` runs after the swap and after the first start: every
file in the chain is in the cache, and read-only. wmf-sbx never
flattens a chain without saying so, and passes `-U` on a running
instance. It warns on a Lima version other than the tested one. Never
accept an overlay or an image from outside.

`--full-disk` and `vz` keep a full copy (vz converts it to raw).
Verified: an overlay create writes 31 to 54 MiB, against 2.6 GiB for a
copy. Overlays save disk, not time. `status` checks the chain and the
golden image's checksum.

**Warm snapshots** (`snapshot.py`). The key is the golden image, nested
or not, the egress proxy (only when on), and the set of (canonical
name, host path, read-only) repos. At the end of a QEMU create, wmf-sbx
freezes the sandbox: the seal runs in the guest (the identity; the setup
logs are kept on the host and put back, `snapshot.keep_script`; with the
egress proxy, `update-ca-certificates --fresh`), the VM stops, its disk
moves to `snapshots/KEY/GEN.qcow2`, `qemu-img rebase` puts it on the
golden image (chain depth 2), it becomes read-only, and the sandbox gets
a new overlay on it and starts.

A warm create runs `refresh_repo`, `composer update`, `npm ci` only when
the lock file changed (a stamp), and `update.php`. A freeze happens only
when the newest snapshot is older than `imageCache.snapshotInterval`
(1 day): each freeze writes about 5 GiB. A snapshot is a gc borrower
(`snapshot-KEY`) of each host repo; a repo with no such entry makes the
create cold. Snapshots age with their golden image only. Flags: `--cold`,
`--no-snapshot`, `--no-expire` (`imageCache.expire`). gc removes a
superseded snapshot after an hour. Verified: a warm create took about
92 s, a cold one about 721 s.

What create ran lives on in later sandboxes with the same key. The
freeze before the first session and the image age limit it.

**Memory.** In plain mode (`WMF_SBX_QEMU_PLAIN=1`, `vm.start_instance`)
the wrapper adds `virtio-balloon-pci,free-page-reporting=on`, so the
guest gives free pages back. It does not free the page cache. The
default memory is 8 GiB: phan ran out of memory at 4.

**PCI slots.** `qemu_wrapper.spread_virtfs` puts each 9p mount on its own
function (`-fsdev` and `virtio-9p-pci` at `addr SLOT.FN`, 8 per slot,
from slot 0x1e down). q35's root bus held only about 24 devices.
`MAX_QEMU_MOUNTS` is 96.

## Nested sandboxes

`--nested` lets the agent run wmf-sbx in the sandbox.

- Only a nested sandbox puts `agent` in the `kvm` group (an invariant).
- Grants: `--allow ~/.lima`, `--allow-unix-socket-subtree-bind ~/.lima`,
  `--allow-file /dev/kvm`, and read of `/usr/share/{qemu,seabios,OVMF,ovmf}`,
  `/usr/local/share/lima` and `/usr/local/libexec/lima`. The inner SSH
  port is in the ephemeral port window. `limactl` refuses to run as
  root.
- The base image is at `/var/cache/wmf-sbx/base` (`image.local_base`;
  not in the image key). Behind the egress proxy, the host downloads it
  to `~/.cache/wmf-sbx/base/` (`nested.host_base`). Its digest is
  checked twice.
- The inner proxy URL becomes `192.168.5.2`, with its credentials kept.
  `lima.redact` removes proxy passwords from output.
- `image.session_ca_file` (`~/.cache/wmf-sbx/session-ca.pem`) holds the
  certificates that the system bundle does not have (compared as DER).
  `resume` and `vm.ensure_running` refresh it.
- The inner image build needs deb.debian.org, security.debian.org,
  github.com, nodejs.org, release-assets.githubusercontent.com,
  downloads.claude.ai, pypi.org, files.pythonhosted.org and
  registry.nono.sh.
- The checkout of wmf-sbx itself (`wmf-sbx-src`) is in the engineer's
  home path (`nestedSrc`), not in `/home/agent`.
- Alternates: `repos.alternates` and `template.is_mountable` mount the
  object stores that a host repo borrows from.
- `resume` runs `nested.stop_inner`, unless an agent nono still runs:
  Landlock scopes signals, so the outer session cannot stop them.
  `vm.stop` checks `ha.pid`.
- `exec` passes `image.guest_proxy_env`. `TMPDIR` is the session's: Lima
  makes the cidata there.
- Default sizes: 14 GiB of memory, 8 GiB for the inner VM.
- The inner credential: see "The credential proxy and shared login".

## Local ports

The ephemeral port range is 49152–53247, with `tcp_tw_reuse`. A session
opens port 4000, port 9876, that window, and `--listen-port 0`. nftables
lets the VM open TCP to the outside on 80 and 443 only. The `block` and
`agent_egress` chains accept `ct direction reply` only, so deleting a
credential proxy rule also cuts the open connections to it.

Lima's DNS: the rule matches `ct original proto-dst 53`. DNS outside the
egress proxy (unfenced mode) is left open; a filtering DNS forwarder
would close it (DoH does not help). Dropping nono's network half is not
worth it while unfenced mode exists.

## Method

- A negative finding is only as strong as its search: name the surface
  searched (`--help`, the rendered page or the API comments, `ls` or
  `ls -la`). "We do not use it" is not "it is not there".
- Probe a binary's capability (`--help` contains the flag), not its
  version number.
- A test that greps the command it tests checks spelling, not
  behaviour: run the script against stubs. Find steps by content, not
  by list position or count.
- A step that verifies its artifact by a path the product does not use
  verifies nothing.
- Tests must isolate `HOME` and `XDG_CONFIG_HOME`: a new config file is
  a new way for the suite to read the developer's machine.
- Capture exit codes in the same compound command. Make each probe
  depend on as little as the question allows. Check a measurement design
  for confounds.
- Error text that a model reads is interface: say what happened and
  what did not.
- Advice to remove something gives the exact argv to restore it.
- Code baked into a VM at image or create time reaches only new
  sandboxes: a change to it needs a rebuild or a new sandbox.
