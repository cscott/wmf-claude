#!/usr/bin/env python3
"""Unit tests for wmf_sbx_setup.py -- run with:
  python3 -m unittest discover -s sbx/tests -v
"""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.setup as s  # noqa: E402

# The real SHARED_SKILLS_DIR is /home/agent/.claude/skills, which on the
# machine these tests most often run on -- a sandbox -- is an actual
# mount point. Leaving it pointed there would make every test that goes
# through run_setup or run_restore try to remount it, and would make the
# suite's behaviour depend on whether the machine running it happens to
# share a skills store. Point it somewhere that is never a mount; the
# tests that care about the locking set it explicitly.
_UNMOUNTED_SKILLS = os.path.join(tempfile.gettempdir(), "wmf-sbx-no-such-skills")


def setUpModule():
    patch = unittest.mock.patch.object(s, "SHARED_SKILLS_DIR", _UNMOUNTED_SKILLS)
    patch.start()
    unittest.addModuleCleanup(patch.stop)


class FakeCompletedProcess:
    def __init__(self, returncode):
        self.returncode = returncode


class ParallelPathTests(unittest.TestCase):
    def test_nested_dir_maps_under_sandbox_home(self):
        self.assertEqual(
            s.parallel_path("/home/cananian", "/home/agent", "/home/cananian/Wikimedia/Cite"),
            "/home/agent/Wikimedia/Cite",
        )

    def test_host_home_itself_maps_to_sandbox_home(self):
        self.assertEqual(s.parallel_path("/home/cananian", "/home/agent", "/home/cananian"), "/home/agent")

    def test_dir_outside_host_home_returns_none(self):
        self.assertIsNone(s.parallel_path("/home/cananian", "/home/agent", "/srv/shared/Skins"))

    def test_sibling_dir_with_shared_prefix_returns_none(self):
        # "/home/cananian2" is not inside "/home/cananian" -- must not match
        # on a bare string prefix without the separator.
        self.assertIsNone(s.parallel_path("/home/cananian", "/home/agent", "/home/cananian2/Skins"))


class WorkPathTests(unittest.TestCase):
    """Which of a layout entry's two paths everything after setup_repo
    should use. See sbx/NOTES.md §63.1."""

    LITERAL = "/home/cananian/Wikimedia/Cite"
    DEST = "/home/agent/Wikimedia/Cite"

    def entry(self, mode):
        return {"literal": self.LITERAL, "dest": self.DEST,
                "orig": None, "mode": mode}

    def test_alias_mode_works_in_the_literal_path(self):
        # The clone is mounted over the literal path, so the literal path
        # *is* the clone -- and it is the one the agent and the engineer
        # both name.
        self.assertEqual(s.work_path(self.entry("alias")), self.LITERAL)

    def test_clone_mode_works_in_the_parallel_path(self):
        # The fallback: the alias didn't take, so the literal path is
        # still the read-only host mirror and `dest` is the only writable
        # copy. Getting this backwards would composer-update a read-only
        # tree.
        self.assertEqual(s.work_path(self.entry("clone")), self.DEST)

    def test_readonly_and_out_of_tree_modes_keep_the_literal_path(self):
        # Neither has a clone at all, so there is no second path that
        # means anything.
        self.assertEqual(s.work_path(self.entry("bind")), self.LITERAL)
        self.assertEqual(s.work_path(self.entry("inplace")), self.LITERAL)


class ShallowestMissingAncestorTests(unittest.TestCase):
    def test_existing_path_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(s.shallowest_missing_ancestor(tmp))

    def test_missing_leaf_with_existing_parent_returns_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            leaf = os.path.join(tmp, "missing")
            self.assertEqual(s.shallowest_missing_ancestor(leaf), leaf)

    def test_missing_chain_returns_topmost_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            topmost = os.path.join(tmp, "a")
            nested = os.path.join(topmost, "b", "c")
            self.assertEqual(s.shallowest_missing_ancestor(nested), topmost)


class CloneIntoParallelTreeTests(unittest.TestCase):
    def test_creates_parent_and_clones_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "Cite")
            dest = os.path.join(tmp, "sandbox-home", "Wikimedia", "Cite")
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return FakeCompletedProcess(0)

            s.clone_into_parallel_tree(literal, dest, run=fake_run)
            self.assertTrue(os.path.isdir(os.path.dirname(dest)))
            self.assertEqual(
                calls,
                [
                    ["git", "clone", "--shared", literal, dest],
                    ["sudo", "chown", "-R", "agent:agent", os.path.join(tmp, "sandbox-home")],
                ],
            )

    def test_skips_clone_if_dest_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "Cite")
            dest = os.path.join(tmp, "existing")
            os.makedirs(dest)

            def fake_run(argv):
                self.fail("should not run git clone when dest already exists")

            s.clone_into_parallel_tree(literal, dest, run=fake_run)  # no exception

    def test_chowns_dest_only_when_parent_already_existed(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "Cite")
            dest = os.path.join(tmp, "Cite-clone")
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return FakeCompletedProcess(0)

            s.clone_into_parallel_tree(literal, dest, run=fake_run)
            self.assertEqual(
                calls,
                [
                    ["git", "clone", "--shared", literal, dest],
                    ["sudo", "chown", "-R", "agent:agent", dest],
                ],
            )

    def test_failure_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "Cite")
            dest = os.path.join(tmp, "dest", "Cite")
            with self.assertRaises(RuntimeError):
                s.clone_into_parallel_tree(literal, dest, run=lambda argv: FakeCompletedProcess(1))

    def test_chown_failure_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "Cite")
            dest = os.path.join(tmp, "dest", "Cite")

            def fake_run(argv):
                return FakeCompletedProcess(1 if argv[0] == "sudo" else 0)

            with self.assertRaises(RuntimeError):
                s.clone_into_parallel_tree(literal, dest, run=fake_run)


class RemountReadonlyTests(unittest.TestCase):
    def test_success_runs_expected_mount_command(self):
        calls = []

        def fake_run(argv):
            calls.append(argv)
            return FakeCompletedProcess(0)

        s.remount_readonly("/w/core", run=fake_run)
        self.assertEqual(calls, [["sudo", "mount", "-o", "remount,ro,bind", "/w/core"]])

    def test_failure_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            s.remount_readonly("/w/core", run=lambda argv: FakeCompletedProcess(1))


class BindIntoParallelTreeTests(unittest.TestCase):
    def test_creates_dest_and_bind_mounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "core")
            dest = os.path.join(tmp, "sandbox-home", "Wikimedia", "core")
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return FakeCompletedProcess(0)

            s.bind_into_parallel_tree(literal, dest, run=fake_run)
            self.assertTrue(os.path.isdir(dest))
            self.assertEqual(calls, [["sudo", "mount", "--bind", literal, dest]])

    def test_skips_if_dest_already_a_mount_point(self):
        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.object(s.os.path, "ismount", return_value=True):
            literal = os.path.join(tmp, "core")
            dest = os.path.join(tmp, "existing")

            def fake_run(argv):
                self.fail("should not mount when dest is already a mount point")

            s.bind_into_parallel_tree(literal, dest, run=fake_run)  # no exception

    def test_failure_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "core")
            dest = os.path.join(tmp, "dest")
            with self.assertRaises(RuntimeError):
                s.bind_into_parallel_tree(literal, dest, run=lambda argv: FakeCompletedProcess(1))


class OriginalsPathTests(unittest.TestCase):
    def test_nested_dir_maps_under_the_originals_dir(self):
        self.assertEqual(
            s.originals_path("/home/cananian", "/home/cananian/Wikimedia/Cite"),
            os.path.join(s.ORIGINALS_DIR, "Wikimedia/Cite"),
        )

    def test_dir_outside_host_home_returns_none(self):
        # Nothing to move it out of the way *for*: those repos get no
        # parallel clone either, so the literal path stays the original.
        self.assertIsNone(s.originals_path("/home/cananian", "/srv/shared/Skins"))


class MoveAsideTests(unittest.TestCase):
    """The writable clone takes over the host repo's own path, so the
    agent's starting working directory is the clone -- sbx/NOTES.md §39."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host_home = os.path.join(self.tmp.name, "home", "cananian")
        self.literal = os.path.join(self.host_home, "Wikimedia", "Cite")
        os.makedirs(self.literal)
        sandbox_home = os.path.join(self.tmp.name, "agent")
        # Not the real /home/agent: setup_repo's makedirs calls are not
        # faked, and tests have no business creating directories there.
        patcher = unittest.mock.patch.multiple(
            s,
            SANDBOX_HOME=sandbox_home,
            ORIGINALS_DIR=os.path.join(sandbox_home, ".sbx-originals"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dest = os.path.join(sandbox_home, "Wikimedia", "Cite")
        self.orig = os.path.join(sandbox_home, ".sbx-originals", "Wikimedia", "Cite")
        # move_original's own makedirs has already created sandbox_home by
        # the time the clone runs, so the chown covers what the clone made.
        self.chown_root = os.path.join(sandbox_home, "Wikimedia")
        self.calls = []
        self.failures = {}

    def fake_run(self, argv):
        self.calls.append(argv)
        for key, code in self.failures.items():
            if key in argv:
                return FakeCompletedProcess(code)
        return FakeCompletedProcess(0)

    def setup_repo(self, is_ro=False, literal=None, mounted=True):
        literal = literal or self.literal
        mounts = {literal} if mounted else set()
        with unittest.mock.patch(
            "os.path.ismount", side_effect=lambda p: p in mounts
        ), contextlib.redirect_stderr(io.StringIO()) as err:
            entry = s.setup_repo(self.host_home, literal, is_ro, run=self.fake_run)
        self.stderr = err.getvalue()
        return entry

    def test_the_clone_lands_on_the_host_repos_own_path(self):
        entry = self.setup_repo()
        dest = entry["dest"]
        self.assertEqual(entry, {"literal": self.literal, "dest": self.dest,
                                 "orig": self.orig, "mode": "alias"})
        self.assertEqual(
            self.calls,
            [
                ["sudo", "mount", "--move", self.literal, self.orig],
                # From the *moved* original, never from self.literal: the
                # alternates line --shared writes has to point at something
                # the bind mount below won't shadow, or the clone ends up
                # alternating to itself.
                ["git", "clone", "--shared", self.orig, dest],
                ["sudo", "chown", "-R", f"{s.SANDBOX_USER}:{s.SANDBOX_USER}", self.chown_root],
                ["sudo", "mount", "-o", "remount,ro,bind", self.orig],
                ["sudo", "mount", "--bind", dest, self.literal],
            ],
        )
        self.assertTrue(os.path.isdir(self.orig))

    def test_a_failed_move_falls_back_to_the_old_layout(self):
        self.failures["--move"] = 32
        entry = self.setup_repo()
        dest = entry["dest"]
        self.assertEqual(dest, self.dest)
        # Recorded as "clone", so the restart pass doesn't move a mirror
        # this clone's own alternates line points at.
        self.assertEqual(entry["mode"], "clone")
        self.assertIsNone(entry["orig"])
        self.assertEqual(
            self.calls[1:],
            [
                ["git", "clone", "--shared", self.literal, dest],
                ["sudo", "chown", "-R", f"{s.SANDBOX_USER}:{s.SANDBOX_USER}", self.chown_root],
                ["sudo", "mount", "-o", "remount,ro,bind", self.literal],
            ],
        )
        self.assertIn("warning: sudo mount --move", self.stderr)

    def test_a_failed_bind_costs_the_alias_and_nothing_else(self):
        self.failures["--bind"] = 32
        entry = self.setup_repo()
        dest = entry["dest"]
        self.assertEqual(dest, self.dest)
        self.assertEqual(entry["mode"], "clone")
        self.assertEqual(self.calls[-1], ["sudo", "mount", "--bind", dest, self.literal])
        self.assertIn("warning: sudo mount --bind", self.stderr)
        self.assertIn(dest, self.stderr)

    def test_a_path_that_is_not_a_mount_is_left_where_it_is(self):
        # A hand invocation, or a directory sbx never mounted -- mounting
        # the clone over it would hide real files rather than a mirror.
        dest = self.setup_repo(mounted=False)["dest"]
        self.assertNotIn("--move", [a for call in self.calls for a in call])
        self.assertNotIn("--bind", [a for call in self.calls for a in call])
        self.assertEqual(self.calls[0], ["git", "clone", "--shared", self.literal, dest])
        self.assertIn("is not a mount point", self.stderr)

    def test_a_ro_repo_keeps_the_original_at_its_own_path(self):
        # ':ro' asks for the pristine original; the literal path showing it
        # is the feature, not the thing §39 was fixing.
        entry = self.setup_repo(is_ro=True)
        dest = entry["dest"]
        self.assertEqual(entry["mode"], "bind")
        self.assertEqual(
            self.calls,
            [
                ["sudo", "mount", "-o", "remount,ro,bind", self.literal],
                ["sudo", "mount", "--bind", self.literal, dest],
                ["sudo", "mount", "-o", "remount,ro,bind", dest],
            ],
        )

    def test_a_repo_outside_host_home_is_not_moved(self):
        outside = os.path.join(self.tmp.name, "srv", "Skins")
        os.makedirs(outside)
        entry = self.setup_repo(literal=outside)
        self.assertIsNone(entry["dest"])
        self.assertEqual(entry["mode"], "inplace")
        outside = entry["literal"]
        self.assertEqual(self.calls, [["sudo", "mount", "-o", "remount,ro,bind", outside]])

    def test_binding_a_clone_that_is_already_the_literal_path_is_a_no_op(self):
        # Idempotence: the alias is already up (a re-run inside a live
        # sandbox), so there is nothing to mount and stacking a second bind
        # mount would only make the teardown harder.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(s.bind_over(self.literal, self.literal, run=self.fake_run))
        self.assertEqual(self.calls, [])


class RestoreTests(unittest.TestCase):
    """The restart pass. `sbx stop` throws the mount namespace away, so
    every mount setup_repo made has to be remade -- and the ones it didn't
    make must be left alone (sbx/NOTES.md §40)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.originals = os.path.join(self.tmp.name, "originals")
        self.literal = os.path.join(self.tmp.name, "home", "cananian", "Cite")
        self.dest = os.path.join(self.tmp.name, "agent", "Cite")
        self.orig = os.path.join(self.originals, "Cite")
        os.makedirs(self.literal)
        os.makedirs(self.dest)
        self.calls = []

    def fake_run(self, argv):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def entry(self, mode):
        return {"literal": self.literal, "dest": self.dest,
                "orig": self.orig if mode == "alias" else None, "mode": mode}

    def restore(self, entry, mounts=None):
        mounts = {self.literal} if mounts is None else mounts
        with unittest.mock.patch(
            "os.path.ismount", side_effect=lambda p: p in mounts
        ), contextlib.redirect_stderr(io.StringIO()) as err:
            s.restore_repo(entry, run=self.fake_run)
        self.stderr = err.getvalue()

    def test_the_alias_is_rebuilt_move_remount_bind(self):
        self.restore(self.entry("alias"))
        self.assertEqual(
            self.calls,
            [
                ["sudo", "mount", "--move", self.literal, self.orig],
                # The read-only remount is namespace state too, so the
                # mirror came back writable; this is what closes that.
                ["sudo", "mount", "-o", "remount,ro,bind", self.orig],
                ["sudo", "mount", "--bind", self.dest, self.literal],
            ],
        )

    def test_nothing_happens_when_the_alias_is_already_up(self):
        # A start with no intervening stop: restore_repo runs, sees the
        # literal path is already the clone, and does nothing.
        self.restore({**self.entry("alias"), "literal": self.dest})
        self.assertEqual(self.calls, [])

    def test_a_fallback_clone_only_gets_its_mirror_locked_again(self):
        # mode "clone" means this clone's alternates point at the literal
        # path. Moving it now would break exactly what §39 avoided.
        self.restore(self.entry("clone"))
        self.assertEqual(
            self.calls, [["sudo", "mount", "-o", "remount,ro,bind", self.literal]]
        )

    def test_a_ro_repo_gets_both_of_its_mounts_back(self):
        self.restore(self.entry("bind"))
        self.assertEqual(
            self.calls,
            [
                ["sudo", "mount", "-o", "remount,ro,bind", self.literal],
                ["sudo", "mount", "--bind", self.literal, self.dest],
                ["sudo", "mount", "-o", "remount,ro,bind", self.dest],
            ],
        )

    def test_a_repo_outside_host_home_is_only_remounted(self):
        entry = {"literal": self.literal, "dest": None, "orig": None, "mode": "inplace"}
        self.restore(entry)
        self.assertEqual(
            self.calls, [["sudo", "mount", "-o", "remount,ro,bind", self.literal]]
        )

    def test_the_first_container_start_is_a_no_op(self):
        # Startup commands run before the install step has cloned
        # anything, so a dest that isn't there yet is not a failure.
        entry = {**self.entry("alias"), "dest": os.path.join(self.tmp.name, "nope")}
        self.restore(entry)
        self.assertEqual(self.calls, [])

    def test_one_failing_repo_does_not_stop_the_others(self):
        def fake_run(argv):
            self.calls.append(argv)
            return FakeCompletedProcess(32 if self.literal in argv else 0)

        other = os.path.join(self.tmp.name, "home", "cananian", "core")
        os.makedirs(other)
        entries = [
            self.entry("clone"),
            {"literal": other, "dest": self.dest, "orig": None, "mode": "clone"},
        ]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            status = s.restore_mounts(entries, run=fake_run)
        self.assertEqual(status, 1)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("error: sudo mount -o remount,ro,bind", err.getvalue())

    def test_layout_round_trips(self):
        entries = [self.entry("alias"), self.entry("bind")]
        self.assertTrue(s.write_layout(entries, originals_dir=self.originals))
        self.assertEqual(s.load_layout(self.originals), entries)

    def test_no_layout_file_is_not_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(s.load_layout(self.originals))
            self.assertEqual(s.run_restore([self.originals], run=self.fake_run), 0)
        self.assertEqual(self.calls, [])
        self.assertNotIn("error:", err.getvalue())

    def test_a_layout_from_the_future_is_refused_rather_than_guessed_at(self):
        os.makedirs(self.originals)
        with open(s.layout_path(self.originals), "w", encoding="utf-8") as f:
            json.dump({"version": s.LAYOUT_VERSION + 1, "repos": [self.entry("alias")]}, f)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(s.load_layout(self.originals))
        self.assertIn("error:", err.getvalue())

    def test_a_restore_that_changed_nothing_is_reported_as_an_error(self):
        # The failure this check exists for (§41): every mount command
        # "succeeds", the pass exits 0 with an empty problem list, and the
        # sandbox has none of its mounts. Post-conditions, not exit codes.
        with unittest.mock.patch("os.path.ismount", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            status = s.restore_mounts([self.entry("alias")], run=self.fake_run)
        self.assertEqual(status, 1)
        self.assertIn(f"error: {self.literal} is not the clone at {self.dest}",
                      err.getvalue())
        self.assertIn("was not moved aside", err.getvalue())

    def test_a_restore_that_took_reports_nothing(self):
        entry = {**self.entry("alias"), "literal": self.dest}
        with unittest.mock.patch("os.path.ismount", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            status = s.restore_mounts([entry], run=self.fake_run)
        self.assertEqual(status, 0)
        self.assertNotIn("error:", err.getvalue())

    def test_a_dangling_alternate_is_an_error_all_by_itself(self):
        # 6707 reachable commits before a restart, 5 after -- and git says
        # nothing until you walk history (§40).
        info = os.path.join(self.dest, ".git", "objects", "info")
        os.makedirs(info)
        gone = os.path.join(self.tmp.name, "originals", "Cite", ".git", "objects")
        with open(os.path.join(info, "alternates"), "w", encoding="utf-8") as f:
            f.write(gone + "\n")
        entry = {**self.entry("alias"), "literal": self.dest}
        with unittest.mock.patch("os.path.ismount", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            status = s.restore_mounts([entry], run=self.fake_run)
        self.assertEqual(status, 1)
        self.assertIn(f"borrows objects from {gone}, which does not exist", err.getvalue())

    def test_the_restore_narrates_what_it_did_to_each_repo(self):
        # A silent success is indistinguishable from a silent no-op in
        # /var/log, which is where this pass is read from.
        self.restore(self.entry("clone"))
        self.assertIn(f"{self.literal}: restoring (clone)", self.stderr)

    def test_restore_writes_its_own_log_not_the_setup_one(self):
        # The setup report is the record of the run that built the
        # sandbox; a restart must not overwrite it (§38).
        log_dir = os.path.join(self.tmp.name, "log")
        os.makedirs(log_dir)
        with contextlib.redirect_stderr(io.StringIO()):
            code = s.main(["--restore", self.originals], run=self.fake_run, log_dir=log_dir)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(log_dir, s.RESTORE_STATUS_NAME)))
        self.assertFalse(os.path.exists(os.path.join(log_dir, s.SETUP_STATUS_NAME)))

    def test_the_restore_log_keeps_the_runs_before_this_one(self):
        # It runs on every container start, and every `sbx exec` is a
        # start, so the question it gets asked is "what did the last few
        # do?". A truncating log can't answer, and makes an empty file
        # ambiguous between "never ran" and "ran and overwrote" (§46).
        log_dir = os.path.join(self.tmp.name, "log")
        os.makedirs(log_dir)
        s.write_layout([self.entry("clone")], originals_dir=self.originals)
        for _ in range(3):
            with contextlib.redirect_stderr(io.StringIO()):
                s.main(["--restore", self.originals], run=self.fake_run, log_dir=log_dir)
        with open(os.path.join(log_dir, s.RESTORE_LOG_NAME), encoding="utf-8") as f:
            log = f.read()
        self.assertEqual(log.count("=== --restore "), 3)
        self.assertEqual(log.count(f"{self.literal}: restoring (clone)"), 3)

    def test_a_restore_log_that_has_grown_too_big_starts_over(self):
        log_dir = os.path.join(self.tmp.name, "log")
        os.makedirs(log_dir)
        path = os.path.join(log_dir, s.RESTORE_LOG_NAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * (s.RESTORE_LOG_MAX_BYTES + 1))
        s.write_layout([self.entry("clone")], originals_dir=self.originals)
        with contextlib.redirect_stderr(io.StringIO()):
            s.main(["--restore", self.originals], run=self.fake_run, log_dir=log_dir)
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("xxxx", f.read())


class MountOptionsTests(unittest.TestCase):
    """Reading ro-vs-rw out of /proc/self/mountinfo. `os.path.ismount`
    answers "is something mounted here"; the other half of the question is
    the whole of §40's fourth finding."""

    def mountinfo(self, *lines):
        path = os.path.join(self.tmp.name, "mountinfo")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_options_come_from_the_matching_line(self):
        info = self.mountinfo(
            "28 1 0:24 / /home/agent/other ro,relatime - virtiofs host rw",
            "29 1 0:25 / /home/agent/.sbx-originals/Cite ro,nosuid,relatime - virtiofs host rw",
        )
        self.assertEqual(
            s.mount_options("/home/agent/.sbx-originals/Cite", mountinfo=info),
            ["ro", "nosuid", "relatime"],
        )

    def test_the_topmost_mount_wins(self):
        # §39 stacks a bind mount over a mount that is already there. The
        # one that decides whether a write succeeds is the last one.
        info = self.mountinfo(
            "28 1 0:24 / /home/cananian/Cite ro,relatime - virtiofs host rw",
            "29 1 0:25 / /home/cananian/Cite rw,relatime - overlay overlay rw",
        )
        self.assertEqual(s.mount_options("/home/cananian/Cite", mountinfo=info)[0], "rw")

    def test_nothing_mounted_there_is_none_not_an_empty_list(self):
        info = self.mountinfo("28 1 0:24 / /elsewhere ro,relatime - virtiofs host rw")
        self.assertIsNone(s.mount_options("/home/cananian/Cite", mountinfo=info))

    def test_an_unreadable_mountinfo_is_none(self):
        self.assertIsNone(s.mount_options("/", mountinfo="/nonexistent/mountinfo"))

    def test_a_space_in_the_path_is_unescaped(self):
        info = self.mountinfo(
            "28 1 0:24 / /home/agent/My\\040Repo ro,relatime - virtiofs host rw"
        )
        self.assertEqual(s.mount_options("/home/agent/My Repo", mountinfo=info), ["ro", "relatime"])

    def test_a_writable_mirror_is_a_problem_worth_words(self):
        info = self.mountinfo("28 1 0:24 / /mirror rw,relatime - virtiofs host rw")
        with unittest.mock.patch.object(s, "MOUNTINFO", info):
            problem = s.readonly_problem("/mirror", "the host mirror")
        self.assertIn("the host mirror is writable from inside the sandbox", problem)

    def test_a_read_only_mirror_is_not(self):
        info = self.mountinfo("28 1 0:24 / /mirror ro,relatime - virtiofs host rw")
        with unittest.mock.patch.object(s, "MOUNTINFO", info):
            self.assertIsNone(s.readonly_problem("/mirror", "the host mirror"))

    def test_an_unmeasurable_mount_is_not_claimed_to_be_writable(self):
        # The §41 mistake in reverse: don't report a failure we haven't
        # measured. "Not in mountinfo" is the callers' own check to make.
        with unittest.mock.patch.object(s, "MOUNTINFO", "/nonexistent/mountinfo"):
            self.assertIsNone(s.readonly_problem("/mirror", "the host mirror"))


class SharedSkillsTests(unittest.TestCase):
    """Locking ~/.claude/skills, which sbx mounts into *every* sandbox on
    the host from one host directory (MEASURED, sbx/NOTES.md §77.2-§77.4:
    writable, and readable and deletable from a second sandbox, outliving
    the `sbx rm` of the one that wrote it).

    What is being tested is a second layer and not a boundary -- the agent
    is root in its own sandbox and can `mount -o remount,rw,bind` straight
    back out of it (§79). These tests assert it is applied, consistently,
    on every path into a sandbox; SECURITY.md §7.6 carries what that is
    and is not worth."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.skills = os.path.join(self.tmp.name, "skills")
        os.makedirs(self.skills)
        self.calls = []

    def fake_run(self, argv):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def mountinfo(self, options):
        path = os.path.join(self.tmp.name, "mountinfo")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"28 1 0:24 / {self.skills} {options} - virtiofs none rw\n")
        return unittest.mock.patch.object(s, "MOUNTINFO", path)

    def lock(self, mounted=True):
        with unittest.mock.patch("os.path.ismount", return_value=mounted), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.locked = s.lock_shared_skills(path=self.skills, run=self.fake_run)
        return err.getvalue()

    def test_the_store_is_remounted_read_only(self):
        with self.mountinfo("rw"):
            err = self.lock()
        self.assertTrue(self.locked)
        self.assertEqual(
            self.calls, [["sudo", "mount", "-o", "remount,ro,bind", self.skills]]
        )
        self.assertIn("remounted read-only", err)

    def test_a_store_that_is_already_locked_is_left_alone(self):
        # Every `sbx exec` is a container start, so this runs constantly on
        # a sandbox that never stopped. Remounting something already ro is
        # harmless but noisy, and the log is read by a human.
        with self.mountinfo("ro"):
            self.lock()
        self.assertTrue(self.locked)
        self.assertEqual(self.calls, [])

    def test_a_sandbox_with_no_shared_store_is_not_touched(self):
        # An sbx that does not share a skills store -- an older one, or a
        # future one that fixed this -- has a plain directory there, which
        # is private to the sandbox and none of our business.
        err = self.lock(mounted=False)
        self.assertTrue(self.locked)
        self.assertEqual(self.calls, [])
        self.assertEqual(err, "")

    def test_a_failed_remount_warns_and_does_not_stop_the_setup(self):
        def failing(argv):
            self.calls.append(argv)
            return FakeCompletedProcess(32)

        with self.mountinfo("rw"), \
                unittest.mock.patch("os.path.ismount", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            locked = s.lock_shared_skills(path=self.skills, run=failing)
        self.assertFalse(locked)
        # A warning, not an error: the sandbox is still worth having, and
        # SetupLog collects both prefixes into the status file either way.
        self.assertIn("warning:", err.getvalue())
        self.assertIn("shared with every other sandbox", err.getvalue())

    def test_a_writable_store_is_a_reported_problem(self):
        with self.mountinfo("rw"), unittest.mock.patch("os.path.ismount", return_value=True):
            problem = s.shared_skills_problem(self.skills)
        self.assertIn("the shared agent-skills store is writable", problem)

    def test_nothing_mounted_there_is_not_claimed_as_a_problem(self):
        # §41's mistake in reverse: "not in mountinfo" is not evidence of
        # a writable shared mount, it is evidence of no shared mount.
        with unittest.mock.patch("os.path.ismount", return_value=False):
            self.assertIsNone(s.shared_skills_problem(self.skills))

    def test_the_restart_pass_relocks_it_before_looking_for_a_layout(self):
        # The store is namespace state like every other mount, so `sbx
        # stop` drops the lock -- and on the *first* start there is no
        # layout at all, which is exactly when the old code returned early.
        originals = os.path.join(self.tmp.name, "originals")
        with unittest.mock.patch.object(s, "SHARED_SKILLS_DIR", self.skills), \
                self.mountinfo("rw"), \
                unittest.mock.patch("os.path.ismount", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            status = s.run_restore([originals], run=self.fake_run)
        self.assertEqual(status, 0)
        self.assertEqual(
            self.calls, [["sudo", "mount", "-o", "remount,ro,bind", self.skills]]
        )

    def test_verify_reports_a_store_that_came_back_writable(self):
        # The same asymmetry §40 found for the host mirrors: the repos can
        # all be restored correctly while this one mount is not, and
        # nothing about the path or its permissions shows it.
        originals = os.path.join(self.tmp.name, "originals")
        self.assertTrue(s.write_layout([], originals_dir=originals))
        with unittest.mock.patch.object(s, "SHARED_SKILLS_DIR", self.skills), \
                self.mountinfo("rw"), \
                unittest.mock.patch("os.path.ismount", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            status = s.run_verify([originals])
        self.assertEqual(status, 1)
        self.assertIn("error: ", err.getvalue())
        self.assertIn("the shared agent-skills store is writable", err.getvalue())


class VerifyTests(unittest.TestCase):
    """`wmf-sbx-setup --verify`: the same post-conditions `--restore`
    checks, for every other way into a sandbox -- a plain `sbx run`, an
    `sbx exec`, a start `wmf-sbx-resume` never saw (§44)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.originals = os.path.join(self.tmp.name, "originals")
        self.literal = os.path.join(self.tmp.name, "home", "cananian", "Cite")
        self.dest = os.path.join(self.tmp.name, "agent", "Cite")
        self.orig = os.path.join(self.originals, "Cite")
        os.makedirs(self.dest)
        os.makedirs(self.orig)

    def entry(self, **over):
        entry = {"literal": self.literal, "dest": self.dest,
                 "orig": self.orig, "mode": "alias"}
        entry.update(over)
        return entry

    def write_layout(self, *entries):
        self.assertTrue(s.write_layout(list(entries), originals_dir=self.originals))

    def verify(self, *entries):
        self.write_layout(*entries)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.status = s.run_verify([self.originals])
        return err.getvalue()

    def mountinfo(self, options):
        path = os.path.join(self.tmp.name, "mountinfo")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"28 1 0:24 / {self.orig} {options} - virtiofs host rw\n")
        return unittest.mock.patch.object(s, "MOUNTINFO", path)

    def test_a_restored_alias_is_reported_ok(self):
        entry = self.entry(literal=self.dest)
        with unittest.mock.patch("os.path.ismount", return_value=True), self.mountinfo("ro"):
            err = self.verify(entry)
        self.assertEqual(self.status, 0)
        self.assertIn(f"{self.dest}: ok (alias)", err)

    def test_a_writable_mirror_is_an_error_even_though_the_alias_is_up(self):
        # The alias half can succeed while the lock-down half doesn't --
        # that is exactly the shape of the §40 hole, and it is invisible
        # unless something looks at the mount options.
        entry = self.entry(literal=self.dest)
        with unittest.mock.patch("os.path.ismount", return_value=True), self.mountinfo("rw"):
            err = self.verify(entry)
        self.assertEqual(self.status, 1)
        self.assertIn("is writable from inside the sandbox", err)

    def test_a_lost_alias_is_an_error(self):
        with unittest.mock.patch("os.path.ismount", return_value=False):
            err = self.verify(self.entry())
        self.assertEqual(self.status, 1)
        self.assertIn(f"error: {self.literal} is not the clone at {self.dest}", err)

    def test_a_repo_that_was_never_set_up_does_not_look_like_success(self):
        entry = self.entry(dest=os.path.join(self.tmp.name, "nope"))
        err = self.verify(entry)
        self.assertEqual(self.status, 0)
        self.assertIn("nothing at", err)
        self.assertNotIn(": ok", err)

    def test_no_layout_is_an_error_here_unlike_restore(self):
        # --restore runs unattended on every container start, where "no
        # layout" is the ordinary state of a hand-built kit. --verify was
        # typed by someone expecting an answer; not finding one is a
        # failed check, not a quiet 0.
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(s.run_verify([self.originals]), 1)
        self.assertIn("no layout to verify", err.getvalue())

    def test_a_wait_gives_the_dispatcher_time_to_finish(self):
        # §46: the startup dispatcher restores the layout on every
        # container start, but it doesn't block the `sbx exec` that
        # triggered the start. A check made immediately loses a race it
        # would win a second later, so --verify can wait for it.
        self.write_layout(self.entry())
        attempts = []
        clock = iter([0.0, 0.0, 0.5, 0.5, 1.0, 1.0, 1.0])

        def verify(entry):
            attempts.append(entry)
            return [] if len(attempts) >= 3 else ["not yet"]

        with contextlib.redirect_stderr(io.StringIO()) as err:
            status = s.run_verify(
                [self.originals, "--wait", "5"], verify=verify,
                sleep=lambda seconds: None, clock=lambda: next(clock),
            )
        self.assertEqual(status, 0)
        self.assertEqual(len(attempts), 3)
        self.assertNotIn("error:", err.getvalue())

    def test_a_wait_that_runs_out_reports_the_problems_it_still_has(self):
        self.write_layout(self.entry())
        clock = iter([0.0, 0.0, 2.0, 2.0, 9.0, 9.0, 9.0])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            status = s.run_verify(
                [self.originals, "--wait=5"], verify=lambda entry: ["still wrong"],
                sleep=lambda seconds: None, clock=lambda: next(clock),
            )
        self.assertEqual(status, 1)
        self.assertIn("error: still wrong", err.getvalue())
        self.assertIn("still wrong after 5s", err.getvalue())

    def test_without_a_wait_it_checks_once(self):
        self.write_layout(self.entry())
        attempts = []
        with contextlib.redirect_stderr(io.StringIO()):
            s.run_verify([self.originals], verify=lambda e: attempts.append(e) or ["no"])
        self.assertEqual(len(attempts), 1)

    def test_a_wait_that_is_not_a_number_says_so(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(s.run_verify([self.originals, "--wait=soon"]), 1)
        self.assertIn("--wait wants a number of seconds", err.getvalue())

    def test_the_dir_and_the_wait_can_come_in_either_order(self):
        self.assertEqual(s.parse_verify_argv(["--wait=3", "/originals"]),
                         ("/originals", 3.0))
        self.assertEqual(s.parse_verify_argv(["/originals", "--wait", "3"]),
                         ("/originals", 3.0))
        self.assertEqual(s.parse_verify_argv([]), (None, 0.0))

    def test_verify_changes_nothing(self):
        # No mounts, no root, no /var/log: it is safe to run at any time,
        # which is the point of having it separate from --restore.
        def explode(*a, **k):  # pragma: no cover - must never be called
            raise AssertionError("--verify ran a command")

        self.write_layout(self.entry())
        log_dir = os.path.join(self.tmp.name, "log")
        os.makedirs(log_dir)
        with unittest.mock.patch("os.path.ismount", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            s.main(["--verify", self.originals], run=explode, log_dir=log_dir)
        self.assertEqual(os.listdir(log_dir), [])


class SetupRepoTests(unittest.TestCase):
    def test_repo_under_host_home_is_cloned_then_remounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "home", "Wikimedia", "Cite")
            os.makedirs(literal)
            host_home = os.path.join(tmp, "home")
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return FakeCompletedProcess(0)

            # A tempdir is not a mount point, so this is the §39 fallback:
            # nothing to move aside, clone from the literal path, and the
            # layout is the one every sandbox had before §39.
            with contextlib.redirect_stderr(io.StringIO()):
                dest = s.setup_repo(host_home, literal, False, run=fake_run)["dest"]
            self.assertEqual(dest, os.path.join(s.SANDBOX_HOME, "Wikimedia", "Cite"))
            self.assertEqual(calls[0][:3], ["git", "clone", "--shared"])
            self.assertEqual(calls[1][:3], ["sudo", "chown", "-R"])
            self.assertEqual(calls[2], ["sudo", "mount", "-o", "remount,ro,bind", literal])

    def test_repo_outside_host_home_is_only_remounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "Skins")
            host_home = os.path.join(tmp, "home")
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return FakeCompletedProcess(0)

            dest = s.setup_repo(host_home, literal, False, run=fake_run)["dest"]
            self.assertIsNone(dest)
            self.assertEqual(calls, [["sudo", "mount", "-o", "remount,ro,bind", literal]])

    def test_ro_repo_under_host_home_is_remounted_then_bind_mounted_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "home", "Wikimedia", "core")
            os.makedirs(literal)
            host_home = os.path.join(tmp, "home")
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return FakeCompletedProcess(0)

            dest = s.setup_repo(host_home, literal, True, run=fake_run)["dest"]
            expected_dest = os.path.join(s.SANDBOX_HOME, "Wikimedia", "core")
            self.assertEqual(dest, expected_dest)
            self.assertEqual(
                calls,
                [
                    ["sudo", "mount", "-o", "remount,ro,bind", literal],
                    ["sudo", "mount", "--bind", literal, expected_dest],
                    ["sudo", "mount", "-o", "remount,ro,bind", expected_dest],
                ],
            )

    def test_ro_repo_outside_host_home_is_only_remounted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "Skins")
            host_home = os.path.join(tmp, "home")
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return FakeCompletedProcess(0)

            dest = s.setup_repo(host_home, literal, True, run=fake_run)["dest"]
            self.assertIsNone(dest)
            self.assertEqual(calls, [["sudo", "mount", "-o", "remount,ro,bind", literal]])


class SetupLogTests(unittest.TestCase):
    """The report that survives `sbx create`'s collapsed install step --
    sbx/NOTES.md §32.1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "setup.log")

    def test_everything_written_reaches_both_the_stream_and_the_file(self):
        stream = io.StringIO()
        log = s.SetupLog(stream, self.path)
        print("cloned /a -> /b", file=log)
        log.close()
        self.assertEqual(stream.getvalue(), "cloned /a -> /b\n")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "cloned /a -> /b\n")

    def test_problem_lines_are_collected_and_ordinary_ones_are_not(self):
        log = s.SetupLog(io.StringIO(), self.path)
        print("cloned /a -> /b", file=log)
        print("warning: could not rename origin", file=log)
        print("error: npm install failed", file=log)
        log.close()
        self.assertEqual(
            log.problems,
            ["warning: could not rename origin", "error: npm install failed"],
        )

    def test_a_line_split_across_writes_is_still_recognised(self):
        # print() with several arguments writes each separately, so a
        # scanner that only looked at whole write() calls would miss this.
        log = s.SetupLog(io.StringIO(), self.path)
        log.write("warning: could not ")
        log.write("chown /home/agent/core\n")
        log.close()
        self.assertEqual(log.problems, ["warning: could not chown /home/agent/core"])

    def test_a_trailing_line_without_a_newline_is_not_lost(self):
        log = s.SetupLog(io.StringIO(), self.path)
        log.write("error: died mid-line")
        log.close()
        self.assertEqual(log.problems, ["error: died mid-line"])

    def test_the_same_problem_is_only_reported_once(self):
        log = s.SetupLog(io.StringIO(), self.path)
        print("warning: same thing", file=log)
        log.record("warning: same thing")
        log.close()
        self.assertEqual(log.problems, ["warning: same thing"])

    def test_an_unopenable_log_costs_the_log_not_the_setup(self):
        stream = io.StringIO()
        log = s.SetupLog(stream, os.path.join(self.tmp.name, "nope", "setup.log"))
        print("cloned /a -> /b", file=log)
        log.close()
        self.assertIsNone(log.path)
        self.assertIn("could not open", stream.getvalue())
        self.assertIn("cloned /a -> /b", stream.getvalue())


class WriteStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "setup.status")

    def test_writes_the_version_exit_and_problems(self):
        self.assertTrue(s.write_status(self.path, 1, ["error: boom"], log_path="/var/log/x"))
        with open(self.path, encoding="utf-8") as f:
            status = json.load(f)
        self.assertEqual(status, {
            "version": s.STATUS_VERSION, "exit": 1,
            "problems": ["error: boom"], "log": "/var/log/x",
        })

    def test_an_unwritable_status_warns_rather_than_raising(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            written = s.write_status(
                os.path.join(self.tmp.name, "nope", "setup.status"), 0, []
            )
        self.assertFalse(written)
        self.assertIn("could not write", err.getvalue())


class MainLoggingTests(unittest.TestCase):
    """main()'s end of it: the log and status files actually appear, and a
    crash in the setup itself still produces a report."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = os.path.join(self.tmp.name, "log")
        os.makedirs(self.log_dir)
        self.host_home = os.path.join(self.tmp.name, "home")
        self.repo = os.path.join(self.host_home, "Wikimedia", "Cite")
        os.makedirs(self.repo)
        # run_setup records the layout for the restart pass; keep that out
        # of the real /home/agent/.sbx-originals.
        self.originals = os.path.join(self.tmp.name, "originals")
        patcher = unittest.mock.patch.object(s, "ORIGINALS_DIR", self.originals)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The helper install reads ~/bin. Give it a directory that holds
        # every helper: a clean run is what this test measures, and a
        # missing helper is a warning, so otherwise the result depends on
        # the machine that runs the test.
        helper_bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(helper_bin)
        for name in s.HELPER_SCRIPTS:
            open(os.path.join(helper_bin, name), "w").close()
        patcher = unittest.mock.patch.object(s, "HELPER_SOURCE_DIR", helper_bin)
        patcher.start()
        self.addCleanup(patcher.stop)

    def layout(self):
        with open(s.layout_path(self.originals), encoding="utf-8") as f:
            return json.load(f)

    def run_main(self, run=None):
        run = run or (lambda argv, cwd=None: FakeCompletedProcess(0))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = s.main(
                [self.host_home, "9418", self.repo], run=run,
                popen=lambda *a, **kw: None, log_dir=self.log_dir,
            )
        return code, err.getvalue()

    def status(self):
        with open(os.path.join(self.log_dir, s.SETUP_STATUS_NAME), encoding="utf-8") as f:
            return json.load(f)

    def log(self):
        with open(os.path.join(self.log_dir, s.SETUP_LOG_NAME), encoding="utf-8") as f:
            return f.read()

    def test_a_clean_run_still_leaves_a_log_and_an_empty_problem_list(self):
        code, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.status()["problems"], [])
        self.assertEqual(self.status()["exit"], 0)
        # Same narration in both places: the log is the terminal output
        # that `sbx create` swallowed, not a separate summary.
        self.assertIn(f"cloned {self.repo} ->", self.log())
        self.assertIn(f"cloned {self.repo} ->", err)
        # And the restart pass has something to work from.
        self.assertEqual(
            self.layout()["repos"],
            [{"literal": self.repo, "dest": os.path.join(s.SANDBOX_HOME, "Wikimedia", "Cite"),
              "orig": None, "mode": "clone"}],
        )

    def test_a_failing_step_is_recorded_as_a_problem(self):
        def fake_run(argv, cwd=None):
            # Anything but the clone succeeds, so setup_repo raises.
            return FakeCompletedProcess(1 if "clone" in argv else 0)

        code, _err = self.run_main(run=fake_run)
        self.assertEqual(code, 1)
        status = self.status()
        self.assertEqual(status["exit"], 1)
        self.assertTrue(any("git clone" in p for p in status["problems"]), status)

    def test_a_crash_still_produces_a_status_and_a_traceback(self):
        def explode(argv, cwd=None):
            raise OSError("no such thing as git")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = s.main(
                [self.host_home, "9418", self.repo], run=explode,
                popen=lambda *a, **kw: None, log_dir=self.log_dir,
            )
            # Checked here, inside the redirect: main swaps sys.stderr for
            # the SetupLog, and a crash must not leave it swapped -- every
            # later print in the process would write to a closed file.
            self.assertIs(sys.stderr, err)
        self.assertEqual(code, 1)
        self.assertEqual(self.status()["exit"], 1)
        self.assertTrue(
            any("did not finish" in p for p in self.status()["problems"]),
            self.status(),
        )
        self.assertIn("Traceback", self.log())

    def test_the_status_names_the_log(self):
        self.run_main()
        self.assertEqual(self.status()["log"], os.path.join(self.log_dir, s.SETUP_LOG_NAME))


class StartDaemonTests(unittest.TestCase):
    def test_invokes_git_daemon_detached(self):
        calls = []

        def fake_popen(argv, **kw):
            calls.append((argv, kw))

        s.start_daemon("/home/agent", 9418, popen=fake_popen)
        self.assertEqual(len(calls), 1)
        argv, kw = calls[0]
        self.assertEqual(
            argv,
            [
                "sudo", "-u", "agent",
                "git", "-c", "safe.directory=*", "daemon", "--reuseaddr", "--export-all",
                "--base-path=/home/agent", "--listen=0.0.0.0", "--port=9418",
                "/home/agent",
            ],
        )
        self.assertEqual(kw["start_new_session"], True)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["stdout"], subprocess.DEVNULL)
        self.assertEqual(kw["stderr"], subprocess.DEVNULL)


class MainTests(unittest.TestCase):
    def _run_main(self, argv, run=None, popen=None):
        run = run or (lambda argv: FakeCompletedProcess(0))
        popen = popen or (lambda *a, **kw: None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            # log_dir=None: the tests have no business writing
            # to /var/log, and the log is exercised separately.
            code = s.main(argv, run=run, popen=popen, log_dir=None)
        return code, out.getvalue(), err.getvalue()

    def test_usage_error_with_fewer_than_three_args(self):
        code, _out, err = self._run_main(["/home/cananian", "9418"])
        self.assertEqual(code, 1)
        self.assertIn("Usage:", err)

    def test_reports_each_repo_cloned_or_remounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            host_home = os.path.join(tmp, "home")
            nested = os.path.join(host_home, "Wikimedia", "Cite")
            os.makedirs(nested)
            outside = os.path.join(tmp, "Skins")
            os.makedirs(outside)

            code, _out, err = self._run_main([host_home, "9418", nested, outside])
            self.assertEqual(code, 0)
            self.assertIn(f"cloned {nested} ->", err)
            self.assertIn(f"{outside} is outside {host_home}; remounted read-only in place", err)

    def test_starts_daemon_after_repos_are_set_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            host_home = os.path.join(tmp, "home")
            nested = os.path.join(host_home, "Wikimedia", "Cite")
            os.makedirs(nested)
            calls = []

            def fake_popen(argv, **kw):
                calls.append((argv, kw))

            code, _out, err = self._run_main([host_home, "9418", nested], popen=fake_popen)
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            argv, kw = calls[0]
            self.assertEqual(
                argv,
                [
                    "sudo", "-u", s.SANDBOX_USER,
                    "git", "-c", "safe.directory=*", "daemon", "--reuseaddr", "--export-all",
                    f"--base-path={s.SANDBOX_HOME}", "--listen=0.0.0.0", "--port=9418",
                    s.SANDBOX_HOME,
                ],
            )
            self.assertTrue(kw.get("start_new_session"))
            self.assertIn(f"git daemon listening on 0.0.0.0:9418, serving {s.SANDBOX_HOME}", err)

    def test_stops_and_reports_error_on_failure(self):
        def fake_run(argv):
            return FakeCompletedProcess(1)

        with tempfile.TemporaryDirectory() as tmp:
            host_home = os.path.join(tmp, "home")
            nested = os.path.join(host_home, "Wikimedia", "Cite")
            os.makedirs(nested)

            code, _out, err = self._run_main([host_home, "9418", nested], run=fake_run)
            self.assertEqual(code, 1)
            self.assertIn("error:", err)


class LinkIntoCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(os.path.join(self.core, "extensions"))
        self.cite = os.path.join(self.tmp.name, "Extensions", "Cite")
        os.makedirs(self.cite)
        # link_into_core chowns with os.lchown, which needs root -- these
        # tests don't run as root, so record the calls and swallow them.
        self.chowned = []

        def fake_lchown(path, uid, gid):
            self.chowned.append((path, uid, gid))

        patched = unittest.mock.patch.object(os, "lchown", fake_lchown)
        patched.start()
        self.addCleanup(patched.stop)

    UNSET = object()

    def link(self, links=None, core=UNSET, core_readonly=False):
        links = links if links is not None else [("extensions", "Cite", self.cite)]
        core = self.core if core is self.UNSET else core
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            count = s.link_into_core(core, links, core_readonly=core_readonly)
        return count, err.getvalue()

    def test_creates_the_symlink_and_chowns_the_link_itself(self):
        count, _err = self.link()
        dest = os.path.join(self.core, "extensions", "Cite")
        self.assertEqual(count, 1)
        # The target is the *parallel clone*, never the read-only
        # host-mirrored original -- the whole point of the parallel tree.
        self.assertEqual(os.readlink(dest), self.cite)
        # lchown, not chown: the link itself, not the clone it points at
        # (which is already agent-owned). `sudo chown -h` was what this
        # used to shell out to, and it left every link root-owned in the
        # first real sandbox.
        self.assertEqual(self.chowned, [(dest, *s.sandbox_ids())])

    def test_chown_failure_warns_but_keeps_the_link(self):
        def boom(path, uid, gid):
            raise PermissionError(1, "Operation not permitted")

        with unittest.mock.patch.object(os, "lchown", boom):
            count, err = self.link()
        self.assertEqual(count, 1)
        self.assertIn("could not chown", err)
        self.assertTrue(os.path.islink(os.path.join(self.core, "extensions", "Cite")))

    def test_creates_a_missing_skins_directory(self):
        skin = os.path.join(self.tmp.name, "Vector")
        os.makedirs(skin)
        count, _err = self.link([("skins", "Vector", skin)])
        self.assertEqual(count, 1)
        self.assertEqual(os.readlink(os.path.join(self.core, "skins", "Vector")), skin)

    def test_existing_correct_link_is_left_alone(self):
        # wmf-sbx-resume runs this again; a second pass must be a no-op.
        self.link()
        self.chowned.clear()
        count, _err = self.link()
        self.assertEqual(count, 0)
        self.assertEqual(self.chowned, [])

    def test_stale_link_is_replaced(self):
        dest = os.path.join(self.core, "extensions", "Cite")
        os.symlink("/some/host/path/Cite", dest)
        count, _err = self.link()
        self.assertEqual(count, 1)
        self.assertEqual(os.readlink(dest), self.cite)

    def test_real_directory_is_never_clobbered(self):
        dest = os.path.join(self.core, "extensions", "Cite")
        os.makedirs(dest)
        with open(os.path.join(dest, "keep-me"), "w", encoding="utf-8") as f:
            f.write("work in progress\n")
        count, err = self.link()
        self.assertEqual(count, 0)
        self.assertIn("already exists and is not a symlink", err)
        self.assertTrue(os.path.isfile(os.path.join(dest, "keep-me")))

    def test_readonly_core_is_skipped_with_a_reason(self):
        count, err = self.link(core_readonly=True)
        self.assertEqual(count, 0)
        self.assertIn("read-only bind mount", err)
        self.assertFalse(os.path.exists(os.path.join(self.core, "extensions", "Cite")))

    def test_no_core_in_the_sandbox_is_skipped_with_a_reason(self):
        count, err = self.link(core=None)
        self.assertEqual(count, 0)
        self.assertIn("no mediawiki/core", err)

    def test_nothing_to_link_says_nothing(self):
        count, err = self.link(links=[], core=None)
        self.assertEqual(count, 0)
        self.assertEqual(err, "")

    def test_one_bad_link_does_not_stop_the_rest(self):
        # Warn and continue: the git daemon the host fetches through is
        # worth more than an exact extensions/ directory.
        blocked = os.path.join(self.core, "extensions", "Blocked")
        os.makedirs(blocked)
        echo = os.path.join(self.tmp.name, "Extensions", "Echo")
        os.makedirs(echo)
        count, err = self.link([
            ("extensions", "Blocked", os.path.join(self.tmp.name, "Blocked")),
            ("extensions", "Echo", echo),
        ])
        self.assertEqual(count, 1)
        self.assertIn("warning:", err)
        self.assertTrue(os.path.islink(os.path.join(self.core, "extensions", "Echo")))


class LoadPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        path = os.path.join(self.tmp.name, "plan.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_reads_a_valid_plan(self):
        path = self.write('{"version": 1, "hostHome": "/home/c", '
                          '"daemonPort": "9418", "repos": []}')
        self.assertEqual(s.load_plan(path)["hostHome"], "/home/c")

    def test_missing_file_raises(self):
        with self.assertRaises(RuntimeError):
            s.load_plan(os.path.join(self.tmp.name, "nope.json"))

    def test_malformed_json_raises(self):
        with self.assertRaises(RuntimeError):
            s.load_plan(self.write("{oops"))

    def test_wrong_version_raises(self):
        with self.assertRaises(RuntimeError) as caught:
            s.load_plan(self.write('{"version": 99, "hostHome": "/h", '
                                   '"daemonPort": "1", "repos": []}'))
        self.assertIn("version", str(caught.exception))

    def test_missing_key_raises(self):
        # A half-understood plan silently drops repos, which is much worse
        # than refusing to start.
        with self.assertRaises(RuntimeError):
            s.load_plan(self.write('{"version": 1, "hostHome": "/h", "daemonPort": "1"}'))


class PlanFromArgvTests(unittest.TestCase):
    def test_normalises_the_positional_form(self):
        plan = s.plan_from_argv(["/home/c", "9418", "/home/c/Cite", "/home/c/core:ro"])
        self.assertEqual(plan["hostHome"], "/home/c")
        self.assertEqual(plan["primary"], "/home/c/Cite")
        self.assertEqual(
            plan["repos"][1],
            {"path": "/home/c/core", "canonical": None, "readOnly": True,
             "linkName": None, "linkDir": None, "upstreamUrl": None,
             "requested": True},
        )

    def test_carries_no_link_plan(self):
        # Positionally there is nowhere to put one -- link names contain
        # spaces and canonicals only exist host-side.
        plan = s.plan_from_argv(["/home/c", "9418", "/home/c/Cite"])
        self.assertTrue(all(r["linkName"] is None for r in plan["repos"]))

    def test_a_flag_this_copy_has_never_heard_of_says_so(self):
        # The in-sandbox script is a create-time snapshot, so a flag added
        # later arrives at an older sandbox as a plan-file path. "could not
        # read the plan file --verify" is a baffling way to learn that
        # (sbx/NOTES.md §45).
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(s.run_setup(["--verify"]), 1)
        message = err.getvalue()
        self.assertIn("error: unknown option '--verify'", message)
        self.assertIn("needs a newer sandbox", message)
        self.assertIn("Usage:", message)


class InstallHelperScriptsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.src)
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(self.code)

    def install(self, names=s.HELPER_SCRIPTS, code=0):
        self.code = code
        for name in names:
            open(os.path.join(self.src, name), "w").close()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            installed = s.install_helper_scripts(
                run=self.fake_run, source_dir=self.src, dest_dir="/usr/local/bin")
        return installed, err.getvalue()

    def test_installs_every_helper_executable(self):
        installed, _err = self.install()
        self.assertEqual(installed, list(s.HELPER_SCRIPTS))
        self.assertEqual(self.calls[0][:3], ["install", "-m", "0755"])
        self.assertTrue(self.calls[0][-1].endswith("/usr/local/bin/git-safe-reset"))

    def test_a_missing_helper_warns_and_does_not_stop_the_other(self):
        installed, err = self.install(names=["git-review-check"])
        self.assertEqual(installed, ["git-review-check"])
        self.assertIn("git-safe-reset is missing", err)
        self.assertIn("mw-install-browser is missing", err)

    def test_a_failed_install_warns_rather_than_raising(self):
        installed, err = self.install(code=1)
        self.assertEqual(installed, [])
        self.assertIn("could not install", err)


class AsAgentTests(unittest.TestCase):
    def test_runs_under_sudo_with_the_agents_own_home(self):
        seen = {}

        def fake_run(argv, cwd=None):
            seen["argv"], seen["cwd"] = argv, cwd
            return FakeCompletedProcess(0)

        s.as_agent(["composer", "update"], cwd="/w/core", run=fake_run)
        # -H, or composer's cache lands in /root and the files it writes
        # come out root-owned.
        self.assertEqual(seen["argv"], ["sudo", "-u", "agent", "-H", "composer", "update"])
        self.assertEqual(seen["cwd"], "/w/core")


class ConfigureRemotesTests(unittest.TestCase):
    """Swapping `origin` from the host mirror to the real upstream, and the
    remote name git-safe-reset is told to use afterwards."""

    URL = "https://gerrit.wikimedia.org/r/mediawiki/skins/MinervaNeue"

    def setUp(self):
        self.calls = []
        self.fail_on = None

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv[4:])  # drop the sudo -u agent -H prefix
        code = 1 if self.fail_on and self.fail_on in argv else 0
        return FakeCompletedProcess(code)

    def configure(self, url=URL):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            remote = s.configure_remotes("/home/agent/skin", url, run=self.fake_run)
        return remote, err.getvalue()

    def test_renames_the_mirror_and_installs_the_upstream(self):
        remote, err = self.configure()
        self.assertEqual(remote, "origin")
        self.assertEqual(self.calls, [
            ["git", "remote", "rename", "origin", "local"],
            ["git", "remote", "add", "origin", self.URL],
            ["git", "fetch", "--quiet", "origin"],
            ["git", "config", "checkout.defaultRemote", "origin"],
        ])
        self.assertEqual(err, "")

    def test_two_remotes_get_a_default_for_dwim_checkout(self):
        # Regression: with `local` and `origin` both carrying a master and
        # no local master (a clone whose HEAD was a topic branch), the
        # `git checkout master` inside git-safe-reset dies with "fatal:
        # 'master' matched multiple (2) remote tracking branches" -- which
        # is how a Parsoid clone silently skipped its reset. Reproduced,
        # and fixed by git's own suggested config.
        self.configure()
        self.assertIn(["git", "config", "checkout.defaultRemote", "origin"], self.calls)

    def test_the_dwim_default_follows_the_fallback(self):
        # Fetch failed, so safe-reset resets against `local`; the DWIM
        # branch must come from the same remote, not from an `origin` whose
        # refs never arrived.
        self.fail_on = "fetch"
        self.configure()
        self.assertEqual(self.calls[-1], ["git", "config", "checkout.defaultRemote", "local"])

    def test_without_an_upstream_the_mirror_keeps_the_origin_name(self):
        # Renaming would invent a `local` that means something different
        # here than in every other clone in the tree.
        remote, _err = self.configure(url=None)
        self.assertEqual(remote, "origin")
        self.assertEqual(self.calls, [])

    def test_an_offline_fetch_falls_back_to_the_mirror(self):
        self.fail_on = "fetch"
        remote, err = self.configure()
        self.assertEqual(remote, "local")
        self.assertIn("falling back to local", err)

    def test_a_failed_rename_leaves_origin_pointing_at_the_mirror(self):
        self.fail_on = "rename"
        remote, err = self.configure()
        self.assertEqual(remote, "origin")
        self.assertIn("host mirror", err)
        # Nothing added on top of a rename that didn't happen.
        self.assertEqual(len(self.calls), 1)


class StepCommandTests(unittest.TestCase):
    """git_safe_reset / composer_update / npm_install / install_mediawiki --
    what they run, and when they decline to run at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.calls = []
        self.code = 0

    def fake_run(self, argv, cwd=None):
        self.calls.append((argv, cwd))
        return FakeCompletedProcess(self.code)

    def touch(self, name, content=""):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
            f.write(content)

    def argv(self):
        # Drop the sudo prefix; the rest is the interesting part.
        return self.calls[0][0][4:]

    def test_safe_reset_forces_past_the_review_check(self):
        self.assertTrue(s.git_safe_reset(self.repo, run=self.fake_run))
        self.assertEqual(self.argv(), ["git", "safe-reset", "--force", "origin"])
        self.assertEqual(self.calls[0][1], self.repo)

    def test_safe_reset_names_its_remote_explicitly(self):
        # Never left to git-safe-reset's own `origin` default: which remote
        # is right depends on whether configure_remotes got the real
        # upstream installed.
        self.assertTrue(s.git_safe_reset(self.repo, remote="local", run=self.fake_run))
        self.assertEqual(self.argv(), ["git", "safe-reset", "--force", "local"])

    def test_safe_reset_reports_failure(self):
        self.code = 1
        self.assertFalse(s.git_safe_reset(self.repo, run=self.fake_run))

    def test_composer_update_is_skipped_without_a_composer_json(self):
        self.assertIsNone(s.composer_update(self.repo, run=self.fake_run))
        self.assertEqual(self.calls, [])

    def test_composer_update_runs_non_interactively(self):
        self.touch("composer.json", "{}")
        self.assertTrue(s.composer_update(self.repo, run=self.fake_run))
        # No tty in commands.install, and composer's progress bar renders
        # badly in the create log.
        self.assertEqual(self.argv(),
                         ["composer", "update", "--no-interaction", "--no-progress"])

    def test_npm_is_skipped_without_a_package_json(self):
        self.assertIsNone(s.npm_install(self.repo, run=self.fake_run))
        self.assertEqual(self.calls, [])

    def test_npm_ci_when_there_is_a_lockfile(self):
        self.touch("package.json", "{}")
        self.touch("package-lock.json", "{}")
        self.assertTrue(s.npm_install(self.repo, run=self.fake_run))
        # ci, not install: package-lock.json is tracked in core, and an
        # install that rewrites it leaves the clone dirty. The env prefix
        # keeps cypress from downloading an 800 MB binary at every create;
        # mw-install-cypress fetches it on demand (sbx/NOTES.md §91).
        self.assertEqual(
            self.argv(),
            ["env", "CYPRESS_INSTALL_BINARY=0", "npm", "ci", "--no-audit", "--no-fund"])

    def test_npm_install_when_there_is_no_lockfile(self):
        self.touch("package.json", "{}")
        self.assertTrue(s.npm_install(self.repo, run=self.fake_run))
        self.assertEqual(
            self.argv(),
            ["env", "CYPRESS_INSTALL_BINARY=0", "npm", "install", "--no-audit", "--no-fund"])

    def test_phpunit_config_is_skipped_without_a_composer_json(self):
        self.assertIsNone(s.phpunit_config(self.repo, run=self.fake_run))
        self.assertEqual(self.calls, [])

    def test_phpunit_config_runs_cores_own_script(self):
        # Core ships phpunit.xml.template, so a fresh checkout has no
        # config and `vendor/bin/phpunit <path>` loads no bootstrap
        # (sbx/DESIGN-testing-instructions.md §2.1).
        self.touch("composer.json", "{}")
        self.assertTrue(s.phpunit_config(self.repo, run=self.fake_run))
        self.assertEqual(self.argv(), ["composer", "phpunit:config"])
        self.assertEqual(self.calls[0][1], self.repo)

    def test_install_passes_with_extensions(self):
        self.assertTrue(s.install_mediawiki(self.repo, run=self.fake_run))
        # Without this flag CliInstaller enables every skin and no
        # extension, and the whole dependency walk buys nothing.
        self.assertEqual(self.argv(),
                         ["composer", "mw-install:sqlite", "--", "--with-extensions"])

    def test_install_is_skipped_when_localsettings_exists(self):
        self.touch("LocalSettings.php", "<?php")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(s.install_mediawiki(self.repo, run=self.fake_run))
        self.assertEqual(self.calls, [])
        self.assertIn("already exists", err.getvalue())


class LinkParsoidCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(self.core)
        self.parsoid_dest = os.path.join(self.tmp.name, "parsoid")

    def write_local_settings(self, body):
        path = os.path.join(self.core, "LocalSettings.php")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def test_replaces_the_wfloadextension_line(self):
        path = self.write_local_settings(
            "<?php\n"
            "$wgDefaultSkin = \"vector-2022\";\n\n"
            "// Enabled skins.\n"
            "wfLoadSkin( 'Vector' );\n\n"
            "wfLoadExtension( 'Parsoid' );\n"
            "wfLoadExtension( 'Cite' );\n"
        )
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(s.link_parsoid_checkout(self.core, self.parsoid_dest))
        with open(path, encoding="utf-8") as f:
            contents = f.read()
        # The original single-line call is gone from its old spot...
        self.assertNotIn("wfLoadExtension( 'Parsoid' );\n", contents)
        # ...replaced by a block pointing at this sandbox's own checkout...
        self.assertIn(f"$parsoidInstallDir = '{self.parsoid_dest}';", contents)
        self.assertIn(
            "wfLoadExtension( 'Parsoid', \"$parsoidInstallDir/extension.json\" );", contents
        )
        # ...with no dead vendor-vs-checkout guard left in...
        self.assertNotIn("vendor/wikimedia/parsoid", contents)
        # ...placed ahead of every skin and extension load, not where the
        # original call was...
        self.assertLess(
            contents.index("$parsoidInstallDir ="), contents.index("// Enabled skins.")
        )
        self.assertLess(contents.index("// Enabled skins."), contents.index("wfLoadSkin"))
        # ...and every other line is untouched, in place.
        self.assertIn("wfLoadSkin( 'Vector' );", contents)
        self.assertIn("wfLoadExtension( 'Cite' );", contents)
        self.assertIn(f"linked Parsoid checkout ({self.parsoid_dest})", err.getvalue())

    def test_escapes_a_single_quote_or_backslash_in_the_path(self):
        # Unusual, but the path is not under this function's control, and a
        # raw interpolation would produce a syntactically broken
        # LocalSettings.php.
        tricky = os.path.join(self.tmp.name, "weird'dir\\here")
        self.write_local_settings("// Enabled skins.\nwfLoadExtension( 'Parsoid' );\n")
        self.assertTrue(s.link_parsoid_checkout(self.core, tricky))
        with open(os.path.join(self.core, "LocalSettings.php"), encoding="utf-8") as f:
            contents = f.read()
        escaped = self.tmp.name + "/weird\\'dir\\\\here"
        self.assertIn(f"$parsoidInstallDir = '{escaped}';", contents)

    def test_missing_marker_line_warns_and_leaves_the_file_alone(self):
        original = "<?php\nwfLoadExtension( 'Cite' );\n"
        self.write_local_settings(original)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(s.link_parsoid_checkout(self.core, self.parsoid_dest))
        with open(os.path.join(self.core, "LocalSettings.php"), encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.assertIn("no", err.getvalue())
        self.assertIn("Parsoid checkout not linked", err.getvalue())

    def test_missing_skins_marker_warns_and_leaves_the_file_alone(self):
        # The extension line is there, but not the "// Enabled skins."
        # comment mw-install:sqlite is expected to also have written --
        # an unexpectedly-shaped LocalSettings.php, so refuse rather than
        # guess where "before every skin and extension" would even mean.
        original = "<?php\nwfLoadExtension( 'Parsoid' );\n"
        self.write_local_settings(original)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(s.link_parsoid_checkout(self.core, self.parsoid_dest))
        with open(os.path.join(self.core, "LocalSettings.php"), encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.assertIn("Parsoid checkout not linked", err.getvalue())

    def test_missing_file_warns_instead_of_raising(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(s.link_parsoid_checkout(self.core, self.parsoid_dest))
        self.assertIn("could not read", err.getvalue())


class ParseInstallParamsTests(unittest.TestCase):
    SCRIPT = ("@php maintenance/run.php install --server=http://localhost:4000 "
              "--dbtype sqlite --with-developmentsettings --dbpath cache/ "
              "--scriptpath= --pass adminpassword MediaWiki Admin")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def core(self, composer=None):
        path = os.path.join(self.tmp.name, "core")
        os.makedirs(path, exist_ok=True)
        if composer is not None:
            with open(os.path.join(path, "composer.json"), "w", encoding="utf-8") as f:
                f.write(composer)
        return path

    def test_reads_the_installs_own_parameters(self):
        core = self.core(json.dumps({"scripts": {"mw-install:sqlite": self.SCRIPT}}))
        params = s.parse_install_params(core)
        self.assertEqual(params["server"], "http://localhost:4000")
        self.assertEqual(params["scriptPath"], "")  # docroot install; valid
        self.assertEqual(params["adminPassword"], "adminpassword")

    def test_a_list_valued_script_is_joined(self):
        core = self.core(json.dumps({"scripts": {"mw-install:sqlite": [
            "@php maintenance/run.php install",
            "--server=http://localhost:9999 --pass hunter2",
        ]}}))
        self.assertEqual(s.parse_install_params(core)["server"], "http://localhost:9999")

    def test_space_separated_flags_are_read_too(self):
        core = self.core(json.dumps({"scripts": {
            "mw-install:sqlite": "install --server http://localhost:1234"}}))
        self.assertEqual(s.parse_install_params(core)["server"], "http://localhost:1234")

    def test_missing_composer_json_falls_back_to_the_plan(self):
        params = s.parse_install_params(self.core(), fallback={"adminUser": "Someone"})
        self.assertEqual(params["adminUser"], "Someone")
        self.assertEqual(params["server"], s.DEFAULT_INSTALL_PARAMS["server"])

    def test_unparseable_composer_json_falls_back_rather_than_raising(self):
        core = self.core("{not json")
        self.assertEqual(s.parse_install_params(core), s.DEFAULT_INSTALL_PARAMS)

    def test_a_core_without_the_script_falls_back(self):
        core = self.core(json.dumps({"scripts": {"test": "phpunit"}}))
        self.assertEqual(s.parse_install_params(core), s.DEFAULT_INSTALL_PARAMS)


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(self.core)
        self.path = os.path.join(self.core, ".env")
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def write(self, params=None):
        params = params or dict(s.DEFAULT_INSTALL_PARAMS)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            written = s.write_env_file(self.core, params, uid=1000, gid=1000,
                                       run=self.fake_run)
        return written, err.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_writes_the_installs_values_not_the_docker_ones(self):
        written, _err = self.write()
        self.assertTrue(written)
        # mediawiki-core-clean/.env says 8080 and /w and dockerpass; that
        # would point the harnesses at a port nothing listens on.
        self.assertIn("MW_SERVER=http://localhost:4000\n", self.read())
        self.assertIn("MEDIAWIKI_PASSWORD=adminpassword\n", self.read())

    def test_an_empty_script_path_is_written_as_a_slash(self):
        # The install's own --scriptpath= is empty, and wdio-mediawiki
        # rejects that value; Quibble writes `/` here too. See
        # sbx/DESIGN-testing-instructions.md §2.6.
        self.write()
        self.assertIn("MW_SCRIPT_PATH=/\n", self.read())

    def test_the_docker_port_follows_the_server_url(self):
        self.write({"server": "http://localhost:8080", "scriptPath": "/w"})
        self.assertIn("MW_DOCKER_PORT=8080\n", self.read())

    def test_uid_and_gid_are_not_hardcoded(self):
        with contextlib.redirect_stderr(io.StringIO()):
            s.write_env_file(self.core, dict(s.DEFAULT_INSTALL_PARAMS),
                             uid=4242, gid=4243, run=self.fake_run)
        self.assertIn("MW_DOCKER_UID=4242\n", self.read())
        self.assertIn("MW_DOCKER_GID=4243\n", self.read())

    def test_chowns_the_file_to_the_agent(self):
        self.write()
        self.assertEqual(self.calls, [["sudo", "chown", "agent:agent", self.path]])

    def test_rewriting_an_identical_file_is_a_no_op(self):
        self.write()
        self.calls.clear()
        written, _err = self.write()
        self.assertTrue(written)
        self.assertEqual(self.calls, [])

    def test_an_edited_env_is_left_alone(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("MW_SERVER=http://localhost:9999\n")
        written, err = self.write()
        self.assertFalse(written)
        self.assertIn("leaving it alone", err)
        self.assertEqual(self.read(), "MW_SERVER=http://localhost:9999\n")


class ComposerLocalTests(unittest.TestCase):
    """Core's composer.local.json -- quibble's merge of every extension's
    and skin's Composer dependencies into core's vendor/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(self.core)
        self.path = os.path.join(self.core, s.COMPOSER_LOCAL_NAME)
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def write(self, parsoid_checkout=False):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            written = s.write_composer_local(
                self.core, parsoid_checkout=parsoid_checkout, run=self.fake_run)
        return written, err.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    QUIBBLE = {"extra": {"merge-plugin": {"include": [
        "extensions/*/composer.json", "skins/*/composer.json"]}}}

    def test_the_file_holds_quibbles_globs(self):
        written, err = self.write()
        self.assertTrue(written)
        self.assertIn("wrote", err)
        self.assertEqual(json.loads(self.read()), self.QUIBBLE)
        self.assertIn(["sudo", "chown", "agent:agent", self.path], self.calls)

    def test_a_parsoid_checkout_adds_the_classmap_exclusion(self):
        # Ours, not quibble's: without it, every core composer update
        # prints 359 warnings for the Parsoid checkout. Without a
        # checkout, the wiki needs the vendor copy, so there is no
        # exclusion then.
        self.write(parsoid_checkout=True)
        self.assertEqual(
            json.loads(self.read()),
            dict(self.QUIBBLE, autoload={
                "exclude-from-classmap": ["vendor/wikimedia/parsoid/"]}),
        )

    def test_the_other_form_is_ours_and_is_replaced(self):
        for before, after in ((False, True), (True, False)):
            with self.subTest(before=before, after=after):
                self.write(parsoid_checkout=before)
                written, err = self.write(parsoid_checkout=after)
                self.assertTrue(written)
                self.assertNotIn("leaving it alone", err)
                self.assertEqual(self.read(), s.composer_local_contents(after))

    def test_the_file_is_indented_with_tabs(self):
        # Core's eslint lints every JSON file, as for the api-testing
        # config.
        self.write(parsoid_checkout=True)
        indented = [line for line in self.read().splitlines() if line[:1].isspace()]
        self.assertTrue(indented)
        for line in indented:
            self.assertTrue(line.startswith("\t"), repr(line))

    def test_our_own_file_is_left_as_it_is(self):
        self.write()
        self.calls.clear()
        written, err = self.write()
        self.assertTrue(written)
        self.assertEqual(err, "")
        self.assertEqual(self.calls, [])

    def test_an_engineers_own_file_is_left_alone(self):
        mine = '{"extra": {"merge-plugin": {"include": ["extensions/Cite/composer.json"]}}}\n'
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(mine)
        written, err = self.write()
        self.assertFalse(written)
        self.assertIn("leaving it alone", err)
        self.assertIn("phan", err)
        self.assertEqual(self.read(), mine)


class ApiTestingConfigTests(unittest.TestCase):
    """sbx/DESIGN-testing-instructions.md §5.5 -- the config the
    api-testing suites need, written into core at create time."""

    SECRET = "22d812ebf36429193b72d9d54adcb2e62fba7b37658b69ed0f3aeabf88700b23"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = os.path.join(self.tmp.name, "core")
        os.makedirs(self.core)
        self.path = os.path.join(self.core, s.API_TESTING_CONFIG_NAME)
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def local_settings(self, body=None):
        if body is None:
            body = f'<?php\n$wgSecretKey = "{self.SECRET}";\n'
        with open(os.path.join(self.core, "LocalSettings.php"), "w",
                  encoding="utf-8") as f:
            f.write(body)

    def write(self, params=None):
        params = params or dict(s.DEFAULT_INSTALL_PARAMS)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            written = s.write_api_testing_config(self.core, params, run=self.fake_run)
        return written, err.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_the_secret_key_comes_from_localsettings(self):
        self.local_settings()
        self.assertEqual(s.read_secret_key(self.core), self.SECRET)

    def test_single_quotes_read_the_same(self):
        self.local_settings(f"<?php\n$wgSecretKey = '{self.SECRET}';\n")
        self.assertEqual(s.read_secret_key(self.core), self.SECRET)

    def test_no_localsettings_means_no_key(self):
        self.assertIsNone(s.read_secret_key(self.core))

    def test_the_config_names_every_field_the_library_reads(self):
        self.local_settings()
        written, _err = self.write()
        self.assertTrue(written)
        config = self.read()
        # api-testing/lib/config.js strips the trailing slash and appends
        # `api.php` itself, and this is a docroot wiki, so the server root
        # is the whole base_uri.
        self.assertEqual(config["base_uri"], "http://localhost:4000/")
        self.assertEqual(config["main_page"], s.API_TESTING_MAIN_PAGE)
        self.assertEqual(config["root_user"],
                         {"name": "Admin", "password": "adminpassword"})
        self.assertEqual(config["secret_key"], self.SECRET)

    def test_the_config_is_indented_with_tabs(self):
        # Core's eslint lints this file in `npm test`. Its `indent` rule
        # rejects spaces, so a space-indented config fails core's lint on
        # a clean checkout. Found by the blind acceptance run (§9).
        self.local_settings()
        self.write()
        with open(self.path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        indented = [line for line in lines if line[:1].isspace()]
        self.assertTrue(indented)
        for line in indented:
            self.assertTrue(line.startswith("\t"), repr(line))
            self.assertNotIn(" ", line[:len(line) - len(line.lstrip())])

    def test_the_old_space_indented_form_is_replaced(self):
        # Sandboxes made before the tab fix hold the four-space form. It is
        # our own file, not an engineer's edit, so resume rewrites it.
        self.local_settings()
        params = dict(s.DEFAULT_INSTALL_PARAMS)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(s.api_testing_config_contents(params, self.SECRET, indent=4))
        written, err = self.write(params)
        self.assertTrue(written)
        self.assertEqual(err, "")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(
                f.read(), s.api_testing_config_contents(params, self.SECRET))

    def test_the_values_are_the_installs_own(self):
        self.local_settings()
        self.write({"server": "http://localhost:8080", "scriptPath": "/w",
                    "adminUser": "Root", "adminPassword": "hunter2"})
        config = self.read()
        self.assertEqual(config["base_uri"], "http://localhost:8080/")
        self.assertEqual(config["root_user"],
                         {"name": "Root", "password": "hunter2"})

    def test_a_wiki_without_a_secret_key_gets_no_config(self):
        # Worse than no file: a config with the wrong key fails at login,
        # with nothing in the message to point at the cause.
        self.local_settings("<?php\n// no secret key here\n")
        written, err = self.write()
        self.assertFalse(written)
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("could not read $wgSecretKey", err)

    def test_chowns_the_file_to_the_agent(self):
        self.local_settings()
        self.write()
        self.assertEqual(self.calls, [["sudo", "chown", "agent:agent", self.path]])

    def test_an_edited_config_is_left_alone(self):
        self.local_settings()
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"base_uri": "http://example.test/"}')
        written, err = self.write()
        self.assertFalse(written)
        self.assertIn("leaving it alone", err)
        self.assertEqual(self.read()["base_uri"], "http://example.test/")


class MainPlanFileTests(unittest.TestCase):
    """main() driven by a plan file rather than positionally."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host_home = os.path.join(self.tmp.name, "home")
        self.sandbox_home = os.path.join(self.tmp.name, "sandbox")
        os.makedirs(self.sandbox_home)
        self.originals = os.path.join(self.sandbox_home, ".sbx-originals")
        # ORIGINALS_DIR alongside SANDBOX_HOME: run_setup writes the layout
        # there, and the "alias" path makedirs into it. Neither belongs in
        # the real /home/agent of whatever machine runs the tests.
        patcher = unittest.mock.patch.multiple(
            s, SANDBOX_HOME=self.sandbox_home, ORIGINALS_DIR=self.originals,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Empty unless a test fills it -- otherwise main() finds the real
        # ~/bin of whatever machine is running the tests.
        self.helper_bin = os.path.join(self.sandbox_home, "bin")
        os.makedirs(self.helper_bin)
        patcher = unittest.mock.patch.object(s, "HELPER_SOURCE_DIR", self.helper_bin)
        patcher.start()
        self.addCleanup(patcher.stop)

    def host_repo(self, *parts):
        path = os.path.join(self.host_home, *parts)
        os.makedirs(path)
        return path

    def sandbox_repo(self, *parts):
        """Stand in for the clone wmf-sbx-setup would have made -- `run` is
        faked here, so nothing actually clones."""
        path = os.path.join(self.sandbox_home, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def write_plan(self, repos, **kw):
        plan = {
            "version": 1, "hostHome": self.host_home, "daemonPort": "9418",
            "primary": repos[0]["path"] if repos else None, "repos": repos,
        }
        plan.update(kw)
        path = os.path.join(self.tmp.name, "plan.json")
        with open(path, "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(plan, f)
        return path

    def run_main(self, path, returncodes=None):
        """returncodes: {first-word-after-the-sudo-prefix: exit code} for
        the steps that should fail; everything else succeeds."""
        self.calls = []

        def fake_run(argv, cwd=None):
            self.calls.append((argv, cwd))
            for word, code in (returncodes or {}).items():
                if word in argv:
                    return FakeCompletedProcess(code)
            return FakeCompletedProcess(0)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = s.main([path], run=fake_run, popen=lambda *a, **kw: None,
                          log_dir=None)
        return code, err.getvalue()

    def test_links_extensions_into_the_core_clone(self):
        core = self.host_repo("Wikimedia", "core")
        cite = self.host_repo("Wikimedia", "Extensions", "Cite")
        self.sandbox_repo("Wikimedia", "core", "extensions")
        cite_clone = self.sandbox_repo("Wikimedia", "Extensions", "Cite")
        path = self.write_plan([
            {"path": cite, "canonical": "gerrit:mediawiki/extensions/Cite",
             "readOnly": False, "linkName": "Cite", "linkDir": "extensions"},
            {"path": core, "canonical": "gerrit:mediawiki/core",
             "readOnly": False, "linkName": None, "linkDir": None},
        ])
        code, err = self.run_main(path)
        self.assertEqual(code, 0)
        link = os.path.join(self.sandbox_home, "Wikimedia", "core", "extensions", "Cite")
        self.assertEqual(os.readlink(link), cite_clone)
        self.assertIn("linked extensions/Cite ->", err)

    def test_core_declared_read_only_gets_no_links(self):
        core = self.host_repo("Wikimedia", "core")
        cite = self.host_repo("Wikimedia", "Extensions", "Cite")
        self.sandbox_repo("Wikimedia", "core")
        self.sandbox_repo("Wikimedia", "Extensions", "Cite")
        path = self.write_plan([
            {"path": cite, "canonical": "gerrit:mediawiki/extensions/Cite",
             "readOnly": False, "linkName": "Cite", "linkDir": "extensions"},
            {"path": core, "canonical": "gerrit:mediawiki/core",
             "readOnly": True, "linkName": None, "linkDir": None},
        ])
        code, err = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertIn("read-only bind mount", err)

    def test_the_shared_skills_store_is_locked_before_any_repo_work(self):
        # The one mount in the sandbox that is shared with *other*
        # sandboxes, so the window before it is read-only is the one worth
        # keeping short (SECURITY.md §7.6). Mocked rather than driven
        # through mountinfo: lock_shared_skills has its own tests, and
        # faking a mount point here would fight setup_repo's ismount
        # checks for the repos.
        core = self.host_repo("Wikimedia", "core")
        self.sandbox_repo("Wikimedia", "core")
        path = self.write_plan([
            {"path": core, "canonical": "gerrit:mediawiki/core",
             "readOnly": False, "linkName": None, "linkDir": None},
        ])
        with unittest.mock.patch.object(s, "lock_shared_skills") as lock:
            code, _err = self.run_main(path)
        self.assertEqual(code, 0)
        lock.assert_called_once()

    def test_a_bad_plan_stops_before_touching_anything(self):
        path = os.path.join(self.tmp.name, "plan.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"version": 2, "hostHome": "/h", "daemonPort": "1", "repos": []}')
        code, err = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn("error:", err)


class MainSetupChainTests(MainPlanFileTests):
    """The whole in-sandbox chain, driven through main() -- ordering,
    which repos each step touches, and what's fatal. See
    sbx/DESIGN-setup-steps.md §8."""

    def plan_with_cite_and_core(self, primary=None, core_files=(),
                                core_requested=False, **plan_kw):
        """Cite is the primary; core is a dependency. Both get clones.

        core_requested stands in for `wmf-sbx-create Cite core` -- core
        named on the command line rather than found by the dependency
        walk."""
        core = self.host_repo("Wikimedia", "core")
        cite = self.host_repo("Wikimedia", "Extensions", "Cite")
        core_clone = self.sandbox_repo("Wikimedia", "core")
        self.sandbox_repo("Wikimedia", "core", "extensions")
        cite_clone = self.sandbox_repo("Wikimedia", "Extensions", "Cite")
        for name, content in core_files:
            with open(os.path.join(core_clone, name), "w", encoding="utf-8") as f:
                f.write(content)
        path = self.write_plan([
            {"path": cite, "canonical": "gerrit:mediawiki/extensions/Cite",
             "readOnly": False, "linkName": "Cite", "linkDir": "extensions",
             "requested": True},
            {"path": core, "canonical": "gerrit:mediawiki/core",
             "readOnly": False, "linkName": None, "linkDir": None,
             "requested": core_requested},
        ], primary=primary or cite, **plan_kw)
        return path, core_clone, cite_clone

    def commands(self):
        """Each call's argv with the `sudo -u agent -H` prefix stripped."""
        out = []
        for argv, cwd in self.calls:
            if argv[:4] == ["sudo", "-u", "agent", "-H"]:
                out.append((argv[4:], cwd))
        return out

    def test_the_primary_is_composer_updated_but_never_safe_reset(self):
        path, core_clone, cite_clone = self.plan_with_cite_and_core(
            core_files=[("composer.json", "{}")])
        with open(os.path.join(cite_clone, "composer.json"), "w") as f:
            f.write("{}")
        code, _err = self.run_main(path)
        self.assertEqual(code, 0)
        reset_dirs = [cwd for argv, cwd in self.commands() if argv[:2] == ["git", "safe-reset"]]
        update_dirs = [cwd for argv, cwd in self.commands() if argv[:2] == ["composer", "update"]]
        # Cite is the primary: the host's copy is assumed to hold work in
        # progress this sandbox exists to continue.
        self.assertEqual(reset_dirs, [core_clone])
        # composer update touches only vendor/, so it has no reason to skip
        # anything -- and when the primary IS core, skipping it would leave
        # no vendor/ and the install would fail.
        self.assertEqual(sorted(update_dirs), sorted([core_clone, cite_clone]))

    def test_core_is_composer_updated_after_the_links_and_the_merge_file(self):
        # The merge globs find the extensions through the links, so both
        # must exist when core's composer update runs.
        path, core_clone, cite_clone = self.plan_with_cite_and_core(
            core_files=[("composer.json", "{}")])
        seen = []

        def fake_run(argv, cwd=None):
            if "composer" in argv and "update" in argv and cwd == core_clone:
                seen.append((
                    os.path.islink(os.path.join(core_clone, "extensions", "Cite")),
                    os.path.exists(os.path.join(core_clone, s.COMPOSER_LOCAL_NAME)),
                ))
            return FakeCompletedProcess(0)

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = s.main([path], run=fake_run, popen=lambda *a, **kw: None)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(True, True)])

    def test_a_repo_named_on_the_command_line_is_not_safe_reset(self):
        # Naming a repo is how you say "this is what I'm here to work on",
        # so its clone keeps the host's branch -- not just the primary's.
        path, _core, _cite = self.plan_with_cite_and_core(core_requested=True)
        code, err = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertEqual(
            [cwd for argv, cwd in self.commands() if argv[:2] == ["git", "safe-reset"]], []
        )
        self.assertIn("named on the command line", err)

    def test_reset_all_resets_the_primary_too(self):
        path, core_clone, cite_clone = self.plan_with_cite_and_core(
            core_requested=True, resetAll=True)
        code, _err = self.run_main(path)
        self.assertEqual(code, 0)
        reset_dirs = [cwd for argv, cwd in self.commands() if argv[:2] == ["git", "safe-reset"]]
        # "All" that exempted the primary would be a trap.
        self.assertEqual(sorted(reset_dirs), sorted([core_clone, cite_clone]))

    def test_the_daemon_starts_before_the_slow_mediawiki_phase(self):
        path, _core, _cite = self.plan_with_cite_and_core()
        started = []
        out, err = io.StringIO(), io.StringIO()
        calls = []

        def fake_run(argv, cwd=None):
            calls.append(argv)
            return FakeCompletedProcess(0)

        def fake_popen(argv, **kw):
            started.append(len(calls))

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            s.main([path], run=fake_run, popen=fake_popen)
        after = [c for c in calls[started[0]:] if "composer" in c or "npm" in c]
        # The host fetches through the daemon; no reason to make that wait
        # on composer and npm.
        self.assertTrue(after, "no MediaWiki steps ran after the daemon started")

    def test_a_failed_dependency_step_warns_and_the_sandbox_still_comes_up(self):
        path, _core, _cite = self.plan_with_cite_and_core()
        code, err = self.run_main(path, returncodes={"safe-reset": 1})
        self.assertEqual(code, 0)
        self.assertIn("git safe-reset origin failed", err)
        self.assertIn("1 step(s) did not succeed", err)

    def test_a_failed_install_fails_the_sandbox(self):
        path, _core, _cite = self.plan_with_cite_and_core()
        code, err = self.run_main(path, returncodes={"mw-install:sqlite": 1})
        # A sandbox without a working wiki is the thing this was all for.
        self.assertEqual(code, 1)
        self.assertIn("mw-install:sqlite failed", err)

    def test_a_failed_npm_fails_the_sandbox(self):
        path, core_clone, _cite = self.plan_with_cite_and_core(
            core_files=[("package.json", "{}")])
        code, err = self.run_main(path, returncodes={"npm": 1})
        self.assertEqual(code, 1)
        self.assertIn("npm install failed", err)

    def test_every_clone_with_a_package_json_gets_an_npm_install(self):
        # sbx/DESIGN-testing-instructions.md §5.1: an extension's own JS
        # tests, linters and selenium harness come from its own
        # node_modules, so core alone is not enough.
        path, core_clone, cite_clone = self.plan_with_cite_and_core(
            core_files=[("package.json", "{}")])
        with open(os.path.join(cite_clone, "package.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        code, _err = self.run_main(path)
        self.assertEqual(code, 0)
        npm_dirs = [cwd for argv, cwd in self.commands() if "npm" in argv]
        self.assertEqual(sorted(npm_dirs), sorted([core_clone, cite_clone]))

    def test_a_failed_npm_in_a_dependency_only_warns(self):
        # Core's npm is fatal (the test above), but one extension's JS
        # deps should not cost the engineer the whole sandbox. Core has no
        # package.json here, so only Cite's install runs.
        path, _core, cite_clone = self.plan_with_cite_and_core()
        with open(os.path.join(cite_clone, "package.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        code, err = self.run_main(path, returncodes={"npm": 1})
        self.assertEqual(code, 0)
        self.assertIn(f"npm install failed in {cite_clone}", err)

    def test_the_test_configs_are_written_after_the_install(self):
        # §5.2 and §5.5: phpunit.xml and the api-testing config both need
        # a wiki that exists -- the second reads $wgSecretKey out of it.
        secret = "b" * 64
        path, core_clone, _cite = self.plan_with_cite_and_core(core_files=[
            ("composer.json", json.dumps({"scripts": {
                "mw-install:sqlite": "install --server=http://localhost:4000 --scriptpath="}})),
            ("LocalSettings.php", f'<?php\n$wgSecretKey = "{secret}";\n'),
        ])
        code, _err = self.run_main(path)
        self.assertEqual(code, 0)
        commands = self.commands()
        config_at = [i for i, (argv, cwd) in enumerate(commands)
                     if argv == ["composer", "phpunit:config"] and cwd == core_clone]
        install_at = [i for i, (argv, _cwd) in enumerate(commands)
                      if "mw-install:sqlite" in argv]
        self.assertEqual(len(config_at), 1)
        # The install is skipped here (LocalSettings.php is already
        # there), so this asserts the generator runs either way: a resumed
        # sandbox needs the config as much as a fresh one.
        self.assertEqual(install_at, [])
        with open(os.path.join(core_clone, s.API_TESTING_CONFIG_NAME),
                  encoding="utf-8") as f:
            config = json.load(f)
        self.assertEqual(config["secret_key"], secret)
        self.assertEqual(config["base_uri"], "http://localhost:4000/")

    def test_the_env_file_lands_in_the_core_clone(self):
        path, core_clone, _cite = self.plan_with_cite_and_core(core_files=[
            ("composer.json", json.dumps({"scripts": {
                "mw-install:sqlite": "install --server=http://localhost:4000 --scriptpath="}})),
        ])
        code, _err = self.run_main(path)
        self.assertEqual(code, 0)
        with open(os.path.join(core_clone, ".env"), encoding="utf-8") as f:
            self.assertIn("MW_SERVER=http://localhost:4000", f.read())

    def test_a_read_only_core_skips_the_whole_chain(self):
        core = self.host_repo("Wikimedia", "core")
        cite = self.host_repo("Wikimedia", "Extensions", "Cite")
        self.sandbox_repo("Wikimedia", "core")
        self.sandbox_repo("Wikimedia", "Extensions", "Cite")
        path = self.write_plan([
            {"path": cite, "canonical": "gerrit:mediawiki/extensions/Cite",
             "readOnly": False, "linkName": "Cite", "linkDir": "extensions"},
            {"path": core, "canonical": "gerrit:mediawiki/core",
             "readOnly": True, "linkName": None, "linkDir": None},
        ])
        code, err = self.run_main(path)
        self.assertEqual(code, 0)
        # Nothing to link into and no wiki to install, so resetting and
        # composer-updating the dependencies would be minutes for nothing.
        self.assertEqual(self.commands(), [])
        self.assertIn("skipping the MediaWiki setup", err)

    def test_no_core_at_all_skips_the_chain_and_says_why(self):
        cite = self.host_repo("Wikimedia", "Extensions", "Cite")
        self.sandbox_repo("Wikimedia", "Extensions", "Cite")
        path = self.write_plan([
            {"path": cite, "canonical": "gerrit:mediawiki/extensions/Cite",
             "readOnly": False, "linkName": "Cite", "linkDir": "extensions"},
        ])
        code, err = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertEqual(self.commands(), [])
        self.assertIn("no mediawiki/core clone", err)

    def test_the_helper_scripts_go_onto_path_before_anything_uses_them(self):
        for name in s.HELPER_SCRIPTS:
            open(os.path.join(self.helper_bin, name), "w").close()
        path, _core, _cite = self.plan_with_cite_and_core()
        self.run_main(path)
        argvs = [argv for argv, _cwd in self.calls]
        installed = [i for i, argv in enumerate(argvs) if argv[0] == "install"]
        reset = [i for i, argv in enumerate(argvs) if "safe-reset" in argv]
        self.assertEqual(len(installed), len(s.HELPER_SCRIPTS))
        # `git safe-reset` only resolves as a git subcommand once
        # git-safe-reset is on PATH, and it calls git-review-check by name.
        self.assertLess(max(installed), min(reset))

    def test_positional_invocation_still_runs_the_chain(self):
        core = self.host_repo("Wikimedia", "core")
        self.sandbox_repo("Wikimedia", "core")
        out, err = io.StringIO(), io.StringIO()
        self.calls = []

        def fake_run(argv, cwd=None):
            self.calls.append((argv, cwd))
            return FakeCompletedProcess(0)

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = s.main([self.host_home, "9418", core], run=fake_run,
                          popen=lambda *a, **kw: None)
        self.assertEqual(code, 0)
        # No canonicals in the positional form, so nothing is recognised as
        # core and the MediaWiki chain is skipped -- see plan_from_argv.
        self.assertIn("no mediawiki/core clone", err.getvalue())

    def run_main_with_fake_install(self, path, core_clone):
        """Like run_main, but the faked mw-install:sqlite actually writes a
        LocalSettings.php with the line install_mediawiki's real composer
        script emits -- needed to test link_parsoid_checkout's wiring,
        which only fires once that file is genuinely there to patch."""
        self.calls = []

        def fake_run(argv, cwd=None):
            self.calls.append((argv, cwd))
            if "mw-install:sqlite" in argv:
                with open(os.path.join(core_clone, "LocalSettings.php"), "w",
                          encoding="utf-8") as f:
                    f.write(
                        "// Enabled skins.\nwfLoadSkin( 'Vector' );\n\n"
                        "wfLoadExtension( 'Parsoid' );\n"
                    )
            return FakeCompletedProcess(0)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = s.main([path], run=fake_run, popen=lambda *a, **kw: None, log_dir=None)
        return code, err.getvalue()

    def test_a_writable_parsoid_clone_gets_linked_into_localsettings(self):
        core = self.host_repo("Wikimedia", "core")
        parsoid = self.host_repo("Wikimedia", "Parsoid")
        core_clone = self.sandbox_repo("Wikimedia", "core")
        parsoid_clone = self.sandbox_repo("Wikimedia", "Parsoid")
        path = self.write_plan([
            {"path": core, "canonical": "gerrit:mediawiki/core",
             "readOnly": False, "linkName": None, "linkDir": None},
            {"path": parsoid, "canonical": "gerrit:mediawiki/services/parsoid",
             "readOnly": False, "linkName": None, "linkDir": None},
        ])
        code, err = self.run_main_with_fake_install(path, core_clone)
        self.assertEqual(code, 0)
        with open(os.path.join(core_clone, "LocalSettings.php"), encoding="utf-8") as f:
            contents = f.read()
        # Nothing is really mounted here, so move_original declines and
        # every repo lands in mode "clone" -- the fallback where the alias
        # did *not* take and the literal path is still the read-only host
        # mirror. work_path therefore picks the parallel clone, which is
        # the only writable copy. The alias case is
        # test_the_alias_mode_works_in_the_literal_path below.
        self.assertIn(f"$parsoidInstallDir = '{parsoid_clone}';", contents)
        self.assertIn(f"linked Parsoid checkout ({parsoid_clone})", err)

    def test_only_a_parsoid_clone_excludes_the_vendor_copy_from_the_classmap(self):
        # Without a Parsoid clone, the wiki loads Parsoid from core's
        # vendor/, so the vendor copy must stay in the classmap.
        for with_parsoid in (True, False):
            with self.subTest(with_parsoid=with_parsoid):
                name = "with" if with_parsoid else "without"
                core = self.host_repo(name, "core")
                core_clone = self.sandbox_repo(name, "core")
                repos = [{"path": core, "canonical": "gerrit:mediawiki/core",
                          "readOnly": False, "linkName": None, "linkDir": None}]
                if with_parsoid:
                    parsoid = self.host_repo(name, "Parsoid")
                    self.sandbox_repo(name, "Parsoid")
                    repos.append({"path": parsoid,
                                  "canonical": "gerrit:mediawiki/services/parsoid",
                                  "readOnly": False, "linkName": None, "linkDir": None})
                path = self.write_plan(repos)
                code, _err = self.run_main_with_fake_install(path, core_clone)
                self.assertEqual(code, 0)
                with open(os.path.join(core_clone, s.COMPOSER_LOCAL_NAME),
                          encoding="utf-8") as f:
                    self.assertEqual(f.read(), s.composer_local_contents(with_parsoid))

    def test_the_alias_mode_works_in_the_literal_path(self):
        """The point of sbx/NOTES.md §63.1: once setup_repo has mounted the
        clone over the host path, composer, npm and the installer all run
        there, and what they write names that path rather than
        /home/agent."""
        core = self.host_repo("Wikimedia", "core")
        parsoid = self.host_repo("Wikimedia", "Parsoid")
        self.sandbox_repo("Wikimedia", "core")
        self.sandbox_repo("Wikimedia", "Parsoid")
        # In "alias" mode the steps run against the literal paths, so the
        # files that gate them have to be there and not in the clone.
        for repo in (core, parsoid):
            with open(os.path.join(repo, "composer.json"), "w", encoding="utf-8") as f:
                f.write("{}")
        with open(os.path.join(core, "package.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        path = self.write_plan([
            {"path": core, "canonical": "gerrit:mediawiki/core",
             "readOnly": False, "linkName": None, "linkDir": None},
            {"path": parsoid, "canonical": "gerrit:mediawiki/services/parsoid",
             "readOnly": False, "linkName": None, "linkDir": None},
        ])
        # Both host repos look like sbx mounts, so move_original goes
        # ahead and bind_over succeeds -- mode "alias" for both.
        with unittest.mock.patch(
            "os.path.ismount", side_effect=lambda p: p in {core, parsoid}
        ):
            code, err = self.run_main_with_fake_install(path, core)
        self.assertEqual(code, 0)
        cwds = [cwd for argv, cwd in self.commands()
                if argv[:1] in (["composer"], ["npm"])]
        self.assertTrue(cwds)
        # Not one /home/agent path among them.
        self.assertEqual(set(cwds), {core, parsoid})
        with open(os.path.join(core, "LocalSettings.php"), encoding="utf-8") as f:
            contents = f.read()
        self.assertIn(f"$parsoidInstallDir = '{parsoid}';", contents)
        self.assertIn(f"linked Parsoid checkout ({parsoid})", err)

    def test_a_readonly_parsoid_bind_mount_is_not_linked(self):
        # Only a writable clone is "this sandbox's own checkout" -- a :ro
        # mount is the host's, same as core's own read-only case.
        core = self.host_repo("Wikimedia", "core")
        parsoid = self.host_repo("Wikimedia", "Parsoid")
        core_clone = self.sandbox_repo("Wikimedia", "core")
        self.sandbox_repo("Wikimedia", "Parsoid")
        path = self.write_plan([
            {"path": core, "canonical": "gerrit:mediawiki/core",
             "readOnly": False, "linkName": None, "linkDir": None},
            {"path": parsoid, "canonical": "gerrit:mediawiki/services/parsoid",
             "readOnly": True, "linkName": None, "linkDir": None},
        ])
        code, err = self.run_main_with_fake_install(path, core_clone)
        self.assertEqual(code, 0)
        with open(os.path.join(core_clone, "LocalSettings.php"), encoding="utf-8") as f:
            contents = f.read()
        self.assertIn("wfLoadExtension( 'Parsoid' );", contents)
        self.assertNotIn("parsoidInstallDir", contents)
        self.assertNotIn("linked Parsoid checkout", err)

    def test_no_parsoid_in_the_plan_is_a_no_op(self):
        path, core_clone, _cite = self.plan_with_cite_and_core()
        code, err = self.run_main_with_fake_install(path, core_clone)
        self.assertEqual(code, 0)
        self.assertNotIn("linked Parsoid checkout", err)
        self.assertNotIn("could not link the Parsoid checkout", err)


class DeepMergeTests(unittest.TestCase):
    def test_dicts_merge_key_by_key(self):
        self.assertEqual(
            s.deep_merge({"a": 1, "n": {"x": 1}}, {"b": 2, "n": {"y": 2}}),
            {"a": 1, "b": 2, "n": {"x": 1, "y": 2}},
        )

    def test_lists_are_unioned_not_replaced(self):
        # permissions.deny is the only list the kit patches, and replacing
        # would silently drop a deny the engineer or a later sbx added.
        self.assertEqual(
            s.deep_merge({"deny": ["Bash(ssh:*)"]}, {"deny": ["mcp__mcp-gateway"]}),
            {"deny": ["Bash(ssh:*)", "mcp__mcp-gateway"]},
        )

    def test_a_repeated_list_entry_is_not_duplicated(self):
        self.assertEqual(s.deep_merge({"d": ["a"]}, {"d": ["a", "b"]}), {"d": ["a", "b"]})

    def test_scalars_are_replaced_and_the_input_is_not_mutated(self):
        base = {"model": "old", "n": {"k": 1}}
        self.assertEqual(s.deep_merge(base, {"model": "new"})["model"], "new")
        self.assertEqual(base, {"model": "old", "n": {"k": 1}})


class MergeSettingsTests(unittest.TestCase):
    PATCH = {"enabledPlugins": {"wmf-claude@wikimedia": True},
             "permissions": {"deny": ["Bash(ssh:*)"]}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, ".claude", "settings.json")
        self.calls = []

    def fake_run(self, argv, cwd=None):
        self.calls.append(argv)
        return FakeCompletedProcess(0)

    def write(self, content):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))

    def merge(self, patch=None):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ok = s.merge_settings(self.path, self.PATCH if patch is None else patch,
                                  run=self.fake_run)
        return ok, err.getvalue()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_creates_the_file_when_there_is_none(self):
        ok, _err = self.merge()
        self.assertTrue(ok)
        self.assertEqual(self.read(), self.PATCH)

    def test_keeps_what_sbx_wrote(self):
        # sbx writes its own settings.json; losing defaultMode would leave
        # the agent asking for approval it has no terminal to give.
        self.write({"permissions": {"defaultMode": "bypassPermissions"},
                    "model": "opus"})
        ok, _err = self.merge()
        self.assertTrue(ok)
        merged = self.read()
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["permissions"]["defaultMode"], "bypassPermissions")
        self.assertEqual(merged["permissions"]["deny"], ["Bash(ssh:*)"])
        self.assertTrue(merged["enabledPlugins"]["wmf-claude@wikimedia"])

    def test_it_is_idempotent_and_the_second_run_does_not_rewrite(self):
        # It runs at install and again on every container start.
        self.merge()
        before = os.stat(self.path).st_mtime_ns
        ok, err = self.merge()
        self.assertTrue(ok)
        self.assertIn("already has", err)
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_it_chowns_because_the_install_run_is_root(self):
        self.merge()
        self.assertEqual(self.calls, [["sudo", "chown", "agent:agent", self.path]])

    def test_unparseable_settings_are_left_alone_and_reported(self):
        self.write("{not json")
        ok, err = self.merge()
        self.assertFalse(ok)
        self.assertIn("could not read", err)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_a_settings_file_that_is_not_an_object_is_left_alone(self):
        self.write("[1, 2]")
        ok, err = self.merge()
        self.assertFalse(ok)
        self.assertIn("not a JSON object", err)


class RunSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.tmp.name, "settings.json")

    def patch_file(self, content):
        path = os.path.join(self.tmp.name, "patch.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        return path

    def run_settings(self, argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = s.run_settings(argv, run=lambda a, cwd=None: FakeCompletedProcess(0))
        return code, err.getvalue()

    def test_merges_the_patch_into_the_named_target(self):
        patch = self.patch_file({"enabledPlugins": {"wmf-claude@wikimedia": True}})
        code, _err = self.run_settings([patch, self.target])
        self.assertEqual(code, 0)
        with open(self.target, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["enabledPlugins"]["wmf-claude@wikimedia"])

    def test_the_target_defaults_to_the_agents_settings_json(self):
        self.assertEqual(s.SANDBOX_SETTINGS_FILE,
                         os.path.join(s.SANDBOX_HOME, ".claude", "settings.json"))

    def test_no_patch_path_is_an_error(self):
        code, err = self.run_settings([])
        self.assertEqual(code, 1)
        self.assertIn("JSON patch file", err)

    def test_an_unreadable_or_malformed_patch_fails_loudly(self):
        code, err = self.run_settings(["/nonexistent/patch.json", self.target])
        self.assertEqual(code, 1)
        self.assertIn("could not read", err)
        code, err = self.run_settings([self.patch_file("[]"), self.target])
        self.assertEqual(code, 1)
        self.assertIn("not a JSON object", err)

    def test_main_dispatches_settings(self):
        patch = self.patch_file({"permissions": {"deny": ["Bash(ssh:*)"]}})
        with contextlib.redirect_stderr(io.StringIO()):
            code = s.main(["--settings", patch, self.target],
                          run=lambda a, cwd=None: FakeCompletedProcess(0))
        self.assertEqual(code, 0)
        with open(self.target, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["permissions"]["deny"], ["Bash(ssh:*)"])


class RunExecBitsTests(unittest.TestCase):
    """`wmf-sbx-setup --exec-bits` -- the fix for a kit drop that loses the
    executable bit and takes the SessionStart hook with it (§71)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def script(self, name, mode=0o644):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n")
        os.chmod(path, mode)
        return path

    def run_exec_bits(self, argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = s.run_exec_bits(argv)
        return code, err.getvalue()

    def test_it_makes_the_kits_programs_executable(self):
        path = self.script("session-start.sh")
        code, err = self.run_exec_bits([path])
        self.assertEqual(code, 0)
        self.assertTrue(os.access(path, os.X_OK))
        self.assertIn("was 0644", err)
        self.assertIn("1 restored, 0 already executable", err)

    def test_a_file_that_is_already_executable_is_left_alone(self):
        path = self.script("proxy", mode=0o755)
        code, err = self.run_exec_bits([path])
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755)
        self.assertIn("0 restored, 1 already executable", err)

    def test_a_missing_path_warns_and_does_not_fail_the_create(self):
        # A --no-mcp kit ships no proxy, and a create is not worth failing
        # over a chmod.
        code, err = self.run_exec_bits([os.path.join(self.tmp.name, "gone")])
        self.assertEqual(code, 0)
        self.assertIn("not in this sandbox", err)

    def test_a_chmod_that_fails_is_an_error(self):
        path = self.script("session-start.sh")
        with mock.patch.object(os, "chmod", side_effect=OSError("nope")):
            code, err = self.run_exec_bits([path])
        self.assertEqual(code, 1)
        self.assertIn("could not make", err)

    def test_no_paths_is_an_error(self):
        code, err = self.run_exec_bits([])
        self.assertEqual(code, 1)
        self.assertIn("at least one path", err)

    def test_main_dispatches_exec_bits(self):
        path = self.script("session-start.sh")
        with contextlib.redirect_stderr(io.StringIO()):
            code = s.main(["--exec-bits", path], log_dir=None)
        self.assertEqual(code, 0)
        self.assertTrue(os.access(path, os.X_OK))


class RegisterMcpServersTests(unittest.TestCase):
    """`wmf-sbx-setup --mcp` -- one `claude mcp add` per host-side server,
    pointed at the in-sandbox proxy. sbx/DESIGN-plugin-integration.md §5
    step 4."""

    WANTED = {
        "gerrit": {"command": "/home/agent/.local/bin/wmf-sbx-mcp-proxy",
                   "args": ["gerrit", "--tools", "get_file_diff", "--no-add"]},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "claude.json")
        self.calls = []

    def claude_json(self, servers):
        with open(self.path, "w", encoding="utf-8") as f:
            # Alongside sbx's own gateway entry and the rest of the file,
            # which is the reason this reconciles rather than overwrites.
            json.dump({"numStartups": 3, "mcpServers": dict(
                servers, **{"mcp-gateway": {"type": "http"}})}, f)

    def fake_run(self, cmd, cwd=None):
        self.calls.append(cmd)
        return FakeCompletedProcess(0)

    def argv(self, index=0):
        """One recorded call with any `sudo -u agent -H` prefix stripped and
        `claude` normalised back to its bare name.

        Whether the sudo prefix is there depends on the euid the test
        itself happens to be running under; whether argv[0] is absolute
        depends on where `claude` is installed where the test is running
        (see claude_executable, and §69 for why it is absolute at all)."""
        cmd = list(self.calls[index])
        start = next(i for i, a in enumerate(cmd)
                     if a == "claude" or a.endswith("/claude"))
        return ["claude"] + cmd[start + 1:]

    def verbs(self):
        return [self.argv(i)[2] for i in range(len(self.calls))]

    def register(self, config=None):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            ok = s.register_mcp_servers(config or self.WANTED, path=self.path,
                                        run=self.fake_run)
        return ok, err.getvalue()

    def test_registers_a_server_that_is_not_there(self):
        self.claude_json({})
        ok, err = self.register()
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.argv(), [
            "claude", "mcp", "add", "--scope", "user", "gerrit", "--",
            "/home/agent/.local/bin/wmf-sbx-mcp-proxy",
            "gerrit", "--tools", "get_file_diff", "--no-add",
        ])
        self.assertIn("registered the gerrit MCP server", err)

    def test_an_identical_registration_is_left_alone(self):
        # This runs at install and again on every container start; a
        # restart must not churn ~/.claude.json.
        self.claude_json({"gerrit": dict(self.WANTED["gerrit"], type="stdio",
                                         env={})})
        ok, err = self.register()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])
        self.assertIn("already registered", err)

    def test_a_changed_allowlist_is_removed_and_re_added(self):
        # `claude mcp add` refuses a name that is taken (MEASURED: exit 1,
        # "already exists in user config") and there is no `mcp set`. A
        # shortened --tools list is a policy change that must land.
        self.claude_json({"gerrit": {"type": "stdio",
                                     "command": self.WANTED["gerrit"]["command"],
                                     "args": ["gerrit", "--tools", "everything"]}})
        ok, _err = self.register()
        self.assertTrue(ok)
        self.assertEqual(self.verbs(), ["remove", "add"])

    def test_an_unreadable_claude_json_still_registers(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        ok, _err = self.register()
        self.assertTrue(ok)
        self.assertEqual(self.verbs(), ["add"])

    def test_a_failed_add_is_reported_and_fails_the_step(self):
        self.claude_json({})

        def failing(cmd, cwd=None):
            self.calls.append(cmd)
            return FakeCompletedProcess(1)

        with contextlib.redirect_stderr(io.StringIO()) as err:
            ok = s.register_mcp_servers(self.WANTED, path=self.path, run=failing)
        self.assertFalse(ok)
        self.assertIn("could not register the gerrit MCP server", err.getvalue())

    def test_as_root_it_goes_through_sudo_u_agent(self):
        # Otherwise the install-time run writes the registration into
        # /root/.claude.json, where the agent never sees it.
        with contextlib.redirect_stderr(io.StringIO()):
            s.claude_mcp(["ls"], run=self.fake_run, root=True)
        self.assertEqual(self.calls[0][:4], ["sudo", "-u", s.SANDBOX_USER, "-H"])
        self.assertEqual(self.calls[0][4:], [s.claude_executable(), "mcp", "ls"])
        with contextlib.redirect_stderr(io.StringIO()):
            s.claude_mcp(["ls"], run=self.fake_run, root=False)
        self.assertEqual(self.calls[1], [s.claude_executable(), "mcp", "ls"])

    def test_the_sudo_run_names_claude_by_absolute_path(self):
        # §69: sudo replaces PATH with its own secure_path, which does not
        # contain ~/.local/bin, so `sudo -u agent -H claude` is
        # `sudo: claude: command not found` -- exit 1, at the last install
        # step of a five-minute create.
        with mock.patch.object(s.os, "access", return_value=True):
            self.assertEqual(s.claude_executable(), s.SANDBOX_CLAUDE_BIN)
        self.assertTrue(os.path.isabs(s.SANDBOX_CLAUDE_BIN))

    def test_claude_executable_falls_back_to_the_path(self):
        # Anywhere that isn't a sandbox built from this image -- a host
        # checkout, the tests -- there is no /home/agent to find.
        with mock.patch.object(s.os, "access", return_value=False), \
                mock.patch.object(s.shutil, "which", return_value="/opt/claude"):
            self.assertEqual(s.claude_executable(), "/opt/claude")
        with mock.patch.object(s.os, "access", return_value=False), \
                mock.patch.object(s.shutil, "which", return_value=None):
            self.assertEqual(s.claude_executable(), "claude")

    def test_the_default_target_is_the_agents_claude_json(self):
        self.assertEqual(s.SANDBOX_CLAUDE_JSON,
                         os.path.join(s.SANDBOX_HOME, ".claude.json"))

    def test_run_mcp_reads_the_kits_file_and_main_dispatches_it(self):
        self.claude_json({})
        config_path = os.path.join(self.tmp.name, "wmf-sbx-mcp.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.WANTED, f)
        with contextlib.redirect_stderr(io.StringIO()):
            code = s.main(["--mcp", config_path, self.path], run=self.fake_run)
        self.assertEqual(code, 0)
        self.assertEqual(self.verbs(), ["add"])

    def test_run_mcp_complains_about_a_missing_or_malformed_file(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(s.run_mcp([], run=self.fake_run), 1)
            self.assertEqual(
                s.run_mcp(["/nonexistent/mcp.json"], run=self.fake_run), 1)
        self.assertIn("JSON registration file", err.getvalue())
        self.assertIn("could not read", err.getvalue())


SAMPLE_CLAUDE_MD = """\
## Keep

Good advice.

```bash
# Not a heading
echo hi
```

## Wrong

Direct mode says something false.

### Wrong subsection

More of it.

## Keep too

### Aspire

Remove me.

## Last
"""


class ApplyClaudeMdEditsTests(unittest.TestCase):
    def test_replace_takes_the_subsections_with_it(self):
        new, failures = s.apply_claude_md_edits(SAMPLE_CLAUDE_MD, [
            {"op": "replace", "heading": "## Wrong", "text": "## Right\n\nTrue."}])
        self.assertEqual(failures, [])
        self.assertNotIn("Wrong subsection", new)
        self.assertIn("## Right\n\nTrue.\n\n## Keep too", new)
        self.assertIn("Good advice.", new)

    def test_remove_stops_at_a_heading_of_the_same_or_a_higher_level(self):
        new, failures = s.apply_claude_md_edits(SAMPLE_CLAUDE_MD, [
            {"op": "remove", "heading": "### Aspire"}])
        self.assertEqual(failures, [])
        self.assertNotIn("Remove me", new)
        self.assertIn("## Keep too\n\n## Last", new)

    def test_a_shell_comment_in_a_code_block_is_not_a_heading(self):
        new, failures = s.apply_claude_md_edits(SAMPLE_CLAUDE_MD, [
            {"op": "remove", "heading": "# Not a heading"}])
        self.assertIsNone(new)
        self.assertIn("no such heading", failures[0])
        # And it does not end the section above it.
        new, _ = s.apply_claude_md_edits(SAMPLE_CLAUDE_MD, [
            {"op": "remove", "heading": "## Keep"}])
        self.assertNotIn("echo hi", new)

    def test_white_space_in_the_heading_does_not_matter(self):
        new, failures = s.apply_claude_md_edits(SAMPLE_CLAUDE_MD, [
            {"op": "remove", "heading": "##   Wrong  "}])
        self.assertEqual(failures, [])

    def test_all_or_nothing(self):
        new, failures = s.apply_claude_md_edits(SAMPLE_CLAUDE_MD, [
            {"op": "remove", "heading": "### Aspire"},
            {"op": "remove", "heading": "## Gone upstream"}])
        self.assertIsNone(new)
        self.assertEqual(len(failures), 1)
        self.assertIn("edit 2", failures[0])
        self.assertIn("'## Gone upstream'", failures[0])

    def test_contains_catches_a_section_upstream_rewrote(self):
        new, failures = s.apply_claude_md_edits(SAMPLE_CLAUDE_MD, [
            {"op": "remove", "heading": "## Wrong",
             "contains": ["Direct   mode", "clone mode"]}])
        self.assertIsNone(new)
        self.assertIn("'clone mode'", failures[0])
        self.assertNotIn("Direct", failures[0])

    def test_a_duplicate_heading_is_a_failure(self):
        text = SAMPLE_CLAUDE_MD + "\n## Wrong\n"
        new, failures = s.apply_claude_md_edits(text, [
            {"op": "remove", "heading": "## Wrong"}])
        self.assertIsNone(new)
        self.assertIn("2 times", failures[0])

    def test_an_unknown_op_is_a_failure(self):
        _, failures = s.apply_claude_md_edits(SAMPLE_CLAUDE_MD, [
            {"op": "patch", "heading": "## Wrong"}])
        self.assertIn("not a valid edit", failures[0])


class EditClaudeMdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.target = os.path.join(d, "Extensions", "CLAUDE.md")
        os.makedirs(os.path.dirname(self.target))
        with open(self.target, "w", encoding="utf-8") as f:
            f.write(SAMPLE_CLAUDE_MD)
        self.edits = os.path.join(d, "edits.json")
        self.write_edits([{"op": "remove", "heading": "### Aspire"}])
        self.upstream = os.path.join(d, "upstream-CLAUDE.md")
        self.plan = os.path.join(d, "plan.json")
        with open(self.plan, "w", encoding="utf-8") as f:
            json.dump({"version": s.PLAN_VERSION, "hostHome": d, "daemonPort": 1,
                       "repos": [], "primary": os.path.join(d, "Extensions", "Cite")}, f)

    def write_edits(self, edits):
        with open(self.edits, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "upstream": "sbx vTEST", "edits": edits}, f)

    def edit(self, target=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return s.edit_claude_md(self.edits, target, upstream_copy=self.upstream,
                                    plan_path=self.plan)

    def read(self, path=None):
        with open(path or self.target, encoding="utf-8") as f:
            return f.read()

    def test_finds_the_file_from_the_plan_and_edits_it(self):
        self.assertEqual(self.edit(), (0, []))
        text = self.read()
        self.assertTrue(text.startswith(s.CLAUDE_MD_MARKER))
        self.assertNotIn("Remove me", text)
        # The upstream text is kept for `wmf-sbx refresh-claude-md`.
        self.assertEqual(self.read(self.upstream), SAMPLE_CLAUDE_MD)
        self.assertFalse(os.path.exists(self.target + ".wmf-sbx.tmp"))

    def test_is_idempotent(self):
        self.edit()
        first = self.read()
        # Even with edits that would fail now: a marked file is done.
        self.write_edits([{"op": "remove", "heading": "## Not there"}])
        self.assertEqual(self.edit(), (0, []))
        self.assertEqual(self.read(), first)
        self.assertEqual(self.read(self.upstream), SAMPLE_CLAUDE_MD)

    def test_keeps_the_mode(self):
        os.chmod(self.target, 0o640)
        self.edit()
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o640)

    def test_edits_that_do_not_apply_leave_the_file_alone_and_say_why(self):
        self.write_edits([{"op": "remove", "heading": "### Aspire"},
                          {"op": "remove", "heading": "## Not there"}])
        code, problems = self.edit()
        self.assertEqual(code, 1)
        self.assertEqual(self.read(), SAMPLE_CLAUDE_MD)
        self.assertIn("sbx vTEST", problems[0])
        self.assertTrue(all(p.startswith("error:") for p in problems))
        self.assertTrue(any("'## Not there'" in p for p in problems))
        # Saved even so: the refresh helper needs it most now.
        self.assertEqual(self.read(self.upstream), SAMPLE_CLAUDE_MD)

    def test_a_missing_file_is_a_warning(self):
        os.remove(self.target)
        code, problems = self.edit()
        self.assertEqual(code, 0)
        self.assertTrue(problems[0].startswith("warning:"))

    def test_an_explicit_target_wins_over_the_plan(self):
        other = os.path.join(self.tmp.name, "other.md")
        with open(other, "w", encoding="utf-8") as f:
            f.write(SAMPLE_CLAUDE_MD)
        self.edit(other)
        self.assertTrue(self.read(other).startswith(s.CLAUDE_MD_MARKER))
        self.assertEqual(self.read(), SAMPLE_CLAUDE_MD)

    def test_a_bad_edits_file_is_an_error(self):
        with open(self.edits, "w", encoding="utf-8") as f:
            f.write("{")
        code, problems = self.edit()
        self.assertEqual(code, 1)
        self.assertIn("could not read the edits", problems[0])

    def test_main_writes_its_own_status_and_exits_0_on_failure(self):
        self.write_edits([{"op": "remove", "heading": "## Not there"}])
        log_dir = os.path.join(self.tmp.name, "log")
        os.makedirs(log_dir)
        with contextlib.redirect_stderr(io.StringIO()):
            code = s.main(["--claude-md", self.edits, self.target], log_dir=log_dir)
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(log_dir), [s.CLAUDE_MD_STATUS_NAME])
        with open(s.claude_md_status_path(log_dir), encoding="utf-8") as f:
            status = json.load(f)
        self.assertEqual(status["exit"], 1)
        self.assertTrue(status["problems"])


if __name__ == "__main__":
    unittest.main()
