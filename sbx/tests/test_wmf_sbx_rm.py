#!/usr/bin/env python3
"""Unit tests for wmf_sbx_rm.py -- run with:
  python3 -m unittest discover -s sbx/tests -v

`sbx rm` is unrecoverable and its exit status is uninformative in both
directions (a nonexistent sandbox exits 1; *declining the prompt exits
0*), so the tests that matter here are the negative ones: that declining
leaves every host remote untouched, and that the unfetched-work guard
blocks before anything is destroyed.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.remotes as remotes_mod  # noqa: E402
import wmf_sbx.rm as rm  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", cwd] + list(args), capture_output=True, text=True, check=True
    )


def make_repo(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", path], check=True,
                   capture_output=True, text=True)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    with open(os.path.join(path, "README"), "w", encoding="utf-8") as f:
        f.write("hello\n")
    git(path, "add", "README")
    git(path, "commit", "-qm", "initial")
    return path


@unittest.skipUnless(HAVE_GIT, "git not installed")
class ArgumentTests(unittest.TestCase):
    def test_no_name_and_no_prune_is_an_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            rm.main([])
        self.assertIn("--prune", err.getvalue())


if __name__ == "__main__":
    unittest.main()
