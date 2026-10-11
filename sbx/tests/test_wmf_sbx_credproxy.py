#!/usr/bin/env python3
"""Unit tests for the host credential proxy (credproxy.py, track L) -- run
with:
  python3 -m unittest discover -s sbx/tests -v

The proxy ran against api.anthropic.com in L1 (sbx/NOTES.md, "Shared
login"); here it forwards to a fake upstream on the loopback."""

import http.client
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.credproxy as cp  # noqa: E402

REAL_KEY = "sk-test-real-key"
REAL_TOKEN = "oauth-real-token"


class Done:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class SourceTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"HOME": self.tmp.name}

    def write(self, path, text, mode=0o600):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)

    def login(self, token=REAL_TOKEN, expires_ms=None):
        oauth = {"accessToken": token}
        if expires_ms is not None:
            oauth["expiresAt"] = expires_ms
        self.write(cp.claude_credentials_file(self.env), json.dumps({"claudeAiOauth": oauth}))

    def source(self, **kw):
        return cp.host_source(self.env, platform="linux", **kw)

    def test_nothing_on_the_host(self):
        self.assertIsNone(self.source())

    def test_the_variables_come_first(self):
        self.login()
        self.env.update(ANTHROPIC_API_KEY=REAL_KEY, CLAUDE_CODE_OAUTH_TOKEN="t")
        s = self.source()
        self.assertEqual((s.kind, s.header, s.value()), ("api-key", "x-api-key", REAL_KEY))
        self.assertEqual(s.phantom_var, "ANTHROPIC_API_KEY")
        self.env.pop("ANTHROPIC_API_KEY")
        s = self.source()
        self.assertEqual((s.header, s.value()), ("authorization", "Bearer t"))
        self.assertEqual(s.phantom_var, "CLAUDE_CODE_OAUTH_TOKEN")

    def test_the_token_file_before_the_login(self):
        self.login()
        self.write(cp.token_file(self.env), " tok \n")
        self.assertEqual(self.source().value(), "Bearer tok")

    def test_a_token_file_others_can_read_is_refused(self):
        self.write(cp.token_file(self.env), "tok", mode=0o644)
        with self.assertRaisesRegex(cp.CredentialError, "chmod 600"):
            self.source()

    def test_the_host_login_is_read_again_for_each_request(self):
        self.login("first")
        s = self.source()
        self.assertEqual(s.value(), "Bearer first")
        self.login("refreshed")
        self.assertEqual(s.value(), "Bearer refreshed")
        self.assertIn(".credentials.json", s.describe)

    def test_an_expired_login_is_still_the_source_but_says_why(self):
        self.login(expires_ms=1000)
        s = self.source()
        self.assertIsNotNone(s)
        self.assertRegex(cp.check_source(s), "expired.*run `claude`")
        self.login(expires_ms=(2 ** 41))
        self.assertIsNone(cp.check_source(s))

    def test_the_login_can_be_left_out(self):
        self.login()
        self.env[cp.HOST_LOGIN_VAR] = "0"
        self.assertIsNone(self.source())

    def test_macos_reads_the_keychain_and_caches_it(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return Done(stdout=json.dumps({"claudeAiOauth": {"accessToken": "kc"}}))
        clock = [0.0]
        read = cp.live_login_reader(self.env, run=run, platform="darwin",
                                    clock=lambda: clock[0])
        self.assertEqual(read(), "kc")
        self.assertEqual(read(), "kc")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["security", "find-generic-password"])
        self.assertIn(cp.KEYCHAIN_SERVICE, calls[0])
        clock[0] = cp.KEYCHAIN_CACHE_SECONDS + 1
        read()
        self.assertEqual(len(calls), 2)

    def test_no_keychain_item_is_no_source(self):
        s = cp.host_source(self.env, platform="darwin",
                           run=lambda argv, **kw: Done(returncode=44))
        self.assertIsNone(s)


# -- the proxy, against a fake upstream ------------------------------------

class FakeUpstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def _serve(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeUpstream.seen.append((self.command, self.path,
                                  {k.lower(): v for k, v in self.headers.items()}, body))
        out = json.dumps({"ok": True, "path": self.path}).encode() * 3
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(out)

    do_GET = do_POST = do_HEAD = _serve


class ProxyTests(unittest.TestCase):

    def setUp(self):
        FakeUpstream.seen = []
        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "proxy.log")
        self.proxy = self.make_proxy(cp.Source("api-key", "test", lambda: REAL_KEY))

    def make_proxy(self, source):
        proxy = cp.CredentialProxy(source, env={}, log_path=self.log)
        port = self.upstream.server_address[1]
        proxy.connect = lambda: http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def request(self, method, path, headers=None, body=None, proxy=None):
        proxy = proxy or self.proxy
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_the_phantom_is_swapped_for_the_real_key(self):
        status, data = self.request("POST", "/v1/messages?beta=true", body=b'{"m":1}',
                                    headers={"x-api-key": self.proxy.phantom,
                                             "content-type": "application/json"})
        self.assertEqual(status, 200)
        self.assertIn(b'"ok": true', data)
        (method, path, headers, body), = FakeUpstream.seen
        self.assertEqual((method, path, body), ("POST", "/v1/messages?beta=true", b'{"m":1}'))
        self.assertEqual(headers["x-api-key"], REAL_KEY)
        self.assertEqual(headers["host"], cp.UPSTREAM)
        self.assertNotIn(self.proxy.phantom, json.dumps(headers))

    def test_a_bearer_phantom_gets_the_real_token(self):
        proxy = self.make_proxy(cp.Source("oauth", "t", lambda: REAL_TOKEN))
        status, _ = self.request("POST", "/v1/messages", body=b"{}", proxy=proxy,
                                 headers={"Authorization": f"Bearer {proxy.phantom}"})
        self.assertEqual(status, 200)
        self.assertEqual(FakeUpstream.seen[0][2]["authorization"], f"Bearer {REAL_TOKEN}")

    def test_no_phantom_or_a_wrong_one_is_refused_and_not_forwarded(self):
        for headers in ({}, {"x-api-key": "guess"}, {"Authorization": "Bearer guess"}):
            with self.subTest(headers=headers):
                status, data = self.request("POST", "/v1/messages", headers=headers, body=b"{}")
                self.assertEqual(status, 403)
                self.assertIn(b"no session credential", data)
        self.assertEqual(FakeUpstream.seen, [])

    def test_only_the_api_paths_are_forwarded(self):
        status, _ = self.request("GET", "/api/oauth/profile",
                                 headers={"x-api-key": self.proxy.phantom})
        self.assertEqual(status, 403)
        self.assertEqual(FakeUpstream.seen, [])

    def test_the_probe_goes_through_without_a_credential(self):
        status, data = self.request("HEAD", "/api/hello")
        self.assertEqual((status, data), (200, b""))
        (_m, _p, headers, _b), = FakeUpstream.seen
        self.assertNotIn("x-api-key", headers)

    def test_a_credential_problem_is_a_401_that_says_why(self):
        def expired():
            raise cp.CredentialError("the host's Claude Code login has expired")
        proxy = self.make_proxy(cp.Source("oauth", "t", expired))
        status, data = self.request("POST", "/v1/messages", body=b"{}", proxy=proxy,
                                    headers={"x-api-key": proxy.phantom})
        self.assertEqual(status, 401)
        self.assertIn(b"expired", data)

    def test_the_log_has_no_credential(self):
        self.request("POST", "/v1/messages", body=b"secret body",
                     headers={"x-api-key": self.proxy.phantom})
        text = ""
        for _ in range(50):   # the proxy logs after the last byte is sent
            if os.path.exists(self.log):
                with open(self.log) as f:
                    text = f.read()
            if text:
                break
            time.sleep(0.05)
        self.assertIn("POST /v1/messages 200", text)
        for s in (REAL_KEY, self.proxy.phantom, "secret body"):
            self.assertNotIn(s, text)

    def test_the_session_env(self):
        env = self.proxy.session_env()
        self.assertEqual(env, {"ANTHROPIC_API_KEY": self.proxy.phantom,
                               "ANTHROPIC_BASE_URL": f"http://192.168.5.2:{self.proxy.port}"})

    def test_each_proxy_has_its_own_phantom(self):
        other = self.make_proxy(cp.Source("api-key", "t", lambda: REAL_KEY))
        self.assertNotEqual(other.phantom, self.proxy.phantom)
        self.assertGreaterEqual(len(self.proxy.phantom), 40)


# -- the guest's host block -------------------------------------------------

FAKE_NFT = r"""#!/bin/sh
# Records each call; `-a list chain` prints a rule per tagged port in $PORTS.
echo "$*" >> "$LOG"
if [ "$1" = "-a" ] || [ "$1" = "list" ]; then
  n=10
  for p in $PORTS; do
    n=$((n+1))
    echo "  ip daddr 192.168.5.2 tcp dport $p accept comment \"wmf-sbx-cred-$p\" # handle $n"
  done
  echo "  counter reject # handle 99"
fi
"""


class GuestRuleTests(unittest.TestCase):

    def run_guest(self, argv, ports=""):
        self.assertEqual(argv[:2], ["sudo", "-n"])
        with tempfile.TemporaryDirectory() as d:
            nft = os.path.join(d, "nft")
            with open(nft, "w") as f:
                f.write(FAKE_NFT)
            os.chmod(nft, 0o755)
            log = os.path.join(d, "log")
            env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", LOG=log, PORTS=ports)
            res = subprocess.run(argv[2:], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            calls = open(log).read().splitlines() if os.path.exists(log) else []
            return res.stdout, calls

    def test_the_ports_of_the_rules(self):
        out, _ = self.run_guest(cp.guest_rule_ports_argv(), ports="2345 1234")
        self.assertEqual(out.split(), ["1234", "2345"])

    def test_add_tags_a_rule_in_both_chains(self):
        _, calls = self.run_guest(cp.guest_rules_argv(add=[4321]))
        self.assertEqual(calls, [
            f'insert rule inet wmf_sbx_hostblock {c} ip daddr 192.168.5.2 tcp dport 4321 '
            f'accept comment wmf-sbx-cred-4321' for c in cp.CHAINS])

    def test_remove_deletes_only_that_ports_rules(self):
        _, calls = self.run_guest(cp.guest_rules_argv(remove=[1234]), ports="1234 2345")
        deletes = [c for c in calls if c.startswith("delete")]
        self.assertEqual(deletes, [f"delete rule inet wmf_sbx_hostblock {c} handle 11"
                                   for c in cp.CHAINS])

    def test_ports_must_be_numbers(self):
        with self.assertRaises(ValueError):
            cp.guest_rules_argv(add=["1; reboot"])


if __name__ == "__main__":
    unittest.main()
