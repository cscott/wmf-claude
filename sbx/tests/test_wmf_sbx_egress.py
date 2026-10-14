#!/usr/bin/env python3
"""Unit tests for the host egress proxy's files and auth shim (egress.py,
lima-port/PLAN-SUDO.md A2.2) -- run with:
  python3 -m unittest discover -s sbx/tests -v

`nono proxy` itself ran in A2.0 (sbx/NOTES.md, "`--sudo` egress"); here
nothing runs nono. The shim forwards to a fake upstream proxy on the
loopback."""

import base64
import datetime
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
import wmf_sbx.nested as nested  # noqa: E402
import wmf_sbx.session as session  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402

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

    def test_the_directory_is_private(self):
        egress.write_config("demo", self.env)
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
        egress.write_config("demo", self.env)
        egress.remove("demo", self.env)
        self.assertFalse(os.path.exists(egress.egress_dir("demo", self.env)))

    def test_set_port_writes_the_port_file(self):
        egress.write_config("demo", self.env)
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
        self.assertEqual(argv[:7], ["nono", "proxy", "-v", "--no-auth", "--listen",
                                    "127.0.0.1", "--profile"])
        self.assertTrue(argv[7].endswith("/profiles/wmf-engineer.json"))
        allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allow-domain"]
        self.assertEqual(allowed, list(session.REGISTRY_DOMAINS)
                         + ["deb.debian.org", "registry.nono.sh", "example.org"])
        self.assertEqual(argv[argv.index("--proxy-ca-cert") + 1],
                         egress.ca_cert("demo", self.env))
        self.assertNotIn("--port", argv)
        self.assertNotIn("--upstream-proxy", argv)

    def test_a_nested_sandbox_allows_what_its_inner_vms_need(self):
        def allowed():
            argv = egress.proxy_argv("demo", self.env)
            return [argv[i + 1] for i, a in enumerate(argv) if a == "--allow-domain"]
        self.assertNotIn("cloud.debian.org", allowed())
        state_mod.save({"name": "demo", "nested": True}, self.env)
        domains = allowed()
        for domain in nested.DOMAINS:
            self.assertIn(domain, domains)
        self.assertEqual(len(domains), len(set(domains)))

    def test_the_guest_needs_no_password(self):
        config = egress.proxy_config("demo", self.env)
        self.assertIn("--no-auth", config["argv"])
        # nono proxy exits 2 on --no-auth with a password.
        self.assertNotIn("NONO_PROXY_PASS", config["env"])

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
                               {"env": {"XDG_STATE_HOME": "/s"}})
        self.assertEqual(env, {"PATH": "/bin", "XDG_STATE_HOME": "/s"})


class GuestTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}

    def test_the_guest_variables_point_at_the_guestfwd(self):
        variables = egress.guest_environment("demo", self.env)
        self.assertEqual(variables["https_proxy"], "http://192.168.5.100:3128")
        self.assertEqual(variables["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(variables["NODE_USE_ENV_PROXY"], "1")

    def test_with_the_forwarder_the_variables_point_at_the_loopback(self):
        variables = egress.guest_environment("demo", self.env, forward=True)
        self.assertEqual(variables["https_proxy"], "http://127.0.0.1:3128")
        self.assertEqual(variables["HTTP_PROXY"], "http://127.0.0.1:3128")

    def test_apt_and_git_use_the_proxy(self):
        files = egress.guest_files("demo", self.env)
        self.assertIn('Acquire::https::Proxy "http://192.168.5.100:3128";',
                      files["/etc/apt/apt.conf.d/90wmf-sbx-proxy"])
        self.assertIn("git config --system http.proxy http://192.168.5.100:3128",
                      egress.guest_provision())

    def test_the_provisioning_points_glibc_at_the_resolver_over_tcp(self):
        script = egress.guest_provision()
        self.assertIn("printf 'nameserver 192.168.5.100\\noptions use-vc\\n' "
                      "> /etc/resolv.conf", script)
        self.assertIn("systemctl mask systemd-resolved.service", script)

    def test_the_provisioning_replaces_its_own_environment_block(self):
        script = egress.guest_provision()
        self.assertIn("sed -i '/^# wmf-sbx egress begin$/,/^# wmf-sbx egress end$/d' "
                      "/etc/environment", script)
        self.assertIn("https_proxy=http://192.168.5.100:3128\n", script)
        self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True).returncode, 0)

    def test_the_guestfwds_reach_the_resolver_and_the_credential_proxy(self):
        config = egress.proxy_config("demo", self.env)
        fwds = egress.guestfwds(config).split(",")
        self.assertEqual(len(fwds), 3)
        self.assertTrue(fwds[0].startswith("guestfwd=tcp:192.168.5.100:3128-cmd:"))
        self.assertEqual(fwds[1], "guestfwd=tcp:192.168.5.100:53-cmd:sh -c 'exec socat - "
                                  f"UNIX-CONNECT:{egress.dns_socket('demo', self.env)}'")
        self.assertEqual(fwds[2], "guestfwd=tcp:192.168.5.100:3129-cmd:sh -c 'exec socat - "
                                  f"UNIX-CONNECT:{egress.cred_socket('demo', self.env)}'")

    def test_the_guestfwds_of_an_old_config_are_the_proxy_only(self):
        self.assertEqual(egress.guestfwds({"portFile": "/p"}), egress.guestfwd("/p"))

    def test_the_guestfwd_reads_the_port_at_each_connection(self):
        self.assertEqual(
            egress.guestfwd("/s/egress/demo/port"),
            "guestfwd=tcp:192.168.5.100:3128-cmd:"
            "sh -c 'exec socat - TCP:127.0.0.1:$(cat /s/egress/demo/port)'")

    def test_a_port_path_that_breaks_the_option_is_refused(self):
        for bad in ("/a,b/port", "/a'b/port", "/a b/port", "/a:b/port"):
            with self.assertRaises(egress.EgressError):
                egress.guestfwd(bad)
            with self.assertRaises(egress.EgressError):
                egress.socket_guestfwd(53, bad)


class StartTests(unittest.TestCase):
    """The wrapper's variables for `limactl start`, and the wait for
    Lima's time sync."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name, "PATH": "/usr/bin"}

    def test_the_wrapper_runs_as_qemu_and_finds_the_real_one(self):
        def which(cmd, path=None):
            self.assertEqual((cmd, path), ("qemu-system-aarch64", "/usr/bin"))
            return "/usr/bin/qemu-system-aarch64"
        variables = egress.wrapper_env("demo", self.env, arch="aarch64", which=which)
        self.assertEqual(variables["QEMU_SYSTEM_AARCH64"], egress.WRAPPER)
        self.assertTrue(egress.WRAPPER.endswith("/sbx/bin/wmf-sbx-qemu"))
        self.assertTrue(os.path.isfile(egress.WRAPPER))
        self.assertEqual(variables["WMF_SBX_QEMU_REAL"], "/usr/bin/qemu-system-aarch64")
        self.assertEqual(variables["WMF_SBX_EGRESS_CONFIG"], egress.config_file("demo", self.env))
        self.assertTrue(os.path.isfile(variables["WMF_SBX_EGRESS_CONFIG"]))

    def test_no_qemu_is_an_error(self):
        with self.assertRaises(egress.EgressError):
            egress.wrapper_env("demo", self.env, arch="x86_64", which=lambda c, path=None: None)

    def test_the_host_tools_that_are_missing(self):
        have = {"qemu-system-x86_64", "openssl"}
        seen = []

        def which(cmd, path=None):
            seen.append(path)
            return f"/usr/bin/{cmd}" if cmd in have else None
        self.assertEqual(egress.missing_tools(self.env, arch="x86_64", which=which), ["nono"])
        self.assertEqual(set(seen), {"/usr/bin"})
        have.add("nono")
        self.assertEqual(egress.missing_tools(self.env, arch="x86_64", which=which), [])
        # no PATH: nothing is found (not the test runner's own PATH)
        self.assertEqual(egress.missing_tools({}, arch="aarch64"),
                         ["qemu-system-aarch64", "nono", "openssl"])

    def test_the_machine_names_map_to_qemu_s(self):
        self.assertEqual(egress.qemu_arch("arm64"), "aarch64")
        self.assertEqual(egress.qemu_arch("AMD64"), "x86_64")
        with self.assertRaises(egress.EgressError):
            egress.qemu_arch("riscv64")

    def test_only_the_state_key_turns_it_on(self):
        self.assertTrue(egress.enabled({"egress": "proxy"}))
        for state in (None, {}, {"egress": "none"}):
            self.assertFalse(egress.enabled(state))

    LOG = ('{"level":"info","msg":"Time sync: guest agent is alive, starting time '
           'synchronization","time":"2026-10-13T12:38:16-04:00"}\n'
           'not json\n'
           '{"level":"info","msg":"Time sync: guest clock adjusted (was -1530ms off)",'
           '"time":"2026-10-13T12:38:21-04:00"}\n')

    def test_a_sync_after_the_start_counts(self):
        since = datetime.datetime.fromisoformat("2026-10-13T16:38:00+00:00")
        self.assertTrue(egress.time_synced(self.LOG, since))

    def test_a_sync_before_the_start_does_not(self):
        since = datetime.datetime.fromisoformat("2026-10-13T16:38:30+00:00")
        self.assertFalse(egress.time_synced(self.LOG, since))

    def test_a_sync_within_the_threshold_counts(self):
        log = ('{"level":"debug","msg":"Time sync: drift 0ms within threshold",'
               '"time":"2026-10-13T12:39:10-04:00"}\n')
        since = datetime.datetime.fromisoformat("2026-10-13T12:39:00-04:00")
        self.assertTrue(egress.time_synced(log, since))

    def test_the_wait_gives_up(self):
        t = [0.0]
        since = datetime.datetime.now().astimezone()
        ok = egress.wait_time_sync(os.path.join(self.tmp.name, "missing"), since, timeout=2,
                                   clock=lambda: t[0],
                                   sleep=lambda s: t.__setitem__(0, t[0] + s))
        self.assertFalse(ok)


class StatusTests(unittest.TestCase):

    NOW = datetime.datetime(2026, 10, 13, tzinfo=datetime.timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self.tmp.name}
        egress.make_dir("demo", self.env)

    def openssl(self, end):
        def run(argv, **kw):
            self.assertEqual(argv[:2], ["openssl", "x509"])
            return subprocess.CompletedProcess(argv, 0 if end else 1,
                                               stdout=f"notAfter={end}\n" if end else "")
        return run

    def lines(self, end="Oct 13 00:00:00 2027 GMT", running=True):
        return egress.status_lines("demo", self.env, running=running, run=self.openssl(end),
                               now=self.NOW)

    def test_a_listening_proxy_and_a_good_ca(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            egress.set_port("demo", s.getsockname()[1], self.env)
            (proxy, ok1), (ca, ok2), (allow, ok3) = self.lines()
        self.assertTrue(ok1 and ok2 and ok3)
        self.assertIn("listening on 127.0.0.1:", proxy)
        self.assertEqual(ca, "CA expires 2027-10-13")
        self.assertEqual(allow, "allowed by `wmf-sbx allow`: none")

    def test_a_dead_proxy_and_an_expired_ca_are_problems(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]   # bound, not listening
            egress.set_port("demo", port, self.env)
            (proxy, ok1), (ca, ok2), _ = self.lines("Oct  1 00:00:00 2026 GMT")
        self.assertFalse(ok1)
        self.assertIn("not listening", proxy)
        self.assertFalse(ok2)
        self.assertIn("expired on 2026-10-01", ca)

    def test_a_ca_near_its_end_is_a_warning_and_a_missing_one_a_problem(self):
        (_p, _), (ca, ok), _ = self.lines("Oct 30 00:00:00 2026 GMT", running=False)
        self.assertTrue(ok)
        self.assertIn("warning: in 17 days", ca)
        (proxy, ok), (ca, ok2), _ = self.lines(None, running=False)
        self.assertTrue(ok)
        self.assertIn("the VM is stopped", proxy)
        self.assertFalse(ok2)
        self.assertIn("missing", ca)


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
