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
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.cp as cp  # noqa: E402
import wmf_sbx.create as create  # noqa: E402
import wmf_sbx.exec as exec_mod  # noqa: E402
import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.lima as lima_mod  # noqa: E402
import wmf_sbx.nested as nested  # noqa: E402
import wmf_sbx.repos as repos  # noqa: E402
import wmf_sbx.ls as ls  # noqa: E402
import wmf_sbx.rm as rm  # noqa: E402
import wmf_sbx.egress as egress  # noqa: E402
import wmf_sbx.start as start  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import wmf_sbx.status as status_mod  # noqa: E402
import wmf_sbx.stop as stop  # noqa: E402
import wmf_sbx.template as template  # noqa: E402
import wmf_sbx.vm as vm  # noqa: E402

HOST_UID = os.getuid() or 30033


def git_repo(path):
    """A real repository with one commit, on branch main."""
    os.makedirs(path)
    for argv in (["init", "-q", "-b", "main"],
                 ["-c", "user.name=t", "-c", "user.email=t@example.org",
                  "commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", "-C", path] + argv, check=True)


def allow_tmp(case):
    """The tests' repos are under the temporary directory, which
    repos.check_vm_path refuses (/tmp is a tmpfs in the VM)."""
    p = mock.patch.object(repos, "RESERVED_PREFIXES",
                          tuple(x for x in repos.RESERVED_PREFIXES
                                if not tempfile.gettempdir().startswith(x)))
    p.start()
    case.addCleanup(p.stop)


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
        self.configs = {}
        self.backings = {}

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

    def config_text(self, name, env=None):
        return self.configs.get(name, "")

    def edit(self, name, expression):
        self.calls.append(("edit", name, expression))

    lima_version = "2.2.1"

    def version(self):
        return self.lima_version

    def disk_path(self, name, env=None):
        return os.path.join(lima_mod.lima_home(env), name, "disk")

    def placeholder(self, cache_root):
        path = os.path.join(cache_root, "placeholder.qcow2")
        if not os.path.exists(path):
            os.makedirs(cache_root, exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"placeholder")
        return path

    def overlay_disk(self, name, backing, env=None):
        self.calls.append(("overlay", name, backing))
        self.backings[name] = backing
        disk = self.disk_path(name, env)
        os.makedirs(os.path.dirname(disk), exist_ok=True)
        with open(disk, "wb") as f:
            f.write(b"overlay")

    def rebase_disk(self, path, backing):
        self.calls.append(("rebase", path, backing))

    def disk_chain(self, name, env=None, force_share=False):
        """What `qemu-img info --backing-chain` says of an overlay made by
        overlay_disk: the disk, then its backing file."""
        self.calls.append(("disk_chain", name, force_share))
        disk = self.disk_path(name, env)
        backing = self.backings.get(name)
        if backing is None:
            return [{"filename": disk, "format": "qcow2"}]
        return [{"filename": disk, "format": "qcow2", "backing-filename": backing,
                 "full-backing-filename": backing},
                {"filename": backing, "format": "qcow2"}]

    def call(self, *args, check=True, capture=True, input=None, text=True, interactive=False):
        self.calls.append(("call",) + tuple(args))
        argv = list(args)
        if argv[0] == "shell" and "bash" in argv and any("echo 0:" in a or "0:ok" in a
                                                       or "WMF_SBX_HOST_UID" in a for a in argv):
            out = "\n".join(f"{i}:{'ok' if ok else 'FAIL'}" for i, ok in enumerate(self.invariants))
            return Done(stdout=out)
        if argv[0] == "shell" and any("mktemp /dev/shm/wmf-sbx-cred." in a for a in argv):
            return Done(stdout="/dev/shm/wmf-sbx-cred.Xy12Ab\n")
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
    MANIFEST_INPUTS = {"arch": image.host_arch(), "agent": {"uid": os.getuid()}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                    "HOME": self.tmp,
                    # not the Lima instances of the machine that runs the tests
                    "LIMA_HOME": os.path.join(self.tmp, "lima"),
                    # not the keychain of the machine that runs the tests
                    "WMF_SBX_HOST_LOGIN": "0"}
        d = image.entry_dir(self.KEY, self.env)
        os.makedirs(d)
        g = os.path.join(d, "golden.qcow2")
        with open(g, "wb") as f:
            f.write(b"golden")
        os.chmod(g, 0o444)
        with open(os.path.join(d, "golden.sha256"), "w") as f:
            f.write(image._sha256_file(g) + "  golden.qcow2\n")
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"key": self.KEY, "inputs": self.MANIFEST_INPUTS}, f)

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
        git_repo(self.repo)
        allow_tmp(self)
        self.config = os.path.join(self.tmp, "repos.yaml")
        self.inputs = {"arch": "x86_64", "k": 1}
        self.built = []

    def fake_inputs(self):
        return self.inputs

    def fake_build(self, inputs, **kw):
        """Record the build; leave a read-only golden file, which the
        overlay's chain check looks at."""
        self.built.append(inputs)
        key = image.cache_key(inputs)
        os.makedirs(image.entry_dir(key, self.env), exist_ok=True)
        path = image.golden_path(key, self.env)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(b"golden")
            os.chmod(path, 0o444)

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
        self.assertEqual(st["disk"], "overlay")
        # On QEMU: Lima gets the placeholder; then the disk becomes an
        # overlay on the golden image, checked before and after the start.
        golden = image.golden_path(self.KEY, self.env)
        self.assertEqual(self.lima.calls[:5], [
            ("validate",), ("create", "wmf-sbx-demo"), ("overlay", "wmf-sbx-demo", golden),
            ("disk_chain", "wmf-sbx-demo", False), ("start", "wmf-sbx-demo")])
        self.assertEqual(self.lima.calls[5], ("disk_chain", "wmf-sbx-demo", True))
        tmpl = self.lima.templates["wmf-sbx-demo"]
        template.check_template(tmpl, host_uid=os.getuid())
        self.assertEqual(tmpl["images"][0]["location"],
                         os.path.join(image.cache_root(self.env), "placeholder.qcow2"))
        self.assertIn("ready", self.err)

    def test_full_disk_and_vz_copy_the_golden_image(self):
        for flags in (["--full-disk", "--vm-type", "qemu"], ["--vm-type", "vz"]):
            with self.subTest(flags=flags):
                name = "full" if "--full-disk" in flags else "vz"
                self.assertEqual(self.create("--name", name, *flags), 0, self.err)
                self.assertNotIn("disk", state_mod.load(name, self.env))
                self.assertFalse([c for c in self.lima.calls if c[0] == "overlay"])
                self.assertEqual(self.lima.templates[f"wmf-sbx-{name}"]["images"][0]["location"],
                                 image.golden_path(self.KEY, self.env))

    def test_a_wrong_backing_chain_fails_the_create(self):
        class Flat(FakeLima):
            def overlay_disk(self, name, backing, env=None):
                self.calls.append(("overlay", name, backing))   # but no backing file

        lima = Flat()
        self.assertEqual(self.create("--name", "demo", "--vm-type", "qemu", lima=lima), 1)
        self.assertIn("has no backing file", self.err)
        self.assertNotIn(("start", "wmf-sbx-demo"), lima.calls)

    def test_an_untested_lima_gets_a_warning(self):
        lima = FakeLima()
        lima.lima_version = "2.3.0"
        self.assertEqual(self.create("--name", "demo", "--vm-type", "qemu", lima=lima), 0,
                         self.err)
        self.assertIn("Lima 2.3.0", self.err)
        self.assertIn("--full-disk", self.err)

    # -- warm snapshots (track B3) --

    def snapshots(self):
        import wmf_sbx.snapshot as snapshot
        return [(k, g, p, m) for k in snapshot.list_keys(self.env)
                for g, p, m in snapshot.list_generations(k, self.env)]

    def clone_calls(self):
        return [i for i in self.lima_inputs if i and ("clone_repo " in i or "refresh_repo " in i)]

    def create_tracking(self, *extra, lima=None):
        """create(), with the guest scripts' stdin recorded."""
        lima = lima or FakeLima()
        self.lima_inputs = []
        orig = lima.call

        def call(*args, **kw):
            self.lima_inputs.append(kw.get("input"))
            return orig(*args, **kw)
        lima.call = call
        return self.create(*extra, lima=lima)

    def test_a_cold_create_makes_a_snapshot_after_the_setup(self):
        import wmf_sbx.snapshot as snapshot
        self.assertEqual(self.create_tracking("--name", "one"), 0, self.err)
        (key, _gen, path, meta), = self.snapshots()
        self.assertIn("this create makes snapshot", self.err)
        self.assertEqual(meta["sandbox"], "one")
        self.assertEqual(meta["repos"], [{"canonical": None, "path": os.path.realpath(self.repo),
                                          "readOnly": False}])
        self.assertFalse(os.stat(path).st_mode & 0o222)
        # sealed after the setup, stopped, rebased onto the golden image,
        # and the sandbox is on a new overlay on the snapshot
        inputs = [i or "" for i in self.lima_inputs]
        setup_i = max(i for i, x in enumerate(inputs) if "def mediawiki_setup" in x)
        seal_i = max(i for i, x in enumerate(inputs) if "cloud-init clean --logs" in x)
        self.assertLess(setup_i, seal_i)
        self.assertIn(("stop", "wmf-sbx-one"), self.lima.calls)
        self.assertIn(("rebase", os.path.join(snapshot.key_dir(key, self.env),
                                              "." + os.path.basename(path)),
                       image.golden_path(self.KEY, self.env)), self.lima.calls)
        self.assertEqual(self.lima.backings["wmf-sbx-one"], path)
        self.assertEqual(state_mod.load("one", self.env)["snapshot"], path)
        self.assertIn(snapshot.borrower_name(key),
                      subprocess.run(["git", "-C", self.repo, "config", "--get-all",
                                      "wmfSbx.borrower"], capture_output=True,
                                     text=True).stdout)

    def test_the_next_create_of_the_same_repos_is_warm(self):
        self.assertEqual(self.create("--name", "one"), 0, self.err)
        (_key, _g1, first, _m), = self.snapshots()
        self.assertEqual(self.create_tracking("--name", "two"), 0, self.err)
        self.assertIn("+ warm: from snapshot", self.err)
        self.assertEqual(state_mod.load("two", self.env).get("disk"), "overlay")
        # its disk was an overlay on the snapshot; its clones were refreshed
        self.assertIn(("overlay", "wmf-sbx-two", first), self.lima.calls)
        self.assertTrue(any("\nrefresh_repo " in i for i in self.clone_calls()))
        self.assertFalse(any("\nclone_repo " in i for i in self.clone_calls()))
        # and it froze the next generation
        self.assertEqual(len(self.snapshots()), 2)

    def test_cold_and_no_snapshot(self):
        self.assertEqual(self.create("--name", "one"), 0, self.err)
        self.assertEqual(self.create_tracking("--name", "two", "--cold"), 0, self.err)
        self.assertIn("+ cold: --cold", self.err)
        self.assertFalse(any("\nrefresh_repo " in i for i in self.clone_calls()))
        self.assertEqual(len(self.snapshots()), 2)
        self.assertEqual(self.create("--name", "three", "--no-snapshot"), 0, self.err)
        self.assertEqual(len(self.snapshots()), 2)
        self.assertNotIn("snapshot", state_mod.load("three", self.env))

    def test_a_replaced_host_repo_makes_it_cold(self):
        import wmf_sbx.snapshot as snapshot
        self.assertEqual(self.create("--name", "one"), 0, self.err)
        (key, _g, _p, _m), = self.snapshots()
        subprocess.run(["git", "-C", self.repo, "config", "--unset-all", "wmfSbx.borrower"],
                       check=True)
        self.assertEqual(self.create_tracking("--name", "two"), 0, self.err)
        self.assertIn("was replaced or removed", self.err)
        self.assertFalse(any("\nrefresh_repo " in i for i in self.clone_calls()))
        self.assertIn(snapshot.borrower_name(key),
                      subprocess.run(["git", "-C", self.repo, "config", "--get-all",
                                      "wmfSbx.borrower"], capture_output=True,
                                     text=True).stdout)

    def test_vz_makes_no_snapshot(self):
        self.assertEqual(self.create("--name", "one", "--vm-type", "vz"), 0, self.err)
        self.assertEqual(self.snapshots(), [])

    def test_a_failed_freeze_keeps_the_sandbox_on_its_own_disk(self):
        class NoRebase(FakeLima):
            def rebase_disk(self, path, backing):
                raise lima_mod.LimaError("qemu-img rebase failed")
        lima = NoRebase()
        self.assertEqual(self.create("--name", "one", lima=lima), 0, self.err)
        self.assertIn("The sandbox is fine", self.err)
        self.assertEqual(self.snapshots(), [])
        self.assertTrue(os.path.exists(lima.disk_path("wmf-sbx-one", self.env)))
        self.assertEqual(lima.vms["wmf-sbx-one"], "Running")

    def walk_create(self, *extra, maps=({}, {})):
        """A --dry-run create with the dependency walk (offline): its exit
        status, and how often it fetched WMF CI's maps."""
        fetched = []

        def fetch_maps(*a, **kw):
            fetched.append(1)
            return maps
        argv = ["--config", self.config, "--image", self.KEY, "--dry-run",
                "--name", "demo", self.repo] + list(extra)
        with mock.patch.object(create.ci_mod, "fetch_maps", fetch_maps), \
                mock.patch.object(create.deps_mod, "fetch_manifest",
                                  lambda canonical, warn=None, **kw: None):
            rc = self.quiet(create.main, argv, lima=FakeLima(), env=self.env,
                            build=self.fake_build, inputs_fn=self.fake_inputs,
                            prompt=lambda q: "")
        return rc, len(fetched)

    def test_ci_deps_are_fetched_once_per_create(self):
        other = os.path.join(self.tmp, "src", "other")
        git_repo(other)
        self.assertEqual(self.walk_create(other), (0, 1), self.err)

    def test_no_ci_deps_and_no_deps_fetch_nothing(self):
        for flag in ("--no-ci-deps", "--no-deps"):
            with self.subTest(flag=flag):
                self.assertEqual(self.walk_create(flag), (0, 0), self.err)

    def test_unreachable_ci_deps_warn_and_go_on(self):
        self.assertEqual(self.walk_create(maps=None), (0, 1), self.err)
        self.assertIn("could not fetch WMF CI's dependency lists", self.err)
        self.assertIn("--no-ci-deps", self.err)

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

    def date_image(self, created):
        path = os.path.join(image.entry_dir(self.KEY, self.env), "manifest.json")
        with open(path, "w") as f:
            json.dump({"key": self.KEY, "created": created, "inputs": self.MANIFEST_INPUTS}, f)

    def test_a_create_removes_old_unused_images(self):
        self.date_image("2026-01-01T00:00:00+00:00")
        self.assertEqual(self.create("--name", "demo", inputs=False), 0, self.err)
        self.assertFalse(os.path.exists(image.entry_dir(self.KEY, self.env)))
        self.assertIn("removed 1 unused golden image(s) and snapshot(s) older than 7 days", self.err)

    def test_a_create_keeps_unused_images_younger_than_the_gc_age(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.date_image((now - datetime.timedelta(days=3)).isoformat(timespec="seconds"))
        self.assertEqual(self.create("--name", "demo", inputs=False), 0, self.err)
        self.assertTrue(os.path.isdir(image.entry_dir(self.KEY, self.env)))

    def old_entry_for_the_inputs(self):
        """A cached image of self.inputs, built in January."""
        self.fake_build(self.inputs)
        with open(os.path.join(image.entry_dir(image.cache_key(self.inputs), self.env),
                               "manifest.json"), "w") as f:
            json.dump({"created": "2026-01-01T00:00:00+00:00", "inputs": self.inputs}, f)
        self.built = []

    def test_an_old_golden_image_is_built_again(self):
        self.old_entry_for_the_inputs()
        self.assertEqual(self.create("--name", "demo", inputs=False), 0, self.err)
        (built,) = self.built
        self.assertIn("epoch", built)
        self.assertEqual(state_mod.load("demo", self.env)["image"], image.cache_key(built))
        self.assertIn("building a new one", self.err)

    def test_a_cold_snapshot_is_not_made_on_an_image_about_to_expire(self):
        self.fake_build(self.inputs)
        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=13, hours=12)
        with open(os.path.join(image.entry_dir(image.cache_key(self.inputs), self.env),
                               "manifest.json"), "w") as f:
            json.dump({"created": created.isoformat(), "inputs": self.inputs}, f)
        self.built = []
        self.assertEqual(self.create("--name", "demo", inputs=False), 0, self.err)
        (built,) = self.built
        self.assertIn("epoch", built)
        # Without a snapshot to make, the image has a day and a half left.
        self.built = []
        self.assertEqual(self.create("--name", "other", "--no-snapshot", inputs=False), 0,
                         self.err)
        self.assertNotIn("epoch", self.built[0])

    def test_no_expire_keeps_an_old_golden_image(self):
        self.old_entry_for_the_inputs()
        self.assertEqual(self.create("--name", "demo", "--no-expire", inputs=False), 0,
                         self.err)
        self.assertEqual(self.built, [self.inputs])
        self.assertIn("create --cold", self.err)

    def test_the_gc_age_is_a_setting(self):
        with open(self.config, "w") as f:
            f.write("images:\n  gcAge: 2d\n")
        now = datetime.datetime.now(datetime.timezone.utc)
        self.date_image((now - datetime.timedelta(days=3)).isoformat(timespec="seconds"))
        self.assertEqual(self.create("--name", "demo", inputs=False), 0, self.err)
        self.assertFalse(os.path.exists(image.entry_dir(self.KEY, self.env)))

    def test_latest_uses_the_newest_cached_image_and_builds_nothing(self):
        self.env[image.TREE_OVERRIDE_VAR] = "latest"
        self.assertEqual(self.create("--name", "demo", inputs=False), 0, self.err)
        self.assertEqual(self.built, [])
        self.assertEqual(state_mod.load("demo", self.env)["image"], self.KEY)
        self.assertIn("the newest cached one", self.err)

    def test_latest_with_an_empty_cache_builds(self):
        self.env[image.TREE_OVERRIDE_VAR] = "latest"
        shutil.rmtree(image.entry_dir(self.KEY, self.env))
        self.assertEqual(self.create("--name", "demo", inputs=False), 0, self.err)
        self.assertEqual(self.built, [self.inputs])

    def test_a_cached_image_removes_nothing(self):
        other = "fedcba9876543210"
        shutil.copytree(image.entry_dir(self.KEY, self.env), image.entry_dir(other, self.env))
        self.date_image("2026-01-01T00:00:00+00:00")
        self.assertEqual(self.create("--name", "demo"), 0, self.err)
        self.assertTrue(os.path.isdir(image.entry_dir(other, self.env)))

    def test_nested_installs_lima_and_records_it(self):
        inputs = []
        lima = FakeLima()
        orig = lima.call

        def call(*args, **kw):
            inputs.append(kw.get("input") or "")
            return orig(*args, **kw)
        lima.call = call
        self.assertEqual(self.create("--name", "demo", "--nested", lima=lima), 0, self.err)
        st = state_mod.load("demo", self.env)
        self.assertTrue(st["nested"])
        # The inner repos go in the engineer's home path, as on the host.
        src = os.path.join(os.path.normpath(self.env["HOME"]), "wmf-sbx-src")
        self.assertEqual(st["nestedSrc"], src)
        tmpl = lima.templates["wmf-sbx-demo"]
        self.assertEqual((tmpl["memory"], tmpl["disk"]), ("16GiB", "120GiB"))
        (script,) = [i for i in inputs if "usermod -aG kvm agent" in i]
        self.assertIn(f"lima-{nested.LIMA_VERSION}-Linux-", script)
        self.assertIn("sha256sum -c", script)
        self.assertIn(nested.MARKER, script)
        self.assertIn(f'path: "{src}/extensions/{{name}}"', script)

    def test_nested_refuses_a_home_the_vm_uses_itself(self):
        self.env["HOME"] = "/home/agent"
        self.assertEqual(self.create("--name", "demo", "--nested"), 1)
        self.assertIn("--nested: /home/agent/wmf-sbx-src is under /home/agent", self.err)

    def test_not_nested_by_default(self):
        self.assertEqual(self.create("--name", "demo", "--memory", "6GiB"), 0, self.err)
        self.assertNotIn("nested", state_mod.load("demo", self.env))
        tmpl = self.lima.templates["wmf-sbx-demo"]
        self.assertEqual((tmpl["memory"], tmpl["disk"]), ("6GiB", template.DEFAULT_DISK))
        self.assertFalse(any("usermod -aG kvm" in " ".join(map(str, c))
                             for c in self.lima.calls))

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

    def hostagent(self, pid="4242"):
        """A Lima home with the instance's host agent pidfile."""
        self.env["LIMA_HOME"] = os.path.join(self.tmp, "lima")
        d = os.path.join(self.env["LIMA_HOME"], "wmf-sbx-sbx-demo")
        os.makedirs(d)
        with open(os.path.join(d, "ha.pid"), "w") as f:
            f.write(pid + "\n")

    def test_stop_refuses_a_host_agent_outside_the_sandbox(self):
        # An earlier nono session started the VM: Landlock gives EPERM,
        # and limactl would wait 3 minutes for nothing (RAN, N3).
        lima = self.sandbox()
        self.hostagent()

        def kill(pid, sig):
            self.assertEqual((pid, sig), (4242, 0))
            raise PermissionError(1, "Operation not permitted")
        with mock.patch.object(vm.os, "kill", kill):
            self.assertEqual(self.quiet(stop.main, ["sbx-demo"], lima=lima, env=self.env), 1)
        self.assertNotIn(("stop", "wmf-sbx-sbx-demo"), lima.calls)
        self.assertIn("pid 4242", self.err)
        self.assertIn("limactl stop wmf-sbx-sbx-demo", self.err)

    def test_stop_goes_ahead_after_a_stale_pidfile(self):
        lima = self.sandbox()
        self.hostagent()

        def kill(pid, sig):
            raise ProcessLookupError(3, "No such process")
        with mock.patch.object(vm.os, "kill", kill):
            self.assertEqual(self.quiet(stop.main, ["sbx-demo"], lima=lima, env=self.env), 0,
                             self.err)
        self.assertIn(("stop", "wmf-sbx-sbx-demo"), lima.calls)

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
                                             "/home/agent"))
        # PATH, then the variables (SANDBOX_NAME, ...), then the command.
        self.assertTrue(argv[i + 8].startswith("PATH="))
        j = argv.index("sh", i + 9)
        self.assertTrue(all("=" in a for a in argv[i + 9:j]), argv[i + 9:j])
        self.assertEqual(argv[j:j + 2], ("sh", "-c"))
        self.assertEqual(argv[-4:], ("sh", "/home/me/src/demo", "git", "status"))

    def test_an_explicit_workdir_is_used_as_is(self):
        lima = self.sandbox()
        self.quiet(exec_mod.main, ["-w", "/srv/x", "sbx-demo", "--", "ls"], lima=lima, env=self.env)
        argv = lima.shells()[-1]
        i = argv.index("--")
        self.assertEqual(argv[i + 6:i + 8], ("-C", "/srv/x"))
        self.assertEqual(argv[-1], "ls")

    def test_exec_gives_the_agent_the_guests_proxy(self):
        # sudo drops the proxy Lima put in the guest's environment; a
        # nested sandbox's inner VM has no other way out (RAN, N3:
        # mw-install-browser got ENETUNREACH).
        lima = self.sandbox()
        env = dict(self.env, HTTPS_PROXY="http://nono:pw@127.0.0.1:50723")
        for args in (["sbx-demo", "--", "ls"], ["-w", "/srv/x", "sbx-demo", "--", "ls"]):
            with self.subTest(args=args):
                self.quiet(exec_mod.main, args, lima=lima, env=env)
                self.assertIn("HTTPS_PROXY=http://nono:pw@192.168.5.2:50723",
                              lima.shells()[-1])

    def test_exec_names_the_sandbox_as_a_session_does(self):
        # bin/git-review-check reads $SANDBOX_NAME (`git safe-reset local`).
        lima = self.sandbox()
        for args in (["sbx-demo", "--", "ls"], ["-w", "/srv/x", "sbx-demo", "--", "ls"]):
            with self.subTest(args=args):
                self.quiet(exec_mod.main, args, lima=lima, env=self.env)
                self.assertIn("SANDBOX_NAME=sbx-demo", lima.shells()[-1])

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

    def test_r_is_accepted_and_a_directory_is_copied_whole(self):
        lima = self.sandbox()
        src = os.path.join(self.tmp, "dir")
        os.makedirs(src)
        for flag in ("-r", "-R", "--recursive", None):
            with self.subTest(flag=flag):
                argv = ([flag] if flag else []) + [src, "sbx-demo:notes"]
                self.assertEqual(self.quiet(cp.main, argv, lima=lima, env=self.env), 0, self.err)
                copy = [c for c in lima.calls if c[0] == "copy"][-1]
                self.assertEqual((copy[1], copy[3]), (src, True))

    def test_at_least_one_side_is_in_a_sandbox(self):
        lima = self.sandbox()
        self.assertEqual(self.quiet(cp.main, ["/a", "/b"], lima=lima, env=self.env), 1)
        self.assertIn("NAME:PATH", self.err)

    def test_sandbox_to_sandbox_goes_through_a_host_temp_dir(self):
        lima = self.sandbox()
        self.sandbox("other", primary="/home/me/src/other", lima=lima)
        steps = []

        def copy_out(name, src, dest, lima):
            steps.append(("out", name, src, dest))
            with open(dest, "w") as f:
                f.write("x")

        def copy_in(name, src, dest, lima):
            steps.append(("in", name, src, dest))
            self.assertTrue(os.path.exists(src))
        with mock.patch.object(cp, "copy_out", copy_out), mock.patch.object(cp, "copy_in", copy_in):
            rc = self.quiet(cp.main, ["sbx-demo:notes.txt", "other:/tmp/x/notes.txt"],
                            lima=lima, env=self.env)
        self.assertEqual(rc, 0, self.err)
        (out, into) = steps
        self.assertEqual(out[1:3], ("sbx-demo", "/home/me/src/demo/notes.txt"))
        self.assertEqual(into[1], "other")
        self.assertEqual((into[2], into[3]), (out[3], "/tmp/x/notes.txt"))
        self.assertFalse(os.path.exists(os.path.dirname(out[3])))   # removed
        self.assertIn("->", self.err)

    def test_the_host_temp_dir_goes_on_failure_too(self):
        lima = self.sandbox()
        self.sandbox("other", primary="/home/me/src/other", lima=lima)
        staged = []

        def copy_out(name, src, dest, lima):
            staged.append(dest)
            open(dest, "w").close()

        def copy_in(name, src, dest, lima):
            raise vm.VmError("boom")
        with mock.patch.object(cp, "copy_out", copy_out), mock.patch.object(cp, "copy_in", copy_in):
            rc = self.quiet(cp.main, ["sbx-demo:/a", "other:/b"], lima=lima, env=self.env)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(os.path.dirname(staged[0])))

    def test_a_relative_path_without_a_workspace_is_in_the_agents_home(self):
        lima = self.sandbox(primary=None)
        src = os.path.join(self.tmp, "f.txt")
        open(src, "w").close()
        self.assertEqual(self.quiet(cp.main, [src, "sbx-demo:x.txt"], lima=lima, env=self.env), 0)
        agent_cp = [s for s in lima.shells() if "cp" in s and "agent" in s][0]
        self.assertEqual(agent_cp[-1], "/home/agent/x.txt")


class CaRefreshTests(Env):
    """A stopped sandbox's start refreshes the extra CA file (a nested
    sandbox's inner VMs get the current session's nono CA)."""

    def test_a_start_refreshes_the_ca_file_a_running_vm_does_not(self):
        calls = []
        with mock.patch.object(image, "extra_ca_files", lambda env=None: calls.append(env) or []):
            lima = self.sandbox(status="Stopped")
            vm.ensure_running("sbx-demo", lima=lima, log=lambda m: None, env=self.env)
            self.assertEqual(len(calls), 1)
            vm.ensure_running("sbx-demo", lima=lima, log=lambda m: None, env=self.env)
            self.assertEqual(len(calls), 1)

    def test_a_refresh_failure_does_not_stop_the_start(self):
        def fail(env=None):
            raise image.ImageError("no such file")
        logs = []
        with mock.patch.object(image, "extra_ca_files", fail):
            lima = self.sandbox(status="Stopped")
            vm.ensure_running("sbx-demo", lima=lima, log=logs.append, env=self.env)
        self.assertIn(("start", "wmf-sbx-sbx-demo"), lima.calls)
        self.assertTrue(any("could not refresh" in m for m in logs))


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

    def test_rm_deletes_the_egress_directory(self):
        lima = self.sandbox()
        egress.write_config("sbx-demo", self.env)
        self.assertEqual(self.quiet(rm.main, ["-f", "sbx-demo"], lima=lima, env=self.env,
                                    confirm=lambda q: "y"), 0, self.err)
        self.assertFalse(os.path.exists(egress.egress_dir("sbx-demo", self.env)))

    def test_declining_keeps_the_egress_directory(self):
        lima = self.sandbox()
        egress.write_config("sbx-demo", self.env)
        self.quiet(rm.main, ["sbx-demo"], lima=lima, env=self.env, confirm=lambda q: "n")
        self.assertTrue(os.path.exists(egress.egress_dir("sbx-demo", self.env)))

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
