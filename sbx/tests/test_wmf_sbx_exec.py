#!/usr/bin/env python3
"""Unit tests for wmf_sbx/exec.py -- run with:
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

import wmf_sbx.create as create_mod  # noqa: E402
import wmf_sbx.exec as execmod  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        self.refreshed = []
        patcher = mock.patch.object(
            create_mod, "refresh_host_port",
            lambda name, state, **kw: (self.refreshed.append(name), 32784)[1],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        state = state_mod.new_state("mw-cite", daemon_port=9977, host_port=32783)
        state_mod.save(state, env=self.env)

    def fake_run(self, calls, exec_rc=0, cmd_rc=0):
        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == [create_mod.WMF_SBX, "--upstream", "exec"] and cmd[-1] == "true":
                return FakeCompletedProcess(exec_rc)
            if "wmf-sbx-setup" in cmd:
                # The mount-verify/--restore calls start_and_restore makes
                # on our behalf, not the caller's command -- always succeed
                # so a test's cmd_rc only governs the actual command below.
                return FakeCompletedProcess(0)
            if cmd[:3] == [create_mod.WMF_SBX, "--upstream", "exec"]:
                return FakeCompletedProcess(cmd_rc)
            return FakeCompletedProcess(0)
        return run

    def _main(self, argv, run):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = execmod.main(argv, run=run, env=self.env)
        return code, err.getvalue()

    def test_restores_then_runs_the_command(self):
        calls = []
        code, _err = self._main(
            ["mw-cite", "git", "log", "--oneline"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "true"],
            # The startup commands, then the mounts: a `wmf-sbx exec` that
            # starts the container gets the same guarantees as a resume
            # (§97). Only the CLAUDE.md report is skipped here.
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "python3",
             "-c", create_mod.STARTUP_RAN_PROBE],
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "python3",
             "/home/agent/wmf-sbx-setup", "--verify", "--wait=20"],
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "python3",
             "-c", 'import socket, sys; sys.exit(0 if '
             'socket.socket().connect_ex(("127.0.0.1", 9977)) == 0 else 1)'],
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "git", "log", "--oneline"],
        ])
        self.assertEqual(self.refreshed, ["mw-cite"])

    def test_the_commands_exit_code_is_returned(self):
        calls = []
        code, _err = self._main(
            ["mw-cite", "false"], self.fake_run(calls, cmd_rc=3)
        )
        self.assertEqual(code, 3)

    def test_a_sandbox_that_will_not_start_is_an_error_and_the_command_never_runs(self):
        calls = []
        code, err = self._main(
            ["mw-cite", "git", "log"], self.fake_run(calls, exec_rc=1)
        )
        self.assertEqual(code, 1)
        self.assertIn("could not start", err)
        self.assertFalse(any(c[-2:] == ["git", "log"] for c in calls))

    def test_no_command_is_an_error(self):
        calls = []
        with self.assertRaises(SystemExit) as ctx:
            self._main(["mw-cite"], self.fake_run(calls))
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(calls, [])

    def test_no_remotes_skips_the_repoint(self):
        calls = []
        code, _err = self._main(
            ["--no-remotes", "mw-cite", "true"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.refreshed, [])

    def test_no_restore_skips_the_mount_pass(self):
        calls = []
        self._main(["--no-restore", "mw-cite", "true"], self.fake_run(calls))
        self.assertFalse(any("--restore" in c or "--verify" in c for c in calls))

    def test_dry_run_starts_and_runs_nothing(self):
        calls = []
        code, err = self._main(
            ["--dry-run", "mw-cite", "git", "log"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.refreshed, [])
        self.assertIn("exec mw-cite git log", err)

    def test_a_path_shortcut_resolves_to_its_sandbox(self):
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        state = state_mod.load("mw-cite", env=self.env)
        state["primaryDir"] = repo.name
        state_mod.save(state, env=self.env)

        calls = []
        code, err = self._main([repo.name, "true"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertIn("resolved", err)
        self.assertIn("mw-cite", err)
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "true"])

    def test_an_invalid_name_is_refused_before_anything_runs(self):
        calls = []
        code, err = self._main(
            ["../escape", "true"], self.fake_run(calls)
        )
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("error:", err)

    def test_a_sandbox_with_no_recorded_state_still_execs(self):
        calls = []
        code, _err = self._main(["mw-other", "true"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertEqual(self.refreshed, [])
        self.assertEqual(
            calls[-1], [create_mod.WMF_SBX, "--upstream", "exec", "mw-other", "true"]
        )

    def test_a_command_that_looks_like_our_own_flags_is_passed_through_untouched(self):
        # The whole point of argparse.REMAINDER: once NAME is consumed,
        # nothing after it -- however dash-prefixed -- is parsed as one
        # of wmf-sbx-exec's own options.
        calls = []
        code, _err = self._main(
            ["mw-cite", "ls", "--no-restore", "-i"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            calls[-1],
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "ls", "--no-restore", "-i"],
        )
        # --no-restore in the *command* must not have skipped our own
        # restore pass -- only a leading --no-restore does that.
        self.assertTrue(any("--verify" in c for c in calls))

    def test_it_forwards_to_the_real_sbx_exec(self):
        # The to-do's own example: `wmf-sbx exec -it NAME bash`.
        calls = []
        code, _err = self._main(
            ["-it", "mw-cite", "bash"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            calls[-1],
            [create_mod.WMF_SBX, "--upstream", "exec", "-i", "-t", "mw-cite", "bash"],
        )

    def test_env_and_env_file_are_repeatable_and_forwarded_in_order(self):
        calls = []
        code, _err = self._main(
            ["-e", "A=1", "--env-file", "f1", "-e", "B=2", "--env-file", "f2",
             "mw-cite", "true"],
            self.fake_run(calls),
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            calls[-1],
            [create_mod.WMF_SBX, "--upstream", "exec", "-e", "A=1", "-e", "B=2",
             "--env-file", "f1", "--env-file", "f2", "mw-cite", "true"],
        )

    def test_user_workdir_privileged_and_detach_keys_are_forwarded(self):
        calls = []
        code, _err = self._main(
            ["-u", "root:root", "-w", "/srv", "--privileged",
             "--detach-keys", "ctrl-p", "mw-cite", "true"],
            self.fake_run(calls),
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            calls[-1],
            [create_mod.WMF_SBX, "--upstream", "exec", "--detach-keys", "ctrl-p",
             "--privileged", "-u", "root:root", "-w", "/srv", "mw-cite", "true"],
        )

    def test_debug_is_a_global_flag_placed_before_the_subcommand(self):
        calls = []
        code, _err = self._main(
            ["-D", "mw-cite", "true"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            calls[-1],
            [create_mod.WMF_SBX, "--upstream", "-D", "exec", "mw-cite", "true"],
        )


if __name__ == "__main__":
    unittest.main()
