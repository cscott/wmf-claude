# wmf-sbx

One command makes a disposable MediaWiki development sandbox: a Lima VM
with clones of the repos you name and their MediaWiki dependencies, a
working wiki, and the wmf-claude plugin. Claude Code runs in it as an
unprivileged user under nono, or, with `create --sudo`, as a user with
sudo and no nono, behind the host's egress proxy ("Two modes"). The host
gets a git remote to fetch the agent's work.

- `SECURITY.md`: what the agent can and cannot reach.
- `NOTES.md`: open to-do items, and the decisions and findings that the
  code depends on.
- `MACOS.md`: the tests that check wmf-sbx on a Mac, and what macOS
  still lacks (copy-on-write disks and the egress proxy on `vz`).
- `../lima-port/HANDOFF-LIMA.md`: the port's decisions (D1 to D12) and
  the record of the work after the MVP (tracks A, B, C and L).

## Install

On the host you need:

- [Lima](https://lima-vm.io/) 2.x. On Linux also QEMU (`qemu-system-x86`
  or `qemu-system-arm`, `qemu-utils`) and, for speed, KVM. On macOS Lima
  uses `vz`.
- For the egress proxy (QEMU; see "The egress proxy"):
  [nono](https://github.com/nolabs-ai/nono) and `openssl`. Without them,
  `create` warns and makes a sandbox that is not behind the proxy.
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
| `resume NAME` | start Claude Code in the sandbox |
| `exec NAME -- CMD` | run a command in the VM as the agent, outside nono, with a session's variables (`$MW_INSTALL_PATH`, ...) |
| `allow NAME HOST...`, `logs NAME` | let a sandbox behind the egress proxy reach more hosts; show what the proxy allowed and denied ("The egress proxy") |
| `cp [-r] SRC... DST` | copy files or directories between the host and a sandbox, or between two sandboxes (`NAME:PATH`); `-r` is optional. Several sources need a directory DST; a quoted `'NAME:*.txt'` is expanded in the sandbox |
| `ls`, `status NAME` | list sandboxes; check one (VM, image, security invariants) |
| `start NAME`, `stop NAME` | boot or stop the VM; nothing is lost |
| `rm NAME` | remove the VM, its state and its host remotes |
| `image build/ls/rm/gc/prune` | manage the cached golden images |
| `login` | sign in to Claude once, for every sandbox ("Sign-in") |
| `resolve`, `ls-remotes` | show how a repo name resolves; list a repo's sandbox remotes |

A `NAME` can also be a path shortcut such as `.`: the sandbox whose
primary repo is that directory.

## Getting started

1. **Tell wmf-sbx where your repos are.** wmf-sbx names repos by their
   Gerrit or GitLab project (`gerrit:mediawiki/extensions/Cite`); rules in
   `~/.config/wmf-sbx/settings.yaml` map projects to directories on your
   machine. A sandbox works on clones of those directories. `create`
   offers to clone a repo that is not there yet (`--clone-missing` clones
   without asking). Start from the example
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
   so on a new machine make the first sandbox with `create --no-start`,
   sign in, then `wmf-sbx resume` it.

3. **Make a sandbox; `create` then starts Claude Code in it:**

   ```bash
   wmf-sbx create Cite        # 10-20 minutes the first time (the golden image)
   wmf-sbx resume sbx-cite    # later sessions
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
  settings: false                     # take no option defaults from this file
                                      # ("Defaults from settings.yaml"; default: true)
  native_copy: false                  # a copy in Claude Code does not reach the
                                      # host's clipboard ("Copy"; default: true)
```

`create --sudo` by default is `create: {sudo: true}`, not a `session:`
key: see "Defaults from settings.yaml".

Two more sections of the file apply to every new sandbox:

```yaml
environment:          # variables for the sessions (the agent's settings `env`)
  GERRITUA: "..."
  CLAUDE_CODE_DISABLE_1M_CONTEXT: "1"
packages:             # extra Debian packages in the golden image
  - nano
```

`environment:` cannot set `ANTHROPIC_*` or `NO_PROXY` (the credential
proxy's). With `GERRITUA` set, the session-start hook tells Claude to send
it as the User-Agent to gerrit.wikimedia.org, for the higher rate limit,
and to treat it as a secret; the hook never shows the value. `packages:` are part of the golden image's cache key, so a
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

## Copy

A mouse selection in Claude Code goes to the host's clipboard: Ctrl+V
and middle click on the host paste it. Claude Code copies with
`wl-copy`, and sends OSC 52 too, which GNOME Terminal drops; in the VM,
`create` puts a `wl-copy` of wmf-sbx's in `~/.local/bin` and sets
`WAYLAND_DISPLAY=wmf-sbx` (a name only: there is no display) in the
agent's settings `env`. For the length of a session, `resume` listens
for that shim on the host, and gives the text to the host's `wl-copy`
(Wayland), `xclip` (X11) or `pbcopy` (macOS). Its log, a line per copy
with the size and never the text, is
`~/.local/state/wmf-sbx/sandboxes/NAME.clipboard.log`.

- Only behind the egress proxy, for now (not `vz` or
  `--no-egress-proxy`). A sandbox made before this gets it when made
  again.
- Copy only. Paste from the keyboard (Ctrl+Shift+V) and Shift+middle
  click work as they do in any terminal; middle click, right click and
  Ctrl+V in Claude Code read the clipboard, and still paste nothing.
- With no display on the host (an SSH login), `resume` says so and the
  session starts without it.
- The agent can set your clipboard while a session runs (`SECURITY.md`
  §6): check a command before you paste it into a shell.
  `session: {native_copy: false}` leaves out the shim in new sandboxes,
  and the listener for every sandbox.

## Defaults from settings.yaml

Every option of every command takes its default from
`~/.config/wmf-sbx/settings.yaml`; `logs` has no options of its own (its
arguments are `tail`'s), and `resume`'s launcher flags and Claude Code
arguments, after its `--`, take none. For `--some-flag` of `wmf-sbx VERB`,
the key is `VERB.someFlag` (the option's name in camelCase). A
subcommand nests:

```yaml
create:
  cpus: 8
  cloneMissing: true
  vmType: qemu
rm:
  scan: [~/Wikimedia]       # a list option takes a list
image:
  build:
    keepBuilder: true
```

- The command line wins over settings.yaml, which wins over the code's
  own default. Each flag is a pair, so a default from settings can be
  undone: `--no-cold`, `--no-force`, `--no-dry-run`. A list on the
  command line replaces the list from settings.
- `null` is the same as no key: wmf-sbx chooses, as it does with nothing
  set (for `vmType`, `memory`, `egressProxy`...).
- `--no-settings` (after the verb) ignores these defaults for one
  command; `session: {settings: false}` ignores them for every command,
  and `--settings` then turns them on for one. This switch is for option
  defaults only: `repos:`, `environment:`, `packages:` and `imageCache:`
  are always read. `--config PATH` reads another file.
- `--help` shows each option's key, and the value from settings when
  there is one: `[settings: create.cpus: 8]`.
- A key that the command does not have, an unknown section, or an
  unknown `session:` key gets a warning and is ignored, so a stable and
  a development wmf-sbx can share one file. A value of the wrong type
  (`cold: yes`, `cpus: four`) is an error.
- A value in settings.yaml is a request from you, as one on the command
  line is: wmf-sbx does not second-guess it. `create: {sudo: true}`,
  `create: {nested: true}`, `exec: {engineer: true}` and
  `rm: {force: true}` lower safety for every command that does not undo
  them; `create: {memory: 1GiB}` gets 1 GiB, `--nested` or not. Caveat
  emptor.

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

One image is built at a time per key. When a second `create` (or `image
build`) needs an image that another one is building now, it asks: wait
for that build and use its image (the default), delete the other
builder and build here, or abort. When stdin is not a terminal, it
aborts, with exit status 1. A builder that a crashed build left behind
is deleted without a question.

When you work on wmf-claude, every commit changes the key. To keep the
image while you commit, set `WMF_SBX_GIT_HASH_OVERRIDE`: its value
replaces the commit in the key. Change the value to build a new image
from the current HEAD:

```bash
export WMF_SBX_GIT_HASH_OVERRIDE=v1   # commits no longer rebuild the image
export WMF_SBX_GIT_HASH_OVERRIDE=v2   # rebuild once, from HEAD
```

(Its first name, `WMF_CLAUDE_GIT_HASH_OVERRIDE`, still works for now,
with a note.) `WMF_SBX_GIT_HASH_OVERRIDE=latest` uses the newest cached image (for
your machine's architecture and uid) whatever its inputs, as `wmf-sbx
login` does, and builds one only when the cache has none. It ignores
other changes too (a new Claude Code release, `packages:`).

To keep the image you already have, set the override to its commit:
`wmf-sbx image ls` shows it after `wmf-claude`. An image built with an
override has the tree of the HEAD at its build, which `image ls` shows
in parentheses.

A golden image older than 14 days (settings.yaml `imageCache: {maxAge:
14d}`) is built again at the next `create`, with the same inputs, so
that new sandboxes get the Debian updates since. `--no-expire` (or
`imageCache: {expire: false}`) keeps the old one with a warning, for a demo
that must not wait for a build.

At its end, `create` runs `wmf-sbx image gc`: it deletes the images that
no sandbox uses and that are older than 7 days (settings.yaml `imageCache:
{gcAge: 7d}`). `wmf-sbx image prune` deletes every unused image, to free
disk space. `wmf-sbx image ls` lists the images and snapshots, and ends
with what they and the sandboxes' own disks take on disk, and what
`image prune` would free.

On QEMU (the default on Linux), a sandbox's disk is a copy-on-write
overlay on the golden image: a new sandbox takes megabytes, not the
image's gigabytes, and the golden image must stay where it is, read-only
(`wmf-sbx status` checks both). An image that a sandbox's disk sits on
is in use, with or without a state file. `--full-disk` gives the sandbox
a full copy instead, as on `vz` (macOS's default), which cannot boot an
overlay. This relies on how Lima 2.2.1 behaves; `create` warns on any
other Lima version.

On QEMU, `wmf-sbx` starts the VM through `sbx/bin/wmf-sbx-qemu`, which
adds free-page reporting: the memory a sandbox frees goes back to your
machine, so a sandbox that once ran a big build does not hold all its
memory while it idles. The wrapper also puts the git-dir mounts 8 to a
PCI slot. Start a sandbox with `wmf-sbx start`: a plain `limactl start`
runs it without free-page reporting, and does not boot a sandbox of
more than about 24 repos.

### Warm snapshots

On QEMU, each `create` ends by freezing the sandbox's disk as a
**snapshot** of its set of repos, before any session runs in it: the
clones, composer's `vendor/`, `node_modules` and the installed wiki. The
next `create` of the same set of repos (the same host paths, the same
`:ro`s, the same golden image, `--nested` or not, behind the egress
proxy or not) starts from that
snapshot: it fetches, checks out and updates only what changed (`git
fetch`, `composer update`, `npm ci` only where `package-lock.json`
changed, `update.php`). If the snapshot it started from is older than a
day (settings.yaml `imageCache: {snapshotInterval: 1d}`), it then freezes
the result as the newest snapshot; within the day it does not, and the
next create fetches and updates a little more instead.
How a repo got in (named, or as a dependency) does not matter, so the
dependency flags change the snapshot only by changing the set.
A sandbox behind the egress proxy (the default on QEMU) and one that is
not have separate snapshots: the guest's side of the proxy (its resolver
and proxy settings) is on the disk. Each sandbox gets its own egress CA;
the freeze removes the one that made the snapshot.

- The freeze stops and starts the sandbox once, at the end of `create`,
  and writes a new snapshot of a few GiB; `create` says how long it
  took. A cold create always freezes; `snapshotInterval: 0m` freezes at
  every create.
- `--cold` ignores the snapshot and makes a new one (and builds a new
  golden image first if the current one is old). `--no-snapshot`
  neither uses nor makes one.
- A snapshot's clones borrow your host repos' objects, as a sandbox's
  do, so a host repo's gc stays off while a snapshot of it exists (the
  repo lists `snapshot-KEY` in `git config wmfSbx.borrower`). A host
  repo that you delete or clone again no longer lists it, and the next
  `create` of it is cold.
- Snapshots are cached images: `image ls` lists them, `image gc`
  removes those that no sandbox uses and that are older than
  `imageCache.gcAge` (so a set of repos you have not created for a week is
  cold again), and `image prune` removes every unused one. A snapshot
  keeps its golden image.
- A new golden image (every 14 days, or a new wmf-claude) starts new
  snapshots: the first `create` of each set after it is cold.

## Quick start

```bash
wmf-sbx create Cite          # 10-20 min the first time; then Claude Code starts in it
wmf-sbx resume sbx-cite      # Claude Code again later, in the primary clone
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
first, if you agree: `create` lists those repos and asks before it clones
them. With no terminal to ask on, it stops with an error.
`--clone-missing` (or `create: {cloneMissing: true}` in settings.yaml)
clones them without asking; `--no-clone-missing` overrides that setting.
After a Gerrit clone, `create` runs `git review -s` in it, for the
`gerrit` remote and the commit-msg hook (if git-review is not installed,
it warns). (Before `settings.yaml`, the file was `repos.yaml` and
`repos:` was `rules:`; the old names still work, with a note.)

## Dependencies

By default `create` walks `requires`, `dev-requires` and `suggests` in
`extension.json` and `skin.json`, and adds core and Vector. So
`wmf-sbx create Translate` also gives you core, Vector and
UniversalLanguageSelector, linked into core and loaded by the wiki.

It also adds what WMF CI checks out when a repo you name is under test,
which no manifest declares (as of October 2026: for Cite, ParserFunctions, Popups,
VisualEditor and six more; for Parsoid, ParserFunctions, Scribunto and
TimedMediaHandler). Without them some of the repo's own tests fail. `create`
reads CI's lists from integration/config (`zuul/dependencies.yaml`, and
`zuul/phan_dependencies.yaml` for CI's phan job) on gitiles, once per
`create`: two requests, about 90 KB. If gitiles cannot be reached, it
warns and goes on without them. settings.yaml `extra_dependencies:` adds
your own.

`--no-deps` uses only the repos you name; `--no-ci-deps` leaves out CI's
lists, and `--no-phan-deps` only the phan job's part of them;
`--no-dev` and `--no-suggests` narrow the walk.

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
  reset to your host repo's `master` (or `main`) instead, which may be
  behind upstream; `create` warns for each.
- `--name NAME`: the sandbox name (default `sbx-<primary>`). The host
  remote has the same name. A `create` holds its name from the start, so
  a second `create` of the same name, while the first runs, stops at once
  (a default name gets a suffix instead).
- `--no-remotes`: add no host remote, and leave host gc as it is.
- `--start/--no-start`: start Claude Code in the new sandbox at the end,
  as `wmf-sbx resume` does. By default `create` starts it when stdin and
  stdout are a terminal, and not in a script or a pipe.
- `--clone-missing`: clone the repos that are not on the host yet without
  asking ("Naming a repo", above).
- `--cold`, `--no-snapshot`, `--no-expire`: see "Warm snapshots" and
  "The golden image cache". `--full-disk`: a full copy of the golden
  image on QEMU too, and no snapshot.
- `--vm-type qemu|vz`, `--cpus N` (4), `--memory SIZE` (8GiB),
  `--disk SIZE` (60GiB).
- On QEMU a sandbox takes at most 96 repos (git dirs mounted, the
  dependencies counted): each mount is a PCI device function, 8 to a
  slot of the VM's bus. `create` refuses more; `--no-phan-deps`,
  `--no-ci-deps` or `--no-dev` leave dependencies out.
- `--no-egress-proxy`: the VM is not behind the host's egress proxy
  ("The egress proxy"). `--egress-proxy` makes `create` fail, not warn,
  when the proxy cannot be used.
- `--sudo`: the agent has passwordless sudo ("Two modes"). It needs the
  egress proxy, so QEMU, and cannot be combined with `--no-egress-proxy`.
  `--no-sudo` overrides `create: sudo: true` in `settings.yaml`.
- `--nested`: **lower security; only for work on wmf-claude or wmf-sbx
  itself.** Do not use it for other work. nono gives the agent more in a
  nested sandbox: `/dev/kvm`, the `kvm` group, Lima's sockets, more
  writable directories and more download hosts (SECURITY.md, §6a). The
  agent can run Lima VMs of its own, under nono (image builds, inner sandboxes, `real-hardware.sh`):
  `create` installs QEMU, OVMF and Lima in this sandbox (the golden image
  has none), adds the agent to the `kvm` group, and the sessions get the
  grants for `~/.lima`, `/dev/kvm` and the download hosts of an image
  build. It also writes a `settings.yaml` for the inner wmf-sbx (repos
  in `~/wmf-sbx-src`, at your home's path as on the host) if there is
  none. Defaults: 14 GiB of memory, a 120 GiB (sparse) disk; a sandbox
  made inside it defaults to 8 GiB (one inner VM at a time fits). Nested KVM
  needs the host to allow it (Linux: `kvm_intel`/`kvm_amd` `nested=1`, the
  default on recent kernels; macOS: M3 or later with macOS 15, on `vz`);
  without it the inner VMs run, slowly, without KVM. A session cannot
  stop an inner VM that an earlier session started, so `resume` first
  stops the inner VMs that still run (not while another session runs in
  the sandbox); start them again in the session. For example:
  `wmf-sbx create --nested --no-deps ~/src/wmf-claude`. An inner
  sandbox's session spends the enclosing session's credential: each
  `resume` of a nested sandbox writes its session's phantom and proxy
  address into the marker `/etc/wmf-sbx/nested`, and `resume` in the
  sandbox takes them as its host's credential and chains to that proxy
  (SECURITY.md, §4). `$CLAUDE_CODE_OAUTH_TOKEN` and `$ANTHROPIC_BASE_URL`
  override them.
- `--image KEY`: use a cached golden image (`wmf-sbx image ls`), not the
  one for the current inputs (see also `WMF_SBX_GIT_HASH_OVERRIDE`,
  "The golden image cache").
- `--dry-run`: print the plan, the image key and the Lima config.

`create` stops when a step fails. The VM is kept: fix the cause with
`wmf-sbx exec`, or `wmf-sbx rm` it. A failed per-repo setup step (a
`composer update` or `npm ci`) is only a warning; the agent finds the
list in `~/.wmf-sbx/wmf-sbx-setup.status`.

## The egress proxy

On QEMU, a sandbox is by default behind an **egress proxy on the host**:
the VM has no network route and no DNS, and its one way out is a
`nono proxy` that the QEMU wrapper runs on the host. The guest cannot
change the policy, even as root. The proxy allows the `wmf-engineer`
profile's hosts (with its read-only endpoint rules for the wikis), the
package registries and Debian's mirror; anything else gets `403`.

```bash
wmf-sbx allow NAME example.org       # at once, also in a running session
wmf-sbx allow NAME '*.example.org'
wmf-sbx allow NAME --list            # or --rm HOST
wmf-sbx logs NAME                    # the proxy's log; -f follows it (tail's options)
```

The agent in the sandbox is told to ask you for `wmf-sbx allow` when a
host it needs is denied.

`create` makes a sandbox that is not behind the proxy, with a warning
that the guest then enforces its network policy itself (nono and
nftables in the VM, as before), when:

- the VM is `vz` (macOS): the proxy needs QEMU's `restrict=on` network;
  `--vm-type qemu` puts it behind the proxy;
- the host lacks `qemu-system-ARCH`, `nono` or `openssl` on its `PATH`.

`--no-egress-proxy` asks for that on purpose, with no warning; an
explicit `--egress-proxy` turns the warning into an error.

## Two modes

**Contained** (the default): the agent has no sudo, and each session
runs upstream's `bin/claude`, so nono, inside the VM. After `resume`'s
`--`, the words are `bin/claude`'s, by its own rule: its flags before a
second `--`, Claude Code's after it. So
`wmf-sbx resume sbx-cite -- --allow-post=HOST --` continues the
conversation with one more grant, and `-- -p "..."` passes Claude Code
its arguments. Without arguments for Claude Code, `resume` continues
the last conversation once there is one; `resume --continue` always
does (when the first session ended without a clean exit, the sandbox
does not know it had one), and `resume --no-continue` starts a new
one. Leave `resume.continue` out of settings.yaml: `true` fails on a
new sandbox, which has nothing to continue.

**`--sudo`**: the agent has passwordless sudo (a file in
`/etc/sudoers.d`; it is in neither the `sudo` nor the `docker` group),
and each session runs Claude Code with the wmf-claude plugin and the
tool layer of `wiring/settings-merge.json`, but with no nono. The agent
can `sudo apt-get install` what a task needs, through the proxy. The
boundary is the VM and the host's egress proxy, which root in the guest
cannot change; the proxy still applies the profile's endpoint rules (a
wiki POST gets `403`). So `--sudo` needs the proxy, and `create` fails
rather than fall back to the guest's own rules. A `--sudo` sandbox takes
no launcher flags (`wmf-sbx allow` adds a host), and `status` and `ls`
show its mode. The invariants still run at every start, but root in the
guest can make them pass; `SECURITY.md` §6b says what that changes.

```bash
wmf-sbx create --sudo ~/src/Cite
```

## Working with Gerrit

1. `wmf-sbx create Cite` (it starts a session; `wmf-sbx resume sbx-cite` later).
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
`templates/MEDIAWIKI-TESTING.md`). Core and the repos you named have
`vendor/` and `node_modules/` (a dependency has neither of its own, as in
CI: the agent runs `composer update` and `npm ci` in one it changes); core has a SQLite wiki with every extension loaded;
`composer serve` serves it on port 4000. `mw-install-browser` installs
Chrome for Testing for karma and wdio (not on an aarch64 VM: there is
no Linux arm64 build), and `mw-install-cypress REPO` installs a repo's
Cypress binary.

## Cleaning up

```bash
wmf-sbx rm NAME            # asks first; refuses if the agent has unfetched commits
wmf-sbx rm --force NAME
wmf-sbx rm --prune         # remotes and state of sandboxes whose VM is gone
wmf-sbx rm --scan DIR      # remotes and gc entries, in the repos under DIR, of sandboxes with no state file
wmf-sbx image gc           # golden images that no sandbox uses, older than imageCache.gcAge
wmf-sbx image prune        # every golden image that no sandbox uses
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
sandbox is a Lima instance `wmf-sbx-NAME` whose disk is a copy-on-write
overlay on the golden image on QEMU (`disk.py`), and a copy of it on
`vz`. It has two users: `engineer` (Lima's user, with sudo, drives
setup) and `agent` (no sudo unless `--sudo`, the host uid). Provisioning at every boot
loads an nftables host block (no host, LAN or link-local addresses; the
agent gets TCP 80 and 443 only, off the VM), narrows the ephemeral port
range (see `SECURITY.md`), and turns TIOCSTI off. `vm.INVARIANTS` are
checked at every start, resume and status.

**The egress proxy** (`qemu_wrapper.py`, `egress.py`, `fakedns.py`,
`allow.py`; `SECURITY.md` §3). On QEMU, `limactl start` runs
`bin/wmf-sbx-qemu` as `QEMU_SYSTEM_<ARCH>`. The wrapper passes Lima's
probe calls to the real QEMU unchanged; for the boot call it refuses any
network but one `-netdev user`, adds `restrict=on` (no route out, no
DNS) and three `guestfwd`s on 192.168.5.100, and runs QEMU as its child:

- 3128: a `nono proxy` with `profiles/wmf-engineer.json`, the package
  registries, Debian's mirror and the hosts of `wmf-sbx allow`. It is
  the same policy as a contained session's nono, endpoint rules
  included, so there is one allowlist. Each sandbox has its own CA
  (`egress.make_ca`), which Lima's `caCerts` puts in the guest at
  create. `egress.proxy_argv` writes the proxy's argv to
  `proxy.json` in `~/.local/state/wmf-sbx/egress/NAME/` at each start,
  so a start picks up a new allowlist. When the host itself is behind
  a proxy that needs credentials, `egress.AuthShim` (a thread of the
  wrapper) adds them: `nono proxy --upstream-proxy` sends none.
- 53: `fakedns.py`, a thread of the wrapper. A contained session's
  nono looks up each name before it sends the name in its upstream
  CONNECT, and fails when the lookup fails. The resolver gives every
  name its own address in 198.18.0.0/15 and keeps the mapping for the
  sandbox's life; it never asks a real resolver, which would be a DNS
  tunnel past the allowlist. glibc reaches it over TCP (`options
  use-vc`; a guestfwd is TCP only), and systemd-resolved is masked.
- 3129: the session's credential proxy, on `cred.sock` (one session
  at a time).

The wrapper owns the proxy: it starts it before QEMU, stops it when
QEMU exits, and on Linux gives both `PR_SET_PDEATHSIG`. So the proxy
lives exactly as long as the VM, whoever stops it, with no pidfile to
go stale. `nono proxy` cannot change its allowlist while it runs, so
`wmf-sbx allow` writes the list, signals the wrapper (SIGUSR1), and the
wrapper starts a new proxy and rewrites the port file that the guestfwd
command reads at each connection: new connections go to the new proxy,
and open ones finish on the old. The guest has no NTP, and nono's leaf
certificates start at "now", so `start` waits for Lima's first time
sync (`egress.TIME_SYNC_DONE`) before it returns. `create` falls back
to the guest's own route, with a warning, where the proxy cannot run
(`vz`, missing host tools), and `--no-egress-proxy` asks for that.

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
`vendor/`, as CI does), `composer update` in core and in each named repo
(not in a dependency), `npm ci` the same way, the
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
servers (Phabricator, Gerrit, GitLab) in the VM. Behind the egress
proxy, the session's nono allows every host (`--allow-domain '*'`) and
chains to the egress proxy (`--upstream-proxy`), so that proxy is the
only allowlist and `wmf-sbx allow` reaches a running session; nono
still applies the profile's endpoint rules.

**`--sudo` sessions** (`session.sudo_launcher_argv`; `SECURITY.md`
§6b). The provisioning gives the agent a sudoers file and the marker
`/etc/wmf-sbx/sudo` (a contained sandbox removes both at each boot),
and `vm.invariants` swaps the no-sudo checks for sudo ones. A session
runs `bin/launch-claude.sh` with the plugin and
`wiring/settings-merge.json`, with no nono: its network is the egress
proxy's variables, and `NODE_EXTRA_CA_CERTS` names the system bundle,
which holds the egress CA. `WMF_CLAUDE_SANDBOX_BACKEND=lima-sbx-sudo`
selects its SessionStart text. A nested `--sudo` sandbox has no nono
proxy on the guest's loopback for inner VMs to reach, so a root socket
unit (`wmf-sbx-egress-forward.socket`, `systemd-socket-proxyd`) forwards
127.0.0.1:3128 to the egress proxy, and inner VMs trust the CAs that
Lima put in the outer VM.
