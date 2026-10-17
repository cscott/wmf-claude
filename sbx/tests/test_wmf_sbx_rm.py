#!/usr/bin/env python3
"""Unit tests for rm.py's argument parsing -- run with:
  python3 -m unittest discover -s sbx/tests -v

The removal itself (the prompt, the unfetched-work guard, the host
remotes) is tested in test_wmf_sbx_lifecycle.py.
"""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.rm as rm  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


class ArgumentTests(unittest.TestCase):
    def test_no_name_and_no_prune_is_an_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            rm.main([])
        self.assertIn("--prune", err.getvalue())


if __name__ == "__main__":
    unittest.main()
