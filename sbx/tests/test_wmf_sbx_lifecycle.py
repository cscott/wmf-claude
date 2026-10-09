#!/usr/bin/env python3
"""Unit tests for the Lima lifecycle: vm.py and the verbs create, start,
stop, exec, cp, rm, status and ls -- run with:
  python3 -m unittest discover -s sbx/tests -v

A FakeLima stands in for limactl: it keeps instances and their status,
and answers the guest calls that the verbs make. No test starts a VM.
The real round trip ran in a VM (lima-port/HANDOFF-LIMA.md §11, phase 3).
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.cp as cp  # noqa: E402
import wmf_sbx.create as create  # noqa: E402
import wmf_sbx.exec as exec_mod  # noqa: E402
import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.lima as lima_mod  # noqa: E402
import wmf_sbx.ls as ls  # noqa: E402
import wmf_sbx.rm as rm  # noqa: E402
import wmf_sbx.start as start  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import wmf_sbx.status as status_mod  # noqa: E402
import wmf_sbx.stop as stop  # noqa: E402
import wmf_sbx.template as template  # noqa: E402
import wmf_sbx.vm as vm  # noqa: E402

HOST_UID = os.getuid() or 30033


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeLima:
    """Instances and their status; records every call. `invariants` is
    the guest's verdict for each of vm.INVARIANTS."""

    def __init__(self, instances=None, invariants=None, rc=0):
        self.vms = dict(instances or {})
        self.calls = []
        self.invariants = invariants or [True] * len(vm.INVARIANTS)
        self.rc = rc
        self.templates = {}

    def instances(self):
        return dict(self.vms)

    def exists(self, name):
        return name in self.vms

    def status(self, name):
        return self.vms.get(name)

    def validate(self, path):
        self.calls.append(("validate",))

    def create(self, name, path):
        with open(path) as f:
            self.templates[name] = image.yaml.safe_load(f)
        self.calls.append(("create", name))
        self.vms[name] = "Stopped"

    def start(self, name, timeout=None):
        self.calls.append(("start", name))
        self.vms[name] = "Running"

    def stop(self, name):
        self.calls.append(("stop", name))
        self.vms[name] = "Stopped"

    def delete(self, name):
        self.calls.append(("delete", name))
        self.vms.pop(name, None)

    def copy(self, src, dst, recursive=False):
        self.calls.append(("copy", src, dst, recursive))

    def call(self, *args, check=True, capture=True, input=None, text=True):
        self.calls.append(("call",) + tuple(args))
        argv = list(args)
        if argv[0] == "shell" and "bash" in argv and any("echo 0:" in a or "0:ok" in a
                                                       or "WMF_SBX_HOST_UID" in a for a in argv):
            out = "\n".join(f"{i}:{'ok' if ok else 'FAIL'}" for i, ok in enumerate(self.invariants))
            return Done(stdout=out)
        if argv[0] == "shell" and any("mktemp -d /tmp/wmf-sbx-cp" in a for a in argv):
            return Done(stdout="/tmp/wmf-sbx-cp.AbC123\n")
        if argv[0] == "shell" and argv[-3:-1] == ["test", "-d"]:
            return Done(returncode=1)
        return Done(returncode=self.rc)

    def shells(self):
        return [c[1:] for c in self.calls if c[0] == "call" and c[1] == "shell"]


class Env(unittest.TestCase):
    """State and cache in a temporary directory; one cached image."""

    KEY = "0123456789abcdef"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                    "HOME": self.tmp}
        d = image.entry_dir(self.KEY, self.env)
        os.makedirs(d)
        g = os.path.join(d, "golden.qcow2")
        with open(g, "wb") as f:
            f.write(b"golden")
        os.chmod(g, 0o444)
        with open(os.path.join(d, "golden.sha256"), "w") as f:
            f.write(image._sha256_file(g) + "  golden.qcow2\n")
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"key": self.KEY, "inputs": {"arch": "x86_64"}}, f)

    def _cleanup(self):
        for dirpath, _dirs, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(dirpath, f), 0o644)
        shutil.rmtree(self.tmp)

    def sandbox(self, name="sbx-demo", status="Running", primary="/home/me/src/demo", lima=None,
                backend="lima"):
        st = state_mod.new_state(name, created="2026-10-09T00:00:00+00:00", primary_dir=primary,
                                 image=self.KEY, vm_type="qemu", repos=[primary])
        st["backend"] = backend
        state_mod.save(st, self.env)
        lima = lima or FakeLima()
        if status:
            lima.vms[vm.instance_name(name)] = status
        return lima

    def quiet(self, fn, *a, **kw):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = fn(*a, **kw)
        self.err, self.out = err.getvalue(), out.getvalue()
        return rc


# -- vm.py ----------------------------------------------------------------

class VmTests(Env):

    def test_instance_names_are_prefixed(self):
        self.assertEqual(vm.instance_name("sbx-cite"), "wmf-sbx-sbx-cite")
        with self.assertRaises(vm.VmError):
            vm.instance_name("builder-1234")

    def test_agent_argv_has_a_fixed_path_and_no_login_shell(self):
        argv = vm.agent_argv(["git", "log"], workdir="/home/me/src/x")
        self.assertEqual(argv[:7], ["sudo", "-H", "-u", "agent", "env", "-C", "/home/me/src/x"])
        self.assertEqual(argv[7], f"PATH={vm.AGENT_PATH}")
        self.assertTrue(vm.AGENT_PATH.endswith("/home/agent/.local/bin"))
        self.assertEqual(argv[-2:], ["git", "log"])
        self.assertNotIn("-i", argv)

    def test_check_invariants_reads_each_verdict(self):
        lima = self.sandbox(lima=FakeLima(invariants=[True, False] + [True] * (len(vm.INVARIANTS) - 2)))
        results = vm.check_invariants("sbx-demo", host_uid=HOST_UID, lima=lima)
        self.assertEqual([ok for _d, ok in results][:2], [True, False])
        script = lima.shells()[-1][-1]
        self.assertIn(f"WMF_SBX_HOST_UID={HOST_UID}", script)

    def test_a_missing_verdict_counts_as_failed(self):
        class Silent(FakeLima):
            def call(self, *a, **kw):
                return Done(stdout="")
        results = vm.check_invariants("x", host_uid=HOST_UID, lima=Silent())
        self.assertFalse(any(ok for _d, ok in results))

    def test_create_refuses_an_existing_instance(self):
        lima = FakeLima({"wmf-sbx-dup": "Stopped"})
        with self.assertRaisesRegex(vm.VmError, "exists already"):
            vm.create("dup", {"x": 1}, lima=lima, log=lambda m: None)

    def test_resolve_requires_a_lima_state_file(self):
        with self.assertRaisesRegex(state_mod.StateError, "not a wmf-sbx sandbox"):
            vm.resolve("nobody", env=self.env)
        self.sandbox(name="old", backend=None)
        with self.assertRaisesRegex(state_mod.StateError, "Docker sbx sandbox"):
            vm.resolve("old", env=self.env)


# -- create ---------------------------------------------------------------

class CreateTests(Env):

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "demo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.config = os.path.join(self.tmp, "repos.yaml")
        self.inputs = {"arch": "x86_64", "k": 1}
        self.built = []

    def fake_inputs(self):
        return self.inputs

    def fake_build(self, inputs, **kw):
        self.built.append(inputs)

    def create(self, *extra, lima=None, inputs=True):
        lima = lima or FakeLima()
        self.lima = lima
        argv = ["--no-deps", "--config", self.config, "--image", self.KEY, self.repo] + list(extra)
        if not inputs:
            argv.remove("--image")
            argv.remove(self.KEY)
        return self.quiet(create.main, argv, lima=lima, env=self.env, build=self.fake_build,
                          inputs_fn=self.fake_inputs, prompt=lambda q: "")

    def test_a_create_saves_the_state_first_then_makes_and_checks_the_vm(self):
        self.assertEqual(self.create("--name", "demo"), 0, self.err)
        st = state_mod.load("demo", self.env)
        self.assertEqual(st["backend"], "lima")
        self.assertEqual(st["image"], self.KEY)
        self.assertEqual(st["primaryDir"], os.path.realpath(self.repo))
        self.assertEqual(st["repos"], [os.path.realpath(self.repo)])
        kinds = [c[0] for c in self.lima.calls]
        self.assertEqual(kinds[:3], ["validate", "create", "start"])
        tmpl = self.lima.templates["wmf-sbx-demo"]
        template.check_template(tmpl, host_uid=os.getuid())
        self.assertEqual(tmpl["images"][0]["location"], image.golden_path(self.KEY, self.env))
        self.assertIn("ready", self.err)

    def test_dry_run_creates_nothing_and_prints_the_config(self):
        self.assertEqual(self.create("--dry-run", "--name", "demo"), 0, self.err)
        self.assertEqual(self.lima.calls, [])
        self.assertIsNone(state_mod.load("demo", self.env))
        self.assertIn("plain: false", self.err)
        self.assertIn("wmf-sbx-demo", self.err)

    def test_without_image_the_current_inputs_are_built(self):
        self.assertEqual(self.create("--name", "demo", inputs=False), 0, self.err)
        self.assertEqual(self.built, [self.inputs])
        self.assertEqual(state_mod.load("demo", self.env)["image"], image.cache_key(self.inputs))

    def test_sudo_is_refused_for_now(self):
        self.assertEqual(self.create("--sudo"), 1)
        self.assertIn("not ported yet", self.err)
        self.assertEqual(self.lima.calls, [])

    def test_a_taken_name_is_refused(self):
        self.assertEqual(self.create("--name", "demo", lima=FakeLima({"wmf-sbx-demo": "Stopped"})), 1)
        self.assertIn("exists already", self.err)

    def test_the_default_name_avoids_lima_instances_too(self):
        lima = FakeLima({"wmf-sbx-sbx-demo": "Stopped"})
        self.assertEqual(self.create(lima=lima), 0, self.err)
        self.assertIsNotNone(state_mod.load("sbx-demo-2", self.env))

    def test_a_failed_invariant_fails_the_create(self):
        lima = FakeLima(invariants=[False] * len(vm.INVARIANTS))
        self.assertEqual(self.create("--name", "demo", lima=lima), 1)
        self.assertIn("security invariant", self.err)

    def test_a_failed_vm_create_keeps_the_state_for_rm(self):
        class Broken(FakeLima):
            def start(self, name, timeout=None):
                raise lima_mod.LimaError("boom")
        self.assertEqual(self.create("--name", "demo", lima=Broken()), 1)
        self.assertIsNotNone(state_mod.load("demo", self.env))
        self.assertIn("wmf-sbx rm demo", self.err)

    def test_a_missing_or_changed_image_is_refused(self):
        g = image.golden_path(self.KEY, self.env)
        os.chmod(g, 0o644)
        self.assertEqual(self.create("--name", "demo"), 1)
        self.assertIn("writable", self.err)


# -- start, stop, status, ls ----------------------------------------------

class StartStopStatusTests(Env):

    def test_start_boots_a_stopped_vm_and_checks_it(self):
        lima = self.sandbox(status="Stopped")
        self.assertEqual(self.quiet(start.main, ["sbx-demo"], lima=lima, env=self.env), 0, self.err)
        self.assertIn(("start", "wmf-sbx-sbx-demo"), lima.calls)

    def test_start_fails_on_a_broken_invariant(self):
        lima = self.sandbox(lima=FakeLima(invariants=[True] * (len(vm.INVARIANTS) - 1) + [False]))
        self.assertEqual(self.quiet(start.main, ["sbx-demo"], lima=lima, env=self.env), 1)
        self.assertIn("FAIL", self.err)

    def test_start_refuses_a_sandbox_wmf_sbx_does_not_own(self):
        lima = FakeLima({"wmf-sbx-other": "Stopped"})
        self.assertEqual(self.quiet(start.main, ["other"], lima=lima, env=self.env), 1)
        self.assertEqual(lima.calls, [])

    def test_stop(self):
        lima = self.sandbox()
        self.assertEqual(self.quiet(stop.main, ["sbx-demo"], lima=lima, env=self.env), 0)
        self.assertEqual(lima.vms["wmf-sbx-sbx-demo"], "Stopped")

    def test_status_is_good_for_a_running_sound_sandbox(self):
        lima = self.sandbox()
        self.assertEqual(self.quiet(status_mod.main, ["sbx-demo"], lima=lima, env=self.env), 0,
                         self.out + self.err)
        self.assertIn("checksum matches", self.out)

    def test_status_reports_a_changed_image_and_a_missing_vm(self):
        lima = self.sandbox(status=None)
        g = image.golden_path(self.KEY, self.env)
        os.chmod(g, 0o644)
        self.assertEqual(self.quiet(status_mod.main, ["sbx-demo"], lima=lima, env=self.env), 1)
        self.assertIn("MISSING", self.out)
        self.assertIn("PROBLEM", self.out)

    def test_ls_shows_lima_sandboxes(self):
        lima = self.sandbox()
        self.sandbox(name="old", backend=None, lima=lima)
        self.assertEqual(self.quiet(ls.main, [], lima=lima, env=self.env), 0)
        self.assertIn("sbx-demo", self.out)
        self.assertNotIn("old", self.out)


# -- exec -----------------------------------------------------------------

class ExecTests(Env):

    def test_exec_runs_as_the_agent_in_the_primary_workspace(self):
        lima = self.sandbox(status="Stopped")
        rc = self.quiet(exec_mod.main, ["sbx-demo", "--", "git", "status"], lima=lima, env=self.env)
        self.assertEqual(rc, 0)
        self.assertIn(("start", "wmf-sbx-sbx-demo"), lima.calls)
        argv = lima.shells()[-1]
        i = argv.index("--")
        self.assertEqual(argv[i + 1:i + 8], ("sudo", "-H", "-u", "agent", "env", "-C",
                                             "/home/me/src/demo"))
        self.assertEqual(argv[-2:], ("git", "status"))

    def test_exec_returns_the_commands_status(self):
        lima = self.sandbox(lima=FakeLima(rc=7))
        self.assertEqual(self.quiet(exec_mod.main, ["sbx-demo", "--", "false"],
                                    lima=lima, env=self.env), 7)

    def test_engineer_refuses_an_agent_writable_workdir(self):
        lima = self.sandbox()
        for wd in ("/home/agent", "/home/agent/x", "/tmp", "/tmp/y"):
            with self.subTest(wd=wd):
                self.assertEqual(self.quiet(exec_mod.main, ["--engineer", "-w", wd, "sbx-demo",
                                                            "--", "ls"], lima=lima, env=self.env), 1)
                self.assertIn("agent can write", self.err)

    def test_engineer_runs_without_sudo_u_agent(self):
        lima = self.sandbox()
        self.assertEqual(self.quiet(exec_mod.main, ["--engineer", "sbx-demo", "--", "id"],
                                    lima=lima, env=self.env), 0)
        argv = lima.shells()[-1]
        self.assertNotIn("agent", argv[argv.index("--") + 1:])

    def test_a_command_is_required(self):
        lima = self.sandbox()
        with self.assertRaises(SystemExit):
            self.quiet(exec_mod.main, ["sbx-demo"], lima=lima, env=self.env)


# -- cp -------------------------------------------------------------------

class CpTests(Env):

    def test_copy_in_stages_then_the_agent_copies(self):
        lima = self.sandbox()
        src = os.path.join(self.tmp, "f.txt")
        open(src, "w").close()
        self.assertEqual(self.quiet(cp.main, [src, "sbx-demo:notes/f.txt"], lima=lima,
                                    env=self.env), 0, self.err)
        copies = [c for c in lima.calls if c[0] == "copy"]
        self.assertEqual(copies, [("copy", src, "wmf-sbx-sbx-demo:/tmp/wmf-sbx-cp.AbC123/", False)])
        agent_cp = [s for s in lima.shells() if "cp" in s and "agent" in s][0]
        self.assertEqual(agent_cp[-2:], ("/tmp/wmf-sbx-cp.AbC123/f.txt",
                                         "/home/me/src/demo/notes/f.txt"))

    def test_copy_out_has_the_agent_stage_it(self):
        lima = self.sandbox()
        dst = os.path.join(self.tmp, "out")
        self.assertEqual(self.quiet(cp.main, ["sbx-demo:/home/me/src/demo/README", dst],
                                    lima=lima, env=self.env), 0, self.err)
        staging = [s for s in lima.shells() if any("mktemp" in a for a in s)][0]
        self.assertIn("agent", staging)
        copies = [c for c in lima.calls if c[0] == "copy"]
        self.assertEqual(copies, [("copy", "wmf-sbx-sbx-demo:/tmp/wmf-sbx-cp.AbC123/README",
                                   dst, False)])

    def test_exactly_one_side_is_in_the_sandbox(self):
        lima = self.sandbox()
        self.assertEqual(self.quiet(cp.main, ["/a", "/b"], lima=lima, env=self.env), 1)
        self.assertEqual(self.quiet(cp.main, ["sbx-demo:/a", "sbx-demo:/b"], lima=lima,
                                    env=self.env), 1)

    def test_a_relative_path_without_a_workspace_is_in_the_agents_home(self):
        lima = self.sandbox(primary=None)
        src = os.path.join(self.tmp, "f.txt")
        open(src, "w").close()
        self.assertEqual(self.quiet(cp.main, [src, "sbx-demo:x.txt"], lima=lima, env=self.env), 0)
        agent_cp = [s for s in lima.shells() if "cp" in s and "agent" in s][0]
        self.assertEqual(agent_cp[-1], "/home/agent/x.txt")


# -- rm -------------------------------------------------------------------

class RmTests(Env):

    def test_rm_refuses_a_sandbox_wmf_sbx_does_not_own(self):
        lima = FakeLima({"wmf-sbx-stranger": "Running"})
        self.assertEqual(self.quiet(rm.main, ["stranger"], lima=lima, env=self.env,
                                    confirm=lambda q: "y"), 1)
        self.assertIn("wmf-sbx-stranger", lima.vms)

    def test_declining_leaves_everything(self):
        lima = self.sandbox()
        self.assertEqual(self.quiet(rm.main, ["sbx-demo"], lima=lima, env=self.env,
                                    confirm=lambda q: "n"), 1)
        self.assertIn("wmf-sbx-sbx-demo", lima.vms)
        self.assertIsNotNone(state_mod.load("sbx-demo", self.env))

    def test_yes_deletes_the_vm_and_the_state(self):
        lima = self.sandbox()
        self.assertEqual(self.quiet(rm.main, ["sbx-demo"], lima=lima, env=self.env,
                                    confirm=lambda q: "y"), 0, self.err)
        self.assertNotIn("wmf-sbx-sbx-demo", lima.vms)
        self.assertIsNone(state_mod.load("sbx-demo", self.env))

    def test_force_asks_nothing(self):
        lima = self.sandbox()
        def never(q):
            raise AssertionError("asked")
        self.assertEqual(self.quiet(rm.main, ["-f", "sbx-demo"], lima=lima, env=self.env,
                                    confirm=never), 0)
        self.assertNotIn("wmf-sbx-sbx-demo", lima.vms)

    def test_dry_run_changes_nothing(self):
        lima = self.sandbox()
        self.assertEqual(self.quiet(rm.main, ["--dry-run", "sbx-demo"], lima=lima, env=self.env,
                                    confirm=lambda q: "y"), 0)
        self.assertIn("wmf-sbx-sbx-demo", lima.vms)
        self.assertIsNotNone(state_mod.load("sbx-demo", self.env))

    def test_prune_forgets_a_sandbox_whose_vm_is_gone(self):
        lima = self.sandbox(status=None)
        self.sandbox(name="alive", lima=lima)
        self.assertEqual(self.quiet(rm.main, ["--prune"], lima=lima, env=self.env), 0, self.err)
        self.assertIsNone(state_mod.load("sbx-demo", self.env))
        self.assertIsNotNone(state_mod.load("alive", self.env))


if __name__ == "__main__":
    unittest.main()
