#!/usr/bin/env python3
"""Every test module keeps the machine's own state out (isolation.py)."""

import glob
import importlib
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.nested as nested  # noqa: E402
import wmf_sbx.resolve as resolve  # noqa: E402
import isolation  # noqa: E402

REAL_HOME = os.environ.get("HOME")


def setUpModule():
    isolation.isolate()


class IsolationTests(unittest.TestCase):

    def test_every_test_module_isolates_itself(self):
        here = os.path.dirname(os.path.abspath(__file__))
        names = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(here, "test_*.py")))
        self.assertIn("test_wmf_sbx_isolation", names)
        for name in names:
            with self.subTest(name):
                setup = getattr(importlib.import_module(name), "setUpModule", None)
                # The module's last setUpModule is the one unittest runs.
                self.assertIsNotNone(setup, f"{name} has no setUpModule")
                self.assertTrue({"isolation", "isolate"} <= set(setup.__code__.co_names),
                                f"{name}'s setUpModule does not call isolation.isolate()")

    def test_the_machine_state_is_out_of_reach(self):
        self.assertNotEqual(os.environ["HOME"], REAL_HOME)
        self.assertEqual(os.path.expanduser("~"), os.environ["HOME"])
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                    "PHABRICATOR_USERNAME", "LIMA_HOME"):
            self.assertNotIn(var, os.environ)
        self.assertFalse([k for k in os.environ if k.startswith("WMF_SBX_")])
        self.assertFalse(os.path.exists(image.NESTED_MARKER))
        self.assertFalse(nested.inside())
        self.assertFalse(os.path.exists(resolve.DEFAULT_CONFIG))
        for stub in isolation.STUBS:
            self.assertTrue(shutil.which(stub).startswith(os.environ["HOME"] + os.sep), stub)


if __name__ == "__main__":
    unittest.main()
