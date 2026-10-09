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
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

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

    def scripts(self):
        return repos.guest_scripts(self.REPOS, {"/home/me/core": "https://g/core"},
                                   keep={"/home/me/core"}, readonly={"/home/me/Cite"})

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


class CloneRepoTests(Tmp):
    """clone_repo from sandbox-repos.sh, run on the host: the "mount" is
    the host repo's .git, the "upstream" another local repository."""

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

    def clone(self, dest, branch, sha, upstream, reset, check=True):
        env = dict(os.environ, PATH=os.path.join(SBX, "bin") + ":" + os.environ["PATH"],
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.org")
        script = open(repos.GUEST_SCRIPT).read() + "\n" + repos._call(
            "clone_repo", os.path.join(self.host, ".git"), dest, branch, sha, upstream, reset)
        return subprocess.run(["bash", "-s"], input=script, text=True, env=env,
                              capture_output=True, check=check)

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

    def test_a_dependency_is_reset_to_upstream_master(self):
        dest = os.path.join(self.tmp, "vm", "dep")
        self.clone(dest, "topic", git(self.host, "rev-parse", "HEAD"), self.upstream, "1")
        self.assertEqual(git(dest, "rev-parse", "HEAD"),
                         git(self.upstream, "rev-parse", "master"))

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


class HelperTests(Tmp):

    def setUp(self):
        super().setUp()
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp, "state")}
        state_mod.save(state_mod.new_state("demo"), self.env)

    def serve(self, *lines, url="wmfsbx://demo/home/me/core"):
        out = io.BytesIO()
        self.execs = []
        rc = helper.serve(url, io.BytesIO(b"".join(l + b"\n" for l in lines)), out,
                          execvp=lambda f, argv: self.execs.append(argv), env=self.env)
        return rc, out.getvalue()

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
        argv = ["--no-deps", "--config", os.path.join(self.tmp, "repos.yaml"),
                "--image", self.KEY, "--name", "demo", self.repo] + list(extra)
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

    def test_no_remotes_leaves_the_host_config_alone(self):
        before = open(os.path.join(self.repo, ".git", "config")).read()
        self.assertEqual(self.create("--no-remotes"), 0, self.err)
        self.assertEqual(open(os.path.join(self.repo, ".git", "config")).read(), before)
        self.assertIn("gc stays on", self.err)

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
