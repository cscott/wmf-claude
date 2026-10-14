#!/usr/bin/env python3
"""Sign in once for all sandboxes, without Claude Code on the host
(track L): `claude setup-token` in a throwaway VM.

Usage:
  wmf-sbx login [--image KEY]

Boots a VM from a cached golden image, with no repo, no mount and no
session; runs `claude setup-token` there on your terminal (open the URL
it prints in your browser, sign in, paste the code back); saves the
token on the host in ~/.config/wmf-sbx/claude-oauth-token (mode 0600),
which `resume` gives to sessions through the host credential proxy; and
deletes the VM. The token is good for a year, for model requests only
(no Remote Control, no claude.ai connectors; README.md, "Sign-in").

Nothing runs in that VM before the token exists but Claude Code itself,
so no repo code and no agent can read it there.
"""

import argparse
import getpass
import os
import re
import secrets
import shutil
import signal
import sys

from . import cli as cli_mod
from . import color as color_mod
from . import credproxy as credproxy_mod
from . import image as image_mod
from . import lima as lima_mod
from . import template as template_mod
from . import vm as vm_mod

LOGIN_MEMORY = "2GiB"
# setup-token prints the token once. `script` records the terminal, at
# the engineer's terminal's width (RAN: a wider pty garbles Claude Code's
# screen); find_token() joins a token that wraps.
TRANSCRIPT = "/dev/shm/wmf-sbx-login.typescript"
SETUP_TOKEN = ("umask 077; "
               f"script -q -e -c 'claude setup-token' {TRANSCRIPT}")
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{20,}")
TOKEN_PREFIX = "sk-ant-oat01-"
TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]*")
# CSI sequences, OSC sequences (setup-token prints its URL as an OSC 8
# hyperlink, ended by ESC \ or BEL), and carriage returns.
ANSI_RE = re.compile(r"\x1b\[[0-9;?>]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\r")


class LoginError(Exception):
    pass


def pick_image(key=None, env=None):
    """(key, arch) of the golden image to boot: KEY, or the newest cached
    one (any image has Claude Code; the login needs nothing else)."""
    if key:
        image_mod.verify_entry(key, env)
        manifest = dict(image_mod.list_images(env)).get(key) or {}
    else:
        newest = image_mod.newest_image(env)
        if not newest:
            raise LoginError("no golden image in the cache; `wmf-sbx image build` first "
                             "(or `wmf-sbx create`, which builds it)")
        key, manifest = newest
        image_mod.verify_entry(key, env)
    arch = (manifest.get("inputs") or {}).get("arch") or image_mod.host_arch()
    return key, arch


def find_token(transcript, width=None):
    """The last setup-token token in a terminal transcript, or None. A
    token that fills its line to `width` (the terminal's) and goes on at
    the start of the next line was wrapped, and is joined."""
    lines = ANSI_RE.sub("", transcript).split("\n")
    found = None
    for i, line in enumerate(lines):
        start = line.rfind(TOKEN_PREFIX)
        if start < 0:
            continue
        token = TOKEN_CHARS.match(line, start + len(TOKEN_PREFIX)).group()
        token = TOKEN_PREFIX + token
        j = i
        while (width and j + 1 < len(lines)
               and len(lines[j].rstrip()) >= width - 1
               and lines[j].rstrip().endswith(token[-8:])):
            more = TOKEN_CHARS.match(lines[j + 1].lstrip()).group()
            if not more:
                break
            token += more
            j += 1
        if TOKEN_RE.fullmatch(token):
            found = token
    return found


def save_token(token, env=None):
    """Write the token file (0600), atomically. Returns its path."""
    path = credproxy_mod.token_file(env)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(tmp, path)
    return path


def run_login(name, lima, ask=getpass.getpass, log=print):
    """Run setup-token in the VM, interactively; return the token."""
    argv = vm_mod.agent_argv(["bash", "-c", SETUP_TOKEN])
    res = vm_mod.shell(name, argv, lima=lima, check=False, capture=False, interactive=True)
    if res.returncode != 0:
        raise LoginError(f"claude setup-token failed (exit {res.returncode})")
    out = vm_mod.shell(name, vm_mod.agent_argv(
        ["sh", "-c", f'cat {TRANSCRIPT}; rm -f {TRANSCRIPT}']), lima=lima, check=False)
    token = find_token(out.stdout or "", width=shutil.get_terminal_size().columns)
    if token:
        return token
    log("wmf-sbx: could not read the token from setup-token's output.")
    token = ask("Paste the token that setup-token printed (sk-ant-oat01-...): ").strip()
    if not TOKEN_RE.fullmatch(token):
        raise LoginError("that is not a setup-token token (want sk-ant-oat01-...)")
    return token


class _Stopped(Exception):
    """SIGTERM or SIGHUP: stop, and still delete the VM."""


def _stop(signum, _frame):
    raise _Stopped(signal.Signals(signum).name)


def main(argv=None, lima=None, env=None, ask=getpass.getpass, isatty=None):
    parser = argparse.ArgumentParser(prog="wmf-sbx login", description=__doc__.splitlines()[0])
    parser.add_argument("--image", metavar="KEY",
                        help="the golden image to boot (default: the newest cached one)")
    args, _config = cli_mod.parse_args(parser, argv, "login")
    # setup-token waits for a code that you paste; without a terminal it
    # waits for ever (RAN).
    if not (isatty or sys.stdin.isatty)():
        print(color_mod.error("error: wmf-sbx login needs a terminal (you paste a code "
                              "into it)"), file=sys.stderr)
        return 2
    lima = lima or lima_mod.Limactl()

    def say(msg):
        print(color_mod.dim(msg), file=sys.stderr)

    # Not a sandbox: no state file, so ls, resume and rm never see it.
    name = "login-" + secrets.token_hex(3)
    inst = vm_mod.instance_name(name)
    try:
        key, arch = pick_image(args.image, env)
        tmpl = template_mod.sandbox_template(
            image_mod.golden_path(key, env), arch, memory=LOGIN_MEMORY,
            proxy_ports=template_mod.loopback_proxy_ports(env),
            ca_files=image_mod.extra_ca_files(env))
    except (LoginError, image_mod.ImageError, template_mod.TemplateError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    say(f"+ a throwaway VM for the login ({inst}, image {key})")
    # A closed terminal (SIGHUP) or a kill (SIGTERM) must not leave the VM
    # behind: Python runs no `finally` for them by default (RAN).
    old = {s: signal.signal(s, _stop) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        vm_mod.create(name, tmpl, lima=lima, log=say)
        print("Sign in with your Claude account. Open the URL below in your browser, "
              "then paste the code back here.", file=sys.stderr)
        token = run_login(name, lima, ask=ask)
        path = save_token(token, env)
    except (LoginError, vm_mod.VmError, lima_mod.LimaError, OSError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, _Stopped):
        print(color_mod.error("interrupted; no token saved"), file=sys.stderr)
        return 130
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)
        say(f"+ limactl delete --force {inst}")
        try:
            lima.delete(inst)
        except lima_mod.LimaError as e:
            print(color_mod.error(f"warning: could not delete {inst} ({e}); "
                                  f"`limactl delete --force {inst}`"), file=sys.stderr)
    print(f"Saved the token in {path}. New sessions (`wmf-sbx resume`) use it; it is "
          f"good for a year.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
