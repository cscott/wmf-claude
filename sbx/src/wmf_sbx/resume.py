#!/usr/bin/env python3
"""Start a Claude Code session in a sandbox (lima-port/HANDOFF-LIMA.md §8,
contained mode).

Usage:
  wmf-sbx resume [--[no-]continue] NAME [-- [LAUNCHER-FLAG ... --] CLAUDE-ARG ...]

Starts the VM if it is stopped and checks the invariants, then runs
upstream's bin/claude in the VM as the agent, in the primary clone, under
nono (session.launcher_argv). After the first `--`, the words are
bin/claude's, by its own rule: those before a second `--` are its flags
(LAUNCHER-FLAGs, e.g. `--allow-post=HOST`, `--local-db` or
`--allow PATH`), and the rest are Claude Code's. Without a second `--`,
they are all Claude Code's: `wmf-sbx resume NAME -- --allow-post=HOST --`
continues the conversation with one more grant. A `create --sudo`
sandbox runs Claude Code with no bin/claude and no nono, so it takes no
LAUNCHER-FLAGs.

`--continue` passes Claude Code `--continue`, and `--no-continue` starts
a new conversation. With neither, a sandbox that has had a conversation
gets `--continue` when there are no CLAUDE-ARGs. The sandbox has had one
once an interactive session in it has exited cleanly; the first session
that does not (a closed terminal, a host that slept) leaves that unknown,
and `--continue` then picks it up.

The credential stays on the host (track L, credproxy.py): $ANTHROPIC_API_KEY,
$CLAUDE_CODE_OAUTH_TOKEN, the file ~/.config/wmf-sbx/claude-oauth-token
(a token from `claude setup-token`, mode 0600), or the host's own Claude
Code login. For the length of the session, a proxy on the host's loopback
puts it into Claude Code's requests; the VM gets a phantom. Without a
credential, Claude Code asks you to log in, and that login stays in this
VM.
"""

import argparse
import os
import sys

from . import cli as cli_mod
from . import color as color_mod
from . import credproxy as credproxy_mod
from . import egress as egress_mod
from . import image as image_mod
from . import lima as lima_mod
from . import nested as nested_mod
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
PRINT_FLAGS = ("-p", "--print")
AGENT_SUBCOMMANDS = frozenset(
    "agents attach auth auto-mode doctor gateway import install logs mcp "
    "plugin plugins project respawn rm setup-token stop kill ultrareview "
    "update upgrade".split()
)


def split_launcher(rest):
    """(bin/claude's flags, Claude Code's args), from the words after the
    first `--`, by bin/claude's own rule: its flags are the ones before a
    `--`, and without one every word is Claude Code's."""
    if "--" in rest:
        i = rest.index("--")
        return list(rest[:i]), list(rest[i + 1:])
    return [], list(rest)


def starts_a_conversation(claude_args):
    """Whether this tail hands over to a conversation, which is what the
    `attached` flag records. `-- --version` or `-- mcp list` does not, and
    marking the sandbox attached for it makes the next resume pass
    --continue to a Claude with nothing to continue, which exits 1. Nor
    does `-p`: `--continue` does not take up a print-mode run (RAN: "No
    conversation found to continue" after only `-p` runs)."""
    args = list(claude_args or [])
    if any(a in INFORMATIONAL_FLAGS + PRINT_FLAGS for a in args):
        return False
    return not (args and args[0] in AGENT_SUBCOMMANDS)


def claude_args_for(cont, claude_args, attached):
    """Claude Code's args. `cont` is --continue (True), --no-continue
    (False), or neither (None): then --continue once a conversation
    exists, if the caller gave no args. An arg that already picks the
    conversation (`-c`, `--resume ID`) gets no second one."""
    claude_args = list(claude_args)
    if cont is None:
        cont = attached and not claude_args
    if cont and not any(a in SESSION_FLAGS or a.startswith("--resume=") for a in claude_args):
        return ["--continue"] + claude_args
    return claude_args


def set_attached(name, env=None):
    state = state_mod.load(name, env=env)
    if state is None or state.get("attached"):
        return
    state["attached"] = True
    try:
        state_mod.save(state, env=env)
    except OSError as e:
        print(f"warning: could not record the attach state ({e}).", file=sys.stderr)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="wmf-sbx resume", description="Start a Claude Code session in a sandbox.",
        epilog="After the first --, the words are bin/claude's: the ones before a second "
               "-- are its flags (--allow-post=HOST, --local-db, --allow PATH, ...), the "
               "rest are Claude Code's. With no second --, they are all Claude Code's: "
               "`wmf-sbx resume NAME -- --allow-post=HOST --` continues the conversation "
               "with one more grant.")
    parser.add_argument("--continue", dest="continue", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="continue the last conversation, or (--no-continue) start a new "
                             "one (default: continue once the sandbox has had one, when no "
                             "CLAUDE-ARGs are given)")
    parser.add_argument("name", help="the sandbox (or a path shortcut such as .)")
    parser.add_argument("rest", nargs=argparse.REMAINDER, metavar="ARGS",
                        help="[LAUNCHER-FLAG ... --] CLAUDE-ARG ...")
    return parser


def main(argv=None, lima=None, env=None):
    parser = build_parser()
    args, _config = cli_mod.parse_args(parser, argv, "resume")
    launcher_flags, claude_args = split_launcher(args.rest)
    # resume's --local-web= opens the wiki's and the browsers' ports, and
    # bin/claude keeps the last --local-web= it is given.
    if any(f.startswith("--local-web=") for f in launcher_flags):
        parser.error(f"--local-web=PORTS would replace the ports a session already has "
                     f"({session_mod.local_ports()}); `--open-port=PORT` opens one more")
    lima = lima or lima_mod.Limactl()
    try:
        name, state = vm_mod.resolve(args.name, env=env)
        if state.get("sudo") and launcher_flags:
            print(color_mod.error(
                f"error: {launcher_flags[0]}: {name} is a sudo sandbox, which runs Claude "
                f"Code with no bin/claude and no nono, so it takes no launcher flags. Its "
                f"network is the host egress proxy's: `wmf-sbx allow {name} HOST` adds a "
                f"host; local ports need no flag."), file=sys.stderr)
            return 2
        vm_mod.ensure_running(name, lima=lima, env=env,
                              log=lambda m: print(color_mod.dim(m), file=sys.stderr))
        if not start_mod.check(name, lima=lima, env=env):
            print(color_mod.error(f"error: {name} breaks a security invariant; no session "
                                  f"is started. `wmf-sbx rm {name}` and create it again."),
                  file=sys.stderr)
            return 1
        if state.get("nested"):
            nested_mod.stop_inner(name, lima=lima,
                                  log=lambda m: print(color_mod.dim(m), file=sys.stderr))
        source = credproxy_mod.host_source(env)
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError,
            credproxy_mod.CredentialError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    if source is None:
        print(color_mod.dim(
            "note: no Claude credential on the host ($ANTHROPIC_API_KEY, "
            f"$CLAUDE_CODE_OAUTH_TOKEN, {credproxy_mod.token_file(env)}, or a Claude Code "
            "login). Run `wmf-sbx login` once for all sandboxes; for now, Claude Code will "
            "ask you to log in, in this VM only."), file=sys.stderr)
    claude_args = claude_args_for(getattr(args, "continue"), claude_args,
                                  state.get("attached"))
    proxy = None
    try:
        cred_path = None
        if source is not None:
            proxy, cred_path = start_proxy(name, source, lima=lima, env=env,
                                           egress=egress_mod.enabled(state))
            port = egress_mod.CRED_PORT if proxy.port is None else proxy.port
            if not state.get("sudo"):
                launcher_flags = list(launcher_flags) + [f"--open-port={port}"]
        argv = session_mod.launcher_argv(
            state, cred_path=cred_path, launcher_flags=launcher_flags,
            claude_args=claude_args, proxy=session_mod.upstream_proxy(env, state))
        print(color_mod.dim(f"+ claude in {vm_mod.instance_name(name)}, as the agent, in "
                            f"{state['primaryDir']}"), file=sys.stderr)
        result = vm_mod.shell(name, argv, lima=lima, workdir="/", check=False,
                              capture=False, interactive=True)
    except (lima_mod.LimaError, session_mod.SessionError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    finally:
        if proxy is not None:
            stop_proxy(name, proxy, lima=lima)
    if result.returncode == 0 and starts_a_conversation(claude_args):
        set_attached(name, env=env)
        session_mod.mark_first_run_seen(env)
    return result.returncode


def proxy_log_path(name, env=None):
    return os.path.join(state_mod.state_dir(env), f"{name}.credproxy.log")


def start_proxy(name, source, lima=None, env=None, egress=False):
    """Start the session's credential proxy on the host, open the guest's
    host block to its port, and pass the phantom and the proxy's address
    into the VM. Returns (proxy, the credential file's path in the VM).

    Behind the egress proxy (`egress`), the guest cannot reach the host's
    loopback (restrict=on): the proxy listens on the sandbox's Unix
    socket, which the QEMU wrapper's guestfwd at EGRESS_ADDR:CRED_PORT
    connects to, and the guest's nftables already let that through
    (sandbox-provision.sh)."""
    problem = credproxy_mod.check_source(source)
    if problem:
        print(color_mod.error(f"warning: {problem}"), file=sys.stderr)
    proxy = credproxy_mod.CredentialProxy(
        source, env=env, ca_files=image_mod.extra_ca_files(env),
        log_path=proxy_log_path(name, env))
    if egress:
        # One socket per sandbox (the guestfwd is fixed when QEMU starts):
        # a second session would take it over, and the first one's
        # phantom would then be refused.
        path = egress_mod.cred_socket(name, env)
        if credproxy_mod.socket_in_use(path):
            raise session_mod.SessionError(
                f"another session runs in {name}; behind the egress proxy, a sandbox "
                f"has one session at a time")
        try:
            proxy.start(unix_path=path)
        except OSError as e:
            raise session_mod.SessionError(
                f"could not start the credential proxy on {path} ({e})") from None
        try:
            return proxy, session_mod.put_credential(name, proxy.session_env(
                base_url=f"http://{egress_mod.EGRESS_ADDR}:{egress_mod.CRED_PORT}"),
                lima=lima)
        except BaseException:
            proxy.stop()
            raise
    proxy.start()
    try:
        # A rule whose port has no listener on the host was left by a
        # resume that did not end cleanly; remove it with ours added.
        res = vm_mod.shell(name, credproxy_mod.guest_rule_ports_argv(), lima=lima,
                           check=False)
        stale = [p for p in (res.stdout or "").split()
                 if p.isdigit() and not credproxy_mod.port_in_use(int(p))]
        res = vm_mod.shell(name, credproxy_mod.guest_rules_argv(add=[proxy.port],
                                                                remove=stale),
                           lima=lima, check=False)
        if res.returncode != 0:
            raise session_mod.SessionError(
                f"could not open the VM's host block to the credential proxy "
                f"(exit {res.returncode})")
        return proxy, session_mod.put_credential(name, proxy.session_env(), lima=lima)
    except BaseException:
        stop_proxy(name, proxy, lima=lima)
        raise


def stop_proxy(name, proxy, lima=None):
    """Close the guest's host block to the proxy's port again, then stop it."""
    if proxy.port is None:   # on the egress Unix socket: no rule
        proxy.stop()
        return
    try:
        vm_mod.shell(name, credproxy_mod.guest_rules_argv(remove=[proxy.port]),
                     lima=lima, check=False)
    except lima_mod.LimaError:
        pass   # the next resume removes the rule (no listener)
    proxy.stop()


if __name__ == "__main__":
    sys.exit(main())
