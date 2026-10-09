#!/usr/bin/env python3
"""Unit tests for wmf_sbx_remotes.py -- run with:
  python3 -m unittest discover -s sbx/tests -v

These drive **real git repositories** in tempdirs rather than faking
`subprocess.run`. The whole module is built on git's exit-code and config
contracts (`git remote add` exits 3 on a taken name, `git remote remove`
exits 2 on a missing one, `git remote remove` drops the whole
`remote.<name>.*` section including our ownership marker), and a fake
would just re-assert my assumptions about git instead of checking them.
The daemon URLs are local paths, which `git ls-remote` accepts, so even
unfetched_tips runs against real refs.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.remotes as m  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", cwd] + list(args), capture_output=True, text=True, check=True
    )


def make_repo(path, commit=True):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", path], check=True,
                   capture_output=True, text=True)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    if commit:
        with open(os.path.join(path, "README"), "w", encoding="utf-8") as f:
            f.write("hello\n")
        git(path, "add", "README")
        git(path, "commit", "-qm", "initial")
    return path


def remotes_of(path):
    return git(path, "remote").stdout.split()


@unittest.skipUnless(HAVE_GIT, "git not installed")
class RemoteNameForTests(unittest.TestCase):
    def test_matches_the_sandbox_name_exactly(self):
        # The sandbox name already carries the `sbx-` prefix (see
        # wmf_sbx_create.default_sandbox_name), so the remote gets no
        # second prefix on top of it -- `sbx-cite` in, `sbx-cite` out.
        self.assertEqual(m.remote_name_for("sbx-cite"), "sbx-cite")

    def test_a_custom_name_is_also_unprefixed(self):
        self.assertEqual(m.remote_name_for("custom"), "custom")


class GitContractTests(unittest.TestCase):
    """The two exit codes wmf_sbx_remotes treats as expected outcomes
    rather than failures. If a future git changes these, this is the test
    that should break -- not something subtle in `wmf-sbx-rm`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(os.path.join(self.tmp.name, "repo"))

    def test_remote_add_on_taken_name_exits_3(self):
        git(self.repo, "remote", "add", "origin", "git://x/y")
        result = subprocess.run(
            ["git", "-C", self.repo, "remote", "add", "origin", "git://a/b"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, m.EXIT_REMOTE_EXISTS)

    def test_remote_remove_on_missing_name_exits_2(self):
        result = subprocess.run(
            ["git", "-C", self.repo, "remote", "remove", "nope"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, m.EXIT_NO_SUCH_REMOTE)

    def test_remote_remove_drops_the_marker_too(self):
        # remove_remotes relies on this: it never has to clean the marker
        # up separately, so it can't leave a half-removed remote behind.
        git(self.repo, "remote", "add", "sandbox-x", "git://x/y")
        m.set_marker(self.repo, "sandbox-x", "x")
        git(self.repo, "remote", "remove", "sandbox-x")
        self.assertIsNone(m.marker_value(self.repo, "sandbox-x"))


@unittest.skipUnless(HAVE_GIT, "git not installed")
class SyncRemotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(os.path.join(self.tmp.name, "Cite"))
        self.warnings = []

    def candidate(self, host_dir=None, url="git://127.0.0.1:49281/Wikimedia/Cite", **kw):
        cand = {
            "hostDir": host_dir or self.repo,
            "remote": "sandbox-mw-cite",
            "url": url,
            "sandboxPath": "/home/agent/Wikimedia/Cite",
            "readOnly": False,
        }
        cand.update(kw)
        return cand

    def sync(self, candidates, name="mw-cite"):
        return m.sync_remotes(name, candidates, warn=self.warnings.append)

    def test_adds_a_marked_remote(self):
        remotes, skipped = self.sync([self.candidate()])
        self.assertEqual(skipped, [])
        self.assertEqual(len(remotes), 1)
        self.assertFalse(remotes[0]["adopted"])
        self.assertIn("sandbox-mw-cite", remotes_of(self.repo))
        self.assertEqual(m.marker_value(self.repo, "sandbox-mw-cite"), "mw-cite")

    def test_adds_with_no_tags(self):
        # Tags are one flat namespace per repo, so a default fetch would
        # drop the sandbox's tags in among the host's real ones.
        self.sync([self.candidate()])
        config = git(self.repo, "config", "--get", "remote.sandbox-mw-cite.tagOpt").stdout
        self.assertEqual(config.strip(), "--no-tags")

    def test_non_repo_is_skipped(self):
        plain = os.path.join(self.tmp.name, "not-a-repo")
        os.makedirs(plain)
        remotes, skipped = self.sync([self.candidate(host_dir=plain)])
        self.assertEqual(remotes, [])
        self.assertEqual(skipped[0]["reason"], "not-a-git-repo")

    def test_missing_directory_is_skipped(self):
        remotes, skipped = self.sync([self.candidate(host_dir="/nonexistent/x")])
        self.assertEqual(remotes, [])
        self.assertEqual(skipped[0]["reason"], "not-a-git-repo")

    def test_foreign_remote_with_the_same_name_is_left_alone(self):
        git(self.repo, "remote", "add", "sandbox-mw-cite", "git://mine/x")
        remotes, skipped = self.sync([self.candidate()])
        self.assertEqual(remotes, [])
        self.assertEqual(skipped[0]["reason"], "name-taken")
        self.assertEqual(
            git(self.repo, "remote", "get-url", "sandbox-mw-cite").stdout.strip(),
            "git://mine/x",
        )
        self.assertTrue(any("isn't" in w for w in self.warnings))

    def test_another_sandboxs_remote_is_left_alone(self):
        git(self.repo, "remote", "add", "sandbox-mw-cite", "git://other/x")
        m.set_marker(self.repo, "sandbox-mw-cite", "someone-else")
        remotes, skipped = self.sync([self.candidate()])
        self.assertEqual(remotes, [])
        self.assertEqual(skipped[0]["reason"], "name-taken")

    def test_our_own_stale_remote_is_repointed(self):
        # This is the wmf-sbx-resume path: `sbx stop` drops the published
        # port, so the URL legitimately changes while the remote stays
        # ours. Matching on URL instead of the marker would strand it.
        self.sync([self.candidate(url="git://127.0.0.1:40001/Wikimedia/Cite")])
        remotes, skipped = self.sync([self.candidate(url="git://127.0.0.1:40002/Wikimedia/Cite")])
        self.assertEqual(skipped, [])
        self.assertTrue(remotes[0]["adopted"])
        self.assertEqual(
            git(self.repo, "remote", "get-url", "sandbox-mw-cite").stdout.strip(),
            "git://127.0.0.1:40002/Wikimedia/Cite",
        )

    def test_records_only_what_it_touched(self):
        other = os.path.join(self.tmp.name, "Other")
        os.makedirs(other)
        remotes, skipped = self.sync([
            self.candidate(),
            self.candidate(host_dir=other),
        ])
        self.assertEqual([r["hostDir"] for r in remotes], [self.repo])
        self.assertEqual([s["hostDir"] for s in skipped], [other])

    def test_add_is_backed_out_if_the_marker_cannot_be_set(self):
        # Without a marker the remote could never be safely removed
        # again, so a leaked *unowned* remote is the worst outcome.
        with mock.patch.object(m, "set_marker", return_value=False):
            remotes, skipped = self.sync([self.candidate()])
        self.assertEqual(remotes, [])
        self.assertEqual(skipped[0]["reason"], "marker-failed")
        self.assertEqual(remotes_of(self.repo), [])


@unittest.skipUnless(HAVE_GIT, "git not installed")
class GcSuspensionTests(unittest.TestCase):
    """Turning gc off in a host repo for as long as a sandbox clone is
    reading its objects through a `--shared` alternate, and putting the
    engineer's own settings back afterwards."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(os.path.join(self.tmp.name, "Cite"))
        self.warnings = []

    def local(self, key):
        result = subprocess.run(
            ["git", "-C", self.repo, "config", "--local", "--get", key],
            capture_output=True, text=True,
        )
        return None if result.returncode != 0 else result.stdout.rstrip("\n")

    def add_marked_remote(self, name="sandbox-mw-cite", sandbox="mw-cite"):
        git(self.repo, "remote", "add", name, "git://x/y")
        m.set_marker(self.repo, name, sandbox)

    def test_unset_of_a_missing_key_exits_5(self):
        # The contract _config_unset treats as an expected outcome.
        result = subprocess.run(
            ["git", "-C", self.repo, "config", "--local", "--unset", "gc.auto"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, m.EXIT_NOTHING_TO_UNSET)

    def test_suspend_turns_gc_off_and_records_that_it_did(self):
        self.assertTrue(m.suspend_gc(self.repo, warn=self.warnings.append))
        self.assertEqual(self.local("gc.auto"), "0")
        self.assertEqual(self.local("gc.pruneExpire"), "never")
        self.assertEqual(self.local("wmfSbx.gcSuspended"), "true")
        self.assertEqual(self.warnings, [])

    def test_restore_unsets_keys_that_were_unset_before(self):
        m.suspend_gc(self.repo)
        self.assertTrue(m.resume_gc(self.repo, warn=self.warnings.append))
        self.assertIsNone(self.local("gc.auto"))
        self.assertIsNone(self.local("gc.pruneExpire"))
        self.assertIsNone(self.local("wmfSbx.gcSuspended"))
        self.assertIsNone(self.local("wmfSbx.savedGcAuto"))

    def test_restore_puts_the_engineers_own_values_back(self):
        git(self.repo, "config", "--local", "gc.auto", "1500")
        m.suspend_gc(self.repo)
        self.assertEqual(self.local("gc.auto"), "0")
        m.resume_gc(self.repo)
        self.assertEqual(self.local("gc.auto"), "1500")

    def test_a_second_sandbox_does_not_stash_the_suspended_value(self):
        # Otherwise the first sandbox's gc.auto=0 becomes what gets
        # "restored" when the last remote goes away.
        git(self.repo, "config", "--local", "gc.auto", "1500")
        m.suspend_gc(self.repo)
        self.assertTrue(m.suspend_gc(self.repo))
        self.assertEqual(self.local("wmfSbx.savedGcAuto"), "1500")

    def test_resume_waits_for_the_last_sandbox_remote(self):
        self.add_marked_remote()
        m.suspend_gc(self.repo)
        self.assertFalse(m.resume_gc(self.repo))
        self.assertEqual(self.local("gc.auto"), "0")
        git(self.repo, "remote", "remove", "sandbox-mw-cite")
        self.assertTrue(m.resume_gc(self.repo))

    def test_resume_is_a_no_op_when_we_never_suspended(self):
        git(self.repo, "config", "--local", "gc.auto", "1500")
        self.assertFalse(m.resume_gc(self.repo))
        self.assertEqual(self.local("gc.auto"), "1500")

    def test_marked_remotes_lists_every_sandbox(self):
        self.add_marked_remote("sandbox-a", "a")
        self.add_marked_remote("sandbox-b", "b")
        git(self.repo, "remote", "add", "origin", "git://mine/x")
        self.assertEqual(sorted(m.marked_remotes(self.repo)),
                         ["sandbox-a", "sandbox-b"])

    def test_a_half_finished_suspend_leaves_the_repos_settings_alone(self):
        git(self.repo, "config", "--local", "gc.auto", "1500")
        real_set = m._config_set

        def fail_on_prune_expire(host_dir, key, value, run=subprocess.run):
            if key == "gc.pruneExpire":
                return False
            return real_set(host_dir, key, value, run=run)

        with mock.patch.object(m, "_config_set", fail_on_prune_expire):
            self.assertFalse(m.suspend_gc(self.repo, warn=self.warnings.append))
        # Backed out completely: gc.auto is the engineer's again, and no
        # bookkeeping key is left to confuse a later restore.
        self.assertEqual(self.local("gc.auto"), "1500")
        self.assertIsNone(self.local("wmfSbx.gcSuspended"))
        self.assertIsNone(self.local("wmfSbx.savedGcAuto"))
        self.assertTrue(self.warnings)

    def test_sync_and_remove_bracket_the_suspension(self):
        candidate = {
            "hostDir": self.repo,
            "remote": "sandbox-mw-cite",
            "url": "git://127.0.0.1:49281/Wikimedia/Cite",
            "sandboxPath": "/home/agent/Wikimedia/Cite",
            "readOnly": False,
        }
        git(self.repo, "config", "--local", "gc.auto", "1500")
        m.sync_remotes("mw-cite", [candidate], warn=self.warnings.append)
        self.assertEqual(self.local("gc.auto"), "0")

        state = state_mod.new_state("mw-cite")
        state["remotes"] = [candidate]
        m.remove_remotes(state, warn=self.warnings.append)
        self.assertEqual(self.local("gc.auto"), "1500")
        self.assertIsNone(self.local("wmfSbx.gcSuspended"))

    def test_a_dry_run_removal_leaves_gc_suspended(self):
        candidate = {
            "hostDir": self.repo,
            "remote": "sandbox-mw-cite",
            "url": "git://x/y",
            "sandboxPath": "/home/agent/Wikimedia/Cite",
            "readOnly": False,
        }
        m.sync_remotes("mw-cite", [candidate])
        state = state_mod.new_state("mw-cite")
        state["remotes"] = [candidate]
        m.remove_remotes(state, dry_run=True)
        self.assertEqual(self.local("gc.auto"), "0")
        self.assertEqual(self.local("wmfSbx.gcSuspended"), "true")


@unittest.skipUnless(HAVE_GIT, "git not installed")
class RemoveRemotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(os.path.join(self.tmp.name, "Cite"))
        self.warnings = []
        self.state = state_mod.new_state("mw-cite")
        self.state["remotes"] = [{
            "hostDir": self.repo,
            "remote": "sandbox-mw-cite",
            "url": "git://127.0.0.1:49281/Wikimedia/Cite",
            "sandboxPath": "/home/agent/Wikimedia/Cite",
        }]

    def remove(self, **kw):
        return m.remove_remotes(self.state, warn=self.warnings.append, **kw)

    def add_ours(self):
        git(self.repo, "remote", "add", "sandbox-mw-cite", "git://x/y")
        m.set_marker(self.repo, "sandbox-mw-cite", "mw-cite")

    def test_removes_our_remote(self):
        self.add_ours()
        self.assertEqual(self.remove(), [(self.repo, "sandbox-mw-cite", "removed")])
        self.assertEqual(remotes_of(self.repo), [])

    def test_dry_run_changes_nothing(self):
        self.add_ours()
        self.assertEqual(self.remove(dry_run=True), [(self.repo, "sandbox-mw-cite", "removed")])
        self.assertIn("sandbox-mw-cite", remotes_of(self.repo))

    def test_already_removed_is_success(self):
        # Idempotent by design: --prune, wmf-sbx-rm, and wmf-sbx-create's
        # opportunistic sweep can all race to the same remote.
        self.assertEqual(self.remove(), [(self.repo, "sandbox-mw-cite", "absent")])

    def test_missing_host_repo_is_not_an_error(self):
        shutil.rmtree(self.repo)
        self.assertEqual(self.remove(), [(self.repo, "sandbox-mw-cite", "gone")])

    def test_unmarked_replacement_is_left_alone(self):
        git(self.repo, "remote", "add", "sandbox-mw-cite", "git://mine/x")
        self.assertEqual(self.remove(), [(self.repo, "sandbox-mw-cite", "not-ours")])
        self.assertIn("sandbox-mw-cite", remotes_of(self.repo))
        self.assertTrue(self.warnings)

    def test_reassigned_remote_is_left_alone(self):
        git(self.repo, "remote", "add", "sandbox-mw-cite", "git://other/x")
        m.set_marker(self.repo, "sandbox-mw-cite", "mw-cite-2")
        self.assertEqual(self.remove(), [(self.repo, "sandbox-mw-cite", "not-ours")])
        self.assertIn("sandbox-mw-cite", remotes_of(self.repo))

    def test_round_trip_with_sync_remotes(self):
        candidates = [{
            "hostDir": self.repo, "remote": "sandbox-mw-cite",
            "url": "git://127.0.0.1:49281/Wikimedia/Cite",
            "sandboxPath": "/home/agent/Wikimedia/Cite", "readOnly": False,
        }]
        remotes, _skipped = m.sync_remotes("mw-cite", candidates)
        self.state["remotes"] = remotes
        self.remove()
        self.assertEqual(remotes_of(self.repo), [])


@unittest.skipUnless(HAVE_GIT, "git not installed")
class UnfetchedTipsTests(unittest.TestCase):
    """The guard that stands between `sbx rm` and unrecoverable work.
    `url` is a local path here; git treats that as a perfectly good
    transport for ls-remote, so this exercises the real ref walk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = make_repo(os.path.join(self.tmp.name, "host"))
        self.sandbox = os.path.join(self.tmp.name, "sandbox")
        subprocess.run(["git", "clone", "-q", self.host, self.sandbox],
                       check=True, capture_output=True, text=True)
        git(self.sandbox, "config", "user.email", "test@example.invalid")
        git(self.sandbox, "config", "user.name", "Test")
        self.state = state_mod.new_state("mw-cite")
        self.state["remotes"] = [{
            "hostDir": self.host, "remote": "sandbox-mw-cite", "url": self.sandbox,
        }]

    def commit_in_sandbox(self, message="agent work"):
        with open(os.path.join(self.sandbox, "NEW"), "a", encoding="utf-8") as f:
            f.write(message + "\n")
        git(self.sandbox, "add", "NEW")
        git(self.sandbox, "commit", "-qm", message)
        return git(self.sandbox, "rev-parse", "HEAD").stdout.strip()

    def test_nothing_new_is_clean(self):
        self.assertEqual(m.unfetched_tips(self.state), ([], []))

    def test_new_commit_is_reported(self):
        sha = self.commit_in_sandbox()
        findings, unreachable = m.unfetched_tips(self.state)
        self.assertEqual(unreachable, [])
        self.assertEqual(findings, [(self.host, "sandbox-mw-cite", [sha])])

    def test_fetched_commit_is_not_reported(self):
        self.commit_in_sandbox()
        git(self.host, "fetch", "-q", self.sandbox, "main")
        self.assertEqual(m.unfetched_tips(self.state), ([], []))

    def test_new_branch_is_reported(self):
        git(self.sandbox, "checkout", "-qb", "wip")
        sha = self.commit_in_sandbox()
        findings, _unreachable = m.unfetched_tips(self.state)
        self.assertEqual(findings[0][2], [sha])

    def test_tags_are_ignored(self):
        # Tags aren't where lost work lives, and `--no-tags` means we
        # never fetch them anyway -- reporting them would make the guard
        # cry wolf on every sandbox that tagged anything.
        git(self.sandbox, "tag", "-a", "v1", "-m", "tagged")
        self.assertEqual(m.unfetched_tips(self.state), ([], []))

    def upstream_commit_the_host_lacks(self):
        """A commit that reached the sandbox from Gerrit, not from the
        agent: the sandbox fetched upstream more recently than the host
        did. `git daemon` advertises refs/remotes/*, so these show up in
        the ls-remote output like anything else."""
        upstream = make_repo(os.path.join(self.tmp.name, "gerrit"))
        with open(os.path.join(upstream, "UPSTREAM"), "w", encoding="utf-8") as f:
            f.write("someone else's commit\n")
        git(upstream, "add", "UPSTREAM")
        git(upstream, "commit", "-qm", "upstream work")
        git(self.sandbox, "remote", "add", "gerrit", upstream)
        git(self.sandbox, "fetch", "-q", "gerrit")
        return git(self.sandbox, "rev-parse", "gerrit/main").stdout.strip()

    def test_upstream_commits_are_not_the_agents_work(self):
        # §36.2: 14 "unfetched tips" across two repos, every one of them a
        # Gerrit commit the sandbox had fetched and the host hadn't. Not
        # work that dies with the sandbox -- it's sitting on the origin it
        # came from -- and crying wolf here makes --force routine.
        self.upstream_commit_the_host_lacks()
        self.assertEqual(m.unfetched_tips(self.state), ([], []))

    def test_a_branch_sitting_on_an_upstream_tip_is_not_reported(self):
        # What every clone looks like until the agent commits: the local
        # branch and the remote-tracking ref name the same commit.
        sha = self.upstream_commit_the_host_lacks()
        git(self.sandbox, "branch", "tracks-upstream", sha)
        self.assertEqual(m.unfetched_tips(self.state), ([], []))

    def test_agent_commits_on_top_of_upstream_are_still_reported(self):
        # The filter must not swallow the case the guard exists for.
        self.upstream_commit_the_host_lacks()
        git(self.sandbox, "checkout", "-q", "-b", "wip", "gerrit/main")
        sha = self.commit_in_sandbox()
        findings, unreachable = m.unfetched_tips(self.state)
        self.assertEqual(unreachable, [])
        self.assertEqual(findings, [(self.host, "sandbox-mw-cite", [sha])])

    def test_unreachable_daemon_is_reported_separately(self):
        # "Can't check" must never be read as "nothing to lose" -- the
        # caller treats this as blocking too.
        self.state["remotes"][0]["url"] = os.path.join(self.tmp.name, "nonexistent")
        findings, unreachable = m.unfetched_tips(self.state)
        self.assertEqual(findings, [])
        self.assertEqual(len(unreachable), 1)
        self.assertEqual(unreachable[0][0], self.host)

    def test_missing_host_repo_is_skipped(self):
        shutil.rmtree(self.host)
        self.assertEqual(m.unfetched_tips(self.state), ([], []))


@unittest.skipUnless(HAVE_GIT, "git not installed")
class PruneDeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        self.repo = make_repo(os.path.join(self.tmp.name, "Cite"))

    def record(self, name):
        git(self.repo, "remote", "add", f"sandbox-{name}", "git://x/y")
        m.set_marker(self.repo, f"sandbox-{name}", name)
        state = state_mod.new_state(name)
        state["remotes"] = [{
            "hostDir": self.repo, "remote": f"sandbox-{name}", "url": "git://x/y",
        }]
        state_mod.save(state, env=self.env)

    def test_prunes_only_dead_sandboxes(self):
        self.record("mw-cite")
        self.record("mw-core")
        pruned = m.prune_dead({"mw-core"}, env=self.env)
        self.assertEqual([name for name, _r in pruned], ["mw-cite"])
        self.assertEqual(remotes_of(self.repo), ["sandbox-mw-core"])
        self.assertEqual(state_mod.list_names(env=self.env), ["mw-core"])

    def test_dry_run_changes_nothing(self):
        self.record("mw-cite")
        pruned = m.prune_dead(set(), env=self.env, dry_run=True)
        self.assertEqual([name for name, _r in pruned], ["mw-cite"])
        self.assertEqual(remotes_of(self.repo), ["sandbox-mw-cite"])
        self.assertEqual(state_mod.list_names(env=self.env), ["mw-cite"])

    def test_corrupt_state_is_warned_not_raised(self):
        os.makedirs(state_mod.state_dir(self.env), exist_ok=True)
        with open(state_mod.state_path("mw-bad", self.env), "w", encoding="utf-8") as f:
            f.write("{oops")
        warnings = []
        self.assertEqual(m.prune_dead(set(), env=self.env, warn=warnings.append), [])
        self.assertTrue(warnings)

    def test_nothing_to_prune(self):
        self.assertEqual(m.prune_dead({"mw-cite"}, env=self.env), [])


if __name__ == "__main__":
    unittest.main()
