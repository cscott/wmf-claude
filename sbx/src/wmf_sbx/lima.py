#!/usr/bin/env python3
"""A thin wrapper around `limactl`, so that the Lima backend can be tested
without a VM.

Every Lima operation of wmf-sbx goes through `Limactl`. The class runs
`limactl` with a `run` callable that has the signature of
`subprocess.run`. The tests give it a fake, which records the argv and
returns canned results. See lima-port/HANDOFF-LIMA.md §10.

Rules for the callers:

- Give each argument as its own list item. Nothing goes through a shell
  on the host. `limactl shell` quotes each argument for the guest shell.
- Use `--tty=false` for every call that can prompt, so that a call never
  waits for an answer.
- `limactl` writes warnings ("Non-strict YAML detected", "/dev/kvm is not
  available") to stderr. The binary-safe calls (`shell` with `input`)
  read stdout only, so the warnings do not change the data (RAN, see
  HANDOFF-LIMA.md §11, phase 0).
"""

import json
import os
import re
import subprocess


# user:password@ in a URL. limactl logs proxy URLs in full, and in a
# session the proxy is nono's, with its session password (RAN, N3).
URL_PASSWORD_RE = re.compile(r"(://[^/@\s:]+:)[^/@\s]+@")


def redact(text):
    """text, with the passwords in its URLs replaced by ***."""
    return URL_PASSWORD_RE.sub(r"\1***@", text)


class LimaError(Exception):
    """A `limactl` call failed. The message includes its stderr."""


def lima_home(env=None):
    """The directory that holds the Lima instances: $LIMA_HOME, or
    ~/.lima."""
    env = os.environ if env is None else env
    return env.get("LIMA_HOME") or os.path.join(os.path.expanduser("~"), ".lima")


class Limactl:
    def __init__(self, run=subprocess.run, exe="limactl", env=None):
        self.run = run
        self.exe = exe
        self.env = env

    # -- the call itself -------------------------------------------------

    def call(self, *args, check=True, input=None, text=True, capture=True,
             interactive=False, extra_env=None):
        """Run limactl. Its stdin is /dev/null unless `input` is given or
        the command is `interactive` (the engineer's terminal): `limactl
        shell` passes stdin to SSH, which reads it even when the command
        does not, so a pipe into `wmf-sbx resume` was eaten by the checks
        before the session (RAN), and a prompt would hang invisibly.
        `extra_env` adds variables to this call's environment."""
        argv = [self.exe] + [str(a) for a in args]
        env = self.env
        if extra_env:
            env = dict(os.environ if env is None else env, **extra_env)
        kwargs = {"env": env, "input": input}
        if input is None and not interactive:
            kwargs["stdin"] = subprocess.DEVNULL
        if capture:
            kwargs["capture_output"] = True
        if text:
            kwargs["text"] = True
        try:
            result = self.run(argv, **kwargs)
        except FileNotFoundError:
            raise LimaError(f"{self.exe} is not on PATH: install Lima 2.x, or add its "
                            f"bin directory to PATH") from None
        if check and result.returncode != 0:
            err = result.stderr if isinstance(result.stderr, str) else (
                (result.stderr or b"").decode("utf-8", "replace"))
            raise LimaError(
                f"{' '.join(argv[:3])} ... failed (exit {result.returncode}): "
                f"{redact((err or '').strip()[-2000:])}")
        return result

    # -- instances -------------------------------------------------------

    def instances(self):
        """{name: status} for every Lima instance."""
        result = self.call("list", "--json")
        found = {}
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            found[item["name"]] = item.get("status", "")
        return found

    def exists(self, name):
        return name in self.instances()

    def status(self, name):
        return self.instances().get(name)

    def validate(self, yaml_path):
        self.call("validate", yaml_path)

    def create(self, name, yaml_path, extra_args=()):
        self.call("create", "--tty=false", f"--name={name}", *extra_args, yaml_path)

    def start(self, name, timeout=None, extra_env=None):
        """`extra_env`: variables for limactl and its host agent, which
        starts QEMU (vm.qemu_env)."""
        args = ["start", "--tty=false"]
        if timeout:
            args.append(f"--timeout={timeout}")
        self.call(*args, name, extra_env=extra_env)

    def stop(self, name):
        self.call("stop", name)

    def delete(self, name):
        self.call("delete", "--force", name)

    def edit(self, name, expression):
        """`limactl edit --set EXPRESSION` (a yq expression) on a stopped
        instance. It takes effect at the next start."""
        self.call("edit", "--tty=false", name, "--set", expression)

    def config_text(self, name, env=None):
        """The instance's lima.yaml, or "" if it is not there."""
        try:
            with open(os.path.join(lima_home(env), name, "lima.yaml"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def disk_path(self, name, env=None):
        """The instance's boot disk. Lima 2.x calls it `disk`; `diffdisk`
        is the name before 2.0 (HANDOFF-LIMA.md, the measurement run)."""
        return os.path.join(lima_home(env), name, "disk")

    def version(self):
        """Lima's version ("2.2.1"), or None."""
        try:
            out = self.call("--version", check=False).stdout or ""
        except LimaError:
            return None
        words = out.split()
        return words[-1] if words else None

    # -- the instance's disk (disk.py, D12) ------------------------------

    def _qemu_img(self, *args):
        try:
            return self.run(["qemu-img"] + [str(a) for a in args], check=True,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise LimaError("qemu-img is not on PATH: install QEMU's tools "
                            "(qemu-utils on Debian)") from None
        except subprocess.CalledProcessError as e:
            raise LimaError(f"qemu-img {args[0]} failed (exit {e.returncode}): "
                            f"{(e.stderr or '').strip()[-2000:]}") from None

    def placeholder(self, cache_root):
        """disk.ensure_placeholder, with this object's `run`."""
        from . import disk as disk_mod
        return disk_mod.ensure_placeholder(cache_root, run=self.run)

    def overlay_disk(self, name, backing, env=None):
        """Replace the stopped instance's `disk` with a new qcow2 overlay
        on `backing` (an absolute path). Method B (disk.py)."""
        disk = self.disk_path(name, env)
        tmp = disk + ".wmf-sbx-overlay"
        self._qemu_img("create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", backing, tmp)
        os.replace(tmp, disk)

    def rebase_disk(self, path, backing):
        """`qemu-img rebase` of a stopped qcow2 file onto `backing`: what
        the file's old backing chain held, and `backing` lacks, is copied
        into it first (safe mode)."""
        self._qemu_img("rebase", "-f", "qcow2", "-F", "qcow2", "-b", backing, path)

    def disk_chain(self, name, env=None, force_share=False):
        """`qemu-img info --backing-chain --output=json` of the instance's
        disk, as a list. `force_share` (-U) for a running instance, whose
        QEMU holds the lock."""
        args = ["info", "--backing-chain", "--output=json"]
        if force_share:
            args.append("-U")
        out = self._qemu_img(*args, self.disk_path(name, env)).stdout
        try:
            data = json.loads(out)
        except ValueError:
            raise LimaError(f"qemu-img info: not JSON: {out[:200]!r}") from None
        return data if isinstance(data, list) else [data]

    # -- commands in the guest -------------------------------------------

    def shell(self, name, argv, workdir=None, input=None, check=True, text=True):
        """Run argv in the guest, as Lima's default user. No tty."""
        args = ["shell"]
        if workdir:
            args.append(f"--workdir={workdir}")
        args += [name, "--"] + list(argv)
        return self.call(*args, input=input, check=check, text=text)

    def copy(self, src, dst, recursive=False):
        """`limactl copy`. A guest path is written `NAME:PATH`."""
        self.call("copy", *(["-r"] if recursive else []), src, dst)
