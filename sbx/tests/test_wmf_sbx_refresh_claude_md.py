#!/usr/bin/env python3
"""Unit tests for wmf_sbx/refresh_claude_md.py (sbx/NOTES.md §95)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.create as create_mod  # noqa: E402
import wmf_sbx.kit as kit_mod  # noqa: E402
import wmf_sbx.refresh_claude_md as r  # noqa: E402
import wmf_sbx.setup as setup_mod  # noqa: E402


class FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def sandbox(files):
    """A fake `sbx exec` that runs exec_cat's command against `files`,
    with the text sbx prints when it has to start the sandbox."""
    def run(argv, **kw):
        path = argv[-1]
        if path not in files:
            return FakeCompletedProcess(1)
        return FakeCompletedProcess(
            0, stdout=f"Sandbox x started successfully\n{r.BEGIN}\n"
                      f"{files[path]}\n{r.END}\n")
    return run


UPSTREAM = """\
## Git workspace mode

Direct mode is /run/sandbox/source.

## Network access

Keep this.

### .NET Aspire: IPv6 loopback (`[::1]`) and the proxy

Aspire.

## Git Authentication

github.com. Pushing to GitHub.

## Claude Code: Environment Persistence

CLAUDE_ENV_FILE.
"""


class ExecCatTests(unittest.TestCase):
    def test_the_command_passes_the_path_as_an_argument(self):
        seen = []
        r.exec_cat("x", "/a b", run=lambda argv, **kw: seen.append(argv)
                   or FakeCompletedProcess(1))
        self.assertEqual(seen[0][:6],
                         [create_mod.WMF_SBX, "--upstream", "exec", "x", "--", "sh"])
        self.assertEqual(seen[0][-1], "/a b")

    def test_keeps_the_exact_text(self):
        for text in ("a\nb", "a\nb\n", ""):
            with self.subTest(text=text):
                self.assertEqual(r.exec_cat("x", "/f", run=sandbox({"/f": text})), text)

    def test_a_missing_file_is_none(self):
        self.assertIsNone(r.exec_cat("x", "/f", run=sandbox({})))


class ReadUpstreamTests(unittest.TestCase):
    def test_prefers_the_saved_copy(self):
        text, where = r.read_upstream("x", run=sandbox({
            setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM: "saved"}))
        self.assertEqual((text, where), ("saved", setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM))

    def test_falls_back_to_the_file_the_plan_names(self):
        plan = json.dumps({"primary": "/home/c/Extensions/Cite"})
        text, where = r.read_upstream("x", run=sandbox({
            setup_mod.SANDBOX_PLAN_FILE: plan,
            "/home/c/Extensions/CLAUDE.md": "live"}))
        self.assertEqual((text, where), ("live", "/home/c/Extensions/CLAUDE.md"))

    def test_an_edited_file_without_the_saved_copy_is_an_error(self):
        plan = json.dumps({"primary": "/home/c/Extensions/Cite"})
        with self.assertRaisesRegex(RuntimeError, "already edited"):
            r.read_upstream("x", run=sandbox({
                setup_mod.SANDBOX_PLAN_FILE: plan,
                "/home/c/Extensions/CLAUDE.md": setup_mod.CLAUDE_MD_MARKER + " -->\n"}))

    def test_no_plan_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "is it a wmf-sbx sandbox"):
            r.read_upstream("x", run=sandbox({}))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshot = os.path.join(self.tmp.name, "upstream.md")
        with open(self.snapshot, "w", encoding="utf-8") as f:
            f.write(UPSTREAM)
        p = mock.patch.object(kit_mod, "CLAUDE_MD_UPSTREAM_SNAPSHOT", self.snapshot)
        p.start()
        self.addCleanup(p.stop)

    def main(self, argv, files):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = r.main(argv, run=sandbox(files))
        return code, out.getvalue(), err.getvalue()

    def snapshot_text(self):
        with open(self.snapshot, encoding="utf-8") as f:
            return f.read()

    def test_unchanged_text_passes(self):
        code, out, _err = self.main(["x"], {setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM: UPSTREAM})
        self.assertEqual(code, 0)
        self.assertIn("no change", out)
        self.assertIn("all 4 edit(s) apply", out)

    def test_a_small_change_is_written_and_still_passes(self):
        changed = UPSTREAM.replace("CLAUDE_ENV_FILE.", "CLAUDE_ENV_FILE, reworded.")
        code, out, _err = self.main(["x"], {setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM: changed})
        self.assertEqual(code, 0)
        self.assertIn("+CLAUDE_ENV_FILE, reworded.", out)
        self.assertEqual(self.snapshot_text(), changed)

    def test_a_renamed_section_fails_and_names_the_edit(self):
        changed = UPSTREAM.replace("## Git workspace mode", "## Workspace modes")
        code, _out, err = self.main(["x"], {setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM: changed})
        self.assertEqual(code, 1)
        self.assertIn("'## Git workspace mode'", err)
        self.assertIn("no such heading", err)
        # Written anyway: the new snapshot is what the fixed edits must match.
        self.assertEqual(self.snapshot_text(), changed)

    def test_dry_run_does_not_write(self):
        changed = UPSTREAM + "\n## New\n"
        code, out, _err = self.main(["--dry-run", "x"],
                                    {setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM: changed})
        self.assertEqual(code, 0)
        self.assertIn("--dry-run", out)
        self.assertEqual(self.snapshot_text(), UPSTREAM)

    def test_file_reads_a_local_copy(self):
        local = os.path.join(self.tmp.name, "local.md")
        with open(local, "w", encoding="utf-8") as f:
            f.write(UPSTREAM)
        code, out, _err = self.main(["--file", local], {})
        self.assertEqual(code, 0)
        self.assertIn(local, out)


if __name__ == "__main__":
    unittest.main()
