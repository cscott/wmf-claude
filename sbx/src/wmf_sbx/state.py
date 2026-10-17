#!/usr/bin/env python3
"""Persistent per-sandbox host state for wmf-sbx.

This is the project's first *durable* host-side state -- everything else
`wmf-claude` writes is session-scoped and reaped on exit. It exists
because `wmf-sbx-create` adds `<name>` git remotes to host repos
(see wmf_sbx/remotes.py), and those remotes outlive the VM: a plain
`limactl delete` leaves every `.git/config` entry behind. A leaked
remote doesn't merely clutter a config -- it can later fetch from an
unrelated sandbox that is given the same name. Reliable cleanup is
therefore a correctness requirement, and cleanup needs a record of what
we touched.

One JSON file per sandbox rather than a single registry, so two
concurrent `wmf-sbx-create` runs can never read-modify-write the same
file. Writes are atomic (tempfile + os.replace) so a crash mid-write
can't leave a half-parsed file behind.
"""

import contextlib
import fcntl
import json
import os
import re
import tempfile

from . import prompt as prompt_mod

SCHEMA_VERSION = 1

# Lima instance names are already this shape; validating anyway means an
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


def state_root(env=None):
    """`${XDG_STATE_HOME:-~/.local/state}/wmf-sbx`."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "wmf-sbx")


def state_dir(env=None):
    return os.path.join(state_root(env), "sandboxes")


def state_path(name, env=None):
    return os.path.join(state_dir(env), f"{validate_name(name)}.json")


def new_state(name, created=None, primary_dir=None, image=None, vm_type=None, repos=None,
              read_only=None):
    """A fresh, empty state dict. `created` is passed in rather than
    stamped here so callers stay testable without freezing the clock.

    `attached` is False for a new sandbox: resume.set_attached sets it
    when an interactive session exits (resume.py).

    `primary_dir` is the realpath'd host directory of the sandbox's
    *primary* repo -- the first one `wmf-sbx create` named, not an
    extra. It is what `resolve_name_arg()` below matches a path
    shortcut (`.`, `..`, an absolute path, ...) against, so a sandbox
    created before this field existed simply isn't reachable that way;
    it is still reachable by its plain name."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "name": validate_name(name),
        "created": created,
        # Has an agent ever run in this sandbox? `wmf-sbx-resume` uses it
        # to decide whether `--continue` has anything to continue: on the
        # very first attach it doesn't, and claude exits 1 rather than
        # starting fresh.
        "attached": False,
        "primaryDir": primary_dir,
        "remotes": [],
        "skipped": [],
        # The golden image the VM was made from (image.py's key; `image
        # rm` refuses an image that a state file names), the Lima driver,
        # and the host repositories of the sandbox (their git dirs are
        # mounted).
        "image": image,
        "vmType": vm_type,
        "repos": list(repos or []),
        # The ':ro' repos: the session grants them read-only (session.py).
        "readOnly": list(read_only or []),
    }


def write_atomic(path, text, mode=0o600):
    """Write `text` to `path` with `mode`, atomically: a temporary file in
    the same directory, then os.replace. Returns the path."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        if mode != 0o600:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        # Don't leave a stray tempfile behind if the write or rename blew
        # up; os.replace is the only step that makes the new file visible.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def private_dir(path):
    """Make the directory `path` if missing, and make it 0700. Returns it."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def save(state, env=None):
    """Atomically write `state` to its per-sandbox file. Returns the path."""
    path = state_path(state["name"], env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return write_atomic(path, json.dumps(state, indent=2) + "\n")


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


def require(name, env=None):
    """The state of sandbox `name`. Raise StateError if wmf-sbx has none:
    every verb acts only on a sandbox that wmf-sbx owns, so that `rm`
    can never touch another Lima VM."""
    state = load(validate_name(name), env=env)
    if state is None:
        raise StateError(
            f"{name!r} is not a wmf-sbx sandbox (no {state_path(name, env)}). "
            f"`wmf-sbx ls` lists them; `limactl list` lists every Lima VM."
        )
    return state


def delete(name, env=None):
    """Remove the state file. True if there was one, False if not."""
    try:
        os.unlink(state_path(name, env))
        return True
    except FileNotFoundError:
        return False


def _reservation_path(name, env=None):
    return os.path.join(state_dir(env), f".{validate_name(name)}.creating")


def reserve(name, env=None):
    """Reserve `name` for the `create` that calls this: an flock on a file
    beside the state files, held as long as the returned file is open (the
    caller keeps it to the end of create; the OS lets go when the process
    ends, crashed or not). Raises StateError when another process holds it
    (without it, two creates of one name both run, and the second fails
    only at `limactl create`). The file stays: to remove it
    races a process that opened it."""
    os.makedirs(state_dir(env), exist_ok=True)
    f = open(_reservation_path(name, env), "a", encoding="utf-8")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise StateError(f"another wmf-sbx create is making a sandbox named {name!r} "
                         f"now") from None
    return f


def reserved_names(env=None):
    """The names that a running `create` holds (reserve)."""
    try:
        entries = os.listdir(state_dir(env))
    except FileNotFoundError:
        return set()
    names = set()
    for entry in entries:
        if not (entry.startswith(".") and entry.endswith(".creating")):
            continue
        try:
            with open(os.path.join(state_dir(env), entry), "a", encoding="utf-8") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    names.add(entry[1:-len(".creating")])
        except OSError:
            continue
    return names


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
# `.` and `..` alone match too. Matched with re.match (a prefix test,
# not a full match) against the raw argument, before any
# expanduser/realpath.
PATH_SHORTCUT_RE = re.compile(r"^(/|\.\.?$|\.(\.?/))")


def is_path_shortcut(arg):
    """Whether `arg` looks like a filesystem path rather than a bare
    sandbox NAME -- see PATH_SHORTCUT_RE."""
    return bool(PATH_SHORTCUT_RE.match(arg or ""))


def find_by_primary_dir(path, env=None):
    """Names of every sandbox whose recorded `primaryDir` realpath-matches
    `path` (already expected to be realpath'd by the caller). A state
    file that fails to parse is skipped: the caller sees "no match"."""
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
    no saved state for it or `name` is not a valid state filename. Unlike `find_by_primary_dir`
    this goes the other way -- name to directory -- for a caller that
    already has a real sandbox NAME (not a path shortcut) and wants to
    anchor a relative path inside it; see wmf_sbx.cp.resolve_cp_arg."""
    try:
        saved = load(name, env=env)
    except StateError:
        return None
    return (saved or {}).get("primaryDir")


def _ask_which(path, matches, isatty=None, ask=None):
    """On a terminal, let the human pick one of `matches`; else None."""
    i = prompt_mod.Prompt(ask=ask, isatty=isatty or prompt_mod.interactive).pick(
        f"{path} is the primary workspace of {len(matches)} sandboxes:", matches)
    return None if i is None else matches[i]


def resolve_name_arg(arg, env=None, isatty=None, ask=None):
    """If `arg` is a path shortcut (see is_path_shortcut), resolve it to
    the one sandbox whose primary workspace is that directory and return
    its name; otherwise return `arg` unchanged. Raises StateError if no
    sandbox matches, or if more than one does and no human on a terminal
    picks one -- a guess is never right."""
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
        picked = _ask_which(path, matches, isatty=isatty, ask=ask)
        if picked is not None:
            return picked
        raise StateError(
            f"{path!r} is the primary workspace of {len(matches)} sandboxes "
            f"({', '.join(matches)}) -- name one explicitly instead of {arg!r}."
        )
    return matches[0]
