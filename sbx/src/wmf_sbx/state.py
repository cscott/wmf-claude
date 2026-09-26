#!/usr/bin/env python3
"""Persistent per-sandbox host state for wmf-sbx.

This is the project's first *durable* host-side state -- everything else
`wmf-claude` writes is session-scoped and reaped on exit. It exists
because `wmf-sbx-create` adds `<name>` git remotes to host repos
(see wmf_sbx/remotes.py), and those remotes outlive the sandbox: `sbx rm`
destroys the sandbox and leaves every `.git/config` entry behind. Worse,
host ports are recycled (confirmed on the host, 2026-09-07), so a leaked
remote doesn't merely clutter a config -- it can later fetch from an
unrelated sandbox that happened to be handed the same port. Reliable
cleanup is therefore a correctness requirement, and cleanup needs a
record of what we touched. See sbx/DESIGN-host-remotes.md §1-§2.

One JSON file per sandbox rather than a single registry, so two
concurrent `wmf-sbx-create` runs can never read-modify-write the same
file. Writes are atomic (tempfile + os.replace) so a crash mid-write
can't leave a half-parsed file behind.
"""

import contextlib
import json
import os
import re
import tempfile

SCHEMA_VERSION = 1

# `sbx` sandbox names are already this shape; validating anyway means an
# explicit --name that isn't gets rejected before it's ever used as a
# filename, rather than escaping the state directory via '..' or a '/'.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class StateError(Exception):
    """A user-facing failure reading or writing sandbox state."""


def validate_name(name):
    if not NAME_RE.match(name or ""):
        raise StateError(
            f"{name!r} is not a usable sandbox name (expected "
            f"{NAME_RE.pattern}) -- refusing to use it as a state filename."
        )
    return name


def state_dir(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "wmf-sbx", "sandboxes")


def state_path(name, env=None):
    return os.path.join(state_dir(env), f"{validate_name(name)}.json")


def new_state(name, daemon_port=None, host_port=None, created=None, attached=False,
              primary_dir=None):
    """A fresh, empty state dict. `created` is passed in rather than
    stamped here so callers stay testable without freezing the clock.

    `attached` defaults to False, but `wmf-sbx-create` passes True: its
    caller only reaches `new_state()` after the real interactive `sbx
    create ... claude ...` attach already completed successfully (see
    add_host_remotes() in wmf_sbx/create.py), and that attach IS the
    sandbox's first conversation. MEASURED, cananian, 2026-09-08: leaving
    this False meant the very next `wmf-sbx-resume` always saw
    attached=False and stripped `--continue`, forcing a fresh session with
    nothing to continue even though one had already run.

    `primary_dir` is the realpath'd host directory of the sandbox's
    *primary* repo -- the one `sbx create`'s positional argument named,
    not an extra. It is what `resolve_name_arg()` below matches a path
    shortcut (`.`, `..`, an absolute path, ...) against, so a sandbox
    created before this field existed simply isn't reachable that way;
    it is still reachable by its plain name."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "name": validate_name(name),
        "created": created,
        "daemonPort": daemon_port,
        "hostPort": host_port,
        # Has an agent ever run in this sandbox? `wmf-sbx-resume` uses it
        # to decide whether `--continue` has anything to continue: on the
        # very first attach it doesn't, and claude exits 1 rather than
        # starting fresh. Readers use .get(), so a state file written
        # before this key existed is simply "not yet attached".
        "attached": attached,
        "primaryDir": primary_dir,
        "remotes": [],
        "skipped": [],
    }


def save(state, env=None):
    """Atomically write `state` to its per-sandbox file. Returns the path."""
    path = state_path(state["name"], env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        # Don't leave a stray tempfile behind if the write or rename blew
        # up; os.replace is the only step that makes the new file visible.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def load(name, env=None):
    """The saved state for `name`, or None if there is none. A file that
    exists but doesn't parse raises -- silently treating a corrupt state
    file as 'no remotes to clean up' is exactly the leak this module is
    here to prevent."""
    path = state_path(name, env)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise StateError(f"{path} is not valid JSON ({e}).") from None


def delete(name, env=None):
    """Remove the state file. True if there was one, False if not."""
    try:
        os.unlink(state_path(name, env))
        return True
    except FileNotFoundError:
        return False


def list_names(env=None):
    """Every sandbox name we hold state for, sorted. Used by
    `wmf-sbx-rm --prune` to find sandboxes that went away without us."""
    try:
        entries = os.listdir(state_dir(env))
    except FileNotFoundError:
        return []
    names = []
    for entry in entries:
        if not entry.endswith(".json") or entry.startswith("."):
            continue
        stem = entry[: -len(".json")]
        if NAME_RE.match(stem):
            names.append(stem)
    return sorted(names)


# A filesystem-path shortcut for a sandbox name: an absolute path, or a
# relative one that starts `.` or `..` -- as opposed to a bare NAME,
# which never contains a `/` and never starts with `.` (see NAME_RE).
# sbx/NOTES.md "Allow shortcut sandbox names". Matched with re.match
# (a prefix test, not a full match) against the raw argument, before
# any expanduser/realpath.
#
# `.` alone and `..` alone are added on top of the original
# `^(/|.(.?/|$))` from the NOTES.md item: as written, that pattern
# matches `.`, `./...` and `../...` but not a bare `..` (the `(\.?/|$)`
# branch needs either a trailing `/` or end-of-string right after the
# first `.`, and `..` has neither) -- an odd gap, since `..` alone is
# just as natural a thing to type as `.` alone.
PATH_SHORTCUT_RE = re.compile(r"^(/|\.\.?$|\.(\.?/))")


def is_path_shortcut(arg):
    """Whether `arg` looks like a filesystem path rather than a bare
    sandbox NAME -- see PATH_SHORTCUT_RE."""
    return bool(PATH_SHORTCUT_RE.match(arg or ""))


def find_by_primary_dir(path, env=None):
    """Names of every sandbox whose recorded `primaryDir` realpath-matches
    `path` (already expected to be realpath'd by the caller). A sandbox
    created before `primaryDir` existed, or whose state file failed to
    parse, is silently skipped -- the caller sees "no match", the same
    as if the sandbox had never been created with our tooling at all."""
    matches = []
    for name in list_names(env=env):
        try:
            saved = load(name, env=env)
        except StateError:
            continue
        primary_dir = (saved or {}).get("primaryDir")
        if primary_dir and os.path.realpath(primary_dir) == path:
            matches.append(name)
    return matches


def primary_dir_for(name, env=None):
    """The recorded `primaryDir` for sandbox `name`, or None if there is
    no saved state for it, that state predates the `primaryDir` field, or
    `name` isn't even a valid state filename. Unlike `find_by_primary_dir`
    this goes the other way -- name to directory -- for a caller that
    already has a real sandbox NAME (not a path shortcut) and wants to
    anchor a relative path inside it; see wmf_sbx.cp.resolve_cp_arg."""
    try:
        saved = load(name, env=env)
    except StateError:
        return None
    return (saved or {}).get("primaryDir")


def resolve_name_arg(arg, env=None):
    """If `arg` is a path shortcut (see is_path_shortcut), resolve it to
    the one sandbox whose primary workspace is that directory and return
    its name; otherwise return `arg` unchanged. Raises StateError if no
    sandbox matches, or if more than one does -- both cases need a human
    to pick, not a guess."""
    if not is_path_shortcut(arg):
        return arg
    path = os.path.realpath(os.path.expanduser(arg))
    matches = find_by_primary_dir(path, env=env)
    if not matches:
        raise StateError(
            f"no sandbox has {path!r} as its primary workspace -- pass its "
            f"name instead of {arg!r}."
        )
    if len(matches) > 1:
        raise StateError(
            f"{path!r} is the primary workspace of {len(matches)} sandboxes "
            f"({', '.join(matches)}) -- name one explicitly instead of {arg!r}."
        )
    return matches[0]
