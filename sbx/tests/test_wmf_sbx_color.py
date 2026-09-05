#!/usr/bin/env python3
"""Unit tests for wmf_sbx.color -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.color as color  # noqa: E402


class FakeTty(io.StringIO):
    def isatty(self):
        return True


class ColorTests(unittest.TestCase):
    def test_plain_text_when_not_a_tty(self):
        stream = io.StringIO()  # isatty() is False
        self.assertEqual(color.dim("+ git clone x y", stream=stream), "+ git clone x y")
        self.assertEqual(color.highlight("wmf-sbx-resume x", stream=stream),
                          "wmf-sbx-resume x")
        self.assertEqual(color.error("error: boom", stream=stream), "error: boom")

    def test_plain_text_when_no_color_is_set_even_on_a_tty(self):
        stream = FakeTty()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(color.dim("x", stream=stream), "x")
            self.assertEqual(color.highlight("x", stream=stream), "x")
            self.assertEqual(color.error("x", stream=stream), "x")

    def test_escape_codes_on_a_real_tty(self):
        stream = FakeTty()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertEqual(color.dim("x", stream=stream),
                              "\x1b[38;2;128;128;128mx\x1b[m")
            self.assertEqual(color.highlight("x", stream=stream),
                              "\x1b[38;2;122;162;247mx\x1b[m")
            self.assertEqual(color.error("x", stream=stream),
                              "\x1b[38;2;247;118;142mx\x1b[m")

    def test_a_stream_with_no_isatty_is_treated_as_not_a_tty(self):
        class NoIsatty:
            pass

        self.assertEqual(color.dim("x", stream=NoIsatty()), "x")

    def test_default_stream_is_resolved_at_call_time_not_import_time(self):
        # create.py's call sites never pass stream= explicitly, so they
        # rely on the default tracking whatever sys.stderr currently is --
        # including after something like contextlib.redirect_stderr
        # reassigns it post-import. A stream=sys.stderr default bound at
        # def time would miss that; this pins the call-time behavior.
        fake = FakeTty()
        with mock.patch.object(sys, "stderr", fake):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("NO_COLOR", None)
                self.assertEqual(color.dim("x"), "\x1b[38;2;128;128;128mx\x1b[m")
                self.assertEqual(color.highlight("x"), "\x1b[38;2;122;162;247mx\x1b[m")
                self.assertEqual(color.error("x"), "\x1b[38;2;247;118;142mx\x1b[m")


@contextlib.contextmanager
def _real_stdout_fd_capture():
    # pty._copy relays the child through the raw fd (os.write(1, ...)),
    # bypassing sys.stdout entirely -- so capturing it needs a real fd
    # swap, not a mock.patch.object(sys, "stdout", ...).
    r, w = os.pipe()
    saved = os.dup(1)
    os.dup2(w, 1)
    os.close(w)
    try:
        yield r
    finally:
        os.dup2(saved, 1)  # closes the dup'd write end that lived at fd 1
        os.close(saved)


def _read_all(fd):
    chunks = []
    while True:
        data = os.read(fd, 4096)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode()


class RunWithColorTests(unittest.TestCase):
    def test_falls_back_to_the_injected_run_when_not_a_tty(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return "fake-result"

        result = color.run_with_color(
            ["true"], env={"X": "1"}, run=fake_run, stream=io.StringIO(),
        )
        self.assertEqual(result, "fake-result")
        self.assertEqual(calls, [(["true"], {"env": {"X": "1"}})])

    def test_falls_back_when_no_color_is_set_even_on_a_tty(self):
        calls = []
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            color.run_with_color(
                ["true"], run=lambda cmd, **kw: calls.append(cmd), stream=FakeTty(),
            )
        self.assertEqual(calls, [["true"]])

    def test_falls_back_when_pty_is_not_importable(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return "fake-result"

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            with mock.patch.dict(sys.modules, {"pty": None}):
                result = color.run_with_color(
                    ["true"], run=fake_run, stream=FakeTty(),
                )
        self.assertEqual(result, "fake-result")
        self.assertEqual(calls, [["true"]])

    def test_falls_back_when_pty_fork_fails(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return "fake-result"

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            with mock.patch("pty.fork", side_effect=OSError("no ptys")):
                result = color.run_with_color(
                    ["true"], run=fake_run, stream=FakeTty(),
                )
        self.assertEqual(result, "fake-result")
        self.assertEqual(calls, [["true"]])

    def test_real_pty_gives_the_child_a_terminal(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            with _real_stdout_fd_capture() as r:
                result = color.run_with_color(
                    ["/bin/sh", "-c", "test -t 1 && echo tty || echo notty"],
                    stream=FakeTty(),
                )
            output = _read_all(r)
        os.close(r)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(output.strip(), "tty")

    def test_real_pty_passes_env_through_to_the_child(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            env = dict(os.environ, SOME_VAR="hi-from-run-with-color")
            with _real_stdout_fd_capture() as r:
                result = color.run_with_color(
                    ["/bin/sh", "-c", "echo $SOME_VAR"], env=env, stream=FakeTty(),
                )
            output = _read_all(r)
        os.close(r)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(output.strip(), "hi-from-run-with-color")


if __name__ == "__main__":
    unittest.main()
