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


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)
        state_mod.save(
            state_mod.new_state("mw-cite", primary_dir=self.repo.name),
            env=self.env,
        )
        # A sandbox whose state predates `primaryDir` -- its relative PATH
        # can't be anchored, so it must still pass through untouched.
        state_mod.save(state_mod.new_state("mw-old"), env=self.env)
        # Patched the same way test_wmf_sbx_exec.py does: the actual
        # host-remote re-point is start_and_restore's business, not
        # something a cp test needs to drive through `run`.
        self.refreshed = []
        patcher = mock.patch.object(
            create_mod, "refresh_host_port",
            lambda name, state, **kw: (self.refreshed.append(name), 32784)[1],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_run(self, calls, rc=0, exec_rc=0):
        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == [create_mod.WMF_SBX, "--upstream", "exec"] and cmd[-1] == "true":
                # start_sandbox's own probe: whether the container came up.
                return FakeCompletedProcess(exec_rc)
            if "wmf-sbx-setup" in cmd:
                # The startup-ran/mount-verify calls start_and_restore
                # makes on our behalf, not the caller's command -- always
                # succeed so `rc` only governs the actual cp below.
                return FakeCompletedProcess(0)
            if cmd[:3] == [create_mod.WMF_SBX, "--upstream", "exec"]:
                # ensure_git_daemon's socket probe: "already listening".
                return FakeCompletedProcess(0)
            return FakeCompletedProcess(rc)
        return run

    def _main(self, argv, run):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cp.main(argv, run=run, env=self.env)
        return code, err.getvalue()

    def _restore_calls(self, name):
        # What start_and_restore does before cp's own command, in order:
        # start the container, confirm the startup commands ran, wait for
        # the mount layout, confirm the git daemon is up. The host-remote
        # re-point is mocked above rather than running through `run`.
        # sbx/NOTES.md #103: a sandbox cp hasn't touched yet is not
        # running, and plain `sbx cp` used to race this instead of
        # waiting for it the way `wmf-sbx exec` already does.
        return [
            [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "true"],
            [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "python3",
             "-c", create_mod.STARTUP_RAN_PROBE],
            [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "python3",
             "/home/agent/wmf-sbx-setup", "--verify", "--wait=20"],
            [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "python3",
             "-c", 'import socket, sys; sys.exit(0 if '
             'socket.socket().connect_ex(("127.0.0.1", 9977)) == 0 else 1)'],
        ]

    def test_wmf_sbx_cp_foo_dot_colon_bar_resolves_the_destination(self):
        # The exact scenario from sbx/NOTES.md "Allow shortcut sandbox
        # names": a directory-name shortcut for the sandbox, then a
        # relative destination path inside it.
        old_cwd = os.getcwd()
        os.chdir(self.repo.name)
        self.addCleanup(os.chdir, old_cwd)

        calls = []
        code, err = self._main(["foo", ".:bar"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertIn("resolved", err)
        self.assertEqual(calls, self._restore_calls("mw-cite") + [
            [create_mod.WMF_SBX, "--upstream", "cp", "foo", f"mw-cite:{self.repo.name}/bar"],
        ])
        self.assertEqual(self.refreshed, ["mw-cite"])

    def test_a_source_side_shortcut_also_resolves(self):
        calls = []
        code, _err = self._main([f"{self.repo.name}:bar", "dest"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertEqual(calls, self._restore_calls("mw-cite") + [
            [create_mod.WMF_SBX, "--upstream", "cp", f"mw-cite:{self.repo.name}/bar", "dest"],
        ])

    def test_a_bare_sandbox_name_also_gets_its_relative_path_anchored(self):
        # "mw-cite" is already a valid sandbox name -- no `.`/`..`/absolute
        # shortcut involved -- but its PATH is still relative, so it's
        # anchored at the sandbox's own recorded primaryDir too.
        calls = []
        code, err = self._main(["mw-cite:bar", "dest"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertIn("resolved", err)
        self.assertEqual(calls, self._restore_calls("mw-cite") + [
            [create_mod.WMF_SBX, "--upstream", "cp", f"mw-cite:{self.repo.name}/bar", "dest"],
        ])

    def test_a_bare_sandbox_name_without_a_primary_dir_is_untouched(self):
        # "mw-old" predates primaryDir -- nothing to anchor against, so
        # its relative PATH is left exactly as given (today's behavior).
        # It still needs starting and waiting-for first, same as any
        # other real sandbox name.
        calls = []
        code, err = self._main(["mw-old:bar", "dest"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertNotIn("resolved", err)
        self.assertEqual(calls, self._restore_calls("mw-old") + [
            [create_mod.WMF_SBX, "--upstream", "cp", "mw-old:bar", "dest"],
        ])

    def test_no_matching_sandbox_is_refused_before_anything_runs(self):
        calls = []
        code, err = self._main(["foo", "../nowhere:bar"], self.fake_run(calls))
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("error:", err)

    def test_a_sandbox_that_will_not_start_is_an_error_and_cp_never_runs(self):
        # Mirrors test_wmf_sbx_exec.py's own case for the same failure:
        # start_sandbox said no, so there is nothing to copy into.
        calls = []
        code, err = self._main(
            ["foo", "mw-cite:bar"], self.fake_run(calls, exec_rc=1),
        )
        self.assertEqual(code, 1)
        self.assertIn("could not start", err)
        self.assertFalse(any(c[:3] == [create_mod.WMF_SBX, "--upstream", "cp"] for c in calls))

    def test_follow_link_is_forwarded(self):
        calls = []
        self._main(["-L", "foo", "mw-cite:bar"], self.fake_run(calls))
        self.assertEqual(calls, self._restore_calls("mw-cite") + [
            [create_mod.WMF_SBX, "--upstream", "cp", "-L", "foo", f"mw-cite:{self.repo.name}/bar"],
        ])

    def test_debug_is_a_global_flag_placed_before_the_subcommand(self):
        calls = []
        self._main(["-D", "foo", "mw-cite:bar"], self.fake_run(calls))
        self.assertEqual(calls, self._restore_calls("mw-cite") + [
            [create_mod.WMF_SBX, "--upstream", "-D", "cp", "foo", f"mw-cite:{self.repo.name}/bar"],
        ])

    def test_the_commands_exit_code_is_returned(self):
        calls = []
        code, _err = self._main(["foo", "mw-cite:bar"], self.fake_run(calls, rc=3))
        self.assertEqual(code, 3)

    def test_a_sandbox_with_no_recorded_state_still_gets_started(self):
        # Not ours to diagnose (resolve_cp_arg already leaves it alone),
        # but it's still a real NAME as far as `sbx exec ... true` is
        # concerned, so the same wait applies before the cp itself runs.
        calls = []
        code, _err = self._main(["foo", "mw-other:bar"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertEqual(calls, self._restore_calls("mw-other") + [
            [create_mod.WMF_SBX, "--upstream", "cp", "foo", "mw-other:bar"],
        ])


if __name__ == "__main__":
    unittest.main()
