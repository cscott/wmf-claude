#!/usr/bin/env python3
"""Tests for the git-review-check helper -- run with:
  python3 -m unittest discover -s sbx/tests -v

Driven against **real repositories** in tempdirs, like test_git_safe_reset
-- the interesting behaviour here is git's own containment answer
(`git for-each-ref --contains`), so faking it would only re-assert my
reading of git rather than testing anything.

The Gerrit-HTTP path (no REMOTE argument, or a REMOTE that qualifies for
neither the `local` nor the sandbox-state check) is exercised only for its
no-`.gitreview` and unparseable-host failure modes, which need no
network. Actually reaching Gerrit is outside what a unit test should
depend on.

Every test runs with `XDG_STATE_HOME` pointed at a per-test tempdir --
this is a real host, and picking up the *actual* `~/.local/state/wmf-sbx`
would make these tests depend on (and possibly be confused by) whatever
sandboxes really exist.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.state as state_mod  # noqa: E402
import isolation  # noqa: E402
from fakes import commit, git, identity  # noqa: E402


def setUpModule():
    isolation.isolate()


BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
SCRIPT = os.path.join(BIN, "git-review-check")
SAFE_RESET = os.path.join(BIN, "git-safe-reset")

HAVE_GIT = shutil.which("git") is not None
HAVE_BASH = shutil.which("bash") is not None


def write_sandbox_state(state_home, sandbox_name, host_dir, remote):
    """A wmf-sbx state file that lists `remote` against `host_dir`, as
    remotes.sync_remotes() records it, for is_local_check_target() to find."""
    state = state_mod.new_state(sandbox_name)
    state["remotes"].append({
        "hostDir": os.path.realpath(host_dir),
        "remote": remote,
        "url": f"git://127.0.0.1/{remote}",
    })
    state_mod.save(state, {"XDG_STATE_HOME": state_home})


@unittest.skipUnless(HAVE_GIT and HAVE_BASH, "git and bash required")
class LocalIshRemoteTests(unittest.TestCase):
    """`local` and a
    sandbox remote are never Gerrit, so the question git-review-check asks
    of them has to be "has HEAD already reached that remote", not "is
    this on Gerrit" -- answerable entirely from local refs.

    Which remotes qualify is deliberately not a name-prefix guess:
    `local` requires $SANDBOX_NAME to
    be set (i.e. we're actually inside a sandbox), and any other remote
    requires a wmf-sbx state file that actually registers it against this
    repo -- see write_sandbox_state()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.far = os.path.join(self.tmp.name, "far")
        subprocess.run(["git", "init", "-q", "-b", "master", self.far],
                        check=True, capture_output=True, text=True)
        identity(self.far)
        commit(self.far, "first", "README")

        self.repo = os.path.join(self.tmp.name, "repo")
        subprocess.run(["git", "clone", "-q", self.far, self.repo],
                        check=True, capture_output=True, text=True)
        identity(self.repo)

        self.state_home = os.path.join(self.tmp.name, "state")

    def run_script(self, *args, cwd=None, sandbox_name=None):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                   XDG_STATE_HOME=self.state_home)
        env.pop("SANDBOX_NAME", None)
        if sandbox_name is not None:
            env["SANDBOX_NAME"] = sandbox_name
        return subprocess.run(
            ["bash", SCRIPT] + list(args),
            cwd=cwd or self.repo, capture_output=True, text=True, env=env,
        )

    def rename_origin(self, name):
        git(self.repo, "remote", "rename", "origin", name)

    def register_sandbox_remote(self, sandbox_name, remote):
        write_sandbox_state(self.state_home, sandbox_name, self.repo, remote)

    def test_head_already_on_local_remote_passes(self):
        self.rename_origin("local")
        result = self.run_script("local", sandbox_name="mw-cite-sbx")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already reachable", result.stdout)

    def test_an_unregistered_remote_falls_through_to_gerrit(self):
        # Gerrit then refuses for its own reason: this repo has no
        # .gitreview. `local` without $SANDBOX_NAME: we can't tell it is
        # really a sandbox's mirror remote, so a host repo that happens to
        # have a remote named `local` must not get the (weaker) local-only
        # check. `sandbox-mw-cite` that no wmf-sbx state file registered
        # (a person made it up by hand) must not get it for its name
        # either. Nor any other remote, nor no remote argument at all.
        current = "origin"
        for remote, args in (("local", ("local",)),
                             ("sandbox-mw-cite", ("sandbox-mw-cite",)),
                             ("upstream", ("upstream",)),
                             ("origin", ())):
            with self.subTest(remote=remote, args=args):
                git(self.repo, "remote", "rename", current, remote)
                current = remote
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("No .gitreview found", result.stderr)

    def test_head_not_yet_on_local_remote_fails(self):
        self.rename_origin("local")
        stray = commit(self.repo, "local-only work", "LOCAL")
        result = self.run_script("local", sandbox_name="mw-cite-sbx")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Not yet reachable from 'local'", result.stderr)
        self.assertIn(stray, result.stderr)

    def test_head_already_on_registered_sandbox_remote_passes(self):
        self.rename_origin("sandbox-mw-cite")
        self.register_sandbox_remote("mw-cite", "sandbox-mw-cite")
        result = self.run_script("sandbox-mw-cite")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_head_not_yet_on_registered_sandbox_remote_fails(self):
        self.rename_origin("sandbox-mw-cite")
        self.register_sandbox_remote("mw-cite", "sandbox-mw-cite")
        commit(self.repo, "local-only work", "LOCAL")
        result = self.run_script("sandbox-mw-cite")
        self.assertEqual(result.returncode, 1)

    def test_registered_remote_for_a_different_repo_does_not_count(self):
        # The state file's hostDir has to match *this* repo -- a remote
        # of the same name registered against some other checkout must
        # not be treated as covering this one.
        self.rename_origin("sandbox-mw-cite")
        other_repo = os.path.join(self.tmp.name, "other-repo")
        os.makedirs(other_repo)
        self.register_sandbox_remote("mw-cite", "sandbox-mw-cite")
        write_sandbox_state(self.state_home, "elsewhere", other_repo,
                             "sandbox-mw-cite")
        # Overwrite with only the "elsewhere" registration, pointed at a
        # repo that isn't this one.
        sandboxes = os.path.join(self.state_home, "wmf-sbx", "sandboxes")
        os.remove(os.path.join(sandboxes, "mw-cite.json"))
        result = self.run_script("sandbox-mw-cite")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No .gitreview found", result.stderr)

    def test_a_later_commit_on_the_remote_still_counts_as_reachable(self):
        # HEAD doesn't have to be the remote's tip -- an ancestor is fine,
        # because a copy of HEAD's own state already exists over there.
        self.rename_origin("local")
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        commit(self.far, "second, only on far", "MORE")
        git(self.repo, "fetch", "-q", "local")
        self.assertEqual(head, git(self.repo, "rev-parse", "HEAD").stdout.strip())
        result = self.run_script("local", sandbox_name="mw-cite-sbx")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_commit_the_agent_has_passes_whatever_the_remote(self):
        # A sandbox branch (refs/remotes/NAME/*) or a hand-off the agent
        # took (refs/remotes/NAME-seen/*, fetched by git sbx-pull).
        self.register_sandbox_remote("sbx-p", "sbx-p")
        commit(self.repo, "handed off", "LOCAL")
        for ref in ("refs/remotes/sbx-p/fix-thing", "refs/remotes/sbx-p-seen/fix-thing"):
            with self.subTest(ref=ref):
                git(self.repo, "update-ref", ref, "HEAD")
                for args in ((), ("origin",)):
                    result = self.run_script(*args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"HEAD is in a sandbox ({ref[len('refs/remotes/'):]})",
                                  result.stdout)
                git(self.repo, "update-ref", "-d", ref)

    def test_a_commit_the_agent_does_not_have_goes_to_gerrit(self):
        self.register_sandbox_remote("sbx-p", "sbx-p")
        git(self.repo, "update-ref", "refs/remotes/sbx-p/fix-thing", "HEAD")
        commit(self.repo, "not handed off", "LOCAL")
        result = self.run_script("origin")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No .gitreview found", result.stderr)

    def test_an_unregistered_remotes_refs_do_not_count(self):
        git(self.repo, "update-ref", "refs/remotes/sbx-p/fix-thing", "HEAD")
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("No .gitreview found", result.stderr)



@unittest.skipUnless(HAVE_GIT and HAVE_BASH, "git and bash required")
class SafeResetAgainstSandboxRemoteTests(unittest.TestCase):
    """End-to-end: git-safe-reset against a sandbox-shaped remote, in a
    repo that *does* have a .gitreview."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.far = os.path.join(self.tmp.name, "far")
        subprocess.run(["git", "init", "-q", "-b", "master", self.far],
                        check=True, capture_output=True, text=True)
        identity(self.far)
        commit(self.far, "first", "README")

        # far keeps its working tree checked out at master, so accept a
        # push there instead of a plain fetch, which git refuses into a
        # checked-out branch.
        git(self.far, "config", "receive.denyCurrentBranch", "updateInstead")

        self.repo = os.path.join(self.tmp.name, "repo")
        subprocess.run(["git", "clone", "-q", self.far, self.repo],
                        check=True, capture_output=True, text=True)
        identity(self.repo)
        git(self.repo, "remote", "rename", "origin", "sandbox-mw-cite")
        with open(os.path.join(self.repo, ".gitreview"), "w", encoding="utf-8") as f:
            f.write("[gerrit]\nhost=gerrit.example.invalid\n")
        git(self.repo, "add", ".gitreview")
        git(self.repo, "commit", "-qm", "add .gitreview")
        # This commit is now HEAD and only exists in `repo`; make the far
        # side of sandbox-mw-cite catch up, the way a real `git push` into
        # a sandbox would.
        git(self.repo, "push", "sandbox-mw-cite", "HEAD:master")

        self.state_home = os.path.join(self.tmp.name, "state")
        write_sandbox_state(self.state_home, "mw-cite", self.repo,
                             "sandbox-mw-cite")

    def run_script(self, *args):
        # git-safe-reset calls git-review-check by bare name; it only
        # resolves if BIN (where both scripts live) is on PATH, the way
        # wmf_sbx_kit's real install puts them there.
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                   XDG_STATE_HOME=self.state_home)
        env.pop("SANDBOX_NAME", None)
        env["PATH"] = BIN + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            ["bash", SAFE_RESET] + list(args),
            cwd=self.repo, capture_output=True, text=True, env=env,
        )

    def test_reset_against_sandbox_remote_no_longer_needs_gerrit(self):
        result = self.run_script("sandbox-mw-cite")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Not uploaded to Gerrit", result.stderr)

    def test_a_sandbox_branch_goes_to_git_sbx_pull(self):
        git(self.repo, "config", "remote.sandbox-mw-cite.wmfSbxSandbox", "mw-cite")
        git(self.far, "checkout", "-q", "-b", "fix-thing")
        tip = commit(self.far, "agent work", "FIX")
        for args in (("sandbox-mw-cite/fix-thing",), ("sandbox-mw-cite", "fix-thing")):
            with self.subTest(args=args):
                git(self.repo, "checkout", "-q", "master")
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("warning: sandbox-mw-cite is a sandbox; running "
                              "`git sbx-pull sandbox-mw-cite/fix-thing`", result.stderr)
                self.assertNotIn("Resetting", result.stdout)
                self.assertEqual(git(self.repo, "rev-parse", "fix-thing").stdout.strip(), tip)
                self.assertEqual(git(self.repo, "config", "branch.fix-thing.wmfsbx")
                                 .stdout.strip(), "mw-cite")



# A curl that records its argv and stdin, and answers $FAKE_CODE with
# $FAKE_BODY, the way `-w '\n%{http_code}'` makes the real one.
FAKE_CURL = """#!/bin/bash
printf '%s\\n' "$@" > "$FAKE_LOG.argv"
if [[ " $* " == *" -H @- "* ]]; then cat > "$FAKE_LOG.stdin"; fi
printf '%s\\n%s' "$FAKE_BODY" "$FAKE_CODE"
"""


@unittest.skipUnless(HAVE_GIT and HAVE_BASH, "git and bash required")
class GerritQueryTests(unittest.TestCase):
    """The Gerrit check, with a fake curl: Gerrit's edge refuses curl's
    own user agent (a 403, "Could not reach Gerrit"), so
    GERRITUA goes to gerrit.wikimedia.org, and only there."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "repo")
        subprocess.run(["git", "init", "-q", "-b", "master", self.repo],
                       check=True, capture_output=True, text=True)
        identity(self.repo)
        commit(self.repo, "first", "README")
        bindir = os.path.join(tmp.name, "bin")
        os.mkdir(bindir)
        with open(os.path.join(bindir, "curl"), "w", encoding="utf-8") as f:
            f.write(FAKE_CURL)
        os.chmod(os.path.join(bindir, "curl"), 0o755)
        self.log = os.path.join(tmp.name, "curl")
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"],
                        FAKE_LOG=self.log, XDG_STATE_HOME=os.path.join(tmp.name, "state"))
        for name in ("SANDBOX_NAME", "GERRITUA"):
            self.env.pop(name, None)

    def gitreview(self, host="gerrit.wikimedia.org"):
        with open(os.path.join(self.repo, ".gitreview"), "w", encoding="utf-8") as f:
            f.write(f"[gerrit]\nhost={host}\nport=29418\n")

    def run_script(self, code="200", body=")]}'\n[]", **env):
        return subprocess.run(["bash", SCRIPT], cwd=self.repo, capture_output=True,
                              text=True, env=dict(self.env, FAKE_CODE=code,
                                                  FAKE_BODY=body, **env))

    def sent(self):
        with open(self.log + ".argv", encoding="utf-8") as f:
            argv = f.read()
        try:
            with open(self.log + ".stdin", encoding="utf-8") as f:
                stdin = f.read()
        except FileNotFoundError:
            stdin = None
        return argv, stdin

    def test_gerritua_goes_to_gerrit_on_stdin(self):
        self.gitreview()
        res = self.run_script(body=")]}'\n[{\"change_id\":\"I1\"}]", GERRITUA="secret-ua")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("All changes uploaded", res.stdout)
        argv, stdin = self.sent()
        self.assertNotIn("secret-ua", argv)
        self.assertEqual(stdin, "User-Agent: secret-ua\n")

    def test_no_other_host_gets_gerritua(self):
        self.gitreview("gerrit.example.invalid")
        self.run_script(GERRITUA="secret-ua")
        argv, stdin = self.sent()
        self.assertNotIn("secret-ua", argv)
        self.assertIsNone(stdin)

    def test_not_uploaded(self):
        self.gitreview()
        res = self.run_script(GERRITUA="secret-ua")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Not uploaded to Gerrit", res.stderr)

    def test_a_refusal_says_the_status_and_names_gerritua(self):
        self.gitreview()
        res = self.run_script(code="403", body="<html>403</html>")
        self.assertEqual(res.returncode, 1)
        self.assertIn("answered HTTP 403", res.stderr)
        self.assertIn("set GERRITUA", res.stderr)


if __name__ == "__main__":
    unittest.main()
