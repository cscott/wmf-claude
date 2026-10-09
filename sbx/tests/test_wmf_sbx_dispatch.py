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

    def add_sibling(self, verb, directory=None):
        path = os.path.join(directory or self.work, f"wmf-sbx-{verb}")
        write_recorder(path, self.sibling_log)
        return path

    @property
    def sibling_log(self):
        return os.path.join(self.work, "sibling.log")

    def test_a_verb_runs_its_sibling_and_never_sbx(self):
        self.add_sibling("create")
        r = self.run_wmf_sbx(["create", "--dry-run", "Cite"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read_log(self.sibling_log),
                         [{"name": "wmf-sbx-create", "argv": ["--dry-run", "Cite"],
                           "ssh_auth_sock": None}])
        self.assertEqual(read_log(self.sbx_log), [])

    def test_no_verb_reaches_the_docker_sbx(self):
        for args in (["ls"], ["--upstream", "rm", "x"], ["policy", "ls"], ["frobnicate"],
                     ["create", "--cloud"], []):
            with self.subTest(args=args):
                self.run_wmf_sbx(args)
        self.assertEqual(read_log(self.sbx_log), [])

    def test_resume_and_run_are_not_ported_yet(self):
        for verb in ("resume", "run"):
            with self.subTest(verb=verb):
                r = self.run_wmf_sbx([verb, "x"])
                self.assertEqual(r.returncode, 1)
                self.assertIn("not ported to Lima yet", r.stderr)

    def test_docker_only_verbs_are_retired(self):
        for verb in ("refresh-claude-md", "settings", "ports"):
            with self.subTest(verb=verb):
                r = self.run_wmf_sbx([verb])
                self.assertEqual(r.returncode, 1)
                self.assertIn("retired", r.stderr)

    def test_an_unknown_verb_is_an_error(self):
        r = self.run_wmf_sbx(["frobnicate"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown verb", r.stderr)

    def test_help_and_no_args(self):
        r = self.run_wmf_sbx(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("wmf-sbx create PRIMARY", r.stdout)
        self.assertEqual(self.run_wmf_sbx([]).returncode, 1)

    def test_missing_sibling_errors(self):
        r = self.run_wmf_sbx(["start", "x"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't find 'wmf-sbx-start'", r.stderr)

    def test_sibling_found_via_path_fallback(self):
        other = os.path.join(self.work, "elsewhere")
        os.makedirs(other)
        self.add_sibling("stop", directory=other)
        r = self.run_wmf_sbx(["stop", "x"], extra_path=other)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read_log(self.sibling_log)[0]["name"], "wmf-sbx-stop")

    def test_symlink_invocation_still_finds_colocated_sibling(self):
        self.add_sibling("status")
        link_dir = os.path.join(self.work, "links")
        os.makedirs(link_dir)
        link = os.path.join(link_dir, "wmf-sbx")
        os.symlink(self.wmf_sbx, link)
        r = self.run_wmf_sbx(["status", "x"], binary=link)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read_log(self.sibling_log)[0]["name"], "wmf-sbx-status")

    def test_ssh_auth_sock_is_stripped(self):
        self.add_sibling("exec")
        r = self.run_wmf_sbx(["exec", "x", "--", "true"],
                             env_extra={"SSH_AUTH_SOCK": "/tmp/agent.sock"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(read_log(self.sibling_log)[0]["ssh_auth_sock"])


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
        m = re.search(r'^VERBS="([^"]*)"', script, re.MULTILINE)
        self.assertIsNotNone(m, "VERBS not found in bin/wmf-sbx")
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
