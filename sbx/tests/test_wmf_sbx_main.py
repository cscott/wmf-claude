#!/usr/bin/env python3
"""Unit tests for wmf_sbx/__main__.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import os
import subprocess
import sys
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

import wmf_sbx.__main__ as m  # noqa: E402


class MainDispatchTests(unittest.TestCase):
    def test_unknown_command_is_an_error(self):
        self.assertEqual(m.main(["no-such-command"]), 1)

    def test_no_args_is_an_error(self):
        self.assertEqual(m.main([]), 1)

    def test_help_flag_exits_zero(self):
        self.assertEqual(m.main(["--help"]), 0)
        self.assertEqual(m.main(["-h"]), 0)

    def test_every_advertised_command_resolves_to_a_real_module(self):
        import importlib

        for command, modname in m.COMMANDS.items():
            module = importlib.import_module(modname)
            self.assertTrue(
                callable(getattr(module, "main", None)),
                f"{command} -> {modname} has no callable main()",
            )

    def test_dispatches_to_the_right_module(self):
        calls = []

        class FakeModule:
            def main(self, argv):
                calls.append(argv)
                return 42

        import wmf_sbx.resolve as resolve_mod

        orig = resolve_mod.main
        resolve_mod.main = FakeModule().main
        try:
            self.assertEqual(m.main(["resolve", "Cite", "--json"]), 42)
            self.assertEqual(calls, [["Cite", "--json"]])
        finally:
            resolve_mod.main = orig


@unittest.skipUnless(sys.executable, "python3 required")
class CliTests(unittest.TestCase):
    """End-to-end: `python3 -m wmf_sbx ...` with sbx/src on PYTHONPATH."""

    def run_module(self, *args):
        env = dict(os.environ, PYTHONPATH=os.path.abspath(SRC))
        return subprocess.run(
            [sys.executable, "-m", "wmf_sbx"] + list(args),
            capture_output=True, text=True, env=env,
        )

    def test_no_args_prints_usage_and_exits_nonzero(self):
        result = self.run_module()
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage:", result.stderr)

    def test_unknown_command_names_itself_in_the_error(self):
        result = self.run_module("bogus")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bogus", result.stderr)

    def test_resolve_dash_dash_help_reaches_the_submodule(self):
        # Proves argv actually reaches wmf_sbx.resolve's own argparser,
        # not just that the dispatcher recognizes the command name.
        result = self.run_module("resolve", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower() + result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
