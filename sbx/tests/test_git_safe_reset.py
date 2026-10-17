#!/usr/bin/env python3
"""Tests for the git-safe-reset helper -- run with:
  python3 -m unittest discover -s sbx/tests -v

The script is what `wmf_sbx_setup.reset_to_upstream` runs in every
dependency clone, and what the engineer runs by hand afterwards. It is
driven here against **real repositories** in tempdirs: its whole job is
git's behaviour (does the tree look clean, does this remote exist, does
the reset land where it should), so faking git would only re-assert my
reading of it.

No `.gitreview` is created in these repos, which keeps git-review-check
-- a separate script, and not the subject here -- out of the path.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import isolation
from fakes import commit, git, identity


def setUpModule():
    isolation.isolate()


BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
SCRIPT = os.path.join(BIN, "git-safe-reset")

HAVE_GIT = shutil.which("git") is not None
HAVE_BASH = shutil.which("bash") is not None


@unittest.skipUnless(HAVE_GIT and HAVE_BASH, "git and bash required")
class GitSafeResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.upstream = os.path.join(self.tmp.name, "upstream")
        subprocess.run(["git", "init", "-q", "-b", "master", self.upstream],
                       check=True, capture_output=True, text=True)
        identity(self.upstream)
        commit(self.upstream, "first", "README")

        self.clone = os.path.join(self.tmp.name, "clone")
        subprocess.run(["git", "clone", "-q", self.upstream, self.clone],
                       check=True, capture_output=True, text=True)
        identity(self.clone)
        # Something to be reset away, and something to be reset *to*.
        self.stray = commit(self.clone, "local work", "LOCAL")
        self.target = commit(self.upstream, "second", "README")

    def run_script(self, *args):
        return subprocess.run(
            ["bash", SCRIPT] + list(args),
            cwd=self.clone, capture_output=True, text=True,
            # A fresh env would lose PATH; git-safe-reset shells out to git.
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )

    def head(self):
        return git(self.clone, "rev-parse", "HEAD").stdout.strip()

    def assert_reset_worked(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resetting to origin/master", result.stdout)
        self.assertEqual(self.head(), self.target)

    def test_each_way_to_name_origin_master(self):
        # No arguments, a bare remote, a remote and a branch, and the
        # slash form: how git itself prints a remote-tracking branch, so
        # it's the form you have in your scrollback.
        for args in ((), ("origin",), ("origin", "master"), ("origin/master",)):
            with self.subTest(args=args):
                git(self.clone, "reset", "-q", "--hard", self.stray)
                self.assert_reset_worked(self.run_script(*args))

    def test_the_slash_form_splits_on_the_first_slash_only(self):
        # Branch names routinely contain slashes; remote names do not.
        git(self.upstream, "checkout", "-qb", "feature/wip")
        target = commit(self.upstream, "feature work", "FEATURE")
        result = self.run_script("origin/feature/wip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resetting to origin/feature/wip", result.stdout)
        self.assertEqual(self.head(), target)

    def test_a_remote_whose_name_contains_a_slash_still_wins(self):
        # The split is a convenience, not a reinterpretation: if the whole
        # argument names a real remote, that is what it means.
        git(self.clone, "remote", "add", "weird/name", self.upstream)
        result = self.run_script("weird/name")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resetting to weird/name/master", result.stdout)
        self.assertEqual(self.head(), self.target)

    def test_an_unknown_remote_is_named_not_guessed_at(self):
        # `nosuch/master` is a mistake, and it should look like one rather
        # than quietly resetting to some other remote's master -- or, as
        # it did before the check, failing three commands later with
        # "pathspec 'main' did not match any file(s) known to git".
        result = self.run_script("nosuch/master")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No such remote 'nosuch/master'", result.stderr)
        self.assertIn("origin", result.stderr)
        self.assertEqual(self.head(), self.stray)

    def test_a_dirty_tree_is_refused(self):
        with open(os.path.join(self.clone, "README"), "a", encoding="utf-8") as f:
            f.write("uncommitted\n")
        result = self.run_script("origin/master")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not clean", result.stdout)
        self.assertEqual(self.head(), self.stray)

    def test_main_is_the_fallback_when_there_is_no_master(self):
        main_upstream = os.path.join(self.tmp.name, "main-upstream")
        subprocess.run(["git", "init", "-q", "-b", "main", main_upstream],
                       check=True, capture_output=True, text=True)
        identity(main_upstream)
        commit(main_upstream, "first", "README")
        clone = os.path.join(self.tmp.name, "main-clone")
        subprocess.run(["git", "clone", "-q", main_upstream, clone],
                       check=True, capture_output=True, text=True)
        result = subprocess.run(
            ["bash", SCRIPT], cwd=clone, capture_output=True, text=True,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resetting to origin/main", result.stdout)


if __name__ == "__main__":
    unittest.main()
