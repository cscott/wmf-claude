#!/usr/bin/env python3
"""Unit tests for wmf_sbx/template.py and sandbox-provision.sh -- run with:
  python3 -m unittest discover -s sbx/tests -v

These are the static template checks of HANDOFF-LIMA.md §5.3."""

import copy
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.template as template  # noqa: E402

GOLDEN = "/cache/wmf-sbx/images/0123456789abcdef/golden.qcow2"
HOST_UID = 30033


def make(**kw):
    kw.setdefault("host_uid", HOST_UID)
    kw.setdefault("vm_type", "qemu")
    return template.sandbox_template(GOLDEN, "x86_64", **kw)


class TemplateTests(unittest.TestCase):

    def test_the_default_template_passes_its_own_checks(self):
        t = make()
        template.check_template(t, host_uid=HOST_UID)
        self.assertIs(t["plain"], False)
        self.assertEqual(t["images"], [{"location": GOLDEN, "arch": "x86_64"}])
        self.assertEqual(t["mounts"], [])

    def test_mount_type_follows_the_driver(self):
        self.assertEqual(make(vm_type="qemu")["mountType"], "9p")
        self.assertEqual(make(vm_type="vz")["mountType"], "virtiofs")
        with self.assertRaises(template.TemplateError):
            make(vm_type="krunkit")

    def test_default_driver(self):
        self.assertEqual(template.default_vm_type("Darwin"), "vz")
        self.assertEqual(template.default_vm_type("Linux"), "qemu")

    def test_git_dirs_mount_read_only_under_run_wmf_sbx_host(self):
        t = make(gitdirs=["/home/me/src/core/.git", "/home/me/src/Cite/.git"])
        self.assertEqual(t["mounts"][0], {
            "location": "/home/me/src/core/.git",
            "mountPoint": "/run/wmf-sbx/host/home/me/src/core/.git",
            "writable": False, "9p": {"cache": "none"}})
        self.assertEqual(len(t["mounts"]), 2)
        self.assertNotIn("9p", make(vm_type="vz", gitdirs=["/h/c/.git"])["mounts"][0])

    def test_git_dir_names(self):
        for ok in ("/h/c/.git", "/h/c.git", "/h/c/.git/modules/vendor"):
            self.assertTrue(template.is_git_dir_name(ok), ok)
        for bad in ("/h/c", "/h/c/.git-not", "/h/modules/x"):
            self.assertFalse(template.is_git_dir_name(bad), bad)

    def test_the_engineer_does_not_get_the_host_uid(self):
        t = make()
        self.assertEqual(t["user"]["name"], "engineer")
        self.assertNotIn(t["user"]["uid"], (HOST_UID, image.BUILDER_UID))
        with self.assertRaises(template.TemplateError):
            make(host_uid=template.ENGINEER_UID)

    def test_proxy_ports_and_cas(self):
        t = make(proxy_ports=[3128, 8080], ca_files=["/etc/x.crt"])
        script = t["provision"][0]["script"]
        self.assertIn('PROXY_PORTS="3128 8080"', script)
        self.assertIn('PROXY_RULE="ip daddr 192.168.5.2 tcp dport { ${PROXY_PORTS// /, } } accept"', script)
        self.assertEqual(t["caCerts"], {"files": ["/etc/x.crt"]})
        self.assertNotIn("caCerts", make())
        self.assertIn('PROXY_PORTS=""', make()["provision"][0]["script"])

    def test_loopback_proxy_ports(self):
        env = {"https_proxy": "http://127.0.0.1:3128", "HTTP_PROXY": "http://localhost:8080/",
               "http_proxy": "http://proxy.example:3128", "no_proxy": "localhost"}
        self.assertEqual(template.loopback_proxy_ports(env), [3128, 8080])
        self.assertEqual(template.loopback_proxy_ports({}), [])

    def test_the_template_is_valid_for_limactl(self):
        if not shutil.which("limactl") or os.geteuid() == 0:
            self.skipTest("needs limactl and a non-root user")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            image.yaml.safe_dump(make(gitdirs=["/tmp/x/.git"], proxy_ports=[3128]), f)
        self.addCleanup(os.unlink, f.name)
        subprocess.run(["limactl", "validate", f.name], check=True, capture_output=True)


class CheckTests(unittest.TestCase):
    """Each rule of check_template, broken once."""

    def broken(self, mutate):
        t = copy.deepcopy(make(gitdirs=["/home/me/src/core/.git"]))
        mutate(t)
        with self.assertRaises(template.TemplateError):
            template.check_template(t, host_uid=HOST_UID)

    def test_each_rule(self):
        cases = {
            "rosetta": lambda t: t.update(rosetta={"enabled": True}),
            "vmOpts": lambda t: t.update(vmOpts={}),
            "networks": lambda t: t.update(networks=[{"lima": "user-v2"}]),
            "base": lambda t: t.update(base=["template:default"]),
            "plain": lambda t: t.update(plain=True),
            "forwards": lambda t: t.update(portForwards=[]),
            "containerd": lambda t: t.update(containerd={"system": False, "user": True}),
            "agent forwarding": lambda t: t["ssh"].update(forwardAgent=True),
            "reverse-sshfs": lambda t: t.update(mountType="reverse-sshfs"),
            "writable": lambda t: t["mounts"][0].update(writable=True),
            "9p cache": lambda t: t["mounts"][0].pop("9p"),
            "mount point": lambda t: t["mounts"][0].update(mountPoint="/home/me/src/core/.git"),
            "not a git dir": lambda t: t["mounts"][0].update(location="/home/me/src/core"),
            "home": lambda t: t["mounts"].append({"location": os.path.expanduser("~"),
                                                  "mountPoint": "/run/wmf-sbx/host/h",
                                                  "writable": False}),
            "host uid": lambda t: t["user"].update(uid=HOST_UID),
            "builder uid": lambda t: t["user"].update(uid=image.BUILDER_UID),
        }
        for name, mutate in cases.items():
            with self.subTest(rule=name):
                self.broken(mutate)


class ProvisionTests(unittest.TestCase):

    def script(self):
        return make(proxy_ports=[3128])["provision"][0]["script"]

    def test_it_parses(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(self.script())
        self.addCleanup(os.unlink, f.name)
        subprocess.run(["bash", "-n", f.name], check=True)

    def test_every_placeholder_is_filled(self):
        for token in ("@ENGINEER@", "@PROXY_PORTS@"):
            self.assertNotIn(token, self.script())
        self.assertIn('ENGINEER="engineer"', self.script())

    def test_the_proxy_rule_expands_to_valid_nft_syntax(self):
        out = subprocess.run(
            ["bash", "-c", 'PROXY_PORTS="3128 8080"; echo "ip daddr 192.168.5.2 tcp dport { ${PROXY_PORTS// /, } } accept"'],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "ip daddr 192.168.5.2 tcp dport { 3128, 8080 } accept")

    def test_no_go_template_syntax(self):
        # Lima runs provision scripts through Go's text/template.
        self.assertNotIn("{{", self.script())

    def test_kostas_rules_are_there(self):
        s = self.script()
        for needle in ("gpasswd -d", "ip daddr @private4 counter reject",
                       "tcp dport { 80, 443 } accept", "meta skuid $AGENT_UID jump agent_egress",
                       "dev.tty.legacy_tiocsti = 0", "touch /run/wmf-sbx/provisioned"):
            self.assertIn(needle, s)
        self.assertNotIn("flush ruleset", s)


if __name__ == "__main__":
    unittest.main()
