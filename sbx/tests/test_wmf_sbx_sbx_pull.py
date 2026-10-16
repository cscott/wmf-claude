#!/usr/bin/env python3
"""Tests for `git sbx-pull` on the host (sbx_pull.py) -- run with:
  python3 -m unittest discover -s sbx/tests -v

Driven against real repositories in tempdirs, like test_git_review_check:
an upstream (`up`, the host's `origin`), the host repo, and a sandbox
clone of the host, as sandbox-repos.sh makes it (`local` is the host,
`origin` the upstream). The host's sandbox remote is a plain path here,
not wmfsbx://; the guest's upstream is read from the clone directly, and
Gerrit is a set of shas."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.sbx_pull as sbx_pull  # noqa: E402
import wmf_sbx.prompt as prompt_mod  # noqa: E402
import isolation  # noqa: E402


def setUpModule():
    isolation.isolate()


HAVE_GIT = shutil.which("git") is not None


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True,
                          text=True, check=check)


def out(cwd, *args):
    return git(cwd, *args).stdout.strip()


def commit(repo, message, name="file"):
    with open(os.path.join(repo, name), "a", encoding="utf-8") as f:
        f.write(message + "\n")
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)
    return out(repo, "rev-parse", "HEAD")


def identity(repo):
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")


class PullCase(unittest.TestCase):
    """An upstream, the host, and helpers for sandboxes of the host."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.state = os.path.join(self.tmp, "state")
        self.env = {"XDG_STATE_HOME": self.state}
        self.up = os.path.join(self.tmp, "up")
        subprocess.run(["git", "init", "-q", "-b", "master", self.up], check=True)
        identity(self.up)
        self.base = commit(self.up, "first")
        git(self.up, "branch", "REL1_46")
        git(self.up, "checkout", "-q", "REL1_46")
        self.rel = commit(self.up, "release fix", name="rel")
        git(self.up, "checkout", "-q", "master")
        self.master = commit(self.up, "second")
        self.host = os.path.join(self.tmp, "host")
        subprocess.run(["git", "clone", "-q", self.up, self.host], check=True)
        identity(self.host)
        self.gerrit = set()
        self.msgs = []

    def sandbox(self, name, branch="master"):
        """A sandbox clone of the host on `branch`, and its host remote."""
        path = os.path.join(self.tmp, name)
        subprocess.run(["git", "clone", "-q", "-b", branch, self.host, path], check=True)
        identity(path)
        git(path, "remote", "rename", "origin", "local")
        git(path, "remote", "add", "origin", self.up)
        git(path, "fetch", "-q", "origin")
        git(self.host, "remote", "add", "--no-tags", name, path)
        git(self.host, "config", f"remote.{name}.wmfSbxSandbox", name)
        sandboxes = os.path.join(self.state, "wmf-sbx", "sandboxes")
        os.makedirs(sandboxes, exist_ok=True)
        saved = {"schemaVersion": 1, "name": name, "remotes": [
            {"hostDir": os.path.realpath(self.host), "remote": name,
             "url": f"wmfsbx://{name}{path}"}]}
        with open(os.path.join(sandboxes, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(saved, f)
        return path

    def upstream_of(self, entry, branch):
        path = entry["url"][len("wmfsbx://" + entry["sandbox"]):]
        return out(path, "for-each-ref", "--format=%(upstream)", f"refs/heads/{branch}")

    def pull(self, *argv, prompt=None):
        self.msgs = []
        return sbx_pull.main(list(argv), env=self.env, cwd=self.host,
                             sandbox_upstream_fn=self.upstream_of,
                             gerrit_has_fn=lambda root, sha: sha in self.gerrit,
                             say=self.msgs.append,
                             # No terminal, even when the tests run on one.
                             prompt=prompt or prompt_mod.Prompt(isatty=lambda: False))

    def said(self):
        return "\n".join(self.msgs)

    def head(self, repo=None):
        return out(repo or self.host, "rev-parse", "HEAD")

    def branch(self):
        return out(self.host, "symbolic-ref", "--short", "HEAD")

    def upstream(self, local):
        return out(self.host, "for-each-ref", "--format=%(upstream:short)",
                   f"refs/heads/{local}")

    def agent_branch(self, sb, name, start="origin/master", message="agent work"):
        git(sb, "checkout", "-q", "-b", name, "--track", start)
        return commit(sb, message)

    def hand_off(self, sb, branch):
        """What guest mode does: take the host's branch, and mark it seen."""
        git(sb, "fetch", "-q", "local")
        git(sb, "checkout", "-q", "-B", branch, f"local/{branch}")
        git(sb, "update-ref", f"refs/sbx/seen/{branch}", "HEAD")

    def amend(self, repo, message):
        with open(os.path.join(repo, "file"), "a", encoding="utf-8") as f:
            f.write(message + "\n")
        git(repo, "commit", "-qa", "--amend", "-m", message)
        return self.head(repo)


@unittest.skipUnless(HAVE_GIT, "git not installed")
class PullTests(PullCase):

    # case 1

    def test_first_pull_makes_a_branch_that_tracks_gerrit_and_records_the_sandbox(self):
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull(), 0, self.said())
        self.assertEqual((self.branch(), self.head()), ("fix-thing", tip))
        self.assertEqual(self.upstream("fix-thing"), "origin/master")
        self.assertIn("the sandbox branch's upstream", self.said())
        self.assertEqual(out(self.host, "config", "branch.fix-thing.wmfsbx"), "sbx-p")
        self.assertEqual(out(self.host, "config", "branch.fix-thing.wmfsbxBranch"),
                         "fix-thing")
        self.assertEqual(out(self.host, "config", "branch.fix-thing.wmfsbxSha"), tip)

    def test_branch_or_name_slash_branch(self):
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull("fix-thing"), 0, self.said())
        self.assertEqual(self.head(), tip)
        git(self.host, "checkout", "-q", "master")
        self.assertEqual(self.pull("sbx-p/fix-thing"), 0, self.said())
        self.assertEqual((self.branch(), self.head()), ("fix-thing", tip))

    def test_a_branch_in_two_sandboxes_needs_its_sandbox(self):
        self.agent_branch(self.sandbox("sbx-p"), "fix-thing")
        tip = self.agent_branch(self.sandbox("sbx-q"), "fix-thing", message="other")
        self.assertEqual(self.pull("fix-thing"), 1)
        self.assertIn("sbx-p/fix-thing, sbx-q/fix-thing", self.said())
        self.assertEqual(self.pull(), 1)
        self.assertIn("more than one new branch", self.said())
        self.assertEqual(self.pull("sbx-q/fix-thing"), 0, self.said())
        self.assertEqual(self.head(), tip)

    def test_a_branch_in_two_sandboxes_takes_the_sandbox_of_the_hosts_branch(self):
        self.agent_branch(self.sandbox("sbx-p"), "fix-thing")
        sb_q = self.sandbox("sbx-q")
        self.agent_branch(sb_q, "fix-thing", message="other")
        self.assertEqual(self.pull("sbx-q/fix-thing"), 0, self.said())
        tip = commit(sb_q, "more")
        git(self.host, "checkout", "-q", "master")
        self.assertEqual(self.pull("fix-thing"), 0, self.said())
        self.assertEqual((self.branch(), self.head()), ("fix-thing", tip))

    def test_no_new_branch(self):
        self.sandbox("sbx-p")
        self.assertEqual(self.pull(), 1)
        self.assertIn("no new branch", self.said())

    def test_a_sandbox_that_cannot_be_fetched_is_said(self):
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "fix-thing")
        self.sandbox("sbx-q")
        git(self.host, "remote", "set-url", "sbx-q", os.path.join(self.tmp, "gone"))
        self.assertEqual(self.pull(), 0, self.said())
        self.assertIn("cannot fetch from sbx-q: is it running? (wmf-sbx start sbx-q)",
                      self.said())
        self.assertEqual(self.head(), tip)

    def test_no_sandbox_remote(self):
        self.assertEqual(self.pull(), 1)
        self.assertIn("no sandbox remote", self.said())

    def test_a_remote_without_the_marker_is_not_a_sandbox(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        git(self.host, "config", "--unset", "remote.sbx-p.wmfSbxSandbox")
        self.assertEqual(self.pull(), 1)
        self.assertIn("no sandbox remote", self.said())

    def test_pull_again_after_a_hand_off(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull(), 0, self.said())
        mine = self.amend(self.host, "engineer's edit")
        self.hand_off(sb, "fix-thing")
        theirs = self.amend(sb, "agent's edit")
        # From the branch, with no name: its record.
        self.assertEqual(self.pull(), 0, self.said())
        self.assertEqual(self.head(), theirs)
        self.assertIn(f"{mine[:12]} -> {theirs[:12]}", self.said())

    def test_a_recorded_branch_the_agent_deleted(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull(), 0, self.said())
        git(sb, "checkout", "-q", "master")
        git(sb, "branch", "-qD", "fix-thing")
        self.assertEqual(self.pull(), 1)
        self.assertIn("sbx-p has no branch fix-thing now", self.said())

    def test_a_change_the_agent_did_not_take_stops_the_move(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull(), 0, self.said())
        mine = self.amend(self.host, "engineer's edit")
        git(sb, "checkout", "-q", "fix-thing")
        theirs = self.amend(sb, "agent's edit, without the hand-off")
        self.assertEqual(self.pull(), 1)
        self.assertIn(f"fix-thing ({mine[:12]}) has commits", self.said())
        self.assertEqual(self.head(), mine)
        self.assertEqual(self.pull("-f"), 0, self.said())
        self.assertEqual(self.head(), theirs)
        self.assertIn(f"{mine[:12]} -> ", self.said())
        self.assertEqual(out(self.host, "rev-parse", "fix-thing@{1}"), mine)

    def test_a_pulled_tip_is_moved_without_a_hand_off(self):
        # The agent amends its own commit, which the host has not touched.
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull(), 0, self.said())
        theirs = self.amend(sb, "agent's second try")
        self.assertEqual(self.pull("fix-thing"), 0, self.said())
        self.assertEqual(self.head(), theirs)

    def test_a_dirty_tree_stops_leaving(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        with open(os.path.join(self.host, "file"), "a", encoding="utf-8") as f:
            f.write("dirty\n")
        self.assertEqual(self.pull("-f"), 1)
        self.assertIn("the tree has changes", self.said())
        self.assertEqual(self.branch(), "master")

    def test_leaving_work_that_is_saved_nowhere(self):
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "fix-thing")
        wip = commit(self.host, "WIP")
        self.assertEqual(self.pull(), 1)
        self.assertIn(f"master ({wip[:12]}) is not on Gerrit, origin or a sandbox",
                      self.said())
        self.assertEqual(self.branch(), "master")
        self.gerrit.add(wip)
        self.assertEqual(self.pull(), 0, self.said())
        self.assertEqual(self.head(), tip)
        self.assertEqual(out(self.host, "rev-parse", "master"), wip)

    def test_leaving_work_with_f(self):
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "fix-thing")
        wip = commit(self.host, "WIP")
        self.assertEqual(self.pull("--force"), 0, self.said())
        self.assertEqual(self.head(), tip)
        self.assertEqual(out(self.host, "rev-parse", "master"), wip)

    def test_leaving_work_the_agent_has(self):
        # Case 3: a WIP commit on master, which the agent fetched.
        wip = commit(self.host, "WIP")
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "wip-thing", start="local/master")
        self.assertEqual(self.pull(), 0, self.said())
        self.assertEqual(self.head(), tip)
        # The sandbox branch tracks local/master; master tracks origin/master.
        self.assertEqual(self.upstream("wip-thing"), "origin/master")
        self.assertIn("the upstream of master, which the sandbox branch tracks",
                      self.said())
        self.assertEqual(out(self.host, "rev-parse", "master"), wip)

    def test_switching_back_to_a_recorded_branch(self):
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "fix-thing")
        other = self.agent_branch(sb, "other", message="other work")
        self.assertEqual(self.pull("fix-thing"), 0, self.said())
        # The agent goes on; the host's tip is no longer in the sandbox.
        git(sb, "checkout", "-q", "fix-thing")
        newer = self.amend(sb, "agent's second try")
        self.assertEqual(self.pull("other"), 0, self.said())
        self.assertEqual(self.head(), other)
        self.assertEqual(out(self.host, "rev-parse", "fix-thing"), tip)
        self.assertEqual(self.pull("fix-thing"), 0, self.said())
        self.assertEqual((self.branch(), self.head()), ("fix-thing", newer))

    def test_a_branch_that_is_another_sandboxs_stops(self):
        self.agent_branch(self.sandbox("sbx-q"), "fix-thing", message="q's")
        self.assertEqual(self.pull("sbx-q/fix-thing"), 0, self.said())
        git(self.host, "checkout", "-q", "master")
        self.agent_branch(self.sandbox("sbx-p"), "fix-thing")
        self.assertEqual(self.pull("sbx-p/fix-thing"), 1)
        self.assertIn("branch fix-thing is sbx-q's (sbx-q/fix-thing); rename one",
                      self.said())
        self.assertEqual(self.branch(), "master")

    # case 2

    def review_branch(self, onto="master"):
        """`git review -d`: a branch at a commit on Gerrit, tracking origin."""
        git(self.host, "checkout", "-q", "-b", "review/someone/1354096", "--track",
            f"origin/{onto}")
        change = commit(self.host, "the change")
        self.gerrit.add(change)
        return change

    def test_case_2_the_review_branch_takes_the_agents_amend(self):
        self.review_branch()
        sb = self.sandbox("sbx-p", branch="review/someone/1354096")
        theirs = self.amend(sb, "the change, amended")
        self.assertEqual(self.pull(), 0, self.said())
        self.assertEqual((self.branch(), self.head()), ("review/someone/1354096", theirs))
        self.assertEqual(self.upstream("review/someone/1354096"), "origin/master")
        self.assertEqual(out(self.host, "config",
                             "branch.review/someone/1354096.wmfsbx"), "sbx-p")

    def test_case_2_an_agent_branch_with_no_upstream_takes_the_review_branchs(self):
        change = self.review_branch(onto="REL1_46")
        sb = self.sandbox("sbx-p", branch="review/someone/1354096")
        git(sb, "checkout", "-q", "--no-track", "-b", "agent-branch", change)
        tip = commit(sb, "agent work")
        self.assertEqual(self.pull(), 0, self.said())
        self.assertEqual((self.branch(), self.head()), ("agent-branch", tip))
        self.assertEqual(self.upstream("agent-branch"), "origin/REL1_46")
        self.assertIn("the upstream of review/someone/1354096, which the work sits on",
                      self.said())

    def test_a_sandbox_remote_is_never_the_upstream(self):
        # master tracks the sandbox, as the old `git safe-reset NAME/BRANCH` left it.
        sb = self.sandbox("sbx-p")
        git(self.host, "fetch", "-q", "sbx-p")
        git(self.host, "branch", "-q", "--set-upstream-to=sbx-p/master", "master")
        git(sb, "checkout", "-q", "--no-track", "-b", "fix-thing")
        commit(sb, "agent work")
        self.assertEqual(self.pull(), 0, self.said())
        self.assertEqual(self.upstream("fix-thing"), "origin/master")
        self.assertIn("a guess", self.said())

    def test_onto(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull("--onto", "REL1_46"), 0, self.said())
        self.assertEqual(self.upstream("fix-thing"), "origin/REL1_46")
        self.assertEqual(self.pull("--onto", "origin/master"), 0, self.said())
        self.assertEqual(self.upstream("fix-thing"), "origin/master")
        self.assertEqual(self.pull("--onto", "REL9_99"), 1)
        self.assertIn("there is no origin/REL9_99", self.said())

    def test_the_guess_is_the_branch_with_the_fewest_commits(self):
        sb = self.sandbox("sbx-p")
        git(sb, "checkout", "-q", "--no-track", "-b", "backport", self.rel)
        tip = commit(sb, "backport")
        self.assertEqual(self.pull(), 0, self.said())
        self.assertEqual(self.head(), tip)
        self.assertEqual(self.upstream("backport"), "origin/REL1_46")
        self.assertIn("a guess", self.said())

    def test_the_guess_without_ahead_behind(self):
        sb = self.sandbox("sbx-p")
        git(sb, "checkout", "-q", "--no-track", "-b", "backport", self.rel)
        tip = commit(sb, "backport")
        g = sbx_pull.Git(self.host)
        git(self.host, "fetch", "-q", "sbx-p", "+refs/heads/*:refs/remotes/sbx-p/*")

        def old_git(argv, **kw):
            if any("ahead-behind" in a for a in argv):
                return subprocess.CompletedProcess(argv, 128, "", "fatal: bad atom")
            return subprocess.run(argv, **kw)
        g.run = old_git
        self.assertEqual(sbx_pull.guess_upstream(g, tip), "REL1_46")
        self.assertEqual(sbx_pull.guess_upstream(g, self.master), "master")


class FakePrompt:
    """Answers from a list; records the questions."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def pick(self, question, labels, default=None):
        self.asked.append((question, labels))
        self.default = default
        return self.answers.pop(0)

    def confirm(self, question, default=False):
        self.asked.append((question, None))
        self.default = default
        return self.answers.pop(0)


@unittest.skipUnless(HAVE_GIT, "git not installed")
class PromptPullTests(PullCase):
    """On a terminal: a choice is asked, not refused."""

    def test_more_than_one_new_branch_is_a_list(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "aaa")
        git(sb, "checkout", "-q", "master")
        tip = self.agent_branch(sb, "bbb")
        prompt = FakePrompt(1)
        self.assertEqual(self.pull(prompt=prompt), 0, self.said())
        self.assertEqual(prompt.asked[0][1], ["sbx-p/aaa", "sbx-p/bbb"])
        self.assertEqual((self.branch(), self.head()), ("bbb", tip))

    def test_an_empty_answer_is_the_old_stop(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "aaa")
        git(sb, "checkout", "-q", "master")
        self.agent_branch(sb, "bbb")
        self.assertEqual(self.pull(prompt=FakePrompt(None)), 1)
        self.assertIn("more than one new branch; name one", self.said())
        self.assertEqual(self.branch(), "master")

    def test_a_branch_in_two_sandboxes_is_a_list(self):
        self.agent_branch(self.sandbox("sbx-p"), "fix-thing")
        tip = self.agent_branch(self.sandbox("sbx-q"), "fix-thing", message="other")
        prompt = FakePrompt(1)
        self.assertEqual(self.pull("fix-thing", prompt=prompt), 0, self.said())
        self.assertEqual(prompt.asked[0][1], ["sbx-p/fix-thing", "sbx-q/fix-thing"])
        self.assertEqual(self.head(), tip)

    def test_no_new_branch_lists_every_branch_but_origins(self):
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull(), 0, self.said())
        git(self.host, "checkout", "-q", "master")
        prompt = FakePrompt(0)
        self.assertEqual(self.pull(prompt=prompt), 0, self.said())
        self.assertEqual(prompt.asked[0][1], ["sbx-p/fix-thing"])
        self.assertEqual((self.branch(), self.head()), ("fix-thing", tip))

    def test_two_local_branches_of_one_sandbox_branch_is_a_list(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull(), 0, self.said())
        git(self.host, "branch", "fix-copy")
        for key, value in (("wmfsbx", "sbx-p"), ("wmfsbxBranch", "fix-thing")):
            git(self.host, "config", f"branch.fix-copy.{key}", value)
        git(self.host, "checkout", "-q", "master")
        tip = commit(sb, "more")
        prompt = FakePrompt(1)
        self.assertEqual(self.pull("fix-thing", prompt=prompt), 0, self.said())
        self.assertEqual(prompt.asked[0][1], ["fix-copy", "fix-thing"])
        self.assertEqual(prompt.default, 1)  # the sandbox branch's own name
        self.assertEqual((self.branch(), self.head()), ("fix-thing", tip))

    def test_leaving_unsaved_work_asks(self):
        sb = self.sandbox("sbx-p")
        tip = self.agent_branch(sb, "fix-thing")
        wip = commit(self.host, "WIP")
        prompt = FakePrompt(False)
        self.assertEqual(self.pull(prompt=prompt), 1)
        self.assertIn("Leave it anyway", prompt.asked[0][0])
        self.assertIn("-f to leave it anyway", self.said())
        self.assertEqual(self.branch(), "master")
        self.assertEqual(self.pull(prompt=FakePrompt(True)), 0, self.said())
        self.assertEqual(self.head(), tip)
        self.assertEqual(out(self.host, "rev-parse", "master"), wip)

    def test_moving_over_unsaved_work_asks(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        self.assertEqual(self.pull(), 0, self.said())
        mine = self.amend(self.host, "engineer's edit")
        theirs = self.amend(sb, "agent's edit, without the hand-off")
        prompt = FakePrompt(False)
        self.assertEqual(self.pull(prompt=prompt), 1)
        self.assertIn("Move fix-thing anyway", prompt.asked[0][0])
        self.assertEqual(self.head(), mine)
        self.assertEqual(self.pull(prompt=FakePrompt(True)), 0, self.said())
        self.assertEqual(self.head(), theirs)
        self.assertEqual(out(self.host, "rev-parse", "fix-thing@{1}"), mine)

    def test_a_dirty_tree_never_asks(self):
        sb = self.sandbox("sbx-p")
        self.agent_branch(sb, "fix-thing")
        with open(os.path.join(self.host, "file"), "a", encoding="utf-8") as f:
            f.write("dirty\n")
        prompt = FakePrompt()
        self.assertEqual(self.pull(prompt=prompt), 1)
        self.assertEqual(prompt.asked, [])


class GerritTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.urls = []

    def gitreview(self, host="gerrit.wikimedia.org"):
        with open(os.path.join(self.root, ".gitreview"), "w", encoding="utf-8") as f:
            f.write(f"[gerrit]\nhost={host}\nport=29418\ntrack=1\n")

    def urlopen(self, body):
        import io

        def fake(req, timeout):
            if isinstance(req, str):
                self.urls.append((req, {}))
            else:
                self.urls.append((req.full_url, req.header_items()))
            return io.BytesIO(body)
        return fake

    def test_a_change_with_the_commit(self):
        self.gitreview()
        self.assertTrue(sbx_pull.gerrit_has(
            self.root, "abc", env={},
            urlopen=self.urlopen(b')]}\'\n[{"change_id":"I123"}]\n')))
        ((url, headers),) = self.urls
        self.assertEqual(url, "https://gerrit.wikimedia.org/r/changes/?q=commit%3Aabc&n=1")
        self.assertEqual(headers, {})

    def test_gerritua_goes_to_gerrit(self):
        self.gitreview()
        sbx_pull.gerrit_has(self.root, "abc", env={"GERRITUA": "secret-ua"},
                            urlopen=self.urlopen(b")]}'\n[]\n"))
        ((_, headers),) = self.urls
        self.assertEqual(dict(headers), {"User-agent": "secret-ua"})

    def test_no_other_host_gets_gerritua(self):
        self.gitreview("gerrit.example.invalid")
        sbx_pull.gerrit_has(self.root, "abc", env={"GERRITUA": "secret-ua"},
                            urlopen=self.urlopen(b")]}'\n[]\n"))
        ((url, headers),) = self.urls
        self.assertTrue(url.startswith("https://gerrit.example.invalid/"))
        self.assertEqual(headers, {})

    def test_no_change(self):
        self.gitreview()
        self.assertFalse(sbx_pull.gerrit_has(self.root, "abc", env={},
                                             urlopen=self.urlopen(b")]}'\n[]\n")))

    def test_no_gitreview_asks_nobody(self):
        self.assertFalse(sbx_pull.gerrit_has(self.root, "abc", env={},
                                             urlopen=self.urlopen(b"")))
        self.assertEqual(self.urls, [])

    def test_unreachable(self):
        self.gitreview()

        def fail(req, timeout):
            raise OSError("no route")
        self.assertFalse(sbx_pull.gerrit_has(self.root, "abc", env={}, urlopen=fail))


if __name__ == "__main__":
    unittest.main()
