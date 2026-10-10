#!/usr/bin/env python3
"""Unit tests for phase 6, the Claude session: session.py, resume.py,
setup.write_session, the proxy-port refresh in vm.py, the create wiring,
and the lima-sbx SessionStart text -- run with:
  python3 -m unittest discover -s sbx/tests -v

The real session ran in a VM (lima-port/HANDOFF-LIMA.md §11, phase 6)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.create as create  # noqa: E402
import wmf_sbx.resume as resume  # noqa: E402
import wmf_sbx.session as session  # noqa: E402
import wmf_sbx.setup as setup  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import wmf_sbx.vm as vm  # noqa: E402
from test_wmf_sbx_lifecycle import Env, FakeLima, allow_tmp, git_repo  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
STATE = {"name": "demo", "primaryDir": "/home/me/src/Cite",
         "repos": ["/home/me/src/Cite", "/home/me/src/core", "/home/me/src/Vector"],
         "readOnly": ["/home/me/src/Vector"]}
SECRET = "sk-test-not-a-real-key"


class CredentialTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_CONFIG_HOME": self.tmp.name}

    def write_token(self, mode):
        path = session.token_file(self.env)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write(" tok \n")
        os.chmod(path, mode)

    def test_the_variables_come_first_in_claudes_order(self):
        env = dict(self.env, ANTHROPIC_API_KEY="k", CLAUDE_CODE_OAUTH_TOKEN="t")
        self.assertEqual(session.host_credential(env), {"ANTHROPIC_API_KEY": "k"})
        env.pop("ANTHROPIC_API_KEY")
        self.assertEqual(session.host_credential(env), {"CLAUDE_CODE_OAUTH_TOKEN": "t"})

    def test_the_token_file(self):
        self.assertEqual(session.host_credential(self.env), {})
        self.write_token(0o600)
        self.assertEqual(session.host_credential(self.env), {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})

    def test_a_token_file_others_can_read_is_refused(self):
        self.write_token(0o644)
        with self.assertRaisesRegex(session.SessionError, "chmod 600"):
            session.host_credential(self.env)

    def test_the_file_contents_are_safe_to_source(self):
        text = session.credential_file_contents({"ANTHROPIC_API_KEY": "a'b $(x)"})
        out = subprocess.run(["bash", "-c", 'set -a; . /dev/stdin; printf %s "$ANTHROPIC_API_KEY"'],
                             input=text, capture_output=True, text=True).stdout
        self.assertEqual(out, "a'b $(x)")

    def test_the_upstream_proxy_is_the_hosts_as_the_guest_sees_it(self):
        self.assertEqual(session.upstream_proxy({"https_proxy": "http://127.0.0.1:3128/"}),
                         "192.168.5.2:3128")
        self.assertEqual(session.upstream_proxy({"HTTPS_PROXY": "http://u:p@proxy.example:8080"}),
                         "proxy.example:8080")
        self.assertIsNone(session.upstream_proxy({}))


class LauncherTests(unittest.TestCase):

    def argv(self, **kw):
        return session.launcher_argv(STATE, **kw)

    def test_the_agent_runs_bin_claude_in_the_primary_clone(self):
        a = self.argv()
        self.assertEqual(a[:4], ["sudo", "-H", "-u", "agent"])
        self.assertIn("-C", a)
        self.assertEqual(a[a.index("-C") + 1], "/home/me/src/Cite")
        self.assertIn("WMF_CLAUDE_SANDBOX_BACKEND=lima-sbx", a)
        self.assertIn(session.LAUNCHER, a)

    def test_grants_for_the_other_clones_and_the_mounts(self):
        a = self.argv()
        i = a.index(session.LAUNCHER)
        launcher = a[i:a.index("--", i)]
        self.assertIn("--local-web=4000", launcher)
        self.assertIn("--landlock-only", launcher)
        pairs = list(zip(launcher, launcher[1:]))
        self.assertIn(("--allow", "/home/me/src/core"), pairs)
        self.assertIn(("--read", "/home/me/src/Vector"), pairs)
        self.assertNotIn(("--allow", "/home/me/src/Cite"), pairs)
        for path in ("/run/wmf-sbx/host", "/opt/claude-code", "/opt/node", "/etc/php"):
            self.assertIn(("--read", path), pairs)
        self.assertIn(("--read-file", "/etc/gitconfig"), pairs)
        self.assertIn(("--allow", "/home/agent/.npm"), pairs)
        self.assertNotIn(("--read-file", "/home/agent/.bashrc"), pairs)
        # A deny under an allowed path stops nono (Landlock).
        self.assertNotIn(("--allow", "/home/agent/.config/composer"), pairs)
        for domain in ("registry.npmjs.org", "repo.packagist.org"):
            self.assertIn(("--allow-domain", domain), pairs)

    def test_proxy_flags_and_claude_args_are_in_their_places(self):
        a = self.argv(proxy="192.168.5.2:3128", launcher_flags=["--allow-post=x.org"],
                      claude_args=["-p", "hi"], cred_path="/dev/shm/wmf-sbx-cred.x")
        sep = a.index("--", a.index(session.LAUNCHER))
        self.assertEqual(a[sep - 3:sep], ["--upstream-proxy", "192.168.5.2:3128",
                                          "--allow-post=x.org"])
        self.assertEqual(a[sep + 1:], ["-p", "hi"])
        self.assertIn("/dev/shm/wmf-sbx-cred.x", a)

    def test_the_wrapper_reads_and_deletes_the_credential_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "cred")
            with open(f, "w") as fh:
                fh.write(session.credential_file_contents({"CLAUDE_CODE_OAUTH_TOKEN": "t0k"}))
            out = subprocess.run(["bash", "-c", session.CREDENTIAL_WRAPPER, "_", f,
                                  "sh", "-c", 'printf %s "$CLAUDE_CODE_OAUTH_TOKEN"'],
                                 capture_output=True, text=True).stdout
            self.assertEqual(out, "t0k")
            self.assertFalse(os.path.exists(f))
        out = subprocess.run(["bash", "-c", session.CREDENTIAL_WRAPPER, "_", "", "echo", "ran"],
                             capture_output=True, text=True).stdout
        self.assertEqual(out, "ran\n")


class SessionFilesTests(unittest.TestCase):

    def test_home_claude_md_names_the_sandbox_and_keeps_running_tests(self):
        text = session.home_claude_md("sbx-cite")
        self.assertIn("fetch sbx-cite", text)
        self.assertIn("## Running tests", text)
        for docker in (".sbx-originals", "git://", "The other CLAUDE.md"):
            self.assertNotIn(docker, text)

    def test_write_session_merges_env_and_writes_files(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".claude"))
            with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
                json.dump({"env": {"KEEP": "1"}, "model": "x"}, f)
            run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0)  # noqa: E731
            ok = setup.write_session({"env": {"MW_SERVER": "http://localhost:4000"},
                                      "files": [{"path": "~/.claude/CLAUDE.md", "content": "hi"},
                                                {"path": "/etc/passwd", "content": "no"},
                                                {"path": "~/../x", "content": "no"}]},
                                     home=home, run=run)
            self.assertFalse(ok)  # the two bad paths
            with open(os.path.join(home, ".claude", "settings.json")) as f:
                settings = json.load(f)
            self.assertEqual(settings["env"], {"KEEP": "1", "MW_SERVER": "http://localhost:4000"})
            self.assertEqual(settings["model"], "x")
            with open(os.path.join(home, ".claude", "CLAUDE.md")) as f:
                self.assertEqual(f.read(), "hi")


class ContextTextTests(unittest.TestCase):

    def test_the_lima_sbx_sandbox_text_starts_with_nonos(self):
        ctx = os.path.join(REPO_ROOT, "hooks", "context")
        with open(os.path.join(ctx, "nono", "sandbox.txt"), encoding="utf-8") as f:
            nono = f.read()
        with open(os.path.join(ctx, "lima-sbx", "sandbox.txt"), encoding="utf-8") as f:
            lima = f.read()
        self.assertTrue(lima.startswith(nono))
        self.assertIn("wmf-sbx resume", lima[len(nono):])
        self.assertTrue(os.path.isfile(os.path.join(ctx, "lima-sbx", "environment.txt")))

    def test_the_hook_selects_the_backend(self):
        hook = os.path.join(REPO_ROOT, "bin", "session-start.sh")
        out = subprocess.run(["bash", hook], capture_output=True, text=True,
                             env=dict(os.environ, WMF_CLAUDE_SANDBOX_BACKEND="lima-sbx")).stdout
        self.assertIn("disposable Lima VM", out)
        self.assertIn("composer serve", out)
        self.assertIn("$TMPDIR", out)


class ProxyRefreshTests(unittest.TestCase):

    def lima(self, ports):
        lima = FakeLima({"wmf-sbx-demo": "Stopped"})
        lima.configs["wmf-sbx-demo"] = f'script: "PROXY_PORTS=\\"{ports}\\"\\n"'
        return lima

    def test_a_stopped_vm_gets_the_new_port_before_it_starts(self):
        lima = self.lima("1111")
        vm.ensure_running("demo", lima=lima, env={"https_proxy": "http://127.0.0.1:2222"},
                          log=lambda m: None)
        kinds = [c[0] for c in lima.calls]
        self.assertEqual(kinds, ["edit", "start"])
        self.assertIn('PROXY_PORTS=\\"2222\\"', lima.calls[0][2])

    def test_the_same_port_is_left_alone(self):
        lima = self.lima("2222")
        vm.ensure_running("demo", lima=lima, env={"https_proxy": "http://127.0.0.1:2222"},
                          log=lambda m: None)
        self.assertEqual([c[0] for c in lima.calls], ["start"])

    def test_a_running_vm_gets_a_warning(self):
        lima = self.lima("1111")
        lima.vms["wmf-sbx-demo"] = "Running"
        said = []
        vm.ensure_running("demo", lima=lima, env={"https_proxy": "http://127.0.0.1:2222"},
                          log=said.append)
        self.assertEqual(lima.calls, [])
        self.assertIn("stop demo", said[0])


class ResumeArgsTests(unittest.TestCase):

    def test_split(self):
        self.assertEqual(resume.split_args(["x", "--local-db", "--", "-p", "hi"]),
                         (["x", "--local-db"], ["-p", "hi"]))
        self.assertEqual(resume.split_args(["x"]), (["x"], None))

    def test_conversations(self):
        self.assertTrue(resume.starts_a_conversation([]))
        self.assertTrue(resume.starts_a_conversation(["-p", "mcp list"]))
        self.assertFalse(resume.starts_a_conversation(["mcp", "list"]))
        self.assertFalse(resume.starts_a_conversation(["--version"]))
        self.assertEqual(resume.default_claude_args(False), [])
        self.assertEqual(resume.default_claude_args(True), ["--continue"])


class ResumeMainTests(Env):

    def setUp(self):
        super().setUp()
        st = state_mod.new_state("demo", primary_dir="/home/me/src/Cite",
                                 repos=["/home/me/src/Cite"])
        state_mod.save(st, self.env)
        self.lima = FakeLima({"wmf-sbx-demo": "Stopped"})
        self.inputs = []
        orig = self.lima.call

        def call(*args, **kw):
            self.inputs.append(kw.get("input"))
            return orig(*args, **kw)
        self.lima.call = call

    def resume(self, *argv, env=None):
        return self.quiet(resume.main, ["demo"] + list(argv), lima=self.lima,
                          env=dict(self.env, **(env or {})))

    def launches(self):
        return [s for s in self.lima.shells() if session.LAUNCHER in s]

    def test_resume_starts_the_vm_checks_it_and_launches(self):
        self.assertEqual(self.resume(), 0, self.err)
        self.assertIn(("start", "wmf-sbx-demo"), self.lima.calls)
        (launch,) = self.launches()
        self.assertEqual(launch[launch.index("--", launch.index(session.LAUNCHER)) + 1:], ())
        self.assertIn("log in", self.err)
        self.assertTrue(state_mod.load("demo", self.env)["attached"])

    def test_the_second_resume_continues(self):
        self.resume()
        self.resume()
        last = self.launches()[-1]
        self.assertEqual(last[-1], "--continue")

    def test_the_credential_goes_on_stdin_and_never_on_a_command_line(self):
        self.assertEqual(self.resume("--", "-p", "hi", env={"ANTHROPIC_API_KEY": SECRET}), 0)
        self.assertIn(f"ANTHROPIC_API_KEY={SECRET}\n", [i for i in self.inputs if i])
        for call in self.lima.calls:
            self.assertFalse(any(SECRET in str(a) for a in call), call)
        (launch,) = self.launches()
        self.assertIn("/dev/shm/wmf-sbx-cred.Xy12Ab", launch)

    def test_a_broken_invariant_starts_no_session(self):
        self.lima.invariants = [False] * len(vm.INVARIANTS)
        self.assertEqual(self.resume(), 1)
        self.assertEqual(self.launches(), [])
        self.assertIn("security invariant", self.err)

    def test_a_non_flag_before_the_separator_is_refused(self):
        self.assertEqual(self.resume("hello"), 2)
        self.assertEqual(self.lima.calls, [])

    def test_info_commands_do_not_mark_it_attached(self):
        self.resume("--", "mcp", "list")
        self.assertFalse(state_mod.load("demo", self.env)["attached"])

    def test_run_is_an_alias(self):
        rc = self.quiet(resume.run_main, ["--name", "demo", "--", "-p", "x"],
                        lima=self.lima, env=self.env)
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(len(self.launches()), 1)


class CreateSessionTests(Env):

    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(self.tmp)
        allow_tmp(self)
        self.repo = os.path.join(self.tmp, "src", "demo")
        git_repo(self.repo)
        self.ro = os.path.join(self.tmp, "src", "dep")
        git_repo(self.ro)

    def test_create_runs_wmf_claude_setup_and_sends_the_session(self):
        lima = FakeLima()
        inputs = []
        orig = lima.call

        def call(*args, **kw):
            inputs.append((args, kw.get("input")))
            return orig(*args, **kw)
        lima.call = call
        argv = ["--no-deps", "--config", os.path.join(self.tmp, "repos.yaml"),
                "--image", self.KEY, "--name", "demo", self.repo, self.ro + ":ro"]
        self.assertEqual(self.quiet(create.main, argv, lima=lima,
                                    env=dict(self.env, PHABRICATOR_USERNAME="Tester")), 0,
                         self.err)
        shells = lima.shells()
        self.assertTrue(any(session.WMF_CLAUDE_SETUP in s for s in shells))
        config = [i for a, i in inputs if i and "phabricatorUsername" in i]
        self.assertEqual(json.loads(config[0]), {"phabricatorUsername": "Tester"})
        plan = json.loads([i for a, i in inputs if i and '"repos"' in i][0])
        self.assertEqual(plan["session"]["files"][0]["path"], "~/.claude/CLAUDE.md")
        self.assertIn("fetch demo", plan["session"]["files"][0]["content"])
        self.assertEqual(state_mod.load("demo", self.env)["readOnly"], [self.ro])
        self.assertIn("wmf-sbx resume demo", self.err)


if __name__ == "__main__":
    unittest.main()
