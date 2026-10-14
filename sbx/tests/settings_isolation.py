"""Keeps the engineer's own ~/.config/wmf-sbx/settings.yaml out of the
tests: every command reads it for its option defaults (cli.parse_args).
A test module that runs a command's main calls `isolate()` from its
setUpModule."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.resolve as resolve_mod  # noqa: E402


def isolate():
    for name in ("DEFAULT_CONFIG", "LEGACY_CONFIG"):
        patch = mock.patch.object(resolve_mod, name, f"/nonexistent/wmf-sbx/{name}.yaml")
        patch.start()
        unittest.addModuleCleanup(patch.stop)
