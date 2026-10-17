#!/usr/bin/env python3
"""Unit tests for the host credential proxy (credproxy.py, track L) -- run
with:
  python3 -m unittest discover -s sbx/tests -v

The proxy ran against api.anthropic.com in L1 (sbx/NOTES.md, "Shared
login"); here it forwards to a fake upstream on the loopback."""

import base64
import contextlib
import http.client
import http.server
import io
import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.credproxy as cp  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


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
        # Not this machine's marker, if it runs in a nested sandbox.
        self.marker = os.path.join(self.tmp.name, "nested")
        patch = mock.patch.object(cp.image_mod, "NESTED_MARKER", self.marker)
        patch.start()
        self.addCleanup(patch.stop)

    def test_in_a_nested_sandbox_the_enclosing_sessions_phantom(self):
        self.write(self.marker, "ANTHROPIC_BASE_URL=http://192.168.5.100:3129\n"
                                "CLAUDE_CODE_OAUTH_TOKEN=wmfsbx-outer\n", mode=0o640)
        src = cp.host_source(self.env)
        self.assertEqual((src.kind, src.value()), ("oauth", "Bearer wmfsbx-outer"))
        self.assertIn(self.marker, src.describe)
        self.assertEqual(cp.upstream_url(self.env), ("http", "192.168.5.100", 3129))
        # The variables win over the marker.
        env = dict(self.env, CLAUDE_CODE_OAUTH_TOKEN="t2", ANTHROPIC_BASE_URL="http://h:1")
        self.assertEqual(cp.host_source(env).value(), "Bearer t2")
        self.assertEqual(cp.upstream_url(env), ("http", "h", 1))
        # An API key phantom, from a session whose host had one.
        self.write(self.marker, "ANTHROPIC_API_KEY=wmfsbx-k\n", mode=0o640)
        src = cp.host_source(self.env)
        self.assertEqual((src.kind, src.value()), ("api-key", "wmfsbx-k"))

    def test_an_empty_marker_is_no_credential(self):
        # A nested sandbox before its first session (or made before this).
        self.write(self.marker, "", mode=0o644)
        self.assertEqual(cp.nested_credential(), {})
        self.assertEqual(cp.upstream_url(self.env), ("https", cp.UPSTREAM, 443))

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
        self.assertRegex(cp.check_source(s), "expired.*run `wmf-sbx login`")
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

def in_a_nested_sandbox(test):
    """Patch the nested marker to a file that exists, for `test`."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    marker = os.path.join(tmp.name, "nested")
    open(marker, "w").close()
    patch = mock.patch.object(cp.image_mod, "NESTED_MARKER", marker)
    patch.start()
    test.addCleanup(patch.stop)


class UpstreamUrlTests(unittest.TestCase):

    def setUp(self):
        in_a_nested_sandbox(self)

    def test_api_anthropic_com_by_default(self):
        self.assertEqual(cp.upstream_url({}), ("https", cp.UPSTREAM, 443))

    def test_the_base_url_is_ignored_outside_a_nested_sandbox(self):
        # The host's real credential goes only to api.anthropic.com, over
        # TLS, whatever the engineer's $ANTHROPIC_BASE_URL says.
        with mock.patch.object(cp.image_mod, "NESTED_MARKER", "/nonexistent/nested"):
            for url in ("http://192.168.5.100:3129", "https://gw.example/", "ftp://h"):
                self.assertEqual(cp.upstream_url({"ANTHROPIC_BASE_URL": url}),
                                 ("https", cp.UPSTREAM, 443))

    def test_the_base_url_when_set(self):
        self.assertEqual(cp.upstream_url({"ANTHROPIC_BASE_URL": "http://192.168.5.100:3129"}),
                         ("http", "192.168.5.100", 3129))
        self.assertEqual(cp.upstream_url({"ANTHROPIC_BASE_URL": "https://gw.example/"}),
                         ("https", "gw.example", 443))

    def test_a_base_url_with_a_path_or_another_scheme_is_refused(self):
        for bad in ("http://h:1/api", "ftp://h", "h:1"):
            with self.assertRaises(cp.CredentialError):
                cp.upstream_url({"ANTHROPIC_BASE_URL": bad})


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
        if self.path.startswith("/v1/stream"):
            # A long streamed response, as /v1/messages sends.
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(200 * 65536))
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x" * 65536)
                    time.sleep(0.005)
            except OSError:
                pass
            return
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

    def test_each_response_closes_the_connection(self):
        # nono's forward proxy copies a response until this side closes.
        for path in ("/v1/messages", "/api/oauth/profile"):
            with self.subTest(path=path):
                s = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=10)
                self.addCleanup(s.close)
                s.sendall(b"POST %s HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n"
                          b"x-api-key: %s\r\nContent-Length: 2\r\n\r\n{}"
                          % (path.encode(), self.proxy.phantom.encode()))
                data = b""
                while chunk := s.recv(65536):
                    data += chunk
                head = data.split(b"\r\n\r\n")[0].lower()
                self.assertIn(b"\r\nconnection: close", head)

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

    def test_a_unix_socket_proxy_serves_and_stop_removes_it(self):
        path = os.path.join(self.tmp.name, "cred.sock")
        proxy = cp.CredentialProxy(cp.Source("api-key", "test", lambda: REAL_KEY),
                                   env={}, log_path=self.log)
        port = self.upstream.server_address[1]
        proxy.connect = lambda: http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.assertIsNone(proxy.start(unix_path=path))
        self.addCleanup(proxy.stop)
        self.assertIsNone(proxy.port)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertTrue(cp.socket_in_use(path))
        conn = http.client.HTTPConnection("localhost", timeout=10)
        conn.sock = socket.socket(socket.AF_UNIX)
        conn.sock.connect(path)
        conn.request("POST", "/v1/messages", body=b"{}",
                     headers={"x-api-key": proxy.phantom})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(FakeUpstream.seen[0][2]["x-api-key"], REAL_KEY)
        proxy.stop()
        self.assertFalse(os.path.exists(path))
        self.assertFalse(cp.socket_in_use(path))

    def test_stop_leaves_a_socket_that_a_later_proxy_bound(self):
        path = os.path.join(self.tmp.name, "cred.sock")
        first = cp.CredentialProxy(cp.Source("api-key", "test", lambda: REAL_KEY), env={})
        first.start(unix_path=path)
        second = cp.CredentialProxy(cp.Source("api-key", "test", lambda: REAL_KEY), env={})
        second.start(unix_path=path)
        self.addCleanup(second.stop)
        first.stop()
        self.assertTrue(cp.socket_in_use(path))

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

    def test_a_client_that_goes_away_is_logged_not_printed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
            conn.request("POST", "/v1/stream", body=b"{}",
                         headers={"x-api-key": self.proxy.phantom})
            # The response's "connection: close" takes conn.sock from conn.
            sock = conn.sock
            resp = conn.getresponse()
            resp.read(1000)
            sock.shutdown(socket.SHUT_RDWR)
            conn.close()
            text = ""
            for _ in range(100):
                if os.path.exists(self.log):
                    with open(self.log) as f:
                        text = f.read()
                if "closed the connection" in text:
                    break
                time.sleep(0.05)
        self.assertIn("POST /v1/stream - 0B", text)
        self.assertIn("the client closed the connection", text)
        self.assertEqual(err.getvalue(), "")
        # The proxy still serves.
        status, _ = self.request("POST", "/v1/messages", body=b"{}",
                                 headers={"x-api-key": self.proxy.phantom})
        self.assertEqual(status, 200)

    def test_other_errors_go_to_the_log(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.proxy._server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(err.getvalue(), "")
        with open(self.log) as f:
            self.assertIn("error in the proxy", f.read())

    def test_the_session_env(self):
        env = self.proxy.session_env()
        self.assertEqual(env, {"ANTHROPIC_API_KEY": self.proxy.phantom,
                               "ANTHROPIC_BASE_URL": f"http://192.168.5.2:{self.proxy.port}"})

    def test_a_nested_proxy_chains_to_the_base_url(self):
        # In a nested sandbox: $ANTHROPIC_BASE_URL is the enclosing
        # session's proxy and the credential its phantom. No connect()
        # override: the proxy itself goes there, over plain HTTP.
        in_a_nested_sandbox(self)
        port = self.upstream.server_address[1]
        proxy = cp.CredentialProxy(cp.Source("oauth", "t", lambda: "outer-phantom"),
                                   env={"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                                        "HTTPS_PROXY": "http://127.0.0.1:9"},
                                   log_path=self.log)
        proxy.start()
        self.addCleanup(proxy.stop)
        status, _ = self.request("POST", "/v1/messages", body=b"{}", proxy=proxy,
                                 headers={"Authorization": f"Bearer {proxy.phantom}"})
        self.assertEqual(status, 200)
        (_m, path, headers, _b), = FakeUpstream.seen
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["authorization"], "Bearer outer-phantom")
        self.assertEqual(headers["host"], f"127.0.0.1:{port}")

    def test_in_a_nono_session_a_nested_proxy_goes_through_nonos_proxy(self):
        # nono 0.80 refuses a connect off the loopback: a CONNECT to its
        # proxy, which goes to the enclosing session's proxy.
        in_a_nested_sandbox(self)
        port = self.upstream.server_address[1]
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        heads = []

        def relay():
            conn, _ = server.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(65536)
            head, _, rest = buf.partition(b"\r\n\r\n")
            heads.append(head.decode())
            up = socket.create_connection(("127.0.0.1", port))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            up.sendall(rest)

            def pipe(a, b):
                while (data := a.recv(65536)):
                    b.sendall(data)
                b.shutdown(socket.SHUT_WR)
            t = threading.Thread(target=pipe, args=(up, conn), daemon=True)
            t.start()
            pipe(conn, up)
            t.join(5)
            conn.close()
            up.close()
        threading.Thread(target=relay, daemon=True).start()
        nono = f"http://nono:tok@127.0.0.1:{server.getsockname()[1]}"
        proxy = cp.CredentialProxy(cp.Source("oauth", "t", lambda: "outer-phantom"),
                                   env={"ANTHROPIC_BASE_URL": f"http://192.0.2.1:{port}",
                                        "NONO_PROXY_TOKEN": "tok", "HTTP_PROXY": nono},
                                   log_path=self.log)
        proxy.start()
        self.addCleanup(proxy.stop)
        status, _ = self.request("POST", "/v1/messages", body=b"{}", proxy=proxy,
                                 headers={"Authorization": f"Bearer {proxy.phantom}"})
        self.assertEqual(status, 200)
        (head,) = heads
        self.assertTrue(head.startswith(f"CONNECT 192.0.2.1:{port} HTTP/1."), head)
        self.assertIn("Proxy-Authorization: Basic "
                      + base64.b64encode(b"nono:tok").decode(), head)
        (_m, _p, headers, _b), = FakeUpstream.seen
        self.assertEqual(headers["authorization"], "Bearer outer-phantom")

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
