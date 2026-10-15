#!/usr/bin/env python3
"""Tests for the sandbox's `git sbx-pull` (sbx/helpers/git-sbx-pull) -- run
with:
  python3 -m unittest discover -s sbx/tests -v

Against real repositories in tempdirs: an upstream (`up`), the host repo
(a clone of it, with the sandbox remote `sbx-p`) and the guest clone
(`local` is the host, `origin` the upstream, as sandbox-repos.sh makes
it). The script is loaded as a module, in the guest's directory; Gerrit
is a set of shas."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import isolation


def setUpModule():
    isolation.isolate()


SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "helpers",
                      "git-sbx-pull")
HAVE_GIT = shutil.which("git") is not None


def load():
    loader = importlib.machinery.SourceFileLoader("git_sbx_pull_guest", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # No __pycache__ beside the helper, which the image copies by name.
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


guest_mod = load()


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True,
                          text=True, check=True)


def out(cwd, *args):
    return git(cwd, *args).stdout.strip()


def commit(repo, message, name="file"):
    with open(os.path.join(repo, name), "a", encoding="utf-8") as f:
        f.write(message + "\n")
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)
    return out(repo, "rev-parse", "HEAD")


def identity(repo):
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")


@unittest.skipUnless(HAVE_GIT, "git not installed")
class GuestPullTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.up = os.path.join(tmp.name, "up")
        subprocess.run(["git", "init", "-q", "-b", "master", self.up], check=True)
        identity(self.up)
        commit(self.up, "first")
        self.host = os.path.join(tmp.name, "host")
        subprocess.run(["git", "clone", "-q", self.up, self.host], check=True)
        identity(self.host)
        self.guest = os.path.join(tmp.name, "guest")
        subprocess.run(["git", "clone", "-q", self.host, self.guest], check=True)
        identity(self.guest)
        git(self.guest, "remote", "rename", "origin", "local")
        git(self.guest, "remote", "add", "origin", self.up)
        git(self.guest, "fetch", "-q", "origin")
        git(self.host, "remote", "add", "--no-tags", "sbx-p", self.guest)
        self.gerrit = set()
        cwd = os.getcwd()
        os.chdir(self.guest)
        self.addCleanup(os.chdir, cwd)

    def pull(self, *argv, sandbox="sbx-p"):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"SANDBOX_NAME": sandbox} if sandbox else {}
        with mock.patch.object(guest_mod, "gerrit_has",
                               lambda root, sha, env: sha in self.gerrit), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = guest_mod.main(list(argv), env=env)
        self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()
        return code

    def host_pulls(self, branch):
        """What the host's git sbx-pull does, in short."""
        git(self.host, "fetch", "-q", "sbx-p", "+refs/heads/*:refs/remotes/sbx-p/*")
        git(self.host, "checkout", "-q", "-B", branch, f"refs/remotes/sbx-p/{branch}")

    def amend(self, repo, message):
        with open(os.path.join(repo, "file"), "a", encoding="utf-8") as f:
            f.write(message + "\n")
        git(repo, "commit", "-qa", "--amend", "-m", message)
        return out(repo, "rev-parse", "HEAD")

    def agent_work(self, branch="fix-thing"):
        git(self.guest, "checkout", "-q", "-b", branch, "--track", "origin/master")
        return commit(self.guest, "agent work")

    def head(self):
        return out(self.guest, "rev-parse", "HEAD")

    def test_a_hand_off_after_the_engineers_edit(self):
        old = self.agent_work()
        self.host_pulls("fix-thing")
        new = self.amend(self.host, "engineer's edit")
        self.assertEqual(self.pull(), 0, self.stderr)
        self.assertEqual(out(self.guest, "symbolic-ref", "--short", "HEAD"), "fix-thing")
        self.assertEqual(self.head(), new)
        self.assertEqual(out(self.guest, "for-each-ref", "--format=%(upstream:short)",
                             "refs/heads/fix-thing"), "local/fix-thing")
        self.assertEqual(out(self.guest, "rev-parse", "refs/sbx/seen/fix-thing"), new)
        self.assertIn(f"old tip: {old}\nnew tip: {new}\n", self.stdout)
        self.assertIn(f"git range-diff {old[:12]}...{new[:12]}", self.stdout)

    def test_work_the_host_has_not_pulled_stops_it(self):
        self.agent_work()
        git(self.host, "checkout", "-q", "-b", "fix-thing")
        new = commit(self.host, "engineer's own")
        self.assertEqual(self.pull(), 1)
        self.assertIn("has commits that the host does not have", self.stderr)
        self.assertIn("git sbx-pull` on the host first", self.stderr)
        self.assertEqual(self.pull("-f"), 0, self.stderr)
        self.assertEqual(self.head(), new)

    def test_work_on_gerrit_may_be_left(self):
        old = self.agent_work()
        git(self.host, "checkout", "-q", "-b", "fix-thing")
        new = commit(self.host, "engineer's own")
        self.gerrit.add(old)
        self.assertEqual(self.pull(), 0, self.stderr)
        self.assertEqual(self.head(), new)

    def test_leaving_another_branch_the_host_has_not_pulled_stops_it(self):
        self.agent_work("other")
        git(self.host, "checkout", "-q", "-b", "fix-thing")
        commit(self.host, "engineer's own")
        self.assertEqual(self.pull(), 1)
        self.assertIn("other (", self.stderr)

    def test_a_wip_hand_off_from_master_needs_a_name(self):
        wip = commit(self.host, "WIP: the start of a thing")
        self.assertEqual(self.pull(), 1)
        self.assertIn("the host is on master, with 1 commit(s) that origin does not "
                      "have; name a branch for the work: git sbx-pull --as NAME", self.stderr)
        self.assertIn("git log -p origin/master..local/master", self.stderr)
        self.assertEqual(self.pull("--as", "start-thing"), 0, self.stderr)
        self.assertEqual(out(self.guest, "symbolic-ref", "--short", "HEAD"), "start-thing")
        self.assertEqual(self.head(), wip)
        self.assertEqual(out(self.guest, "for-each-ref", "--format=%(upstream:short)",
                             "refs/heads/start-thing"), "local/master")
        self.assertEqual(out(self.guest, "rev-parse", "refs/sbx/seen/start-thing"), wip)

    def test_a_clean_master_is_nothing_to_take(self):
        self.assertEqual(self.pull(), 1)
        self.assertIn("nothing that origin does not have", self.stderr)

    def test_a_named_branch(self):
        git(self.host, "branch", "-q", "fix-thing")
        git(self.host, "checkout", "-q", "fix-thing")
        tip = commit(self.host, "engineer's")
        git(self.host, "checkout", "-q", "master")
        self.assertEqual(self.pull("fix-thing"), 0, self.stderr)
        self.assertEqual(self.head(), tip)
        self.assertEqual(self.pull("nosuch"), 1)
        self.assertIn("the host has no branch nosuch", self.stderr)

    def test_a_dirty_tree_stops_it(self):
        git(self.host, "checkout", "-q", "-b", "fix-thing")
        commit(self.host, "engineer's")
        with open(os.path.join(self.guest, "file"), "a", encoding="utf-8") as f:
            f.write("dirty\n")
        self.assertEqual(self.pull("-f"), 1)
        self.assertIn("the tree has changes", self.stderr)

    def test_outside_a_sandbox(self):
        self.assertEqual(self.pull(sandbox=None), 1)
        self.assertIn("$SANDBOX_NAME is not set", self.stderr)


class GerritUserAgentTests(unittest.TestCase):
    """GERRITUA goes to gerrit.wikimedia.org only: it is a secret."""

    def ask(self, host):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, ".gitreview"), "w", encoding="utf-8") as f:
            f.write(f"[gerrit]\nhost={host}\nport=29418\n")
        sent = []

        def fake(req, timeout):
            sent.append(req)
            return io.BytesIO(b")]}'\n[]\n")
        with mock.patch.object(guest_mod.urllib.request, "urlopen", fake):
            guest_mod.gerrit_has(tmp.name, "abc", {"GERRITUA": "secret-ua"})
        (req,) = sent
        return req

    def test_gerrit_gets_it_unredirected(self):
        req = self.ask("gerrit.wikimedia.org")
        self.assertEqual(req.unredirected_hdrs, {"User-agent": "secret-ua"})
        self.assertEqual(req.headers, {})

    def test_another_host_does_not(self):
        req = self.ask("gerrit.example.invalid")
        self.assertEqual(req.header_items(), [])


if __name__ == "__main__":
    unittest.main()
