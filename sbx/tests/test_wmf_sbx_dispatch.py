#!/usr/bin/env python3
"""Unit tests for bin/wmf-sbx's verb-redirect dispatch -- run with:
  python3 -m unittest discover -s sbx/tests -v

sbx/NOTES.md "wmf-sbx redirects": a bare `wmf-sbx create ...` (and the
other six REDIRECT_VERBS) should reach the matching `wmf-sbx-<verb>`
smart wrapper instead of the raw `sbx` binary, unless `--upstream` asks
for the old passthrough behavior explicitly.

Driven as a real subprocess, like test_git_review_check.py -- the
interesting behaviour here is bin/wmf-sbx's own shell logic (which verb
matched, which script it execs, whether SSH_AUTH_SOCK survives), so
faking it in Python would only re-assert my reading of the script.

Each test gets its own copy of bin/wmf-sbx in a fresh tempdir rather than
running it in place: the real bin/ directory already has real
wmf-sbx-<verb> siblings next to it, which would make "missing sibling"
and "PATH-fallback" cases impossible to set up, and would exercise the
real Python wrappers instead of the small stand-ins these tests need.
Stand-ins are plain recorder scripts that append one entry (their own
name, argv, and whatever SSH_AUTH_SOCK they saw) to a log file and exit;
read_log() parses that log back into dicts.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.__main__ as main_mod  # noqa: E402

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
REAL_WMF_SBX = os.path.join(BIN, "wmf-sbx")

HAVE_BASH = shutil.which("bash") is not None

_SEP = "\x1f"


def write_script(path, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\nset -u\n" + body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def write_recorder(path, log_file, exit_code=0):
    """A stub executable: appends one entry to `log_file` recording its
    own basename, argv, and whether SSH_AUTH_SOCK reached it, then exits
    `exit_code`. Never actually does anything -- these tests only care
    which script bin/wmf-sbx chose to run and with what."""
    write_script(path, f'''\
{{
  echo "NAME=$(basename "$0")"
  printf 'ARGV='
  if [ "$#" -gt 0 ]; then
    printf '%s{_SEP}' "$@"
  fi
  printf '\\n'
  echo "SSH_AUTH_SOCK=${{SSH_AUTH_SOCK-<unset>}}"
  echo '---'
}} >> {path_quote(log_file)}
exit {exit_code}
''')


def path_quote(path):
    # These are all tempdir-generated paths with no shell metacharacters;
    # a single-quote wrap is enough and keeps the recorder script readable.
    return "'" + path.replace("'", "'\\''") + "'"


def read_log(log_file):
    if not os.path.exists(log_file):
        return []
    with open(log_file, encoding="utf-8") as f:
        content = f.read()
    records = []
    for block in content.split("---\n"):
        block = block.strip("\n")
        if not block:
            continue
        record = {"argv": []}
        for line in block.splitlines():
            if line.startswith("NAME="):
                record["name"] = line[len("NAME="):]
            elif line.startswith("ARGV="):
                raw = line[len("ARGV="):]
                record["argv"] = raw.split(_SEP)[:-1] if raw else []
            elif line.startswith("SSH_AUTH_SOCK="):
                val = line[len("SSH_AUTH_SOCK="):]
                record["ssh_auth_sock"] = None if val == "<unset>" else val
        records.append(record)
    return records


@unittest.skipUnless(HAVE_BASH, "bash required")
class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = self.tmp.name

        # Our own copy, so its dirname is this tempdir -- not the real
        # bin/, whose co-located wmf-sbx-<verb> scripts are the real
        # Python wrappers, not these tests' stand-ins.
        self.wmf_sbx = os.path.join(self.work, "wmf-sbx")
        shutil.copyfile(REAL_WMF_SBX, self.wmf_sbx)
        os.chmod(self.wmf_sbx, 0o755)

        # A fake `sbx` stands in for the upstream binary everywhere.
        self.path_dir = os.path.join(self.work, "path")
        os.makedirs(self.path_dir)
        self.sbx_log = os.path.join(self.work, "sbx.log")
        write_recorder(os.path.join(self.path_dir, "sbx"), self.sbx_log)

    def run_wmf_sbx(self, args, binary=None, extra_path=None, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(
            p for p in [extra_path, self.path_dir, "/usr/bin", "/bin"] if p
        )
        env.pop("SSH_AUTH_SOCK", None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", binary or self.wmf_sbx] + args,
            capture_output=True, text=True, env=env,
        )

    def test_redirect_verb_dispatches_to_sibling_not_real_sbx(self):
        sibling_log = os.path.join(self.work, "create.log")
        write_recorder(os.path.join(self.work, "wmf-sbx-create"), sibling_log)
        result = self.run_wmf_sbx(["create", "foo", "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_log(self.sbx_log), [])
        records = read_log(sibling_log)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "wmf-sbx-create")
        self.assertEqual(records[0]["argv"], ["foo", "--dry-run"])

    def test_upstream_flag_forwards_to_real_sbx_raw(self):
        sibling_log = os.path.join(self.work, "create.log")
        write_recorder(os.path.join(self.work, "wmf-sbx-create"), sibling_log)
        result = self.run_wmf_sbx(["--upstream", "create", "foo"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_log(sibling_log), [])
        records = read_log(self.sbx_log)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["argv"], ["create", "foo"])

    def test_non_redirect_verb_falls_through_to_real_sbx(self):
        result = self.run_wmf_sbx(["ls"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_log(self.sbx_log)[0]["argv"], ["ls"])

    def test_no_args_falls_through(self):
        result = self.run_wmf_sbx([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_log(self.sbx_log)[0]["argv"], [])

    def test_help_flag_falls_through(self):
        result = self.run_wmf_sbx(["--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_log(self.sbx_log)[0]["argv"], ["--help"])

    def test_missing_sibling_errors_without_touching_real_sbx(self):
        # No wmf-sbx-rm anywhere -- neither co-located nor on PATH.
        result = self.run_wmf_sbx(["rm", "mw-cite"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wmf-sbx-rm", result.stderr)
        self.assertEqual(read_log(self.sbx_log), [])

    def test_sibling_found_via_path_fallback(self):
        # Sibling lives only on PATH, not next to this copy of wmf-sbx --
        # covers "only wmf-sbx is on the user's PATH" from the to-do.
        other_dir = os.path.join(self.work, "other")
        os.makedirs(other_dir)
        sibling_log = os.path.join(self.work, "exec.log")
        write_recorder(os.path.join(other_dir, "wmf-sbx-exec"), sibling_log)
        result = self.run_wmf_sbx(
            ["exec", "mw-cite", "true"], extra_path=other_dir
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        records = read_log(sibling_log)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["argv"], ["mw-cite", "true"])

    def test_symlink_invocation_still_finds_colocated_sibling(self):
        # Reached through a symlink (a ~/.local/bin entry, or a symlinked
        # checkout) -- the sibling lookup must resolve back to the real
        # file's directory, not the symlink's.
        sibling_log = os.path.join(self.work, "start.log")
        write_recorder(os.path.join(self.work, "wmf-sbx-start"), sibling_log)
        link_dir = os.path.join(self.work, "linkdir")
        os.makedirs(link_dir)
        link_path = os.path.join(link_dir, "wmf-sbx")
        os.symlink(self.wmf_sbx, link_path)
        result = self.run_wmf_sbx(["start", "mw-cite"], binary=link_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = read_log(sibling_log)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["argv"], ["mw-cite"])

    def test_ssh_auth_sock_stripped_before_redirect_dispatch(self):
        # Regression check for the bug this session found: the redirect
        # exec()s out early, before the point that used to be the only
        # place SSH_AUTH_SOCK got unset.
        sibling_log = os.path.join(self.work, "create.log")
        write_recorder(os.path.join(self.work, "wmf-sbx-create"), sibling_log)
        result = self.run_wmf_sbx(
            ["create", "foo"],
            env_extra={"SSH_AUTH_SOCK": "/tmp/fake-agent.sock"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(read_log(sibling_log)[0]["ssh_auth_sock"])

    def test_ssh_auth_sock_stripped_on_upstream_path(self):
        result = self.run_wmf_sbx(
            ["--upstream", "ls"],
            env_extra={"SSH_AUTH_SOCK": "/tmp/fake-agent.sock"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(read_log(self.sbx_log)[0]["ssh_auth_sock"])

    def test_cloud_flag_still_blocked_on_upstream_path(self):
        result = self.run_wmf_sbx(["--upstream", "--cloud", "ls"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing '--cloud'", result.stderr)
        self.assertEqual(read_log(self.sbx_log), [])

    def test_cloud_flag_still_blocked_on_fallthrough_path(self):
        # Not a redirect verb, and not --upstream either -- confirms the
        # dispatch addition didn't change how a plain, non-redirected
        # invocation reaches the existing --cloud guard.
        result = self.run_wmf_sbx(["--cloud", "ls"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing '--cloud'", result.stderr)
        self.assertEqual(read_log(self.sbx_log), [])


class RedirectVerbsAgreeTests(unittest.TestCase):
    """The set of redirected verbs is written out three times -- bin/wmf-
    sbx's own REDIRECT_VERBS, wmf_sbx.__main__.COMMANDS (its "same
    commands sbx/bin/wmf-sbx-<command> exposes" list), and the actual
    wmf-sbx-<verb> scripts on disk -- and nothing enforces they agree.
    Adding a bin/wmf-sbx-foo without also adding "foo" to REDIRECT_VERBS
    would leave `wmf-sbx foo` silently going to raw `sbx foo`, which is
    exactly the footgun this to-do exists to close. See the same pattern
    for the skill list in the repo's own CLAUDE.md."""

    def redirect_verbs(self):
        with open(REAL_WMF_SBX, encoding="utf-8") as f:
            script = f.read()
        m = re.search(r'^REDIRECT_VERBS="([^"]*)"', script, re.MULTILINE)
        self.assertIsNotNone(m, "REDIRECT_VERBS not found in bin/wmf-sbx")
        return set(m.group(1).split())

    def test_matches_main_module_commands(self):
        self.assertEqual(self.redirect_verbs(), set(main_mod.COMMANDS))

    def test_every_redirect_verb_has_a_sibling_script_on_disk(self):
        for verb in self.redirect_verbs():
            sibling = os.path.join(BIN, f"wmf-sbx-{verb}")
            self.assertTrue(
                os.path.isfile(sibling), f"missing sibling script: {sibling}"
            )


if __name__ == "__main__":
    unittest.main()
