#!/usr/bin/env python3
"""Unit tests for wmf_sbx_rm.py -- run with:
  python3 -m unittest discover -s sbx/tests -v

`sbx rm` is unrecoverable and its exit status is uninformative in both
directions (a nonexistent sandbox exits 1; *declining the prompt exits
0*), so the tests that matter here are the negative ones: that declining
leaves every host remote untouched, and that the unfetched-work guard
blocks before anything is destroyed.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.remotes as remotes_mod  # noqa: E402
import wmf_sbx.rm as rm  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", cwd] + list(args), capture_output=True, text=True, check=True
    )


def make_repo(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", path], check=True,
                   capture_output=True, text=True)
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    with open(os.path.join(path, "README"), "w", encoding="utf-8") as f:
        f.write("hello\n")
    git(path, "add", "README")
    git(path, "commit", "-qm", "initial")
    return path


@unittest.skipUnless(HAVE_GIT, "git not installed")
class RemoveOneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        self.host = make_repo(os.path.join(self.tmp.name, "host"))
        self.sandbox = os.path.join(self.tmp.name, "sandbox")
        subprocess.run(["git", "clone", "-q", self.host, self.sandbox],
                       check=True, capture_output=True, text=True)
        git(self.sandbox, "config", "user.email", "test@example.invalid")
        git(self.sandbox, "config", "user.name", "Test")
        # A real remote, marked as ours, pointed at a real "sandbox" repo.
        git(self.host, "remote", "add", "sandbox-mw-cite", self.sandbox)
        remotes_mod.set_marker(self.host, "sandbox-mw-cite", "mw-cite")
        state = state_mod.new_state("mw-cite", daemon_port=9418, host_port=49281)
        state["remotes"] = [{
            "hostDir": self.host, "remote": "sandbox-mw-cite", "url": self.sandbox,
        }]
        state_mod.save(state, env=self.env)

    def commit_in_sandbox(self):
        with open(os.path.join(self.sandbox, "NEW"), "w", encoding="utf-8") as f:
            f.write("agent work\n")
        git(self.sandbox, "add", "NEW")
        git(self.sandbox, "commit", "-qm", "agent work")

    def fake_run(self, removes=True, calls=None, starts=True):
        """Stands in for `wmf-sbx`: `ls --json` reports the sandbox as
        gone (or still there, if removes=False, which is what declining
        the prompt looks like from out here)."""
        # The sandbox starts out *stopped*, which is the state `wmf-sbx-rm`
        # meets most often -- someone stops a sandbox and then decides to
        # remove it. A stopped sandbox publishes nothing and refuses to
        # publish; `exec` is what brings it up.
        sandbox = {"running": False}

        def run(cmd, **kwargs):
            if calls is not None:
                calls.append(cmd)
            if "ls" in cmd:
                listed = [] if removes else [{"name": "mw-cite",
                                              "status": "stopped"}]
                return FakeCompletedProcess(
                    0, stdout=json.dumps({"sandboxes": listed}))
            if "rm" in cmd:
                return FakeCompletedProcess(0)
            if "exec" in cmd:
                # The guard starts a stopped sandbox before giving up on
                # its daemon -- `exec ... true` is what does that.
                if not starts:
                    return FakeCompletedProcess(1)
                sandbox["running"] = True
                return FakeCompletedProcess(0)
            if "ports" in cmd:
                # The unfetched-work guard re-points the remotes first, so
                # it always asks where the daemon is published now. These
                # remotes are local paths, so there is nothing to re-point.
                if not sandbox["running"]:
                    if "--publish" in cmd:
                        return FakeCompletedProcess(1, stderr=(
                            "ERROR: publish ports: request failed: 500 "
                            "Internal Server Error: request[0]: failed to "
                            "resolve endpoint: no container endpoint with IP "
                            "address found"
                        ))
                    return FakeCompletedProcess(0, stdout="[]")
                if "--publish" in cmd:
                    return FakeCompletedProcess(0)
                return FakeCompletedProcess(0, stdout=json.dumps([
                    {"host_ip": "127.0.0.1", "host_port": 32794,
                     "sandbox_port": 9418, "protocol": "tcp"},
                ]))
            return subprocess.run(cmd, **kwargs)
        return run

    def _main(self, argv, run):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rm.main(argv, run=run, env=self.env)
        return code, err.getvalue()

    def remotes_of_host(self):
        return git(self.host, "remote").stdout.split()

    def test_removes_sandbox_remote_and_state(self):
        code, _err = self._main(["mw-cite"], self.fake_run())
        self.assertEqual(code, 0)
        self.assertEqual(self.remotes_of_host(), [])
        self.assertIsNone(state_mod.load("mw-cite", env=self.env))

    def test_declining_the_prompt_leaves_everything_alone(self):
        # `sbx rm` exits 0 when the user answers N, so the only honest
        # signal is that the sandbox is still listed afterward. Trusting
        # the exit code here would strip the remotes off a sandbox the
        # engineer had just decided to keep.
        code, err = self._main(["mw-cite"], self.fake_run(removes=False))
        self.assertEqual(code, 1)
        self.assertIn("still present", err)
        self.assertEqual(self.remotes_of_host(), ["sandbox-mw-cite"])
        self.assertIsNotNone(state_mod.load("mw-cite", env=self.env))

    def test_unfetched_commits_block_removal(self):
        calls = []
        self.commit_in_sandbox()
        code, err = self._main(["mw-cite"], self.fake_run(calls=calls))
        self.assertEqual(code, 1)
        self.assertIn("never fetched", err)
        # Nothing was destroyed: no `rm` reached wmf-sbx at all.
        self.assertFalse(any("rm" in cmd for cmd in calls))
        self.assertEqual(self.remotes_of_host(), ["sandbox-mw-cite"])

    def test_the_guard_re_points_the_remotes_before_probing_them(self):
        # The daemon's published host port moves on every container start
        # (sbx/NOTES.md §34), so probing the URL recorded at create time
        # would report every remote as unreachable and push the engineer
        # to --force -- destroying exactly the work this guard protects.
        calls = []
        self.commit_in_sandbox()
        code, _err = self._main(["mw-cite"], self.fake_run(calls=calls))
        self.assertEqual(code, 1)
        ports = [i for i, cmd in enumerate(calls) if "ports" in cmd]
        self.assertTrue(ports, "expected a `wmf-sbx ports` lookup")
        # Before the probe, which is what makes it worth doing at all.
        probes = [i for i, cmd in enumerate(calls) if "ls-remote" in cmd]
        self.assertTrue(probes, "expected the daemon to be probed")
        self.assertLess(ports[0], probes[0])

    def test_a_stopped_sandbox_is_started_rather_than_declared_unreachable(self):
        # Removing a sandbox you stopped weeks ago is the normal case, and
        # a stopped sandbox publishes no port at all -- `ports --publish`
        # against one fails with `500 ... no container endpoint with IP
        # address found`. Refusing there would make --force the routine
        # answer, which is the opposite of what this guard is for.
        calls = []
        self.commit_in_sandbox()
        self._main(["mw-cite"], self.fake_run(calls=calls))
        self.assertTrue(
            any("exec" in cmd and "true" in cmd for cmd in calls),
            f"expected the guard to start the sandbox; got {calls}",
        )
        # And then waited for the daemon before probing: the container is
        # up before the daemon is, and a probe in between gets
        # `Connection reset by peer` from Docker's port proxy (§36.1).
        probes = [i for i, cmd in enumerate(calls) if "connect_ex" in " ".join(cmd)]
        self.assertTrue(probes, f"expected a daemon readiness probe; got {calls}")
        ls_remotes = [i for i, cmd in enumerate(calls) if "ls-remote" in cmd]
        self.assertTrue(ls_remotes)
        self.assertLess(probes[0], ls_remotes[0])

    def test_a_daemon_still_starting_after_an_idle_restart_is_waited_for(self):
        # sbx's own idle-auto-stop (sbx/NOTES.md §81.2) restarts the
        # container on demand -- the port mapping can be back before the
        # git daemon inside is. The guard used to only start-and-wait
        # when the port lookup came back empty, which this scenario
        # never triggers: the mapping is already there, just not yet
        # backed by anything listening. Refusing here would make
        # --force the routine answer for the *normal* idle-restart case,
        # not just the fully-stopped one covered above.
        self.commit_in_sandbox()
        calls = []
        probe_attempts = {"n": 0}

        def run(cmd, **kwargs):
            calls.append(cmd)
            if "ls" in cmd:
                return FakeCompletedProcess(0, stdout=json.dumps({"sandboxes": []}))
            if "rm" in cmd:
                return FakeCompletedProcess(0)
            if "connect_ex" in " ".join(cmd):
                probe_attempts["n"] += 1
                return FakeCompletedProcess(0 if probe_attempts["n"] >= 2 else 1)
            if "exec" in cmd:
                return FakeCompletedProcess(0)
            if "ports" in cmd:
                # The container -- and its port mapping -- is already
                # up; only the daemon inside is still coming up.
                return FakeCompletedProcess(0, stdout=json.dumps([
                    {"host_ip": "127.0.0.1", "host_port": 32794,
                     "sandbox_port": 9418, "protocol": "tcp"},
                ]))
            return subprocess.run(cmd, **kwargs)

        code, err = self._main(["mw-cite"], run)
        # Blocked for the real reason (unfetched commits), not because
        # the daemon was declared unreachable before it had a chance to
        # come up.
        self.assertEqual(code, 1)
        self.assertIn("never fetched", err)
        self.assertNotIn("could not reach", err)
        self.assertGreaterEqual(probe_attempts["n"], 2)

    def test_the_expected_stopped_sandbox_failure_is_not_announced(self):
        # `ports --publish` against a stopped sandbox always fails with
        # `500 ... no container endpoint`. The guard starts the sandbox and
        # retries, so printing that first makes a command that worked read
        # as one that broke (§37.1).
        _code, err = self._main(["mw-cite"], self.fake_run())
        self.assertNotIn("--publish", err)

    def test_force_skips_the_guard(self):
        self.commit_in_sandbox()
        calls = []
        code, _err = self._main(["--force", "mw-cite"], self.fake_run(calls=calls))
        self.assertEqual(code, 0)
        self.assertEqual(self.remotes_of_host(), [])
        self.assertTrue(any("--force" in cmd for cmd in calls))

    def test_short_f_is_an_alias_for_force(self):
        # sbx rm's own -f/--force -- wmf-sbx-rm should take the same
        # short flag, not just the long spelling.
        self.commit_in_sandbox()
        calls = []
        code, _err = self._main(["-f", "mw-cite"], self.fake_run(calls=calls))
        self.assertEqual(code, 0)
        self.assertEqual(self.remotes_of_host(), [])
        # Still forwarded to the real sbx rm as the long spelling (rm.py's
        # own comment on why: --force is verified against a real sbx, -f
        # is not).
        self.assertTrue(any("--force" in cmd for cmd in calls))

    def test_unreachable_daemon_blocks_removal(self):
        # "Can't check" is not "nothing to lose": a stopped sandbox's
        # daemon is unreachable, and stopping is exactly what someone does
        # right before deciding to remove it.
        shutil.rmtree(self.sandbox)
        code, err = self._main(["mw-cite"], self.fake_run())
        self.assertEqual(code, 1)
        self.assertIn("could not reach", err)
        self.assertEqual(self.remotes_of_host(), ["sandbox-mw-cite"])

    def test_keep_remotes_skips_the_guard_and_the_cleanup(self):
        self.commit_in_sandbox()
        code, _err = self._main(["--keep-remotes", "mw-cite"], self.fake_run())
        self.assertEqual(code, 0)
        self.assertEqual(self.remotes_of_host(), ["sandbox-mw-cite"])
        self.assertIsNotNone(state_mod.load("mw-cite", env=self.env))

    def test_dry_run_changes_nothing(self):
        calls = []
        code, err = self._main(["--dry-run", "mw-cite"], self.fake_run(calls=calls))
        self.assertEqual(code, 0)
        self.assertIn("would remove", err)
        self.assertFalse(any("rm" in cmd for cmd in calls))
        self.assertEqual(self.remotes_of_host(), ["sandbox-mw-cite"])
        self.assertIsNotNone(state_mod.load("mw-cite", env=self.env))

    def test_unknown_sandbox_still_gets_removed(self):
        # No state file just means we have nothing to clean up -- it must
        # not stop the engineer from removing the sandbox itself.
        code, err = self._main(["mw-other"], self.fake_run())
        self.assertEqual(code, 0)
        self.assertIn("no wmf-sbx state", err)

    def test_unusable_name_is_rejected(self):
        code, err = self._main(["../evil"], self.fake_run())
        self.assertEqual(code, 1)
        self.assertIn("not a usable sandbox name", err)

    def test_several_names_and_a_failure_reports_nonzero(self):
        def run(cmd, **kwargs):
            if "ls" in cmd:
                # 'mw-keep' survives its prompt; 'mw-cite' doesn't exist.
                return FakeCompletedProcess(
                    0, stdout=json.dumps({"sandboxes": [{"name": "mw-keep"}]}))
            if "rm" in cmd:
                return FakeCompletedProcess(0)
            return subprocess.run(cmd, **kwargs)

        code, _err = self._main(["--force", "mw-keep", "mw-other"], run)
        self.assertEqual(code, 1)


@unittest.skipUnless(HAVE_GIT, "git not installed")
class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        self.host = make_repo(os.path.join(self.tmp.name, "host"))
        git(self.host, "remote", "add", "sandbox-mw-cite", "git://x/y")
        remotes_mod.set_marker(self.host, "sandbox-mw-cite", "mw-cite")
        state = state_mod.new_state("mw-cite")
        state["remotes"] = [{
            "hostDir": self.host, "remote": "sandbox-mw-cite", "url": "git://x/y",
        }]
        state_mod.save(state, env=self.env)

    def run_with(self, *listed):
        """`wmf-sbx ls --json` naming exactly `listed` (see §75.4)."""
        payload = json.dumps({"sandboxes": [{"name": n} for n in listed]})

        def run(cmd, **kwargs):
            if cmd[0] == "git":
                return subprocess.run(cmd, **kwargs)
            if "ls" in cmd:
                return FakeCompletedProcess(0, stdout=payload)
            # --prune never destroys a sandbox, so it can be run
            # opportunistically (wmf-sbx-create does) without ever
            # springing an irreversible prompt on the engineer.
            raise AssertionError(f"--prune must not run {cmd!r}")
        return run

    def _main(self, argv, run):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rm.main(argv, run=run, env=self.env)
        return code, err.getvalue()

    def test_prunes_remotes_of_a_vanished_sandbox(self):
        # `sbx rm` behind our back, or `sbx logout`. Never invokes `sbx rm`
        # itself -- the fake above asserts that.
        code, err = self._main(["--prune"], self.run_with())
        self.assertEqual(code, 0)
        self.assertIn("removed sandbox-mw-cite", err)
        self.assertEqual(git(self.host, "remote").stdout.split(), [])
        self.assertEqual(state_mod.list_names(env=self.env), [])

    def test_live_sandbox_is_left_alone(self):
        code, err = self._main(["--prune"], self.run_with("mw-cite"))
        self.assertEqual(code, 0)
        self.assertIn("nothing to prune", err)
        self.assertEqual(git(self.host, "remote").stdout.split(), ["sandbox-mw-cite"])

    def test_prune_dry_run_changes_nothing(self):
        self._main(["--prune", "--dry-run"], self.run_with())
        self.assertEqual(git(self.host, "remote").stdout.split(), ["sandbox-mw-cite"])
        self.assertEqual(state_mod.list_names(env=self.env), ["mw-cite"])


class ArgumentTests(unittest.TestCase):
    def test_no_name_and_no_prune_is_an_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            rm.main([])
        self.assertIn("--prune", err.getvalue())


if __name__ == "__main__":
    unittest.main()
