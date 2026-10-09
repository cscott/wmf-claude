#!/usr/bin/env python3
"""Unit tests for wmf_sbx/image.py and wmf_sbx/lima.py -- run with:
  python3 -m unittest discover -s sbx/tests -v

No test starts a VM, and none uses the network. A fake `limactl`
records the calls; a fake `run` stands in for git, tar and qemu-img. The
real build was run once in a VM (lima-port/HANDOFF-LIMA.md §11, phase 2).
"""

import copy
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.kit as kit  # noqa: E402
import wmf_sbx.lima as lima  # noqa: E402


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


CLAUDE_SHA = "a" * 64


def fake_fetch(urls=None):
    """A fetch() for claude_release: `stable` is 2.1.286."""
    urls = urls if urls is not None else []

    def fetch(url):
        urls.append(url)
        if url.endswith("/stable"):
            return "2.1.286\n"
        if url.endswith("/2.1.286/manifest.json"):
            return json.dumps({"version": "2.1.286", "platforms": {
                "linux-x64": {"checksum": CLAUDE_SHA},
                "linux-arm64": {"checksum": "b" * 64}}})
        raise AssertionError(f"unexpected URL {url}")
    return fetch


def sample_inputs(**changes):
    inputs = {
        "schema": 1, "arch": "x86_64",
        "agent": {"name": "agent", "uid": 1000, "gid": 1000},
        "base": {"location": "https://example.invalid/debian.qcow2",
                 "arch": "x86_64", "digest": "sha512:" + "c" * 128},
        "packages": ["php", "git"], "nono": "0.78.0",
        "claude": {"version": "2.1.286", "platform": "linux-x64", "sha256": CLAUDE_SHA},
        "tree": "0123456789ab", "helpers": {"git-safe-reset": "d" * 64},
        "build_script": "e" * 64,
    }
    inputs.update(changes)
    return inputs


class TempEnv(unittest.TestCase):
    """XDG_CACHE_HOME and XDG_STATE_HOME in a temporary directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.env = {"XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                    "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "LIMA_HOME": os.path.join(self.tmp, "lima")}

    def _cleanup(self):
        for dirpath, _dirs, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(dirpath, f), 0o644)
        shutil.rmtree(self.tmp)

    def make_entry(self, key, created="2026-10-09T00:00:00+00:00", payload=b"qcow2"):
        d = image.entry_dir(key, self.env)
        os.makedirs(d)
        g = os.path.join(d, "golden.qcow2")
        with open(g, "wb") as f:
            f.write(payload)
        os.chmod(g, 0o444)
        with open(os.path.join(d, "golden.sha256"), "w") as f:
            f.write(image._sha256_file(g) + "  golden.qcow2\n")
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"key": key, "created": created, "inputs": sample_inputs()}, f)
        return d

    def use_image(self, sandbox, key):
        sdir = os.path.join(self.env["XDG_STATE_HOME"], "wmf-sbx", "sandboxes")
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, f"{sandbox}.json"), "w") as f:
            json.dump({"name": sandbox, "image": key}, f)


# -- inputs and the key ---------------------------------------------------

class InputTests(unittest.TestCase):

    def test_the_base_image_comes_from_kostas_template_with_a_digest(self):
        for arch in ("x86_64", "aarch64"):
            base = image.base_image(arch)
            self.assertEqual(base["arch"], arch)
            self.assertTrue(base["digest"].startswith("sha512:"), base)
            self.assertRegex(base["location"], r"^https://cloud\.debian\.org/images/cloud/trixie/\d")
            self.assertNotRegex(base["location"], r"latest|daily")

    def test_a_template_image_without_a_digest_is_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("images:\n- location: https://x/y.qcow2\n  arch: x86_64\n")
        self.addCleanup(os.unlink, f.name)
        with self.assertRaisesRegex(image.ImageError, "no digest"):
            image.base_image("x86_64", template=f.name)

    def test_claude_release_takes_stable_and_the_manifest_checksum(self):
        urls = []
        version, sha = image.claude_release("x86_64", fetch=fake_fetch(urls))
        self.assertEqual((version, sha), ("2.1.286", CLAUDE_SHA))
        self.assertEqual(urls[0], image.CLAUDE_RELEASES + "/stable")

    def test_claude_release_refuses_a_page_that_is_not_a_version(self):
        with self.assertRaisesRegex(image.ImageError, "unexpected Claude Code version"):
            image.claude_release("x86_64", fetch=lambda url: "<html>error</html>")

    def test_claude_release_refuses_a_manifest_without_the_platform(self):
        def fetch(url):
            return "2.1.286" if url.endswith("/stable") else json.dumps({"platforms": {}})
        with self.assertRaisesRegex(image.ImageError, "no checksum"):
            image.claude_release("x86_64", fetch=fetch)

    def test_the_packages_extend_the_kit_and_add_no_docker(self):
        for p in kit.BASE_PACKAGES:
            self.assertIn(p, image.IMAGE_PACKAGES)
        for p in ("nodejs", "npm", "git", "jq", "curl", "python3-venv", "nftables"):
            self.assertIn(p, image.IMAGE_PACKAGES)
        self.assertFalse([p for p in image.IMAGE_PACKAGES if "docker" in p])
        self.assertEqual(len(image.IMAGE_PACKAGES), len(set(image.IMAGE_PACKAGES)))

    def test_the_helpers_are_the_kits_helpers(self):
        self.assertEqual(set(image.helper_files()), set(kit.HELPER_SCRIPTS))
        for path in image.helper_files().values():
            self.assertTrue(os.path.isfile(path), path)

    def test_image_inputs_has_every_key_input(self):
        def run(argv, **kw):
            self.assertEqual(argv[:2], ["git", "-C"])
            return Done(stdout="0123456789ab\n")
        inputs = image.image_inputs(arch="x86_64", fetch=fake_fetch(), run=run,
                                    uid=30033, gid=30033)
        self.assertEqual(set(inputs), {"schema", "arch", "agent", "base", "packages", "nono",
                                       "claude", "tree", "helpers", "build_script"})
        self.assertEqual(inputs["agent"], {"name": "agent", "uid": 30033, "gid": 30033})
        self.assertEqual(inputs["nono"], image.nono_version())
        self.assertEqual(inputs["tree"], "0123456789ab")
        self.assertEqual(inputs["claude"]["platform"], "linux-x64")

    def test_the_key_is_stable_and_changes_with_every_input(self):
        base = sample_inputs()
        key = image.cache_key(base)
        self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.assertEqual(key, image.cache_key(copy.deepcopy(base)))
        changes = {
            "arch": "aarch64", "packages": ["php"], "nono": "0.79.0",
            "tree": "ba9876543210", "build_script": "f" * 64,
            "base": dict(base["base"], digest="sha512:" + "0" * 128),
            "claude": dict(base["claude"], version="2.1.287"),
            "helpers": {"git-safe-reset": "0" * 64},
            "agent": dict(base["agent"], uid=501),
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(image.cache_key(sample_inputs(**{field: value})), key)


# -- the builder template and the build script ----------------------------

class BuilderTests(unittest.TestCase):

    def test_the_builder_is_closed_like_kostas_template(self):
        t = image.builder_template(sample_inputs())
        self.assertIs(t["plain"], True)
        self.assertEqual(t["mounts"], [])
        self.assertEqual(t["containerd"], {"system": False, "user": False})
        self.assertIn({"guestIP": "0.0.0.0", "proto": "any", "ignore": True}, t["portForwards"])
        self.assertFalse(any(t["ssh"].values()))
        for key in ("rosetta", "vmOpts", "networks", "vmType"):
            self.assertNotIn(key, t)

    def test_the_builder_boots_the_pinned_image(self):
        inputs = sample_inputs()
        t = image.builder_template(inputs)
        self.assertEqual(t["images"], [inputs["base"]])

    def test_the_builder_user_has_its_own_uid_not_the_hosts(self):
        t = image.builder_template(sample_inputs())
        self.assertEqual(t["user"]["name"], image.BUILDER_USER)
        self.assertEqual(t["user"]["uid"], image.BUILDER_UID)

    def test_the_agent_ids_refuse_root_and_the_builder_uid(self):
        self.assertEqual(image.agent_ids(1000, 1000), {"name": "agent", "uid": 1000, "gid": 1000})
        self.assertEqual(image.agent_ids(501, 20)["gid"], 20)
        for uid, gid in ((0, 1000), (1000, 0), (image.BUILDER_UID, 1000)):
            with self.subTest(uid=uid, gid=gid), self.assertRaises(image.ImageError):
                image.agent_ids(uid, gid)

    def test_the_build_script_makes_the_agent_after_removing_the_builder(self):
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        made = script.index('useradd -m -u "$WMF_SBX_AGENT_UID" -g "$WMF_SBX_AGENT_GID"')
        self.assertGreater(made, script.index('userdel -r -f "$WMF_SBX_BUILDER_USER"'))
        self.assertNotRegex(script[made:], r"usermod[^\n]*agent|sudo")

    def test_the_template_is_valid_for_limactl(self):
        if not shutil.which("limactl") or os.geteuid() == 0:
            self.skipTest("needs limactl and a non-root user")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            image.yaml.safe_dump(image.builder_template(sample_inputs()), f)
        self.addCleanup(os.unlink, f.name)
        subprocess.run(["limactl", "validate", f.name], check=True, capture_output=True)

    def test_build_env_names_every_variable_the_script_requires(self):
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        required = set(re.findall(r'"\$\{(WMF_SBX_[A-Z0-9_]+):\?\}"', script))
        self.assertTrue(required)
        self.assertEqual(required, set(image.build_env(sample_inputs(), "/tmp/x")))

    def test_the_build_script_parses(self):
        subprocess.run(["bash", "-n", image.BUILD_SCRIPT], check=True)

    def test_the_build_script_checks_what_it_downloads(self):
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        self.assertIn("SHA256SUMS.txt", script)
        self.assertIn('"$WMF_SBX_CLAUDE_SHA256  $CC_DIR/claude" | sha256sum -c --strict', script)
        self.assertNotRegex(script, r"curl[^\n]*\|\s*(ba)?sh")

    def test_the_build_script_seals_the_identity_last(self):
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        seal = script.index('step "seal"')
        for needle in ("rm -f /etc/ssh/ssh_host_*",
                       "cloud-init clean --logs --seed --machine-id",
                       'userdel -r -f "$WMF_SBX_BUILDER_USER"'):
            self.assertGreater(script.index(needle), seal, needle)
        self.assertGreater(seal, script.index('step "helpers"'))

    def test_no_extra_ca_means_no_cacerts_key(self):
        self.assertEqual(image.extra_ca_files({}), [])
        self.assertNotIn("caCerts", image.builder_template(sample_inputs()))

    def test_extra_ca_files_go_to_the_builder_and_not_into_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.crt"), os.path.join(d, "b.crt")
            for f in (a, b):
                open(f, "w").close()
            files = image.extra_ca_files({image.CA_CERTS_VAR: f"{a}:{b}"})
            self.assertEqual(files, [a, b])
            t = image.builder_template(sample_inputs(), files)
            self.assertEqual(t["caCerts"], {"files": [a, b]})
            with self.assertRaisesRegex(image.ImageError, "no such file"):
                image.extra_ca_files({image.CA_CERTS_VAR: os.path.join(d, "missing.crt")})

    def test_the_build_script_removes_the_build_time_cas_when_it_seals(self):
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        seal = script.index('step "seal"')
        self.assertGreater(script.index("rm -f /usr/local/share/ca-certificates/cloud-init-ca-cert-*.crt"), seal)
        self.assertGreater(script.index("update-ca-certificates --fresh"), seal)

    def test_proxies_on_loopback_are_rewritten_for_the_guest(self):
        env = {"https_proxy": "http://127.0.0.1:3128", "HTTP_PROXY": "http://localhost:8080/",
               "no_proxy": "localhost,127.0.0.1", "HOME": "/x",
               "http_proxy": "http://proxy.example:3128"}
        out = image.guest_proxy_env(env)
        self.assertEqual(out["https_proxy"], "http://192.168.5.2:3128")
        self.assertEqual(out["HTTP_PROXY"], "http://192.168.5.2:8080/")
        self.assertEqual(out["http_proxy"], "http://proxy.example:3128")
        self.assertEqual(out["no_proxy"], "localhost,127.0.0.1")
        self.assertNotIn("HOME", out)

    def test_no_proxy_means_no_variables(self):
        self.assertEqual(image.guest_proxy_env({"HOME": "/x"}), {})


class ExportCheckTests(unittest.TestCase):

    def test_a_plain_qcow2_passes(self):
        image.check_exported({"format": "qcow2", "format-specific": {"data": {}}})

    def test_raw_is_refused(self):
        with self.assertRaisesRegex(image.ImageError, "not qcow2"):
            image.check_exported({"format": "raw"})

    def test_a_backing_file_is_refused(self):
        for k in ("backing-filename", "full-backing-filename"):
            with self.subTest(k=k), self.assertRaisesRegex(image.ImageError, "backing file"):
                image.check_exported({"format": "qcow2", k: "/srv/base.qcow2"})

    def test_an_external_data_file_is_refused(self):
        with self.assertRaisesRegex(image.ImageError, "external data file"):
            image.check_exported({"format": "qcow2",
                                  "format-specific": {"data": {"data-file": "/x"}}})


# -- the cache ------------------------------------------------------------

class CacheTests(TempEnv):

    def test_cache_root_follows_xdg_cache_home_and_is_absolute(self):
        root = image.cache_root(self.env)
        self.assertTrue(os.path.isabs(root))
        self.assertTrue(root.endswith("/cache/wmf-sbx/images"))

    def test_a_key_that_is_not_16_hex_digits_is_refused(self):
        for bad in ("", "../x", "0123456789abcdeG", "0123456789abcdef0"):
            with self.subTest(bad=bad), self.assertRaises(image.ImageError):
                image.entry_dir(bad, self.env)

    def test_list_shows_complete_entries_oldest_first(self):
        self.make_entry("bbbbbbbbbbbbbbbb", created="2026-10-09T02:00:00+00:00")
        self.make_entry("aaaaaaaaaaaaaaaa", created="2026-10-09T01:00:00+00:00")
        os.makedirs(os.path.join(image.cache_root(self.env), ".build-cccc.x"))
        os.makedirs(os.path.join(image.cache_root(self.env), "dddddddddddddddd"))
        self.assertEqual([k for k, _ in image.list_images(self.env)],
                         ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])

    def test_verify_accepts_a_good_entry(self):
        self.make_entry("aaaaaaaaaaaaaaaa")
        image.verify_entry("aaaaaaaaaaaaaaaa", self.env)

    def test_verify_refuses_a_writable_or_changed_image(self):
        d = self.make_entry("aaaaaaaaaaaaaaaa")
        g = os.path.join(d, "golden.qcow2")
        os.chmod(g, 0o644)
        with self.assertRaisesRegex(image.ImageError, "writable"):
            image.verify_entry("aaaaaaaaaaaaaaaa", self.env)
        with open(g, "ab") as f:
            f.write(b"x")
        os.chmod(g, 0o444)
        with self.assertRaisesRegex(image.ImageError, "does not match"):
            image.verify_entry("aaaaaaaaaaaaaaaa", self.env)

    def test_rm_refuses_an_image_that_a_sandbox_uses(self):
        self.make_entry("aaaaaaaaaaaaaaaa")
        self.use_image("wiki1", "aaaaaaaaaaaaaaaa")
        with self.assertRaisesRegex(image.ImageError, "in use by: wiki1"):
            image.remove_image("aaaaaaaaaaaaaaaa", self.env)
        self.assertTrue(os.path.isdir(image.entry_dir("aaaaaaaaaaaaaaaa", self.env)))

    def test_rm_removes_an_unused_read_only_entry(self):
        self.make_entry("aaaaaaaaaaaaaaaa")
        image.remove_image("aaaaaaaaaaaaaaaa", self.env)
        self.assertFalse(os.path.exists(image.entry_dir("aaaaaaaaaaaaaaaa", self.env)))

    def test_prune_keeps_used_and_kept_images(self):
        for k in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"):
            self.make_entry(k)
        self.use_image("wiki1", "aaaaaaaaaaaaaaaa")
        gone = image.prune_images(keep={"bbbbbbbbbbbbbbbb"}, env=self.env)
        self.assertEqual(gone, ["cccccccccccccccc"])
        self.assertEqual({k for k, _ in image.list_images(self.env)},
                         {"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"})

    def test_a_broken_state_file_does_not_stop_rm(self):
        self.make_entry("aaaaaaaaaaaaaaaa")
        sdir = os.path.join(self.env["XDG_STATE_HOME"], "wmf-sbx", "sandboxes")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "bad.json"), "w") as f:
            f.write("{not json")
        image.remove_image("aaaaaaaaaaaaaaaa", self.env)


# -- the build sequence ---------------------------------------------------

class FakeLima:
    """Records the calls; `image-build.sh` exits with `build_rc`."""

    def __init__(self, env, existing=(), build_rc=0):
        self.env = env
        self.calls = []
        self.names = set(existing)
        self.build_rc = build_rc

    def exists(self, name):
        self.calls.append(("exists", name))
        return name in self.names

    def delete(self, name):
        self.calls.append(("delete", name))
        self.names.discard(name)

    def validate(self, path):
        self.calls.append(("validate", os.path.basename(path)))
        with open(path) as f:
            self.template = image.yaml.safe_load(f)

    def create(self, name, path):
        self.calls.append(("create", name))
        self.names.add(name)

    def start(self, name, timeout=None):
        self.calls.append(("start", name, timeout))

    def shell(self, name, argv, **kw):
        self.calls.append(("shell", name, tuple(argv)))
        return Done()

    def copy(self, src, dst, recursive=False):
        self.calls.append(("copy", os.path.basename(src), dst, recursive))

    def call(self, *args, check=True, capture=True, **kw):
        self.calls.append(("call",) + args)
        return Done(returncode=self.build_rc)

    def stop(self, name):
        self.calls.append(("stop", name))

    def disk_path(self, name, env=None):
        return os.path.join(self.env["LIMA_HOME"], name, "disk")


class BuildTests(TempEnv):

    def setUp(self):
        super().setUp()
        self.inputs = sample_inputs()
        self.key = image.cache_key(self.inputs)
        self.runs = []
        p = mock.patch.object(image, "stage_dir", self.fake_stage)
        p.start()
        self.addCleanup(p.stop)

    def fake_stage(self, dest, inputs, key, run=None, root=None):
        for f in ("tree.tgz", "image.json", "image-build.sh"):
            open(os.path.join(dest, f), "w").close()
        os.makedirs(os.path.join(dest, "helpers"))

    def fake_run(self, argv, **kw):
        self.runs.append(list(argv))
        if argv[:2] == ["qemu-img", "convert"]:
            with open(argv[-1], "wb") as f:
                f.write(b"exported")
            return Done()
        if argv[:2] == ["qemu-img", "info"]:
            return Done(stdout=json.dumps({"format": "qcow2", "virtual-size": 21474836480,
                                           "format-specific": {"data": {}}}))
        raise AssertionError(f"unexpected command {argv}")

    def build(self, fake, **kw):
        return image.build(self.inputs, lima=fake, run=self.fake_run, env=self.env,
                           log=lambda m: None, **kw)

    def test_a_good_build_makes_a_read_only_verified_entry_and_deletes_the_builder(self):
        fake = FakeLima(self.env)
        self.assertEqual(self.build(fake), self.key)
        g = image.golden_path(self.key, self.env)
        self.assertEqual(stat.S_IMODE(os.stat(g).st_mode), 0o444)
        image.verify_entry(self.key, self.env)
        with open(os.path.join(image.entry_dir(self.key, self.env), "manifest.json")) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["inputs"], self.inputs)
        self.assertEqual(manifest["key"], self.key)
        name = image.builder_name(self.key)
        self.assertEqual(fake.calls[-1], ("delete", name))
        self.assertEqual(fake.template["images"], [self.inputs["base"]])

    def test_the_steps_run_in_order(self):
        fake = FakeLima(self.env)
        self.build(fake)
        order = [c[0] for c in fake.calls if c[0] in ("create", "start", "copy", "call", "stop", "delete")]
        self.assertEqual(order[:2], ["create", "start"])
        self.assertLess(order.index("copy"), order.index("call"))
        self.assertLess(order.index("call"), order.index("stop"))
        self.assertEqual(order[-1], "delete")
        convert = [r for r in self.runs if r[:2] == ["qemu-img", "convert"]][0]
        self.assertEqual(convert[2:4], ["-O", "qcow2"])
        self.assertEqual(convert[4], fake.disk_path(image.builder_name(self.key)))

    def test_the_build_script_runs_as_root_with_its_variables_and_the_proxy(self):
        self.env["https_proxy"] = "http://127.0.0.1:3128"
        fake = FakeLima(self.env)
        self.build(fake)
        call = [c for c in fake.calls if c[0] == "call"][0]
        argv = list(call[call.index("--") + 1:])
        self.assertEqual(argv[:2], ["sudo", "env"])
        self.assertEqual(argv[-2], "bash")
        self.assertTrue(argv[-1].endswith("/image-build.sh"))
        self.assertIn("https_proxy=http://192.168.5.2:3128", argv)
        for k, v in image.build_env(self.inputs, argv[-1].rsplit("/", 1)[0]).items():
            self.assertIn(f"{k}={v}", argv)

    def test_directories_are_copied_recursively(self):
        fake = FakeLima(self.env)
        self.build(fake)
        copies = {c[1]: c[3] for c in fake.calls if c[0] == "copy"}
        self.assertEqual(copies, {"helpers": True, "image-build.sh": False,
                                  "image.json": False, "tree.tgz": False})

    def test_a_cached_image_starts_no_vm(self):
        fake = FakeLima(self.env)
        self.build(fake)
        again = FakeLima(self.env)
        self.assertEqual(self.build(again), self.key)
        self.assertEqual(again.calls, [])

    def test_a_failed_build_keeps_the_builder_and_leaves_no_entry(self):
        fake = FakeLima(self.env, build_rc=1)
        with self.assertRaisesRegex(image.ImageError, "builder is kept"):
            self.build(fake)
        self.assertNotIn(("delete", image.builder_name(self.key)), fake.calls)
        self.assertEqual(image.list_images(self.env), [])
        self.assertEqual([n for n in os.listdir(image.cache_root(self.env))], [])

    def test_an_export_with_a_backing_file_is_not_cached(self):
        def run(argv, **kw):
            if argv[:2] == ["qemu-img", "info"]:
                return Done(stdout=json.dumps({"format": "qcow2",
                                               "backing-filename": "/elsewhere"}))
            return self.fake_run(argv, **kw)
        with self.assertRaisesRegex(image.ImageError, "backing file"):
            image.build(self.inputs, lima=FakeLima(self.env), run=run, env=self.env,
                        log=lambda m: None)
        self.assertEqual(image.list_images(self.env), [])

    def test_a_builder_left_by_an_earlier_build_is_deleted_first(self):
        name = image.builder_name(self.key)
        fake = FakeLima(self.env, existing=[name])
        self.build(fake)
        self.assertEqual(fake.calls[:2], [("exists", name), ("delete", name)])

    def test_keep_builder(self):
        fake = FakeLima(self.env)
        self.build(fake, keep_builder=True)
        self.assertNotIn(("delete", image.builder_name(self.key)), fake.calls)


# -- the limactl wrapper --------------------------------------------------

class LimactlTests(unittest.TestCase):

    def setUp(self):
        self.argvs = []

    def runner(self, result=None):
        def run(argv, **kw):
            self.argvs.append(argv)
            return result or Done()
        return run

    def test_instances_reads_one_json_object_per_line(self):
        out = '{"name":"a","status":"Running"}\n{"name":"b","status":"Stopped"}\n'
        l = lima.Limactl(run=self.runner(Done(stdout=out)))
        self.assertEqual(l.instances(), {"a": "Running", "b": "Stopped"})
        self.assertEqual(self.argvs[0], ["limactl", "list", "--json"])

    def test_the_argv_of_each_operation(self):
        l = lima.Limactl(run=self.runner())
        l.create("vm", "/t.yaml")
        l.start("vm", timeout="30m")
        l.shell("vm", ["sudo", "true"], workdir="/home/x")
        l.copy("/a", "vm:/b", recursive=True)
        l.delete("vm")
        self.assertEqual(self.argvs, [
            ["limactl", "create", "--tty=false", "--name=vm", "/t.yaml"],
            ["limactl", "start", "--tty=false", "--timeout=30m", "vm"],
            ["limactl", "shell", "--workdir=/home/x", "vm", "--", "sudo", "true"],
            ["limactl", "copy", "-r", "/a", "vm:/b"],
            ["limactl", "delete", "--force", "vm"],
        ])

    def test_a_failure_raises_with_stderr(self):
        l = lima.Limactl(run=self.runner(Done(returncode=1, stderr="boom")))
        with self.assertRaisesRegex(lima.LimaError, "exit 1.*boom"):
            l.stop("vm")

    def test_the_disk_is_called_disk(self):
        l = lima.Limactl()
        self.assertEqual(l.disk_path("vm", env={"LIMA_HOME": "/L"}), "/L/vm/disk")


if __name__ == "__main__":
    unittest.main()
