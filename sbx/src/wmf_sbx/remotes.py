#!/usr/bin/env python3
"""Adding, re-pointing, and removing the host-side git remotes -- named
the same as the sandbox itself, e.g. `sbx-cite` (see remote_name_for) --
that make a sandbox's clones fetchable from the host. The URL is
wmfsbx://NAME/PATH, served by git-remote-wmfsbx (remote_helper.py,
lima-port/HANDOFF-LIMA.md §6.2).

Two things here are load-bearing and non-obvious:

**Ownership is a config marker, not the URL.** A remote is ours iff
`remote.<remote>.wmfSbxSandbox` equals the sandbox name. The marker is a
two-sided record: the state file says where to look, the repo itself
says "yes, this one is ours." (A wmfsbx:// URL does not change, but a
leaked remote named like a later sandbox would still reach that sandbox.)

**`--no-tags`.** Tags live in one flat namespace per repository, so a
default fetch from a sandbox would drop the sandbox's tags straight into
the host repo's refs/tags/*, indistinguishable from real ones. Branches
are namespaced under refs/remotes/<name>/* and are fine.

sync_remotes() re-points an existing remote that carries our marker
with `git remote set-url`, rather than skipping it.

**Suspending gc is part of the same lifecycle.** The sandbox's clone of a
host repo is a `git clone --shared`: its .git/objects/info/alternates
points back at the host's object store, through the read-only mount of
the host's git dir, and it holds no second copy of those objects
(repos.py). The read-only mount stops the *sandbox* corrupting that, but
not the engineer -- a `git gc` in the host repo can collect objects the
sandbox clone is still reading through the alternate, and the first
symptom is a broken sandbox. So a repo we register a remote in also gets
gc.auto=0 / gc.pruneExpire=never, with the previous values stashed in the
repo's own config under wmfSbx.saved*. Each sandbox that borrows the
repo's objects is listed in wmfSbx.borrower, whether or not it got a
remote (the "name-taken" skip gets none), and gc comes back when no
borrower and no sandbox remote is left.
"""

import os
import subprocess

from . import state as state_mod

MARKER = "wmfSbxSandbox"

# `git remote add` when the name is taken; `git remote remove` when it
# isn't. Both verified against real git -- treated as expected outcomes
# rather than failures.
EXIT_REMOTE_EXISTS = 3
EXIT_NO_SUCH_REMOTE = 2

# `git config --unset` for a key that isn't set. Also an expected outcome.
EXIT_NOTHING_TO_UNSET = 5

# `git sbx-pull` (sbx_pull.py) records the sandbox of a host branch in
# branch.<branch>.<key>: the sandbox, its name for the branch, and the
# tip at the last pull. It fetches the guest's hand-off refs to
# refs/remotes/<remote><SEEN_SUFFIX>/*, which `git remote remove` leaves.
BRANCH_SANDBOX_KEY = "wmfsbx"
BRANCH_KEYS = (BRANCH_SANDBOX_KEY, "wmfsbxBranch", "wmfsbxSha")
SEEN_SUFFIX = "-seen"

# (live key, value we force it to, where the old value is stashed).
GC_SETTINGS = (
    ("gc.auto", "0", "wmfSbx.savedGcAuto"),
    ("gc.pruneExpire", "never", "wmfSbx.savedGcPruneExpire"),
)
GC_SUSPENDED_KEY = "wmfSbx.gcSuspended"
# Multi-valued: the sandboxes whose clones borrow this repo's objects.
BORROWER_KEY = "wmfSbx.borrower"

# Distinguishes "the key was unset before we touched it" from "we have no
# record of this key", which restore has to treat differently: the first
# means unset it again, the second means leave it alone. A sentinel value
# is safe because gc.auto and gc.pruneExpire have no such legal value.
UNSET_SENTINEL = "(unset)"


def _git(host_dir, args, run=subprocess.run):
    return run(
        ["git", "-C", host_dir] + args, capture_output=True, text=True,
        stdin=subprocess.DEVNULL
    )


def _config_get(host_dir, key, run=subprocess.run):
    """The repo's *own* value for key, or None if it doesn't set one.

    --local throughout: without it we would read a value inherited from
    ~/.gitconfig and then write that inherited value into the repo on
    restore, quietly pinning a global setting to one repo forever."""
    result = _git(host_dir, ["config", "--local", "--get", key], run=run)
    if result.returncode != 0:
        return None
    return (result.stdout or "").rstrip("\n")


def _config_set(host_dir, key, value, run=subprocess.run):
    return _git(host_dir, ["config", "--local", key, value], run=run).returncode == 0


def _config_unset(host_dir, key, run=subprocess.run):
    result = _git(host_dir, ["config", "--local", "--unset", key], run=run)
    return result.returncode in (0, EXIT_NOTHING_TO_UNSET)


def borrowers(host_dir, run=subprocess.run):
    """The sandboxes listed as borrowing this repo's objects."""
    result = _git(host_dir, ["config", "--local", "--get-all", BORROWER_KEY], run=run)
    if result.returncode != 0:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def add_borrower(host_dir, name, run=subprocess.run):
    """List `name` as a borrower (once). True if it is listed."""
    if name in borrowers(host_dir, run=run):
        return True
    return _git(host_dir, ["config", "--local", "--add", BORROWER_KEY, name],
                run=run).returncode == 0


def remove_borrower(host_dir, name, run=subprocess.run):
    """Unlist `name` (a validated sandbox name: no regex characters but
    `-` and `.`, which are escaped)."""
    pattern = "^" + name.replace(".", "\\.") + "$"
    result = _git(host_dir, ["config", "--local", "--unset-all", BORROWER_KEY, pattern], run=run)
    return result.returncode in (0, EXIT_NOTHING_TO_UNSET)


def marked_remotes(host_dir, run=subprocess.run):
    """Names of the remotes in this repo that carry our marker, whichever
    sandbox they belong to. Used to decide whether the *last* sandbox
    remote just went away, which is when gc gets turned back on."""
    result = _git(
        host_dir,
        ["config", "--local", "--get-regexp", r"^remote\..*\." + MARKER.lower() + "$"],
        run=run,
    )
    if result.returncode != 0:
        return []
    names = []
    for line in (result.stdout or "").splitlines():
        key = line.split(maxsplit=1)[0]
        parts = key.split(".")
        if len(parts) >= 3:
            # A remote name may itself contain dots.
            names.append(".".join(parts[1:-1]))
    return names


def suspend_gc(host_dir, run=subprocess.run, warn=None):
    """Stash the repo's gc settings and turn gc off. True if gc is off when
    this returns (including "another sandbox already turned it off").

    Idempotent via GC_SUSPENDED_KEY rather than by comparing values: two
    sandboxes sharing one host repo must not have the second one stash
    gc.auto=0 as if it were the engineer's own setting."""
    warn = warn or (lambda msg: None)
    if _config_get(host_dir, GC_SUSPENDED_KEY, run=run) is not None:
        return True

    # Every stash first, then every live key: _restore_gc leaves a key
    # alone when there is no stashed record of it, so a failure part-way
    # through this loop can never make restore unset a value that was the
    # engineer's all along.
    for key, _value, saved_key in GC_SETTINGS:
        old = _config_get(host_dir, key, run=run)
        if not _config_set(
            host_dir, saved_key, UNSET_SENTINEL if old is None else old, run=run
        ):
            warn(f"{host_dir}: could not record the current {key}; leaving gc alone.")
            _restore_gc(host_dir, run=run, warn=warn)
            return False

    for key, value, _saved_key in GC_SETTINGS:
        if not _config_set(host_dir, key, value, run=run):
            warn(
                f"{host_dir}: could not set {key}={value}; a `git gc` here can "
                f"break the sandbox's clone of this repo."
            )
            _restore_gc(host_dir, run=run, warn=warn)
            return False

    if not _config_set(host_dir, GC_SUSPENDED_KEY, "true", run=run):
        warn(f"{host_dir}: could not record that we suspended gc; undoing it.")
        _restore_gc(host_dir, run=run, warn=warn)
        return False
    return True


def _restore_gc(host_dir, run=subprocess.run, warn=None):
    """Put back what suspend_gc stashed and drop our bookkeeping keys.

    Also the back-out path for a half-finished suspend_gc, which is why an
    absent stash means "leave the live key alone" and not "unset it"."""
    warn = warn or (lambda msg: None)
    ok = True
    for key, _value, saved_key in GC_SETTINGS:
        old = _config_get(host_dir, saved_key, run=run)
        if old is None:
            pass
        elif old == UNSET_SENTINEL:
            if not _config_unset(host_dir, key, run=run):
                ok = False
                warn(f"{host_dir}: could not unset {key} again.")
        elif not _config_set(host_dir, key, old, run=run):
            ok = False
            warn(f"{host_dir}: could not restore {key}={old}.")
        _config_unset(host_dir, saved_key, run=run)
    _config_unset(host_dir, GC_SUSPENDED_KEY, run=run)
    return ok


def resume_gc(host_dir, run=subprocess.run, warn=None, dry_run=False):
    """Turn gc back on, but only once no sandbox remote is left in this
    repo. True if it restored (or would have, under dry_run)."""
    if _config_get(host_dir, GC_SUSPENDED_KEY, run=run) is None:
        return False
    if marked_remotes(host_dir, run=run) or borrowers(host_dir, run=run):
        # Another sandbox -- or another of this sandbox's remotes -- still
        # has a clone reading this repo's objects through an alternate.
        return False
    if dry_run:
        return True
    return _restore_gc(host_dir, run=run, warn=warn)


def is_git_repo(host_dir, run=subprocess.run):
    if not os.path.isdir(host_dir):
        return False
    try:
        result = _git(host_dir, ["rev-parse", "--git-dir"], run=run)
    except OSError:
        return False
    return result.returncode == 0


def marker_value(host_dir, remote, run=subprocess.run):
    """The sandbox name recorded on this remote, or None if unmarked."""
    result = _git(host_dir, ["config", "--get", f"remote.{remote}.{MARKER}"], run=run)
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def set_marker(host_dir, remote, name, run=subprocess.run):
    return _git(
        host_dir, ["config", f"remote.{remote}.{MARKER}", name], run=run
    ).returncode == 0


def remote_name_for(sandbox_name):
    # The sandbox name already carries the `sbx-` prefix (see
    # wmf_sbx_create.default_sandbox_name) -- no second prefix on top of
    # it, so a sandbox named `sbx-visualeditor` gets a remote of the same
    # name, not `sandbox-sbx-visualeditor`.
    return sandbox_name


def sync_remotes(name, candidates, run=subprocess.run, warn=None):
    """Add (or re-point) one `<name>` remote per candidate.

    `candidates` is a list of dicts with hostDir/remote/url/sandboxPath
    -- see repos.remote_candidates. A ':ro' repo gets a remote too: its
    clone in the VM also borrows the host's objects. Returns (remotes, skipped) in
    the shape wmf_sbx_state stores: `remotes` lists only what we actually
    added or adopted, so cleanup can never try to remove something we
    never created.

    Everything is best-effort. By the time this runs the sandbox already
    exists, and a config lock or a read-only repo is not a reason to fail
    the whole `create` -- warn, record what worked, carry on.
    """
    warn = warn or (lambda msg: None)
    remotes, skipped = [], []
    for cand in candidates:
        host_dir = cand["hostDir"]
        remote = cand["remote"]
        url = cand["url"]

        if not is_git_repo(host_dir, run=run):
            skipped.append({"hostDir": host_dir, "reason": "not-a-git-repo"})
            continue

        entry = {
            "hostDir": host_dir,
            "remote": remote,
            "url": url,
            "sandboxPath": cand.get("sandboxPath"),
            "adopted": False,
        }

        # The clone borrows this repo's objects whether or not it gets a
        # remote here, so gc goes off first. The borrower entry is what
        # lets remove_remotes find its way back to turn it on again.
        if add_borrower(host_dir, name, run=run):
            suspend_gc(host_dir, run=run, warn=warn)
        else:
            warn(f"{host_dir}: could not record sandbox {name!r} as a borrower; "
                 f"gc stays on here, so a `git gc --prune=now` can break its clone.")

        result = _git(host_dir, ["remote", "add", "--no-tags", remote, url], run=run)
        if result.returncode == EXIT_REMOTE_EXISTS:
            existing = marker_value(host_dir, remote, run=run)
            if existing != name:
                # Either the engineer's own remote or one belonging to a
                # different sandbox. Never silently steal the name.
                warn(
                    f"{host_dir}: remote {remote!r} already exists and isn't "
                    f"ours (marker: {existing or 'none'}) -- leaving it alone."
                )
                skipped.append({"hostDir": host_dir, "reason": "name-taken"})
                continue
            # A same-named predecessor's stale entry, or a resume with a
            # freshly published port: re-point it.
            set_url = _git(host_dir, ["remote", "set-url", remote, url], run=run)
            if set_url.returncode != 0:
                warn(
                    f"{host_dir}: could not re-point remote {remote!r} "
                    f"(exit {set_url.returncode}): {(set_url.stderr or '').strip()}"
                )
                skipped.append({"hostDir": host_dir, "reason": "set-url-failed"})
                continue
            entry["adopted"] = True
        elif result.returncode != 0:
            warn(
                f"{host_dir}: could not add remote {remote!r} "
                f"(exit {result.returncode}): {(result.stderr or '').strip()}"
            )
            skipped.append({"hostDir": host_dir, "reason": "add-failed"})
            continue

        if not set_marker(host_dir, remote, name, run=run):
            # Without the marker we could never safely remove this remote
            # again, so back the add out rather than leak an unowned one.
            warn(f"{host_dir}: could not mark remote {remote!r} as ours -- removing it again.")
            _git(host_dir, ["remote", "remove", remote], run=run)
            skipped.append({"hostDir": host_dir, "reason": "marker-failed"})
            continue

        remotes.append(entry)
    return remotes, skipped


def forget_pulls(host_dir, name, remote, run=subprocess.run):
    """Unset the branch records (BRANCH_KEYS) that name sandbox `name`,
    and delete `remote`'s seen-refs. The branches stay."""
    result = _git(host_dir, ["config", "--local", "--get-regexp",
                             r"^branch\..*\." + BRANCH_SANDBOX_KEY.lower() + "$"], run=run)
    for line in (result.stdout or "").splitlines() if result.returncode == 0 else []:
        key, _, value = line.partition(" ")
        if value != name:
            continue
        branch = key[len("branch."):-len("." + BRANCH_SANDBOX_KEY)]
        for k in BRANCH_KEYS:
            _config_unset(host_dir, f"branch.{branch}.{k}", run=run)
    seen = _git(host_dir, ["for-each-ref", "--format=%(refname)",
                           f"refs/remotes/{remote}{SEEN_SUFFIX}/"], run=run)
    for ref in (seen.stdout or "").splitlines() if seen.returncode == 0 else []:
        _git(host_dir, ["update-ref", "-d", ref], run=run)


def remove_remotes(state, run=subprocess.run, warn=None, dry_run=False):
    """Remove every remote `state` records as ours. Returns
    [(hostDir, remote, outcome), ...] where outcome is one of 'removed',
    'gone' (host repo moved or deleted), 'absent' (remote already gone),
    'not-ours' (marker missing or changed -- left alone), or 'failed'.

    Idempotent by construction: every "already in the desired state" case
    is a success, because this runs from `wmf-sbx-rm`, `--prune`, and
    opportunistically from `wmf-sbx-create`, and any of them may have got
    there first.
    """
    warn = warn or (lambda msg: None)
    name = state["name"]

    def remove_one(host_dir, remote):
        existing = marker_value(host_dir, remote, run=run)
        if existing is None:
            # Distinguish "someone removed our remote already" from
            # "someone replaced it with their own": only the latter has a
            # remote still sitting there under our name.
            listed = _git(host_dir, ["remote"], run=run)
            names = (listed.stdout or "").split()
            if remote not in names:
                return "absent"
            warn(f"{host_dir}: remote {remote!r} lost its {MARKER} marker -- leaving it alone.")
            return "not-ours"
        if existing != name:
            warn(
                f"{host_dir}: remote {remote!r} now belongs to sandbox "
                f"{existing!r}, not {name!r} -- leaving it alone."
            )
            return "not-ours"
        if dry_run:
            return "removed"
        result = _git(host_dir, ["remote", "remove", remote], run=run)
        if result.returncode == 0:
            # `git remote remove` drops the whole remote.<name>.* section,
            # marker included; git sbx-pull's records are ours to clean.
            forget_pulls(host_dir, name, remote, run=run)
            return "removed"
        if result.returncode == EXIT_NO_SUCH_REMOTE:
            return "absent"
        warn(
            f"{host_dir}: could not remove remote {remote!r} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()}"
        )
        return "failed"

    results = []
    for entry in state.get("remotes", []):
        host_dir, remote = entry["hostDir"], entry["remote"]
        if not is_git_repo(host_dir, run=run):
            # Expected, not an error: the engineer moved or deleted the
            # checkout. Nothing to clean up there -- including the gc
            # settings, which went with it.
            results.append((host_dir, remote, "gone"))
            continue
        results.append((host_dir, remote, remove_one(host_dir, remote)))
    # Every repo this sandbox borrowed from, with or without a remote.
    host_dirs = [e["hostDir"] for e in state.get("remotes", [])]
    host_dirs += [e["hostDir"] for e in state.get("skipped", []) if e.get("hostDir")]
    for host_dir in dict.fromkeys(host_dirs):
        if dry_run or not is_git_repo(host_dir, run=run):
            continue
        remove_borrower(host_dir, name, run=run)
        # Unconditional, even after 'not-ours' or 'failed': resume_gc
        # decides for itself, and its condition is "no sandbox remote and
        # no borrower is left in this repo", which is exactly the state
        # that makes gc safe again regardless of how we got there.
        resume_gc(host_dir, run=run, warn=warn)
    return results


def unfetched_tips(state, run=subprocess.run):
    """Commits the sandbox's clones have that the host repo has never
    seen -- i.e. work that `wmf-sbx rm` would destroy irrecoverably, since
    the agent's commits live only on the VM's disk. `git ls-remote` of a
    wmfsbx:// URL runs upload-pack in the VM, so the VM must run.

    Returns (findings, unreachable): findings is
    [(hostDir, remote, [sha, ...]), ...]; unreachable is
    [(hostDir, remote, reason), ...] for remotes we could not probe at
    all. "Can't check" must never read as "nothing to lose", so callers
    treat unreachable as blocking too (see wmf_sbx_rm).
    """
    findings, unreachable = unfetched_refs(state, run=run)
    return ([(host_dir, remote, [sha for sha, _refs in tips])
             for host_dir, remote, tips in findings], unreachable)


def unfetched_refs(state, run=subprocess.run):
    """As unfetched_tips, but each finding lists (sha, [ref, ...]): the
    refs that point at the commit (HEAD, refs/heads/...). The ref names
    come from the guest's upload-pack."""
    findings, unreachable = [], []
    for entry in state.get("remotes", []):
        host_dir, remote, url = entry["hostDir"], entry["remote"], entry["url"]
        if not is_git_repo(host_dir, run=run):
            continue
        listed = run(
            ["git", "ls-remote", url], capture_output=True, text=True,
            stdin=subprocess.DEVNULL
        )
        if listed.returncode != 0:
            unreachable.append((host_dir, remote, (listed.stderr or "").strip()))
            continue
        refs = _parse_ls_remote(listed.stdout)
        # Everything the sandbox got *from* somewhere else. upload-pack
        # advertises the whole of refs/, remote-tracking refs included, so
        # a sandbox that fetched Gerrit more recently than the host looks
        # like a pile of unfetched work -- 14 tips across two repos, in
        # the case that prompted this. None of it is
        # the agent's, and none of it dies with the sandbox: it is sitting
        # on the origin the sandbox pulled it from.
        upstream = {sha for sha, ref in refs if ref.startswith("refs/remotes/")}
        missing, names, checked = [], {}, set()
        for sha, ref in refs:
            if ref.startswith("refs/tags/") or ref.startswith("refs/remotes/"):
                # Peeled tag entries ("...^{}") and lightweight tags both
                # show up here; branch tips are what matter for lost work.
                continue
            if sha in upstream:
                # A branch sitting exactly on origin/master, which is what
                # every clone looks like until the agent commits.
                continue
            if sha in checked:
                # HEAD and the branch it points at are two lines with one
                # commit -- report the work once, not once per ref.
                names[sha].append(ref)
                continue
            checked.add(sha)
            names[sha] = [ref]
            have = _git(host_dir, ["cat-file", "-e", f"{sha}^{{commit}}"], run=run)
            if have.returncode != 0:
                missing.append(sha)
        if missing:
            findings.append((host_dir, remote, [(sha, names[sha]) for sha in missing]))
    return findings, unreachable


def _parse_ls_remote(stdout):
    """[(sha, ref), ...] from `git ls-remote` output, skipping any line
    that isn't one."""
    refs = []
    for line in (stdout or "").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            refs.append((parts[0], parts[1].strip()))
    return refs


def prune_dead(live_names, run=subprocess.run, warn=None, dry_run=False, env=None):
    """Clean up after sandboxes that went away without us -- a plain
    `limactl delete`, or a machine that lost its Lima state. Takes
    the set of still-live sandbox names as an argument rather than
    shelling out for it, so this module stays free of any dependency on
    wmf_sbx_create (which imports *this*).

    Never destroys a sandbox and never prompts (`wmf-sbx rm --prune`
    runs it). A leaked remote must not survive until a later sandbox
    takes its name.

    Returns [(name, results), ...] with results as from remove_remotes.
    """
    warn = warn or (lambda msg: None)
    live = set(live_names)
    pruned = []
    for name in state_mod.list_names(env=env):
        if name in live:
            continue
        try:
            state = state_mod.load(name, env=env)
        except state_mod.StateError as e:
            warn(str(e))
            continue
        if state is None:
            continue
        results = remove_remotes(state, run=run, warn=warn, dry_run=dry_run)
        if not dry_run:
            state_mod.delete(name, env=env)
        pruned.append((name, results))
    return pruned


# Not walked into by scan_orphans: no repo of ours is inside them, and
# they can be large.
SCAN_SKIP_DIRS = (".git", "node_modules", "vendor")


def find_repos(root):
    """Every git working tree under `root` (a directory with a .git dir
    or file), `root` included."""
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames or ".git" in filenames:
            yield dirpath
        dirnames[:] = sorted(d for d in dirnames if d not in SCAN_SKIP_DIRS)


def scan_orphans(root, known_names, run=subprocess.run, warn=None, dry_run=False):
    """For a lost state file (`wmf-sbx rm --scan DIR`): in every repo under
    `root`, remove the sandbox remotes and the borrower entries of
    sandboxes not in `known_names`, then turn gc back on if nothing is
    left. Returns [(host_dir, what), ...] of what was (or would be)
    removed. Only what carries our marker is touched."""
    warn = warn or (lambda msg: None)
    known = set(known_names)
    found = []
    for host_dir in find_repos(root):
        changed = False
        for remote in marked_remotes(host_dir, run=run):
            owner = marker_value(host_dir, remote, run=run)
            if owner is None or owner in known:
                continue
            found.append((host_dir, f"remote {remote} (sandbox {owner})"))
            if not dry_run:
                result = _git(host_dir, ["remote", "remove", remote], run=run)
                if result.returncode not in (0, EXIT_NO_SUCH_REMOTE):
                    warn(f"{host_dir}: could not remove remote {remote!r}: "
                         f"{(result.stderr or '').strip()}")
                changed = True
        for owner in borrowers(host_dir, run=run):
            if owner in known:
                continue
            found.append((host_dir, f"gc borrower (sandbox {owner})"))
            if not dry_run:
                remove_borrower(host_dir, owner, run=run)
                changed = True
        if changed:
            resume_gc(host_dir, run=run, warn=warn)
    return found
