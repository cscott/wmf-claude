#!/usr/bin/env python3
"""Unit tests for the clipboard listener and the guest's wl-copy shim
(clipboard.py) -- run with:
  python3 -m unittest discover -s sbx/tests -v

The host's tool is a fake here; the copy to a real Wayland clipboard
was RAN by hand (lima-port/HANDOFF-CLIPBOARD.md)."""

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.clipboard as clipboard  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


def which_all(name):
    return f"/usr/bin/{name}"


def which_none(name):
    return None


class HostCommandTests(unittest.TestCase):

    def test_wayland_uses_wl_copy_for_both_selections(self):
        env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
        self.assertEqual(clipboard.host_command("clipboard", env, "linux", which_all),
                         ["wl-copy", "--type", "text/plain;charset=utf-8"])
        self.assertEqual(clipboard.host_command("primary", env, "linux", which_all),
                         ["wl-copy", "--type", "text/plain;charset=utf-8", "--primary"])

    def test_x11_uses_xclip(self):
        env = {"DISPLAY": ":0"}
        self.assertEqual(clipboard.host_command("primary", env, "linux", which_all),
                         ["xclip", "-selection", "primary"])

    def test_wayland_without_wl_copy_falls_back_to_xclip(self):
        env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
        which = lambda n: None if n == "wl-copy" else which_all(n)  # noqa: E731
        self.assertEqual(clipboard.host_command("clipboard", env, "linux", which),
                         ["xclip", "-selection", "clipboard"])

    def test_macos_has_no_primary(self):
        self.assertEqual(clipboard.host_command("clipboard", {}, "darwin", which_none),
                         ["pbcopy"])
        self.assertIsNone(clipboard.host_command("primary", {}, "darwin", which_none))

    def test_no_display_no_tool(self):
        # an SSH login to the host, say
        self.assertIsNone(clipboard.host_command("clipboard", {}, "linux", which_all))
        self.assertIsNone(clipboard.host_command(
            "clipboard", {"WAYLAND_DISPLAY": "w"}, "linux", which_none))


class ListenerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "clip.sock")
        self.log_path = os.path.join(self.tmp.name, "clip.log")
        self.ran = []
        self.done = threading.Event()
        self.returncode = 0

        def run(argv, **kw):
            self.ran.append((argv, kw))
            self.done.set()
            return subprocess.CompletedProcess(argv, self.returncode)
        self.listener = clipboard.Listener(self.path, {"WAYLAND_DISPLAY": "wayland-0"},
                                           log_path=self.log_path, run=run,
                                           platform="linux", which=which_all)
        self.listener.start()
        self.addCleanup(self.listener.stop)

    def send(self, data):
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(self.path)
            s.sendall(data)

    def log(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.path.exists(self.log_path):
                with open(self.log_path, encoding="utf-8") as f:
                    text = f.read()
                if text:
                    return text
            time.sleep(0.01)
        self.fail("nothing logged")

    def test_the_socket_is_the_owners_only(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_copy_reaches_the_hosts_tool_and_the_log_has_no_text(self):
        self.send("primary\nnono décrit\n".encode())
        self.assertTrue(self.done.wait(5))
        ((argv, kw),) = self.ran
        self.assertEqual(argv[-1], "--primary")
        self.assertEqual(kw["input"], "nono décrit\n".encode())
        self.assertTrue(kw["start_new_session"])
        log = self.log()
        self.assertRegex(log, r"primary 13B copied\n$")
        self.assertNotIn("nono", log)

    def test_a_failed_tool_is_logged(self):
        self.returncode = 1
        self.send(b"clipboard\nhi")
        self.assertRegex(self.log(), r"clipboard 2B failed: wl-copy exit 1\n$")

    def test_bad_writes_are_refused(self):
        for data, why in ((b"paste\nhi", "no selection"),
                          (b"clipboard\n", "clipboard refused: empty"),
                          (b"clipboard\n" + b"x" * (clipboard.MAX_BYTES + 1),
                           "more than 1048576 bytes")):
            with self.subTest(why=why):
                if os.path.exists(self.log_path):
                    os.unlink(self.log_path)
                self.send(data)
                self.assertIn(why, self.log())
        self.assertEqual(self.ran, [])

    def test_the_largest_write_is_taken(self):
        self.send(b"clipboard\n" + b"x" * clipboard.MAX_BYTES)
        self.assertTrue(self.done.wait(5))
        self.assertEqual(len(self.ran[0][1]["input"]), clipboard.MAX_BYTES)

    def test_stop_removes_the_socket_but_not_a_later_one(self):
        self.listener.stop()
        self.assertFalse(os.path.exists(self.path))
        self.listener.start()
        os.unlink(self.path)
        with socket.socket(socket.AF_UNIX) as later:
            later.bind(self.path)
            self.listener.stop()
            self.assertTrue(os.path.exists(self.path))


class ShimTests(unittest.TestCase):
    """GUEST_SHIM, run by bash against a TCP listener in place of the
    guestfwd."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.shim = os.path.join(self.tmp.name, "wl-copy")
        with open(self.shim, "w", encoding="utf-8") as f:
            f.write(clipboard.GUEST_SHIM)
        os.chmod(self.shim, 0o755)

    def run_shim(self, args, text, env):
        return subprocess.run([self.shim] + list(args), input=text, capture_output=True,
                              env=dict(env, PATH=os.environ["PATH"]), timeout=10)

    def serve_once(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        got = []

        def accept():
            conn, _ = server.accept()
            with conn:
                chunks = []
                while True:
                    b = conn.recv(65536)
                    if not b:
                        break
                    chunks.append(b)
                got.append(b"".join(chunks))
        t = threading.Thread(target=accept, daemon=True)
        t.start()
        return f"127.0.0.1:{server.getsockname()[1]}", got, t

    def test_the_shim_is_valid_bash(self):
        self.assertEqual(subprocess.run(["bash", "-n", self.shim]).returncode, 0)

    def test_each_selection_is_named_and_the_text_follows(self):
        for args, sel in (([], b"clipboard"), (["--primary"], b"primary"),
                          (["-p"], b"primary"), (["--type", "text/plain"], b"clipboard")):
            with self.subTest(args=args):
                addr, got, t = self.serve_once()
                res = self.run_shim(args, "ligne 1\nligne 2 é".encode(),
                                    {clipboard.ADDRESS_VAR: addr})
                t.join(5)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(got, [sel + "\nligne 1\nligne 2 é".encode()])

    def test_no_address_fails(self):
        res = self.run_shim([], b"hi", {})
        self.assertEqual(res.returncode, 1)
        self.assertIn(b"WMF_CLAUDE_CLIPBOARD is not set", res.stderr)

    def test_no_listener_fails_fast(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]   # closed again, so refused
        start = time.monotonic()
        res = self.run_shim([], b"hi", {clipboard.ADDRESS_VAR: f"127.0.0.1:{port}"})
        self.assertNotEqual(res.returncode, 0)
        self.assertLess(time.monotonic() - start, 2)


if __name__ == "__main__":
    unittest.main()
