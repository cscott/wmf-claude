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
import subprocess


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

    def call(self, *args, check=True, input=None, text=True, capture=True):
        argv = [self.exe] + [str(a) for a in args]
        kwargs = {"env": self.env, "input": input}
        if capture:
            kwargs["capture_output"] = True
        if text:
            kwargs["text"] = True
        result = self.run(argv, **kwargs)
        if check and result.returncode != 0:
            err = result.stderr if isinstance(result.stderr, str) else (
                (result.stderr or b"").decode("utf-8", "replace"))
            raise LimaError(
                f"{' '.join(argv[:3])} ... failed (exit {result.returncode}): "
                f"{(err or '').strip()[-2000:]}")
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

    def start(self, name, timeout=None):
        args = ["start", "--tty=false"]
        if timeout:
            args.append(f"--timeout={timeout}")
        self.call(*args, name)

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
