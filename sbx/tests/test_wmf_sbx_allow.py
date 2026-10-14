#!/usr/bin/env python3
"""Unit tests for `wmf-sbx allow` (allow.py) -- run with:
  python3 -m unittest discover -s sbx/tests -v"""

import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.allow as allow  # noqa: E402
import wmf_sbx.egress as egress  # noqa: E402
import wmf_sbx.lima as lima_mod  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import settings_isolation  # noqa: E402


def setUpModule():
    settings_isolation.isolate()


class AllowTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state"),
                    "LIMA_HOME": os.path.join(self.tmp.name, "lima"), "PATH": "/usr/bin"}
        st = state_mod.new_state("demo", primary_dir="/home/me/src/x", repos=["/home/me/src/x"])
        st["egress"] = "proxy"
        state_mod.save(st, self.env)
        self.killed = []
        self.ps_out = None

    def ps(self, argv, **kw):
        self.assertEqual(argv[:2], ["ps", "-p"])
        if self.ps_out is None:
            return subprocess.CompletedProcess(argv, 1, stdout="")
        return subprocess.CompletedProcess(argv, 0, stdout=self.ps_out)

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        egress.set_port("demo", 41000 + len(self.killed), self.env)   # the new proxy

    def run_allow(self, *argv):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = allow.main(list(argv), env=self.env, kill=self.kill, ps=self.ps)
        self.err, self.out = err.getvalue(), out.getvalue()
        return rc

    def running_wrapper(self, pid=4242):
        """A pid file, and `ps` showing this VM's wrapper."""
        egress.make_dir("demo", self.env)
        egress.set_port("demo", 41000, self.env)
        config = egress.proxy_config("demo", self.env)
        egress.write_pid(config["pidFile"], pid)
        inst = os.path.join(lima_mod.lima_home(self.env), "wmf-sbx-demo")
        self.ps_out = (f"/usr/bin/python3 /opt/sbx/bin/wmf-sbx-qemu -pidfile {inst}/qemu.pid "
                       f"-qmp unix:{inst}/qmp.sock\n")

    def test_a_host_is_added_and_the_running_wrapper_swaps_its_proxy(self):
        self.running_wrapper()
        self.assertEqual(self.run_allow("demo", "Example.org", "*.example.net"), 0, self.err)
        self.assertEqual(egress.extra_domains("demo", self.env), ["example.org", "*.example.net"])
        self.assertEqual(self.killed, [(4242, signal.SIGUSR1)])
        self.assertIn("port 41001", self.err)
        # the wrapper reads the config again: the new hosts are in its argv
        argv = egress.read_config(egress.config_file("demo", self.env))["argv"]
        self.assertIn("example.org", argv)
        self.assertEqual(self.run_allow("demo", "--list"), 0)
        self.assertEqual(self.out, "example.org\n*.example.net\n")

    def test_rm_removes_and_an_unknown_host_is_an_error(self):
        egress.set_extra_domains("demo", ["a.example.org", "b.example.org"], self.env)
        self.assertEqual(self.run_allow("demo", "--rm", "a.example.org"), 0, self.err)
        self.assertEqual(egress.extra_domains("demo", self.env), ["b.example.org"])
        self.assertEqual(self.run_allow("demo", "--rm", "c.example.org"), 1)
        self.assertIn("not in demo's list: c.example.org", self.err)

    def test_settings_allow_list_and_the_command_line_rm(self):
        settings = os.path.join(self.tmp.name, "settings.yaml")
        with open(settings, "w", encoding="utf-8") as f:
            f.write("allow:\n  list: true\n")
        egress.set_extra_domains("demo", ["a.example.org", "b.example.org"], self.env)
        self.assertEqual(self.run_allow("--config", settings, "demo"), 0, self.err)
        self.assertEqual(self.out, "a.example.org\nb.example.org\n")
        # --rm on the command line drops the settings' list: true
        self.assertEqual(self.run_allow("--config", settings, "demo", "--rm", "a.example.org"),
                         0, self.err)
        self.assertEqual(egress.extra_domains("demo", self.env), ["b.example.org"])

    def test_a_stopped_vm_gets_it_at_the_next_start(self):
        self.assertEqual(self.run_allow("demo", "example.org"), 0, self.err)
        self.assertEqual(self.killed, [])
        self.assertIn("next start", self.err)

    def test_a_stale_pid_is_not_signalled(self):
        self.running_wrapper()
        for out in ("/usr/bin/vim notes.txt\n",
                    # another sandbox's wrapper
                    self.ps_out.replace("wmf-sbx-demo/", "wmf-sbx-demo2/")):
            with self.subTest(out=out):
                self.ps_out = out
                self.assertEqual(self.run_allow("demo", f"h{len(out)}.example.org"), 0,
                                 self.err)
                self.assertEqual(self.killed, [])

    def test_a_wrapper_that_starts_no_proxy_is_an_error(self):
        self.running_wrapper()
        clock = iter(range(0, 1000, 10))
        with self.assertRaisesRegex(allow.AllowError, "started no new proxy"):
            allow.swap("demo", self.env, kill=lambda p, s: None, ps=self.ps,
                       clock=lambda: next(clock), sleep=lambda s: None)

    def test_bad_hosts_and_an_open_sandbox_are_refused(self):
        for host in ("*", "https://example.org/", "example.org:443", "-x", "localhost",
                     "a b.org", "*.*.org"):
            with self.subTest(host=host):
                self.assertEqual(self.run_allow("demo", "--", host), 1)
                self.assertIn("not a host name", self.err)
        self.assertEqual(egress.extra_domains("demo", self.env), [])
        st = state_mod.load("demo", self.env)
        del st["egress"]
        state_mod.save(st, self.env)
        self.assertEqual(self.run_allow("demo", "example.org"), 1)
        self.assertIn("not behind the egress proxy", self.err)


if __name__ == "__main__":
    unittest.main()
