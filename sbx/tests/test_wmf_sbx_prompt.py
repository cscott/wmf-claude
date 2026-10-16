#!/usr/bin/env python3
"""Tests for wmf_sbx.prompt (questions on the terminal) -- run with:
  python3 -m unittest discover -s sbx/tests -v"""

import contextlib
import io
import os
import sys
import unittest
import isolation

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.prompt as prompt_mod  # noqa: E402


def setUpModule():
    isolation.isolate()


def answers(*lines):
    """An input()-like ask that gives `lines`, then EOFError."""
    it = iter(lines)

    def ask(question):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return ask


class PromptTests(unittest.TestCase):
    def prompt(self, *lines):
        self.err = io.StringIO()
        return prompt_mod.Prompt(ask=answers(*lines), out=self.err)

    def test_pick_lists_the_labels_on_out(self):
        self.assertEqual(self.prompt("2").pick("Which?", ["a", "b"]), 1)
        self.assertIn("Which?\n  1) a\n  2) b\n", self.err.getvalue())

    def test_pick_by_label(self):
        self.assertEqual(self.prompt("b").pick("Which?", ["a", "b"]), 1)

    def test_pick_asks_again_after_a_bad_answer(self):
        self.assertEqual(self.prompt("x", "9", "1").pick("Which?", ["a", "b"]), 0)

    def test_pick_gives_up_after_three_bad_answers(self):
        self.assertIsNone(self.prompt("x", "y", "z", "1").pick("Which?", ["a", "b"]))

    def test_pick_enter_or_eof_is_none(self):
        self.assertIsNone(self.prompt("").pick("Which?", ["a"]))
        self.assertIsNone(self.prompt().pick("Which?", ["a"]))

    def test_confirm_is_no_unless_yes(self):
        for answer, want in (("y", True), ("YES ", True), ("", False), ("sure", False)):
            with self.subTest(answer=answer):
                self.assertEqual(self.prompt(answer).confirm("Go?"), want)
        self.assertFalse(self.prompt().confirm("Go?"))

    def test_the_default_ask_writes_to_stderr_and_reads_stdin(self):
        stdin = sys.stdin
        sys.stdin = io.StringIO("y\n")
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertTrue(prompt_mod.Prompt(isatty=lambda: True).confirm("Go?"))
        finally:
            sys.stdin = stdin
        self.assertEqual(err.getvalue(), "Go? [y/N] ")
        self.assertEqual(out.getvalue(), "")


class TerminalTests(unittest.TestCase):
    def test_with_no_terminal_the_default_asks_nothing_and_answers_no(self):
        stdin = sys.stdin
        sys.stdin = io.StringIO("y\n1\n")  # a pipe: never read
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertFalse(prompt_mod.interactive())
                p = prompt_mod.Prompt()
                self.assertFalse(p.available)
                self.assertFalse(p.confirm("Go?"))
                self.assertIsNone(p.pick("Which?", ["a"]))
                self.assertEqual(p.ask("Name?"), "")
            self.assertEqual(sys.stdin.read(), "y\n1\n")
        finally:
            sys.stdin = stdin
        self.assertEqual(err.getvalue(), "")

    def test_isatty_overrides_the_check(self):
        p = prompt_mod.Prompt(ask=answers("y"), isatty=lambda: False)
        self.assertFalse(p.confirm("Go?"))

    def test_a_callers_ask_is_asked_with_no_terminal(self):
        self.assertTrue(prompt_mod.Prompt(ask=answers("y"), out=io.StringIO()).confirm("Go?"))

    def test_both_must_be_ttys(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        self.assertTrue(prompt_mod.interactive(Tty(), Tty()))
        self.assertFalse(prompt_mod.interactive(Tty(), io.StringIO()))


if __name__ == "__main__":
    unittest.main()
