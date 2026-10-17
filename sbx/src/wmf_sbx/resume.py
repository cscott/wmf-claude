#!/usr/bin/env python3
"""Start a Claude Code session in a sandbox (lima-port/HANDOFF-LIMA.md §8;
README.md, "Modes").

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
sandbox starts Claude Code with bin/launch-claude.sh, so it takes no
LAUNCHER-FLAGs.

`--continue` passes Claude Code `--continue`, and `--no-continue` starts
a new conversation. With neither, and no CLAUDE-ARGs, `--continue` is
passed when Claude Code has a saved conversation in the primary
workspace: a transcript in the agent's ~/.claude/projects/
(has_conversation). Claude Code saves one only after a message, so a
session exited at once leaves none. If the check fails, the sandbox's
`attached` flag decides: set when an interactive session exited
cleanly.

The credential stays on the host (track L, credproxy.py): $ANTHROPIC_API_KEY,
$CLAUDE_CODE_OAUTH_TOKEN, the file ~/.config/wmf-sbx/claude-oauth-token
(a token from `claude setup-token`, mode 0600), or the host's own Claude
Code login. For the length of the session, a proxy on the host's loopback
puts it into Claude Code's requests; the VM gets a phantom. Without a
credential, Claude Code asks you to log in, and that login stays in this
VM.

A sandbox with the auth proxy (authproxy.py) uses none of these: its
login is the shared one, which `/login` in any such sandbox made; the
host egress proxy keeps it, and the session syncs the phantoms and takes
part in its refresh (authproxy.Coordinator).

In a sandbox made with native_copy (behind the egress proxy, and no
`session: {native_copy: false}` in settings.yaml), a mouse selection in
Claude Code goes to the host's clipboard (clipboard.py): for the length
of the session, a listener on the host gives it to wl-copy, xclip or
pbcopy. `native_copy: false` in settings.yaml turns it off for the
sandboxes made before, too.
"""

import argparse
import importlib.util
import os
import sys

from . import authproxy as authproxy_mod
from . import cli as cli_mod
from . import clipboard as clipboard_mod
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


def claude_context():
    """lima/claude-context.py as a module: upstream's copy of Claude
    Code's rules for its project directories. Its name has a `-`, so it
    is loaded from its path."""
    global _CLAUDE_CONTEXT
    if _CLAUDE_CONTEXT is None:
        path = os.path.join(image_mod.REPO_ROOT, "lima", "claude-context.py")
        spec = importlib.util.spec_from_file_location("wmf_sbx_claude_context", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLAUDE_CONTEXT = module
    return _CLAUDE_CONTEXT


_CLAUDE_CONTEXT = None


def project_dir(cwd):
    """Claude Code's transcript directory for a working directory:
    ~/.claude/projects/ and the path with every character but a letter or
    a digit made `-` (RAN: /home/cananian/Projects/Wikimedia/Extensions/
    Translate is -home-cananian-Projects-Wikimedia-Extensions-Translate).
    A name over 200 characters is cut and hashed (claude-context.py enc,
    checked by upstream against a directory Claude Code made)."""
    return f"{vm_mod.AGENT_HOME}/.claude/projects/{claude_context().enc(cwd)}"


def has_conversation(name, cwd, lima):
    """True when the agent has a saved conversation in `cwd` (a transcript
    in project_dir), False when it has none, None when the check failed.
    Claude Code saves one only once a message was sent: a session that
    ended cleanly with none leaves nothing to --continue (RAN 2026-10-15:
    "No conversation found to continue" after a create that started a
    session that was exited at once)."""
    try:
        res = vm_mod.shell(name, vm_mod.agent_argv(
            ["sh", "-c", 'for f in "$1"/*.jsonl; do [ -s "$f" ] && exit 0; done; exit 1',
             "_", project_dir(cwd)], workdir="/tmp"), lima=lima, check=False)
    except lima_mod.LimaError:
        return None
    return {0: True, 1: False}.get(res.returncode)


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
                             "one (default: continue when the sandbox has a saved "
                             "conversation and no CLAUDE-ARGs are given)")
    parser.add_argument("name", help="the sandbox (or a path shortcut such as .)")
    parser.add_argument("rest", nargs=argparse.REMAINDER, metavar="ARGS",
                        help="[LAUNCHER-FLAG ... --] CLAUDE-ARG ...")
    return parser


def main(argv=None, lima=None, env=None):
    parser = build_parser()
    args, config = cli_mod.parse_args(parser, argv, "resume")
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
                f"error: {launcher_flags[0]}: {name} is a sudo sandbox, which starts Claude "
                f"Code with bin/launch-claude.sh, so it takes no bin/claude flags. Its "
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
        # Not in a sudo sandbox: its sessions have no nono, so a later one
        # can stop an inner VM itself, and stop_inner cannot tell that a
        # session runs (it looks for the agent's nono).
        if state.get("nested") and not state.get("sudo"):
            nested_mod.stop_inner(name, lima=lima,
                                  log=lambda m: print(color_mod.dim(m), file=sys.stderr))
        auth = authproxy_mod.enabled(state)
        source = None if auth else credproxy_mod.host_source(env)
        if not auth:
            credproxy_mod.upstream_url(env)
    except (state_mod.StateError, vm_mod.VmError, lima_mod.LimaError,
            credproxy_mod.CredentialError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1
    if source is None and not auth:
        print(color_mod.dim(
            "note: no Claude credential on the host ($ANTHROPIC_API_KEY, "
            f"$CLAUDE_CODE_OAUTH_TOKEN, {credproxy_mod.token_file(env)}, or a Claude Code "
            "login). Run `wmf-sbx login` once for all sandboxes; for now, Claude Code will "
            "ask you to log in, in this VM only."), file=sys.stderr)
    attached = state.get("attached")
    if getattr(args, "continue") is None and not claude_args and state.get("primaryDir"):
        found = has_conversation(name, state["primaryDir"], lima)
        attached = attached if found is None else found
    claude_args = claude_args_for(getattr(args, "continue"), claude_args, attached)
    proxy = clip = syncer = None
    try:
        cred_path = None
        if auth:
            syncer = authproxy_mod.Coordinator(
                name, lima=lima, env=env,
                log=lambda m: print(color_mod.warning(m), file=sys.stderr))
            syncer.start()
            if authproxy_mod.read_host(env) is None:
                print(color_mod.dim(
                    "note: no shared login yet. Run /login in this session once; the other "
                    "auth-proxy sandboxes then use it too."), file=sys.stderr)
        elif source is not None:
            proxy, cred_path = start_proxy(name, source, lima=lima, env=env,
                                           egress=egress_mod.enabled(state),
                                           nested=bool(state.get("nested")))
            # No --open-port for its port: nono 0.80 opens the loopback only;
            # the session reaches the proxy through nono's (launcher_argv).
        if state.get("nativeCopy") and session_mod.native_copy(config.get("session"),
                                                               egress_mod.enabled(state)):
            clip = start_clipboard(name, env)
        argv = session_mod.launcher_argv(
            state, cred_path=cred_path, launcher_flags=launcher_flags,
            claude_args=claude_args, proxy=session_mod.upstream_proxy(env, state),
            host_env=env)
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
        if clip is not None:
            clip.stop()
        if syncer is not None:
            syncer.stop()
    if result.returncode == 0 and starts_a_conversation(claude_args):
        set_attached(name, env=env)
        session_mod.mark_first_run_seen(env)
    report_uncommitted(name, state, lima=lima)
    return result.returncode


def report_uncommitted(name, state, lima=None):
    """After a session: the files the agent left uncommitted, which no
    fetch brings to the host. Silent when there are none."""
    result = vm_mod.uncommitted(name, state.get("repos") or [], lima=lima)
    if not result or not (result[0] or result[1]):
        return
    print(color_mod.warning(f"note: uncommitted files in {name}. A fetch brings "
                            f"commits only; ask the agent to commit them:"), file=sys.stderr)
    for line in vm_mod.uncommitted_report(*result):
        print(color_mod.dim(line), file=sys.stderr)


def proxy_log_path(name, env=None):
    return os.path.join(state_mod.state_dir(env), f"{name}.credproxy.log")


def clipboard_log_path(name, env=None):
    return os.path.join(state_mod.state_dir(env), f"{name}.clipboard.log")


def start_clipboard(name, env=None):
    """Start the session's clipboard listener on the sandbox's socket,
    which the guestfwd at EGRESS_ADDR:CLIP_PORT connects to. None, with a
    note, when the host has no tool for it or another session has it: the
    session starts all the same, and Claude Code's copies go nowhere."""
    listener = clipboard_mod.Listener(egress_mod.clip_socket(name, env),
                                      os.environ if env is None else env,
                                      log_path=clipboard_log_path(name, env))
    if not listener.available():
        print(color_mod.dim("note: no wl-copy (Wayland), xclip (X11) or pbcopy on this host's "
                            "display, so a copy in Claude Code does not reach its clipboard."),
              file=sys.stderr)
        return None
    if credproxy_mod.socket_in_use(listener.path):
        print(color_mod.warning(f"warning: another session in {name} has the clipboard; "
                                f"this one's copies go nowhere."), file=sys.stderr)
        return None
    try:
        listener.start()
    except OSError as e:
        print(color_mod.warning(f"warning: could not start the clipboard listener on "
                                f"{listener.path} ({e})"), file=sys.stderr)
        return None
    return listener


def start_proxy(name, source, lima=None, env=None, egress=False, nested=False):
    """Start the session's credential proxy on the host, open the guest's
    host block to its port, and pass the phantom and the proxy's address
    into the VM (and, `nested`, to its inner sandboxes). Returns (proxy,
    the credential file's path in the VM).

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
                lima=lima, nested=nested)
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
        return proxy, session_mod.put_credential(name, proxy.session_env(), lima=lima,
                                                 nested=nested)
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
