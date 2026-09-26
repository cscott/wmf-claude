#!/usr/bin/env python3
"""Unit tests for wmf_sbx_resume.py -- run with:
  python3 -m unittest discover -s sbx/tests -v

The ordering is the whole point: a stopped sandbox publishes no port, so
the start has to happen before the port lookup, and both have to happen
before control is handed to the interactive attach.
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
import wmf_sbx.resolve as resolve_mod  # noqa: E402
import wmf_sbx.resume as resume  # noqa: E402
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


class SplitAgentArgsTests(unittest.TestCase):
    def test_no_separator_means_no_agent_args(self):
        # None, not []: the config's resumeArgs only apply when the
        # engineer didn't say anything about the tail themselves.
        self.assertEqual(resume.split_agent_args(["mw-cite"]), (["mw-cite"], None))

    def test_everything_after_the_separator_belongs_to_the_agent(self):
        ours, agent = resume.split_agent_args(
            ["mw-cite", "--", "--continue", "--dry-run"]
        )
        self.assertEqual(ours, ["mw-cite"])
        # --dry-run is ours in the first half and the agent's in the
        # second; argparse can't make that distinction, which is why the
        # split happens first.
        self.assertEqual(agent, ["--continue", "--dry-run"])

    def test_an_empty_tail_is_still_a_tail(self):
        self.assertEqual(resume.split_agent_args(["mw-cite", "--"]), (["mw-cite"], []))


class BuildRunCommandTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(
            resume.build_run_command("mw-cite", []),
            [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"],
        )

    def test_with_agent_args(self):
        self.assertEqual(
            resume.build_run_command("mw-cite", ["--continue"]),
            [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite", "--", "--continue"],
        )


class StartsAConversationTests(unittest.TestCase):
    """What may set the `attached` flag. The list comes from `claude
    --help` (2.1.246) -- see the module's AGENT_SUBCOMMANDS."""

    def test_an_ordinary_attach_does(self):
        self.assertTrue(resume.starts_a_conversation([]))
        self.assertTrue(resume.starts_a_conversation(["--continue"]))

    def test_a_prompt_does(self):
        self.assertTrue(resume.starts_a_conversation(["-p", "summarise this"]))

    def test_version_and_help_do_not(self):
        for flag in ("--version", "-v", "--help", "-h"):
            self.assertFalse(resume.starts_a_conversation([flag]), flag)

    def test_a_subcommand_does_not(self):
        self.assertFalse(resume.starts_a_conversation(["mcp"]))
        self.assertFalse(resume.starts_a_conversation(["doctor"]))

    def test_a_subcommand_name_used_as_a_prompt_still_does(self):
        # `claude --agent rm` is a flag's value, not the `rm` subcommand.
        self.assertTrue(resume.starts_a_conversation(["--agent", "rm"]))


class DefaultAgentArgsTests(unittest.TestCase):
    def test_nothing_to_continue_yet(self):
        self.assertEqual(resume.default_agent_args({}, attached=False), [])

    def test_continue_once_there_is_something_to_continue(self):
        self.assertEqual(resume.default_agent_args({}, attached=True), ["--continue"])

    def test_config_args_keep_their_order_after_the_flag(self):
        self.assertEqual(
            resume.default_agent_args({"resumeArgs": ["--verbose"]}, attached=True),
            ["--continue", "--verbose"],
        )

    def test_a_string_is_split_on_whitespace(self):
        self.assertEqual(
            resume.default_agent_args({"resumeArgs": "--verbose --debug"}, attached=True),
            ["--continue", "--verbose", "--debug"],
        )

    def test_an_existing_session_flag_wins(self):
        # --resume picks a session interactively; adding --continue on top
        # would be two answers to the same question.
        for flag in ("--continue", "-c", "--resume", "-r"):
            with self.subTest(flag=flag):
                self.assertEqual(
                    resume.default_agent_args({"resumeArgs": [flag]}, attached=True),
                    [flag],
                )

    def test_a_config_continue_is_stripped_before_the_first_attach(self):
        self.assertEqual(
            resume.default_agent_args(
                {"resumeArgs": ["-c", "--verbose"]}, attached=False
            ),
            ["--verbose"],
        )


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        self.config = os.path.join(self.tmp.name, "repos.yaml")
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
            code = resume.main(argv, run=run, env=self.env)
        return code, err.getvalue()

    def write_config(self, body):
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(body)

    def set_attached(self, value=True, name="mw-cite"):
        state = state_mod.load(name, env=self.env)
        state["attached"] = value
        state_mod.save(state, env=self.env)

    def attached(self, name="mw-cite"):
        return bool((state_mod.load(name, env=self.env) or {}).get("attached"))

    def test_starts_then_waits_then_repoints_then_attaches(self):
        calls = []
        code, _err = self._main(
            ["--config", self.config, "mw-cite"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "true"],
            # The startup commands first: a container that came up to serve
            # an `sbx exec` can have skipped them (§97).
            startup_probe_call(),
            # The mounts before anything that reads a clone: until they're
            # up, every borrowed object is unreachable (§40/§41). The
            # startup dispatcher does the restoring (§46); this waits for
            # it rather than doing a second one alongside it.
            [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "python3",
             "/home/agent/wmf-sbx-setup", "--verify", "--wait=20"],
        ] + claude_md_calls() + [
            daemon_probe_call(),
            [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"],
        ])
        # And the re-point happened between them, not after the attach
        # returned -- by then the fetch the engineer wanted is long over.
        self.assertEqual(self.refreshed, ["mw-cite"])

    def test_a_wait_that_runs_out_falls_back_to_restoring(self):
        # The dispatcher is measured to work, but "measured to work" is
        # not "cannot fail", and this is the belt that made §41 survivable.
        def run(cmd, **kwargs):
            calls.append(cmd)
            if "--verify" in cmd:
                return FakeCompletedProcess(1)
            return FakeCompletedProcess(0)

        calls = []
        code, err = self._main(["--config", self.config, "mw-cite"], run)
        self.assertEqual(code, 0)
        self.assertIn("re-applying mw-cite's mount layout", err)
        self.assertEqual(calls[3], [
            create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "sudo", "python3",
            "/home/agent/wmf-sbx-setup", "--restore",
        ])
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"])

    def test_a_wait_that_pays_off_does_not_restore_as_well(self):
        # Two concurrent `--restore` passes would be doing `mount --move`
        # at the same paths and writing the same log (§46).
        calls = []
        self._main(["--config", self.config, "mw-cite"], self.fake_run(calls))
        self.assertFalse(any("--restore" in cmd for cmd in calls))

    def test_a_sandbox_that_will_not_start_is_not_attached_to(self):
        calls = []
        code, err = self._main(
            ["--config", self.config, "mw-cite"], self.fake_run(calls, exec_rc=1)
        )
        self.assertEqual(code, 1)
        self.assertIn("could not start", err)
        self.assertFalse(any("run" in cmd for cmd in calls))
        self.assertEqual(self.refreshed, [])

    def test_agent_args_are_passed_through(self):
        calls = []
        self._main(
            ["--config", self.config, "mw-cite", "--", "--continue"],
            self.fake_run(calls),
        )
        self.assertEqual(calls[-1][-2:], ["--", "--continue"])

    @unittest.skipUnless(resolve_mod.yaml is not None, "PyYAML not installed")
    def test_resume_args_come_from_the_config(self):
        # §20: the agent CLI tail doesn't survive a stop, and retyping it
        # on every re-attach is the thing this launcher exists to stop.
        self.set_attached()
        self.write_config("resumeArgs:\n  - --verbose\n")
        calls = []
        self._main(["--config", self.config, "mw-cite"], self.fake_run(calls))
        self.assertEqual(calls[-1][-3:], ["--", "--continue", "--verbose"])

    @unittest.skipUnless(resolve_mod.yaml is not None, "PyYAML not installed")
    def test_a_string_resume_args_is_split_not_exploded(self):
        self.set_attached()
        self.write_config("resumeArgs: --continue --verbose\n")
        calls = []
        self._main(["--config", self.config, "mw-cite"], self.fake_run(calls))
        # --continue is already there; it isn't added a second time.
        self.assertEqual(calls[-1][-3:], ["--", "--continue", "--verbose"])

    @unittest.skipUnless(resolve_mod.yaml is not None, "PyYAML not installed")
    def test_an_explicit_empty_tail_overrides_the_config(self):
        self.write_config("resumeArgs:\n  - --continue\n")
        calls = []
        self._main(["--config", self.config, "mw-cite", "--"], self.fake_run(calls))
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"])

    def test_no_remotes_skips_the_repoint(self):
        calls = []
        self._main(
            ["--config", self.config, "--no-remotes", "mw-cite"], self.fake_run(calls)
        )
        self.assertEqual(self.refreshed, [])
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"])

    def test_the_mount_pass_says_it_is_happening(self):
        calls = []
        _code, err = self._main(
            ["--config", self.config, "mw-cite"], self.fake_run(calls)
        )
        self.assertIn("waiting for mw-cite's mount layout", err)

    def test_no_restore_skips_the_mount_pass(self):
        calls = []
        self._main(
            ["--config", self.config, "--no-restore", "mw-cite"], self.fake_run(calls)
        )
        self.assertFalse(any("--restore" in cmd or "--verify" in cmd for cmd in calls))
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"])

    def test_a_restore_that_reports_problems_still_attaches(self):
        # The mounts are a convenience layer; a sandbox you can't fetch
        # from cleanly is still one you can work in, so this never blocks.
        def run(cmd, **kwargs):
            calls.append(cmd)
            if "--verify" in cmd:
                return FakeCompletedProcess(1)
            if "cat" in cmd:
                return FakeCompletedProcess(0, stdout=(
                    '{"version": 1, "exit": 1, "problems": '
                    '["/home/cananian/Cite is not the clone at /home/agent/Cite"]}'
                ))
            return FakeCompletedProcess(0)

        calls = []
        code, err = self._main(["--config", self.config, "mw-cite"], run)
        self.assertEqual(code, 0)
        self.assertIn("is not the clone at", err)
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"])

    def test_dry_run_starts_nothing(self):
        calls = []
        code, err = self._main(
            ["--config", self.config, "--dry-run", "mw-cite"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.refreshed, [])
        self.assertIn("--upstream run --name mw-cite", err)

    def test_a_sandbox_with_no_recorded_state_still_attaches(self):
        # Created by plain `sbx create`, or its state was pruned. There is
        # nothing to re-point, and that is not an error.
        calls = []
        code, _err = self._main(
            ["--config", self.config, "mw-other"], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.refreshed, [])
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-other"])

    def test_the_first_attach_passes_no_continue(self):
        # A brand-new sandbox has no conversation to continue; claude
        # exits 1 on --continue rather than starting a fresh one.
        calls = []
        self._main(["--config", self.config, "mw-cite"], self.fake_run(calls))
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"])

    def test_a_clean_attach_records_that_it_happened(self):
        self.assertFalse(self.attached())
        self._main(["--config", self.config, "mw-cite"], self.fake_run([]))
        self.assertTrue(self.attached())

    def test_the_second_attach_defaults_to_continue(self):
        self._main(["--config", self.config, "mw-cite"], self.fake_run([]))
        calls = []
        self._main(["--config", self.config, "mw-cite"], self.fake_run(calls))
        self.assertEqual(calls[-1][-2:], ["--", "--continue"])

    def test_a_version_check_is_not_an_attach(self):
        # `-- --version` exits 0 without starting anything. Recording that
        # as an attach is what made the *next* resume pass --continue to a
        # claude with nothing to continue (§43).
        self._main(
            ["--config", self.config, "mw-cite", "--", "--version"],
            self.fake_run([]),
        )
        self.assertFalse(self.attached())

    def test_a_continue_with_nothing_to_continue_is_retried_without_it(self):
        self.set_attached()

        def run(cmd, **kwargs):
            calls.append(cmd)
            if "run" in cmd and "--continue" in cmd:
                return FakeCompletedProcess(1)
            return FakeCompletedProcess(0)

        calls = []
        code, err = self._main(["--config", self.config, "mw-cite"], run)
        self.assertEqual(code, 0)
        runs = [c for c in calls if "run" in c]
        self.assertEqual(len(runs), 2)
        self.assertNotIn("--continue", runs[1])
        self.assertIn("starting a fresh session", err)
        # And the fresh session is a conversation, so the flag comes back.
        self.assertTrue(self.attached())

    def test_a_continue_the_caller_typed_is_theirs_to_fix(self):
        self.set_attached()

        def run(cmd, **kwargs):
            calls.append(cmd)
            return FakeCompletedProcess(0 if "exec" in cmd else 1)

        calls = []
        code, _err = self._main(
            ["--config", self.config, "mw-cite", "--", "--continue"], run
        )
        self.assertEqual(code, 1)
        self.assertEqual(len([c for c in calls if "run" in c]), 1)

    def test_a_failed_attach_is_not_recorded(self):
        # Nothing ran, so nothing was started to continue -- and a sticky
        # flag would make every later resume ask for a session that was
        # never created.
        def run(cmd, **kwargs):
            return FakeCompletedProcess(0 if "exec" in cmd else 1)
        code, _err = self._main(["--config", self.config, "mw-cite"], run)
        self.assertEqual(code, 1)
        self.assertFalse(self.attached())

    def test_no_continue_opts_out_for_one_invocation(self):
        self.set_attached()
        calls = []
        self._main(
            ["--config", self.config, "--no-continue", "mw-cite"], self.fake_run(calls)
        )
        self.assertEqual(calls[-1], [create_mod.WMF_SBX, "--upstream", "run", "--name", "mw-cite"])
        # And it stays opt-out for this run only.
        calls = []
        self._main(["--config", self.config, "mw-cite"], self.fake_run(calls))
        self.assertEqual(calls[-1][-2:], ["--", "--continue"])

    @unittest.skipUnless(resolve_mod.yaml is not None, "PyYAML not installed")
    def test_a_config_continue_is_dropped_on_the_first_attach(self):
        # `resumeArgs: --continue` is the natural thing to write in
        # repos.yaml; it must not turn the first launch into an exit 1.
        self.write_config("resumeArgs:\n  - --continue\n  - --verbose\n")
        calls = []
        self._main(["--config", self.config, "mw-cite"], self.fake_run(calls))
        self.assertEqual(calls[-1][-2:], ["--", "--verbose"])

    def test_an_explicit_tail_is_passed_through_verbatim(self):
        # The engineer typed it; --continue on a fresh sandbox is their
        # call to make.
        calls = []
        self._main(
            ["--config", self.config, "mw-cite", "--", "--continue"],
            self.fake_run(calls),
        )
        self.assertEqual(calls[-1][-2:], ["--", "--continue"])

    def test_an_invalid_name_is_refused_before_anything_runs(self):
        calls = []
        code, err = self._main(
            ["--config", self.config, "../escape"], self.fake_run(calls)
        )
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("error:", err)

    def test_a_path_shortcut_resolves_to_its_sandbox(self):
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        state = state_mod.load("mw-cite", env=self.env)
        state["primaryDir"] = repo.name
        state_mod.save(state, env=self.env)

        calls = []
        code, err = self._main(
            ["--config", self.config, repo.name], self.fake_run(calls)
        )
        self.assertEqual(code, 0)
        self.assertIn("resolved", err)
        self.assertIn("mw-cite", err)
        self.assertEqual(calls[0], [create_mod.WMF_SBX, "--upstream", "exec", "mw-cite", "--", "true"])

    def test_an_ambiguous_path_shortcut_is_refused_before_anything_runs(self):
        state_mod.save(
            state_mod.new_state("mw-ve", daemon_port=9977, host_port=32783,
                                 primary_dir=self.tmp.name),
            env=self.env,
        )
        state = state_mod.load("mw-cite", env=self.env)
        state["primaryDir"] = self.tmp.name
        state_mod.save(state, env=self.env)

        calls = []
        code, err = self._main(
            ["--config", self.config, self.tmp.name], self.fake_run(calls)
        )
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()
