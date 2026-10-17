#!/usr/bin/env python3
"""Unit tests for wmf_sbx/image.py and wmf_sbx/lima.py -- run with:
  python3 -m unittest discover -s sbx/tests -v

No test starts a VM, and none uses the network. A fake `limactl`
records the calls; a fake `run` stands in for git, tar and qemu-img.
"""

import copy
import datetime
import io
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
import isolation  # noqa: E402
from fakes import Done  # noqa: E402


# A nested sandbox has a local base image (image.LOCAL_BASE_DIR); the
# tests must not see it.
_NO_LOCAL_BASE = mock.patch.object(image, "LOCAL_BASE_DIR", "/nonexistent/wmf-sbx-base")


def setUpModule():
    isolation.isolate()
    _NO_LOCAL_BASE.start()
    unittest.addModuleCleanup(_NO_LOCAL_BASE.stop)


CLAUDE_SHA = "a" * 64
NODE_SHA = "9" * 64
NODE_SUMS = (f"{'1' * 64}  node-v{image.NODE_VERSION}-darwin-arm64.tar.gz\n"
             f"{NODE_SHA}  node-v{image.NODE_VERSION}-linux-x64.tar.xz\n"
             f"{'2' * 64}  node-v{image.NODE_VERSION}-linux-x64.tar.gz\n"
             f"{'3' * 64}  node-v{image.NODE_VERSION}-linux-arm64.tar.xz\n")


def fake_fetch(urls=None):
    """A fetch() for claude_release: `stable` is 2.1.286."""
    urls = urls if urls is not None else []

    def fetch(url):
        urls.append(url)
        if url.endswith("/stable"):
            return "2.1.286\n"
        if url == f"{image.NODE_DIST}/v{image.NODE_VERSION}/SHASUMS256.txt":
            return NODE_SUMS
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
        "node": {"version": "24.18.0", "file": "node-v24.18.0-linux-x64.tar.xz",
                 "sha256": NODE_SHA},
        "php_ast": dict(image.PHP_AST),
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
            f.write(image.file_digest(g) + "  golden.qcow2\n")
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

    def test_the_base_image_comes_from_the_upstream_template_with_a_digest(self):
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
        version, sha = image.claude_release("x86_64", fetch=fake_fetch(urls), env={})
        self.assertEqual((version, sha), ("2.1.286", CLAUDE_SHA))
        self.assertEqual(urls[0], image.CLAUDE_RELEASES + "/stable")

    def test_claude_release_follows_wmf_sbx_claude_version(self):
        def fetch(url):
            if url.endswith("/latest"):
                return "2.1.296\n"
            if url.endswith("/manifest.json"):
                version = url.rsplit("/", 2)[-2]
                return json.dumps({"platforms": {"linux-x64": {"checksum": version[-1] * 64}}})
            raise AssertionError(f"unexpected URL {url}")
        var = image.CLAUDE_VERSION_VAR
        self.assertEqual(image.claude_release("x86_64", fetch=fetch, env={var: "latest"}),
                         ("2.1.296", "6" * 64))
        self.assertEqual(image.claude_release("x86_64", fetch=fetch, env={var: "2.1.280"}),
                         ("2.1.280", "0" * 64))
        self.assertEqual(image.claude_release("x86_64", fetch=fake_fetch(), env={var: ""})[0],
                         "2.1.286")
        for bad in ("beta", "2.1", "2.1.280; rm -rf /"):
            with self.assertRaisesRegex(image.ImageError, var):
                image.claude_release("x86_64", fetch=fetch, env={var: bad})

    def test_claude_release_refuses_a_page_that_is_not_a_version(self):
        with self.assertRaisesRegex(image.ImageError, "unexpected Claude Code version"):
            image.claude_release("x86_64", fetch=lambda url: "<html>error</html>")

    def test_claude_release_refuses_a_manifest_without_the_platform(self):
        def fetch(url):
            return "2.1.286" if url.endswith("/stable") else json.dumps({"platforms": {}})
        with self.assertRaisesRegex(image.ImageError, "no checksum"):
            image.claude_release("x86_64", fetch=fetch, env={})

    def test_the_packages_extend_the_kit_and_add_no_docker(self):
        for p in kit.BASE_PACKAGES:
            self.assertIn(p, image.IMAGE_PACKAGES)
        for p in ("git", "jq", "curl", "python3-venv", "nftables", "xz-utils", "time", "bc"):
            self.assertIn(p, image.IMAGE_PACKAGES)
        # Node comes from nodejs.org (NODE_VERSION), never from Debian.
        for p in ("nodejs", "npm"):
            self.assertNotIn(p, image.IMAGE_PACKAGES)
        # php-ast is built from source (PHP_AST): Debian's is too old for Phan 6.
        self.assertNotIn("php-ast", image.IMAGE_PACKAGES)
        self.assertEqual(image.build_env(sample_inputs(), "/s")["WMF_SBX_PHP_AST_COMMIT"],
                         image.PHP_AST["commit"])
        self.assertFalse([p for p in image.IMAGE_PACKAGES if "docker" in p])
        self.assertEqual(len(image.IMAGE_PACKAGES), len(set(image.IMAGE_PACKAGES)))

    def test_node_release_takes_the_tarball_checksum_for_the_arch(self):
        urls = []
        self.assertEqual(image.node_release("x86_64", fetch=fake_fetch(urls)), {
            "version": image.NODE_VERSION,
            "file": f"node-v{image.NODE_VERSION}-linux-x64.tar.xz", "sha256": NODE_SHA})
        self.assertEqual(urls, [f"{image.NODE_DIST}/v{image.NODE_VERSION}/SHASUMS256.txt"])
        self.assertEqual(image.node_release("aarch64", fetch=fake_fetch())["sha256"], "3" * 64)

    def test_node_release_refuses_sums_without_the_tarball(self):
        with self.assertRaisesRegex(image.ImageError, "no checksum for node-v"):
            image.node_release("x86_64", fetch=lambda url: "<html>not found</html>")

    def test_node_matches_wmf_ci(self):
        # WMF CI's images are node24-* at 24.18.0; change both together.
        self.assertEqual(image.NODE_VERSION, "24.18.0")

    def test_the_helpers_are_the_kits_helpers(self):
        self.assertEqual(set(image.helper_files()), set(kit.HELPER_SCRIPTS))
        for path in image.helper_files().values():
            self.assertTrue(os.path.isfile(path), path)

    def test_image_inputs_has_every_key_input(self):
        def run(argv, **kw):
            self.assertEqual(argv[:2], ["git", "-C"])
            return Done(stdout="0123456789ab\n")
        inputs = image.image_inputs(arch="x86_64", fetch=fake_fetch(), run=run,
                                    uid=30033, gid=30033, env={})
        self.assertEqual(set(inputs), {"schema", "arch", "agent", "base", "packages", "nono",
                                       "node", "php_ast", "claude", "tree", "helpers",
                                   "build_script"})
        self.assertEqual(inputs["node"]["sha256"], NODE_SHA)
        self.assertEqual(inputs["agent"], {"name": "agent", "uid": 30033, "gid": 30033})
        self.assertEqual(inputs["nono"], image.nono_version())
        self.assertEqual(inputs["tree"], "0123456789ab")
        self.assertEqual(inputs["claude"]["platform"], "linux-x64")

    def test_the_override_replaces_the_head_commit(self):
        def run(argv, **kw):
            raise AssertionError("git must not run")
        env = {image.TREE_OVERRIDE_VAR: "v2"}
        self.assertEqual(image.tree_revision(run=run, env=env), "v2")
        for bad in ("../x", "v 2", "-v2", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(image.ImageError):
                image.tree_revision(run=run, env={image.TREE_OVERRIDE_VAR: bad})

    def test_without_the_override_the_key_has_the_head_commit(self):
        def run(argv, **kw):
            return Done(stdout="0123456789ab\n")
        self.assertEqual(image.tree_revision(run=run, env={image.TREE_OVERRIDE_VAR: " "}),
                         "0123456789ab")

    def test_latest_keys_on_the_head_commit_when_it_builds(self):
        env = {image.TREE_OVERRIDE_VAR: "latest"}
        self.assertTrue(image.wants_latest(env))
        self.assertFalse(image.wants_latest({image.TREE_OVERRIDE_VAR: "v1"}))
        self.assertEqual(image.tree_revision(run=lambda a, **k: Done(stdout="abc123\n"),
                                             env=env), "abc123")

    def test_extra_packages_extend_the_list_and_the_key(self):
        self.assertEqual(image.image_packages(), list(image.IMAGE_PACKAGES))
        extra = image.image_packages(["nano", "nano", image.IMAGE_PACKAGES[0]])
        self.assertEqual(extra, list(image.IMAGE_PACKAGES) + ["nano"])
        for bad in ("Nano", "nano;rm", "", 3):
            with self.subTest(bad=bad), self.assertRaises(image.ImageError):
                image.image_packages([bad])

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
            "node": dict(base["node"], sha256="8" * 64),
            "helpers": {"git-safe-reset": "0" * 64},
            "agent": dict(base["agent"], uid=501),
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(image.cache_key(sample_inputs(**{field: value})), key)


# -- the builder template and the build script ----------------------------

class BuilderTests(unittest.TestCase):

    def test_the_builder_is_closed_like_the_upstream_template(self):
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

    def test_the_builder_boots_a_local_copy_of_the_base_image_when_there_is_one(self):
        inputs = sample_inputs()
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, os.path.basename(inputs["base"]["location"]))
            open(local, "w").close()
            t = image.builder_template(inputs, base_dir=d)
        self.assertEqual(t["images"], [dict(inputs["base"], location=local)])
        self.assertEqual(image.cache_key(inputs), image.cache_key(sample_inputs()))

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
        # limactl takes an instance name from the file name, and a
        # tempfile name ending in "_" or holding "__" is not a valid one.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "lima.yaml")
        with open(path, "w", encoding="utf-8") as f:
            image.yaml.safe_dump(image.builder_template(sample_inputs()), f)
        res = subprocess.run(["limactl", "validate", path], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_build_env_names_every_variable_the_script_requires(self):
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        required = set(re.findall(r'"\$\{(WMF_SBX_[A-Z0-9_]+):\?\}"', script))
        self.assertTrue(required)
        self.assertEqual(required, set(image.build_env(sample_inputs(), "/tmp/x")))

    def test_the_build_script_masks_apts_timers(self):
        # apt-listchanges's changelog fetches filled the egress log.
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        loop = next(l for l in script.splitlines() if l.startswith("for t in apt-"))
        for timer in ("apt-listchanges.timer", "apt-daily.timer", "apt-daily-upgrade.timer"):
            self.assertIn(timer, loop)
        self.assertIn('  systemctl mask "$t"', script)

    def test_the_build_script_parses(self):
        subprocess.run(["bash", "-n", image.BUILD_SCRIPT], check=True)

    def test_the_build_script_checks_what_it_downloads(self):
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        self.assertIn("SHA256SUMS.txt", script)
        self.assertIn('"$WMF_SBX_CLAUDE_SHA256  $CC_DIR/claude" | sha256sum -c --strict', script)
        self.assertNotRegex(script, r"curl[^\n]*\|\s*(ba)?sh")

    def test_the_build_script_makes_the_cypress_dirs_for_the_agent(self):
        # nono grants only paths that exist (session.CYPRESS_GRANTS).
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        useradd = script.index("useradd ")
        for d in ("/home/agent/.config/Cypress", "/var/lib/xkb"):
            line = next(l for l in script.splitlines() if l.startswith("install -d") and d in l)
            self.assertIn("-o agent", line)
            self.assertGreater(script.index(line), useradd)

    def test_the_build_script_seals_the_identity_last(self):
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        seal = script.index('step "seal"')
        for needle in ("rm -f /etc/ssh/ssh_host_*",
                       "cloud-init clean --logs --seed --machine-id",
                       'userdel -r -f "$WMF_SBX_BUILDER_USER"'):
            self.assertGreater(script.index(needle), seal, needle)
        self.assertGreater(seal, script.index('step "helpers"'))

    def test_a_sudo_nested_sandbox_gives_the_inner_vms_its_own_cas(self):
        # No session nono, so no SSL_CERT_FILE: the CAs Lima put in the
        # guest (the egress CA, and the host's extra ones) are the ones.
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "nested")
            open(marker, "w").close()
            cas = [os.path.join(d, f"cloud-init-ca-cert-{i}.crt") for i in (2, 1)]
            for ca in cas:
                open(ca, "w").close()
            glob_ = os.path.join(d, "cloud-init-ca-cert-*.crt")
            with mock.patch.multiple(image, NESTED_MARKER=marker, GUEST_CA_GLOB=glob_):
                self.assertEqual(image.extra_ca_files({}), sorted(cas))
                self.assertEqual(image.extra_ca_files(
                    {"SSL_CERT_FILE": os.path.join(d, "missing")}), sorted(cas))
                self.assertEqual(image.extra_ca_files({image.CA_CERTS_VAR: ""}), [])

    def test_no_extra_ca_means_no_cacerts_key(self):
        self.assertEqual(image.extra_ca_files({}), [])
        self.assertNotIn("caCerts", image.builder_template(sample_inputs()))

    @staticmethod
    def pem(body):
        return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"

    def test_a_nested_sandbox_gives_the_inner_vms_only_nonos_ca(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "nested")
            system = os.path.join(d, "system.crt")
            bundle = os.path.join(d, "bundle.pem")
            with open(system, "w") as f:
                f.write(self.pem("U1lTVEVNMQ==") + self.pem("U1lTVEVNMg=="))
            with open(bundle, "w") as f:
                f.write(self.pem("U1lTVEVNMQ==") + "\n" + self.pem("Tk9OTw==") + self.pem("U1lTVEVNMg=="))
            env = {"SSL_CERT_FILE": bundle, "XDG_CACHE_HOME": os.path.join(d, "cache")}
            self.assertEqual(image.extra_ca_files(env), [])
            open(marker, "w").close()
            with mock.patch.multiple(image, NESTED_MARKER=marker, SYSTEM_CA_BUNDLE=system):
                files = image.extra_ca_files(env)
                self.assertEqual(files, [os.path.join(d, "cache", "wmf-sbx", "session-ca.pem")])
                with open(files[0]) as f:
                    self.assertEqual(f.read(), self.pem("Tk9OTw=="))
                # A later session's CA replaces it, at the same path.
                with open(bundle, "w") as f:
                    f.write(self.pem("U1lTVEVNMQ==") + self.pem("TkVX"))
                self.assertEqual(image.extra_ca_files(env), files)
                with open(files[0]) as f:
                    self.assertEqual(f.read(), self.pem("TkVX"))
                self.assertEqual(image.extra_ca_files(dict(env, **{image.CA_CERTS_VAR: ""})), [])
                # Nothing that the system lacks: nothing to add.
                with open(bundle, "w") as f:
                    f.write(self.pem("U1lTVEVNMQ=="))
                self.assertEqual(image.extra_ca_files(env), [])

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

    def test_the_browser_wrappers_run_what_the_helper_links(self):
        # The agent has no sudo: the image owns /usr/bin/chromium and
        # /usr/bin/chromedriver, and mw-install-browser fills the links.
        with open(image.BUILD_SCRIPT, encoding="utf-8") as f:
            script = f.read()
        links = re.search(r"^BROWSER_LINKS=(\S+)$", script, re.M).group(1)
        self.assertIn(r'"\$bin" --no-sandbox --user-data-dir="\$d" "\$@"', script)
        for name in ("mw-install-browser", "mw-install-cypress"):
            with open(image.helper_files()[name], encoding="utf-8") as f:
                helper = f.read()
            code = [line for line in helper.splitlines() if not line.lstrip().startswith("#")]
            self.assertFalse([line for line in code if re.search(r"\bsudo\b", line)], name)
            if name == "mw-install-browser":
                self.assertIn(f'LINKS="${{WMF_SBX_BROWSER_LINKS:-{links}}}"', helper)
        for pkg in ("xvfb", "xauth", "libgtk-3-0t64", "libnss3", "libasound2t64", "libgbm1"):
            self.assertIn(pkg, image.IMAGE_PACKAGES)

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

    def test_credentials_survive_the_rewrite(self):
        # nono's proxy URL has credentials; with 127.0.0.1 left unchanged,
        # apt in the builder finds no proxy.
        out = image.guest_proxy_env({"https_proxy": "http://nono:s3cret@127.0.0.1:49509",
                                     "http_proxy": "http://u:p@proxy.example:3128"})
        self.assertEqual(out["https_proxy"], "http://nono:s3cret@192.168.5.2:49509")
        self.assertEqual(out["http_proxy"], "http://u:p@proxy.example:3128")

    def test_git_sends_proxy_credentials_at_once(self):
        # git's anyauth CONNECT has no credentials, and nono's proxy
        # aborts it.
        out = image.guest_proxy_env({"https_proxy": "http://nono:s3cret@127.0.0.1:49509"})
        self.assertEqual(out["GIT_HTTP_PROXY_AUTHMETHOD"], "basic")
        out = image.guest_proxy_env({"https_proxy": "http://127.0.0.1:3128"})
        self.assertNotIn("GIT_HTTP_PROXY_AUTHMETHOD", out)

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
        self.assertEqual([k for k, _n in gone], ["cccccccccccccccc"])
        self.assertGreater(gone[0][1], 0)
        self.assertEqual({k for k, _ in image.list_images(self.env)},
                         {"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"})

    def test_prune_with_min_age_keeps_young_and_undated_images(self):
        self.make_entry("aaaaaaaaaaaaaaaa", created="2026-10-09T00:00:00+00:00")
        self.make_entry("bbbbbbbbbbbbbbbb", created="2026-10-09T11:30:00+00:00")
        self.make_entry("cccccccccccccccc", created="")
        now = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=datetime.timezone.utc)
        gone = image.prune_images(env=self.env, min_age=datetime.timedelta(hours=1), now=now)
        self.assertEqual([k for k, _n in gone], ["aaaaaaaaaaaaaaaa"])

    def test_newest_image_for_this_arch_and_uid(self):
        self.make_entry("aaaaaaaaaaaaaaaa", created="2026-10-09T01:00:00+00:00")
        self.make_entry("bbbbbbbbbbbbbbbb", created="2026-10-09T02:00:00+00:00")
        key, _m = image.newest_image(self.env, arch="x86_64", uid=1000)
        self.assertEqual(key, "bbbbbbbbbbbbbbbb")
        self.assertIsNone(image.newest_image(self.env, arch="aarch64", uid=1000))
        self.assertIsNone(image.newest_image(self.env, arch="x86_64", uid=501))

    def test_a_broken_state_file_does_not_stop_rm(self):
        self.make_entry("aaaaaaaaaaaaaaaa")
        sdir = os.path.join(self.env["XDG_STATE_HOME"], "wmf-sbx", "sandboxes")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "bad.json"), "w") as f:
            f.write("{not json")
        image.remove_image("aaaaaaaaaaaaaaaa", self.env)


# -- the build sequence ---------------------------------------------------

class SubmoduleTests(unittest.TestCase):

    def check(self, stdout):
        return image.check_submodules("/repo", run=lambda argv, **kw: Done(stdout=stdout))

    def test_checked_out_submodules_pass(self):
        self.check(" e54c722 gerrit-mcp-server (heads/main)\n"
                   "+cee06f2 mcp-phabricator (v1-2-gcee06f2)\n")

    def test_a_submodule_that_is_not_checked_out_stops_the_build(self):
        with self.assertRaisesRegex(image.ImageError, "gitlab-mcp-server.*submodule update"):
            self.check(" e54c722 gerrit-mcp-server (heads/main)\n"
                       "-e88d8e8 gitlab-mcp-server\n")


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

    def start(self, name, timeout=None, env=None):
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
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return Done(stdout="feedface0123\n")
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
        self.assertEqual(manifest["commit"], "feedface0123")
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

    def hold_lock(self):
        """Another process's build of the key: its lock, held."""
        import fcntl
        os.makedirs(image.cache_root(self.env), exist_ok=True)
        f = open(image.build_lock_path(self.key, self.env), "a")
        fcntl.flock(f, fcntl.LOCK_EX)
        self.addCleanup(f.close)
        return f

    def test_a_builder_left_by_a_crashed_build_is_deleted(self):
        # Its lock is free: no build is running.
        name = image.builder_name(self.key)
        fake = FakeLima(self.env, existing=[name])
        self.assertEqual(self.build(fake), self.key)
        self.assertEqual(fake.calls[:2], [("exists", name), ("delete", name)])
        self.assertLess(fake.calls.index(("delete", name)), fake.calls.index(("create", name)))

    def test_a_running_build_is_not_touched_by_default(self):
        self.hold_lock()
        name = image.builder_name(self.key)
        fake = FakeLima(self.env, existing=[name])
        with self.assertRaisesRegex(image.ImageError, "another wmf-sbx is building"):
            self.build(fake)
        self.assertEqual(fake.calls, [])

    def test_wait_uses_the_image_the_other_build_made(self):
        holder = self.hold_lock()
        seen = []

        def busy(key, name):
            seen.append((key, name))
            holder.close()                      # the other build ends ...
            self.build(FakeLima(self.env))      # ... with the image made
            return "wait"
        fake = FakeLima(self.env, existing=[image.builder_name(self.key)])
        self.assertEqual(self.build(fake, busy=busy), self.key)
        self.assertEqual(seen, [(self.key, image.builder_name(self.key))])
        self.assertEqual(fake.calls, [])        # no builder of its own

    def test_delete_removes_the_other_builder_then_builds_here(self):
        holder = self.hold_lock()
        name = image.builder_name(self.key)
        fake = FakeLima(self.env, existing=[name])

        def busy(key, n):
            # The other build fails once its builder is gone; its lock goes.
            fake_delete = fake.delete

            def delete(n2):
                fake_delete(n2)
                holder.close()
            fake.delete = delete
            return "delete"
        self.assertEqual(self.build(fake, busy=busy), self.key)
        self.assertEqual(fake.calls[:2], [("exists", name), ("delete", name)])
        self.assertIn(("create", name), fake.calls)

    def test_the_busy_prompt(self):
        self.assertEqual(image.busy_prompt(False)("k", "b"), "abort")
        for answers, want in ((["", ], "wait"), (["d"], "delete"), (["x", "abort"], "abort"),
                              (["2"], "delete"), ([], "abort")):
            it = iter(answers)
            def ask(q):
                try:
                    return next(it)
                except StopIteration:
                    raise EOFError from None
            choose = image.busy_prompt(True, prompt=ask, err=io.StringIO())
            self.assertEqual(choose("k", "b"), want, answers)

    def test_a_builder_that_does_not_start_is_deleted(self):
        fake = FakeLima(self.env)

        def refuse(name, timeout=None):
            raise image.lima_mod.LimaError("limactl start ... failed")
        fake.start = refuse
        with self.assertRaises(image.lima_mod.LimaError):
            self.build(fake)
        self.assertEqual(fake.calls[-1], ("delete", image.builder_name(self.key)))
        self.assertEqual(image.list_images(self.env), [])

    def test_a_failed_build_keeps_the_builder_and_leaves_no_entry(self):
        fake = FakeLima(self.env, build_rc=1)
        with self.assertRaisesRegex(image.ImageError, "builder is kept"):
            self.build(fake)
        self.assertNotIn(("delete", image.builder_name(self.key)), fake.calls)
        self.assertEqual(image.list_images(self.env), [])
        self.assertEqual([n for n in os.listdir(image.cache_root(self.env))
                          if not n.endswith(".lock")], [])

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

    def test_no_limactl_is_a_lima_error(self):
        def run(argv, **kw):
            raise FileNotFoundError(argv[0])
        with self.assertRaisesRegex(lima.LimaError, "limactl is not on PATH"):
            lima.Limactl(run=run).call("list")

    def test_an_error_does_not_show_proxy_passwords(self):
        err = ('msg="Replacing `http_proxy` value `http://nono:s3cret@127.0.0.1:49383` '
               'with `http://nono:s3cret@192.168.5.2:49383`"')
        with self.assertRaises(lima.LimaError) as cm:
            lima.Limactl(run=self.runner(Done(returncode=1, stderr=err))).call("create", "x")
        self.assertNotIn("s3cret", str(cm.exception))
        self.assertIn("http://nono:***@127.0.0.1:49383", str(cm.exception))
        self.assertEqual(lima.redact("https://example.org/a:b@c"), "https://example.org/a:b@c")

    def test_stdin_is_closed_unless_interactive_or_given(self):
        seen = []

        def run(argv, **kw):
            seen.append(kw)
            return Done()
        l = lima.Limactl(run=run)
        l.call("shell", "x", "--", "true")
        self.assertIs(seen[-1]["stdin"], subprocess.DEVNULL)
        l.call("shell", "x", "--", "cat", input="data")
        self.assertNotIn("stdin", seen[-1])
        self.assertEqual(seen[-1]["input"], "data")
        l.call("shell", "x", "--", "claude", capture=False, interactive=True)
        self.assertNotIn("stdin", seen[-1])

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
