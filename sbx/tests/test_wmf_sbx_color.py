#!/usr/bin/env python3
"""Unit tests for wmf_sbx.color -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.color as color  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


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
        self.assertEqual(color.warning("warning: x", stream=stream), "warning: x")

    def test_plain_text_when_no_color_is_set_even_on_a_tty(self):
        stream = FakeTty()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(color.dim("x", stream=stream), "x")
            self.assertEqual(color.highlight("x", stream=stream), "x")
            self.assertEqual(color.error("x", stream=stream), "x")
            self.assertEqual(color.warning("x", stream=stream), "x")

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
            self.assertEqual(color.warning("x", stream=stream),
                              "\x1b[38;2;255;158;100mx\x1b[m")

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


if __name__ == "__main__":
    unittest.main()
