#!/usr/bin/env python3
"""Unit tests for wmf_sbx/start.py -- run with:
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
import wmf_sbx.start as start  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402


def claude_md_calls(name="mw-cite"):
    """The execs of create_mod.amend_workspace_claude_md (§95)."""
    edits = "/home/agent/.claude/wmf-sbx-claude-md.json"
    return [
        [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "test", "-f", edits],
        [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "sudo", "python3",
         "/home/agent/wmf-sbx-setup", "--claude-md", edits],
        [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "cat",
         "/var/log/wmf-sbx-claude-md.status"],
    ]


def startup_probe_call(name="mw-cite"):
    """The exec of create_mod.ensure_startup_ran's probe (§97)."""
    return [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "python3",
            "-c", create_mod.STARTUP_RAN_PROBE]


def daemon_probe_call(name="mw-cite"):
    """The exec of create_mod.ensure_git_daemon's probe (§97)."""
    return [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "python3",
            "-c", 'import socket, sys; sys.exit(0 if '
            'socket.socket().connect_ex(("127.0.0.1", 9977)) == 0 else 1)']


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

    def fake_run(self, calls, exec_rc=0):
        def run(cmd, **kwargs):
            calls.append(cmd)
            if "exec" in cmd:
                return FakeCompletedProcess(exec_rc)
            return FakeCompletedProcess(0)
        return run

    def _main(self, argv, run):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = start.main(argv, run=run, env=self.env)
        return code, err.getvalue()

    def test_starts_waits_and_repoints_but_never_attaches(self):
        calls = []
        code, _err = self._main(["mw-cite"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "true"],
            startup_probe_call(),
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "python3",
             "/home/agent/wmf-sbx-setup", "--verify", "--wait=20"],
        ] + claude_md_calls() + [daemon_probe_call()])
        self.assertFalse(any("run" in c for c in calls))
        self.assertEqual(self.refreshed, ["mw-cite"])

    def test_a_wait_that_runs_out_falls_back_to_restoring(self):
        def run(cmd, **kwargs):
            calls.append(cmd)
            if "--verify" in cmd:
                return FakeCompletedProcess(1)
            return FakeCompletedProcess(0)

        calls = []
        code, err = self._main(["mw-cite"], run)
        self.assertEqual(code, 0)
        self.assertIn("re-applying mw-cite's mount layout", err)
        self.assertEqual(calls[3], [
            create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "sudo", "python3",
            "/home/agent/wmf-sbx-setup", "--restore",
        ])

    def test_a_sandbox_that_will_not_start_is_an_error(self):
        calls = []
        code, err = self._main(["mw-cite"], self.fake_run(calls, exec_rc=1))
        self.assertEqual(code, 1)
        self.assertIn("could not start", err)
        self.assertEqual(self.refreshed, [])

    def test_no_remotes_skips_the_repoint(self):
        calls = []
        code, _err = self._main(["--no-remotes", "mw-cite"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertEqual(self.refreshed, [])

    def test_no_restore_skips_the_mount_pass(self):
        calls = []
        code, _err = self._main(["--no-restore", "mw-cite"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertFalse(any("--restore" in c or "--verify" in c for c in calls))
        self.assertEqual(
            calls, [[create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "true"],
                    startup_probe_call()]
            + claude_md_calls() + [daemon_probe_call()]
        )

    def test_an_older_sandbox_without_the_edits_is_left_alone(self):
        def run(cmd, **kwargs):
            calls.append(cmd)
            return FakeCompletedProcess(1 if "test" in cmd else 0)

        calls = []
        code, err = self._main(["--no-restore", "mw-cite"], run)
        self.assertEqual(code, 0)
        self.assertEqual(
            calls[1:], [startup_probe_call()] + claude_md_calls()[:1]
            + [daemon_probe_call()])
        self.assertNotIn("CLAUDE.md", err)

    def test_dry_run_starts_nothing(self):
        calls = []
        code, err = self._main(["--dry-run", "mw-cite"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.refreshed, [])
        self.assertIn("exec mw-cite -- true", err)
        self.assertIn("re-point the mw-cite remotes", err)
        self.assertIn("would edit mw-cite's workspace CLAUDE.md", err)

    def test_a_sandbox_with_no_recorded_state_still_starts(self):
        calls = []
        code, _err = self._main(["mw-other"], self.fake_run(calls))
        self.assertEqual(code, 0)
        self.assertEqual(self.refreshed, [])

    def test_an_invalid_name_is_refused_before_anything_runs(self):
        calls = []
        code, err = self._main(["../escape"], self.fake_run(calls))
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("error:", err)

    def test_never_builds_a_run_command(self):
        # The whole point: wmf-sbx-start must not attach an agent, unlike
        # wmf-sbx-resume, which this module's restore logic is shared with.
        calls = []
        self._main(["mw-cite"], self.fake_run(calls))
        self.assertFalse(any(c[:2] == [create_mod.WMF_SBX, "run"] for c in calls))


if __name__ == "__main__":
    unittest.main()
