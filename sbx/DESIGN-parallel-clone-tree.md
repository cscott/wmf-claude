# The parallel clone tree (Layer B): one writable clone per repo

**Status: implemented.** `sbx/src/wmf_sbx/create.py`, `kit.py`, `setup.py`,
and `remotes.py` carry this end to end: the writable-parallel-clone-tree
mechanism, its `:ro` opt-out (including the always-relock-the-original
behavior), port selection, and the git daemon `wmf_sbx_setup` starts once
the clones are in place. `wmf-sbx-create` wires the resulting
`sandbox-<name>` remotes into each host source repo automatically (see
`sbx/DESIGN-host-remotes.md`) rather than just printing the command.

This document covers the architecture and design rationale only. For the
security consequences of the mount and daemon choices below — which
mounts are an accident guard versus an enforced boundary, what a restart
leaves behind, and the daemon's trust boundary — see `sbx/SECURITY.md` §3
("Read-only mounts: two layers, only one of them holds"), §4 ("What a
restart leaves behind"), and §5 ("The parallel tree and its git daemon").
This document is cross-referenced from there rather than duplicating that
analysis.

This design supersedes an earlier sketch (recorded in project history)
that assumed the clone workspace would live in a host-mounted "outbox"
directory, so the remote could be a plain filesystem path with no
git-daemon, no port, and no restart handling. That assumption doesn't
hold: the parallel clone tree lives on the sandbox's own writable
overlay — so that ordinary `~/...`-relative paths and tooling work
unmodified inside the sandbox, see §1 below — not on a host-visible bind
mount, so a plain-path remote isn't available and §3 below has to use a
network transport instead.

## 1. Problem

`sbx create --clone` only accepts one target: the rest of the workspace
positionals are read-only bind mounts. That's fine for a single primary
repo, but MediaWiki development regularly spans N repos at once (core +
extensions + skins + Parsoid + config), and each of them needs to be a
real, writable, push-back-*fetchable* clone — not just a read-only mirror
of the host copy — without `sbx` growing multi-target `--clone` support
it doesn't have.

## 2. Design: a parallel clone tree under the sandbox's own `$HOME`

The sandbox's user is always `agent` (uid 1000), `$HOME=/home/agent`,
regardless of the host's username or home directory. A plain
(non-`--clone`) extra directory is bind-mounted at its **literal host
path** (e.g. `/home/cananian/Projects/Wikimedia/core` stays at that exact
path inside the sandbox) — so only paths that happen to already live
under the *sandbox's* `/home/agent` resolve via `~/...`; anything mounted
from a differently-named host home directory does not.

For every resolved repo directory that lives under the *host's* `$HOME`,
compute a parallel path by substituting the sandbox's `$HOME` for the
host's:

```python
def parallel_path(host_home, sandbox_home, resolved_dir):
    """None if resolved_dir isn't under host_home -- such paths keep their
    literal bind-mount location unchanged; there's no ~/-relative meaning
    to translate."""
    rel = os.path.relpath(resolved_dir, host_home)
    if rel.startswith(".."):
        return None
    return os.path.join(sandbox_home, rel)
```

`host_home` is `os.path.expanduser("~")` on the machine running
`wmf-sbx-create` — the launcher already runs host-side, before `exec`ing
`sbx`, so this needs no new information. As a sanity check (not the
mechanism itself — the mechanism is plain prefix substitution against the
real `$HOME`, robust to any host home layout), assert `host_home` starts
with `/home/` or `/Users/` and refuse to translate otherwise, since a
`$HOME` that doesn't look like a normal single-user machine account is a
sign something about the assumption doesn't hold and translating blindly
risks a path collision.

**Writable clone is the default, not opt-in.** A read-only bind mount
makes host-side writes (or another sandbox's writes, for a shared
directory) immediately visible inside the sandbox, which is a real source
of cross-task interference — the isolation a sandbox is supposed to
provide is only as good as its least-isolated mount. So every
repo/dependency named on the `wmf-sbx-create` command line gets a
writable, isolated `--reference` clone into the parallel tree by default.
A plain read-only bind mount into the same parallel location is available
as an explicit, disk-saving opt-out, not the default.

**The existing `:ro` convention, reused across both layers.** An
interspersed `--readonly` flag was considered and rejected as ambiguous
(unclear whether it scopes to the argument before, after, or everything
on one side); the design keeps an `ARG:ro` suffix instead. Any repo
argument, primary or extra, ending in `:ro` opts that one repo out of the
default writable clone and into a read-only bind mount at its parallel
path instead — e.g. `wmf-sbx-create core Parsoid:ro Skins`.

The suffix selects the parallel-tree *strategy* only. Independently of
it, `wmf-sbx-create` appends `:ro` to **every** extra on the `sbx create`
line: nothing is ever supposed to write to a host-mirrored original — the
writable copy is the parallel tree — and create-time `:ro` is the only
layer that actually enforces that (an earlier draft had `wmf-sbx-create`
swallow the suffix entirely, which let a real write-to-original bug
through; extending `:ro` to unsuffixed extras closed the same hole for
the clone path, which only ever reads the original). The primary is the
one exception, because `sbx` rejects the suffix there outright — see
"Dropping `--clone`" below; `wmf-sbx-create` warns if the user asks for
it anyway.

**Default path — writable clone.** For every repo without a `:ro` suffix,
inside the sandbox:

```
git clone --reference <literal-host-path> <literal-host-path> <parallel_path>
```

— cheap (shares objects via the reference), but produces its own writable
working tree in the parallel path, isolated from anything the host (or
any other sandbox sharing that same host directory) does to the original
after this point.

**Read-only opt-out.** For every repo with a `:ro` suffix, skip the clone
and bind-mount instead, preserving the parallel path structure without
paying for a second copy of the objects. Because (per "Dropping
`--clone`" below) `sbx` itself always mounts every repo read-write at its
literal host path, this takes two mounts, not one — first lock down the
pristine original in place, then propagate that into the parallel tree:

```bash
sudo mount -o remount,ro,bind <literal-host-path>
mkdir -p <parent of parallel_path>
sudo mount --bind <literal-host-path> <parallel_path>
sudo mount -o remount,ro,bind <parallel_path>
```

The two-step bind-then-remount is required for *each* of the two mounts:
a plain `mount --bind -o ro` in one step is silently ignored by the
kernel (the mount comes back read-write), and the `remount,ro,bind`
second step is what actually enforces it. This also needs `sudo` where
the clone path doesn't — a real, if currently low-cost, asymmetry worth
documenting rather than assuming away for some other sudo policy. What
this two-step remount does and doesn't protect against — and why it's
different in kind from the create-time `:ro` `sbx` itself applies to
every extra — is `sbx/SECURITY.md` §3's subject, not this document's.

**Dropping `--clone` for the primary.** Confirmed against a real `sbx`
binary: `sbx create --name foo claude \`realpath .\`:ro` fails with
`ERROR: primary workspace must be read/write (remove ':ro' or
':readonly')`. The primary positional is unconditionally read/write —
`sbx` rejects a `:ro` (or `:readonly`) suffix on it outright, with or
without `--clone`, so there is no way to hand `sbx` a read-only primary
directly. The design responds by not fighting this: `--clone` is dropped
entirely, and the primary is passed to `sbx create` as a plain,
read/write positional at its literal host path. `wmf-sbx-create` does
100% of the reference-cloning itself, and `sbx`'s own
`/run/sandbox/source`-plus-clone indirection for `--clone` never comes
into play.

One primary/extra asymmetry survives, and it is `sbx`'s, not this
design's: extras carry their `:ro` through to `sbx create` (the only
layer that actually enforces it), while the primary cannot, because of
the error above. A `:ro` primary therefore gets the in-sandbox remount
only; `wmf-sbx-create` warns rather than letting that look enforced, and
suggests passing it as an extra instead. See `sbx/SECURITY.md` §3 for the
current state of closing this gap upstream. Dropping `--clone` also
collapses what would otherwise be a "two daemons" problem to one: `sbx`
is never asked for `--clone`, so it never starts its own single-repo
daemon, and the daemon in §3 below is the *only* one.

## 3. Making the parallel clones fetchable from the host

The parallel clones live in the sandbox's writable overlay, not on a
host-visible mount, so the remote has to be a network endpoint rather
than a plain filesystem path.

`sbx`'s own git-daemon for the (now-unused) `--clone` primary is
deliberately scoped to just that one repo: it runs as `git daemon
--export-all --base-path=<parent-dir> <primary-target>`, and a sibling
directory under the same `--base-path` that isn't the `--clone` target
gets `access denied or repository not exported` from that same daemon.
There's no piggybacking on it for the parallel tree, even if `--clone`
were still in play.

Instead, an unprivileged second `git daemon` process serves the whole
tree: `git daemon --reuseaddr --export-all --base-path=/home/agent
/home/agent`, run as `agent` (no root needed). Because
`--base-path`+`--export-all` serves everything below that path, **one**
daemon covers the entire parallel tree — no per-repo port needed, just
one additional port for the whole sandbox. It is read-only by design (no
`--enable=receive-pack`): the host fetches from the sandbox to
review/merge work, nothing pushes in, so `git-daemon`'s default
read-only `upload-pack`-only behavior is exactly right. See
`sbx/SECURITY.md` §5 for the daemon's trust boundary (uid, listen
address, and the still-open `--base-path` traversal question, §6 below).

**Port publishing.** `sbx create` has no `-p`/`--publish` flag; port
publishing is a separate, post-creation command: `sbx ports SANDBOX
--publish [[HOST_IP:]HOST_PORT:]SANDBOX_PORT[/PROTOCOL]`, which
auto-allocates an ephemeral host port when `HOST_PORT` is omitted, and
`sbx ports SANDBOX --json` reads the resulting mapping back. The
generated kit declares the daemon's port in kit-spec v2's top-level
`ports:`, so `sbx` publishes it on every container start; the
post-create `--publish` call is only a fallback for an explicit `--kit`
that doesn't declare it (`ensure_published_host_port` looks first,
publishes second). The host port is still ephemeral and still moves on
every container start — see §4 below.

`wmf-sbx-create` fixes a sandbox-internal daemon port
(`wmf_sbx_kit.DEFAULT_DAEMON_PORT`, free to hardcode since it only has to
be unused *inside* that one sandbox's own network namespace) and, once
`sbx create` returns, reads back whatever host port `sbx` assigned via
`sbx ports NAME --json`. That's what the `git://127.0.0.1:HOST_PORT/...`
remote uses, not the sandbox-internal port. Both the port lookup and the
publish call are best-effort: sandbox creation has already succeeded by
this point, so a failure here is a warning, not a hard failure.

Wiring the resulting URL into a host-side `sandbox-<name>` remote,
recording it for teardown, and re-pointing it after a restart is
`sbx/DESIGN-host-remotes.md`'s subject, not this document's — that
design covers the whole sync/prune lifecycle for both the (legacy,
`--clone`-based) primary remote and every parallel-tree remote uniformly.

## 4. Lifecycle: the daemon does not survive `sbx stop`/`sbx run` on its own

There is no init system inside the sandbox image (`systemctl` is
unavailable), and a sandbox's filesystem state (including the parallel
clones themselves) survives a `stop`/`run` cycle while a running process
does not — so a daemon started only during `commands.install` at `create`
time is gone the moment the sandbox is later stopped and restarted, even
though the repos it was serving are still sitting there untouched.

This is resolved by kit-spec v2's `setup.startup` list, whose commands
run on every sandbox start and replay on container restarts: the
generated kit carries the daemon-start command there too, guarded by a
port probe so it no-ops when one is already listening. Both the
install-time start and the startup-time start are kept — install-time so
the sandbox is usable the moment `create` returns, startup so it comes
back after a restart.

What that alone doesn't fix is the *host* half: `sbx` re-applies the
published port itself on every start, but assigns a **new ephemeral host
port every time**. `wmf-sbx-resume` re-points the recorded remotes on
every attach to account for this, and `wmf-sbx-rm` does the same before
its teardown guard probes anything.

## 5. Alternative considered: `ext::` remote via `sbx exec`

Instead of a persistent daemon and a published port, git's `ext::`
transport can run an arbitrary command and speak the pack protocol over
its stdio directly:

```
git remote add sandbox-<name> "ext::wmf-sbx-git-shim <sandbox-name> %S <relative-path>"
```

where the shim runs (approximately) `wmf-sbx exec <sandbox-name> --
git-upload-pack '<relative-path>'` with stdio wired straight through, and
`%S`/`%G` supply the service name and repo path. This sidesteps §4
entirely — `sbx exec` already starts a stopped sandbox on demand, so
there is no separate listener to keep alive and no port to allocate,
publish, or track at all.

Tradeoffs against the daemon approach: no port management and no
lifecycle-tracking work in `wmf-sbx-resume`, but every `git fetch` now
pays a fresh `sbx exec` cost (and, if the sandbox happens to be stopped,
whatever `sbx run` startup latency that implies, mid-fetch); the shim
itself becomes a small piece of trusted code that has to get
binary-transparency, EOF propagation, and exit-status forwarding exactly
right.

**Decision:** the daemon+published-port design (§2–§4) shipped instead —
it's simpler, and mirrors what `sbx` already does for the primary, so
there's no new protocol-plumbing surface to get right. The `ext::` shim
stays documented here as a fallback, worth revisiting if the
daemon-lifecycle piece in `wmf-sbx-resume` ever proves more trouble than
it's worth, or for a stricter network posture that wants to avoid a
second listening service altogether, even a read-only, loopback-only
one.

## 6. Open question

`git-daemon`'s path-traversal protection (`--base-path` request-path
sanitization) is standard, well-established behavior, but has not been
independently verified against this exact invocation — worth an explicit
citation/spot-check before relying on it to keep parallel-tree requests
from escaping `/home/agent`. Carried as a stated assumption, not a
verified claim, in `sbx/SECURITY.md` §5.

## 7. Testing strategy

`parallel_path` is a pure function with direct unit tests (under host
home, outside host home, host-home-equal-to-dir edge case). The
`:ro`-suffix dispatch (clone vs. bind-mount per repo argument) is also
pure — parse each argument once, strip the suffix, and unit-test the
resulting (repo, strategy) list directly, independent of whichever of
`git clone --reference` or `sudo mount`/`remount,ro,bind` actually
executes. Port selection and daemon-start/health logic get fake-`run`-
based tests. The actual daemon and mount behavior — the mount findings in
§2, the daemon-scoping findings in §3, and §6's open question — needs a
real `sbx` binary and can't be meaningfully faked; results from that kind
of spot-check belong in `sbx/NOTES.md`, dated and attributed, not here.
