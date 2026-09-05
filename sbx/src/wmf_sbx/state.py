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


def new_state(name, daemon_port=None, host_port=None, created=None, attached=False):
    """A fresh, empty state dict. `created` is passed in rather than
    stamped here so callers stay testable without freezing the clock.

    `attached` defaults to False, but `wmf-sbx-create` passes True: its
    caller only reaches `new_state()` after the real interactive `sbx
    create ... claude ...` attach already completed successfully (see
    add_host_remotes() in wmf_sbx/create.py), and that attach IS the
    sandbox's first conversation. MEASURED, cananian, 2026-09-08: leaving
    this False meant the very next `wmf-sbx-resume` always saw
    attached=False and stripped `--continue`, forcing a fresh session with
    nothing to continue even though one had already run."""
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
