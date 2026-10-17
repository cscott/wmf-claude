# wmf-sbx

One command makes a disposable MediaWiki development sandbox: a Lima VM
with clones of the repos you name and their MediaWiki dependencies, a
working wiki, and the wmf-claude plugin. Claude Code runs in it as an
unprivileged user under nono, or, with `create --sudo`, as a user with
sudo and no nono. On QEMU, the host's egress proxy is a fence between the
VM and the internet ("Modes"). The host gets a git remote to fetch the
agent's work.

This file tells how to use wmf-sbx. `SECURITY.md` tells what the agent
can and cannot reach; `DESIGN.md`, the reasons for the design and the
facts that the code depends on; `MACOS.md`, the setup, host checks and
differences on a Mac (`vz`); `TODO.md`, the open work; `HISTORY.md`, the
designs that were tried and dropped. `checks/` holds manual checks that
need a real host or a running sandbox (each header says where it runs).

## Install

On the host you need:

- [Lima](https://lima-vm.io/) 2.x. On Linux, also QEMU (`qemu-system-x86`
  or `qemu-system-arm`, `qemu-utils`) and, for speed, KVM. On macOS, Lima
  uses `vz`.
- For the egress proxy (QEMU only):
  [nono](https://github.com/nolabs-ai/nono) and `openssl`. Without them,
  `create` warns and makes an unfenced sandbox. Use the nono version in
  `.nono-version`, which the guest gets: `create` warns when the host's
  nono is older.
- Python 3 with PyYAML, git and jq.
- A checkout of this repository with its submodules
  (`git clone --recurse-submodules`). The golden image is built from it.

"Getting started" below has the steps.

## Commands

Every command is `wmf-sbx VERB ...`, or `wmf-sbx-VERB ...`. A `NAME` can
also be a path shortcut such as `.`: the sandbox whose primary repo is
that directory. `--help` after a verb shows its options.

| Verb | What it does |
|---|---|
| `create PRIMARY [EXTRA...]` | make a sandbox, then start Claude Code in it ("`create`") |
| `resume NAME [-- ARGS]` | start Claude Code in the sandbox ("Sessions") |
| `exec NAME -- CMD` | run a command in the VM, outside nono, with a session's variables (`$MW_INSTALL_PATH`, ...) |
| `cp [-r] SRC... DST` | copy between the host and a sandbox, or two sandboxes (`NAME:PATH`) |
| `allow NAME HOST...` | let a fenced sandbox reach more hosts ("The egress proxy") |
| `logs NAME [TAIL-OPTS]` | the egress proxy's log: what it allowed and denied |
| `ls`, `status NAME` | list the sandboxes; check one (VM, image, mode, security invariants) |
| `start NAME`, `stop NAME` | boot or stop the VM; nothing is lost |
| `rm NAME...` | remove the VM, its state and its host remotes ("Cleaning up") |
| `pull [NAME/]BRANCH` | the same as `git sbx-pull`, in a host repo ("Commit flow") |
| `image build/ls/rm/gc/prune` | manage the cached golden images ("The golden image") |
| `login` | get the inference-only token, for every sandbox ("Sign-in") |
| `resolve`, `ls-remotes` | show how a repo name or directory resolves; list a repo's sandbox remotes |

Options of the smaller verbs:

- `exec`: `--engineer` runs as Lima's user (with sudo), not as the agent;
  `-w`/`--workdir DIR` sets the working directory in the VM.
- `allow`: `--list` shows the added hosts; `--rm HOST...` removes them.
- `cp`: `-r` (`-R`, `--recursive`) is optional. Several sources need a
  directory as DST. A quoted `'NAME:*.txt'` is expanded in the sandbox.
- `rm`: `-f`/`--force`, `--dry-run`, `--keep-remotes` (keep the host
  remotes and the state), `--prune`, `--scan DIR` ("Cleaning up").
- `image build`: `--dry-run` (print the inputs and the key) and
  `--keep-builder`. `image rm KEY` refuses an image that a sandbox uses.
- `login`: `--image KEY` boots that golden image (default: the newest).
- `resolve NAME` or `resolve DIR` (`--path DIR` is the same), and
  `ls-remotes [--path DIR]` (default: the current directory): `--json`.

## Getting started

On Linux. On a Mac, follow `MACOS.md` instead.

1. **Install wmf-claude and its prerequisites** ("Install" above). Clone
   this repository and run its setup, as the top-level `README.md` says:

   ```bash
   git clone --recurse-submodules https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude.git
   cd wmf-claude
   ./setup.sh
   ```

   `setup.sh` installs nono, which the host egress proxy runs, and
   registers the MCP servers for your own `claude`. Then make sure that
   you can use KVM (`ls -l /dev/kvm`; if you are not in its group,
   `sudo usermod -aG kvm $USER` and log in again), and install
   git-review (`sudo apt install git-review`): `create` runs
   `git review -s` in each repo that it clones from Gerrit.

2. **Put `sbx/bin` on your `PATH`**, in `~/.profile` (git's remote
   helper, `git-remote-wmfsbx`, needs it there, not only in an
   interactive shell; see "Troubleshooting"):

   ```bash
   export PATH="$HOME/src/wmf-claude/sbx/bin:$PATH"   # your checkout's path
   ```

3. **Set your Phabricator username**, also in `~/.profile`. The
   Phabricator MCP server in each sandbox uses it for its "my tasks"
   filter. Without it, `create` asks `claude mcp get phabricator`:

   ```bash
   export WMF_CLAUDE_PHAB_USER=YourPhabUsername
   ```

   Log out and in again (or `source ~/.profile`) to get steps 2 and 3.

4. **Optional: tab completion for `git sbx-pull`.** Link it into your
   bash-completion directory (not `~/.bashrc`). It needs bash 4 or
   later, bash-completion 2 and git's own completion; not zsh. A TAB
   first fetches the sandbox remotes (each at most once a minute), so the
   list has the agent's new branches.

   ```bash
   dir=${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion}/completions
   mkdir -p "$dir"
   ln -s "$HOME/src/wmf-claude/sbx/completion/git-sbx-pull" "$dir/git-sbx-pull"
   ```

5. **Tell wmf-sbx where your repos are.** Your settings are in
   `~/.config/wmf-sbx/settings.yaml`. Start from the example:

   ```bash
   mkdir -p ~/.config/wmf-sbx
   cp sbx/reference/example-settings.yaml ~/.config/wmf-sbx/settings.yaml
   $EDITOR ~/.config/wmf-sbx/settings.yaml
   ```

   Change the `repos:` rules to your own layout ("Repos and
   dependencies"). Each rule maps a Gerrit or GitLab project to the
   directory of your checkout. The most specific rule whose directory
   exists wins (rules of equal weight go in file order); when there is no
   checkout yet, the most specific rule is where it goes. For example,
   if your extensions are in `~/src/mw/extensions/` and everything else
   is in `~/src/`:

   ```yaml
   repos:
     - match: "gerrit:mediawiki/core"
       path: "~/src/mw/core"
     - match: "gerrit:mediawiki/extensions/{name}"
       path: "~/src/mw/extensions/{name}"
     - match: "gerrit:mediawiki/skins/{name}"
       path: "~/src/mw/skins/{name}"
     - match: "gerrit:**/{name}"
       path: "~/src/{name}"
     - match: "gitlab:**/{name}"
       path: "~/src/{name}"
   ```

   Then examine the rules with `wmf-sbx resolve`. It prints the project,
   the directory and the rule that maps one to the other. A repo that is
   not on your machine yet resolves too: `create` offers to clone it
   there.

   ```bash
   wmf-sbx resolve Cite                  # the project, and the directory it maps to
   wmf-sbx resolve mediawiki/core
   wmf-sbx resolve ~/src/mw/extensions/Echo   # the directory, back to its project
   ```

   A directory gets its project from its `.gitreview`, from an exact rule,
   or from a gitlab.wikimedia.org `origin`, never from a pattern:
   `~/src/{name}` does not say which namespace `{name}` was in. The `rule:`
   line then shows which of your rules gives that directory, or
   `<no rule>` if none does.

   While you are in the file, look at `session:` (the model, permission
   mode and theme of each new sandbox's Claude Code) and `environment:`
   (for example `GERRITUA`, if you have one).

6. **Sign in, when you make your first sandbox** ("Sign-in"). On QEMU,
   run `/login` in the first session of your first sandbox; later
   sandboxes share that login. You need not do anything now.

## First steps

This walk-through uses a very small extension,
`gerrit:mediawiki/extensions/MultiTitle`, with no dependencies other than
core and the Vector skin, which `create` adds with core, so the clones
are quick. Change the paths to what `wmf-sbx resolve MultiTitle` gives
you.

**Make the sandbox.** `create` resolves MultiTitle, offers to clone it
(and core and Vector, if you do not have them) where your `repos:` rules
say, builds the golden image if it has none (10-20 minutes the first
time), boots the VM, sets up the wiki, and starts Claude Code in the
MultiTitle clone:

```bash
wmf-sbx create gerrit:mediawiki/extensions/MultiTitle    # the sandbox is sbx-multititle
```

**Give the agent a task.** In the session, for example:

> Add a "Testing" section to README.md that tells how to run this
> extension's PHPUnit tests in this sandbox. Run them to check what you
> wrote. Commit it on a branch `readme-testing`.

(Or, with no terminal: `wmf-sbx resume sbx-multititle -- --permission-mode
auto -p "..."`; see "Sessions".)

The agent works in its own clone, in the VM. It cannot write your
checkout, and it ends its turn with a commit on a branch. When you exit
the session, wmf-sbx lists the branches that you have not fetched, with
the command that takes each, and any uncommitted files.

**Take the change on the host**, with `git sbx-pull` in your checkout:

```bash
cd ~/src/mw/extensions/MultiTitle
git sbx-pull readme-testing     # or `git sbx-pull`: the only new branch
git log -p -1
```

You are now on a branch `readme-testing` at the agent's commit, with an
upstream on `origin`, so `git review` sends it to Gerrit as usual.

**Change it on the host, and hand it back.** Edit, then commit on the
same branch (a new commit, or `git commit --amend`):

```bash
$EDITOR README.md
git commit -a --amend
wmf-sbx resume sbx-multititle   # continues the last conversation
```

In the session, type `/sbx-pull`. The agent runs `git sbx-pull` in the
sandbox, which takes your current branch, here `readme-testing`, and
shows what you changed (`git range-diff`). The agent can then continue
from your version.

**Copy files** between the host and the sandbox with `wmf-sbx cp`. One
side is `NAME:PATH`; a relative PATH is in the sandbox's primary clone:

```bash
wmf-sbx cp ~/Downloads/error.log sbx-multititle:      # into the MultiTitle clone
wmf-sbx cp sbx-multititle:'*.log' /tmp/               # quoted: expanded in the sandbox
wmf-sbx cp sbx-multititle:/home/agent/coverage ./coverage   # a directory, whole
```

A file copied in is not committed: tell the agent to read it, or to
commit it. Use `git sbx-pull`, not `cp`, for code.

**Look around, and clean up:**

```bash
wmf-sbx ls                       # your sandboxes
wmf-sbx status sbx-multititle    # its mode, image and security checks
wmf-sbx exec sbx-multititle -- git status   # a command in the VM, as the agent
wmf-sbx stop sbx-multititle      # nothing is lost; `resume` starts it again
wmf-sbx rm sbx-multititle        # first warns about work that you have not fetched
```

## Developing wmf-claude

To work on wmf-claude or wmf-sbx itself, make a nested sandbox of your
wmf-claude checkout: the agent can then run Lima VMs, and so its own
wmf-sbx, in it ("Nested sandboxes"). This is **lower security**: use it
only for work on sandboxing tools. From your wmf-claude checkout:

```bash
cd ~/src/wmf-claude
wmf-sbx create --nested --sudo --name wmf-lima . \
  gerrit:mediawiki/extensions/Cite gerrit:mediawiki/extensions/Translate
```

- `--nested` gives the agent KVM, QEMU and Lima, and grants for them.
- `--sudo` runs Claude Code with sudo and no nono, behind the host's
  egress proxy ("Modes"). The agent can then install what a test needs,
  and nono's limits do not get in the way of the inner VMs.
- Cite and Translate are clones for the agent's tests (Translate has
  dependencies; Cite has CI dependencies). The inner wmf-sbx gets a
  `settings.yaml` of its own, which puts the repos of inner sandboxes in
  `~/wmf-sbx-src`.

`wmf-sbx` is not on the `PATH` in the sandbox: the agent runs
`sbx/bin/wmf-sbx` from its clone (`sbx/CLAUDE.md`), so a test uses the
agent's changed copy. For example, in the session:

> Make an inner sandbox of the Cite clone with `sbx/bin/wmf-sbx create
> --no-start --clone-missing`, run Cite's PHPUnit unit tests in it with
> `sbx/bin/wmf-sbx exec`, report the result, then remove the inner
> sandbox.

The first inner `create` builds an inner golden image first (about
10 minutes with nested KVM). The key of the image has the wmf-claude
commit in it, so the next commit builds again: when the change is not
in the image, the agent sets `WMF_SBX_GIT_HASH_OVERRIDE=latest` to use
the cached one ("The golden image"). An inner VM gets 8 GiB, so one runs
at a time. To check a change to the image or the agent's instructions,
use the blind acceptance run in `checks/acceptance-task.md`.

## Sign-in

Each sandbox uses one of two ways, chosen at `create`. In both, the
credential stays on the host (`SECURITY.md` §4; `DESIGN.md`, "The
credential proxy and shared login").

- **The shared login (the auth proxy).** `/login` in a session, as in
  Claude Code anywhere. The host egress proxy completes the sign-in and
  keeps the login. Claude Code in the VM gets only stand-ins
  ("phantoms"), which the proxy replaces with the real tokens. All
  auth-proxy sandboxes share the one login: sign in once, in any of them,
  and the next `resume` of each other one has it. It is a full login:
  Remote Control, sessions, `/usage` and your organization's managed
  settings work. The proxy refuses claude.ai's connectors
  (`/v1/mcp_servers`) and the telemetry stream. Fenced sandboxes only.
- **The inference-only token** (`wmf-sbx login`, or a key). Any sandbox.

`create --auth-proxy`/`--no-auth-proxy` (or `create: {authProxy: true}`)
chooses. Without them, a fenced sandbox gets the shared login when the
host has no token or key (items 1 to 3 below); other sandboxes get the
token. `--auth-proxy` on an unfenced sandbox is an error. `status` shows
which way a sandbox uses.

**The shared login, on the host.** The real tokens are in nono's capture
store, `~/.local/state/wmf-sbx/auth/state/`, and the current phantoms in
`~/.local/state/wmf-sbx/auth/credentials.json`. `resume` copies the
phantoms into the sandbox (only the `claudeAiOauth` record, and only when
it holds phantoms), and takes a new `/login` from it. A refresh revokes
the old tokens, so the sandboxes never refresh (their copy says that it
expires in one year). One running `resume` refreshes the login about 10
minutes before it expires, with one short Haiku turn in its sandbox, and
tells the other running `resume`s, which put the new phantoms in their
sandboxes. When no `resume` runs, the next one refreshes first.
`rm -r ~/.local/state/wmf-sbx/auth` signs every sandbox out.

**The inference-only token.** `wmf-sbx login` boots a temporary VM from
the golden image and runs `claude setup-token` there on your terminal
(open the URL, sign in, paste the code back). It saves the token in
`~/.config/wmf-sbx/claude-oauth-token` (mode 0600), and deletes the VM.
The token is valid for one year, for model requests only: no Remote
Control, and no connectors, skills or plugins from claude.ai. MCP servers
in the sandbox still work. For each session, `resume` runs a proxy on the
host's loopback that adds the credential to Claude Code's API requests;
the VM gets only the proxy's address and a random stand-in for that
session. Its log (requests, no bodies) is
`~/.local/state/wmf-sbx/sandboxes/NAME.credproxy.log`. `resume` uses the
first of:

1. `$ANTHROPIC_API_KEY`;
2. `$CLAUDE_CODE_OAUTH_TOKEN`;
3. `~/.config/wmf-sbx/claude-oauth-token`; refused if other users can
   read it;
4. your Claude Code login on the host (Linux:
   `~/.claude/.credentials.json`; macOS: the keychain), read for each
   request, so a refresh on the host applies. It expires when Claude Code
   has not run on the host for some hours. `WMF_SBX_HOST_LOGIN=0` leaves
   it out. `create` ignores it when it chooses the way to sign in.

With none of these, Claude Code asks you to log in in each new sandbox,
and the login stays in the VM.

## settings.yaml

`~/.config/wmf-sbx/settings.yaml` (in `$XDG_CONFIG_HOME` when it is
set) holds every setting.
`reference/example-settings.yaml` is a commented example of each section.

```yaml
repos: [...]          # project -> host directory ("Repos and dependencies")
session:              # preferences for new sandboxes, written into the
                      # agent's ~/.claude/settings.json
  model: opus                         # Claude Code's `model` setting
  permission_mode: bypassPermissions  # default, acceptEdits, plan, auto, bypassPermissions
  theme: dark         # dark, light, dark-daltonized, light-daltonized, dark-ansi, light-ansi
  ste: false          # no ASD-STE100 rule in the SessionStart context (default: true)
  settings: false     # no option defaults from this file (default: true)
  native_copy: false  # no copy to the host clipboard (default: true; "Copying text")
environment:          # variables for the sessions (the agent's settings `env`)
  GERRITUA: "..."
packages: [nano]      # extra Debian packages in the golden image
extra_dependencies:   # undeclared dependencies of the repos you name
  gerrit:mediawiki/extensions/Example: [gerrit:mediawiki/extensions/OtherThing]
dependency_overrides: # a display name in `requires`, mapped to a project
  "Abuse Filter": gerrit:mediawiki/extensions/AbuseFilter
link_overrides:       # a repo's name under core's extensions/ or skins/
  gerrit:mediawiki/extensions/Example: ExampleExtension
imageCache: {maxAge: 14d, expire: true, gcAge: 7d, snapshotInterval: 1d}
create: {cpus: 8}     # option defaults ("Option defaults")
```

- `ste: false` sets `WMF_CLAUDE_STE=0`; `write-commit-msg` still uses STE.
- `bypassPermissions` lets the agent use every tool without asking; the
  deny rules still apply. Upstream `bin/claude` refuses it on the host,
  where it also approves the `ask` rules of `wiring/settings-merge.json`.
  In a sandbox, the VM, nono and the deny rules are the boundary.
- A sandbox keeps the `session:` settings it was made with. Change them
  in it with `/model` or `/permissions`, or create a new sandbox.
- `environment:` cannot set `ANTHROPIC_*` or `NO_PROXY` (the credential
  proxy uses them). With `GERRITUA` set, the SessionStart hook tells
  Claude to send it as the User-Agent to gerrit.wikimedia.org, for the
  higher rate limit, and to keep it secret; the hook never shows it.
- `packages:` are in the golden image's cache key: a change builds a new
  image at the next `create`.
- **The first sandbox shows every first-run question**: Claude Code's
  onboarding, "Do you trust the files in this folder?", and the
  bypass-mode confirmation. After its first session, `resume` writes
  `~/.local/state/wmf-sbx/first-run-seen`, and then `create` answers them
  in new sandboxes (`SECURITY.md` §3). Delete the file to see them again.
  Sessions always skip `bin/claude`'s "Press Enter" pause.

### Option defaults

Each option takes its default from settings.yaml, except in `logs` and
`pull`, and `resume`'s words after `--`. For `--some-flag` of
`wmf-sbx VERB`, the key is `VERB.someFlag` (camelCase). A subcommand
nests: `image: {build: {keepBuilder: true}}`. A list option takes a list:
`rm: {scan: [~/Wikimedia]}`.

- The command line wins over settings.yaml, which wins over the code's
  default. Each flag has a `--no-` form to undo a default from settings
  (`--no-cold`, `--no-force`). A list on the command line replaces the
  list from settings. `null` is the same as no key.
- `--no-settings` (after the verb) ignores the option defaults for one
  command. `session: {settings: false}` ignores them for every command;
  then `--settings` uses them for one. The other sections always apply.
  `--config PATH` reads a different file.
- `--help` shows each option's key, and its value from settings:
  `[settings: create.cpus: 8]`.
- An unknown key or section gets a warning and is ignored, so two
  versions of wmf-sbx can share one file. A value of the wrong type
  (`cold: yes`, `cpus: four`) is an error.
- wmf-sbx does not question a value from settings, as it does not
  question a flag. `create: {sudo: true}`, `create: {nested: true}`,
  `exec: {engineer: true}` and `rm: {force: true}` decrease safety for
  every command; `create: {memory: 1GiB}` gives 1 GiB, also with
  `--nested`. Do not set `resume.continue: true`: it fails on a new
  sandbox, which has no conversation.

## The golden image

The golden image holds the system packages, PHP, Node, php-ast, nono,
Claude Code and the built wmf-claude tree. Its cache key is a hash of its
inputs: the host uid, the architecture, the Debian base image, the
package list, the nono, Node, php-ast and Claude Code versions, the
helper and build scripts, and the wmf-claude HEAD commit. A change builds
a new image (about 10 minutes) at the next `create`. The images are in
`${XDG_CACHE_HOME:-~/.cache}/wmf-sbx/images/KEY/`, about 2.5 GB each
(`DESIGN.md`, "The golden image").

- **Claude Code version.** Sandboxes do not update Claude Code. The image
  has the `stable` channel's release at build time.
  `WMF_SBX_CLAUDE_VERSION=latest` (the `latest` channel) or `=2.1.296`
  (an exact release) changes this, for `create` and `image build`. To
  change a sandbox's version, create a new sandbox.
- **Concurrent builds.** When a `create` or `image build` needs an image
  that another process builds now, it asks: wait for it (the default),
  delete the other builder and build here, or abort (the choice with no
  terminal; exit status 1). A crashed build's builder is deleted.
- **Work on wmf-claude.** Each commit changes the key. To keep the image,
  set `WMF_SBX_GIT_HASH_OVERRIDE`: its value replaces the commit. Change
  the value (`v1`, `v2`) to build again from HEAD. To keep the current
  image, use its commit, which `image ls` shows after `wmf-claude` (and,
  for an image built with an override, HEAD's tree in parentheses).
  `latest` uses the newest cached image for your architecture and uid,
  whatever its inputs (as `login` does), and builds only when there is
  none.
- **Age.** An image older than `imageCache.maxAge` (14 days) is built
  again, with the same inputs, at the next `create`, for the Debian
  updates. `--no-expire` (or `imageCache: {expire: false}`) keeps it,
  with a warning, for a demo that must not wait.
- **Cleanup.** At its end, `create` runs `image gc`, which deletes the
  unused images older than `imageCache.gcAge` (7 days). `image prune`
  deletes every unused image. `image ls` lists the images and snapshots
  with their Claude Code versions, their disk use and the sandboxes', and
  what `prune` would free.
- **Disks.** A sandbox with core and a few extensions uses 15-20 GB. On
  QEMU, its disk is a copy-on-write overlay on the golden image, so a new
  sandbox takes megabytes, and the image must stay in its place,
  read-only (`status` checks both). An image under a sandbox's disk is in
  use, with or without a state file. `--full-disk` gives a full copy, as
  on `vz`, which cannot boot an overlay. This depends on Lima 2.2.1's
  behavior: `create` warns on other versions (`DESIGN.md`, "Disks and
  warm snapshots").
- **Start with `wmf-sbx start`**, not `limactl start`. On QEMU, the VM
  starts through `sbx/bin/wmf-sbx-qemu`, which runs the egress proxy,
  adds free-page reporting (an idle sandbox gives freed memory back) and
  puts 8 git-dir mounts on each PCI slot. `limactl start` does none of
  this, and cannot boot a sandbox of more than about 24 repos.

### Warm snapshots

On QEMU, each `create` ends with a **snapshot** of the sandbox's disk,
before any session: the clones, `vendor/`, `node_modules` and the
installed wiki. The next `create` of the same set of repos (the same host
paths, `:ro` flags, golden image, `--nested` and fence) starts from it,
and updates only what changed (`git fetch`, `composer update`, `npm ci`
where `package-lock.json` changed, `update.php`). A repo can be named or a
dependency. When the snapshot is older than `imageCache.snapshotInterval`
(1 day), the result becomes the new snapshot.

- Making a snapshot stops and starts the sandbox once, and writes a few
  GiB; `create` shows the time. A cold create always makes one;
  `snapshotInterval: 0m` makes one at each create.
- `--cold` ignores the snapshot and makes a new one (and first a new
  golden image, if the current one is old). `--no-snapshot` neither uses
  nor makes one. `--full-disk` makes none.
- A snapshot's clones borrow your host repo's objects, so gc stays off in
  the host repo while the snapshot exists (`git config wmfSbx.borrower`
  lists `snapshot-KEY`). A host repo that you delete or clone again gets a
  cold create.
- `image ls`, `gc` and `prune` treat snapshots as images: a set of repos
  that you did not create for `gcAge` is cold again. A snapshot keeps its
  golden image in use; a new golden image starts new snapshots.

## Repos and dependencies

wmf-sbx names repos by their Gerrit or GitLab project. An argument to
`create` or `resolve` can be:

- a short name (`Cite`): Gerrit is searched for a project with that last
  path segment, then GitLab (`repos`, `toolforge-repos`). No match, or
  more than one, is an error that lists the candidates;
- a project path, with or without a forge (`mediawiki/extensions/Cite`,
  `gerrit:mediawiki/extensions/Cite`, `gitlab:repos/...`);
- a local directory (`.`, `~/src/foo`), mapped back to its project.

The `repos:` rules map the project to a host directory, the most specific
rule first: an exact project (`gerrit:mediawiki/core`) before a pattern.
In a pattern, `{name}` is the last path segment and `**` any number of
leading ones. Write rules for your checkouts, and end with a catch-all
such as `gerrit:**/{name}` → `~/Wikimedia/{name}`. When a repo is not on
the host, `create` lists the missing repos and asks before it clones them
(with no terminal, it stops). After a Gerrit clone, it runs
`git review -s` (it warns when git-review is not installed).

**Dependencies.** `create` follows `requires`, `dev-requires` and
`suggests` in `extension.json` and `skin.json`, and adds core and Vector:
`create Translate` also gives core, Vector and UniversalLanguageSelector,
linked into core and loaded by the wiki. It also adds what WMF CI checks
out for a named repo, which no manifest declares (for Cite:
ParserFunctions, Popups, VisualEditor and six more); without them, some
of the repo's tests fail. It reads CI's lists from integration/config on
gitiles (`zuul/dependencies.yaml`, and `zuul/phan_dependencies.yaml` for
the phan job; two requests, about 90 KB), and continues with a warning
when gitiles is not available. A manifest that it cannot get stops the
create (`DESIGN.md`, "Repo resolution and the dependency walk").

## `create`

```
wmf-sbx create PRIMARY[:ro] [EXTRA[:ro] ...] [options]
```

- `PRIMARY` is where the session starts. `:ro` makes a repo read-only for
  the agent.
- The named repos keep the branch and commit of your host checkout.
  Dependencies are reset to the tip of upstream `master` (or `main`),
  which the clone fetches from the real upstream (`origin`). If upstream
  cannot be fetched or identified (no `gerrit:`/`gitlab:` name and no
  https `origin` on the host), the repo is reset to your host repo's
  `master` (or `main`), with a warning. `--reset-all` resets the named
  repos to upstream too.
- `--name NAME`: the sandbox and host remote name (default
  `sbx-<primary>`). A second `create` of a name that a running `create`
  uses stops at once (a default name gets a suffix instead).
- `--no-remotes`: add no host remote, and do not change host gc.
- `--start`/`--no-start`: start Claude Code at the end, as `resume` does
  (default: when stdin and stdout are a terminal). After the session,
  `create` shows the `wmf-sbx` commands for the sandbox.
- `--clone-missing`: clone the missing repos without a question.
- `--no-deps`: only the named repos. `--no-ci-deps`: no CI lists.
  `--no-phan-deps`: no phan-job part. `--no-dev`, `--no-suggests`: a
  narrower manifest walk.
- `--cold`, `--no-snapshot`, `--no-expire`, `--full-disk`: "The golden
  image". `--image KEY`: a cached golden image (`image ls`), not the one
  for the current inputs.
- `--vm-type qemu|vz` (default: `vz` on macOS, else `qemu`), `--cpus N`
  (4), `--memory SIZE` (8GiB), `--disk SIZE` (60GiB). A size is binary
  (`4g` is `4GiB`), a bare number is bytes, and less than 1GiB is an
  error.
- On QEMU, a sandbox has at most 96 repos, dependencies included: each
  git-dir mount is a PCI device function, 8 on each slot. `create`
  refuses more; `--no-phan-deps`, `--no-ci-deps` or `--no-dev` remove
  dependencies.
- `--egress-proxy`/`--no-egress-proxy`: fenced or unfenced ("The egress
  proxy"). `--auth-proxy`/`--no-auth-proxy`: "Sign-in".
  `--sudo`/`--no-sudo`: "Modes". `--nested`: "Nested sandboxes".
- `--dry-run`: print the plan, the image key and the Lima config; build,
  clone and create nothing.

When a step fails, `create` stops and keeps the VM: repair the cause with
`exec`, or `rm` the sandbox. A failed per-repo step (`composer update`,
`npm ci`) is a warning; the agent finds it in
`~/.wmf-sbx/wmf-sbx-setup.status`.

## Modes

A sandbox's mode, chosen at `create` and shown by `status` and `ls`, has
two parts: **fenced** or **unfenced** (whether the host's egress proxy is
a fence between the VM and the internet; on QEMU, fenced is the
default), and **+nono** or **+sudo** (what is around the agent in the
VM). So there are three modes: **fenced+nono** (the default), **fenced+sudo**
(`--sudo`), and **unfenced+nono** (`vz` or `--no-egress-proxy`; to be
removed, `TODO.md`). There is no unfenced+sudo: with root in the guest
and no fence, nothing limits the agent's egress. In each mode, the Claude
credential stays on the host. Compared with upstream's `claude-vm`
(`../docs/lima-vm.md`), its **isolated** VM is approximately
unfenced+nono with the model token in the VM, and its **contained** VM is
a fence whose allowlist has only the model API.

**fenced+nono.** The agent has no sudo. Each session runs upstream's
`bin/claude`, so nono, in the VM, with the `wmf-engineer` profile and
per-session grants (`DESIGN.md`, "nono and Landlock").

**fenced+sudo.** The agent has passwordless sudo (a file in
`/etc/sudoers.d`; it is in neither the `sudo` nor the `docker` group),
and can `sudo apt-get install` what a task needs, through the proxy. Each
session runs Claude Code with the wmf-claude plugin and the tool layer of
`wiring/settings-merge.json`, with no nono. The boundary is the VM and
the host's egress proxy, which root in the guest cannot change; the proxy
still applies the profile's endpoint rules (a wiki POST gets `403`). So
`--sudo` needs QEMU and the proxy, and `create` fails rather than use the
guest's own rules. `--no-sudo` undoes `create: {sudo: true}`. The
invariants still run at each start, but root in the guest can make them
pass (`SECURITY.md` §6b).

## The egress proxy

A fenced VM has no network route and no DNS. Its only way out is a
`nono proxy` that the QEMU wrapper runs on the host, which root in the
guest cannot change. The proxy allows the `wmf-engineer` profile's hosts
(with its read-only endpoint rules for the wikis), the package registries
and Debian's mirror; anything else gets `403` (`SECURITY.md` §3;
`DESIGN.md`, "The egress proxy and `--sudo`").

```bash
wmf-sbx allow NAME example.org       # at once, also in a running session
wmf-sbx allow NAME '*.example.org'
wmf-sbx allow NAME --list            # or --rm HOST
wmf-sbx logs NAME                    # the proxy's log; -f follows it (tail's options)
```

The agent is told to ask you for `wmf-sbx allow` when the proxy denies a
host that it needs. `create` makes an unfenced sandbox, with a warning that the guest
enforces its own network policy (nono and nftables, which root can
change), when the VM is `vz` (the proxy needs QEMU's `restrict=on`
network; `--vm-type qemu` gives a fence), or when the host has no
`qemu-system-ARCH`, `nono` or `openssl` on its `PATH`.
`--no-egress-proxy` asks for an unfenced sandbox, with no warning.
`--egress-proxy` changes the warning into an error.

## Sessions

`resume NAME` checks the security invariants, starts the credential
proxy, and starts Claude Code as the agent in the primary clone, with
your `TERM_PROGRAM`, `TERM_PROGRAM_VERSION` and `COLORTERM`
(`DESIGN.md`, "The session").

- With no arguments for Claude Code, it continues the last conversation
  when there is one. `--continue` always continues (when the first
  session did not exit cleanly, the sandbox does not know that it had
  one); `--no-continue` starts a new one.
- After `--`, the words are `bin/claude`'s: its flags before a second
  `--`, Claude Code's after it. `resume sbx-cite -- --allow-post=HOST --`
  continues with one more grant; `-- -p "..."` gives Claude Code its
  arguments. A `--sudo` sandbox takes no `bin/claude` flags.
- **A one-shot task, with no terminal:** give Claude Code `-p` and a
  permission mode that does not ask, as there is no one to answer:

  ```bash
  wmf-sbx resume sbx-cite -- --permission-mode auto -p "Run the PHPUnit tests"
  wmf-sbx resume --continue sbx-cite -- --permission-mode auto -p "/sbx-pull"
  ```

  It prints the agent's last message and exits. With `-p`, `resume`
  starts a new conversation unless you give `--continue`. Without a
  permission mode, the agent stops at its first command and asks for an
  approval that it cannot get. `bin/claude` refuses
  `--permission-mode bypassPermissions` on the command line; set
  `session.permission_mode` in `settings.yaml` before the `create`
  instead.
- When a session ends, wmf-sbx lists what the host does not have: each
  branch with commits that the host has not fetched, with the
  `git sbx-pull` command that takes it, and the uncommitted files in
  each clone (a read-only `git status`, as the agent).
- The anonymous Phabricator, Gerrit and GitLab MCP servers are
  registered at `create`. The Phabricator username (only for the default
  "my tasks" filter) comes from `$WMF_CLAUDE_PHAB_USER` on the host, as
  for upstream's `claude-vm`, else from the host's
  `claude mcp get phabricator`.

### Copying text

In a fenced sandbox, a mouse selection in Claude Code goes to the host's
clipboard (paste with Ctrl+V or middle click on the host). `create`
installs a `wl-copy` shim in the VM and sets `WAYLAND_DISPLAY=wmf-sbx` (a
name only: there is no display). During a session, `resume` listens for
the shim on the host, and gives the text to the host's `wl-copy`
(Wayland), `xclip` (X11) or `pbcopy` (macOS). Its log, one line for each
copy, with the size and never the text, is
`~/.local/state/wmf-sbx/sandboxes/NAME.clipboard.log`.

- Copy only. To paste into Claude Code, use the terminal's paste
  (Ctrl+Shift+V or Shift+middle click in GNOME Terminal). A plain middle
  click pastes nothing: Claude Code reads the clipboard itself, which the
  sandbox cannot see. A paste channel would let the VM read your
  clipboard, passwords included (`DESIGN.md`, "The clipboard").
- With no display on the host (an SSH login), `resume` says so and
  starts the session without it.
- The agent can set your clipboard while a session runs (`SECURITY.md`
  §6): examine a command before you paste it into a shell.
  `session: {native_copy: false}` removes the shim from new sandboxes,
  and the listener for all sandboxes.

## Nested sandboxes

`create --nested` is **lower security, only for work on sandboxing
tools** (wmf-sbx, wmf-claude or another). nono gives the agent more:
`/dev/kvm`, the `kvm` group, Lima's sockets, more writable directories
and more download hosts (`SECURITY.md` §6a; `DESIGN.md`, "Nested
sandboxes"). The agent can then run Lima VMs of its own, under nono:
image builds, inner sandboxes, `checks/real-hardware.sh`. Example:
`wmf-sbx create --nested --no-deps ~/src/wmf-claude`.

- `create` installs QEMU, OVMF and Lima in the sandbox and adds the agent
  to the `kvm` group; sessions get grants for `~/.lima`, `/dev/kvm` and
  the image build's download hosts. If the inner wmf-sbx has no
  `settings.yaml`, `create` writes one, with the repos in
  `~/wmf-sbx-src` (at your home's path, as on the host).
- Defaults: 14 GiB of memory and a 120 GiB sparse disk. A sandbox inside
  it defaults to 8 GiB, so one inner VM at a time fits.
- Nested KVM needs the host to allow it (Linux: `kvm_intel`/`kvm_amd`
  `nested=1`, the default on recent kernels; macOS: M3 or later with
  macOS 15, on `vz`). Without it, the inner VMs run slowly, without KVM.
- A session cannot stop an inner VM from an earlier session, so `resume`
  first stops the inner VMs that run (not while another session runs);
  start them again in the session.
- An inner sandbox uses the outer session's credential: each `resume`
  writes its session's phantom and proxy address into
  `/etc/wmf-sbx/nested`, and `resume` in the sandbox uses them
  (`SECURITY.md` §4). `$CLAUDE_CODE_OAUTH_TOKEN` and
  `$ANTHROPIC_BASE_URL` override them.

## Commit flow

Work goes between the host and a sandbox as branches, in both directions,
with `git sbx-pull`. There is no push: the sandbox cannot reach the host,
and the host never writes into the VM. A file that the agent did not
commit stays in the VM (`DESIGN.md`, "The commit flow (sbx-pull)"). In
the sandbox, the remote `local` is your host checkout (read-only, as of
your last fetch), and `origin` is the real upstream. The agent ends each
turn with a commit on a branch; you review, amend and `git review` from
the host, as usual.

**From the sandbox to the host.** In the host repo:

```bash
git sbx-pull                    # the current branch's sandbox branch, else the only new one
git sbx-pull fix-thing          # that branch, from the sandbox that has it
git sbx-pull sbx-cite/fix-thing # that branch, from that sandbox
git sbx-pull fix-thing --onto REL1_46   # its upstream on origin, when the guess is wrong
```

It fetches the sandbox remotes and checks out a branch of the same name
at the agent's tip:

- A new branch gets an upstream on origin (the sandbox branch's upstream,
  else the nearest host branch's), for `git review`. An existing branch
  moves with `git reset --keep`; the old tip is `BRANCH@{1}`.
- Each host branch records its sandbox branch (`branch.BRANCH.wmfsbx*`
  in git config), so the next `git sbx-pull` on it needs no argument.
- It stops when it would leave or move a branch whose commits are not in
  a sandbox remote, on origin or on Gerrit; `-f`/`--force` continues. A
  dirty tree always stops it.
- On a terminal, it asks when the choice is yours (which sandbox branch;
  which host branch; y/N before it leaves unsaved commits). Enter or
  Ctrl-D is no. In a script, it never asks.

`wmf-sbx pull` is the same command. `git safe-reset sbx-cite/fix-thing`
tells you to use `git sbx-pull`, then runs it. `git review-check` (the
pre-review hook) accepts a commit that is in a sandbox remote.

**From the host to the sandbox.** Commit on a branch on the host, then
tell the agent to run `/sbx-pull`. It runs `git sbx-pull [BRANCH]` in the
sandbox, which takes your current branch (or BRANCH) as a branch of the
same name with upstream `local/BRANCH`, and shows the change with
`git range-diff`. A hand-off from `master` or `main` gets a new name,
which the agent chooses (`--as NAME`). It stops when the agent has
commits that the host does not have (run `git sbx-pull` on the host
first), unless `-f`.

## Tests in the sandbox

The agent's guide is `~/MEDIAWIKI-TESTING.md` (from
`templates/MEDIAWIKI-TESTING.md`). Core and the named repos have
`vendor/` and `node_modules/`; a dependency has neither, as in CI, and
the agent runs `composer update` and `npm ci` in one that it changes.
Core has a SQLite wiki with every extension loaded; `composer serve`
serves it on port 4000. `mw-install-browser` installs Chrome for Testing
for karma and wdio (not on aarch64: there is no Linux arm64 build), and
`mw-install-cypress REPO` installs a repo's Cypress binary.

## Cleaning up

```bash
wmf-sbx rm NAME            # asks first; refuses if the agent has unfetched commits
wmf-sbx rm --force NAME    # no question, no guard
wmf-sbx rm --prune         # remotes and state of sandboxes whose VM is gone
wmf-sbx rm --scan DIR      # remotes and gc entries, in the repos under DIR,
                           # of sandboxes with no state file
```

When the agent has unfetched commits, `rm` on a terminal offers to fetch
them into `NAME/BRANCH`, which it then deletes with the remote: to keep a
branch, `git sbx-pull` it first. Uncommitted files get only a warning
(`rm` does not start a stopped VM to find them). `rm` also removes the
sandbox's `git sbx-pull` records. `image gc` and `image prune` remove
unused golden images ("The golden image").

## Troubleshooting

- **`git fetch NAME` fails**: git runs the remote helper with the `PATH`
  of the shell that runs git, so set it in `~/.profile` or your shell's
  rc file. It needs wmf-claude's `sbx/bin` (else "git: 'remote-wmfsbx'
  is not a git command"; `create` warns) and `limactl` (else "fatal:
  wmfsbx: limactl is not on PATH").
- **The host proxy port changed** (a proxy on 127.0.0.1): `start` and
  `resume` update a stopped VM. Stop a running VM first.
- **GitHub archive downloads are blocked**: composer fails in setup. Run
  `wmf-sbx exec NAME -- composer config --global preferred-install source`
  and create again. Source installs are large (several GB per repo).
- **TLS interception** (a corporate CA): set `WMF_SBX_CA_CERTS` to the CA
  files, separated by `:`, for the image build and the VMs.
- **Slow create**: `wmf-sbx exec NAME -- grep timing: .wmf-sbx/wmf-sbx-setup.log`
  shows a line for each `composer update` (resolve, install, autoload) and
  each `npm ci`. Composer's own profile is in `~/.wmf-sbx/timings/`.

## Architecture

`bin/wmf-sbx` runs `bin/wmf-sbx-VERB` (or `python3 -m wmf_sbx VERB`).
The code is in `src/wmf_sbx/`, one module per verb, with the tests in
`tests/`; PyYAML is the only dependency. Lima is driven through
`lima.Limactl`. Each sandbox has a JSON state file under
`~/.local/state/wmf-sbx/` (`state.py`). `DESIGN.md`, "Architecture", has
the full description.

| Part | Modules | What it does |
|---|---|---|
| Repo resolution | `resolve.py` | name → project (Gerrit, then GitLab, forks followed to the real upstream) → host directory; and back, from `.gitreview`, the origin URL or an exact rule |
| Dependency walk | `deps.py`, `ci_deps.py` | the manifests, from the checkout or gitiles; link names into core, decided on the host (`create.link_plan`) |
| Golden image | `image.py`, `image-build.sh` | a builder VM from a pinned Debian 13 image, with the generic kernel (the cloud kernel has no 9p or virtiofs); a sealed, read-only qcow2 |
| Sandbox VM | `template.py`, `sandbox-provision.sh`, `vm.py`, `disk.py` | Lima instance `wmf-sbx-NAME`, users `engineer` (Lima's, with sudo) and `agent` (the host uid). At each boot: an nftables host block (no host, LAN or link-local addresses; the agent gets only TCP 80 and 443 off the VM), a narrower ephemeral port range, TIOCSTI off. `vm.INVARIANTS` run at each start, resume and status |
| Egress proxy | `qemu_wrapper.py`, `egress.py`, `fakedns.py`, `allow.py` | one `-netdev user` with `restrict=on`, and `guestfwd`s on 192.168.5.100 to the `nono proxy` (3128), a fake DNS (53) and the credential proxy (3129). The proxy lives as long as QEMU; `allow` makes the wrapper start a new one |
| Repos in the VM | `repos.py`, `sandbox-repos.sh` | each git dir is a read-only mount under `/run/wmf-sbx/host/`, with a `git clone --shared` of it at the host path; `engineer` owns the `:ro` clones |
| VM to host | `remotes.py`, `remote_helper.py` | the host remote `wmfsbx://NAME/PATH`; `git-remote-wmfsbx` serves only `git-upload-pack`, as the agent, with hooks off; host gc is off while clones borrow objects |
| MediaWiki setup | `mediawiki.py`, `setup.py`, `kit.py` | the host's plan, run in the VM as the agent: links into core, `composer.local.json`, `composer update` and `npm ci` in core and each named repo, the SQLite install, `phpunit.xml`, the api-testing config. `setup.py` goes on stdin, so a change needs no new image |
| Session | `session.py`, `resume.py`, `credproxy.py`, `authproxy.py`, `clipboard.py` | the invariants, the credential proxy, upstream's `bin/claude` under nono (`bin/launch-claude.sh` with `--sudo`); `WMF_CLAUDE_SANDBOX_BACKEND=lima-sbx` (`lima-sbx-sudo`) selects the SessionStart text in `hooks/context/` |
| Commit flow | `sbx_pull.py`, `helpers/git-sbx-pull` | `git sbx-pull` on the host and in the guest |
