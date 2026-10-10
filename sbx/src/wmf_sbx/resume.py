#!/usr/bin/env python3
"""Start a Claude Code session in a sandbox (lima-port/HANDOFF-LIMA.md §8,
contained mode).

Usage:
  wmf-sbx resume NAME [LAUNCHER-FLAG ...] [-- CLAUDE-ARG ...]
  wmf-sbx run --name NAME [LAUNCHER-FLAG ...] [-- CLAUDE-ARG ...]

Starts the VM if it is stopped and checks the invariants, then runs
upstream's bin/claude in the VM as the agent, in the primary clone, under
nono (session.launcher_argv). LAUNCHER-FLAGs go to bin/claude, e.g.
`--allow-post=HOST` or `--local-db`; CLAUDE-ARGs go to Claude Code.

Without CLAUDE-ARGs, a sandbox that has had a conversation gets
`--continue`.

The credential (D6, MVP) comes from the host: $ANTHROPIC_API_KEY,
$CLAUDE_CODE_OAUTH_TOKEN, or the file ~/.config/wmf-sbx/claude-oauth-token
(a token from `claude setup-token`, mode 0600). It is given to this
session only (session.py). Without one, Claude Code asks you to log in,
and that login stays in this VM.
"""

import sys

from . import color as color_mod
from . import lima as lima_mod
from . import session as session_mod
from . import start as start_mod
from . import state as state_mod
from . import vm as vm_mod

CONTINUE_FLAGS = ("--continue", "-c")
SESSION_FLAGS = CONTINUE_FLAGS + ("--resume", "-r")

# Tails that never start a conversation, so a clean exit from one says
# nothing about whether there is anything to --continue next time. From
# `claude --help`: the informational flags, and every subcommand.
INFORMATIONAL_FLAGS = ("--version", "-v", "--help", "-h")
AGENT_SUBCOMMANDS = frozenset(
    "agents attach auth auto-mode doctor gateway import install logs mcp "
    "plugin plugins project respawn rm setup-token stop kill ultrareview "
    "update upgrade".split()
)


def split_args(argv):
    """(ours, Claude's or None). Everything after `--` is Claude Code's."""
    if "--" in argv:
        i = argv.index("--")
        return list(argv[:i]), list(argv[i + 1:])
    return list(argv), None


def starts_a_conversation(claude_args):
    """Whether this tail hands over to a conversation, which is what the
    `attached` flag records. `-- --version` or `-- mcp list` does not, and
    marking the sandbox attached for it makes the next resume pass
    --continue to a Claude with nothing to continue, which exits 1."""
    args = list(claude_args or [])
    if any(a in INFORMATIONAL_FLAGS for a in args):
        return False
    return not (args and args[0] in AGENT_SUBCOMMANDS)


def default_claude_args(attached):
    """The tail when the caller gave none: --continue once a conversation
    exists."""
    return ["--continue"] if attached else []


def set_attached(name, env=None):
    state = state_mod.load(name, env=env)
    if state is None or state.get("attached"):
        return
    state["attached"] = True
    try:
        state_mod.save(state, env=env)
    except OSError as e:
        print(f"warning: could not record the attach state ({e}).", file=sys.stderr)


def main(argv=None, lima=None, env=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ours, claude_args = split_args(argv)
    if not ours or ours[0] in ("-h", "--help"):
        print(__doc__.split("Usage:", 1)[1].split("\n\n", 1)[0].rstrip(), file=sys.stderr)
        return 0 if ours else 2
    target, launcher_flags = ours[0], ours[1:]
    bad = [f for f in launcher_flags if not f.startswith("--")]
    if bad:
        print(color_mod.error(f"error: {bad[0]!r}: launcher flags start with --; put "
                              f"Claude Code's arguments after --"), file=sys.stderr)
        return 2
    lima = lima or lima_mod.Limactl()
    try:
        name, state = vm_mod.resolve(target, env=env)
        vm_mod.ensure_running(name, lima=lima, env=env,
                              log=lambda m: print(color_mod.dim(m), file=sys.stderr))
        if not start_mod.check(name, lima=lima):
            print(color_mod.error(f"error: {name} breaks a security invariant; no session "
                                  f"is started. `wmf-sbx rm {name}` and create it again."),
                  file=sys.stderr)
            return 1
        cred = session_mod.host_credential(env)
        cred_path = session_mod.put_credential(name, cred, lima=lima) if cred else None
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError,
            session_mod.SessionError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    if not cred:
        print(color_mod.dim(
            "note: no Claude credential on the host ($ANTHROPIC_API_KEY, "
            f"$CLAUDE_CODE_OAUTH_TOKEN or {session_mod.token_file(env)}); Claude Code "
            "will ask you to log in, in this VM only."), file=sys.stderr)
    if claude_args is None:
        claude_args = default_claude_args(state.get("attached"))
    argv = session_mod.launcher_argv(
        state, cred_path=cred_path, launcher_flags=launcher_flags,
        claude_args=claude_args, proxy=session_mod.upstream_proxy(env))
    print(color_mod.dim(f"+ claude in {vm_mod.instance_name(name)}, as the agent, in "
                        f"{state['primaryDir']}"), file=sys.stderr)
    result = vm_mod.shell(name, argv, lima=lima, workdir="/", check=False, capture=False)
    if result.returncode == 0 and starts_a_conversation(claude_args):
        set_attached(name, env=env)
    return result.returncode


def run_main(argv=None, lima=None, env=None):
    """`wmf-sbx run --name NAME ...`: the same as `resume NAME ...`."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) < 2 or argv[0] != "--name":
        print("usage: wmf-sbx run --name NAME [LAUNCHER-FLAG ...] [-- CLAUDE-ARG ...]",
              file=sys.stderr)
        return 2
    return main([argv[1]] + argv[2:], lima=lima, env=env)


if __name__ == "__main__":
    sys.exit(main())
