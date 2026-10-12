#!/usr/bin/env python3
"""Unit tests for `wmf-sbx login` (login.py, track L) -- run with:
  python3 -m unittest discover -s sbx/tests -v"""

import os
import stat
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.credproxy as credproxy  # noqa: E402
import wmf_sbx.login as login  # noqa: E402
from test_wmf_sbx_lifecycle import Done, Env, FakeLima  # noqa: E402

TOKEN = "sk-ant-oat01-" + "A1b2_C3-" * 12


class LoginLima(FakeLima):
    """FakeLima whose VM prints `transcript` for the setup-token log."""

    def __init__(self, transcript="", setup_rc=0, **kw):
        super().__init__(**kw)
        self.transcript = transcript
        self.setup_rc = setup_rc

    def call(self, *args, **kw):
        joined = " ".join(str(a) for a in args)
        if args[0] == "shell" and "claude setup-token" in joined:
            self.calls.append(("call",) + tuple(args))
            self.interactive = kw.get("interactive")
            return Done(returncode=self.setup_rc)
        if args[0] == "shell" and f"cat {login.TRANSCRIPT}" in joined:
            self.calls.append(("call",) + tuple(args))
            return Done(stdout=self.transcript)
        return super().call(*args, **kw)


class FindTokenTests(unittest.TestCase):

    def test_the_token_is_found_among_terminal_escapes(self):
        text = (f"\x1b[?25l\x1b[2K\x1b[1G Your token:\r\n\x1b[32m{TOKEN}\x1b[39m\r\n"
                "\x1b]0;title\x07Store it securely.")
        self.assertEqual(login.find_token(text), TOKEN)

    def test_an_osc8_hyperlink_does_not_hide_the_token(self):
        url = "https://claude.com/cai/oauth/authorize?code=true&scope=user%3Ainference"
        text = (f"\x1b]8;id=x;{url}\x1b\\{url}\x1b]8;;\x1b\\\r\n"
                f"Paste code here if prompted >\r\n\x1b[>0q{TOKEN}\r\n")
        self.assertEqual(login.find_token(text), TOKEN)

    def test_no_token(self):
        self.assertIsNone(login.find_token("Error: cancelled\r\n"))


class LoginTests(Env):

    def run_login(self, lima, ask=None):
        asked = []

        def fake_ask(prompt):
            asked.append(prompt)
            return ask or ""
        rc = self.quiet(login.main, [], lima=lima, env=self.env, ask=fake_ask,
                        isatty=lambda: True)
        return rc, asked

    def created(self, lima):
        return [c[1] for c in lima.calls if c[0] == "create"]

    def test_the_token_is_saved_0600_and_the_vm_deleted(self):
        lima = LoginLima(transcript=f"Your OAuth token:\r\n{TOKEN}\r\n")
        rc, asked = self.run_login(lima)
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(asked, [])
        path = credproxy.token_file(self.env)
        with open(path) as f:
            self.assertEqual(f.read(), TOKEN + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        (inst,) = self.created(lima)
        self.assertTrue(inst.startswith("wmf-sbx-login-"))
        self.assertEqual(lima.calls[-1], ("delete", inst))
        self.assertTrue(lima.interactive)
        # the token is never on a command line
        self.assertFalse(any(TOKEN in str(c) for c in lima.calls))
        # and resume takes it
        self.assertEqual(credproxy.host_source(self.env).value(), f"Bearer {TOKEN}")

    def test_the_vm_has_no_mounts(self):
        lima = LoginLima(transcript=TOKEN)
        self.run_login(lima)
        (inst,) = self.created(lima)
        self.assertFalse(lima.templates[inst].get("mounts"))

    def test_a_token_that_cannot_be_read_is_asked_for(self):
        rc, asked = self.run_login(LoginLima(transcript="garbled"), ask=TOKEN)
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(len(asked), 1)
        with open(credproxy.token_file(self.env)) as f:
            self.assertEqual(f.read().strip(), TOKEN)

    def test_a_pasted_non_token_is_refused(self):
        rc, _ = self.run_login(LoginLima(transcript="garbled"), ask="hunter2")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(credproxy.token_file(self.env)))

    def test_a_failed_setup_token_saves_nothing_and_deletes_the_vm(self):
        lima = LoginLima(setup_rc=1)
        rc, _ = self.run_login(lima)
        self.assertEqual(rc, 1)
        self.assertIn("setup-token failed", self.err)
        self.assertFalse(os.path.exists(credproxy.token_file(self.env)))
        (inst,) = self.created(lima)
        self.assertEqual(lima.calls[-1], ("delete", inst))

    def test_an_existing_token_is_replaced(self):
        path = credproxy.token_file(self.env)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("old\n")
        self.assertEqual(self.run_login(LoginLima(transcript=TOKEN))[0], 0, self.err)
        with open(path) as f:
            self.assertEqual(f.read().strip(), TOKEN)

    def test_no_terminal_no_vm(self):
        lima = LoginLima(transcript=TOKEN)
        rc = self.quiet(login.main, [], lima=lima, env=self.env, isatty=lambda: False)
        self.assertEqual(rc, 2)
        self.assertIn("needs a terminal", self.err)
        self.assertEqual(lima.calls, [])

    def test_sigterm_during_the_login_still_deletes_the_vm(self):
        import signal

        class Killed(LoginLima):
            def call(self, *args, **kw):
                if args[0] == "shell" and "claude setup-token" in " ".join(map(str, args)):
                    signal.raise_signal(signal.SIGTERM)
                return super().call(*args, **kw)
        lima = Killed(transcript=TOKEN)
        rc, _ = self.run_login(lima)
        self.assertEqual(rc, 130)
        self.assertFalse(os.path.exists(credproxy.token_file(self.env)))
        (inst,) = self.created(lima)
        self.assertEqual(lima.calls[-1], ("delete", inst))
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_no_cached_image(self):
        import shutil
        import wmf_sbx.image as image
        shutil.rmtree(image.cache_root(self.env))
        lima = LoginLima()
        rc, _ = self.run_login(lima)
        self.assertEqual(rc, 1)
        self.assertIn("no golden image", self.err)
        self.assertEqual(self.created(lima), [])


if __name__ == "__main__":
    unittest.main()
