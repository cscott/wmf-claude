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

import json
import os
import shutil
import subprocess
import tempfile
import unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
SCRIPT = os.path.join(BIN, "git-review-check")
SAFE_RESET = os.path.join(BIN, "git-safe-reset")

HAVE_GIT = shutil.which("git") is not None
HAVE_BASH = shutil.which("bash") is not None


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", cwd] + list(args),
        capture_output=True, text=True, check=check,
    )


def commit(repo, name, message):
    with open(os.path.join(repo, name), "a", encoding="utf-8") as f:
        f.write(message + "\n")
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def write_sandbox_state(state_home, sandbox_name, host_dir, remote):
    """A minimal wmf-sbx state file -- just enough of the shape
    wmf_sbx_state.new_state()/sync_remotes() produce for is_local_check_target()
    to find `remote` registered against `host_dir`."""
    sandboxes = os.path.join(state_home, "wmf-sbx", "sandboxes")
    os.makedirs(sandboxes, exist_ok=True)
    state = {
        "schemaVersion": 1,
        "name": sandbox_name,
        "created": None,
        "daemonPort": None,
        "hostPort": None,
        "attached": True,
        "remotes": [{
            "hostDir": os.path.realpath(host_dir),
            "remote": remote,
            "url": f"git://127.0.0.1/{remote}",
        }],
        "skipped": [],
    }
    with open(os.path.join(sandboxes, f"{sandbox_name}.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f)


@unittest.skipUnless(HAVE_GIT and HAVE_BASH, "git and bash required")
class LocalIshRemoteTests(unittest.TestCase):
    """cananian, 2026-09-08 (sbx/NOTES.md "Still to do"): `local` and a
    sandbox remote are never Gerrit, so the question git-review-check asks
    of them has to be "has HEAD already reached that remote", not "is
    this on Gerrit" -- answerable entirely from local refs.

    Which remotes qualify is deliberately not a name-prefix guess
    (cananian's 2026-09-08 refinement): `local` requires $SANDBOX_NAME to
    be set (i.e. we're actually inside a sandbox), and any other remote
    requires a wmf-sbx state file that actually registers it against this
    repo -- see write_sandbox_state()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.far = os.path.join(self.tmp.name, "far")
        subprocess.run(["git", "init", "-q", "-b", "master", self.far],
                        check=True, capture_output=True, text=True)
        git(self.far, "config", "user.email", "test@example.invalid")
        git(self.far, "config", "user.name", "Test")
        commit(self.far, "README", "first")

        self.repo = os.path.join(self.tmp.name, "repo")
        subprocess.run(["git", "clone", "-q", self.far, self.repo],
                        check=True, capture_output=True, text=True)
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")

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

    def test_local_remote_without_sandbox_name_falls_through_to_gerrit(self):
        # No $SANDBOX_NAME -> we can't tell this is really a sandbox's
        # mirror remote, so a host repo that happens to have a remote
        # named `local` must not get the (weaker) local-only check.
        self.rename_origin("local")
        result = self.run_script("local")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No .gitreview found", result.stderr)

    def test_head_not_yet_on_local_remote_fails(self):
        self.rename_origin("local")
        stray = commit(self.repo, "LOCAL", "local-only work")
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
        commit(self.repo, "LOCAL", "local-only work")
        result = self.run_script("sandbox-mw-cite")
        self.assertEqual(result.returncode, 1)

    def test_sandbox_shaped_name_with_no_state_file_falls_through_to_gerrit(self):
        # A remote literally named `sandbox-mw-cite` that no wmf-sbx state
        # file actually registered (e.g. a person made it up by hand)
        # must not get the local-only check just because of its name.
        self.rename_origin("sandbox-mw-cite")
        result = self.run_script("sandbox-mw-cite")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No .gitreview found", result.stderr)

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
        commit(self.far, "MORE", "second, only on far")
        git(self.repo, "fetch", "-q", "local")
        self.assertEqual(head, git(self.repo, "rev-parse", "HEAD").stdout.strip())
        result = self.run_script("local", sandbox_name="mw-cite-sbx")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_remote_that_is_not_local_or_sandbox_shaped_is_unaffected(self):
        # No .gitreview in this repo, so the (unchanged) Gerrit path
        # should still refuse for its own original reason.
        self.rename_origin("upstream")
        result = self.run_script("upstream")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No .gitreview found", result.stderr)

    def test_no_remote_argument_keeps_the_original_gerrit_only_behavior(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("No .gitreview found", result.stderr)


@unittest.skipUnless(HAVE_GIT and HAVE_BASH, "git and bash required")
class SafeResetAgainstSandboxRemoteTests(unittest.TestCase):
    """End-to-end: git-safe-reset against a sandbox-shaped remote, in a
    repo that *does* have a .gitreview, is exactly the case that used to
    fail outright."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.far = os.path.join(self.tmp.name, "far")
        subprocess.run(["git", "init", "-q", "-b", "master", self.far],
                        check=True, capture_output=True, text=True)
        git(self.far, "config", "user.email", "test@example.invalid")
        git(self.far, "config", "user.name", "Test")
        commit(self.far, "README", "first")

        # far keeps its working tree checked out at master, so accept a
        # push there instead of a plain fetch, which git refuses into a
        # checked-out branch.
        git(self.far, "config", "receive.denyCurrentBranch", "updateInstead")

        self.repo = os.path.join(self.tmp.name, "repo")
        subprocess.run(["git", "clone", "-q", self.far, self.repo],
                        check=True, capture_output=True, text=True)
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
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


if __name__ == "__main__":
    unittest.main()
