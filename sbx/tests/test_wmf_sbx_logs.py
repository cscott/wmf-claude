#!/usr/bin/env python3
"""Unit tests for `wmf-sbx logs` (logs.py) -- run with:
  python3 -m unittest discover -s sbx/tests -v"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.egress as egress  # noqa: E402
import wmf_sbx.logs as logs  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402


class LogsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state"),
                    "LIMA_HOME": os.path.join(self.tmp.name, "lima"), "PATH": "/usr/bin"}
        st = state_mod.new_state("demo", primary_dir="/home/me/src/x", repos=["/home/me/src/x"])
        st["egress"] = "proxy"
        state_mod.save(st, self.env)
        egress.make_dir("demo", self.env)
        with open(egress.log_file("demo", self.env), "w") as f:
            f.write("decision=\"deny\"\n")
        self.execs = []

    def run_logs(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = logs.main(list(argv), env=self.env,
                           execvp=lambda f, a: self.execs.append((f, a)))
        self.err = err.getvalue()
        return rc

    def test_the_name_first_or_last_and_tails_options_pass_through(self):
        path = egress.log_file("demo", self.env)
        for argv, opts in ((["demo"], []),
                           (["demo", "-f"], ["-f"]),
                           (["demo", "-n", "100"], ["-n", "100"]),
                           (["-50", "demo"], ["-50"]),
                           (["-f", "-n", "5", "demo"], ["-f", "-n", "5"])):
            with self.subTest(argv=argv):
                self.execs = []
                self.assertEqual(self.run_logs(*argv), 0, self.err)
                self.assertEqual(self.execs, [("tail", ["tail"] + opts + ["--", path])])

    def test_the_proxy_writes_the_log_logs_reads(self):
        self.assertIn(egress.log_file("demo", self.env), egress.proxy_argv("demo", self.env))

    def test_no_name_is_a_usage_error(self):
        self.assertEqual(self.run_logs("-f"), 2)
        self.assertEqual(self.run_logs(), 2)
        self.assertEqual(self.execs, [])

    def test_an_unknown_sandbox_is_refused(self):
        self.assertEqual(self.run_logs("nosuch", "-f"), 1)
        self.assertEqual(self.execs, [])

    def test_a_sandbox_without_the_egress_proxy_has_no_log(self):
        st = state_mod.new_state("plain", primary_dir="/home/me/src/y", repos=["/home/me/src/y"])
        state_mod.save(st, self.env)
        self.assertEqual(self.run_logs("plain"), 1)
        self.assertIn("not behind the egress proxy", self.err)
        self.assertEqual(self.execs, [])

    def test_a_missing_log_says_so(self):
        os.unlink(egress.log_file("demo", self.env))
        self.assertEqual(self.run_logs("demo"), 1)
        self.assertIn("no proxy log yet", self.err)

    def test_tail_reads_it(self):
        # The real tail, through the real exec, in a child.
        import subprocess
        res = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import wmf_sbx.logs as l; "
             "sys.exit(l.main(['-1', 'demo']))",
             os.path.join(os.path.dirname(__file__), "..", "src")],
            env=dict(os.environ, **self.env), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "decision=\"deny\"\n")


if __name__ == "__main__":
    unittest.main()
