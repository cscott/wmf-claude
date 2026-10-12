# wmf-sbx

One command makes a disposable MediaWiki development sandbox: a Lima VM
with clones of the repos you name and their MediaWiki dependencies, a
working wiki, and the wmf-claude plugin. Claude Code runs in it as an
unprivileged user under nono. The host gets a git remote to fetch the
agent's work.

- `SECURITY.md`: what the agent can and cannot reach.
- `NOTES.md`: open to-do items, and the decisions and findings that the
  code depends on.
- `../lima-port/HANDOFF-LIMA.md`: the port's decisions (D1 to D12) and
  the work that remains (track A, `--sudo`; track B, copy-on-write disks).

## Install

On the host you need:

- [Lima](https://lima-vm.io/) 2.x. On Linux also QEMU (`qemu-system-x86`
  or `qemu-system-arm`, `qemu-utils`) and, for speed, KVM. On macOS Lima
  uses `vz`.
- Python 3 with PyYAML, git and jq.
- A checkout of this repository with its submodules
  (`git clone --recurse-submodules`). The golden image is built from it.

Then put `sbx/bin` on your `PATH`:

```bash
export PATH="/path/to/wmf-claude/sbx/bin:$PATH"
```

Every command is `wmf-sbx VERB ...`, or `wmf-sbx-VERB ...`:

| Verb | What it does |
|---|---|
| `create` | make a sandbox (builds the golden image first, if needed) |
| `resume NAME`, `run --name NAME` | start Claude Code in the sandbox |
| `exec NAME -- CMD` | run a command in the VM as the agent, outside nono |
| `cp SRC DST` | copy between the host and a sandbox (`NAME:PATH`) |
| `ls`, `status NAME` | list sandboxes; check one (VM, image, security invariants) |
| `start NAME`, `stop NAME` | boot or stop the VM; nothing is lost |
| `rm NAME` | remove the VM, its state and its host remotes |
| `image build/ls/rm/prune` | manage the cached golden images |
| `login` | sign in to Claude once, for every sandbox ("Sign-in") |
| `resolve`, `ls-remotes` | show how a repo name resolves; list a repo's sandbox remotes |

A `NAME` can also be a path shortcut such as `.`: the sandbox whose
primary repo is that directory.

## Getting started

1. **Tell wmf-sbx where your repos are.** wmf-sbx names repos by their
   Gerrit or GitLab project (`gerrit:mediawiki/extensions/Cite`); rules in
   `~/.config/wmf-sbx/settings.yaml` map projects to directories on your
   machine. A sandbox works on clones of those directories, and a repo
   that is not there yet is cloned there first. Start from the example
   and change the paths to your layout:

   ```bash
   mkdir -p ~/.config/wmf-sbx
   cp sbx/reference/example-settings.yaml ~/.config/wmf-sbx/settings.yaml
   $EDITOR ~/.config/wmf-sbx/settings.yaml
   ```

   The `repos:` rules are tried most specific first: an exact project
   (`gerrit:mediawiki/core`) before a pattern, where `{name}` is the last
   path segment and `**` any number of leading ones. Write rules for the
   places your checkouts already are, and end with a catch-all such as
   `gerrit:**/{name}` → `~/Wikimedia/{name}`. Check a few names:

   ```bash
   wmf-sbx resolve Cite        # the project and the directory it maps to
   wmf-sbx resolve core
   wmf-sbx resolve --path ~/Wikimedia/Extensions/Echo   # the other way round
   ```

   The same file holds your preferences for every sandbox ("Preferences"),
   extra environment variables and Debian packages.

2. **Sign in once:** `wmf-sbx login` ("Sign-in"). It needs a golden image,
   so on a new machine run step 3 first and sign in before the first
   `resume`.

3. **Make a sandbox and start Claude Code in it:**

   ```bash
   wmf-sbx create Cite        # 10-20 minutes the first time (the golden image)
   wmf-sbx resume sbx-cite
   ```

## Sign-in

Sign in once, for every sandbox, without Claude Code on the host:

```bash
wmf-sbx login
```

It boots a throwaway VM from the golden image (no repo, no session), runs
`claude setup-token` there on your terminal (open the URL it prints,
sign in, paste the code back), saves the token in
`~/.config/wmf-sbx/claude-oauth-token` (mode 0600), and deletes the VM.
The token lasts a year. It can make model requests only: no Remote
Control, and no connectors, skills or plugins from claude.ai; MCP servers
in the sandbox still work.

The credential stays on the host. For each session, `resume` runs a
small proxy on the host's loopback that puts the credential into Claude
Code's API requests; the VM gets only the proxy's address and a random
stand-in that is good for that session's proxy.

`resume` takes the first of:

1. `$ANTHROPIC_API_KEY`;
2. `$CLAUDE_CODE_OAUTH_TOKEN`;
3. the file `~/.config/wmf-sbx/claude-oauth-token` (`wmf-sbx login`);
4. your Claude Code login on the host, if you use Claude Code there
   (Linux: `~/.claude/.credentials.json`; macOS: the keychain). `resume`
   reads it for each request, so it uses the host's refreshed login; but
   it expires when Claude Code has not run on the host for some hours.
   `WMF_SBX_HOST_LOGIN=0` leaves it out.

With none of these, Claude Code asks you to log in, and that login stays
in the VM: every new sandbox asks again.

`resume` refuses the file if other users can read it. While a session
runs, the agent can use the credential through the proxy, but cannot
read it (`SECURITY.md` §4). The proxy's log is
`~/.local/state/wmf-sbx/sandboxes/NAME.credproxy.log` (requests, no
bodies).

## Preferences

The `session:` section of `~/.config/wmf-sbx/settings.yaml` holds
settings for every new sandbox. `create` writes them into the agent's
`~/.claude/settings.json`, so you do not set them again in each one:

```yaml
session:
  model: opus                         # Claude Code's `model` setting
  permission_mode: bypassPermissions  # default, acceptEdits, plan, auto, bypassPermissions
  theme: dark                         # dark, light, dark-daltonized, light-daltonized,
                                      # dark-ansi, light-ansi
  ste: false                          # leave out the ASD-STE100 rule for comments and
                                      # commit messages (default: true)
```

Two more sections of the file apply to every new sandbox:

```yaml
environment:          # variables for the sessions (the agent's settings `env`)
  GERRITUA: "..."
  CLAUDE_CODE_DISABLE_1M_CONTEXT: "1"
packages:             # extra Debian packages in the golden image
  - nano
```

`environment:` cannot set `ANTHROPIC_*` or `NO_PROXY` (the credential
proxy's). `packages:` are part of the golden image's cache key, so a
change builds a new image at the next `create`.

`ste: false` sets `WMF_CLAUDE_STE=0` for the sessions, which takes the
STE writing rule out of the SessionStart context. The `write-commit-msg`
skill still asks for STE when you use it.

`bypassPermissions` lets the agent use every tool without asking. Upstream
`bin/claude` refuses that mode on the command line: on the host it would
also auto-approve the `ask` rules of `wiring/settings-merge.json` (such as
`WebFetch`). In a sandbox the VM, nono and the deny rules, which still
apply, are the boundary, so it is a choice you can make here, once. A
sandbox keeps the settings it was made with; change them in it with
`/model` or `/permissions`, or create a new one.

**Your first sandbox shows every first-run question and warning**:
Claude Code's onboarding (the theme and the security notes), "Do you
trust the files in this folder?", `bin/claude`'s `--landlock-only`
warning, and the bypass-mode confirmation. After its first session,
`resume` writes `~/.local/state/wmf-sbx/first-run-seen`, and from then on
`create` answers them in every new sandbox, so its first session starts
at the prompt: onboarding done, each repo of the sandbox trusted, the
warning acknowledged (`SECURITY.md` §3 says why that is safe in a
sandbox). Delete that file to see them all again. Sessions always skip
`bin/claude`'s "Press Enter" pause.

## Claude Code version

The golden image holds one Claude Code release, and sandboxes do not
update it. By default it is the release on Claude Code's `stable`
channel when the image is built. `WMF_SBX_CLAUDE_VERSION` picks another
one, for `create` and `image build`:

```bash
WMF_SBX_CLAUDE_VERSION=latest wmf-sbx create Cite    # the `latest` channel
WMF_SBX_CLAUDE_VERSION=2.1.296 wmf-sbx create Cite   # an exact release
```

The version is part of the image's cache key, so a new release (or the
`stable` channel moving on) gives a new image at the next `create`.
`wmf-sbx image ls` shows each image's version. A sandbox keeps the
version of the image it was made from; to change it, create a new
sandbox.

## The golden image cache

The image's cache key is a hash of everything that goes into it: the
host uid, the architecture, the Debian base image, the package list,
the nono, Node, php-ast and Claude Code versions, the helper scripts,
the build script, and the wmf-claude checkout's HEAD commit. A change to
any of them builds a new image (about 10 minutes) at the next `create`.
The images are in `${XDG_CACHE_HOME:-~/.cache}/wmf-sbx/images/KEY/`,
about 2.5 GB each.

When you work on wmf-claude, every commit changes the key. To keep the
image while you commit, set `WMF_CLAUDE_GIT_HASH_OVERRIDE`: its value
replaces the commit in the key. Change the value to build a new image
from the current HEAD:

```bash
export WMF_CLAUDE_GIT_HASH_OVERRIDE=v1   # commits no longer rebuild the image
export WMF_CLAUDE_GIT_HASH_OVERRIDE=v2   # rebuild once, from HEAD
```

`WMF_CLAUDE_GIT_HASH_OVERRIDE=latest` uses the newest cached image (for
your machine's architecture and uid) whatever its inputs, as `wmf-sbx
login` does, and builds one only when the cache has none. It ignores
other changes too (a new Claude Code release, `packages:`).

To keep the image you already have, set the override to its commit:
`wmf-sbx image ls` shows it after `wmf-claude`. An image built with an
override has the tree of the HEAD at its build, which `image ls` shows
in parentheses.

When `create` builds a new image, it deletes the images that no sandbox
uses, except those built in the last hour (another `create` can be about
to use one). `wmf-sbx image prune` deletes all unused images.

## Quick start

```bash
wmf-sbx create Cite          # 10-20 min the first time; the image is cached
wmf-sbx resume sbx-cite      # Claude Code, in the primary clone
git -C ~/Wikimedia/Extensions/Cite fetch sbx-cite   # the agent's commits
wmf-sbx rm sbx-cite
```

## Naming a repo

An argument to `create` (and to `resolve`) can be:

- a short name (`Cite`): Gerrit is searched for a project whose last path
  segment matches exactly, then GitLab (`repos` and `toolforge-repos`).
  No match, or more than one, is an error that lists the candidates;
- a project path, with or without a forge (`mediawiki/extensions/Cite`,
  `gerrit:mediawiki/extensions/Cite`, `gitlab:repos/...`);
- a local directory (`.`, `~/src/foo`), which is mapped back to its
  project.

The project is then mapped to a host directory by the `repos:` rules in
`~/.config/wmf-sbx/settings.yaml` ("Getting started"), most specific rule
first, for example `gerrit:mediawiki/extensions/{name}` →
`~/Wikimedia/Extensions/{name}`. `reference/example-settings.yaml` is a
commented example. A repo that is not on the host yet is cloned there
first. (Before `settings.yaml`, the file was `repos.yaml` and `repos:` was
`rules:`; the old names still work, with a note.)

## Dependencies

By default `create` walks `requires`, `dev-requires` and `suggests` in
`extension.json` and `skin.json`, and adds core and Vector. So
`wmf-sbx create Translate` also gives you core, Vector and
UniversalLanguageSelector, linked into core and loaded by the wiki.
`--no-deps` uses only the repos you name; `--no-dev` and `--no-suggests`
narrow the walk.

## `create`

```
wmf-sbx create PRIMARY[:ro] [EXTRA[:ro] ...] [options]
```

- `PRIMARY` is where the session starts. `:ro` makes a repo read-only
  for the agent.
- Repos you name keep the branch and commit of your host checkout.
  Dependencies are reset to upstream `master` (or `main`): the clone in
  the VM fetches the real upstream (Gerrit or GitLab, as `origin`) and
  resets to its tip. `--reset-all` resets the repos you named too. Only
  if upstream cannot be fetched, or wmf-sbx cannot tell what it is (no
  `gerrit:`/`gitlab:` name and no https `origin` on the host), is a repo
  reset to your host checkout's branch instead; `create` warns for each.
- `--name NAME`: the sandbox name (default `sbx-<primary>`). The host
  remote has the same name.
- `--no-remotes`: add no host remote, and leave host gc as it is.
- `--vm-type qemu|vz`, `--cpus N` (4), `--memory SIZE` (8GiB),
  `--disk SIZE` (60GiB).
- `--nested`: **lower security; only for work on wmf-claude or wmf-sbx
  itself.** Do not use it for other work. nono gives the agent more in a
  nested sandbox: `/dev/kvm`, the `kvm` group, Lima's sockets, more
  writable directories and more download hosts (SECURITY.md, §6a). The
  agent can run Lima VMs of its own, under nono (image builds, inner sandboxes, `real-hardware.sh`):
  `create` installs QEMU, OVMF and Lima in this sandbox (the golden image
  has none), adds the agent to the `kvm` group, and the sessions get the
  grants for `~/.lima`, `/dev/kvm` and the download hosts of an image
  build. Defaults: 16 GiB of memory, a 120 GiB (sparse) disk. Nested KVM
  needs the host to allow it (Linux: `kvm_intel`/`kvm_amd` `nested=1`, the
  default on recent kernels; macOS: M3 or later with macOS 15, on `vz`);
  without it the inner VMs run, slowly, without KVM. For example:
  `wmf-sbx create --nested --no-deps ~/src/wmf-claude`.
- `--image KEY`: use a cached golden image (`wmf-sbx image ls`), not the
  one for the current inputs (see also `WMF_CLAUDE_GIT_HASH_OVERRIDE`,
  "The golden image cache").
- `--dry-run`: print the plan, the image key and the Lima config.

`create` stops when a step fails. The VM is kept: fix the cause with
`wmf-sbx exec`, or `wmf-sbx rm` it. A failed per-repo setup step (a
`composer update` or `npm ci`) is only a warning; the agent finds the
list in `~/.wmf-sbx/wmf-sbx-setup.status`.

## Working with Gerrit

1. `wmf-sbx create Cite`, then `wmf-sbx resume sbx-cite`.
2. In the sandbox, the remote `local` is your host checkout (read-only,
   current as of your last fetch). To give the agent your newer work,
   tell it (or run `! git safe-reset local`).
3. The agent commits on a branch; its `~/.claude/CLAUDE.md` tells it to
   end each turn with a commit.
4. On the host: `git fetch sbx-cite`, or `git safe-reset sbx-cite`, which
   refuses to discard a dirty tree or unsubmitted commits without
   `--force`.
5. Review, amend, and `git review` from the host, as usual. The sandbox
   cannot push.

## Tests in the sandbox

The agent's guide is `~/MEDIAWIKI-TESTING.md` (from
`templates/MEDIAWIKI-TESTING.md`). Every repo has `vendor/` and
`node_modules/`; core has a SQLite wiki with every extension loaded;
`composer serve` serves it on port 4000. `mw-install-browser` installs
Chrome for Testing for karma and wdio (not on an aarch64 VM: there is
no Linux arm64 build), and `mw-install-cypress REPO` installs a repo's
Cypress binary.

## Cleaning up

```bash
wmf-sbx rm NAME            # asks first; refuses if the agent has unfetched commits
wmf-sbx rm --force NAME
wmf-sbx rm --prune         # remotes and state of sandboxes whose VM is gone
wmf-sbx image prune        # golden images that no sandbox uses
```

## Troubleshooting

- **`git fetch NAME` fails**: git runs wmf-sbx's remote helper with the
  `PATH` of the shell that runs git, so both must be on it there (in
  `~/.profile` or your shell's rc file, not only in one terminal):
  wmf-claude's `sbx/bin` (else git says "git: 'remote-wmfsbx' is not a
  git command"; `create` warns about it) and Lima's `limactl` (else
  "fatal: wmfsbx: limactl is not on PATH").

- **The host proxy port changed** (a network that gives you a proxy on
  127.0.0.1): `start` and `resume` update a stopped VM. For a running VM,
  `wmf-sbx stop NAME` first.
- **GitHub archive downloads are blocked** on your network: composer
  fails in setup. Run
  `wmf-sbx exec NAME -- composer config --global preferred-install source`
  and create again. Source installs are large (several GB per repo).
- **TLS interception** (a corporate CA): set `WMF_SBX_CA_CERTS` to the CA
  files, separated by `:`, for the image build and the VMs.
- **Disk**: a sandbox with core and a few extensions uses 15-20 GB, and
  each golden image about 2.5 GB.
- **Slow create**: the setup log in the VM
  (`wmf-sbx exec NAME -- grep timing: .wmf-sbx/wmf-sbx-setup.log`) has a
  `timing:` line per `composer update` (split into resolve, install and
  autoload) and per `npm ci`; composer's own profile is in
  `~/.wmf-sbx/timings/`.

## Architecture

**Commands.** `bin/wmf-sbx` dispatches to `python3 -m wmf_sbx VERB`;
the code is in `src/wmf_sbx/`, one module per verb, and the tests in
`tests/`. PyYAML is the only dependency. Lima is driven through
`lima.Limactl`; the per-sandbox state is a JSON file per sandbox under
`~/.local/state/wmf-sbx/` (`state.py`).

**Repo resolution** (`resolve.py`). A name becomes a canonical project
(`resolve_canonical_path`: Gerrit, then GitLab, following forks to the
real upstream), and a canonical project becomes a host directory
(`resolve_directory`, the `repos:` rules of `settings.yaml`). `reverse_resolve` maps a
directory back, from `.gitreview`, the origin URL or an exact rule.

**Dependency walk** (`deps.py`). `deps.walk` reads each repo's manifest,
from the checkout or from gitiles, and adds what it requires; a manifest
that cannot be fetched stops the create. Link names into core
(`extensions/<Name>`, `skins/<Name>`) are decided on the host
(`create.link_plan`) and sent to the VM in the plan.

**Golden image** (`image.py`, `image-build.sh`). A builder VM from a
pinned Debian 13 image installs the packages (PHP, composer, the browser
libraries), the generic kernel (the cloud kernel has no 9p or
virtiofs), Node from nodejs.org, php-ast from source, nono, Claude Code,
the built wmf-claude tree at `/opt/wmf-claude`, and the helpers; then it
seals the image and exports a qcow2. The cache key is a hash of all of
those inputs, including the tree revision and the host uid (the agent's
uid). `~/.cache/wmf-sbx/images/KEY/` holds the read-only image ("The
golden image cache").

**Sandbox VM** (`template.py`, `sandbox-provision.sh`, `vm.py`). Each
sandbox is a Lima instance `wmf-sbx-NAME` with a copy of the golden
image. It has two users: `engineer` (Lima's user, with sudo, drives
setup) and `agent` (no sudo, the host uid). Provisioning at every boot
loads an nftables host block (no host, LAN or link-local addresses; the
agent gets TCP 80 and 443 only, off the VM), narrows the ephemeral port
range (see `SECURITY.md`), and turns TIOCSTI off. `vm.INVARIANTS` are
checked at every start, resume and status.

**Repos in the VM** (`repos.py`, `sandbox-repos.sh`). Each repo's git
dir is a read-only Lima mount (9p on QEMU, virtiofs on `vz`) under
`/run/wmf-sbx/host/`. The VM has a `git clone --shared` of it at the
same absolute path as on the host, so every path (Claude's project key,
`LocalSettings.php`, symlinks) matches. Its remotes are `local` (the
mount) and `origin` (the upstream). A `:ro` repo is cloned by
`engineer`, so the agent can read but not write it.

**VM to host** (`remotes.py`, `remote_helper.py`). `create` adds a
remote named after the sandbox to each host repo, with the URL
`wmfsbx://NAME/PATH`. `git-remote-wmfsbx` serves only
`git-upload-pack`, run as the agent through `limactl shell`, with hooks
off: the host can fetch, nothing can push. While the sandbox exists, gc
in the host repo is suspended, because the clones borrow its objects.
`rm` refuses to remove a sandbox with commits the host has not fetched.

**MediaWiki setup** (`mediawiki.py`, `setup.py`, `kit.py`). The host
builds a plan (repos, canonical names, links, `:ro` flags) and sends it
with `setup.py`, which runs in the VM as the agent: links into core,
`composer.local.json` (extension libraries merged into core's
`vendor/`, as CI does), `composer update` and `npm ci` per repo, the
SQLite install with every extension, `phpunit.xml` and the api-testing
config. Per-repo steps warn; core steps are fatal. `setup.py` is sent on
stdin, so a change to it needs no new image.

**Session** (`session.py`, `resume.py`). `resume` checks the
invariants, starts the credential proxy (`credproxy.py`), opens the
guest's host block to its port, puts the proxy's address and the
stand-in credential in a 0600 file in the guest's `/dev/shm` (a wrapper
reads and deletes it), and runs upstream's
`bin/claude` as the agent in the primary clone. That is nono with the
wmf-engineer profile, plus per-session grants: the other clones, the
caches, the system paths the tools read, the package registries, the
local ports. `WMF_CLAUDE_SANDBOX_BACKEND=lima-sbx` selects the
SessionStart text in `hooks/context/lima-sbx/`. The wiki variables
(`MW_SERVER`, `CHROME_BIN`, ...) and the engineer's git identity are in
the `env` key of the agent's `~/.claude/settings.json`, which `create`
writes with `~/.claude/CLAUDE.md` and `~/MEDIAWIKI-TESTING.md`.
`wmf-claude-setup` runs at create time to register the anonymous MCP
servers (Phabricator, Gerrit, GitLab) in the VM.
