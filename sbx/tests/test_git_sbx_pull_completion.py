#!/usr/bin/env python3
"""Tests for sbx/completion/git-sbx-pull -- run with:
  python3 -m unittest discover -s sbx/tests -v

The completion is driven through git's own bash completion, as a shell
would: `git sbx-pull <TAB>` reaches _git_sbx_pull only by git's naming
rule, so a test that called the function directly would miss a wrong
name. Skipped where bash-completion or git's completion is not installed.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import isolation


def setUpModule():
    isolation.isolate()


HERE = os.path.dirname(os.path.abspath(__file__))
COMPLETION = os.path.join(HERE, "..", "completion", "git-sbx-pull")
BASH_COMPLETION = "/usr/share/bash-completion/bash_completion"
GIT_COMPLETION = "/usr/share/bash-completion/completions/git"

HAVE_TOOLS = (shutil.which("git") is not None and shutil.which("bash") is not None
              and os.path.exists(BASH_COMPLETION) and os.path.exists(GIT_COMPLETION))

# Completes the words in "$@" (the last is the one under the cursor) and
# prints one candidate per line.
DRIVER = r"""
source "$BASH_COMPLETION"
source "$GIT_COMPLETION"
COMP_WORDS=("$@"); COMP_CWORD=$((${#COMP_WORDS[@]} - 1))
COMP_LINE="${COMP_WORDS[*]}"; COMP_POINT=${#COMP_LINE}
COMPREPLY=()
__git_wrap__git_main
printf '%s\n' "${COMPREPLY[@]}"
"""


class ValidityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "bash required")
    def test_the_completion_is_valid_bash(self):
        self.assertEqual(subprocess.run(["bash", "-n", COMPLETION]).returncode, 0)


@unittest.skipUnless(HAVE_TOOLS, "git, bash, bash-completion and git's completion required")
class CompletionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "repo")
        subprocess.run(["git", "init", "-q", "-b", "master", self.repo], check=True)
        self.git("-c", "user.name=T", "-c", "user.email=t@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "first")
        for ref in ("origin/master", "origin/REL1_46", "origin/HEAD", "local/fix-thing",
                    "local/master", "sbx-cite/fix-thing", "sbx-cite/other",
                    "sbx-cite/master", "sbx-echo/fix-thing", "sbx-echo/REL1_46"):
            self.git("update-ref", f"refs/remotes/{ref}", "HEAD")
        # A stand-in for wmf-sbx-ls-remotes: this repo's sandbox remotes.
        bindir = os.path.join(tmp.name, "bin")
        os.mkdir(bindir)
        ls = os.path.join(bindir, "wmf-sbx-ls-remotes")
        with open(ls, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho sbx-cite\necho sbx-echo\n")
        os.chmod(ls, 0o755)
        # Lazy loading, as installed by the README: the file is linked
        # into the user's bash-completion directory, and nothing sources it.
        compdir = os.path.join(tmp.name, "bash-completion", "completions")
        os.makedirs(compdir)
        os.symlink(os.path.abspath(COMPLETION), os.path.join(compdir, "git-sbx-pull"))
        self.env = {k: v for k, v in os.environ.items()
                    if k != "BASH_COMPLETION_USER_DIR"}
        self.env.update(PATH=bindir + os.pathsep + os.environ["PATH"],
                        XDG_DATA_HOME=tmp.name, BASH_COMPLETION=BASH_COMPLETION,
                        GIT_COMPLETION=GIT_COMPLETION)

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo] + list(args), check=True,
                       capture_output=True)

    def complete(self, *words):
        res = subprocess.run(["bash", "-c", DRIVER, "driver", "git", "sbx-pull"] + list(words),
                             cwd=self.repo, env=self.env, capture_output=True, text=True,
                             check=True)
        return sorted(w.strip() for w in res.stdout.splitlines() if w.strip())

    def test_the_host_offers_each_sandbox_branch_bare_and_qualified(self):
        self.assertEqual(self.complete(""), [
            "fix-thing", "other", "sbx-cite/fix-thing", "sbx-cite/other",
            "sbx-echo/fix-thing"])

    def add_sandbox_remote(self, name):
        """A real remote `name`, a plain repo, with the host's refspecs."""
        path = os.path.join(os.path.dirname(self.repo), name)
        subprocess.run(["git", "clone", "-q", self.repo, path], check=True,
                       capture_output=True)
        self.git("remote", "add", name, path)
        self.git("config", f"remote.{name}.fetch", f"+refs/heads/*:refs/remotes/{name}/*")
        return path

    def test_it_fetches_a_branch_that_the_agent_made_since_the_last_fetch(self):
        sandbox = self.add_sandbox_remote("sbx-cite")
        subprocess.run(["git", "-C", sandbox, "branch", "brand-new"], check=True)
        self.assertIn("sbx-cite/brand-new", self.complete("sbx-cite/"))
        self.assertIn("brand-new", self.complete("b"))

    def test_a_second_tab_in_the_same_minute_does_not_fetch_again(self):
        sandbox = self.add_sandbox_remote("sbx-cite")
        self.complete("")
        subprocess.run(["git", "-C", sandbox, "branch", "later"], check=True)
        self.assertNotIn("later", self.complete(""))

    def test_the_host_leaves_out_branches_named_like_origin_branches(self):
        words = self.complete("")
        for word in ("master", "sbx-cite/master", "REL1_46", "sbx-echo/REL1_46"):
            self.assertNotIn(word, words)

    def test_a_prefix_narrows_the_list(self):
        self.assertEqual(self.complete("sbx-c"), ["sbx-cite/fix-thing", "sbx-cite/other"])

    def test_onto_offers_origin_branches(self):
        self.assertEqual(self.complete("--onto", ""), ["REL1_46", "master"])
        self.assertEqual(self.complete("--onto=R"), ["REL1_46"])

    def test_the_host_options(self):
        self.assertEqual(self.complete("--"), ["--force", "--onto="])


if __name__ == "__main__":
    unittest.main()
