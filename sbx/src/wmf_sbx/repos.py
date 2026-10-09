#!/usr/bin/env python3
"""The repositories of a sandbox (lima-port/HANDOFF-LIMA.md §6, D8, D10).

Host to VM: the host's git dir of each repository is mounted read-only
at /run/wmf-sbx/host/<host path> (template.py). In the VM, a `git clone
--shared` of that mount is at the host path of the repository (D8), so
the alternates point into the mount. After a host `git fetch`, the agent
sees the new objects at once (`git fetch local`).

VM to host: the host repository gets a remote with the sandbox's name and
the URL wmfsbx://NAME/PATH, which git-remote-wmfsbx (remote_helper.py)
serves with `git upload-pack` in the VM. remotes.py adds the remote and
suspends gc, as with Docker sbx.

The git dir is `git rev-parse --git-common-dir`, so that a worktree
mounts the repository's git dir. The clone is on the branch that the
host checkout is on (detached when the host's HEAD is).
"""

import os
import shlex
import subprocess

from . import image as image_mod
from . import template as template_mod
from . import vm as vm_mod

GUEST_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            "sandbox-repos.sh")
SCHEME = "wmfsbx"

# Host paths that a clone cannot have in the VM: the guest uses them
# itself, or they are not persistent (/tmp is a tmpfs on Debian 13).
RESERVED_PREFIXES = ("/bin", "/boot", "/dev", "/etc", "/lib", "/opt", "/proc",
                     "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr",
                     "/var", vm_mod.AGENT_HOME, vm_mod.ENGINEER_HOME)


class RepoError(Exception):
    """A user-facing problem with a host repository."""


def _git(path, args, run):
    return run(["git", "-C", path] + list(args), capture_output=True, text=True,
               stdin=subprocess.DEVNULL)


def check_vm_path(path):
    """Refuse a host path that the clone cannot have in the VM (D8)."""
    if not os.path.isabs(path) or os.path.normpath(path) != path or path.startswith("//"):
        raise RepoError(f"{path!r} is not a normal absolute path")
    for prefix in RESERVED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            raise RepoError(
                f"{path} is under {prefix}, which the VM uses itself; the clone "
                f"in the VM has the host path (D8). Move the checkout.")
    if any(c in path for c in "\n\0"):
        raise RepoError(f"{path!r} has a newline or NUL in it")


def host_repo(path, run=subprocess.run, predicted=False):
    """{path, gitDir, branch, sha} of the host checkout at `path`.

    `predicted` is for a dry run of a repository that is not cloned yet:
    its git dir is then PATH/.git, and the branch and commit are not
    known."""
    check_vm_path(path)
    if predicted:
        return {"path": path, "gitDir": os.path.join(path, ".git"),
                "branch": None, "sha": None}
    res = _git(path, ["rev-parse", "--path-format=absolute", "--git-common-dir"], run)
    if res.returncode != 0:
        raise RepoError(f"{path} is not a git repository; a sandbox gets git "
                        f"repositories only (their git dirs are mounted, D10)")
    gitdir = os.path.realpath(res.stdout.strip())
    res = _git(path, ["rev-parse", "--verify", "-q", "HEAD^{commit}"], run)
    if res.returncode != 0:
        raise RepoError(f"{path} has no commit yet")
    sha = res.stdout.strip()
    res = _git(path, ["symbolic-ref", "-q", "--short", "HEAD"], run)
    branch = res.stdout.strip() if res.returncode == 0 else None
    return {"path": path, "gitDir": gitdir, "branch": branch or None, "sha": sha}


def git_dirs(repos):
    """The git dirs to mount, each once (two worktrees share one)."""
    seen = []
    for r in repos:
        if r["gitDir"] not in seen:
            seen.append(r["gitDir"])
    return seen


def remote_url(name, path):
    return f"{SCHEME}://{name}{path}"


def _call(fn, *args):
    return fn + " " + " ".join(shlex.quote(str(a)) for a in args)


def guest_scripts(repos, upstreams, keep, readonly):
    """(root script, {owner: clone script}) for the guest.

    `upstreams` is {path: upstream URL}; `keep` is the set of paths that
    stay on the host's commit (the repos named on the command line,
    unless --reset-all); `readonly` is the set of ':ro' paths, which the
    engineer owns, so the agent can read them but not change them."""
    with open(GUEST_SCRIPT, encoding="utf-8") as f:
        lib = f.read()
    root = [lib]
    clones = {}
    for r in repos:
        path = r["path"]
        owner = template_mod.ENGINEER if path in readonly else vm_mod.AGENT
        mount = template_mod.mount_point(r["gitDir"])
        root.append(_call("prepare", mount, path, owner))
        clones.setdefault(owner, [lib]).append(_call(
            "clone_repo", mount, path, r["branch"] or "", r["sha"],
            upstreams.get(path, ""), "0" if path in keep else "1"))
    return "\n".join(root) + "\n", {o: "\n".join(s) + "\n" for o, s in clones.items()}


def clone_in_vm(name, repos, upstreams, keep, readonly=(), lima=None, env=None):
    """Make the clones in the VM. Raises RepoError if one fails."""
    root, clones = guest_scripts(repos, upstreams, keep, set(readonly))
    res = vm_mod.shell(name, ["sudo", "bash", "-s"], lima=lima, input=root,
                       check=False, capture=False)
    if res.returncode != 0:
        raise RepoError(f"preparing the clone directories in the VM failed "
                        f"(exit {res.returncode})")
    proxy = image_mod.guest_proxy_env(env)
    for owner, script in clones.items():
        if owner == vm_mod.AGENT:
            argv = vm_mod.agent_argv(["bash", "-s"], env=proxy)
        else:
            argv = ["env"] + [f"{k}={v}" for k, v in sorted(proxy.items())] + ["bash", "-s"]
        res = vm_mod.shell(name, argv, lima=lima, input=script, check=False,
                           capture=False)
        if res.returncode != 0:
            raise RepoError(f"cloning in the VM failed (exit {res.returncode})")


def remote_candidates(name, repos):
    """The host remotes for remotes.sync_remotes: one per repository,
    ':ro' ones too, because their clones also borrow the host's objects
    and gc must stay off for them."""
    return [{"hostDir": r["path"], "remote": name, "url": remote_url(name, r["path"]),
             "sandboxPath": r["path"]} for r in repos]
