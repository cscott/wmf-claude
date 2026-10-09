#!/usr/bin/env python3
"""Unit tests for phase 5: mediawiki.py and setup.py --lima -- run with:
  python3 -m unittest discover -s sbx/tests -v

The MediaWiki steps themselves (composer, npm, the install) are
setup.py's, tested in test_wmf_sbx_setup.py. These tests cover what is
new on Lima: the setup runs as the agent (no sudo), takes its roles from
the plan, skips the resets, and is sent into the VM by create. The real
run was in a VM (lima-port/HANDOFF-LIMA.md §11, phase 5)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.create as create  # noqa: E402
import wmf_sbx.mediawiki as mediawiki  # noqa: E402
import wmf_sbx.setup as setup  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import wmf_sbx.vm as vm  # noqa: E402
from test_wmf_sbx_lifecycle import Done, Env, FakeLima, allow_tmp, git_repo  # noqa: E402

CORE = "/home/me/src/core"
CITE = "/home/me/src/Cite"
VECTOR = "/home/me/src/Vector"
PARSOID = "/home/me/src/parsoid"


def plan(**kw):
    p = {"version": setup.PLAN_VERSION, "primary": CITE, "resetAll": False, "repos": [
        {"path": CORE, "canonical": setup.CORE_CANONICAL, "readOnly": False,
         "linkName": None, "linkDir": None},
        {"path": CITE, "canonical": "gerrit:mediawiki/extensions/Cite", "readOnly": False,
         "linkName": "Cite", "linkDir": "extensions"},
        {"path": VECTOR, "canonical": "gerrit:mediawiki/skins/Vector", "readOnly": True,
         "linkName": "Vector", "linkDir": "skins"},
        {"path": PARSOID, "canonical": setup.PARSOID_CANONICAL, "readOnly": False,
         "linkName": None, "linkDir": None},
    ]}
    p.update(kw)
    return p


class AsAgentTests(unittest.TestCase):

    def test_as_the_agent_commands_run_directly_and_nothing_is_chowned(self):
        calls = []
        run = lambda argv, **kw: calls.append((argv, kw)) or Done()  # noqa: E731
        with mock.patch.object(setup, "running_as_agent", return_value=True):
            setup.as_agent(["composer", "update"], cwd="/x", run=run)
            setup.give_to_agent("/x/.env", run=run)
        self.assertEqual(calls, [(["composer", "update"], {"cwd": "/x"})])

    def test_as_root_it_goes_through_sudo_as_before(self):
        calls = []
        run = lambda argv, **kw: calls.append(argv) or Done()  # noqa: E731
        with mock.patch.object(setup, "running_as_agent", return_value=False):
            setup.as_agent(["composer", "update"], run=run)
            setup.give_to_agent("/x/.env", run=run)
        self.assertEqual(calls, [["sudo", "-u", "agent", "-H", "composer", "update"],
                                 ["sudo", "chown", "agent:agent", "/x/.env"]])


class LimaPlanTests(unittest.TestCase):

    def test_roles_come_from_the_plan_and_ro_repos_are_not_written(self):
        core, core_ro, links, clones, parsoid = setup.lima_repo_roles(plan())
        self.assertEqual((core, core_ro, parsoid), (CORE, False, PARSOID))
        self.assertEqual(links, [("extensions", "Cite", CITE), ("skins", "Vector", VECTOR)])
        self.assertEqual(clones, [CORE, CITE, PARSOID])

    def test_run_lima_setup_runs_the_chain_with_the_roles(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(plan(), f)
        self.addCleanup(os.unlink, f.name)
        with mock.patch.object(setup, "mediawiki_setup", return_value=0) as mw:
            self.assertEqual(setup.run_lima_setup([f.name]), 0)
        args, kw = mw.call_args
        self.assertEqual(args[1], CORE)
        self.assertEqual(kw["parsoid_path"], PARSOID)

    def test_a_bad_plan_fails(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"version": 99, "repos": []}, f)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(setup.run_lima_setup([f.name]), 1)
        self.assertEqual(setup.run_lima_setup([]), 1)

    def test_main_lima_logs_in_the_agents_home(self):
        self.assertEqual(setup.LOG_DIR, "/home/agent/.wmf-sbx")
        with mock.patch.object(setup, "run_lima_setup", return_value=0) as r:
            self.assertEqual(setup.main(["--lima", "/p.json"], log_dir=None), 0)
        self.assertEqual(r.call_args[0][0], ["/p.json"])


class RunSetupTests(unittest.TestCase):

    def test_the_plan_is_written_then_setup_runs_as_the_agent_with_the_proxy(self):
        lima = FakeLima()
        inputs = []
        orig = lima.call

        def call(*args, **kw):
            inputs.append(kw.get("input"))
            return orig(*args, **kw)
        lima.call = call
        mediawiki.run_setup("demo", plan(), lima=lima,
                            env={"https_proxy": "http://127.0.0.1:3128"})
        (write, run_) = lima.shells()
        self.assertIn("sudo", write)
        self.assertIn(f"umask 077 && cat > {setup.SANDBOX_PLAN_FILE}", write)
        self.assertEqual(json.loads(inputs[0])["primary"], CITE)
        self.assertEqual(run_[-4:], ("python3", "-", "--lima", setup.SANDBOX_PLAN_FILE))
        self.assertIn("https_proxy=http://192.168.5.2:3128", run_)
        self.assertIn("def run_lima_setup", inputs[1])

    def test_a_failure_raises(self):
        with self.assertRaisesRegex(mediawiki.SetupError, "could not write the plan"):
            mediawiki.run_setup("demo", plan(), lima=FakeLima(rc=1))


class CreateFlowTests(Env):

    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(self.tmp)
        allow_tmp(self)
        self.repo = os.path.join(self.tmp, "src", "demo")
        git_repo(self.repo)

    def argv(self):
        return ["--no-deps", "--config", os.path.join(self.tmp, "repos.yaml"),
                "--image", self.KEY, "--name", "demo", self.repo]

    def test_create_runs_the_setup_last(self):
        lima = FakeLima()
        self.assertEqual(self.quiet(create.main, self.argv(), lima=lima, env=self.env), 0,
                         self.err)
        self.assertEqual(lima.shells()[-1][-3:], ("-", "--lima", setup.SANDBOX_PLAN_FILE))

    def test_a_failed_setup_fails_the_create_and_keeps_the_sandbox(self):
        class Broken(FakeLima):
            def call(self, *args, **kw):
                if args[0] == "shell" and "--lima" in args:
                    self.calls.append(("call",) + args)
                    return Done(returncode=1)
                return super().call(*args, **kw)
        lima = Broken()
        self.assertEqual(self.quiet(create.main, self.argv(), lima=lima, env=self.env), 1)
        self.assertIn("MediaWiki setup failed", self.err)
        self.assertIsNotNone(state_mod.load("demo", self.env))
        self.assertIn(vm.instance_name("demo"), lima.vms)
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "remote"], text=True,
                                        capture_output=True).stdout.strip(), "demo")


if __name__ == "__main__":
    unittest.main()
