#!/usr/bin/env python3
"""Re-attach to an existing sandbox, with the host-side plumbing redone.

Bare `sbx run --name NAME` works, but it leaves two create-time-only
things stale, both MEASURED on the host 2026-09-08 (sbx/NOTES.md §34).
(`wmf-sbx run --name NAME` no longer has this problem: since run.py,
that spelling redirects here first -- see sbx/NOTES.md "wmf-sbx redirects
run, too". Only `wmf-sbx --upstream run --name NAME`, or a bare `sbx`
invoked directly, still hits the raw command and the staleness below.)

  * **The daemon's published host port moves on every container start.**
    sbx re-applies the publish itself, but with a fresh ephemeral port
    (32783 before `sbx stop`, 32784 after), so every `<name>` remote
    (named the same as the sandbox -- see remotes.remote_name_for) the
    host carries points at a closed port from the first
    restart onward. The daemon itself is fine -- the kit's startup
    command brings it back inside the sandbox (§33.2) -- it's the host
    half of the mapping that's stale.
  * **The agent CLI tail doesn't persist** (§20): whatever followed `--`
    on `sbx run` the first time has to be retyped on every re-attach.
    `resumeArgs:` in ~/.config/wmf-sbx/repos.yaml is that tail, and
    anything after `--` here overrides it for one invocation.

A third thing is stale after a restart, and it isn't cosmetic: **the
sandbox's mounts are gone** (§40). `sbx stop` throws the mount namespace
away, so the read-only lock-down on every host mirror is lifted and each
clone's `--shared` alternates point at a path that no longer exists --
6707 reachable commits before the restart, 5 after. The kit asks for a
restore on every container start and MEASURABLY gets one -- but not
before the `sbx exec` that triggered the start comes back, so resume
waits for the layout to come up (`--verify --wait`) and only re-applies
it itself if the wait runs out (§46).

So: start the container (via `exec ... true`, which is what actually
starts a stopped sandbox), re-apply the mount layout, re-point the
remotes at the port it came up on, then hand over to `wmf-sbx run
--name`. The start has to come first -- a stopped sandbox publishes
nothing to look up.

The default tail is `--continue`, since re-attaching almost always means
resuming the conversation you left -- but only from the *second* attach
onward. On a brand-new sandbox there is no conversation yet and claude
exits 1 with "No conversation found to continue", so the state file
carries an `attached` flag and the flag is added only once it's set. It
is set only by a tail that could have started a conversation --
`-- --version` exits 0 without starting one (§43) -- and if claude
nonetheless comes back saying it has nothing to continue, the flag was
ours to add, so resume drops it and relaunches once.
`--no-continue` opts out for one invocation; an explicit `--` tail
replaces the default outright.

Usage:
  wmf-sbx-resume [--config PATH] [--no-remotes] [--no-restore]
                 [--no-continue] NAME [-- AGENT_ARGS...]
"""

import argparse
import os
import subprocess
import sys

from . import create as create_mod
from . import remotes as remotes_mod
from . import resolve as resolve_mod
from . import state as state_mod


def split_agent_args(argv):
    """(our args, agent args). argparse can't do this: everything after
    `--` belongs to the agent CLI, including flags we'd otherwise claim."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return list(argv), None


# Every spelling of "resume the previous conversation" claude accepts.
# If the caller already asked for one of these -- or for --resume, which
# picks a session interactively -- adding ours on top would be a conflict,
# not a default.
CONTINUE_FLAGS = ("--continue", "-c")
SESSION_FLAGS = CONTINUE_FLAGS + ("--resume", "-r")

# Tails that never start a conversation, so a clean exit from one says
# nothing about whether there is anything to --continue next time. Taken
# from `claude --help` (2.1.246): the informational flags, and every
# subcommand it lists.
INFORMATIONAL_FLAGS = ("--version", "-v", "--help", "-h")
AGENT_SUBCOMMANDS = frozenset(
    "agents attach auth auto-mode doctor gateway import install logs mcp "
    "plugin plugins project respawn rm setup-token stop kill ultrareview "
    "update upgrade".split()
)


def starts_a_conversation(agent_args):
    """Whether this tail actually hands over to a session -- the thing the
    `attached` flag is supposed to record.

    `wmf-sbx-resume NAME -- --version` prints a version and exits 0, which
    is not a conversation; marking the sandbox attached on the strength of
    it makes the *next* resume pass --continue to a claude that has
    nothing to continue, and that exits 1 (MEASURED on the host, cananian,
    2026-09-08 -- and caused by exactly that command from §42's test)."""
    args = list(agent_args or [])
    if any(a in INFORMATIONAL_FLAGS for a in args):
        return False
    # A subcommand has to come first; anything later is a flag's value or
    # part of the prompt, and a prompt *is* a conversation.
    return not (args and args[0] in AGENT_SUBCOMMANDS)


def default_agent_args(config, attached):
    """The tail to pass when the caller gave no `--` of their own.

    `attached` is state's "has an agent ever run here" flag: --continue is
    only meaningful once it's True. On a first attach we also *strip* a
    --continue the config asked for -- `resumeArgs: --continue` is the
    natural thing to write in repos.yaml, and it shouldn't turn the very
    first launch into an immediate exit 1."""
    args = config.get("resumeArgs") or []
    if isinstance(args, str):
        # `resumeArgs: --continue` is the obvious thing to write, and
        # splitting a string into characters would be a baffling failure.
        # One flag, no quoting rules.
        args = args.split()
    args = list(args)
    if not attached:
        return [a for a in args if a not in CONTINUE_FLAGS]
    if any(a in SESSION_FLAGS for a in args):
        return args
    return ["--continue"] + args


def refresh(name, run=subprocess.run, env=None):
    """Re-point the host remotes at the daemon's current host port.
    Best-effort: a sandbox you can't fetch from is still a sandbox you
    can work in, so this never blocks the attach."""
    try:
        state = state_mod.load(name, env=env)
    except state_mod.StateError as e:
        print(f"warning: {e}", file=sys.stderr)
        return None
    if state is None:
        # Created by plain `sbx create`, or its state was pruned. Nothing
        # to re-point, and nothing wrong with that.
        return None
    port = create_mod.refresh_host_port(name, state, run=run, env=env)
    if port is None:
        print(
            f"warning: no published host port found for {name!r}; the "
            f"`{remotes_mod.remote_name_for(name)}` remotes will not fetch. "
            f"Try `wmf-sbx ports {name} --publish "
            f"{state.get('daemonPort') or 9977}`.",
            file=sys.stderr,
        )
    return port


def load_state(name, env=None):
    """The sandbox's state, or None (with a warning) if it can't be read.
    A sandbox made by plain `sbx create` has none, and that's fine -- it
    just means no remotes to re-point and no attach history to consult."""
    try:
        return state_mod.load(name, env=env)
    except state_mod.StateError as e:
        print(f"warning: {e}", file=sys.stderr)
        return None


def set_attached(name, value=True, env=None):
    """Record whether a conversation exists in this sandbox, which is what
    decides the next resume's --continue. Re-read rather than reusing the
    state main() started with: refresh() has since rewritten hostPort and
    the remote list, and this must not clobber that."""
    state = load_state(name, env=env)
    if state is None or bool(state.get("attached")) == value:
        return
    state["attached"] = value
    try:
        state_mod.save(state, env=env)
    except OSError as e:
        # Losing the flag costs one missing --continue next time. Not
        # worth failing an attach that already succeeded.
        print(f"warning: could not record the attach state ({e}).", file=sys.stderr)


def build_run_command(name, agent_args):
    cmd = [create_mod.WMF_SBX, "--upstream", "run", "--name", name]
    if agent_args:
        cmd += ["--"] + list(agent_args)
    return cmd


def start_and_restore(name, no_restore=False, no_remotes=False, dry_run=False,
                       run=subprocess.run, env=None, claude_md=True):
    """Everything a resume needs to do to the *sandbox* before an agent
    ever gets attached: start the container, put its mount layout back,
    re-point the `<name>` remotes at wherever the daemon came up.
    No `wmf-sbx run`, no agent -- shared by `wmf-sbx-resume` (which goes
    on to attach one) and `wmf-sbx-start` (which deliberately stops here,
    see that module's docstring for why). Returns whether the container
    came up; False only means `wmf-sbx exec ... true` itself failed.

    `--upstream`: without it, since sbx/NOTES.md "wmf-sbx redirects",
    this would reach `wmf-sbx-exec` instead of the real `sbx exec` --
    which itself calls back into start_and_restore, recursing forever.

    claude_md=False skips the CLAUDE.md edit report. `wmf-sbx exec`
    passes it: a one-off command should not cost three more execs, and
    the startup step does the edit anyway."""
    start = [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "true"]
    if dry_run:
        print("+ " + " ".join(start), file=sys.stderr)
        if not no_restore:
            print(f"+ (would wait for {name}'s mount layout, and "
                  f"re-apply it if it didn't come up)", file=sys.stderr)
        print(f"+ (would run {name}'s startup commands if the container "
              f"start skipped them, and start its git daemon if nothing "
              f"listens)", file=sys.stderr)
        if claude_md:
            print(f"+ (would edit {name}'s workspace CLAUDE.md)", file=sys.stderr)
        if not no_remotes:
            print(f"+ (would re-point the {remotes_mod.remote_name_for(name)} "
                  f"remotes)", file=sys.stderr)
        return True

    print("+ " + " ".join(start), file=sys.stderr)
    if not create_mod.start_sandbox(name, run=run):
        return False
    # Before everything below: the mount restore, the CLAUDE.md edit and
    # the daemon the remotes point at are all startup commands, and a
    # container that came up to serve an `sbx exec` can have skipped them
    # (sbx/NOTES.md §97).
    create_mod.ensure_startup_ran(name, run=run)
    if not no_restore:
        # Before the remotes and before the agent: the clones' borrowed
        # objects are unreachable until this holds (sbx/NOTES.md §40), so a
        # fetch or a `git log` in between would see a five-commit repo.
        # Announced because it is silent when it works, and an unannounced
        # silent step is indistinguishable from one that didn't run (§41).
        #
        # Wait first, restore second. The container start above set the
        # startup dispatcher going, and it restores the layout itself
        # (§46) -- just not before the exec that started it returns. Two
        # concurrent `--restore` passes would be doing `mount --move` at
        # the same paths.
        print(f"+ (waiting for {name}'s mount layout)", file=sys.stderr)
        if not create_mod.wait_for_sandbox_mounts(name, run=run):
            print(f"+ (re-applying {name}'s mount layout)", file=sys.stderr)
            create_mod.restore_sandbox_mounts(name, run=run)
    # On every start, not only after a create: an sbx upgrade can change
    # the text the edits apply to, and this is where the engineer sees
    # that they no longer do (sbx/NOTES.md §95).
    if claude_md:
        create_mod.amend_workspace_claude_md(name, run=run)
    # After the startup commands, and before the remotes that need it: a
    # daemon backgrounded inside an `sbx exec` does not outlive it (§97).
    create_mod.ensure_git_daemon(name, run=run)
    if not no_remotes:
        refresh(name, run=run, env=env)
    return True


def main(argv=None, run=subprocess.run, env=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ours, agent_args = split_agent_args(argv)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="Sandbox to re-attach to")
    parser.add_argument("--config", default=resolve_mod.DEFAULT_CONFIG)
    parser.add_argument(
        "--no-remotes", action="store_true",
        help="Skip the host-remote re-point; just start and attach",
    )
    parser.add_argument(
        "--no-continue", action="store_true",
        help="Start a fresh conversation instead of defaulting to --continue",
    )
    parser.add_argument(
        "--no-restore", action="store_true",
        help="Skip re-applying the sandbox's mount layout after the start",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would run; start nothing, change nothing",
    )
    args = parser.parse_args(ours)

    original_name = args.name
    try:
        args.name = state_mod.resolve_name_arg(args.name, env=env)
        state_mod.validate_name(args.name)
    except state_mod.StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.name != original_name:
        print(f"+ resolved {original_name!r} to sandbox {args.name!r}", file=sys.stderr)

    state = load_state(args.name, env=env)
    attached = bool(state and state.get("attached"))

    # Whether the --continue about to be passed is one we chose. Only ours
    # is ours to retract if claude has nothing to continue (see below).
    ours_continue = False
    if agent_args is None:
        config = resolve_mod.load_config(args.config)
        agent_args = default_agent_args(
            config, attached and not args.no_continue
        )
        ours_continue = any(a in CONTINUE_FLAGS for a in agent_args)

    cmd = build_run_command(args.name, agent_args)
    if args.dry_run:
        start_and_restore(
            args.name, no_restore=args.no_restore, no_remotes=args.no_remotes,
            dry_run=True, run=run, env=env,
        )
        print("+ " + " ".join(cmd), file=sys.stderr)
        return 0

    if not start_and_restore(
        args.name, no_restore=args.no_restore, no_remotes=args.no_remotes,
        run=run, env=env,
    ):
        return 1

    print("+ " + " ".join(cmd), file=sys.stderr)
    # Inherit stdio: this is the interactive attach, not a captured call.
    # SSH_AUTH_SOCK is stripped by the wmf-sbx wrapper itself.
    returncode = run(cmd).returncode

    if returncode != 0 and ours_continue:
        # "No conversation found to continue" -- claude exits 1 and the
        # engineer is left at a shell, having asked for none of this. The
        # flag was ours to add, so the recovery is ours too: forget it and
        # start the session they wanted. Only once, and only when *we*
        # added the flag; a --continue the caller typed is theirs to fix.
        print(
            "\nwarning: that looks like a --continue with no conversation to "
            "continue; forgetting this sandbox's attach flag and starting a "
            "fresh session.",
            file=sys.stderr,
        )
        set_attached(args.name, False, env=env)
        agent_args = [a for a in agent_args if a not in CONTINUE_FLAGS]
        cmd = build_run_command(args.name, agent_args)
        print("+ " + " ".join(cmd), file=sys.stderr)
        returncode = run(cmd).returncode

    if returncode == 0 and starts_a_conversation(agent_args):
        # Only on a clean exit, and only for a tail that could have started
        # a conversation. A run that died on the way in (sandbox gone, kit
        # broken) started none, `-- --version` started none either, and a
        # sticky `attached` would make every later resume ask claude to
        # continue one that doesn't exist -- the exact failure this flag
        # prevents, and the one it caused (sbx/NOTES.md §43).
        set_attached(args.name, True, env=env)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
