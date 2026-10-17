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
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


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

    def test_an_objects_dir_can_be_mounted(self):
        # RAN (nested): a sandbox's clone borrows /run/wmf-sbx/host/<path>/.git/objects.
        for ok in ("/h/c/.git", "/h/c/.git/objects", "/run/wmf-sbx/host/h/c/.git/objects/",
                   "/h/c.git/objects", "/h/c/.git/modules/vendor/objects"):
            self.assertTrue(template.is_mountable(ok), ok)
        for bad in ("/h/c", "/h/c/objects", "/h/c/.git/objects/pack", "/h/objects"):
            self.assertFalse(template.is_mountable(bad), bad)
        t = make(gitdirs=["/h/c/.git", "/run/wmf-sbx/host/h/c/.git/objects"])
        template.check_template(t, host_uid=HOST_UID)

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
        # limactl takes an instance name from the file name, and a
        # tempfile name ending in "_" or holding "__" is not a valid one.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "lima.yaml")
        with open(path, "w", encoding="utf-8") as f:
            image.yaml.safe_dump(make(gitdirs=["/tmp/x/.git"], proxy_ports=[3128]), f)
        res = subprocess.run(["limactl", "validate", path], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


class NestedTemplateTests(unittest.TestCase):

    def test_vz_asks_for_nested_virtualisation(self):
        t = template.sandbox_template("/x/golden.qcow2", "aarch64", vm_type="vz", nested=True,
                                      host_uid=1000)
        self.assertTrue(t["nestedVirtualization"])
        t = template.sandbox_template("/x/golden.qcow2", "x86_64", vm_type="qemu",
                                      nested=True, host_uid=1000)
        self.assertNotIn("nestedVirtualization", t)
        t = template.sandbox_template("/x/golden.qcow2", "aarch64", vm_type="vz", host_uid=1000)
        self.assertNotIn("nestedVirtualization", t)


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
            "not a git objects dir": lambda t: t["mounts"][0].update(
                location="/home/me/src/core/objects"),
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
        for token in ("@ENGINEER@", "@PROXY_PORTS@", "@EPHEMERAL_PORTS@", "@AGENT_SUDO@"):
            self.assertNotIn(token, self.script())
        self.assertIn('ENGINEER="engineer"', self.script())

    def test_the_ephemeral_window_is_the_one_sessions_open(self):
        # session.py grants exactly this window; the agent's off-VM TCP
        # stays limited to 80 and 443, so the window reaches loopback only.
        lo, hi = template.EPHEMERAL_PORTS
        self.assertIn(f'EPHEMERAL_PORTS="{lo} {hi}"', self.script())
        self.assertIn("net.ipv4.ip_local_port_range = $EPHEMERAL_PORTS", self.script())
        self.assertIn("tcp dport { 80, 443 } accept", self.script())
        self.assertLessEqual(hi - lo + 1, 8192)  # bin/claude's --local-web range limit

    def test_the_guest_reaches_each_guestfwd_on_the_egress_address(self):
        script = template.provision_script(egress=True)
        self.assertIn('EGRESS_RULE="ip daddr $EGRESS tcp dport { 53, 3128, 3129, 3130 } accept"',
                      script)

    def test_the_proxy_rule_expands_to_valid_nft_syntax(self):
        out = subprocess.run(
            ["bash", "-c", 'PROXY_PORTS="3128 8080"; echo "ip daddr 192.168.5.2 tcp dport { ${PROXY_PORTS// /, } } accept"'],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "ip daddr 192.168.5.2 tcp dport { 3128, 8080 } accept")

    def test_no_go_template_syntax(self):
        # Lima runs provision scripts through Go's text/template.
        self.assertNotIn("{{", self.script())

    def sudo_block(self, sudo):
        """The script's sudo block, run with its file paths under a temp
        dir: the files it leaves there."""
        s = make(egress_ca="/x/ca.pem", sudo=sudo)["provision"][0]["script"] if sudo \
            else self.script()
        start = s.index("SUDOERS=/etc/sudoers.d/")
        block = s[start:s.index("\nfi\n", start) + 4]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        os.makedirs(os.path.join(d, "etc", "sudoers.d"))
        block = block.replace("/etc/", f"{d}/etc/").replace("visudo -cqf", "true")
        head = s[s.index("AGENT=agent"):s.index("\n", s.index('AGENT_SUDO="'))]
        subprocess.run(["bash", "-euc", head + "\n" + block], check=True)
        found = {}
        for root, _dirs, files in os.walk(d):
            for f in files:
                with open(os.path.join(root, f)) as fh:
                    found[os.path.relpath(os.path.join(root, f), d)] = fh.read()
        return found

    def test_a_sudo_sandbox_gets_a_sudoers_file_and_the_marker(self):
        files = self.sudo_block(True)
        self.assertEqual(sorted(files), ["etc/sudoers.d/90-wmf-sbx-agent", "etc/wmf-sbx/sudo"])
        sudoers = files["etc/sudoers.d/90-wmf-sbx-agent"]
        self.assertIn("agent ALL=(ALL:ALL) NOPASSWD: ALL", sudoers)
        self.assertIn("env_keep", sudoers)
        self.assertIn("https_proxy", sudoers)

    @unittest.skipUnless(shutil.which("visudo") or os.path.exists("/usr/sbin/visudo"),
                         "no visudo")
    def test_the_sudoers_file_parses(self):
        sudoers = self.sudo_block(True)["etc/sudoers.d/90-wmf-sbx-agent"]
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(sudoers)
        self.addCleanup(os.unlink, f.name)
        visudo = shutil.which("visudo") or "/usr/sbin/visudo"
        res = subprocess.run([visudo, "-cqf", f.name], capture_output=True, text=True)
        if res.returncode == 127:   # visudo there, but cannot run (a missing library)
            self.skipTest(res.stderr.strip())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_a_nono_mode_sandbox_removes_both(self):
        self.assertEqual(self.sudo_block(False), {})
        self.assertIn('AGENT_SUDO=""', self.script())

    def test_sudo_needs_the_egress_proxy(self):
        with self.assertRaisesRegex(template.TemplateError, "egress proxy"):
            make(sudo=True)

    def test_kostas_rules_are_there(self):
        s = self.script()
        for needle in ("gpasswd -d", "ip daddr @private4 counter reject",
                       "tcp dport { 80, 443 } accept", "meta skuid $AGENT_UID jump agent_egress",
                       "dev.tty.legacy_tiocsti = 0", "touch /run/wmf-sbx/provisioned",
                       # DNS after Lima's NAT to the host agent's resolver
                       "ct original ip daddr $NET ct original proto-dst 53 accept"):
            self.assertIn(needle, s)
        self.assertNotIn("flush ruleset", s)

    def test_conntrack_accepts_replies_only(self):
        s = self.script()
        self.assertEqual(s.count("ct state established,related ct direction reply accept"), 2)
        self.assertNotIn("ct state established,related accept", s)




class MountLimitTests(unittest.TestCase):

    def test_qemu_takes_at_most_max_qemu_mounts(self):
        dirs = [f"/h/r{i}/.git" for i in range(template.MAX_QEMU_MOUNTS)]
        self.assertEqual(len(make(vm_type="qemu", gitdirs=dirs)["mounts"]),
                         template.MAX_QEMU_MOUNTS)
        with self.assertRaisesRegex(template.TemplateError, f"at most {template.MAX_QEMU_MOUNTS}.*--no-phan-deps"):
            make(vm_type="qemu", gitdirs=dirs + ["/h/one-more/.git"])

    def test_vz_has_no_such_limit(self):
        dirs = [f"/h/r{i}/.git" for i in range(template.MAX_QEMU_MOUNTS + 1)]
        self.assertEqual(len(make(vm_type="vz", gitdirs=dirs)["mounts"]), len(dirs))


if __name__ == "__main__":
    unittest.main()
