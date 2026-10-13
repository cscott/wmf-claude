#!/usr/bin/env python3
"""Unit tests for the host egress proxy's files and auth shim (egress.py,
lima-port/PLAN-SUDO.md A2.2) -- run with:
  python3 -m unittest discover -s sbx/tests -v

`nono proxy` itself ran in A2.0 (sbx/NOTES.md, "`--sudo` egress"); here
nothing runs nono. The shim forwards to a fake upstream proxy on the
loopback."""

import base64
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.egress as egress  # noqa: E402
import wmf_sbx.session as session  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None


class FilesTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_the_directory_is_beside_the_sandboxes_state(self):
        self.assertEqual(egress.egress_dir("demo", self.env),
                         os.path.join(self.tmp.name, "wmf-sbx", "egress", "demo"))

    def test_a_bad_name_is_refused(self):
        with self.assertRaises(Exception):
            egress.egress_dir("../x", self.env)

    def test_the_password_is_made_once_and_private(self):
        first = egress.password("demo", self.env)
        self.assertEqual(len(first), 64)
        self.assertEqual(egress.password("demo", self.env), first)
        self.assertEqual(self.mode(os.path.join(egress.egress_dir("demo", self.env),
                                                "password")), 0o600)
        self.assertEqual(self.mode(egress.egress_dir("demo", self.env)), 0o700)

    def test_allow_entries_skip_blanks_and_comments(self):
        os.makedirs(egress.egress_dir("demo", self.env))
        with open(os.path.join(egress.egress_dir("demo", self.env), "allow"), "w") as f:
            f.write("# for the task\nexample.org\n\n  https://example.net/x/**  \n")
        self.assertEqual(egress.extra_domains("demo", self.env),
                         ["example.org", "https://example.net/x/**"])

    def test_no_allow_file_is_no_entries(self):
        self.assertEqual(egress.extra_domains("demo", self.env), [])

    def test_remove_deletes_the_directory(self):
        egress.password("demo", self.env)
        egress.remove("demo", self.env)
        self.assertFalse(os.path.exists(egress.egress_dir("demo", self.env)))

    def test_set_port_writes_the_port_file(self):
        egress.password("demo", self.env)
        egress.set_port("demo", 4321, self.env)
        with open(egress.port_file("demo", self.env)) as f:
            self.assertEqual(f.read(), "4321\n")


class CaTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}

    @unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
    def test_the_ca_is_an_ec_ca_with_a_pkcs8_key(self):
        cert = egress.make_ca("demo", self.env)
        key = egress.ca_key("demo", self.env)
        with open(key) as f:
            self.assertEqual(f.readline().strip(), "-----BEGIN PRIVATE KEY-----")
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)
        text = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-text"],
                              capture_output=True, text=True, check=True).stdout
        self.assertIn("CN=wmf-sbx egress demo", text.replace(" = ", "="))
        self.assertIn("CA:TRUE", text)
        self.assertIn("Certificate Sign", text)
        self.assertIn("id-ecPublicKey", text)

    def test_an_existing_ca_is_kept(self):
        os.makedirs(egress.egress_dir("demo", self.env))
        for path in (egress.ca_cert("demo", self.env), egress.ca_key("demo", self.env)):
            with open(path, "w") as f:
                f.write("old\n")
        calls = []
        egress.make_ca("demo", self.env, run=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_an_openssl_failure_says_why_and_leaves_nothing(self):
        def run(argv, **kwargs):
            raise subprocess.CalledProcessError(1, argv, stderr=b"no curve\n")
        with self.assertRaisesRegex(egress.EgressError, "no curve"):
            egress.make_ca("demo", self.env, run=run)
        self.assertEqual(os.listdir(egress.egress_dir("demo", self.env)), [])


class ConfigTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}

    def test_the_argv_has_the_profile_hosts_and_ca(self):
        os.makedirs(egress.egress_dir("demo", self.env))
        with open(os.path.join(egress.egress_dir("demo", self.env), "allow"), "w") as f:
            f.write("example.org\n")
        argv = egress.proxy_argv("demo", self.env)
        self.assertEqual(argv[:6], ["nono", "proxy", "-v", "--listen", "127.0.0.1",
                                    "--profile"])
        self.assertTrue(argv[6].endswith("/profiles/wmf-engineer.json"))
        allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allow-domain"]
        self.assertEqual(allowed, list(session.REGISTRY_DOMAINS)
                         + ["deb.debian.org", "example.org"])
        self.assertEqual(argv[argv.index("--proxy-ca-cert") + 1],
                         egress.ca_cert("demo", self.env))
        self.assertNotIn("--port", argv)
        self.assertNotIn("--upstream-proxy", argv)

    def test_the_password_is_not_in_the_argv(self):
        config = egress.proxy_config("demo", self.env)
        pw = egress.password("demo", self.env)
        self.assertEqual(config["env"]["NONO_PROXY_PASS"], pw)
        self.assertNotIn(pw, " ".join(config["argv"]))

    def test_the_upstream_comes_from_the_hosts_proxy(self):
        self.env["https_proxy"] = "http://nono:s%40cret@127.0.0.1:50077"
        self.assertEqual(egress.upstream(self.env),
                         {"host": "127.0.0.1", "port": 50077, "user": "nono",
                          "password": "s@cret"})

    def test_no_host_proxy_is_no_upstream(self):
        self.assertIsNone(egress.upstream(self.env))

    def test_an_upstream_without_a_port_is_refused(self):
        self.env["HTTPS_PROXY"] = "http://proxy.example"
        with self.assertRaises(egress.EgressError):
            egress.upstream(self.env)

    def test_the_config_file_is_private_json(self):
        path = egress.write_config("demo", self.env)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        config = egress.read_config(path)
        self.assertEqual(config["portFile"], egress.port_file("demo", self.env))
        self.assertIsNone(config["upstream"])

    def test_the_proxy_env_drops_inherited_nono_variables(self):
        env = egress.proxy_env({"PATH": "/bin", "NONO_ALLOW_DOMAIN": "evil.example",
                                "NONO_UPSTREAM_PROXY": "x:1"},
                               {"env": {"NONO_PROXY_PASS": "pw"}})
        self.assertEqual(env, {"PATH": "/bin", "NONO_PROXY_PASS": "pw"})


class GuestTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}

    def test_the_guest_variables_point_at_the_guestfwd(self):
        pw = egress.password("demo", self.env)
        variables = egress.guest_environment("demo", self.env)
        self.assertEqual(variables["https_proxy"], f"http://nono:{pw}@192.168.5.100:3128")
        self.assertEqual(variables["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(variables["NODE_USE_ENV_PROXY"], "1")

    def test_git_sends_basic_auth(self):
        files = egress.guest_files("demo", self.env)
        self.assertIn("proxyAuthMethod = basic", files["/etc/gitconfig"])
        self.assertIn('Acquire::https::Proxy "http://nono:',
                      files["/etc/apt/apt.conf.d/90wmf-sbx-proxy"])

    def test_the_guestfwd_reads_the_port_at_each_connection(self):
        self.assertEqual(
            egress.guestfwd("/s/egress/demo/port"),
            "guestfwd=tcp:192.168.5.100:3128-cmd:"
            "sh -c 'exec socat - TCP:127.0.0.1:$(cat /s/egress/demo/port)'")

    def test_a_port_path_that_breaks_the_option_is_refused(self):
        for bad in ("/a,b/port", "/a'b/port"):
            with self.assertRaises(egress.EgressError):
                egress.guestfwd(bad)


class FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class WaitTests(unittest.TestCase):

    def test_a_listening_port_returns(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            egress.wait_listening(s.getsockname()[1], FakeProc())

    def test_an_exited_proxy_is_an_error(self):
        with self.assertRaisesRegex(egress.EgressError, "exited \\(2\\)"):
            egress.wait_listening(egress.free_port(), FakeProc(2))

    def test_a_proxy_that_never_listens_times_out(self):
        ticks = iter(range(100))
        with self.assertRaisesRegex(egress.EgressError, "within"):
            egress.wait_listening(egress.free_port(), FakeProc(), timeout=3,
                                  clock=lambda: next(ticks), sleep=lambda s: None)


class FakeUpstream:
    """A one-request upstream proxy: records the head it gets, answers
    200 to a CONNECT, then echoes the tunnel's bytes."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.heads = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                self.heads.append(head)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n" + rest)
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    conn.sendall(chunk)

    def close(self):
        self.sock.close()


class ShimTests(unittest.TestCase):

    def setUp(self):
        self.upstream = FakeUpstream()
        self.addCleanup(self.upstream.close)
        self.shim = egress.AuthShim("127.0.0.1", self.upstream.port, "nono", "s3cret")
        self.port = self.shim.start()
        self.addCleanup(self.shim.close)

    def talk(self, request, then=b""):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as c:
            c.sendall(request + then)
            c.shutdown(socket.SHUT_WR)
            out = b""
            while True:
                chunk = c.recv(4096)
                if not chunk:
                    return out
                out += chunk

    def test_a_connect_gets_the_upstream_credential(self):
        out = self.talk(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n"
                        b"Proxy-Authorization: Basic Z3Vlc3Q6eA==\r\n\r\n", b"tunnel bytes")
        self.assertEqual(out, b"HTTP/1.1 200 Connection established\r\n\r\ntunnel bytes")
        head = self.upstream.heads[0].split(b"\r\n")
        self.assertEqual(head[0], b"CONNECT example.org:443 HTTP/1.1")
        auth = [ln for ln in head if ln.lower().startswith(b"proxy-authorization:")]
        self.assertEqual(auth, [b"Proxy-Authorization: Basic "
                                + base64.b64encode(b"nono:s3cret")])
        self.assertIn(b"Host: example.org:443", head)

    def test_a_dead_upstream_is_a_502(self):
        self.upstream.close()
        shim = egress.AuthShim("127.0.0.1", egress.free_port(), "u", "p")
        port = shim.start()
        self.addCleanup(shim.close)
        self.port = port
        out = self.talk(b"CONNECT example.org:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 502"))

    def test_an_endless_head_is_dropped(self):
        out = self.talk(b"CONNECT x:443 HTTP/1.1\r\nX: " + b"a" * 70000)
        self.assertEqual(out, b"")
        self.assertEqual(self.upstream.heads, [])


if __name__ == "__main__":
    unittest.main()
