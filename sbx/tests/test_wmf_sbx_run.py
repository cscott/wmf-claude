#!/usr/bin/env python3
"""Unit tests for wmf_sbx_run.py -- run with:
  python3 -m unittest discover -s sbx/tests -v

wmf-sbx-run exists for exactly one reason: `sbx run --name NAME` is the
upstream re-attach spelling, and since sbx/NOTES.md "wmf-sbx redirects",
`wmf-sbx run ...` reaches this module instead of raw `sbx run`. With
--name it should delegate to wmf-sbx-resume (the wrapper that actually
does the mount-restore and remote-repoint work); without it -- the
create-a-new-sandbox shape of `sbx run` -- it should refuse and point at
`wmf-sbx create`, not silently forward to anything.

resume_mod.main is mocked throughout: these tests are about run.py's own
argv surgery and dispatch decision, not about re-testing resume.py's own
logic (that's test_wmf_sbx_resume.py's job).
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.run as run_mod  # noqa: E402


class ExtractNameTests(unittest.TestCase):
    def test_no_name_returns_none_and_leaves_argv_untouched(self):
        self.assertEqual(run_mod.extract_name(["--dry-run"]), (None, ["--dry-run"]))

    def test_space_form(self):
        self.assertEqual(
            run_mod.extract_name(["--name", "mw-cite", "--dry-run"]),
            ("mw-cite", ["--dry-run"]),
        )

    def test_equals_form(self):
        self.assertEqual(
            run_mod.extract_name(["--name=mw-cite", "--dry-run"]),
            ("mw-cite", ["--dry-run"]),
        )

    def test_name_is_removed_regardless_of_position(self):
        self.assertEqual(
            run_mod.extract_name(["--dry-run", "--name", "mw-cite"]),
            ("mw-cite", ["--dry-run"]),
        )

    def test_last_occurrence_wins_like_argparse_store(self):
        self.assertEqual(
            run_mod.extract_name(["--name", "first", "--name", "second"]),
            ("second", []),
        )

    def test_dangling_name_with_no_value_is_left_alone(self):
        # No value to consume -- leave it for whatever parses `rest` next
        # to complain about, rather than silently swallowing it.
        self.assertEqual(run_mod.extract_name(["--name"]), (None, ["--name"]))


class MainTests(unittest.TestCase):
    def test_no_name_errors_and_suggests_create_without_touching_resume(self):
        with mock.patch.object(run_mod.resume_mod, "main") as resume_main:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = run_mod.main(["claude", "/some/path"])
        self.assertEqual(code, 1)
        resume_main.assert_not_called()
        self.assertIn("wmf-sbx create", stderr.getvalue())

    def test_name_delegates_to_resume(self):
        with mock.patch.object(run_mod.resume_mod, "main", return_value=0) as resume_main:
            code = run_mod.main(["--name", "mw-cite"])
        self.assertEqual(code, 0)
        resume_main.assert_called_once()
        (argv,), kwargs = resume_main.call_args
        self.assertEqual(argv, ["mw-cite"])

    def test_name_with_create_positionals_errors_and_suggests_create(self):
        # `sbx run --name X claude /path` is still the create shape --
        # upstream's --name there just names the new sandbox. Forwarding
        # `claude /path` to resume would surface as a confusing
        # "unrecognized arguments" from resume's own argparse instead of
        # pointing at wmf-sbx create.
        with mock.patch.object(run_mod.resume_mod, "main") as resume_main:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = run_mod.main(["--name", "mw-cite", "claude", "/some/path"])
        self.assertEqual(code, 1)
        resume_main.assert_not_called()
        self.assertIn("wmf-sbx create", stderr.getvalue())

    def test_equals_form_delegates_too(self):
        with mock.patch.object(run_mod.resume_mod, "main", return_value=0) as resume_main:
            run_mod.main(["--name=mw-cite"])
        (argv,), _kwargs = resume_main.call_args
        self.assertEqual(argv, ["mw-cite"])

    def test_extra_ours_flags_are_forwarded_after_the_name(self):
        with mock.patch.object(run_mod.resume_mod, "main", return_value=0) as resume_main:
            run_mod.main(["--name", "mw-cite", "--dry-run"])
        (argv,), _kwargs = resume_main.call_args
        self.assertEqual(argv, ["mw-cite", "--dry-run"])

    def test_agent_args_after_separator_are_preserved(self):
        with mock.patch.object(run_mod.resume_mod, "main", return_value=0) as resume_main:
            run_mod.main(["--name", "mw-cite", "--", "--continue"])
        (argv,), _kwargs = resume_main.call_args
        self.assertEqual(argv, ["mw-cite", "--", "--continue"])

    def test_empty_tail_after_separator_is_still_forwarded(self):
        # An explicit `-- ` with nothing after it is a deliberate "no
        # agent args" from the caller, distinct from not passing -- at
        # all -- resume_mod.main tells those apart itself, so run.py just
        # has to not collapse the distinction.
        with mock.patch.object(run_mod.resume_mod, "main", return_value=0) as resume_main:
            run_mod.main(["--name", "mw-cite", "--"])
        (argv,), _kwargs = resume_main.call_args
        self.assertEqual(argv, ["mw-cite", "--"])

    def test_return_code_passed_through(self):
        with mock.patch.object(run_mod.resume_mod, "main", return_value=1):
            code = run_mod.main(["--name", "mw-cite"])
        self.assertEqual(code, 1)

    def test_run_and_env_forwarded_to_resume(self):
        fake_run = object()
        fake_env = {"FOO": "bar"}
        with mock.patch.object(run_mod.resume_mod, "main", return_value=0) as resume_main:
            run_mod.main(["--name", "mw-cite"], run=fake_run, env=fake_env)
        _args, kwargs = resume_main.call_args
        self.assertIs(kwargs["run"], fake_run)
        self.assertIs(kwargs["env"], fake_env)


if __name__ == "__main__":
    unittest.main()
