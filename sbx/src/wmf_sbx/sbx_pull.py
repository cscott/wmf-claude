#!/usr/bin/env python3
"""git sbx-pull: take a branch from a sandbox into the host repo
(sbx/NOTES.md, "Plan (cananian 2026-10-15, from three use cases)").

    git sbx-pull [[NAME/]BRANCH] [--onto X] [-f]

It fetches the sandbox remotes of this repo (remotes.py: the host remote
is named like the sandbox), chooses the sandbox branch, leaves the
current branch only when nothing would be lost, and switches to a local
branch at the sandbox's tip. Which sandbox branch:

- `NAME/BRANCH`;
- `BRANCH`, when one sandbox has it, else the local branch BRANCH's record;
- none: the current branch's record, else the only *new* sandbox branch:
  not named like an `origin` branch, and not at a tip the host already
  has (a hand-off the agent did not change).

The local branch keeps its upstream on Gerrit (`git review` and release
branches need `track=1` to point at origin/REL1_46, say). The sandbox is
recorded in the branch's own config, which no other git command reads:
`branch.B.wmfsbx` (the sandbox), `branch.B.wmfsbxBranch` (the sandbox's
name for it), `branch.B.wmfsbxSha` (the tip at the last pull).

A commit is "saved" when the agent has it (refs/remotes/NAME/* or
refs/remotes/NAME-seen/*, the hand-offs the agent took, for a sandbox of
this repo), when origin has it, or when Gerrit has it. Leaving a branch
whose tip is not saved, or moving a branch over a tip that is not saved,
stops unless `-f`.

On a terminal (stdin and stderr both TTYs), it asks instead of stopping
when the answer is a choice: which branch, sandbox or local branch, from
a list; and, for the two stops above, whether to go on (y/N). A stop that
needs another command (a dirty tree, a branch name to change) stays a
stop. The sandbox's git sbx-pull never asks: the agent has no terminal.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

from . import deps as deps_mod
from . import ls_remotes as ls_remotes_mod
from . import prompt as prompt_mod
from . import remotes as remotes_mod

PREFIX = "git sbx-pull"
# Branch config keys (branch.<name>.<key>), which `wmf-sbx rm` unsets.
SANDBOX_KEY, BRANCH_KEY, SHA_KEY = remotes_mod.BRANCH_KEYS
# The guest's refs for the hand-offs the agent took (guest mode), fetched
# to refs/remotes/NAME-seen/*.
GUEST_SEEN = "refs/sbx/seen"
SEEN_SUFFIX = remotes_mod.SEEN_SUFFIX
# For the guess, when git has no %(ahead-behind:) (before 2.41).
GUESS_RE = re.compile(r"^(master|main|REL\d+_\d+)$")
GERRIT_TIMEOUT = 15


def pick(prompt, question, choices, labels, error):
    """choices[i] for the label the user picks; else PullError(error)."""
    if prompt:
        i = prompt.pick(question, labels)
        if i is not None:
            return choices[i]
    raise PullError(error)


class PullError(Exception):
    pass


class Git:
    """git in one repo, run with `run` (tests pass a recorder)."""

    def __init__(self, root, run=subprocess.run, env=None):
        self.root = root
        self.run = run
        self.env = env

    def __call__(self, *args, check=True):
        res = self.run(["git", "-C", self.root] + list(args), capture_output=True,
                       text=True, stdin=subprocess.DEVNULL, env=self.env)
        if check and res.returncode != 0:
            raise PullError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
        return res

    def out(self, *args):
        return self(*args).stdout.strip()

    def ok(self, *args):
        return self(*args, check=False).returncode == 0

    def sha(self, ref):
        res = self("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def refs(self, prefix, fmt="%(refname)"):
        return [line for line in self.out("for-each-ref", f"--format={fmt}",
                                          prefix).splitlines() if line]

    def config(self, key):
        res = self("config", "--get", key, check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def current_branch(self):
        res = self("symbolic-ref", "-q", "--short", "HEAD", check=False)
        return res.stdout.strip() if res.returncode == 0 else None


def sandbox_remotes(git, env=None):
    """{remote: entry} for this repo's sandbox remotes: listed in a
    sandbox's state file, and carrying the marker (remotes.py)."""
    found = {}
    for entry in ls_remotes_mod.find_remotes(git.root, env):
        remote = entry.get("remote")
        if remote and git.config(f"remote.{remote}.{remotes_mod.MARKER}") == entry["sandbox"]:
            found[remote] = entry
    return found


def fetch(git, remote, sandbox, say):
    """Fetch a sandbox's branches and seen-refs; False when it fails (the
    sandbox is stopped, say)."""
    res = git("fetch", "-q", "--prune", "--no-tags", remote,
              f"+refs/heads/*:refs/remotes/{remote}/*",
              f"+{GUEST_SEEN}/*:refs/remotes/{remote}{SEEN_SUFFIX}/*", check=False)
    if res.returncode != 0:
        last = (res.stderr.strip().splitlines() or [""])[-1]
        say(f"{PREFIX}: cannot fetch from {remote}: is it running? "
            f"(wmf-sbx start {sandbox})" + (f"\n  {last}" if last else ""))
        return False
    return True


def branches_of(git, remote):
    """{branch: sha} of a fetched sandbox remote."""
    out = {}
    for line in git.refs(f"refs/remotes/{remote}/", "%(objectname) %(refname)"):
        sha, ref = line.split(" ", 1)
        name = ref[len(f"refs/remotes/{remote}/"):]
        if name != "HEAD":
            out[name] = sha
    return out


def recorded(git):
    """[(local branch, sandbox, sandbox branch, sha)] for every branch
    with a record."""
    res = git("config", "--get-regexp", rf"^branch\..*\.{SANDBOX_KEY}$", check=False)
    out = []
    for line in res.stdout.splitlines():
        key, _, sandbox = line.partition(" ")
        local = key[len("branch."):-len(f".{SANDBOX_KEY}")]
        out.append((local, sandbox, git.config(f"branch.{local}.{BRANCH_KEY}"),
                    git.config(f"branch.{local}.{SHA_KEY}")))
    return out


def is_saved(git, sha, remotes, gerrit_has):
    """Why `sha` is safe to leave (a short reason), or None."""
    sandbox_refs = []
    for r in remotes:
        sandbox_refs += [f"refs/remotes/{r}/", f"refs/remotes/{r}{SEEN_SUFFIX}/"]
    if sandbox_refs and git.out("for-each-ref", f"--contains={sha}",
                                "--format=%(refname)", *sandbox_refs):
        return "the agent has it"
    if git.out("for-each-ref", f"--contains={sha}", "--format=%(refname)",
               "refs/remotes/origin/"):
        return "origin has it"
    if gerrit_has(git.root, sha):
        return "Gerrit has it"
    return None


def gerrit_host(root):
    try:
        with open(os.path.join(root, ".gitreview"), encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition("=")
                if key.strip() == "host":
                    return value.strip() or None
    except OSError:
        pass
    return None


def gerrit_has(root, sha, env=None, urlopen=urllib.request.urlopen):
    """Whether the Gerrit of root's .gitreview has a change with commit
    `sha` (git-review-check's question, for any commit). False when there
    is no .gitreview or Gerrit cannot be reached."""
    host = gerrit_host(root)
    if not host:
        return False
    env = os.environ if env is None else env
    url = (f"https://{host}/r/changes/?" +
           urllib.parse.urlencode({"q": f"commit:{sha}", "n": "1"}))
    # GERRITUA to gerrit.wikimedia.org only, not to another .gitreview
    # host or a redirect's: the value is a secret.
    opener = deps_mod.gerrit_opener(env.get("GERRITUA"), urlopen)
    try:
        with opener(url, timeout=GERRIT_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
    except OSError:
        return False
    # Gerrit puts )]}' before the JSON.
    try:
        changes = json.loads(body.split("\n", 1)[1] if body.startswith(")]}'") else body)
    except ValueError:
        return False
    return any(isinstance(c, dict) and c.get("change_id") for c in changes)


def sandbox_upstream(entry, branch, lima=None):
    """The guest branch's upstream (`refs/remotes/origin/X`, ...), or
    None, read with `limactl shell` as the agent."""
    from . import remote_helper as remote_helper_mod
    from . import vm as vm_mod
    try:
        name, path = remote_helper_mod.parse_url(entry.get("url"))
        res = vm_mod.shell(name, vm_mod.agent_argv(
            ["git", "-C", path, "for-each-ref", "--format=%(upstream)",
             f"refs/heads/{branch}"]), lima=lima, check=False)
    except Exception:  # noqa: BLE001 -- an upstream is a nicety; guess instead
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def choose(git, target, remotes, fetched, prompt=None):
    """(remote, sandbox branch) to pull."""
    if target:
        remote, _, branch = target.partition("/")
        if branch and remote in remotes:
            if remote not in fetched:
                raise PullError(f"cannot fetch from {remote}")
            if branch not in branches_of(git, remote):
                raise PullError(f"{remote} has no branch {branch}")
            return remote, branch
        have = [r for r in sorted(fetched) if target in branches_of(git, r)]
        rec = record_of(git, target)
        if len(have) > 1 and rec and rec[1] == target:
            # The host's branch of that name came from one of them.
            have = [r for r in have if remotes[r]["sandbox"] == rec[0]] or have
        if len(have) > 1:
            labels = [f"{r}/{target}" for r in have]
            return pick(prompt, f"More than one sandbox has {target}.", have, labels,
                        f"more than one sandbox has {target}; name one: " +
                        ", ".join(labels)), target
        if have:
            return have[0], target
        if rec:
            return remote_for(rec[0], remotes, fetched), rec[1]
        raise PullError(f"no sandbox of this repo has a branch {target}")
    current = git.current_branch()
    rec = record_of(git, current) if current else None
    if rec:
        return remote_for(rec[0], remotes, fetched), rec[1]
    new = new_branches(git, fetched)
    if len(new) == 1:
        return new[0]
    if not new:
        # Every branch, then, to pull one again.
        every = [(r, b) for r in sorted(fetched) for b in sorted(branches_of(git, r))
                 if not git.sha(f"refs/remotes/origin/{b}")]
        error = f"no new branch in a sandbox of this repo; name one: {PREFIX} NAME/BRANCH"
        if not every:
            raise PullError(error)
        return pick(prompt, "No sandbox branch is new. Pull one again?", every,
                    [f"{r}/{b}" for r, b in every], error)
    labels = [f"{r}/{b}" for r, b in new]
    return pick(prompt, "More than one sandbox branch is new.", new, labels,
                "more than one new branch; name one: " + ", ".join(labels))


def record_of(git, local):
    sandbox = git.config(f"branch.{local}.{SANDBOX_KEY}")
    branch = git.config(f"branch.{local}.{BRANCH_KEY}")
    return (sandbox, branch) if sandbox and branch else None


def remote_for(sandbox, remotes, fetched):
    for remote, entry in remotes.items():
        if entry["sandbox"] == sandbox:
            if remote not in fetched:
                raise PullError(f"cannot fetch from {remote}")
            return remote
    raise PullError(f"sandbox {sandbox} has no remote in this repo (was it removed?)")


def new_branches(git, fetched):
    """[(remote, branch)]: sandbox branches that are not named like an
    origin branch, and not at a tip the host has already."""
    known = set(git.refs("refs/heads/", "%(objectname)"))
    known |= {sha for _, _, _, sha in recorded(git) if sha}
    out = []
    for remote in sorted(fetched):
        for branch, sha in sorted(branches_of(git, remote).items()):
            if git.sha(f"refs/remotes/origin/{branch}") or sha in known:
                continue
            out.append((remote, branch))
    return out


def local_branch(git, sandbox, branch, current, prompt=None):
    """(local branch, whether it exists) for the sandbox branch."""
    mine = sorted(loc for loc, s, b, _ in recorded(git) if s == sandbox and b == branch)
    if current in mine:
        return current, True
    if len(mine) == 1:
        return mine[0], True
    if len(mine) > 1:
        return pick(prompt, f"Branches {', '.join(mine)} all record {sandbox}/{branch}. "
                    f"Update which?", mine, mine,
                    f"branches {', '.join(mine)} all record {sandbox}/{branch}; "
                    f"check out the one to update first"), True
    if git.sha(f"refs/heads/{branch}"):
        other = git.config(f"branch.{branch}.{SANDBOX_KEY}")
        if other and other != sandbox:
            raise PullError(
                f"branch {branch} is {other}'s ({other}/"
                f"{git.config(f'branch.{branch}.{BRANCH_KEY}')}); rename one "
                f"(git branch -m {branch} NEWNAME), then pull again")
        return branch, True
    return branch, False


def guess_upstream(git, new):
    """The origin branch with the fewest commits to `new`, master (then
    main) on a tie; None when origin has no branch."""
    rows = []
    res = git("for-each-ref", f"--format=%(refname:lstrip=3) %(ahead-behind:{new})",
              "refs/remotes/origin/", check=False)
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            name, _ahead, behind = line.split(" ")
            rows.append((int(behind), name))
    else:
        for name in git.refs("refs/remotes/origin/", "%(refname:lstrip=3)"):
            if GUESS_RE.match(name):
                rows.append((int(git.out("rev-list", "--count", f"origin/{name}..{new}")),
                             name))
    rows = [(n, name) for n, name in rows if name != "HEAD"]
    if not rows:
        return None
    rank = {"master": 0, "main": 1}
    return min(rows, key=lambda r: (r[0], rank.get(r[1], 2), r[1]))[1]


def choose_upstream(git, entry, branch, new, onto, sandbox_upstream_fn, remotes=()):
    """(upstream, why) for a new local branch, e.g. ("origin/master",
    "the sandbox branch's upstream"). Never a branch of a sandbox remote
    (`remotes`): the old `git safe-reset NAME/BRANCH` made such branches."""
    def on_gerrit(up):
        return up and up.partition("/")[0] not in remotes
    if onto:
        name = onto[len("origin/"):] if onto.startswith("origin/") else onto
        if not git.sha(f"refs/remotes/origin/{name}"):
            raise PullError(f"--onto {onto}: there is no origin/{name}")
        return f"origin/{name}", "--onto"
    up = sandbox_upstream_fn(entry, branch) or ""
    if up.startswith("refs/remotes/origin/"):
        name = up[len("refs/remotes/origin/"):]
        if git.sha(f"refs/remotes/origin/{name}"):
            return f"origin/{name}", "the sandbox branch's upstream"
    if up.startswith("refs/remotes/local/"):
        host = up[len("refs/remotes/local/"):]
        hu = git.out("for-each-ref", "--format=%(upstream:short)", f"refs/heads/{host}")
        if on_gerrit(hu):
            return hu, f"the upstream of {host}, which the sandbox branch tracks"
    # The host branch the work sits on: the nearest one with an upstream.
    best = None
    for line in git.refs("refs/heads/", "%(refname:short) %(upstream:short)"):
        name, _, hu = line.partition(" ")
        if not on_gerrit(hu) or not git.ok("merge-base", "--is-ancestor", f"refs/heads/{name}", new):
            continue
        n = int(git.out("rev-list", "--count", f"refs/heads/{name}..{new}"))
        if best is None or n < best[0]:
            best = (n, name, hu)
    if best:
        return best[2], f"the upstream of {best[1]}, which the work sits on"
    guess = guess_upstream(git, new)
    if guess:
        return f"origin/{guess}", "a guess: fewest commits; --onto names another"
    return None, None


def main(argv=None, env=None, run=subprocess.run, sandbox_upstream_fn=None,
         gerrit_has_fn=None, say=None, cwd=None, prompt=None):
    parser = argparse.ArgumentParser(
        prog=PREFIX, description=__doc__.splitlines()[0],
        epilog="See sbx/README.md (Commit flow).")
    parser.add_argument("target", nargs="?", metavar="[NAME/]BRANCH",
                        help="the sandbox branch (default: the current branch's, "
                        "else the only new one)")
    parser.add_argument("--onto", metavar="BRANCH",
                        help="the origin branch the work is for (the upstream "
                        "of a new local branch), e.g. REL1_46")
    parser.add_argument("-f", "--force", action="store_true",
                        help="I know this is not upstream yet: leave or move a "
                        "branch whose tip is not saved anywhere")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    say = say or (lambda msg: print(msg, file=sys.stderr))
    sandbox_upstream_fn = sandbox_upstream_fn or sandbox_upstream
    gerrit_has_fn = gerrit_has_fn or (lambda root, sha: gerrit_has(root, sha, env))
    prompt = prompt or prompt_mod.Prompt()  # it asks only on a terminal
    try:
        return pull(args, env, run, sandbox_upstream_fn, gerrit_has_fn, say, cwd, prompt)
    except PullError as e:
        say(f"{PREFIX}: {e}")
        return 1


def pull(args, env, run, sandbox_upstream_fn, gerrit_has_fn, say, cwd=None, prompt=None):
    res = run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
              stdin=subprocess.DEVNULL, cwd=cwd)
    if res.returncode != 0:
        raise PullError("not inside a git repository")
    git = Git(os.path.realpath(res.stdout.strip()), run)
    remotes = sandbox_remotes(git, env)
    if not remotes:
        raise PullError("this repo has no sandbox remote (wmf-sbx ls-remotes)")

    # Fetch only the named sandbox, else all of them.
    name = (args.target or "").partition("/")[0]
    wanted = [name] if args.target and "/" in args.target and name in remotes \
        else sorted(remotes)
    fetched = {r for r in wanted if fetch(git, r, remotes[r]["sandbox"], say)}

    remote, branch = choose(git, args.target, remotes, fetched, prompt)
    entry = remotes[remote]
    sandbox = entry["sandbox"]
    new = git.sha(f"refs/remotes/{remote}/{branch}")
    if not new:
        raise PullError(f"{remote} has no branch {branch} now")
    current = git.current_branch()
    local, exists = local_branch(git, sandbox, branch, current, prompt)
    sandbox_refs = sorted(remotes)

    if local != current:
        if git.out("status", "--porcelain", "--untracked-files=no"):
            raise PullError(f"the tree has changes; commit or stash them before "
                            f"leaving {current or 'HEAD'}")
        head = git.sha("HEAD")
        if head and not args.force:
            why = ("it is what was pulled" if current and head == git.config(
                f"branch.{current}.{SHA_KEY}") else is_saved(git, head, sandbox_refs,
                                                             gerrit_has_fn))
            what = f"{current or 'HEAD'} ({head[:12]}) is not on Gerrit, origin or a sandbox"
            if not why and not (prompt and prompt.confirm(
                    f"{what}. Leave it anyway (it stays as it is)?")):
                raise PullError(f"{what}; upload it (git review), or -f to leave it anyway")

    if exists:
        old = git.sha(f"refs/heads/{local}")
        if old != new and not args.force:
            rec_sha = (git.config(f"branch.{local}.{SHA_KEY}")
                       if record_of(git, local) == (sandbox, branch) else None)
            ok = (git.ok("merge-base", "--is-ancestor", old, new) or old == rec_sha
                  or is_saved(git, old, sandbox_refs, gerrit_has_fn))
            what = (f"{local} ({old[:12]}) has commits that {remote}/{branch} does not "
                    f"have and are saved nowhere else")
            if not ok and not (prompt and prompt.confirm(
                    f"{what}. Move {local} anyway (the old tip stays in its reflog)?")):
                raise PullError(f"{what}; -f to move {local} anyway "
                                f"(the old tip stays in its reflog)")
        if local != current:
            git("checkout", "-q", local)
        if old != new:
            git("reset", "-q", "--keep", new)
            say(f"{local}: {old[:12]} -> {new[:12]} ({remote}/{branch}); "
                f"the old tip is {local}@{{1}}")
        else:
            say(f"{local}: at {new[:12]} ({remote}/{branch}) already")
        if args.onto:
            up, why = choose_upstream(git, entry, branch, new, args.onto,
                                      sandbox_upstream_fn, remotes)
            git("branch", "-q", f"--set-upstream-to={up}", local)
            say(f"{local}: upstream {up} ({why})")
    else:
        git("checkout", "-q", "--no-track", "-b", local, new)
        say(f"{local}: new branch at {new[:12]} ({remote}/{branch})")
        up, why = choose_upstream(git, entry, branch, new, args.onto, sandbox_upstream_fn,
                                  remotes)
        if up:
            git("branch", "-q", f"--set-upstream-to={up}", local)
            say(f"{local}: upstream {up} ({why})")
        else:
            say(f"{local}: no upstream (origin has no branch); --onto names one")

    git("config", f"branch.{local}.{SANDBOX_KEY}", sandbox)
    git("config", f"branch.{local}.{BRANCH_KEY}", branch)
    git("config", f"branch.{local}.{SHA_KEY}", new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
