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

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
SCRIPT = os.path.join(BIN, "git-safe-reset")

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


@unittest.skipUnless(HAVE_GIT and HAVE_BASH, "git and bash required")
class GitSafeResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.upstream = os.path.join(self.tmp.name, "upstream")
        subprocess.run(["git", "init", "-q", "-b", "master", self.upstream],
                       check=True, capture_output=True, text=True)
        git(self.upstream, "config", "user.email", "test@example.invalid")
        git(self.upstream, "config", "user.name", "Test")
        commit(self.upstream, "README", "first")

        self.clone = os.path.join(self.tmp.name, "clone")
        subprocess.run(["git", "clone", "-q", self.upstream, self.clone],
                       check=True, capture_output=True, text=True)
        git(self.clone, "config", "user.email", "test@example.invalid")
        git(self.clone, "config", "user.name", "Test")
        # Something to be reset away, and something to be reset *to*.
        self.stray = commit(self.clone, "LOCAL", "local work")
        self.target = commit(self.upstream, "README", "second")

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

    def test_no_arguments_defaults_to_origin_and_master(self):
        self.assert_reset_worked(self.run_script())

    def test_a_bare_remote_name(self):
        self.assert_reset_worked(self.run_script("origin"))

    def test_a_remote_and_a_branch(self):
        self.assert_reset_worked(self.run_script("origin", "master"))

    def test_the_slash_form_is_the_same_thing(self):
        # How git itself prints a remote-tracking branch, so it's the form
        # you have in your scrollback.
        self.assert_reset_worked(self.run_script("origin/master"))

    def test_the_slash_form_splits_on_the_first_slash_only(self):
        # Branch names routinely contain slashes; remote names do not.
        git(self.upstream, "checkout", "-qb", "feature/wip")
        target = commit(self.upstream, "FEATURE", "feature work")
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
        git(main_upstream, "config", "user.email", "test@example.invalid")
        git(main_upstream, "config", "user.name", "Test")
        commit(main_upstream, "README", "first")
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
