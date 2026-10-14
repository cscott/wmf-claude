#!/usr/bin/env python3
"""Unit tests for phase 4: repos.py, sandbox-repos.sh, remote_helper.py
(git-remote-wmfsbx) and the create flow's mounts, clones and remotes --
run with:
  python3 -m unittest discover -s sbx/tests -v

The guest script and the helper run for real on the host: clone_repo
against local repositories, and `git fetch wmfsbx://...` through a fake
`limactl` that runs the guest's command here. The real round trip ran in
a VM (lima-port/HANDOFF-LIMA.md §11, phase 4)."""

import io
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.create as create  # noqa: E402
import wmf_sbx.remote_helper as helper  # noqa: E402
import wmf_sbx.remotes as remotes  # noqa: E402
import wmf_sbx.repos as repos  # noqa: E402
import wmf_sbx.rm as rm  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import wmf_sbx.template as template  # noqa: E402
import wmf_sbx.vm as vm  # noqa: E402
import settings_isolation  # noqa: E402


def setUpModule():
    settings_isolation.isolate()


from test_wmf_sbx_lifecycle import Env, FakeLima, allow_tmp, git_repo  # noqa: E402

SBX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.org"]


def git(path, *args, check=True):
    return subprocess.run(["git", "-C", path] + IDENT + list(args), check=check,
                          capture_output=True, text=True).stdout.strip()


class Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        allow_tmp(self)


class HostRepoTests(Tmp):

    def test_a_checkout_on_a_branch(self):
        path = os.path.join(self.tmp, "core")
        git_repo(path)
        r = repos.host_repo(path)
        self.assertEqual(r["gitDir"], os.path.join(path, ".git"))
        self.assertEqual(r["branch"], "main")
        self.assertEqual(r["sha"], git(path, "rev-parse", "HEAD"))

    def test_a_detached_head(self):
        path = os.path.join(self.tmp, "core")
        git_repo(path)
        git(path, "checkout", "-q", "--detach")
        r = repos.host_repo(path)
        self.assertIsNone(r["branch"])
        self.assertTrue(r["sha"])

    def test_a_worktree_mounts_the_common_git_dir(self):
        path = os.path.join(self.tmp, "core")
        git_repo(path)
        wt = os.path.join(self.tmp, "core-wt")
        git(path, "worktree", "add", "-q", "-b", "topic", wt)
        r = repos.host_repo(wt)
        self.assertEqual(r["gitDir"], os.path.join(path, ".git"))
        self.assertEqual(r["branch"], "topic")
        self.assertEqual(repos.git_dirs([repos.host_repo(path), r]),
                         [os.path.join(path, ".git")])

    def test_borrowed_object_dirs_are_mounted_too(self):
        base = os.path.join(self.tmp, "base")
        git_repo(base)
        path = os.path.join(self.tmp, "core")
        subprocess.run(["git", "clone", "-q", "--shared", base, path], check=True,
                       capture_output=True)
        # a relative entry, and a missing one, which git skips too
        with open(os.path.join(path, ".git", "objects", "info", "alternates"), "a") as f:
            f.write("../../base/.git/objects\n" + os.path.join(self.tmp, "gone") + "\n")
        r = repos.host_repo(path)
        borrowed = os.path.join(base, ".git", "objects")
        self.assertEqual(r["alternates"], [borrowed])
        self.assertEqual(repos.host_repo(base)["alternates"], [])
        self.assertEqual(repos.git_dirs([r, repos.host_repo(base)]),
                         [os.path.join(path, ".git"), os.path.join(base, ".git"), borrowed])
        _root, clones = repos.guest_scripts([r], {}, set(), set())
        line = [x for x in clones["agent"].splitlines() if x.startswith("clone_repo ")][0]
        self.assertEqual(shlex.split(line)[7:], [template.mount_point(borrowed)])

    def test_not_a_repository(self):
        os.makedirs(os.path.join(self.tmp, "plain"))
        with self.assertRaisesRegex(repos.RepoError, "not a git repository"):
            repos.host_repo(os.path.join(self.tmp, "plain"))

    def test_no_commit(self):
        path = os.path.join(self.tmp, "empty")
        subprocess.run(["git", "init", "-q", path], check=True)
        with self.assertRaisesRegex(repos.RepoError, "no commit"):
            repos.host_repo(path)

    def test_predicted_for_a_dry_run(self):
        r = repos.host_repo(os.path.join(self.tmp, "x"), predicted=True)
        self.assertEqual(r["alternates"], [])
        self.assertEqual(r["gitDir"], os.path.join(self.tmp, "x", ".git"))


class VmPathTests(unittest.TestCase):

    def test_paths_the_vm_uses_are_refused(self):
        for path in ("/tmp/core", "/etc/x", "/run/wmf-sbx/host/a", "/home/agent/core",
                     "/home/engineer/core", "/usr", "relative/core", "/home/me/../x"):
            with self.subTest(path=path), self.assertRaises(repos.RepoError):
                repos.check_vm_path(path)

    def test_ordinary_paths_pass(self):
        for path in ("/home/me/src/core", "/Users/me/src/core", "/srv-not/x", "/home/agentx/c"):
            repos.check_vm_path(path)


class GuestScriptTests(unittest.TestCase):

    REPOS = [{"path": "/home/me/core", "gitDir": "/home/me/core/.git", "branch": "main",
              "sha": "a" * 40},
             {"path": "/home/me/Cite", "gitDir": "/home/me/Cite/.git", "branch": None,
              "sha": "b" * 40},
             {"path": "/home/me/it's", "gitDir": "/home/me/it's/.git", "branch": "x",
              "sha": "c" * 40}]

    def scripts(self, refresh=False):
        return repos.guest_scripts(self.REPOS, {"/home/me/core": "https://g/core"},
                                   keep={"/home/me/core"}, readonly={"/home/me/Cite"},
                                   refresh=refresh)

    def test_root_prepares_each_mount_and_path(self):
        root, _ = self.scripts()
        self.assertIn("prepare /run/wmf-sbx/host/home/me/core/.git /home/me/core agent", root)
        self.assertIn("prepare /run/wmf-sbx/host/home/me/Cite/.git /home/me/Cite engineer", root)

    def test_ro_repos_are_cloned_by_the_engineer(self):
        _, clones = self.scripts()
        self.assertEqual(sorted(clones), ["agent", "engineer"])
        self.assertIn("/home/me/Cite", clones["engineer"])
        self.assertNotIn("/home/me/Cite", clones["agent"])

    def test_reset_and_upstream_arguments(self):
        _, clones = self.scripts()
        self.assertIn("clone_repo /run/wmf-sbx/host/home/me/core/.git /home/me/core main "
                      + "a" * 40 + " https://g/core 0", clones["agent"])
        self.assertIn("'' " + "b" * 40 + " '' 1", clones["engineer"])

    def test_refresh_updates_the_snapshots_clones(self):
        root, clones = self.scripts(refresh=True)
        self.assertIn("prepare /run/wmf-sbx/host/home/me/core/.git /home/me/core agent refresh",
                      root)
        self.assertIn("refresh_repo /run/wmf-sbx/host/home/me/core/.git /home/me/core main ",
                      clones["agent"])
        self.assertNotIn("\nclone_repo ", clones["agent"])

    def test_the_scripts_parse_and_quote(self):
        root, clones = self.scripts()
        for script in [root] + list(clones.values()):
            subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        self.assertIn("""'/home/me/it'"'"'s'""", clones["agent"])

    def test_remote_candidates(self):
        cands = repos.remote_candidates("sbx-core", self.REPOS[:1])
        self.assertEqual(cands, [{"hostDir": "/home/me/core", "remote": "sbx-core",
                                  "url": "wmfsbx://sbx-core/home/me/core",
                                  "sandboxPath": "/home/me/core"}])


class GuestScriptCase(Tmp):
    """sandbox-repos.sh run on the host: the "mount" is the host repo's
    .git, the "upstream" another local repository."""

    def setUp(self):
        super().setUp()
        self.host = os.path.join(self.tmp, "host")
        git_repo(self.host)
        git(self.host, "checkout", "-q", "-b", "topic")
        git(self.host, "commit", "-q", "--allow-empty", "-m", "host work")
        self.upstream = os.path.join(self.tmp, "upstream")
        subprocess.run(["git", "clone", "-q", "-b", "main", self.host, self.upstream],
                       check=True, capture_output=True)
        git(self.upstream, "checkout", "-q", "-b", "master")
        git(self.upstream, "commit", "-q", "--allow-empty", "-m", "upstream moved")
        os.makedirs(os.path.join(self.host, ".git", "hooks"), exist_ok=True)

    def clone(self, dest, branch, sha, upstream, reset, check=True, file_upstreams=False,
              fn="clone_repo", extra=(), mount=None):
        env = dict(os.environ, PATH=os.path.join(SBX, "bin") + ":" + os.environ["PATH"],
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.org")
        if file_upstreams:
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.file.allow",
                       GIT_CONFIG_VALUE_0="always")
        script = open(repos.GUEST_SCRIPT).read() + "\n" + repos._call(
            fn, mount or os.path.join(self.host, ".git"), dest, branch, sha, upstream, reset,
            *extra)
        return subprocess.run(["bash", "-s"], input=script, text=True, env=env,
                              capture_output=True, check=check)


class CloneRepoTests(GuestScriptCase):
    """clone_repo."""

    def test_a_requested_repo_stays_on_the_hosts_branch(self):
        dest = os.path.join(self.tmp, "vm", "host")
        self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), self.upstream, "0")
        self.assertEqual(git(dest, "rev-parse", "HEAD"), git(self.host, "rev-parse", "HEAD"))
        self.assertEqual(git(dest, "symbolic-ref", "--short", "HEAD"), "topic")
        self.assertEqual(git(dest, "remote", "get-url", "origin"), self.upstream)
        self.assertEqual(git(dest, "remote", "get-url", "local"),
                         os.path.join(self.host, ".git"))
        self.assertEqual(git(dest, "config", "checkout.defaultRemote"), "origin")
        with open(os.path.join(dest, ".git", "objects", "info", "alternates")) as f:
            self.assertEqual(f.read().strip(), os.path.join(self.host, ".git", "objects"))

    def test_a_shallow_host_repo_is_copied_not_borrowed(self):
        shallow = os.path.join(self.tmp, "shallow")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "-b", "topic",
                        "file://" + self.host, shallow], check=True, capture_output=True)
        self.host = shallow
        dest = os.path.join(self.tmp, "vm", "shallow")
        self.clone(dest, "topic", git(shallow, "rev-parse", "HEAD"), "", "0")
        self.assertEqual(git(dest, "rev-parse", "HEAD"), git(shallow, "rev-parse", "HEAD"))
        self.assertFalse(os.path.exists(
            os.path.join(dest, ".git", "objects", "info", "alternates")))

    def nested_mount(self):
        """A sandbox's clone, as an inner VM's mount shows it: its
        alternates name the mount's own objects dir (the path of the host
        mount in the outer VM), and the host's objects are elsewhere.
        Returns (the mount, the host's objects)."""
        mount = os.path.join(self.tmp, "run", "host.git")
        shutil.copytree(os.path.join(self.host, ".git"), mount, symlinks=True)
        outer = os.path.join(self.tmp, "outer")
        subprocess.run(["git", "clone", "-q", "--shared", "-b", "topic", mount, outer],
                       check=True, capture_output=True)
        hostobj = os.path.join(self.tmp, "run", "objects-of-host")
        os.rename(os.path.join(mount, "objects"), hostobj)
        shutil.rmtree(mount)
        os.rename(os.path.join(outer, ".git"), mount)
        return mount, hostobj

    def test_a_nested_clone_borrows_from_the_mounted_alternates(self):
        mount, hostobj = self.nested_mount()
        sha = git(self.host, "rev-parse", "HEAD")
        dest = os.path.join(self.tmp, "vm", "inner")
        res = self.clone(dest, "topic", sha, "", "0", mount=mount, check=False)
        self.assertNotEqual(res.returncode, 0)    # RAN, nested: "unable to read tree"
        shutil.rmtree(dest, True)
        res = self.clone(dest, "topic", sha, "", "0", mount=mount, extra=[hostobj])
        self.assertEqual(git(dest, "rev-parse", "HEAD"), sha)
        self.assertEqual(git(dest, "status", "--porcelain"), "")
        with open(os.path.join(dest, ".git", "objects", "info", "alternates")) as f:
            self.assertEqual(f.read().splitlines(), [os.path.join(mount, "objects"), hostobj])
        dest = os.path.join(self.tmp, "vm", "detached")
        self.clone(dest, "", sha, "", "0", mount=mount, extra=[hostobj])
        self.assertEqual(git(dest, "rev-parse", "HEAD"), sha)

    def test_a_dependency_is_reset_to_upstream_master(self):
        dest = os.path.join(self.tmp, "vm", "dep")
        self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), self.upstream, "1")
        self.assertEqual(git(dest, "rev-parse", "HEAD"),
                         git(self.upstream, "rev-parse", "master"))

    def test_submodules_come_from_the_hosts_copies(self):
        sub = os.path.join(self.tmp, "subsrc")
        git_repo(sub)
        subprocess.run(["git", "-C", self.host, "-c", "protocol.file.allow=always",
                        "submodule", "add", "-q", sub, "mcp"], check=True,
                       capture_output=True, env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null"))
        git(self.host, "commit", "-q", "-m", "add a submodule")
        # the host's copy is absorbed into .git/modules; the source goes away,
        # so only the mount can provide it
        shutil.rmtree(sub)
        dest = os.path.join(self.tmp, "vm", "withsub")
        res = self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), "", "0")
        self.assertTrue(os.path.exists(os.path.join(dest, "mcp", ".git")), res.stderr)
        self.assertEqual(git(os.path.join(dest, "mcp"), "rev-parse", "HEAD"),
                         git(os.path.join(self.host, "mcp"), "rev-parse", "HEAD"))
        self.assertNotIn("could not be checked out", res.stderr)

    def add_submodule(self, name="lib/ve"):
        """A submodule at NAME in the host repo, as VisualEditor's lib/ve:
        its upstream is a local repo; the host's copy is absorbed into
        .git/modules/NAME. Returns the upstream's path."""
        sub = os.path.join(self.tmp, "subupstream")
        git_repo(sub)
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null")
        subprocess.run(["git", "-C", self.host, "-c", "protocol.file.allow=always",
                        "submodule", "add", "-q", sub, name], check=True,
                       capture_output=True, env=env)
        git(self.host, "commit", "-q", "-m", "add a submodule")
        return sub

    def test_a_submodule_commit_the_host_lacks_comes_from_upstream(self):
        # VisualEditor's lib/ve: the superproject points at a lib/ve commit
        # that the host's copy (.git/modules/lib/ve) does not have.
        sub = self.add_submodule()
        git(sub, "commit", "-q", "--allow-empty", "-m", "only upstream has this")
        new = git(sub, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", self.host, "update-index", "--cacheinfo",
                        f"160000,{new},lib/ve"], check=True)
        git(self.host, "commit", "-q", "-m", "bump lib/ve")
        self.assertNotEqual(git(os.path.join(self.host, "lib", "ve"), "rev-parse", "HEAD"), new)
        dest = os.path.join(self.tmp, "vm", "ve")
        # The submodule's upstream is a local path here, not https.
        res = self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), "", "0",
                         file_upstreams=True)
        self.assertNotIn("could not be checked out", res.stderr)
        self.assertEqual(git(os.path.join(dest, "lib", "ve"), "rev-parse", "HEAD"), new,
                         res.stderr)

    def test_a_submodule_the_host_never_initialised_comes_from_upstream(self):
        # A host clone made without --recurse-submodules: no
        # .git/modules/lib/ve, an empty lib/ve.
        sub = self.add_submodule()
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null")
        subprocess.run(["git", "-C", self.host, "submodule", "deinit", "-q", "-f", "lib/ve"],
                       check=True, capture_output=True, env=env)
        shutil.rmtree(os.path.join(self.host, ".git", "modules"))
        dest = os.path.join(self.tmp, "vm", "ve")
        res = self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), "", "0",
                         file_upstreams=True)
        self.assertIn("from its upstream", res.stderr)
        self.assertEqual(git(os.path.join(dest, "lib", "ve"), "rev-parse", "HEAD"),
                         git(sub, "rev-parse", "HEAD"), res.stderr)

    def test_a_reset_goes_to_upstream_not_to_the_hosts_branch(self):
        # --reset-all on a repo the engineer named: the host's branch has
        # work that upstream does not; the clone must not keep it.
        dest = os.path.join(self.tmp, "vm", "reset")
        host_work = git(self.host, "rev-parse", "HEAD")
        res = self.clone(dest, "topic", host_work, self.upstream, "1")
        self.assertEqual(git(dest, "rev-parse", "HEAD"),
                         git(self.upstream, "rev-parse", "master"), res.stderr)
        self.assertNotEqual(subprocess.run(
            ["git", "-C", dest, "merge-base", "--is-ancestor", host_work, "HEAD"]).returncode, 0)
        self.assertEqual(git(dest, "remote", "get-url", "origin"), self.upstream)
        self.assertNotIn("warning", res.stderr)

    def test_a_reset_with_no_upstream_says_it_uses_the_hosts_main_line(self):
        dest = os.path.join(self.tmp, "vm", "noup")
        host_work = git(self.host, "rev-parse", "HEAD")
        res = self.clone(dest, "topic", host_work, "", "1")
        self.assertIn("no upstream is known", res.stderr)
        self.assertIn("reset to the host repo's master (or main)", res.stderr)
        # It says so, and that is what it does: origin is the host's repo,
        # and the clone is at its main line, not at the host's branch.
        self.assertEqual(git(dest, "rev-parse", "HEAD"), git(self.host, "rev-parse", "main"))
        self.assertNotEqual(git(dest, "rev-parse", "HEAD"), host_work)
        self.assertEqual(git(dest, "remote", "get-url", "origin"),
                         os.path.join(self.host, ".git"))

    def test_an_unreachable_upstream_says_so(self):
        dest = os.path.join(self.tmp, "vm", "unreach")
        res = self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"),
                         os.path.join(self.tmp, "nowhere"), "1")
        self.assertIn("could not fetch", res.stderr)

    def test_a_detached_host_head(self):
        dest = os.path.join(self.tmp, "vm", "det")
        sha = git(self.host, "rev-parse", "HEAD~1")
        self.clone(dest, "", sha, "", "0")
        self.assertEqual(git(dest, "rev-parse", "HEAD"), sha)
        self.assertNotEqual(git(dest, "symbolic-ref", "-q", "HEAD", check=False), "x")
        self.assertEqual(git(dest, "remote"), "origin")

    def test_the_commit_msg_hook_is_seeded_for_gerrit(self):
        hook = os.path.join(self.host, ".git", "hooks", "commit-msg")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\n")
        with open(os.path.join(self.host, ".gitreview"), "w") as f:
            f.write("[gerrit]\n")
        git(self.host, "add", ".gitreview")
        git(self.host, "commit", "-q", "-m", "review")
        dest = os.path.join(self.tmp, "vm", "g")
        self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), "", "0")
        self.assertTrue(os.access(os.path.join(dest, ".git", "hooks", "commit-msg"), os.X_OK))

    def test_an_unreachable_upstream_falls_back_to_local(self):
        dest = os.path.join(self.tmp, "vm", "nf")
        res = self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"),
                         os.path.join(self.tmp, "nowhere"), "0")
        self.assertIn("could not fetch", res.stderr)
        self.assertEqual(git(dest, "config", "checkout.defaultRemote"), "local")

    def test_a_missing_alternate_fails(self):
        dest = os.path.join(self.tmp, "vm", "alt")
        script = open(repos.GUEST_SCRIPT).read() + "\n" + repos._call(
            "clone_repo", os.path.join(self.tmp, "nope", ".git"), dest, "main", "x", "", "0")
        res = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True)
        self.assertNotEqual(res.returncode, 0)


class RefreshRepoTests(GuestScriptCase):
    """refresh_repo (track B3): a clone from a warm snapshot, made by an
    earlier create, brought to what clone_repo would make now."""

    def refresh(self, *a, **kw):
        return self.clone(*a, fn="refresh_repo", **kw)

    def test_a_requested_repo_moves_to_the_hosts_new_branch(self):
        dest = os.path.join(self.tmp, "vm", "r")
        self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), self.upstream, "0")
        # vendor/ and the like, which setup made: ignored, and kept.
        with open(os.path.join(dest, ".git", "info", "exclude"), "a") as f:
            f.write("vendor/\n")
        os.makedirs(os.path.join(dest, "vendor"))
        open(os.path.join(dest, "vendor", "kept"), "w").close()
        git(self.host, "checkout", "-q", "-b", "topic2")
        git(self.host, "commit", "-q", "--allow-empty", "-m", "newer host work")
        sha = git(self.host, "rev-parse", "HEAD")
        res = self.refresh(dest, "topic2", sha, self.upstream, "0")
        self.assertEqual(git(dest, "rev-parse", "HEAD"), sha, res.stderr)
        self.assertEqual(git(dest, "symbolic-ref", "--short", "HEAD"), "topic2")
        self.assertEqual(git(dest, "for-each-ref", "--format=%(refname:short)", "refs/heads"),
                         "topic2")
        self.assertEqual(git(dest, "rev-parse", "--abbrev-ref", "topic2@{upstream}"),
                         "local/topic2")
        self.assertEqual(git(dest, "remote", "get-url", "origin"), self.upstream)
        self.assertTrue(os.path.exists(os.path.join(dest, "vendor", "kept")))

    def test_a_dependency_follows_upstreams_new_master(self):
        dest = os.path.join(self.tmp, "vm", "d")
        self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), self.upstream, "1")
        git(self.upstream, "commit", "-q", "--allow-empty", "-m", "upstream moved again")
        res = self.refresh(dest, "topic", git(self.host, "rev-parse", "HEAD"),
                           self.upstream, "1")
        self.assertEqual(git(dest, "rev-parse", "HEAD"),
                         git(self.upstream, "rev-parse", "master"), res.stderr)

    def test_a_detached_host_head_from_a_branch_clone(self):
        dest = os.path.join(self.tmp, "vm", "x")
        self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), "", "0")
        sha = git(self.host, "rev-parse", "HEAD~1")
        self.refresh(dest, "", sha, "", "0")
        self.assertEqual(git(dest, "rev-parse", "HEAD"), sha)
        self.assertEqual(git(dest, "for-each-ref", "refs/heads"), "")

    def test_an_upstream_known_now_but_not_then(self):
        dest = os.path.join(self.tmp, "vm", "u")
        self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), "", "0")
        self.refresh(dest, "topic", git(self.host, "rev-parse", "HEAD"), self.upstream, "0")
        self.assertEqual(git(dest, "remote", "get-url", "origin"), self.upstream)
        self.assertEqual(git(dest, "remote", "get-url", "local"),
                         os.path.join(self.host, ".git"))

    def test_no_clone_yet_is_a_clone(self):
        dest = os.path.join(self.tmp, "vm", "new")
        self.refresh(dest, "topic", git(self.host, "rev-parse", "HEAD"), "", "0")
        self.assertEqual(git(dest, "rev-parse", "HEAD"), git(self.host, "rev-parse", "HEAD"))


class HelperTests(Tmp):

    def setUp(self):
        super().setUp()
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp, "state")}
        state_mod.save(state_mod.new_state("demo"), self.env)

    def serve(self, *lines, url="wmfsbx://demo/home/me/core", limactl="limactl"):
        out = io.BytesIO()
        self.execs = []
        self.out = out
        rc = helper.serve(url, io.BytesIO(b"".join(l + b"\n" for l in lines)), out,
                          execvp=lambda f, argv: self.execs.append(argv), env=self.env,
                          which=lambda exe: limactl)
        return rc, out.getvalue()

    def test_no_limactl_on_path_is_a_clear_error_before_the_answer(self):
        with self.assertRaisesRegex(helper.HelperError, "limactl is not on PATH"):
            self.serve(b"capabilities", b"connect git-upload-pack", limactl=None)
        self.assertEqual(self.out.getvalue(), b"connect\n\n")
        self.assertEqual(self.execs, [])

    def test_main_prints_the_error_without_a_traceback(self):
        err = io.StringIO()
        with mock.patch.object(helper.shutil, "which", return_value=None), \
                mock.patch.object(helper.sys, "stdin", io.TextIOWrapper(
                    io.BytesIO(b"connect git-upload-pack\n"))), \
                mock.patch.object(helper.sys, "stdout", io.TextIOWrapper(io.BytesIO())), \
                mock.patch.object(helper.state_mod, "require"), \
                contextlib.redirect_stderr(err):
            rc = helper.main(["demo", "wmfsbx://demo/home/me/core"])
        self.assertEqual(rc, 128)
        self.assertIn("fatal: wmfsbx: limactl is not on PATH", err.getvalue())

    def test_parse_url(self):
        self.assertEqual(helper.parse_url("wmfsbx://demo/home/me/core"),
                         ("demo", "/home/me/core"))
        for bad in ("wmfsbx://demo", "wmfsbx://de mo/x", "wmfsbx://../x", "git://demo/x",
                    "wmfsbx://demo/home/../etc", "wmfsbx://demo//x"):
            with self.subTest(url=bad), self.assertRaises(helper.HelperError):
                helper.parse_url(bad)

    def test_capabilities_are_connect_only(self):
        rc, out = self.serve(b"capabilities", b"")
        self.assertEqual((rc, out), (0, b"connect\n\n"))

    def test_upload_pack_execs_limactl_as_the_agent(self):
        _rc, out = self.serve(b"capabilities", b"connect git-upload-pack")
        self.assertEqual(out, b"connect\n\n\n")
        argv = self.execs[0]
        self.assertEqual(argv[:5], ["limactl", "shell", "--workdir=/", "wmf-sbx-demo", "--"])
        _rc, _out = self.serve(b"connect git-upload-pack", limactl="/opt/lima/bin/limactl")
        self.assertEqual(self.execs[0][0], "/opt/lima/bin/limactl")
        self.assertEqual(argv[5:9], ["sudo", "-H", "-u", "agent"])
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertEqual(argv[-3:], ["upload-pack", "--", "/home/me/core"])

    def test_receive_pack_is_refused(self):
        with self.assertRaisesRegex(helper.HelperError, "fetch-only"):
            self.serve(b"capabilities", b"connect git-receive-pack")
        self.assertEqual(self.execs, [])

    def test_an_unknown_sandbox_is_refused(self):
        with self.assertRaises(state_mod.StateError):
            self.serve(b"connect git-upload-pack", url="wmfsbx://other/home/me/core")


FAKE_LIMACTL = """#!/bin/bash
# Run the guest's command here: drop everything up to `git`.
while [[ $# -gt 0 && "$1" != git ]]; do shift; done
exec "$@"
"""


class FetchThroughHelperTests(Tmp):
    """`git fetch` and `git push` through bin/git-remote-wmfsbx, with a
    fake limactl that runs upload-pack on this host."""

    def setUp(self):
        super().setUp()
        fake = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "limactl"), "w") as f:
            f.write(FAKE_LIMACTL)
        os.chmod(os.path.join(fake, "limactl"), 0o755)
        self.env = dict(os.environ, XDG_STATE_HOME=os.path.join(self.tmp, "state"),
                        PATH=f"{fake}:{os.path.join(SBX, 'bin')}:{os.environ['PATH']}")
        state_mod.save(state_mod.new_state("demo"), self.env)
        self.host = os.path.join(self.tmp, "host")
        git_repo(self.host)
        self.vm = os.path.join(self.tmp, "vmclone")
        subprocess.run(["git", "clone", "-q", "--shared", self.host, self.vm], check=True)
        git(self.vm, "commit", "-q", "--allow-empty", "-m", "agent work")
        git(self.host, "remote", "add", "--no-tags", "demo", f"wmfsbx://demo{self.vm}")

    def host_git(self, *args):
        return subprocess.run(["git", "-C", self.host] + list(args), env=self.env,
                              capture_output=True, text=True)

    def test_fetch_brings_the_agents_commit(self):
        res = self.host_git("fetch", "-q", "demo")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(git(self.host, "rev-parse", "demo/main"), git(self.vm, "rev-parse", "HEAD"))

    def test_ls_remote_and_the_rm_guard_see_unfetched_work(self):
        state = {"name": "demo", "remotes": [{"hostDir": self.host, "remote": "demo",
                                              "url": f"wmfsbx://demo{self.vm}"}]}
        old = os.environ.copy()
        os.environ.update(self.env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        findings, unreachable = remotes.unfetched_tips(state)
        self.assertEqual(unreachable, [])
        self.assertEqual(findings[0][2], [git(self.vm, "rev-parse", "HEAD")])
        self.host_git("fetch", "-q", "demo")
        self.assertEqual(remotes.unfetched_tips(state), ([], []))

    def test_push_is_refused(self):
        res = self.host_git("push", "demo", "main:refs/heads/x")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("fetch-only", res.stderr)
        self.assertEqual(git(self.vm, "branch", "--list", "x"), "")


class CreateFlowTests(Env):
    """create.main with a real host repository and a FakeLima."""

    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(self.tmp)
        allow_tmp(self)
        self.repo = os.path.join(self.tmp, "src", "demo")
        git_repo(self.repo)

    def create(self, *extra):
        self.lima = FakeLima()
        # No warm snapshot: its gc borrower entry is tested in the
        # lifecycle tests.
        argv = ["--no-deps", "--config", os.path.join(self.tmp, "repos.yaml"),
                "--image", self.KEY, "--name", "demo", "--no-snapshot", self.repo] + list(extra)
        return self.quiet(create.main, argv, lima=self.lima, env=self.env,
                          prompt=lambda q: "")

    def test_the_git_dir_is_mounted_and_the_clone_scripts_run(self):
        self.assertEqual(self.create(), 0, self.err)
        tmpl = self.lima.templates["wmf-sbx-demo"]
        self.assertEqual(tmpl["mounts"], [{
            "location": os.path.join(self.repo, ".git"),
            "mountPoint": template.mount_point(os.path.join(self.repo, ".git")),
            "writable": False, "9p": {"cache": "none"}}])
        shells = [list(s) for s in self.lima.shells()]
        self.assertIn(["shell", f"--workdir={vm.ENGINEER_HOME}", "wmf-sbx-demo", "--",
                       "sudo", "bash", "-s"], shells)
        self.assertTrue(any("agent" in s and s[-2:] == ["bash", "-s"] for s in shells))

    def test_the_host_remote_is_added_and_gc_suspended(self):
        self.assertEqual(self.create(), 0, self.err)
        self.assertEqual(git(self.repo, "remote", "get-url", "demo"),
                         f"wmfsbx://demo{self.repo}")
        self.assertEqual(git(self.repo, "config", "gc.auto"), "0")
        self.assertEqual(git(self.repo, "config", "remote.demo.wmfSbxSandbox"), "demo")
        st = state_mod.load("demo", self.env)
        self.assertEqual([r["remote"] for r in st["remotes"]], ["demo"])

    def test_rm_removes_the_remote_and_restores_gc(self):
        self.assertEqual(self.create(), 0, self.err)
        self.assertEqual(self.quiet(rm.main, ["--force", "demo"], env=self.env,
                                    lima=self.lima), 0, self.err)
        self.assertEqual(git(self.repo, "remote"), "")
        self.assertEqual(git(self.repo, "config", "--get", "gc.auto", check=False), "")
        self.assertIsNone(state_mod.load("demo", self.env))

    def test_a_missing_remote_helper_is_a_warning(self):
        real = shutil.which
        with mock.patch.object(create.shutil, "which",
                               lambda exe, *a, **k: None if exe == "git-remote-wmfsbx"
                               else real(exe, *a, **k)):
            self.assertEqual(self.create(), 0, self.err)
        self.assertIn("git-remote-wmfsbx is not on PATH", self.err)

    def test_no_remotes_leaves_the_host_config_alone(self):
        before = open(os.path.join(self.repo, ".git", "config")).read()
        self.assertEqual(self.create("--no-remotes"), 0, self.err)
        self.assertEqual(open(os.path.join(self.repo, ".git", "config")).read(), before)
        self.assertIn("gc stays on", self.err)

    def clone_calls(self, *extra, repos=()):
        """create with the given extra repos; the clone_repo calls of the
        agent's clone script, as argument lists."""
        lima = FakeLima()
        scripts = []
        orig = lima.call

        def call(*args, **kw):
            if kw.get("input") and "clone_repo " in kw["input"]:
                scripts.append(kw["input"])
            return orig(*args, **kw)
        lima.call = call
        argv = ["--no-deps", "--config", os.path.join(self.tmp, "repos.yaml"),
                "--image", self.KEY, "--name", "demo", self.repo] + list(repos) + list(extra)
        self.assertEqual(self.quiet(create.main, argv, lima=lima, env=self.env,
                                    prompt=lambda q: ""), 0, self.err)
        return [shlex.split(line)[1:] for s in scripts for line in s.splitlines()
                if line.startswith("clone_repo ")]

    def second_repo(self):
        other = os.path.join(self.tmp, "src", "other")
        git_repo(other)
        git(other, "remote", "add", "origin", "https://gerrit.wikimedia.org/r/other")
        return other

    def test_reset_all_resets_every_repo_to_its_upstream(self):
        other = self.second_repo()
        calls = self.clone_calls("--reset-all", repos=[other])
        self.assertEqual(sorted(c[1] for c in calls), sorted([self.repo, other]))
        # clone_repo MOUNT PATH BRANCH SHA UPSTREAM RESET
        self.assertEqual([c[5] for c in calls], ["1", "1"])
        upstream = {c[1]: c[4] for c in calls}
        self.assertEqual(upstream[other], "https://gerrit.wikimedia.org/r/other")
        # A repo with no known upstream (no gerrit:/gitlab: name, no https
        # origin): no URL, and sandbox-repos.sh warns per repo.
        self.assertEqual(upstream[self.repo], "")

    def test_without_reset_all_the_named_repos_keep_the_hosts_commit(self):
        other = self.second_repo()
        calls = self.clone_calls(repos=[other])
        self.assertEqual([c[5] for c in calls], ["0", "0"])

    def test_a_borrowing_repo_mounts_what_it_borrows(self):
        # RAN (nested): the template check refused the objects dir mount.
        base = os.path.join(self.tmp, "src", "base")
        git_repo(base)
        borrower = os.path.join(self.tmp, "src", "borrower")
        subprocess.run(["git", "clone", "-q", "--shared", base, borrower], check=True,
                       capture_output=True)
        git(borrower, "remote", "set-url", "origin", "https://gerrit.wikimedia.org/r/b")
        calls = self.clone_calls(repos=[borrower])
        borrowed = template.mount_point(os.path.join(base, ".git", "objects"))
        self.assertEqual({c[1]: c[6:] for c in calls}, {self.repo: [], borrower: [borrowed]})

    def test_a_failed_clone_fails_the_create(self):
        class Broken(FakeLima):
            def call(self, *args, **kw):
                if args[0] == "shell" and args[-2:] == ("bash", "-s"):
                    self.calls.append(("call",) + args)
                    from test_wmf_sbx_lifecycle import Done
                    return Done(returncode=1)
                return super().call(*args, **kw)
        lima = Broken()
        argv = ["--no-deps", "--config", os.path.join(self.tmp, "repos.yaml"),
                "--image", self.KEY, "--name", "demo", self.repo]
        self.assertEqual(self.quiet(create.main, argv, lima=lima, env=self.env), 1)
        self.assertIn("failed", self.err)
        self.assertEqual(git(self.repo, "remote"), "")


if __name__ == "__main__":
    unittest.main()
