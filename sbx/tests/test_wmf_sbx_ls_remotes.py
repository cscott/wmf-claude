#!/usr/bin/env python3
"""Unit tests for wmf_sbx_ls_remotes.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.ls_remotes as m  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
SCRIPT = os.path.join(BIN, "wmf-sbx-ls-remotes")


class FindRepoRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_finds_dot_git_directory(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        self.assertEqual(m.find_repo_root(repo), os.path.realpath(repo))

    def test_walks_up_from_a_nested_subdirectory(self):
        repo = os.path.join(self.tmp.name, "repo")
        nested = os.path.join(repo, "a", "b", "c")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(nested)
        self.assertEqual(m.find_repo_root(nested), os.path.realpath(repo))

    def test_dot_git_file_counts_too(self):
        # A worktree/submodule has a `.git` *file*, not a directory.
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(repo)
        with open(os.path.join(repo, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: /elsewhere\n")
        self.assertEqual(m.find_repo_root(repo), os.path.realpath(repo))

    def test_no_dot_git_anywhere_returns_none(self):
        nested = os.path.join(self.tmp.name, "a", "b")
        os.makedirs(nested)
        self.assertIsNone(m.find_repo_root(nested))

    def test_defaults_to_cwd(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        cwd = os.getcwd()
        try:
            os.chdir(repo)
            self.assertEqual(m.find_repo_root(), os.path.realpath(repo))
        finally:
            os.chdir(cwd)


class FindRemotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)

    def register(self, sandbox_name, host_dir, remote):
        state = state_mod.new_state(sandbox_name)
        state["remotes"].append({
            "hostDir": host_dir, "remote": remote, "url": f"git://x/{remote}",
        })
        state_mod.save(state, env=self.env)

    def test_no_state_files_means_no_matches(self):
        self.assertEqual(m.find_remotes(self.repo, env=self.env), [])

    def test_matches_by_realpath_hostdir(self):
        self.register("mw-cite", self.repo, "sandbox-mw-cite")
        matches = m.find_remotes(os.path.realpath(self.repo), env=self.env)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["sandbox"], "mw-cite")
        self.assertEqual(matches[0]["remote"], "sandbox-mw-cite")
        self.assertEqual(matches[0]["hostDir"], self.repo)
        self.assertEqual(matches[0]["url"], "git://x/sandbox-mw-cite")

    def test_a_remote_registered_against_a_different_repo_does_not_match(self):
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        self.register("mw-cite", other, "sandbox-mw-cite")
        self.assertEqual(m.find_remotes(os.path.realpath(self.repo), env=self.env), [])

    def test_multiple_sandboxes_can_register_the_same_repo(self):
        self.register("mw-cite", self.repo, "sandbox-mw-cite")
        self.register("mw-cite-2", self.repo, "sandbox-mw-cite-2")
        matches = m.find_remotes(os.path.realpath(self.repo), env=self.env)
        self.assertEqual([x["remote"] for x in matches],
                          ["sandbox-mw-cite", "sandbox-mw-cite-2"])

    def test_a_state_file_with_no_remotes_is_skipped_cleanly(self):
        state_mod.save(state_mod.new_state("mw-cite"), env=self.env)
        self.assertEqual(m.find_remotes(os.path.realpath(self.repo), env=self.env), [])


@unittest.skipUnless(sys.executable, "python3 required")
class CliTests(unittest.TestCase):
    """End-to-end: the actual wmf-sbx-ls-remotes script, not the module
    functions -- covers argument parsing and print formatting."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_home = os.path.join(self.tmp.name, "state")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))

    def register(self, sandbox_name, host_dir, remote):
        state = state_mod.new_state(sandbox_name)
        state["remotes"].append({
            "hostDir": host_dir, "remote": remote, "url": f"git://x/{remote}",
        })
        state_mod.save(state, env={"XDG_STATE_HOME": self.state_home})

    def run_script(self, *args):
        env = dict(os.environ, XDG_STATE_HOME=self.state_home)
        return subprocess.run(
            [sys.executable, SCRIPT] + list(args),
            capture_output=True, text=True, env=env,
        )

    def test_default_output_is_just_the_remote_name(self):
        self.register("mw-cite", self.repo, "sandbox-mw-cite")
        result = self.run_script("--path", self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "sandbox-mw-cite\n")

    def test_json_output_is_the_full_entry_per_line(self):
        self.register("mw-cite", self.repo, "sandbox-mw-cite")
        result = self.run_script("--path", self.repo, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        obj = json.loads(result.stdout.strip())
        self.assertEqual(obj["sandbox"], "mw-cite")
        self.assertEqual(obj["remote"], "sandbox-mw-cite")
        self.assertEqual(obj["hostDir"], self.repo)

    def test_no_matches_prints_nothing_and_exits_zero(self):
        result = self.run_script("--path", self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_not_a_git_repo_is_an_error(self):
        outside = os.path.join(self.tmp.name, "not-a-repo")
        os.makedirs(outside)
        result = self.run_script("--path", outside)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not inside a git repository", result.stderr)


if __name__ == "__main__":
    unittest.main()
