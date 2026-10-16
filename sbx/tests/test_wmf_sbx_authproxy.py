#!/usr/bin/env python3
"""Unit tests for the shared login behind the host egress proxy
(authproxy.py) -- run with:
  python3 -m unittest discover -s sbx/tests -v

nono's OAuth capture ran on the engineer's host (track L5, sbx/NOTES.md,
"Shared login"); here nothing runs nono, and the guest is a fake."""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.authproxy as authproxy  # noqa: E402
import wmf_sbx.egress as egress  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


HOUR_MS = 3600 * 1000


def now_ms():
    return int(time.time() * 1000)


def login(expires, access="sk-ant-oat01-phantom", refresh="nono_phantom"):
    return {"accessToken": access, "refreshToken": refresh, "expiresAt": expires,
            "scopes": ["user:inference", "user:profile"], "subscriptionType": None}


class Done:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


class Guest:
    """A guest's ~/.claude/.credentials.json, through FakeLima-style calls."""

    def __init__(self, text=None, rc=0, refreshed=None):
        self.text, self.rc, self.writes = text, rc, []
        self.refreshed, self.refresh_inputs = refreshed, []
        self.lock = threading.Lock()

    def call(self, *args, **kw):
        with self.lock:
            return self._call(*args, **kw)

    def _call(self, *args, check=True, capture=True, input=None, **kw):
        script = args[args.index("-c") + 1] if "-c" in args else ""
        if script == authproxy.WRITE_GUEST:
            self.writes.append(input)
            self.text = input
            return Done()
        if script == authproxy.REFRESH_SCRIPT:
            self.refresh_inputs.append(input)
            return Done(stdout=self.refreshed or "")
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

    def written(self, guest):
        return json.loads(guest.writes[-1])

    def test_a_login_in_the_guest_goes_to_the_host_and_stops_refreshing_there(self):
        new = login(now_ms() + 8 * HOUR_MS)
        guest = Guest(json.dumps({"claudeAiOauth": new, "mcpOAuth": {"s": "x"}}))
        self.assertEqual(self.sync(guest), "pulled")
        self.assertEqual(authproxy.read_host(self.env), new)
        path = authproxy.credentials_file(self.env)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertNotIn("mcpOAuth", f.read())
        # the guest's copy now expires a year later: Claude Code there
        # does not refresh it
        doc = self.written(guest)
        self.assertEqual(doc["claudeAiOauth"]["expiresAt"],
                         new["expiresAt"] + authproxy.GUEST_EXTRA_MS)
        self.assertEqual(doc["mcpOAuth"], {"s": "x"})
        self.assertIsNone(self.sync(guest))      # the same: nothing to do

    def test_the_hosts_login_goes_to_the_guest_and_keeps_the_rest(self):
        host = login(now_ms() + HOUR_MS, access="sk-ant-oat01-new")
        authproxy.write_host(host, self.env)
        old = dict(login(now_ms() + 30 * 60 * 1000), extra=1)   # an older one
        guest = Guest(json.dumps({"claudeAiOauth": old, "mcpOAuth": {"s": "x"}}))
        self.assertEqual(self.sync(guest), "pushed")
        doc = self.written(guest)
        self.assertEqual(doc["mcpOAuth"], {"s": "x"})
        self.assertEqual(doc["claudeAiOauth"]["accessToken"], "sk-ant-oat01-new")
        self.assertEqual(doc["claudeAiOauth"]["extra"], 1)
        self.assertEqual(authproxy.read_host(self.env), host)

    def test_a_guests_far_expiry_is_never_taken_as_new(self):
        # After the leader's refresh the guest still has the old phantoms,
        # with the far expiry that sync wrote.
        host = login(now_ms() + 8 * HOUR_MS, access="sk-ant-oat01-new")
        authproxy.write_host(host, self.env)
        stale = authproxy.for_guest(login(now_ms() + HOUR_MS))
        guest = Guest(json.dumps({"claudeAiOauth": stale}))
        self.assertEqual(self.sync(guest), "pushed")
        self.assertEqual(authproxy.read_host(self.env), host)

    def test_a_new_sandbox_gets_the_login(self):
        host = login(now_ms() + HOUR_MS)
        authproxy.write_host(host, self.env)
        guest = Guest(None)
        self.assertEqual(self.sync(guest), "pushed")
        self.assertEqual(self.written(guest), {"claudeAiOauth": authproxy.for_guest(host)})

    def test_a_real_login_in_the_guest_is_replaced_not_copied(self):
        host = login(now_ms() + HOUR_MS)
        authproxy.write_host(host, self.env)
        real = login(now_ms() + 8 * HOUR_MS, access="sk-ant-oat01-r", refresh="sk-ant-ort01-r")
        guest = Guest(json.dumps({"claudeAiOauth": real}))
        self.assertEqual(self.sync(guest), "pushed")
        self.assertEqual(authproxy.read_host(self.env), host)

    def test_nothing_without_a_login_or_a_guest(self):
        self.assertIsNone(self.sync(Guest(None)))
        authproxy.write_host(login(now_ms()), self.env)
        self.assertIsNone(self.sync(Guest(rc=255)))       # a stopped VM
        self.assertIsNone(self.sync(Guest("x" * (authproxy.MAX_GUEST_FILE + 1))))

    def test_refresh_in_the_guest(self):
        host = login(now_ms() + HOUR_MS)
        new = login(now_ms() + 8 * HOUR_MS, access="sk-ant-oat01-new", refresh="nono_new")
        guest = Guest(refreshed=json.dumps({"claudeAiOauth": new}))
        self.assertEqual(authproxy.refresh_in_guest("demo", host, lima=guest, env=self.env),
                         new)
        sent = json.loads(guest.refresh_inputs[0])["claudeAiOauth"]
        self.assertEqual(sent, dict(host, expiresAt=0))
        # no new tokens, no output, or a far expiry: no refresh
        for out in (json.dumps({"claudeAiOauth": host}), "", "garbage",
                    json.dumps({"claudeAiOauth": authproxy.for_guest(new)})):
            guest = Guest(refreshed=out)
            self.assertIsNone(authproxy.refresh_in_guest("demo", host, lima=guest,
                                                         env=self.env))

    @unittest.skipUnless(shutil.which("jq"), "needs jq")
    def test_the_refresh_script_returns_as_soon_as_the_login_is_new(self):
        # A fake `claude` that refreshes after 0.3 s and then would take
        # 30 s over its turn.
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        new = login(now_ms() + 8 * HOUR_MS, access="sk-ant-oat01-new", refresh="nono_new")
        with open(os.path.join(bin_dir, "claude"), "w") as f:
            f.write("#!/bin/sh\nsleep 0.3\n"
                    f"printf '%s' '{json.dumps({'claudeAiOauth': new})}' "
                    "> \"$CLAUDE_CONFIG_DIR/.credentials.json\"\nsleep 30\n")
        os.chmod(os.path.join(bin_dir, "claude"), 0o755)
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"])
        start = time.monotonic()
        res = subprocess.run(["sh", "-c", authproxy.REFRESH_SCRIPT, "_", "100"], env=env,
                             input=json.dumps({"claudeAiOauth": dict(login(1), expiresAt=0)}),
                             capture_output=True, text=True, timeout=20)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(authproxy.phantoms(json.loads(res.stdout)), new)

    @unittest.skipUnless(shutil.which("jq"), "needs jq")
    def test_the_refresh_script_gives_up(self):
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "claude"), "w") as f:
            f.write("#!/bin/sh\nsleep 30\n")
        os.chmod(os.path.join(bin_dir, "claude"), 0o755)
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"])
        start = time.monotonic()
        old = {"claudeAiOauth": dict(login(1), expiresAt=0)}
        res = subprocess.run(["sh", "-c", authproxy.REFRESH_SCRIPT, "_", "5"], env=env,
                             input=json.dumps(old), capture_output=True, text=True,
                             timeout=20)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(json.loads(res.stdout), old)      # no refresh: the input back

    def test_the_refresher_does_not_restart_its_own_proxy(self):
        authproxy.write_host(login(now_ms() + 60 * 1000), self.env)
        new = login(now_ms() + 8 * HOUR_MS, access="sk-ant-oat01-new", refresh="nono_new")
        self.touch(egress.port_file("a", self.env), 100)
        swapped = []

        def refresher(name, host, lima=None, env=None):
            self.touch(authproxy.store_file(self.env), time.time())   # the capture
            return new
        a = authproxy.Coordinator("a", lima=Guest(None), env=self.env, refresher=refresher,
                                  swap=lambda n, e=None: swapped.append(n), backstop=3600,
                                  rng=lambda lo, hi: 0)
        a.start()
        a.stop()
        self.assertEqual(swapped, [])
        self.assertEqual(authproxy.read_host(self.env), new)

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

    def test_mark_fresh_is_not_before_the_store(self):
        # A store time after the clock (the coarse-clock case, made large).
        self.touch(egress.port_file("demo", self.env), 100)
        self.touch(authproxy.store_file(self.env), time.time() + 60)
        authproxy.mark_fresh("demo", self.env)
        self.assertFalse(authproxy.proxy_stale("demo", self.env))

    def test_the_store_is_where_nono_puts_it(self):
        self.assertEqual(authproxy.store_file(self.env),
                         os.path.join(authproxy.nono_state_dir(self.env), "nono",
                                      "oauth-capture", "providers.json"))


def wait_for(cond, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class CoordinatorTests(Base):

    def setUp(self):
        super().setUp()
        self.logs = []

    def member(self, name, guest, refresher=None, **kw):
        kw.setdefault("rng", lambda a, b: 0)
        c = authproxy.Coordinator(name, lima=guest, env=self.env, log=self.logs.append,
                                  swap=lambda n, e=None: None,
                                  refresher=refresher or self.no_refresh, **kw)
        self.addCleanup(lambda: c._thread and c._thread.is_alive() and c.stop())
        return c

    def no_refresh(self, name, host, lima=None, env=None):
        raise AssertionError(f"{name} refreshed")

    def access(self, guest):
        with guest.lock:
            return guest.text and json.loads(guest.text)["claudeAiOauth"]["accessToken"]

    def sockets(self):
        return sorted(os.listdir(authproxy.members_dir(self.env)))

    def test_each_resume_listens_on_its_own_socket(self):
        authproxy.write_host(login(now_ms() + 8 * HOUR_MS), self.env)
        a = self.member("a", Guest(None))
        b = self.member("b", Guest(None))
        a.start()
        b.start()
        self.assertEqual(len(self.sockets()), 2)
        self.assertEqual(stat.S_IMODE(os.stat(a.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(authproxy.members_dir(self.env)).st_mode),
                         0o700)
        a.stop()
        self.assertEqual(self.sockets(), [os.path.basename(b.path)])
        b.stop()
        self.assertEqual(self.sockets(), [])

    def test_a_dead_resumes_socket_is_removed(self):
        d = authproxy.members_dir(self.env)
        os.makedirs(d)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(os.path.join(d, "x.1.2.sock"))
        dead.close()                      # the file stays; nothing listens
        self.assertEqual(authproxy.notify(self.env), 0)
        self.assertEqual(self.sockets(), [])

    def test_an_expiring_login_is_refreshed_before_the_session(self):
        old = login(now_ms() + 5 * 60 * 1000)           # inside the margin
        new = login(now_ms() + 8 * HOUR_MS, access="sk-ant-oat01-new", refresh="nono_new")
        authproxy.write_host(old, self.env)
        refreshed = []

        def refresher(name, host, lima=None, env=None):
            refreshed.append((name, host))
            return new
        a = self.member("a", Guest(None), refresher=refresher, backstop=3600)
        a.start()
        self.assertEqual(refreshed, [("a", old)])
        self.assertEqual(authproxy.read_host(self.env), new)
        self.assertEqual(json.loads(a.lima.text)["claudeAiOauth"], authproxy.for_guest(new))
        a.stop()

    def test_one_refresh_and_the_others_get_it_without_polling(self):
        host = login(now_ms() + 8 * HOUR_MS)
        authproxy.write_host(host, self.env)
        refreshed = []
        lock = threading.Lock()

        def refresher(name, h, lima=None, env=None):
            with lock:
                refreshed.append(name)
                n = len(refreshed)
            time.sleep(0.2)               # a model turn
            return login(now_ms() + 8 * HOUR_MS, access=f"sk-ant-oat01-n{n}",
                         refresh=f"nono_n{n}")
        members = [self.member(n, Guest(None), refresher=refresher, backstop=3600,
                               recheck=3600) for n in "abc"]
        for m in members:
            m.start()
        writes = [len(m.lima.writes) for m in members]
        # The login nears its expiry: every timer fires at once (no jitter
        # here); one refreshes, the others find the new login under the lock.
        authproxy.write_host(login(now_ms() + 60 * 1000), self.env)
        for m in members:
            m._wake_w.send(b"x")
        self.assertTrue(wait_for(lambda: all(self.access(m.lima) == "sk-ant-oat01-n1"
                                              for m in members)))
        time.sleep(0.3)
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(sum(m.refreshed for m in members), 1)
        for m, w in zip(members, writes):
            self.assertGreater(len(m.lima.writes), w)
            m.stop()

    def test_a_refresher_that_dies_does_not_stop_the_refresh(self):
        # Another process holds the lock and dies without a refresh: the
        # next resume's timer takes the lock and refreshes.
        import fcntl
        authproxy.write_host(login(now_ms() + 60 * 1000), self.env)
        authproxy._make_dir(self.env)
        fd = os.open(os.path.join(authproxy.auth_dir(self.env), "refresh.lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        new = login(now_ms() + 8 * HOUR_MS, access="sk-ant-oat01-new", refresh="nono_new")
        a = self.member("a", Guest(None), refresher=lambda *x, **k: new, backstop=3600)
        threading.Timer(0.3, os.close, (fd,)).start()   # the holder dies
        a.start()
        self.assertEqual(authproxy.read_host(self.env), new)
        a.stop()

    def test_the_timers_are_spread(self):
        exp = now_ms() + 8 * HOUR_MS
        authproxy.write_host(login(exp), self.env)
        a = self.member("a", Guest(None), rng=lambda lo, hi: hi)
        b = self.member("b", Guest(None), rng=lambda lo, hi: lo)
        base = (exp - authproxy.REFRESH_MARGIN_MS - now_ms()) / 1000
        self.assertAlmostEqual(a._due() - base, 30, delta=1)
        self.assertAlmostEqual(b._due() - base, -30, delta=1)

    def test_a_login_in_one_reaches_the_others(self):
        a = self.member("a", Guest(None), backstop=3600, recheck=3600)
        b = self.member("b", Guest(None), backstop=0.2)
        c = self.member("c", Guest(None), backstop=3600, recheck=3600)
        for m in (a, b, c):
            m.start()
        new = login(now_ms() + 8 * HOUR_MS, access="sk-ant-oat01-login", refresh="nono_l")
        with b.lima.lock:
            b.lima.text = json.dumps({"claudeAiOauth": new})       # /login in b
        self.assertTrue(wait_for(lambda: self.access(c.lima) == "sk-ant-oat01-login"))
        self.assertTrue(wait_for(lambda: self.access(a.lima) == "sk-ant-oat01-login"))
        self.assertEqual(authproxy.read_host(self.env), new)
        for m in (a, b, c):
            m.stop()

    def test_the_margin_can_be_set_on_the_host(self):
        a = authproxy.Coordinator("a", env=dict(self.env, WMF_SBX_AUTH_MARGIN_MINUTES="472"))
        self.assertEqual(a.margin_ms, 472 * 60 * 1000)
        self.assertEqual(authproxy.Coordinator("a", env=self.env).margin_ms,
                         authproxy.REFRESH_MARGIN_MS)

    def test_a_failed_refresh_waits_and_warns(self):
        authproxy.write_host(login(now_ms() + 60 * 1000), self.env)
        calls = []

        def refresher(name, host, lima=None, env=None):
            calls.append(name)
            return None
        a = self.member("a", Guest(None), refresher=refresher, retry=3600)
        a.start()
        time.sleep(0.3)
        a.stop()
        self.assertEqual(calls, ["a"])
        self.assertTrue(any("could not refresh" in m for m in self.logs))

    def test_a_held_lock_is_waited_for_then_given_up(self):
        import fcntl
        authproxy.write_host(login(now_ms() + 60 * 1000), self.env)
        authproxy._make_dir(self.env)
        fd = os.open(os.path.join(authproxy.auth_dir(self.env), "refresh.lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        a = self.member("a", Guest(None), lock_wait=0.3, retry=3600)
        a.start()
        a.stop()
        self.assertTrue(any("refresh lock" in m for m in self.logs))

    def test_a_failed_sync_does_not_end_the_session(self):
        class Broken:
            def call(self, *a, **kw):
                raise OSError("gone")
        a = self.member("a", Broken())
        self.assertIsNone(a.start())
        a.stop()
        self.assertTrue(self.logs and "gone" in self.logs[0])


if __name__ == "__main__":
    unittest.main()
