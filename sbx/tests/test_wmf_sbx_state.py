#!/usr/bin/env python3
"""Unit tests for wmf_sbx_state.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.state as s  # noqa: E402


class StateDirTests(unittest.TestCase):
    def test_honors_xdg_state_home(self):
        self.assertEqual(
            s.state_dir({"XDG_STATE_HOME": "/x/state"}),
            "/x/state/wmf-sbx/sandboxes",
        )

    def test_falls_back_to_local_state(self):
        self.assertEqual(
            s.state_dir({}),
            os.path.join(os.path.expanduser("~"), ".local", "state", "wmf-sbx", "sandboxes"),
        )

    def test_empty_xdg_state_home_is_treated_as_unset(self):
        # A set-but-empty XDG_STATE_HOME would otherwise put the state
        # directory at the filesystem root.
        self.assertTrue(s.state_dir({"XDG_STATE_HOME": ""}).startswith(os.path.expanduser("~")))


class ValidateNameTests(unittest.TestCase):
    def test_accepts_sbx_shaped_names(self):
        for name in ("mw-cite", "mw-core-2", "a", "Repo.1_x"):
            self.assertEqual(s.validate_name(name), name)

    def test_rejects_path_traversal(self):
        # The name becomes a filename, so this is the boundary that keeps
        # `--name` from writing outside the state directory.
        for name in ("..", "../evil", "a/b", "", None, ".hidden", "-lead"):
            with self.assertRaises(s.StateError):
                s.validate_name(name)

    def test_state_path_validates(self):
        with self.assertRaises(s.StateError):
            s.state_path("../evil", {"XDG_STATE_HOME": "/x"})


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}

    def test_save_then_load(self):
        state = s.new_state("mw-cite", daemon_port=9418, host_port=49281, created="2026-09-07T00:00:00+00:00")
        state["remotes"].append({"hostDir": "/h/Cite", "remote": "sandbox-mw-cite", "url": "git://x/y"})
        path = s.save(state, env=self.env)
        self.assertEqual(path, s.state_path("mw-cite", self.env))
        self.assertEqual(s.load("mw-cite", env=self.env), state)

    def test_save_creates_the_directory(self):
        s.save(s.new_state("mw-cite"), env={"XDG_STATE_HOME": os.path.join(self.tmp.name, "deep")})
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.tmp.name, "deep", "wmf-sbx", "sandboxes", "mw-cite.json")
            )
        )

    def test_save_leaves_no_tempfile_behind(self):
        s.save(s.new_state("mw-cite"), env=self.env)
        entries = os.listdir(s.state_dir(self.env))
        self.assertEqual(entries, ["mw-cite.json"])

    def test_save_overwrites_atomically(self):
        s.save(s.new_state("mw-cite", host_port=1), env=self.env)
        s.save(s.new_state("mw-cite", host_port=2), env=self.env)
        self.assertEqual(s.load("mw-cite", env=self.env)["hostPort"], 2)

    def test_load_missing_returns_none(self):
        self.assertIsNone(s.load("nope", env=self.env))

    def test_load_corrupt_raises(self):
        # Deliberately *not* treated as "no state": silently reading a
        # damaged file as an empty remote list is exactly how a remote
        # would get leaked (see the module docstring).
        path = s.state_path("mw-cite", self.env)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(s.StateError):
            s.load("mw-cite", env=self.env)

    def test_schema_version_recorded(self):
        s.save(s.new_state("mw-cite"), env=self.env)
        with open(s.state_path("mw-cite", self.env), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["schemaVersion"], s.SCHEMA_VERSION)

    def test_new_state_defaults_to_not_attached(self):
        self.assertFalse(s.new_state("mw-cite")["attached"])

    def test_new_state_attached_true_is_honored(self):
        # wmf-sbx-create passes attached=True: by the time it calls
        # new_state(), the real interactive `sbx create ... claude ...`
        # attach already succeeded, so this sandbox's first conversation
        # already happened. See wmf_sbx_create.add_host_remotes().
        self.assertTrue(s.new_state("mw-cite", attached=True)["attached"])


class ResolveNameArgTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)

    def test_bare_name_passes_through_unchanged(self):
        self.assertEqual(s.resolve_name_arg("mw-cite", env=self.env), "mw-cite")

    def test_path_shortcuts_recognized(self):
        for arg in (".", "..", "./", "../", "./foo", "../foo", "/foo"):
            self.assertTrue(s.is_path_shortcut(arg), arg)
        for arg in ("mw-cite", "foo/bar", "..foo", ".git", ""):
            self.assertFalse(s.is_path_shortcut(arg), arg)

    def test_resolves_dot_to_the_matching_sandbox(self):
        s.save(
            s.new_state("mw-cite", primary_dir=self.repo.name), env=self.env,
        )
        old_cwd = os.getcwd()
        os.chdir(self.repo.name)
        self.addCleanup(os.chdir, old_cwd)
        self.assertEqual(s.resolve_name_arg(".", env=self.env), "mw-cite")

    def test_resolves_an_absolute_path(self):
        s.save(
            s.new_state("mw-cite", primary_dir=self.repo.name), env=self.env,
        )
        self.assertEqual(s.resolve_name_arg(self.repo.name, env=self.env), "mw-cite")

    def test_no_match_raises(self):
        with self.assertRaises(s.StateError):
            s.resolve_name_arg(self.repo.name, env=self.env)

    def test_ambiguous_match_raises(self):
        s.save(s.new_state("mw-cite", primary_dir=self.repo.name), env=self.env)
        s.save(s.new_state("mw-ve", primary_dir=self.repo.name), env=self.env)
        with self.assertRaises(s.StateError):
            s.resolve_name_arg(self.repo.name, env=self.env)

    def test_sandboxes_without_primary_dir_are_not_matched(self):
        # A state file written before `primaryDir` existed.
        s.save(s.new_state("mw-cite"), env=self.env)
        with self.assertRaises(s.StateError):
            s.resolve_name_arg(self.repo.name, env=self.env)

    def test_corrupt_state_file_is_skipped_not_raised(self):
        # find_by_primary_dir must not let one bad file break every lookup.
        path = s.state_path("mw-broken", self.env)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        s.save(s.new_state("mw-cite", primary_dir=self.repo.name), env=self.env)
        self.assertEqual(s.resolve_name_arg(self.repo.name, env=self.env), "mw-cite")


class PrimaryDirForTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)

    def test_returns_the_recorded_primary_dir(self):
        s.save(s.new_state("mw-cite", primary_dir=self.repo.name), env=self.env)
        self.assertEqual(s.primary_dir_for("mw-cite", env=self.env), self.repo.name)

    def test_unknown_sandbox_is_none(self):
        self.assertIsNone(s.primary_dir_for("mw-nope", env=self.env))

    def test_sandbox_predating_primary_dir_is_none(self):
        s.save(s.new_state("mw-cite"), env=self.env)
        self.assertIsNone(s.primary_dir_for("mw-cite", env=self.env))

    def test_an_invalid_state_filename_is_none_not_raised(self):
        self.assertIsNone(s.primary_dir_for("../escape", env=self.env))


class DeleteAndListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}

    def test_delete_reports_whether_there_was_one(self):
        s.save(s.new_state("mw-cite"), env=self.env)
        self.assertTrue(s.delete("mw-cite", env=self.env))
        self.assertFalse(s.delete("mw-cite", env=self.env))

    def test_list_names_sorted(self):
        for name in ("mw-core", "mw-cite", "mw-ve"):
            s.save(s.new_state(name), env=self.env)
        self.assertEqual(s.list_names(env=self.env), ["mw-cite", "mw-core", "mw-ve"])

    def test_list_names_missing_dir_is_empty(self):
        self.assertEqual(s.list_names(env={"XDG_STATE_HOME": "/nonexistent"}), [])

    def test_list_names_ignores_strays(self):
        os.makedirs(s.state_dir(self.env))
        for entry in ("notes.txt", ".tmp-abc.json", "sub.dir"):
            open(os.path.join(s.state_dir(self.env), entry), "w").close()
        s.save(s.new_state("mw-cite"), env=self.env)
        self.assertEqual(s.list_names(env=self.env), ["mw-cite"])


if __name__ == "__main__":
    unittest.main()
