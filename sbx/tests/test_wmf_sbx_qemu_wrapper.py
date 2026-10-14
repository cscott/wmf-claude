#!/usr/bin/env python3
"""Unit tests for the QEMU wrapper (qemu_wrapper.py, bin/wmf-sbx-qemu;
lima-port/PLAN-SUDO.md A2.1) -- run with:
  python3 -m unittest discover -s sbx/tests -v

The argv rewriting is tested as pure functions. The lifetime runs the
real bin/wmf-sbx-qemu with a fake QEMU and a fake `nono proxy` (small
Python scripts written per test): the proxy goes when QEMU does, signals
reach QEMU, SIGUSR1 starts a second proxy."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.qemu_wrapper as qw  # noqa: E402

WRAPPER = os.path.join(os.path.dirname(__file__), "..", "bin", "wmf-sbx-qemu")
GUESTFWD = "guestfwd=tcp:192.168.5.100:3128-cmd:sh -c 'exec socat - TCP:127.0.0.1:$(cat /p)'"
LIMA_NETDEV = ("user,id=net0,net=192.168.5.0/24,dhcpstart=192.168.5.15,"
               "hostfwd=tcp:127.0.0.1:49441-:22")


def boot_args(*net):
    return (["-m", "8192"] + list(net or ["-netdev", LIMA_NETDEV])
            + ["-device", "virtio-net-pci,netdev=net0", "-qmp", "chardev:char-qmp",
               "-pidfile", "/x/qemu.pid"])


class ArgvTests(unittest.TestCase):

    def test_a_probe_is_not_a_boot(self):
        for probe in (["--version"], ["-M", "none", "-accel", "help"], ["-netdev", "help"]):
            self.assertFalse(qw.is_boot(probe), probe)
        self.assertTrue(qw.is_boot(boot_args()))

    def test_the_user_netdev_gets_restrict_and_the_guestfwd(self):
        out = qw.boot_argv(boot_args(), GUESTFWD)
        self.assertEqual(out[out.index("-netdev") + 1],
                         f"{LIMA_NETDEV},restrict=on,{GUESTFWD}")
        self.assertEqual(len(out), len(boot_args()))

    def test_refusals(self):
        cases = {
            "two user netdevs": boot_args("-netdev", LIMA_NETDEV, "-netdev", "user,id=net1"),
            "no netdev": boot_args("-name", "x"),
            "a socket netdev": boot_args("-netdev", LIMA_NETDEV, "-netdev",
                                         "socket,id=vlan,fd=3"),
            "a -nic": boot_args("-netdev", LIMA_NETDEV, "-nic", "user"),
            "a -net": boot_args("-netdev", LIMA_NETDEV, "-net", "nic"),
            "a joined option": boot_args("-netdev", LIMA_NETDEV, "--netdev=user,id=n2"),
            "restrict already set": boot_args("-netdev", LIMA_NETDEV + ",restrict=off"),
            "a guestfwd already set": boot_args("-netdev", LIMA_NETDEV + ",guestfwd=x"),
            "a dangling -netdev": ["-qmp", "x", "-netdev"],
        }
        for what, args in cases.items():
            with self.subTest(what), self.assertRaises(qw.WrapperError):
                qw.boot_argv(args, GUESTFWD)

    def test_exit_status_of_a_signal(self):
        self.assertEqual(qw.exit_status(-15), 143)
        self.assertEqual(qw.exit_status(3), 3)


def virtfs(n, extra=""):
    out = []
    for i in range(n):
        out += ["-virtfs", f"local,path=/h/r{i}/.git,mount_tag=lima-{i},"
                           f"security_model=none,id=lima-{i}{extra}"]
    return out


class VirtfsTests(unittest.TestCase):

    def devices(self, args):
        return [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-device"]

    def test_eight_mounts_to_a_slot_from_the_top(self):
        out = qw.spread_virtfs(["-m", "8192"] + virtfs(9) + ["-qmp", "x"])
        self.assertNotIn("-virtfs", out)
        self.assertEqual(out[:2], ["-m", "8192"])
        self.assertEqual(out[-2:], ["-qmp", "x"])
        devs = self.devices(out)
        self.assertEqual(devs[0], "virtio-9p-pci,fsdev=wmf-sbx-fs0,mount_tag=lima-0,"
                                  "addr=0x1e.0x0,multifunction=on")
        self.assertEqual(devs[7], "virtio-9p-pci,fsdev=wmf-sbx-fs7,mount_tag=lima-7,addr=0x1e.0x7")
        self.assertEqual(devs[8], "virtio-9p-pci,fsdev=wmf-sbx-fs8,mount_tag=lima-8,"
                                  "addr=0x1d.0x0,multifunction=on")
        self.assertEqual(out[out.index("-fsdev") + 1],
                         "local,id=wmf-sbx-fs0,path=/h/r0/.git,security_model=none")

    def test_a_comma_in_a_path_stays_escaped(self):
        out = qw.spread_virtfs(["-virtfs", "local,path=/h/a,,b,mount_tag=t,security_model=none"])
        self.assertEqual(out[1], "local,id=wmf-sbx-fs0,path=/h/a,,b,security_model=none")
        self.assertEqual(qw._split_opts("a,,b,c"), ["a,b", "c"])

    def test_no_virtfs_no_change(self):
        self.assertEqual(qw.spread_virtfs(boot_args()), boot_args())

    def test_the_last_slot_holds_the_limit(self):
        out = qw.spread_virtfs(virtfs(qw.template_mod.MAX_QEMU_MOUNTS))
        last = self.devices(out)[-1]
        slot = qw.NINEP_TOP_SLOT - qw.template_mod.NINEP_SLOTS + 1
        self.assertTrue(last.endswith(f"addr=0x{slot:x}.0x7"), last)

    def test_refusals(self):
        cases = {
            "one more than the limit": virtfs(qw.template_mod.MAX_QEMU_MOUNTS + 1),
            "a joined -virtfs": ["-virtfs=local,path=/h,mount_tag=t"],
            "a proxy -virtfs": ["-virtfs", "proxy,sock_fd=3,mount_tag=t"],
            "no mount_tag": ["-virtfs", "local,path=/h,security_model=none"],
            "a dangling -virtfs": ["-m", "1", "-virtfs"],
        }
        for what, args in cases.items():
            with self.subTest(what), self.assertRaises(qw.WrapperError):
                qw.spread_virtfs(args)


class RunTests(unittest.TestCase):

    def test_a_probe_runs_the_real_qemu_unchanged(self):
        calls = []
        qw.run(["--version"], env={qw.REAL_VAR: "/usr/bin/qemu-system-x86_64"},
               execv=lambda p, a: calls.append((p, a)), self_path=WRAPPER)
        self.assertEqual(calls, [("/usr/bin/qemu-system-x86_64",
                                  ["/usr/bin/qemu-system-x86_64", "--version"])])

    def test_no_real_qemu_is_refused(self):
        with self.assertRaisesRegex(qw.WrapperError, qw.REAL_VAR):
            qw.run(["--version"], env={}, self_path=WRAPPER)

    def test_the_wrapper_as_its_own_qemu_is_refused(self):
        with self.assertRaisesRegex(qw.WrapperError, "this wrapper"):
            qw.run(["--version"], env={qw.REAL_VAR: WRAPPER}, self_path=WRAPPER)

    def test_a_boot_without_a_config_is_refused(self):
        with self.assertRaisesRegex(qw.WrapperError, qw.CONFIG_VAR):
            qw.run(boot_args(), env={qw.REAL_VAR: "/usr/bin/true"}, self_path=WRAPPER,
                   popen=None)
        with self.assertRaisesRegex(qw.WrapperError, qw.PLAIN_VAR):
            qw.run(boot_args(), env={qw.REAL_VAR: "/usr/bin/true", qw.PLAIN_VAR: "yes"},
                   self_path=WRAPPER, popen=None)

    def test_a_plain_boot_gets_only_the_balloon(self):
        calls = []
        qw.run(boot_args(), env={qw.REAL_VAR: "/usr/bin/qemu-system-x86_64", qw.PLAIN_VAR: "1"},
               execv=lambda p, a: calls.append((p, a)), self_path=WRAPPER)
        (path, argv), = calls
        self.assertEqual(argv, ["/usr/bin/qemu-system-x86_64"] + boot_args()
                         + ["-device", qw.BALLOON])

    def test_a_plain_boot_spreads_the_mounts_and_adds_the_balloon(self):
        calls = []
        qw.run(boot_args() + virtfs(2),
               env={qw.REAL_VAR: "/usr/bin/qemu-system-x86_64", qw.PLAIN_VAR: "1"},
               execv=lambda p, a: calls.append((p, a)), self_path=WRAPPER)
        (_path, argv), = calls
        self.assertEqual(argv, ["/usr/bin/qemu-system-x86_64"]
                         + qw.spread_virtfs(boot_args() + virtfs(2)) + ["-device", qw.BALLOON])
        self.assertNotIn("-virtfs", argv)

    def test_the_balloon_goes_on_limas_own_device(self):
        args = boot_args() + ["-device", "virtio-balloon-pci,id=b0"]
        self.assertEqual(qw.with_balloon(args)[-1],
                         "virtio-balloon-pci,id=b0,free-page-reporting=on")
        self.assertEqual(qw.with_balloon(qw.with_balloon(args)), qw.with_balloon(args))
        self.assertEqual(qw.with_balloon(boot_args())[-2:], ["-device", qw.BALLOON])


FAKE_PROXY = textwrap.dedent("""\
    import json, os, signal, socket, sys
    log = sys.argv[1]
    if os.environ.get("FAKE_PROXY_FAIL"):
        sys.exit(2)
    port = int(sys.argv[sys.argv.index("--port") + 1])
    s = socket.socket(); s.bind(("127.0.0.1", port)); s.listen(8)
    with open(log, "a") as f:
        f.write(json.dumps({"pid": os.getpid(), "argv": sys.argv[2:],
                            "state": os.environ.get("XDG_STATE_HOME"),
                            "leak": os.environ.get("NONO_ALLOW_DOMAIN")}) + "\\n")
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    while True:
        s.accept()[0].close()
    """)

# Records its argv and pid; then, by FAKE_QEMU: "exit:N" exits N after
# checking the proxy answers on the port file's port; "wait" runs until
# SIGTERM, then exits 0.
FAKE_QEMU = textwrap.dedent("""\
    import json, os, signal, socket, sys, time
    with open(os.environ["FAKE_QEMU_LOG"], "w") as f:
        json.dump({"pid": os.getpid(), "argv": sys.argv[1:]}, f)
    mode = os.environ["FAKE_QEMU"]
    if mode.startswith("exit:"):
        port = int(open(os.environ["FAKE_PORT_FILE"]).read())
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
        sys.exit(int(mode[5:]))
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    while True:
        time.sleep(0.1)
    """)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); its state says it is gone.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return True


def wait_for(cond, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


class LifetimeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.proxy_log = os.path.join(d, "proxy.log")
        self.qemu_log = os.path.join(d, "qemu.json")
        self.port_file = os.path.join(d, "port")
        self.pid_file = os.path.join(d, "wrapper.pid")
        for name, text in (("proxy.py", FAKE_PROXY), ("qemu.py", FAKE_QEMU)):
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        self.qemu = os.path.join(d, "qemu-system-test")
        with open(self.qemu, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {d}/qemu.py \"$@\"\n")
        os.chmod(self.qemu, 0o755)
        self.config = os.path.join(d, "proxy.json")
        self.write_config()

    def write_config(self, upstream=None, extra=()):
        with open(self.config, "w") as f:
            json.dump({"argv": [sys.executable, os.path.join(self.tmp.name, "proxy.py"),
                                self.proxy_log] + list(extra),
                       "env": {"XDG_STATE_HOME": "/s"}, "upstream": upstream,
                       "portFile": self.port_file, "pidFile": self.pid_file}, f)

    def env(self, mode, **extra):
        env = dict(os.environ, FAKE_QEMU=mode, FAKE_QEMU_LOG=self.qemu_log,
                   FAKE_PORT_FILE=self.port_file, NONO_ALLOW_DOMAIN="leaked.example",
                   **{qw.REAL_VAR: self.qemu, qw.CONFIG_VAR: self.config})
        env.update(extra)
        return env

    def proxies(self):
        if not os.path.exists(self.proxy_log):
            return []
        with open(self.proxy_log) as f:
            return [json.loads(ln) for ln in f]

    def start(self, mode="wait", **extra):
        p = subprocess.Popen([sys.executable, WRAPPER] + boot_args(), env=self.env(mode, **extra),
                             stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        self.addCleanup(self.kill_leftovers)
        return p

    def kill_leftovers(self):
        """A failing test must not leave a fake QEMU or proxy running."""
        pids = [p["pid"] for p in self.proxies()]
        if os.path.exists(self.qemu_log):
            with open(self.qemu_log) as f:
                pids.append(json.load(f)["pid"])
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_qemu_runs_behind_the_proxy_and_the_proxy_goes_with_it(self):
        p = self.start("exit:7")
        self.assertEqual(p.wait(timeout=20), 7, p.stderr.read())
        [proxy] = self.proxies()
        self.assertFalse(alive(proxy["pid"]))
        self.assertEqual(proxy["state"], "/s")
        self.assertIsNone(proxy["leak"])
        with open(self.port_file) as f:
            self.assertEqual(f.read().strip(), proxy["argv"][proxy["argv"].index("--port") + 1])
        with open(self.qemu_log) as f:
            argv = json.load(f)["argv"]
        netdev = argv[argv.index("-netdev") + 1]
        self.assertIn(",restrict=on,guestfwd=tcp:192.168.5.100:3128-cmd:sh -c", netdev)
        self.assertIn(f"$(cat {self.port_file})", netdev)
        self.assertEqual(argv[-2:], ["-device", qw.BALLOON])
        self.assertFalse(os.path.exists(self.pid_file))

    def test_sigterm_reaches_qemu_and_stops_the_proxy(self):
        p = self.start()
        self.assertTrue(wait_for(lambda: os.path.exists(self.qemu_log)))
        self.assertTrue(wait_for(lambda: os.path.exists(self.pid_file)))
        with open(self.pid_file) as f:
            self.assertEqual(int(f.read()), p.pid)
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=20), 0, p.stderr.read())
        self.assertFalse(alive(self.proxies()[0]["pid"]))

    def test_sigusr1_starts_a_second_proxy(self):
        self.write_config(extra=["--allow-domain", "first.example"])
        p = self.start()
        self.assertTrue(wait_for(lambda: os.path.exists(self.pid_file)))
        with open(self.port_file) as f:
            first = f.read()
        self.write_config(extra=["--allow-domain", "second.example"])
        p.send_signal(signal.SIGUSR1)
        self.assertTrue(wait_for(lambda: len(self.proxies()) == 2), p.stderr)
        self.assertTrue(wait_for(lambda: open(self.port_file).read() != first))
        old, new = self.proxies()
        self.assertIn("second.example", new["argv"])
        # The new proxy stays up (a thread that started it and ended
        # took it down with PR_SET_PDEATHSIG), and so does the old one.
        self.assertFalse(wait_for(lambda: not alive(new["pid"]), timeout=1))
        self.assertTrue(alive(old["pid"]))
        p.send_signal(signal.SIGUSR1)
        self.assertTrue(wait_for(lambda: len(self.proxies()) == 3), p.stderr)
        self.assertFalse(wait_for(lambda: not alive(self.proxies()[2]["pid"]), timeout=1))
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=20), 0)
        self.assertFalse(any(alive(x["pid"]) for x in self.proxies()))

    def test_an_upstream_with_credentials_goes_through_the_shim(self):
        self.write_config(upstream={"host": "127.0.0.1", "port": 9, "user": "nono",
                                    "password": "x"})
        p = self.start("exit:0")
        self.assertEqual(p.wait(timeout=20), 0, p.stderr.read())
        argv = self.proxies()[0]["argv"]
        up = argv[argv.index("--upstream-proxy") + 1]
        self.assertTrue(up.startswith("127.0.0.1:"))
        self.assertNotEqual(up, "127.0.0.1:9")

    def test_an_upstream_without_credentials_is_passed_as_it_is(self):
        self.write_config(upstream={"host": "proxy.example", "port": 3128, "user": "",
                                    "password": ""})
        p = self.start("exit:0")
        self.assertEqual(p.wait(timeout=20), 0, p.stderr.read())
        argv = self.proxies()[0]["argv"]
        self.assertEqual(argv[argv.index("--upstream-proxy") + 1], "proxy.example:3128")

    def test_a_proxy_that_fails_stops_the_boot(self):
        p = self.start("exit:0", FAKE_PROXY_FAIL="1")
        self.assertEqual(p.wait(timeout=20), 1)
        self.assertIn("nono proxy exited (2)", p.stderr.read())
        self.assertFalse(os.path.exists(self.qemu_log))

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_PDEATHSIG is Linux")
    def test_a_killed_wrapper_takes_qemu_and_the_proxy_with_it(self):
        p = self.start()
        self.assertTrue(wait_for(lambda: os.path.exists(self.qemu_log)))
        with open(self.qemu_log) as f:
            qemu = json.load(f)["pid"]
        p.kill()
        p.wait()
        self.assertTrue(wait_for(lambda: not alive(self.proxies()[0]["pid"])))
        self.assertTrue(wait_for(lambda: not alive(qemu)))


if __name__ == "__main__":
    unittest.main()
