#!/usr/bin/env python3
"""Unit tests for phase 6, the Claude session: session.py, resume.py,
setup.write_session, the proxy-port refresh in vm.py, the create wiring,
and the lima-sbx SessionStart text -- run with:
  python3 -m unittest discover -s sbx/tests -v

The real session ran in a VM (lima-port/HANDOFF-LIMA.md §11, phase 6)."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.create as create  # noqa: E402
import wmf_sbx.credproxy as credproxy  # noqa: E402
import wmf_sbx.egress as egress  # noqa: E402
import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.resume as resume  # noqa: E402
import wmf_sbx.session as session  # noqa: E402
import wmf_sbx.setup as setup  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import wmf_sbx.vm as vm  # noqa: E402
from test_wmf_sbx_lifecycle import Done, Env, FakeLima, allow_tmp, git_repo  # noqa: E402

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

    def test_behind_the_egress_proxy_the_upstream_is_that_proxy(self):
        self.assertEqual(session.upstream_proxy({"https_proxy": "http://127.0.0.1:3128/"},
                                                dict(STATE, egress="proxy")),
                         "192.168.5.100:3128")

    def test_the_egress_address_skips_the_session_proxy(self):
        self.assertIn("192.168.5.100", session.SESSION_NO_PROXY.split(","))


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
        # The wiki, karma, and the guest's ephemeral window (browser tools).
        self.assertIn("--local-web=4000,9876,49152-53247", launcher)
        self.assertIn(("--listen-port", "0"), list(zip(launcher, launcher[1:])))
        self.assertIn("WMF_CLAUDE_NO_PAUSE=1", a)
        self.assertIn("--landlock-only", launcher)
        pairs = list(zip(launcher, launcher[1:]))
        self.assertIn(("--allow", "/home/me/src/core"), pairs)
        self.assertIn(("--read", "/home/me/src/Vector"), pairs)
        self.assertNotIn(("--allow", "/home/me/src/Cite"), pairs)
        for path in ("/run/wmf-sbx/host", "/opt/claude-code", "/opt/node", "/etc/php",
                     "/home/agent/.wmf-sbx", "/etc/fonts"):
            self.assertIn(("--read", path), pairs)
        self.assertIn(("--read-file", "/etc/gitconfig"), pairs)
        # The home is not readable under the profile; the guide is granted.
        self.assertIn(("--read-file", "/home/agent/MEDIAWIKI-TESTING.md"), pairs)
        self.assertIn(("--allow", "/home/agent/.npm"), pairs)
        # Cypress: Electron's config dir and Xvfb's keymaps (RAN).
        self.assertIn(("--allow", "/home/agent/.config/Cypress"), pairs)
        self.assertIn(("--allow", "/var/lib/xkb"), pairs)
        self.assertNotIn(("--allow", "/home/agent/.config"), pairs)
        self.assertNotIn(("--read-file", "/home/agent/.bashrc"), pairs)
        # A deny under an allowed path stops nono (Landlock).
        self.assertNotIn(("--allow", "/home/agent/.config/composer"), pairs)
        for domain in ("registry.npmjs.org", "repo.packagist.org"):
            self.assertIn(("--allow-domain", domain), pairs)

    def test_behind_the_egress_proxy_every_host_goes_to_it(self):
        a = session.launcher_argv(dict(STATE, egress="proxy", name="box"), proxy="192.168.5.100:3128")
        launcher = a[a.index(session.LAUNCHER):]
        pairs = list(zip(launcher, launcher[1:]))
        self.assertIn(("--allow-domain", "*"), pairs)
        self.assertNotIn(("--allow-domain", "registry.npmjs.org"), pairs)
        self.assertIn(("--upstream-proxy", "192.168.5.100:3128"), pairs)
        # The session-start hook names the sandbox in `wmf-sbx allow`.
        self.assertIn("WMF_CLAUDE_SBX_EGRESS=box", a)

    def test_off_the_egress_proxy_the_hook_says_nothing_of_it(self):
        self.assertFalse([x for x in self.argv() if x.startswith("WMF_CLAUDE_SBX_EGRESS=")])

    def test_nono_lets_through_every_variable_that_the_session_gets(self):
        # nono passes Claude only the profile's allow_vars; a variable
        # off that list is dropped silently (RAN: WMF_SBX_EGRESS was).
        import fnmatch
        profile = os.path.join(image.REPO_ROOT, "profiles", "wmf-engineer.json")
        with open(profile, encoding="utf-8") as f:
            allowed = json.load(f)["environment"]["allow_vars"]
        a = session.launcher_argv(dict(STATE, egress="proxy", name="box"),
                                  proxy="192.168.5.100:3128")
        names = [x.split("=", 1)[0] for x in a[:a.index("bash")]
                 if re.match(r"^[A-Z_][A-Z0-9_]*=", x)]
        self.assertIn("WMF_CLAUDE_SBX_EGRESS", names)
        for name in names:
            self.assertTrue(any(fnmatch.fnmatchcase(name, p) for p in allowed), name)

    def test_a_nested_sandbox_gets_the_lima_grants(self):
        argv = session.launcher_argv(dict(STATE, nested=True, nestedSrc="/home/eng/wmf-sbx-src"))
        joined = " ".join(argv)
        for want in ("--allow-file /dev/kvm", "--allow /home/agent/.lima",
                     "--allow-unix-socket-subtree-bind /home/agent/.lima",
                     "--read /usr/share/qemu", "--allow-domain deb.debian.org",
                     # RAN (N3): an inner create's `nono pull` got a 403 without it.
                     "--allow-domain registry.nono.sh"):
            self.assertIn(want, joined)
        self.assertNotIn("/dev/kvm", " ".join(session.launcher_argv(STATE)))

    def test_the_nested_install_script_is_valid_bash(self):
        import wmf_sbx.nested as nested
        for arch in nested.PACKAGES:
            script = nested.install_script(arch, image.base_image(arch), "/home/eng/wmf-sbx-src")
            res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_a_staged_base_image_is_used_instead_of_a_download(self):
        import wmf_sbx.nested as nested
        base = image.base_image("x86_64")
        script = nested.install_script("x86_64", base, "/home/eng/wmf-sbx-src",
                                       staged=nested.STAGED_BASE)
        res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn(base["location"], script)
        self.assertIn(f"install -m 0644 {nested.STAGED_BASE} ", script)
        self.assertIn("sha512sum -c", script)
        self.assertIn(f"rm -f {nested.STAGED_BASE}", script)

    def test_host_base_downloads_once_and_checks_the_digest(self):
        import hashlib
        import io
        import wmf_sbx.nested as nested
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        env = {"XDG_CACHE_HOME": tmp}
        data = b"qcow2 bytes"
        base = {"location": "https://cloud.debian.org/x/debian.qcow2",
                "digest": "sha512:" + hashlib.sha512(data).hexdigest()}
        fetched = []

        def urlopen(url, timeout=None):
            fetched.append(url)
            return io.BytesIO(data)
        os.makedirs(nested.base_cache_dir(env))
        old = os.path.join(nested.base_cache_dir(env), "old.qcow2")
        open(old, "w").close()
        path = nested.host_base(base, env=env, log=lambda m: None, urlopen=urlopen)
        self.assertEqual(path, os.path.join(nested.base_cache_dir(env), "debian.qcow2"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertFalse(os.path.exists(old))
        nested.host_base(base, env=env, log=lambda m: None, urlopen=urlopen)
        self.assertEqual(fetched, [base["location"]])
        bad = dict(base, location="https://cloud.debian.org/x/other.qcow2",
                   digest="sha512:" + "0" * 128)
        with self.assertRaises(nested.NestedError):
            nested.host_base(bad, env=env, log=lambda m: None, urlopen=urlopen)
        self.assertEqual(os.listdir(nested.base_cache_dir(env)), ["debian.qcow2"])

    def test_the_nested_install_runs_limactl_as_the_agent_and_checks_the_base_image(self):
        import wmf_sbx.nested as nested
        base = image.base_image("x86_64")
        script = nested.install_script("x86_64", base, "/home/eng/wmf-sbx-src")
        # limactl refuses root; every limactl call goes through runuser.
        for line in script.splitlines():
            if "limactl --version" in line:
                self.assertIn("runuser -u agent -- env HOME=/home/agent /usr/local/bin/limactl", line)
        self.assertIn(base["location"], script)
        self.assertIn("sha512sum -c", script)
        self.assertIn(base["digest"].split(":", 1)[1], script)
        self.assertIn('test -e "$1" || cat > "$1"', script)
        grants = " ".join(nested.session_grants("/home/eng/wmf-sbx-src"))
        self.assertIn("--read " + image.LOCAL_BASE_DIR, grants)
        self.assertIn("--allow /home/eng/wmf-sbx-src", grants)

    def run_stop_inner(self, list_out, nono_running=False, stop_rc=0):
        """stop_inner_script with fake pgrep and runuser (which runs the
        limactl it is given: a fake one). Returns (result, limactl calls)."""
        import wmf_sbx.nested as nested
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        log = os.path.join(d, "calls")
        fakes = {
            "pgrep": f"exit {0 if nono_running else 1}",
            # runuser -u agent -- env HOME=... LIMACTL ARGS
            "runuser": 'shift 3; exec "$@"',
            "limactl": (f'echo "$*" >> {log}\n'
                        f'case $1 in list) printf %s {shlex.quote(list_out)};; '
                        f'stop) read -r -t 0.1 x && exit 9; exit {stop_rc};; esac'),
        }
        for tool, body in fakes.items():
            with open(os.path.join(d, tool), "w") as f:
                f.write("#!/bin/bash\n" + body.replace("\\n", "\n") + "\n")
            os.chmod(os.path.join(d, tool), 0o755)
        res = subprocess.run(["bash"], input=nested.stop_inner_script(os.path.join(d, "limactl")),
                             text=True, capture_output=True,
                             env=dict(os.environ, PATH=d + ":" + os.environ["PATH"]))
        if not os.path.exists(log):
            return res, []
        with open(log) as f:
            return res, f.read().splitlines()

    def test_resume_stops_the_inner_vms_that_still_run(self):
        res, calls = self.run_stop_inner("wmf-sbx-inner Running\nwmf-sbx-old Stopped\n"
                                         "wmf-sbx-two Running\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "stopped wmf-sbx-inner\nstopped wmf-sbx-two\n")
        # each stop has no stdin: it did not take the rest of the list
        self.assertEqual(calls[1:], ["stop wmf-sbx-inner", "stop wmf-sbx-two"])

    def test_resume_leaves_the_inner_vms_of_a_running_session(self):
        import wmf_sbx.nested as nested
        res, calls = self.run_stop_inner("wmf-sbx-inner Running\n", nono_running=True)
        self.assertEqual(res.returncode, nested.SESSION_RUNNING)
        self.assertEqual(calls, [])

    def test_resume_reports_an_inner_vm_it_could_not_stop(self):
        res, _calls = self.run_stop_inner("wmf-sbx-inner Running\n", stop_rc=1)
        self.assertEqual(res.stdout, "failed wmf-sbx-inner\n")

    def test_the_inner_repos_have_paths_an_inner_vm_can_use(self):
        # RAN (N3): the inner create refused ~/wmf-sbx-src/extensions/Cite,
        # under the agent's home, which the inner VM uses itself (D8).
        import wmf_sbx.nested as nested
        import wmf_sbx.repos as repos
        import yaml
        src = nested.src_dir("/home/eng/")
        self.assertEqual(src, "/home/eng/wmf-sbx-src")
        repos.check_vm_path(src)
        for rule in yaml.safe_load(nested.settings(src))["repos"]:
            self.assertTrue(rule["path"].startswith(src + "/"), rule)
            repos.check_vm_path(rule["path"].replace("{name}", "x"))
        # Root makes it: the agent may not write its parent.
        self.assertIn("install -d -m 0700 -o agent", nested.install_script(
            "x86_64", image.base_image("x86_64"), src))

    def test_the_sandbox_claude_md_gives_manual_test_its_base_url(self):
        # The manual-test skill looks for a "Manual testing" section; the
        # sandbox wiki is a docroot install, with no /w prefix.
        md = session.home_claude_md("demo")
        self.assertIn("Manual testing (`/wmf-claude:manual-test`)", md)
        self.assertIn("`$MW_SERVER/api.php`", md)
        self.assertNotIn("/w/api.php", md)
        self.assertIn(f"http://localhost:{session.WIKI_PORT}", md)

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

    def test_write_session_merges_the_preference_settings(self):
        with tempfile.TemporaryDirectory() as home:
            run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0)  # noqa: E731
            self.assertTrue(setup.write_session(
                {"env": {"A": "1"},
                 "settings": session.preference_settings(
                     {"model": "opus", "permission_mode": "bypassPermissions"})},
                home=home, run=run))
            with open(os.path.join(home, ".claude", "settings.json")) as f:
                settings = json.load(f)
            self.assertEqual(settings["env"], {"A": "1"})
            self.assertEqual(settings["model"], "opus")
            self.assertEqual(settings["permissions"], {"defaultMode": "bypassPermissions"})
            self.assertIs(settings["skipDangerousModePermissionPrompt"], True)


class PreferenceTests(unittest.TestCase):

    def test_no_preferences_change_nothing(self):
        self.assertEqual(session.preference_settings(None), {})
        self.assertEqual(session.preference_settings({}), {})

    def test_the_model_and_a_cautious_mode(self):
        self.assertEqual(session.preference_settings(
            {"model": "claude-opus-5-5[1m]", "permission_mode": "acceptEdits"}),
            {"model": "claude-opus-5-5[1m]", "permissions": {"defaultMode": "acceptEdits"}})

    def test_only_bypass_skips_the_confirmation(self):
        self.assertNotIn("skipDangerousModePermissionPrompt",
                         session.preference_settings({"permission_mode": "plan"}))
        self.assertTrue(session.preference_settings(
            {"permission_mode": "bypassPermissions"})["skipDangerousModePermissionPrompt"])

    def test_the_first_run_answers(self):
        patch = session.claude_json_patch({"theme": "light", "model": "opus"},
                                          ["/home/me/src/core", "/home/me/src/Cite"], "2.1.287")
        self.assertEqual(patch["hasCompletedOnboarding"], True)
        self.assertEqual(patch["lastOnboardingVersion"], "2.1.287")
        self.assertEqual(patch["theme"], "light")
        self.assertEqual(patch["projects"]["/home/me/src/Cite"],
                         {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True})
        self.assertNotIn("model", patch)  # that one goes to settings.json
        bare = session.claude_json_patch(None, [], None)
        self.assertNotIn("theme", bare)
        self.assertNotIn("lastOnboardingVersion", bare)
        # the theme is a preference, but not a settings.json key
        self.assertEqual(session.preference_settings({"theme": "dark-ansi"}), {})

    def test_write_session_keeps_what_is_in_claude_json(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, ".claude.json"), "w") as f:
                json.dump({"mcpServers": {"phabricator": {"command": "x"}}}, f)
            run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0)  # noqa: E731
            self.assertTrue(setup.write_session(
                {"claude_json": session.claude_json_patch({}, ["/p"], "2.1.287")},
                home=home, run=run))
            with open(os.path.join(home, ".claude.json")) as f:
                data = json.load(f)
            self.assertEqual(data["mcpServers"], {"phabricator": {"command": "x"}})
            self.assertTrue(data["projects"]["/p"]["hasTrustDialogAccepted"])

    def test_ste_false_drops_the_writing_rule_through_the_env(self):
        self.assertEqual(session.preference_settings({"ste": False}),
                         {"env": {"WMF_CLAUDE_STE": "0"}})
        self.assertEqual(session.preference_settings({"ste": True}), {})
        plan = session.session_plan("demo", [], preferences={"ste": False})
        self.assertEqual(plan["env"]["WMF_CLAUDE_STE"], "0")
        self.assertNotIn("env", plan["settings"])

    def test_the_first_sandbox_asks_for_the_bypass_confirmation(self):
        prefs = {"permission_mode": "bypassPermissions"}
        self.assertNotIn("skipDangerousModePermissionPrompt",
                         session.preference_settings(prefs, quiet=False))
        self.assertEqual(session.claude_json_patch({"theme": "light"}, ["/p"], "2", quiet=False),
                         {"theme": "light"})

    def test_bad_preferences_are_refused(self):
        for prefs in ({"modle": "opus"}, {"model": "opus; rm -rf /"}, {"model": 5},
                      {"permission_mode": "yolo"}, {"theme": "solarized"}, {"ste": "no"},
                      ["model"]):
            with self.assertRaises(session.SessionError, msg=prefs):
                session.preference_settings(prefs)


class GitIdentityTests(unittest.TestCase):

    def test_the_hosts_identity_goes_to_author_and_committer(self):
        values = {"user.name": "Ed Engineer", "user.email": "ed@example.org"}

        def run(argv, **kw):
            v = values.get(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if v else 1, stdout=(v or "") + "\n")
        ident = session.git_identity(run=run)
        self.assertEqual(ident["GIT_AUTHOR_NAME"], "Ed Engineer")
        self.assertEqual(ident["GIT_COMMITTER_EMAIL"], "ed@example.org")
        self.assertEqual(session.session_env([], identity=ident)["GIT_AUTHOR_EMAIL"],
                         "ed@example.org")
        values.clear()
        self.assertEqual(session.git_identity(run=run), {})

    def test_composer_scripts_have_no_process_timeout(self):
        # core's `composer test` outlasts the 300 s default in a VM.
        self.assertEqual(session.session_env([])["COMPOSER_PROCESS_TIMEOUT"], "0")

    def test_settings_environment_goes_into_the_session_env(self):
        plan = session.session_plan("demo", [], environment={"GERRITUA": "ua", "N": 1,
                                                             "FLAG": True},
                                    preferences={"ste": False})
        self.assertEqual(plan["env"]["GERRITUA"], "ua")
        self.assertEqual(plan["env"]["N"], "1")
        self.assertEqual(plan["env"]["FLAG"], "1")
        self.assertEqual(plan["env"]["WMF_CLAUDE_STE"], "0")

    def test_the_session_env_names_the_sandbox(self):
        # bin/git-review-check: `local` is the host's repo only in a
        # sandbox, which it knows from $SANDBOX_NAME.
        plan = session.session_plan("demo", [], preferences={"ste": False})
        self.assertEqual(plan["env"]["SANDBOX_NAME"], "demo")

    def test_bad_or_reserved_environment_is_refused(self):
        for bad, why in (({"1X": "a"}, "not a variable name"),
                         ({"NO_PROXY": "x"}, "credential proxy"),
                         ({"ANTHROPIC_BASE_URL": "x"}, "credential proxy"),
                         ({"SANDBOX_NAME": "x"}, "the sandbox's name"),
                         ({"A": ["x"]}, "want a string")):
            with self.subTest(bad=bad), self.assertRaisesRegex(session.SessionError, why):
                session.check_environment(bad)

    def test_the_credential_proxys_address_bypasses_nonos_proxy(self):
        env = session.session_env([])
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,192.168.5.2,192.168.5.100")
        self.assertEqual(env["no_proxy"], env["NO_PROXY"])
        self.assertEqual(env["ENABLE_TOOL_SEARCH"], "true")


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

    def test_the_warning_says_where_to_stop_a_vm_out_of_reach(self):
        lima = self.lima("1111")
        lima.vms["wmf-sbx-demo"] = "Running"
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, "wmf-sbx-demo"))
        with open(os.path.join(home, "wmf-sbx-demo", "ha.pid"), "w") as f:
            f.write("4242\n")

        def kill(pid, sig):
            raise PermissionError(1, "Operation not permitted")
        said = []
        with mock.patch.object(vm.os, "kill", kill):
            vm.ensure_running("demo", lima=lima, log=said.append,
                              env={"https_proxy": "http://127.0.0.1:2222", "LIMA_HOME": home})
        self.assertEqual(lima.calls, [])
        self.assertIn("outside this session's sandbox", said[0])
        self.assertNotIn("`wmf-sbx stop demo`", said[0])


class ResumeArgsTests(unittest.TestCase):

    def test_split(self):
        self.assertEqual(resume.split_args(["x", "--local-db", "--", "-p", "hi"]),
                         (["x", "--local-db"], ["-p", "hi"]))
        self.assertEqual(resume.split_args(["x"]), (["x"], None))

    def test_conversations(self):
        self.assertTrue(resume.starts_a_conversation([]))
        self.assertTrue(resume.starts_a_conversation(["--model", "haiku"]))
        self.assertFalse(resume.starts_a_conversation(["-p", "mcp list"]))
        self.assertFalse(resume.starts_a_conversation(["--print", "hi"]))
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

        self.interactive = []

        def call(*args, **kw):
            self.inputs.append(kw.get("input"))
            if kw.get("interactive"):
                self.interactive.append(("call",) + args)
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
        # a session ran: later sandboxes skip the first-run questions
        self.assertTrue(session.first_run_seen(self.env))

    def test_the_second_resume_continues(self):
        self.resume()
        self.resume()
        last = self.launches()[-1]
        self.assertEqual(last[-1], "--continue")

    def test_the_credential_stays_on_the_host(self):
        self.assertEqual(self.resume("--", "-p", "hi", env={"ANTHROPIC_API_KEY": SECRET}), 0,
                         self.err)
        for call in self.lima.calls:
            self.assertFalse(any(SECRET in str(a) for a in call), call)
        self.assertFalse(any(SECRET in i for i in self.inputs if i))
        # the phantom and the proxy's address go on stdin, into /dev/shm
        (cred,) = [i for i in self.inputs if i and "ANTHROPIC_BASE_URL" in i]
        port = re.search(r"ANTHROPIC_BASE_URL=http://192\.168\.5\.2:(\d+)\n", cred).group(1)
        self.assertRegex(cred, r"ANTHROPIC_API_KEY=wmfsbx-[\w-]{20,}\n")
        (launch,) = self.launches()
        self.assertIn("/dev/shm/wmf-sbx-cred.Xy12Ab", launch)
        self.assertIn(f"--open-port={port}", launch[:launch.index("--", launch.index(session.LAUNCHER))])
        # the host block is opened to the port before the session and closed after it
        rules = [" ".join(s) for s in self.lima.shells() if "wmf_sbx_hostblock" in " ".join(s)]
        added = [i for i, r in enumerate(rules) if f"tcp dport {port} accept" in r]
        removed = [i for i, r in enumerate(rules) if f'"wmf-sbx-cred-{port}" # handle' in r
                   and "insert" not in r]
        self.assertEqual(len(added), 1)
        self.assertEqual(len(removed), 1)
        self.assertLess(added[0], removed[0])
        shells = self.lima.shells()
        self.assertLess(shells.index(launch), [i for i, s in enumerate(shells)
                                               if " ".join(s) == rules[removed[0]]][0])

    def test_only_the_session_gets_the_terminal(self):
        self.resume("--", "-p", "hi", env={"ANTHROPIC_API_KEY": SECRET})
        shell_calls = [c for c in self.lima.calls if c[0] == "call" and c[1] == "shell"]
        self.assertTrue(shell_calls)
        self.assertEqual(self.interactive, [c for c in shell_calls
                                            if session.LAUNCHER in c])

    def test_an_oauth_token_gets_an_oauth_phantom(self):
        self.assertEqual(self.resume("--", "-p", "hi", env={"CLAUDE_CODE_OAUTH_TOKEN": SECRET}), 0)
        (cred,) = [i for i in self.inputs if i and "ANTHROPIC_BASE_URL" in i]
        self.assertRegex(cred, r"CLAUDE_CODE_OAUTH_TOKEN=wmfsbx-")
        self.assertNotIn("ANTHROPIC_API_KEY", cred)

    def test_no_credential_no_proxy(self):
        self.assertEqual(self.resume(), 0, self.err)
        self.assertFalse(any("wmf_sbx_hostblock" in " ".join(s) for s in self.lima.shells()
                             if "nft" in " ".join(s) and "insert" in " ".join(s)))
        (launch,) = self.launches()
        self.assertFalse(any(a.startswith("--open-port") for a in launch))

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

    def nested_resume(self, rc=0, stdout=""):
        st = state_mod.load("demo", self.env)
        st["nested"] = True
        state_mod.save(st, self.env)
        call = self.lima.call

        def fake(*args, **kw):
            if args[0] == "shell" and "pgrep" in (kw.get("input") or ""):
                self.lima.calls.append(("call",) + args)
                return Done(returncode=rc, stdout=stdout)
            return call(*args, **kw)
        self.lima.call = fake
        return self.resume()

    def stop_inner_shells(self):
        return [i for i, s in enumerate(self.lima.shells()) if s[-3:] == ("sudo", "bash", "-s")]

    def test_a_nested_resume_stops_the_inner_vms_first(self):
        self.assertEqual(self.nested_resume(stdout="stopped wmf-sbx-inner\n"), 0, self.err)
        (i,) = self.stop_inner_shells()
        self.assertLess(i, self.lima.shells().index(self.launches()[0]))
        self.assertIn("stopped the inner VMs that an earlier session started: inner", self.err)

    def test_a_nested_resume_beside_a_session_starts_its_own(self):
        import wmf_sbx.nested as nested
        self.assertEqual(self.nested_resume(rc=nested.SESSION_RUNNING), 0, self.err)
        self.assertIn("a session runs in demo", self.err)
        self.assertEqual(len(self.launches()), 1)

    def test_an_ordinary_resume_stops_no_inner_vms(self):
        self.assertEqual(self.resume(), 0, self.err)
        self.assertEqual(self.stop_inner_shells(), [])

    def egress_resume(self, *argv, env=None):
        st = state_mod.load("demo", self.env)
        st["egress"] = "proxy"
        state_mod.save(st, self.env)
        egress.make_dir("demo", self.env)
        with mock.patch.object(egress, "wrapper_env", return_value={"WMF_SBX_X": "1"}), \
                mock.patch.object(egress, "wait_time_sync", return_value=True):
            return self.resume(*argv, env=env)

    def test_behind_the_egress_proxy_the_credential_proxy_is_on_the_socket(self):
        seen = {}
        orig = credproxy.CredentialProxy.start

        def start(proxy, unix_path=None):
            seen["path"] = unix_path
            seen["in_use"] = None
            out = orig(proxy, unix_path=unix_path)
            seen["in_use"] = credproxy.socket_in_use(unix_path)
            return out
        with mock.patch.object(credproxy.CredentialProxy, "start", start):
            rc = self.egress_resume("--", "-p", "hi", env={"ANTHROPIC_API_KEY": SECRET})
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(seen, {"path": egress.cred_socket("demo", self.env), "in_use": True})
        self.assertFalse(os.path.exists(seen["path"]))   # stopped after the session
        (cred,) = [i for i in self.inputs if i and "ANTHROPIC_BASE_URL" in i]
        self.assertIn("ANTHROPIC_BASE_URL=http://192.168.5.100:3129\n", cred)
        (launch,) = self.launches()
        launcher = launch[launch.index(session.LAUNCHER):]
        self.assertIn("--open-port=3129", launcher)
        self.assertIn(("--upstream-proxy", "192.168.5.100:3128"),
                      list(zip(launcher, launcher[1:])))
        # no rule in the guest's host block: the guestfwd is already allowed
        self.assertFalse(any(credproxy.RULE_TAG in " ".join(s) for s in self.lima.shells()))

    def test_behind_the_egress_proxy_a_second_session_is_refused(self):
        path = egress.cred_socket("demo", self.env)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import socket
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(path)
            s.listen(1)
            rc = self.egress_resume("--", "-p", "hi", env={"ANTHROPIC_API_KEY": SECRET})
        self.assertEqual(rc, 1)
        self.assertIn("another session runs in demo", self.err)
        self.assertEqual(self.launches(), [])

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

    def create_demo(self):
        """Run create; return (lima shells, the stdin inputs)."""
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
        return lima.shells(), inputs

    def plan_of(self, inputs):
        return json.loads([i for a, i in inputs if i and '"repos"' in i][0])

    def test_the_first_sandbox_shows_claude_codes_first_run_questions(self):
        self.assertFalse(session.first_run_seen(self.env))
        shells, inputs = self.create_demo()
        self.assertIn("first wmf-sbx sandbox", self.err)
        self.assertFalse(any("landlock-only-ack" in " ".join(s) for s in shells))
        self.assertEqual(self.plan_of(inputs)["session"]["claude_json"], {})

    def test_create_runs_wmf_claude_setup_and_sends_the_session(self):
        session.mark_first_run_seen(self.env)  # not the first sandbox
        shells, inputs = self.create_demo()
        self.assertNotIn("first wmf-sbx sandbox", self.err)
        self.assertTrue(any(session.WMF_CLAUDE_SETUP in s for s in shells))
        # bin/claude's one-time --landlock-only acknowledgement is made at create.
        self.assertTrue(any("landlock-only-ack" in " ".join(s) for s in shells))
        # The image pins Claude Code; setup must not install another one.
        self.assertTrue(any(session.WMF_CLAUDE_SETUP in s and "WMF_CLAUDE_SKIP_UPDATE=1" in s
                            for s in shells))
        # The profile denies ~/.bashrc; it goes, after wmf-claude-setup
        # (which writes its alias there).
        setup_at = next(i for i, s in enumerate(shells) if session.WMF_CLAUDE_SETUP in s)
        rm_at = next(i for i, s in enumerate(shells) if "rm" in s and "/home/agent/.bashrc" in s)
        self.assertGreater(rm_at, setup_at)
        config = [i for a, i in inputs if i and "phabricatorUsername" in i]
        self.assertEqual(json.loads(config[0]), {"phabricatorUsername": "Tester"})
        plan = self.plan_of(inputs)
        self.assertEqual(plan["session"]["files"][0]["path"], "~/.claude/CLAUDE.md")
        self.assertIn("fetch demo", plan["session"]["files"][0]["content"])
        # the first-run questions are answered for each repo of the sandbox
        trusted = plan["session"]["claude_json"]["projects"]
        self.assertEqual(sorted(trusted), sorted([self.repo, self.ro]))
        self.assertEqual("/home/agent/" + plan["session"]["files"][1]["path"][2:],
                         session.TESTING_GUIDE_PATH)
        self.assertEqual(state_mod.load("demo", self.env)["readOnly"], [self.ro])
        self.assertIn("wmf-sbx resume demo", self.err)


if __name__ == "__main__":
    unittest.main()
