"""Fakes and helpers that more than one test module uses: a completed
process, a FakeLima that stands in for limactl, a temporary state and
cache (Env), and real git repositories."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.lima as lima_mod  # noqa: E402
import wmf_sbx.repos as repos  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import wmf_sbx.vm as vm  # noqa: E402


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True,
                          text=True, check=check)


def out(cwd, *args):
    return git(cwd, *args).stdout.strip()


def commit(repo, message, name="file"):
    """Append `message` to `name`, commit it, and return the new HEAD."""
    with open(os.path.join(repo, name), "a", encoding="utf-8") as f:
        f.write(message + "\n")
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)
    return out(repo, "rev-parse", "HEAD")


def identity(repo):
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")


def wait_for(cond, timeout=10, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def git_repo(path):
    """A real repository with one commit, on branch main."""
    os.makedirs(path)
    for argv in (["init", "-q", "-b", "main"],
                 ["-c", "user.name=t", "-c", "user.email=t@example.org",
                  "commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", "-C", path] + argv, check=True)


def allow_tmp(case):
    """The tests' repos are under the temporary directory, which
    repos.check_vm_path refuses (/tmp is a tmpfs in the VM)."""
    p = mock.patch.object(repos, "RESERVED_PREFIXES",
                          tuple(x for x in repos.RESERVED_PREFIXES
                                if not tempfile.gettempdir().startswith(x)))
    p.start()
    case.addCleanup(p.stop)


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeLima:
    """Instances and their status; records every call. `invariants` is
    the guest's verdict for each of vm.INVARIANTS."""

    def __init__(self, instances=None, invariants=None, rc=0):
        self.vms = dict(instances or {})
        self.calls = []
        self.invariants = invariants or [True] * len(vm.INVARIANTS)
        self.rc = rc
        self.templates = {}
        self.configs = {}
        self.backings = {}
        self.start_envs = {}

    def instances(self):
        return dict(self.vms)

    def exists(self, name):
        return name in self.vms

    def status(self, name):
        return self.vms.get(name)

    def validate(self, path):
        self.calls.append(("validate",))

    def create(self, name, path):
        with open(path) as f:
            self.templates[name] = image.yaml.safe_load(f)
        self.calls.append(("create", name))
        self.vms[name] = "Stopped"

    def start(self, name, timeout=None, extra_env=None):
        self.calls.append(("start", name))
        self.start_envs[name] = self.start_env = extra_env
        self.vms[name] = "Running"

    def stop(self, name):
        self.calls.append(("stop", name))
        self.vms[name] = "Stopped"

    def delete(self, name):
        self.calls.append(("delete", name))
        self.vms.pop(name, None)

    def copy(self, src, dst, recursive=False):
        self.calls.append(("copy", src, dst, recursive))

    def config_text(self, name, env=None):
        return self.configs.get(name, "")

    def edit(self, name, expression):
        self.calls.append(("edit", name, expression))

    lima_version = "2.2.1"

    def version(self):
        return self.lima_version

    def disk_path(self, name, env=None):
        return os.path.join(lima_mod.lima_home(env), name, "disk")

    def placeholder(self, cache_root):
        path = os.path.join(cache_root, "placeholder.qcow2")
        if not os.path.exists(path):
            os.makedirs(cache_root, exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"placeholder")
        return path

    def overlay_disk(self, name, backing, env=None):
        self.calls.append(("overlay", name, backing))
        self.backings[name] = backing
        disk = self.disk_path(name, env)
        os.makedirs(os.path.dirname(disk), exist_ok=True)
        with open(disk, "wb") as f:
            f.write(b"overlay")

    def rebase_disk(self, path, backing):
        self.calls.append(("rebase", path, backing))

    def disk_chain(self, name, env=None, force_share=False):
        """What `qemu-img info --backing-chain` says of an overlay made by
        overlay_disk: the disk, then its backing file."""
        self.calls.append(("disk_chain", name, force_share))
        disk = self.disk_path(name, env)
        backing = self.backings.get(name)
        if backing is None:
            return [{"filename": disk, "format": "qcow2"}]
        return [{"filename": disk, "format": "qcow2", "backing-filename": backing,
                 "full-backing-filename": backing},
                {"filename": backing, "format": "qcow2"}]

    def call(self, *args, check=True, capture=True, input=None, text=True, interactive=False):
        self.calls.append(("call",) + tuple(args))
        argv = list(args)
        if argv[0] == "shell" and "bash" in argv and any("echo 0:" in a or "0:ok" in a
                                                       or "WMF_SBX_HOST_UID" in a for a in argv):
            # Checks past `invariants` (the egress ones) pass.
            n = max(len(self.invariants), " ".join(argv).count("then echo"))
            verdicts = list(self.invariants) + [True] * (n - len(self.invariants))
            self.invariant_scripts = getattr(self, "invariant_scripts", []) + [argv[-1]]
            out = "\n".join(f"{i}:{'ok' if ok else 'FAIL'}" for i, ok in enumerate(verdicts))
            return Done(stdout=out)
        if argv[0] == "shell" and any("mktemp /dev/shm/wmf-sbx-cred." in a for a in argv):
            return Done(stdout="/dev/shm/wmf-sbx-cred.Xy12Ab\n")
        if argv[0] == "shell" and any("mktemp -d /tmp/wmf-sbx-cp" in a for a in argv):
            return Done(stdout="/tmp/wmf-sbx-cp.AbC123\n")
        if argv[0] == "shell" and input and "base64 -w0" in input:
            return Done(stdout="S0VQVA==\n")   # snapshot.keep_script's output
        if argv[0] == "shell" and vm.UNCOMMITTED_SCRIPT in argv:
            # The guest's `git status --porcelain` pass: clean by default.
            return getattr(self, "uncommitted_result", Done())
        if argv[0] == "shell" and argv[-3:-1] == ["test", "-d"]:
            return Done(returncode=1)
        return Done(returncode=self.rc)

    def shells(self):
        return [c[1:] for c in self.calls if c[0] == "call" and c[1] == "shell"]


class Env(unittest.TestCase):
    """State and cache in a temporary directory; one cached image."""

    KEY = "0123456789abcdef"
    MANIFEST_INPUTS = {"arch": image.host_arch(), "agent": {"uid": os.getuid()}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                    "HOME": self.tmp,
                    # not the Lima instances of the machine that runs the tests
                    "LIMA_HOME": os.path.join(self.tmp, "lima"),
                    # not the keychain of the machine that runs the tests
                    "WMF_SBX_HOST_LOGIN": "0"}
        d = image.entry_dir(self.KEY, self.env)
        os.makedirs(d)
        g = os.path.join(d, "golden.qcow2")
        with open(g, "wb") as f:
            f.write(b"golden")
        os.chmod(g, 0o444)
        with open(os.path.join(d, "golden.sha256"), "w") as f:
            f.write(image.file_digest(g) + "  golden.qcow2\n")
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"key": self.KEY, "inputs": self.MANIFEST_INPUTS}, f)

    def _cleanup(self):
        for dirpath, _dirs, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(dirpath, f), 0o644)
        shutil.rmtree(self.tmp)

    def sandbox(self, name="sbx-demo", status="Running", primary="/home/me/src/demo", lima=None):
        st = state_mod.new_state(name, created="2026-10-09T00:00:00+00:00", primary_dir=primary,
                                 image=self.KEY, vm_type="qemu", repos=[primary])
        state_mod.save(st, self.env)
        lima = lima or FakeLima()
        if status:
            lima.vms[vm.instance_name(name)] = status
        return lima

    def quiet(self, fn, *a, **kw):
        err, out = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = fn(*a, **kw)
        finally:
            self.err, self.out = err.getvalue(), out.getvalue()
        return rc
