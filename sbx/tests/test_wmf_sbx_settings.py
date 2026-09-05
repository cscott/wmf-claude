#!/usr/bin/env python3
"""Unit tests for wmf_sbx.settings -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.settings as settings  # noqa: E402


class FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


SETTINGS_JSON = """
[
  {"key": "ssh.agentForwardingEnabled", "value": false, "default": true,
   "type": "bool", "description": "..."},
  {"key": "kit.allowLocalKits", "value": true, "default": true,
   "type": "bool", "description": "..."},
  {"key": "kit.requireSignature", "value": false, "default": false,
   "type": "bool", "description": "..."},
  {"key": "no_proxy.sandbox", "value": "", "default": "",
   "type": "string", "description": "..."},
  {"key": "proxy.sandbox", "value": "", "default": "",
   "type": "string", "description": "..."}
]
"""


class ReadSettingsTests(unittest.TestCase):
    def test_parses_a_good_json_array_into_a_dict(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout=SETTINGS_JSON)
        self.assertEqual(
            settings.read_settings("/bin/wmf-sbx", run=run),
            {
                "ssh.agentForwardingEnabled": False,
                "kit.allowLocalKits": True,
                "kit.requireSignature": False,
                "no_proxy.sandbox": "",
                "proxy.sandbox": "",
            },
        )

    def test_calls_settings_list_json_with_devnull_stdin(self):
        import subprocess
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            return FakeCompletedProcess(0, stdout="[]")

        settings.read_settings("/bin/wmf-sbx", run=run)
        self.assertEqual(
            seen["argv"],
            ["/bin/wmf-sbx", "--upstream", "settings", "list", "--json"],
        )
        self.assertEqual(seen["kw"].get("stdin"), subprocess.DEVNULL)

    def test_none_on_nonzero_returncode(self):
        run = lambda argv, **kw: FakeCompletedProcess(1, stdout=SETTINGS_JSON)
        self.assertIsNone(settings.read_settings("/bin/wmf-sbx", run=run))

    def test_none_on_unparseable_json(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout="not json")
        self.assertIsNone(settings.read_settings("/bin/wmf-sbx", run=run))

    def test_none_when_json_is_not_a_list(self):
        run = lambda argv, **kw: FakeCompletedProcess(0, stdout='{"oops": true}')
        self.assertIsNone(settings.read_settings("/bin/wmf-sbx", run=run))

    def test_none_on_oserror(self):
        def run(argv, **kw):
            raise OSError("no such file")

        self.assertIsNone(settings.read_settings("/bin/wmf-sbx", run=run))

    def test_skips_malformed_rows_but_keeps_the_rest(self):
        run = lambda argv, **kw: FakeCompletedProcess(
            0, stdout='[{"key": "a", "value": 1}, "garbage", {"no_key": true}]'
        )
        self.assertEqual(settings.read_settings("/bin/wmf-sbx", run=run), {"a": 1})


class PreflightTests(unittest.TestCase):
    DEFAULTS = {
        "ssh.agentForwardingEnabled": False,
        "kit.allowLocalKits": True,
        "kit.requireSignature": False,
        "no_proxy.sandbox": "",
        "proxy.sandbox": "",
    }

    def test_all_defaults_produce_no_warnings_or_errors(self):
        self.assertEqual(settings.preflight(self.DEFAULTS), ([], []))

    def test_ssh_forwarding_enabled_warns_but_does_not_fail(self):
        values = dict(self.DEFAULTS, **{"ssh.agentForwardingEnabled": True})
        warnings, errors = settings.preflight(values)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("ssh.agentForwardingEnabled", warnings[0])

    def test_kit_allow_local_kits_false_is_an_error(self):
        values = dict(self.DEFAULTS, **{"kit.allowLocalKits": False})
        warnings, errors = settings.preflight(values)
        self.assertEqual(warnings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("kit.allowLocalKits", errors[0])

    def test_kit_require_signature_true_is_an_error(self):
        values = dict(self.DEFAULTS, **{"kit.requireSignature": True})
        warnings, errors = settings.preflight(values)
        self.assertEqual(warnings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("kit.requireSignature", errors[0])

    def test_kit_checks_are_skipped_when_check_kit_is_false(self):
        values = dict(self.DEFAULTS, **{
            "kit.allowLocalKits": False,
            "kit.requireSignature": True,
        })
        warnings, errors = settings.preflight(values, check_kit=False)
        self.assertEqual(warnings, [])
        self.assertEqual(errors, [])

    def test_non_empty_no_proxy_sandbox_is_an_error(self):
        values = dict(self.DEFAULTS, **{"no_proxy.sandbox": "example.com"})
        warnings, errors = settings.preflight(values)
        self.assertEqual(warnings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("no_proxy.sandbox", errors[0])
        self.assertIn("example.com", errors[0])

    def test_non_empty_proxy_sandbox_is_an_error(self):
        values = dict(self.DEFAULTS, **{"proxy.sandbox": "http://proxy:8080"})
        warnings, errors = settings.preflight(values)
        self.assertEqual(warnings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("proxy.sandbox", errors[0])

    def test_no_proxy_sandbox_check_is_not_gated_by_check_kit(self):
        values = dict(self.DEFAULTS, **{"no_proxy.sandbox": "example.com"})
        warnings, errors = settings.preflight(values, check_kit=False)
        self.assertEqual(len(errors), 1)

    def test_multiple_simultaneous_problems_are_all_reported(self):
        values = dict(self.DEFAULTS, **{
            "ssh.agentForwardingEnabled": True,
            "kit.allowLocalKits": False,
            "kit.requireSignature": True,
            "no_proxy.sandbox": "example.com",
            "proxy.sandbox": "http://proxy:8080",
        })
        warnings, errors = settings.preflight(values)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len(errors), 4)

    def test_missing_keys_are_treated_as_unset_not_a_crash(self):
        self.assertEqual(settings.preflight({}), ([], []))


if __name__ == "__main__":
    unittest.main()
