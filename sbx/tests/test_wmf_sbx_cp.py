#!/usr/bin/env python3
"""Unit tests for wmf_sbx/cp.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.cp as cp  # noqa: E402
import wmf_sbx.create as create_mod  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ResolveCpArgTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)
        state_mod.save(
            state_mod.new_state("mw-cite", primary_dir=self.repo.name),
            env=self.env,
        )
        # A sandbox whose state predates the `primaryDir` field (e.g.
        # wmf-claude-sbx, host-confirmed 2026-10-06, responses37.txt) --
        # there is nothing to anchor a relative PATH to, so it must be
        # left exactly as given.
        state_mod.save(state_mod.new_state("mw-old"), env=self.env)

    def test_a_plain_host_path_passes_through(self):
        self.assertEqual(cp.resolve_cp_arg("some/local/file", env=self.env), "some/local/file")

    def test_an_already_valid_sandbox_name_also_anchors_a_relative_path(self):
        # NAME need not be a path shortcut to get a relative PATH
        # anchored -- its own recorded primaryDir is enough.
        self.assertEqual(
            cp.resolve_cp_arg("mw-cite:bar", env=self.env), f"mw-cite:{self.repo.name}/bar",
        )

    def test_a_sandbox_without_a_recorded_primary_dir_is_left_alone(self):
        self.assertEqual(cp.resolve_cp_arg("mw-old:bar", env=self.env), "mw-old:bar")

    def test_an_unknown_sandbox_name_is_left_alone(self):
        # Not ours to diagnose -- upstream `sbx cp` reports an unknown
        # sandbox name itself.
        self.assertEqual(cp.resolve_cp_arg("mw-nope:bar", env=self.env), "mw-nope:bar")

    def test_a_dot_shortcut_before_the_colon_resolves(self):
        # The relative PATH half is anchored at the shortcut's own
        # directory -- upstream `sbx cp` refuses a bare relative PATH
        # (sbx/NOTES.md #99).
        old_cwd = os.getcwd()
        os.chdir(self.repo.name)
        self.addCleanup(os.chdir, old_cwd)
        self.assertEqual(
            cp.resolve_cp_arg(".:bar", env=self.env), f"mw-cite:{self.repo.name}/bar",
        )

    def test_an_absolute_path_shortcut_before_the_colon_resolves(self):
        self.assertEqual(
            cp.resolve_cp_arg(f"{self.repo.name}:bar", env=self.env),
            f"mw-cite:{self.repo.name}/bar",
        )

    def test_an_already_absolute_container_path_is_left_alone(self):
        self.assertEqual(
            cp.resolve_cp_arg(f"{self.repo.name}:/tmp/bar", env=self.env),
            "mw-cite:/tmp/bar",
        )

    def test_no_path_at_all_resolves_to_the_shortcuts_own_directory(self):
        self.assertEqual(
            cp.resolve_cp_arg(f"{self.repo.name}:", env=self.env),
            f"mw-cite:{self.repo.name}",
        )

    def test_the_path_half_survives_untouched_including_extra_colons(self):
        self.assertEqual(
            cp.resolve_cp_arg(f"{self.repo.name}:/etc/foo:bar", env=self.env),
            "mw-cite:/etc/foo:bar",
        )

    def test_no_match_raises(self):
        with self.assertRaises(state_mod.StateError):
            cp.resolve_cp_arg("../nowhere:bar", env=self.env)


if __name__ == "__main__":
    unittest.main()
