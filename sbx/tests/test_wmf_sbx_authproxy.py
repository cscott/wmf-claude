#!/usr/bin/env python3
"""Unit tests for the shared login behind the host egress proxy
(authproxy.py) -- run with:
  python3 -m unittest discover -s sbx/tests -v

nono's OAuth capture ran on the engineer's host (track L5, sbx/NOTES.md,
"Shared login"); here nothing runs nono, and the guest is a fake."""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.authproxy as authproxy  # noqa: E402
import wmf_sbx.egress as egress  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


def login(expires, access="sk-ant-oat01-phantom", refresh="nono_phantom"):
    return {"accessToken": access, "refreshToken": refresh, "expiresAt": expires,
            "scopes": ["user:inference", "user:profile"], "subscriptionType": None}


class Done:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


class Guest:
    """A guest's ~/.claude/.credentials.json, through FakeLima-style calls."""

    def __init__(self, text=None, rc=0):
        self.text, self.rc, self.writes = text, rc, []

    def call(self, *args, check=True, capture=True, input=None, **kw):
        script = args[args.index("-c") + 1] if "-c" in args else ""
        if script == authproxy.WRITE_GUEST:
            self.writes.append(input)
            self.text = input
            return Done()
        if self.rc:
            return Done(returncode=self.rc)
        if self.text is None:
            return Done(returncode=3)
        return Done(stdout=self.text)


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
                    "HOME": self.tmp, "WMF_SBX_HOST_LOGIN": "0"}


class ChooseTests(Base):

    def token_file(self):
        path = os.path.join(self.tmp, "config", "wmf-sbx", "claude-oauth-token")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("sk-ant-oat01-x\n")
        os.chmod(path, 0o600)

    def test_the_flag_wins(self):
        self.token_file()
        self.assertTrue(authproxy.choose(True, True, self.env))
        self.assertFalse(authproxy.choose(False, True, self.env))
        self.assertFalse(authproxy.choose(False, False, self.env))

    def test_the_flag_needs_the_egress_proxy(self):
        with self.assertRaisesRegex(authproxy.AuthProxyError, "needs the host egress proxy"):
            authproxy.choose(True, False, self.env)

    def test_by_default_behind_the_egress_proxy_without_a_token(self):
        self.assertTrue(authproxy.choose(None, True, self.env))
        self.assertFalse(authproxy.choose(None, False, self.env))

    def test_an_inference_token_keeps_the_credential_proxy(self):
        self.token_file()
        self.assertFalse(authproxy.choose(None, True, self.env))

    def test_an_api_key_or_a_token_variable_does_too(self):
        for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            with self.subTest(var=var):
                self.assertFalse(authproxy.choose(None, True, dict(self.env, **{var: "x"})))

    def test_the_hosts_own_login_does_not_count(self):
        path = os.path.join(self.tmp, ".claude", ".credentials.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump({"claudeAiOauth": login(9 ** 15, access="real")}, f)
        env = dict(self.env)
        del env["WMF_SBX_HOST_LOGIN"]
        self.assertTrue(authproxy.choose(None, True, env))


class ProfileTests(Base):

    def test_the_profile_adds_the_provider_and_the_route(self):
        path = authproxy.write_profile(self.env)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(authproxy.auth_dir(self.env)).st_mode), 0o700)
        with open(path) as f:
            doc = json.load(f)
        with open(egress.PROFILE) as f:
            base = json.load(f)
        for key in base:
            if key not in ("credential_providers", "credential_routes"):
                self.assertEqual(doc[key], base[key])
        self.assertEqual(doc["credential_providers"]["claude_code"], authproxy.PROVIDER)
        (route,) = [r for r in doc["credential_routes"] if r["name"] == "anthropic_oauth"]
        policy = route["endpoint_policy"]
        self.assertEqual(policy["default"], {"decision": "allow"})
        denied = [d["path"] for d in policy["deny"]]
        self.assertIn("/v1/mcp_servers", denied)
        self.assertIn("/mcp-registry/**", denied)

    def test_an_auth_proxy_sandbox_gets_it_and_the_shared_state(self):
        state_mod.save({"name": "demo", authproxy.STATE_KEY: True}, self.env)
        config = egress.proxy_config("demo", self.env)
        argv = config["argv"]
        self.assertEqual(argv[argv.index("--profile") + 1], authproxy.profile_file(self.env))
        self.assertEqual(config["env"]["XDG_STATE_HOME"], authproxy.nono_state_dir(self.env))
        # the measurements' variable still wins
        argv = egress.proxy_argv("demo", dict(self.env, WMF_SBX_EGRESS_PROFILE="/x.json"))
        self.assertEqual(argv[argv.index("--profile") + 1], "/x.json")

    def test_other_sandboxes_keep_their_own(self):
        state_mod.save({"name": "demo"}, self.env)
        config = egress.proxy_config("demo", self.env)
        argv = config["argv"]
        self.assertEqual(argv[argv.index("--profile") + 1], egress.PROFILE)
        self.assertEqual(config["env"]["XDG_STATE_HOME"],
                         os.path.join(egress.egress_dir("demo", self.env), "state"))
        self.assertFalse(os.path.exists(authproxy.auth_dir(self.env)))


class PhantomTests(unittest.TestCase):

    def test_phantoms_are_kept_and_nothing_else(self):
        doc = {"claudeAiOauth": dict(login(5), extra="x"), "mcpOAuth": {"s": "secret"}}
        self.assertEqual(authproxy.phantoms(doc), login(5))

    def test_a_real_login_is_not_copied(self):
        for oauth in (login(5, refresh="sk-ant-ort01-real"), login(5, access="real"),
                      login("5"), login(True), {}):
            with self.subTest(oauth=oauth):
                self.assertIsNone(authproxy.phantoms({"claudeAiOauth": oauth}))
        for doc in (None, [], {"claudeAiOauth": "x"}):
            self.assertIsNone(authproxy.phantoms(doc))


class SyncTests(Base):

    def sync(self, guest):
        return authproxy.sync("demo", lima=guest, env=self.env)

    def test_a_login_in_the_guest_goes_to_the_host(self):
        guest = Guest(json.dumps({"claudeAiOauth": login(10), "mcpOAuth": {"s": "x"}}))
        self.assertEqual(self.sync(guest), "pulled")
        self.assertEqual(authproxy.read_host(self.env), login(10))
        path = authproxy.credentials_file(self.env)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertNotIn("mcpOAuth", f.read())
        self.assertIsNone(self.sync(guest))      # the same: nothing to do

    def test_the_hosts_newer_login_goes_to_the_guest_and_keeps_the_rest(self):
        authproxy.write_host(login(20, access="sk-ant-oat01-new"), self.env)
        guest = Guest(json.dumps({"claudeAiOauth": dict(login(10), extra=1),
                                  "mcpOAuth": {"s": "x"}}))
        self.assertEqual(self.sync(guest), "pushed")
        doc = json.loads(guest.writes[-1])
        self.assertEqual(doc["mcpOAuth"], {"s": "x"})
        self.assertEqual(doc["claudeAiOauth"]["accessToken"], "sk-ant-oat01-new")
        self.assertEqual(doc["claudeAiOauth"]["expiresAt"], 20)
        self.assertEqual(doc["claudeAiOauth"]["extra"], 1)

    def test_a_new_sandbox_gets_the_login(self):
        authproxy.write_host(login(20), self.env)
        guest = Guest(None)
        self.assertEqual(self.sync(guest), "pushed")
        self.assertEqual(json.loads(guest.writes[-1]), {"claudeAiOauth": login(20)})

    def test_a_real_login_in_the_guest_is_replaced_not_copied(self):
        authproxy.write_host(login(20), self.env)
        guest = Guest(json.dumps({"claudeAiOauth": login(99, refresh="sk-ant-ort01-r")}))
        self.assertEqual(self.sync(guest), "pushed")
        self.assertEqual(authproxy.read_host(self.env), login(20))

    def test_nothing_without_a_login_or_a_guest(self):
        self.assertIsNone(self.sync(Guest(None)))
        authproxy.write_host(login(20), self.env)
        self.assertIsNone(self.sync(Guest(rc=255)))       # a stopped VM
        self.assertIsNone(self.sync(Guest("x" * (authproxy.MAX_GUEST_FILE + 1))))

    def test_the_syncer_syncs_at_start_and_stop(self):
        authproxy.write_host(login(20), self.env)
        guest = Guest(None)
        syncer = authproxy.Syncer("demo", lima=guest, env=self.env, interval=3600)
        self.assertEqual(syncer.start(), "pushed")
        guest.text = json.dumps({"claudeAiOauth": login(30)})
        self.assertEqual(syncer.stop(), "pulled")
        self.assertEqual(authproxy.read_host(self.env)["expiresAt"], 30)

    def touch(self, path, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("1\n")
        os.utime(path, (mtime, mtime))

    def test_a_proxy_older_than_the_store_is_restarted(self):
        # nono reads its capture store only when it starts (L6).
        swapped = []

        def swap(name, env=None):
            swapped.append(name)
            self.touch(egress.port_file(name, env), 300 * len(swapped))
            return 4242
        self.assertFalse(authproxy.refresh_proxy("demo", self.env, swap=swap))  # no files
        self.touch(egress.port_file("demo", self.env), 100)
        self.touch(authproxy.store_file(self.env), 200)
        self.assertTrue(authproxy.proxy_stale("demo", self.env))
        self.assertTrue(authproxy.refresh_proxy("demo", self.env, swap=swap))
        self.assertFalse(authproxy.refresh_proxy("demo", self.env, swap=swap))
        self.assertEqual(swapped, ["demo"])
        # through the syncer, before the phantoms go in
        self.touch(authproxy.store_file(self.env), 400)
        authproxy.write_host(login(20), self.env)
        guest = Guest(None)
        syncer = authproxy.Syncer("demo", lima=guest, env=self.env, interval=3600, swap=swap)
        self.assertEqual(syncer.start(), "pushed")
        syncer.stop()
        self.assertEqual(swapped, ["demo", "demo"])

    def test_the_store_is_where_nono_puts_it(self):
        self.assertEqual(authproxy.store_file(self.env),
                         os.path.join(authproxy.nono_state_dir(self.env), "nono",
                                      "oauth-capture", "providers.json"))

    def test_a_failed_sync_does_not_end_the_session(self):
        class Broken:
            def call(self, *a, **kw):
                raise OSError("gone")
        logs = []
        syncer = authproxy.Syncer("demo", lima=Broken(), env=self.env, interval=3600,
                                  log=logs.append)
        self.assertIsNone(syncer.start())
        syncer.stop()
        self.assertTrue(logs and "gone" in logs[0])


if __name__ == "__main__":
    unittest.main()
