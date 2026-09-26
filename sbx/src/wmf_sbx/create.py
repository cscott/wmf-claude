#!/usr/bin/env python3
"""Layer A: a single-command clean-checkout `sbx create` launcher.

Resolves each repo argument (bare name, full Gerrit/GitLab path, or
scheme-prefixed path -- see wmf_sbx/resolve.py) to a local directory,
cloning it fresh from Gerrit/GitLab if no local checkout exists yet (a
plain `git clone`, so it lands on the upstream default branch with no
carried-over WIP), then runs `sbx create` with every repo at its literal
host path -- every extra marked `:ro`, the primary read-write since `sbx
create`'s primary-workspace positional is unconditionally so (confirmed
against a real sbx binary -- see sbx/DESIGN-parallel-clone-tree.md). There's
no `--clone` split left to ask sbx for: the generated kit's
`wmf-sbx-setup` install step (see wmf_sbx/kit.py and wmf_sbx/setup.py)
does the writable-cloning itself, once inside the sandbox -- a private,
writable `--reference` clone for every repo by default, or a plain
read-only bind mount instead for any repo argument ending in `:ro`.

Note the asymmetry: *every* extra is mounted read-only, whether or not
the user suffixed it, because nothing should ever write to a
host-mirrored original -- the writable copy is always the parallel-tree
one. A user's `:ro` only picks bind-mount-instead-of-clone for that
parallel copy. Read-only is enforced at create time rather than by
`wmf-sbx-setup`'s remount because only the former holds: the sandboxed
agent has passwordless sudo and can `sudo mount -o remount,rw` its way
out of a remount (confirmed live; see docker/sbx-releases#556 and
sbx/NOTES.md #22), whereas sbx backs a create-time `:ro` read-only a
layer down, where the sandbox can't reach. The remount is kept anyway,
as an accident guard and for the parallel-tree mounts. The whole parallel tree is also served by a single git
daemon wmf-sbx-setup starts on a fixed sandbox-internal port (see
wmf_sbx_kit.DEFAULT_DAEMON_PORT), published to an auto-assigned host port
after creation via `wmf-sbx ports NAME --publish PORT` -- `sbx create` has
no create-time publish flag of its own (confirmed against a real `sbx`
binary; the docs.docker.com mirror describes one anyway -- see
sbx/DESIGN-parallel-clone-tree.md §3, §6). See publish_daemon_port and
lookup_published_host_port. This doesn't yet add the resulting
`git remote add` itself to any host repo (stage-1 limitation, see the
design doc) -- it just prints the command to run, alongside the
`wmf-sbx run --name NAME` reminder.

Doesn't auto-run the new sandbox -- prints the `wmf-sbx run --name NAME` command
to run it instead (see print_run_reminder).

--name is optional; the default is derived from the primary repo (see
default_sandbox_name). If that name is already in use by an existing
sandbox (per `wmf-sbx ls`, any status), the user is prompted for a
different one -- see unique_sandbox_name.

Unless --kit is given (or 'kit:' is set in the config), a MediaWiki kit
is generated on the fly into a temporary directory and passed as --kit --
see wmf_sbx/kit.py and sbx/DESIGN-kit-generation.md. --kit-out DIR puts
it somewhere permanent instead, for `sbx kit validate`.

The Phabricator and Gerrit MCP servers are registered on the *host*
(`sbx mcp add`), not in the sandbox, so the credentials they carry stay
out of the agent's reach; the generated kit only gets a proxy that
forwards an allowlist of read-only tools through sbx's MCP gateway. See
host_mcp_plan/ensure_host_mcp_servers below, wmf_sbx/kit.py's
write_mcp_files, and sbx/SECURITY.md §7. --no-mcp turns the whole
arrangement off.

Usage:
  wmf-sbx-create [--name NAME] [--kit PATH] [--kit-out DIR]
                 [--config PATH] [--no-mcp] [--dry-run]
                 PRIMARY[:ro] [EXTRA[:ro] ...]
"""

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import color as color_mod
from . import deps as deps_mod
from . import kit as kit_mod
from . import remotes as remotes_mod
from . import resolve as resolve_mod
from . import settings as settings_mod
from . import setup as setup_mod
from . import state as state_mod

CLONE_URL_BUILDERS = {
    "gerrit": lambda path: f"https://gerrit.wikimedia.org/r/{path}",
    "gitlab": lambda path: f"https://gitlab.wikimedia.org/{path}.git",
}


class LaunchError(Exception):
    """A user-facing failure building or running the sbx command."""


def clone_url(canonical):
    scheme, path = resolve_mod.split_scheme(canonical)
    try:
        builder = CLONE_URL_BUILDERS[scheme]
    except KeyError:
        raise LaunchError(f"Don't know how to clone {scheme!r} repos.") from None
    return builder(path)


def host_upstream_url(host_dir, run=subprocess.run):
    """The URL the host's own checkout at `host_dir` already has configured
    as `origin` -- or None if it isn't a git repo, has no `origin`, or that
    `origin` isn't something the sandbox can actually fetch.

    This is upstream_plan's fallback for a repo whose canonical name isn't
    known -- most commonly a raw filesystem-path argument (is_raw_path)
    that canonicals_for_kit couldn't identify, because it's a GitLab clone
    (no .gitreview -- that's Gerrit-only) with no exact repos.yaml rule
    either. Reading the host's own git config instead needs no
    forge-specific knowledge at all: it works for any already-cloned
    directory, whatever it was cloned from.

    Only an http(s) URL is used: the sandbox has no SSH agent by design
    (sbx/NOTES.md #8), so an ssh://, git@host:path, or bare local-path
    origin on the host -- all common for an engineer's own checkout --
    would just be unreachable from inside the sandbox. `local` (the host
    mirror, over the loopback git daemon) stays the fallback for those,
    same as for an unidentifiable repo."""
    try:
        result = run(
            ["git", "-C", host_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    return url


def upstream_plan(resolved, run=subprocess.run,
                  gitlab_upstream=resolve_mod.gitlab_upstream):
    """{local_dir: upstream_clone_url} for the repos we can name an upstream
    for, so the sandbox clone can call the real upstream remote `origin`
    and the host mirror `local` (sbx/DESIGN-setup-steps.md §8.1).

    Two ways to learn the upstream, tried in order: a resolved canonical
    (gerrit:/gitlab:) via clone_url() -- what do_clone would have cloned
    from -- or, failing that, whatever `origin` the host's own checkout
    already has configured (host_upstream_url). The second needs no
    project identification at all, so it covers any already-cloned
    directory clone_url()/canonical resolution can't name.

    A gitlab: canonical that is a fork is followed to its root project
    (gitlab_upstream), so `origin` in the sandbox is the real upstream and
    not the engineer's personal fork. If GitLab does not answer, the
    canonical's own URL is used (sbx/NOTES.md §100).

    A repo with neither is simply absent: the clone keeps the host mirror
    as `origin` and git-safe-reset falls back to it."""
    plan = {}
    for canonical, path in resolved:
        url = None
        if canonical is not None:
            try:
                if canonical.startswith("gitlab:"):
                    try:
                        canonical = "gitlab:" + gitlab_upstream(
                            canonical[len("gitlab:"):])
                    except resolve_mod.ResolutionError:
                        pass
                url = clone_url(canonical)
            except LaunchError:
                url = None
        if url is None:
            url = host_upstream_url(path, run=run)
        if url is not None:
            plan[path] = url
    return plan


def default_sandbox_name(primary_canonical):
    _, path = resolve_mod.split_scheme(primary_canonical)
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", path.rsplit("/", 1)[-1]).strip("-").lower()
    return f"sbx-{slug}" if slug else "sbx-sbx"


def existing_sandbox_names(run=subprocess.run):
    """Sandbox names `sbx ls` currently knows about, in any status -- a
    stopped sandbox still occupies its name (see wmf-sbx-ls in the repo
    root for a real example). Best-effort: if wmf-sbx/sbx isn't available
    (e.g. under test, or the daemon can't be reached), return an empty set
    rather than blocking sandbox creation on our own ability to check for
    a collision -- `sbx create` itself remains the final authority either
    way.

    `--json` rather than `-q`'s one-name-per-line, for the same reason as
    everywhere else here: a documented key is a contract, output shape is
    not (MEASURED §75.4). The payload carries `id`, `status`, `ports` and
    `workspaces` per sandbox too; only the names are wanted here.

    `stdin=DEVNULL` here and on every other non-attach `sbx` call in this
    file: MEASURED on the host, cananian, 2026-09-08 -- `sbx` occasionally
    has its own thing to ask (a client/server version-mismatch restart
    prompt was the one seen) on a call none of us expect to be
    interactive, and `capture_output=True` sends that prompt into a
    buffer nobody reads instead of the terminal. Without a `stdin`
    override the subprocess still has the real terminal on its stdin, so
    it hangs waiting for a keypress the user was never shown was
    expected. `DEVNULL` turns that into an immediate EOF, which is a
    fast, visible failure instead of a silent one. The two real
    `sbx create`/`sbx run` attaches (build_sbx_command's `run(cmd,
    env=env)` and wmf_sbx/resume.py's own) are exactly the calls that
    must NOT get this -- they're the actual interactive session."""
    try:
        result = run(
            [WMF_SBX, "--upstream", "ls", "--json"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    try:
        sandboxes = (json.loads(result.stdout or "") or {}).get("sandboxes")
    except ValueError:
        return set()
    if not isinstance(sandboxes, list):
        return set()
    return {s["name"] for s in sandboxes
            if isinstance(s, dict) and isinstance(s.get("name"), str)}


def supported_skills_flag(run=subprocess.run):
    """Which shared-agent-skills opt-out `sbx create` on THIS host accepts:
    `"--skills=off"`, `"--no-share-skills"`, or None if neither.

    The shared store is one host directory mounted into every sandbox at
    ~/.claude/skills, holding instructions a model reads and acts on --
    so a sandbox that can write it can inject into every other sandbox on
    the machine, including ones that don't exist yet, and the contents
    outlive `sbx rm` (MEASURED, sbx/NOTES.md §77.2-§77.4;
    sbx/SECURITY.md §7.6). We want no part of that channel in either
    direction: Route A (§64) ships our plugin as a per-kit copy under
    ~/.claude/plugins/, so nothing of ours writes the store and nothing
    of ours reads it. `off` -- not `readonly` -- is therefore the right
    mode for us: `readonly` protects the store from this sandbox, while
    `off` protects this sandbox from whatever another one planted (§81.1).

    Probed rather than inferred from a version number, because the
    question is precisely "does this binary accept this flag", and
    because the flag's history makes version arithmetic a trap: it was
    `--no-share-skills` (present but UNDOCUMENTED in 0.42.1's
    `create --help` -- MEASURED §79.6, reconciled in §81.1), is
    `--skills=off|readonly|readwrite` from 0.43.0-rc3, and the old
    spelling survives as a deprecated alias. Probing also means this
    function needs no edit on the day cananian upgrades: the same
    checkout does the right thing on both versions, so the upgrade and
    the recreate don't have to be sequenced against a code change.

    Returns None -- and the caller warns, loudly, once -- when neither
    flag is there. That is not a failure: it is 0.42.1's real state, and
    it's what wmf-sbx-setup's in-sandbox read-only remount exists to
    partially cover (§79.1: partially, because the agent is root in its
    own namespace and lifts it with one `sudo mount`).

    Best-effort, like existing_sandbox_names: if `sbx` can't be run at
    all we return None rather than blocking a create on our own ability
    to ask. `stdin=DEVNULL` for the reason spelled out there."""
    try:
        result = run(
            # --upstream: the real sbx's own --help, not wmf-sbx-create's
            # (sbx/NOTES.md "wmf-sbx redirects") -- this probes what the
            # installed sbx binary supports.
            [WMF_SBX, "--upstream", "create", "--help"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
    except OSError:
        return None
    # `--help` exits 0, but don't depend on it: a non-zero exit with
    # usable help text on stdout/stderr is still an answer, and a wrong
    # answer here silently drops a security flag.
    help_text = (result.stdout or "") + (result.stderr or "")
    if "--skills" in help_text:
        return "--skills=off"
    if "--no-share-skills" in help_text:
        return "--no-share-skills"
    return None


def unique_sandbox_name(name, existing, prompt=input):
    """If `name` collides with a name in `existing` (see
    existing_sandbox_names), ask the user to pick a different one -- see
    sbx/NOTES.md §19. An empty response accepts the first
    free auto-suffixed alternative ("name-2", "name-3", ...); the loop
    repeats -- re-suggesting a fresh suffix each time -- until whatever the
    user settles on doesn't collide."""
    while name in existing:
        suffix = 2
        suggestion = f"{name}-{suffix}"
        while suggestion in existing:
            suffix += 1
            suggestion = f"{name}-{suffix}"
        response = prompt(
            f"A sandbox named {name!r} already exists. Enter a different "
            f"name, or press Enter to use {suggestion!r}: "
        ).strip()
        name = response or suggestion
    return name


def split_ro_suffix(spec):
    """Splits a trailing ':ro' opt-out suffix off a repo argument -- see
    sbx/DESIGN-parallel-clone-tree.md §2. The suffix is stripped here so the
    rest of the pipeline (repo resolution, realpath'ing, nested-mount
    checks) sees a plain path.

    What the suffix still controls is only the *parallel tree* strategy:
    it's threaded through to the generated kit's wmf-sbx-setup install
    step (wmf_sbx_kit.build_kit_spec's readonly_dirs), which picks a
    read-only bind mount instead of a writable clone. It no longer decides
    whether the host-mirrored original is mounted read-only -- every extra
    is, unconditionally, at create time (see build_sbx_command).

    Returns (spec_without_suffix, is_ro)."""
    if spec.endswith(":ro"):
        return spec[: -len(":ro")], True
    return spec, False


def publish_daemon_port(name, sandbox_port, run=subprocess.run, quiet=False):
    """Publishes the parallel tree's git daemon (listening inside the
    sandbox on sandbox_port -- see wmf_sbx_kit.DEFAULT_DAEMON_PORT) to the
    host, via `wmf-sbx ports NAME --publish SANDBOX_PORT` -- see
    sbx/DESIGN-parallel-clone-tree.md §3. `sbx create` has no create-time
    publish flag against a real `sbx` binary, so this only runs after
    `sbx create` has already succeeded. Omitting a HOST_PORT prefix (just
    `--publish SANDBOX_PORT`) makes `sbx` pick an ephemeral host port
    itself, so there's no free-port bookkeeping to do here -- just read it
    back afterward via lookup_published_host_port. Returns that host port,
    or None if publishing or the lookup failed; best-effort, since the
    sandbox itself already exists by this point -- not worth failing the
    whole command over, just warn and let the user run `wmf-sbx ports`
    themselves if they need the remote URL.

    quiet suppresses that warning, for callers that expect the failure:
    against a *stopped* sandbox this always fails with `500 ... no
    container endpoint with IP address found`, and printing it before the
    caller starts the sandbox and succeeds on the retry makes a working
    command look broken (sbx/NOTES.md §37.1)."""
    result = run(
        [WMF_SBX, "--upstream", "ports", name, "--publish", str(sandbox_port)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        if quiet:
            return None
        print(
            f"warning: `wmf-sbx ports {name} --publish {sandbox_port}` failed "
            f"(exit {result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    return lookup_published_host_port(name, sandbox_port, run=run)


# Protocol strings a mapping we can dial over IPv4 may carry. sbx 0.39
# published dual-stack `tcp` (§36.3); 0.42.0 made `tcp4` the default for
# both `ports --publish` and kit-declared ports ("a published port no
# longer listens on ::1 unless you name the protocol explicitly"). Empty
# is accepted because the kit reference describes an omitted protocol as
# IPv4, and our kit omits it.
IPV4_PROTOCOLS = frozenset({"tcp", "tcp4", ""})


def ipv4_mapping(mapping, sandbox_port):
    """Whether this `ports --json` entry is one a `git://127.0.0.1:PORT/`
    URL can actually reach.

    Deliberately not an equality test on the protocol string: pinning
    `== "tcp"` would have silently stopped finding the daemon's port the
    day 0.42 started reporting `tcp4`, and the symptom (host remotes that
    don't fetch) is several steps from the cause. Anything with a colon in
    the host IP is IPv6 and is skipped -- our URLs name 127.0.0.1
    literally, so a `::1`-only mapping is no use to us."""
    if mapping.get("sandbox_port") != sandbox_port:
        return False
    if (mapping.get("protocol") or "") not in IPV4_PROTOCOLS:
        return False
    host_ip = mapping.get("host_ip") or ""
    if ":" in host_ip:
        return False
    # 0.0.0.0 (and an unset host IP) include the loopback the URL dials.
    return host_ip in ("127.0.0.1", "0.0.0.0", "")


def lookup_published_host_port(name, sandbox_port, run=subprocess.run):
    """The host port `wmf-sbx ports NAME --json` reports for sandbox_port
    on an IPv4 loopback-reachable mapping, or None if the query fails or
    no such mapping is (yet) listed -- see publish_daemon_port and
    ipv4_mapping. `--json` is only honored on this plain listing form, not
    combined with `--publish` (confirmed against a real `sbx` binary),
    hence the separate call."""
    result = run(
        [WMF_SBX, "--upstream", "ports", name, "--json"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    try:
        mappings = json.loads(result.stdout)
    except ValueError:
        return None
    # 127.0.0.1 first: under a dual-stack publish both halves match, and
    # the explicit loopback entry is the one we've measured.
    for wanted in ("127.0.0.1", None):
        for mapping in mappings:
            if not ipv4_mapping(mapping, sandbox_port):
                continue
            if wanted is None or mapping.get("host_ip") == wanted:
                return mapping.get("host_port")
    return None


def read_setup_status(name, run=subprocess.run, path=None):
    """The wmf-sbx-setup report from inside the sandbox, as a dict, or None
    if it can't be read. `path` selects which report: the create-time one
    by default, or the restart pass's (§41).

    `sbx exec` prints chrome of its own around the command's output
    ("Sandbox NAME started successfully"), so the JSON is located by
    brace rather than by assuming stdout is nothing else."""
    result = run(
        # --upstream: real `sbx exec`, not wmf-sbx-exec -- this runs
        # underneath start_sandbox/wait_for_daemon, so redirecting would
        # recurse (sbx/NOTES.md "wmf-sbx redirects").
        [WMF_SBX, "--upstream", "exec", name, "--", "cat",
         path or setup_mod.setup_status_path()],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout or ""
    start, end = stdout.find("{"), stdout.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        status = json.loads(stdout[start:end + 1])
    except ValueError:
        return None
    return status if isinstance(status, dict) else None


def report_setup_problems(name, run=subprocess.run, quiet=False,
                          status_path=None, label="setup", step="setup step",
                          consequence="the sandbox exists, but its MediaWiki "
                                      "setup is incomplete",
                          followup=None):
    """Print what the in-sandbox setup step reported, since `sbx create`
    itself won't: it collapses that whole step to a single `✓ python3
    /home/agent/wmf-sbx-setup ... (77.8s)` line, so every warning the
    script printed -- including the one for a `git safe-reset` that
    silently didn't run (sbx/NOTES.md §32.1) -- dies inside the sandbox.

    Returns the status dict, or None if there wasn't one. quiet drops the
    "couldn't read it" warning for callers that already know why (a failed
    `sbx create` may not have left a sandbox to exec into at all).
    followup, if given, replaces the closing "full log" line: a step with
    no log of its own says what to do instead."""
    status_path = status_path or setup_mod.setup_status_path()
    status = read_setup_status(name, run=run, path=status_path)
    if status is None:
        if not quiet:
            print(
                f"warning: could not read the {label} report from the sandbox "
                f"({status_path}) -- look for problems in "
                f"`wmf-sbx exec {name} cat {setup_mod.setup_log_path()}`.",
                file=sys.stderr,
            )
        return None
    problems = status.get("problems") or []
    exit_code = status.get("exit")
    if not problems and exit_code == 0:
        return status
    if exit_code:
        print(
            f"\nerror: the sandbox's {step} failed (exit {exit_code}); "
            f"{consequence}.",
            file=sys.stderr,
        )
    if problems:
        print(
            f"\n{len(problems)} problem(s) reported by the sandbox's {step} "
            f"itself:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
    if followup:
        print(f"  {followup}", file=sys.stderr)
    else:
        print(
            f"  (full log: wmf-sbx exec {name} cat "
            f"{status.get('log') or setup_mod.setup_log_path()})",
            file=sys.stderr,
        )
    return status


def start_sandbox(name, run=subprocess.run):
    """Start the sandbox if it isn't already running. `sbx exec` starts a
    stopped sandbox as a side effect (confirmed on the host -- it prints
    "Sandbox NAME started successfully"), and there is no `sbx start`.
    Returns True if the sandbox is up afterward.

    Worth doing before anything that needs the daemon: a stopped sandbox
    publishes no port at all, and `ports --publish` against one fails with
    `500 ... no container endpoint with IP address found`."""
    result = run(
        # --upstream: real `sbx exec` (sbx/NOTES.md "wmf-sbx redirects") --
        # wmf-sbx-exec itself calls start_sandbox, so redirecting recurses.
        [WMF_SBX, "--upstream", "exec", name, "--", "true"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print(
            color_mod.error(
                f"error: could not start sandbox {name!r} "
                f"(exit {result.returncode}): {(result.stderr or '').strip()}"
            ),
            file=sys.stderr,
        )
        hint = daemon_restart_hint(result.stderr)
        if hint:
            print(hint, file=sys.stderr)
        return False
    return True


# sbx prints this when its client is newer than the running daemon. With
# no terminal on stdin, it refuses to ask (sbx/NOTES.md §96).
DAEMON_RESTART_TEXT = "needs to restart"


def daemon_restart_hint(stderr):
    """A next step for the engineer if `stderr` shows that sbx wants to
    restart its daemon after an upgrade, else None."""
    if DAEMON_RESTART_TEXT not in (stderr or ""):
        return None
    return (
        "sbx was upgraded, and its daemon must restart before any sandbox "
        "can start. Run `wmf-sbx ls` in a terminal and answer y. WARNING: "
        "the restart stops every running sandbox."
    )


def wait_for_sandbox_mounts(name, run=subprocess.run, seconds=None):
    """Wait for the startup dispatcher to restore the mount layout, and
    say whether it did. True means every post-condition holds.

    This is the *first* thing to try after a start, not `--restore`
    (sbx/NOTES.md §46). The dispatcher runs our restore pass on every
    container start, but it does not block the `sbx exec` that triggered
    the start, so a check made immediately loses a race it would have won
    a second later -- MEASURED: all nine post-conditions failed, then all
    nine passed ten seconds on. Racing it with a second `--restore` means
    two processes doing `mount --move` at the same paths and both writing
    the same log; waiting means neither.

    Returns False if the wait ran out, if the layout can't be read, or if
    the sandbox's setup script predates `--verify` -- all of which leave
    restore_sandbox_mounts as the thing to do next."""
    wait = setup_mod.DEFAULT_VERIFY_WAIT if seconds is None else seconds
    result = run(
        # --upstream: real `sbx exec` (sbx/NOTES.md "wmf-sbx redirects").
        [WMF_SBX, "--upstream", "exec", name, "--", "python3",
         kit_mod.SANDBOX_SETUP_SCRIPT, "--verify", f"--wait={wait:g}"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return result.returncode == 0


def restore_sandbox_mounts(name, run=subprocess.run, quiet=False):
    """Re-apply the sandbox's mount layout over `sbx exec`, and report what
    it said.

    The same `wmf-sbx-setup --restore` also runs as a startup command
    inside the sandbox, which is MEASURED to work (§46) -- so this is now
    the fallback for when waiting for it didn't pan out, rather than the
    first move. It is idempotent either way: a layout that is already in
    force costs one `samefile` per repo.

    `sudo` because the exec lands as the agent, and the log it writes
    lives in /var/log. Best-effort: a resume must not fail on this."""
    result = run(
        # --upstream: real `sbx exec` (sbx/NOTES.md "wmf-sbx redirects").
        [WMF_SBX, "--upstream", "exec", name, "--", "sudo", "python3",
         kit_mod.SANDBOX_SETUP_SCRIPT, "--restore"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        if not quiet:
            print(
                f"warning: the mount restore pass failed inside {name!r} "
                f"(exit {result.returncode}): {(result.stderr or '').strip()}",
                file=sys.stderr,
            )
        return None
    # quiet on the read: a sandbox made by plain `sbx create`, or by a
    # wmf-sbx older than §41, has no report to leave and nothing is wrong
    # with that. What it *does* report is not quiet.
    return report_setup_problems(
        name, run=run, quiet=True,
        status_path=setup_mod.restore_status_path(),
        label="mount restore", step="mount restore pass",
        consequence="the clones may be missing the objects they borrow from "
                    "the host mirrors, and the mirrors may be writable",
    )


# sbx writes its startup commands as scripts under this directory, and
# runs them through the dispatcher below on a container start. Both paths
# are sbx's, not ours (sbx/NOTES.md §46, §97).
STARTUP_DISPATCHER = "/etc/durable-startup.d/run.sh"
STARTUP_LOG = "/var/log/sbx-kit-startup.log"

# Did the dispatcher run since this container booted? The log holds one
# "=== dispatcher run <ISO-8601 UTC> ===" line per run, and /proc/uptime
# gives the boot time. Exit 0 means it ran, 1 means it did not.
STARTUP_RAN_PROBE = (
    "import calendar, re, time\n"
    "boot = time.time() - float(open('/proc/uptime').read().split()[0])\n"
    "last = 0.0\n"
    "try:\n"
    "    for line in open(%(log)r):\n"
    "        m = re.match(r'=== dispatcher run (\\S+)Z ===', line)\n"
    "        if m:\n"
    "            last = calendar.timegm(time.strptime(m.group(1), "
    "'%%Y-%%m-%%dT%%H:%%M:%%S'))\n"
    "except OSError:\n"
    "    pass\n"
    # 5 s of slack: the log's stamps have whole-second resolution, and the
    # dispatcher starts a moment after boot.
    "raise SystemExit(0 if last >= boot - 5 else 1)\n"
) % {"log": STARTUP_LOG}


def ensure_startup_ran(name, run=subprocess.run):
    """Run sbx's startup commands if a container start did not.

    They are the "run on every container start" hook, and everything that
    does not survive a stop rides on them: the git daemon the engineer
    fetches through, the mount layout, the CLAUDE.md edit, the MCP proxy
    wiring. A container that sbx started *implicitly*, to serve an
    `sbx exec`, can come up without them (measured with sbx v0.43.0,
    sbx/NOTES.md §97), and then the sandbox looks fine but nothing on the
    host can fetch from it.

    The probe reads sbx's own dispatcher log, so it also holds for a
    sandbox made before this check. Best-effort: a resume must not fail
    because of it. Returns True if the startup commands have run."""
    probe = run(
        # --upstream: real `sbx exec` (sbx/NOTES.md "wmf-sbx redirects").
        [WMF_SBX, "--upstream", "exec", name, "--", "python3", "-c",
         STARTUP_RAN_PROBE],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if probe.returncode == 0:
        return True
    if probe.returncode != 1:  # no python3, no log, an sbx error: leave it
        return False
    print(f"+ (running {name}'s startup commands: the container start "
          f"skipped them)", file=sys.stderr)
    result = run(
        [WMF_SBX, "--upstream", "exec", name, "--", "sudo", "sh",
         STARTUP_DISPATCHER],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print(
            color_mod.error(
                f"warning: {name!r}'s startup commands failed "
                f"(exit {result.returncode}): {(result.stderr or '').strip()}"
            ),
            file=sys.stderr,
        )
        return False
    return True


def ensure_git_daemon(name, run=subprocess.run, port=None):
    """Start the sandbox's git daemon if nothing listens on its port.

    The startup command that owns the daemon backgrounds it. A process
    backgrounded inside an `sbx exec` dies when that exec ends, so the
    daemon that ensure_startup_ran starts can go away with it; this
    starts one with `setsid`, in a session of its own, which does not
    (sbx/NOTES.md §97). Without a daemon the host fetches nothing, and
    `wmf-sbx-rm`'s unfetched-work guard sees an empty sandbox (§32.4).

    Best-effort, and quiet when the daemon is already up. Returns True if
    the port answers afterwards."""
    if port is None:
        port = kit_mod.DEFAULT_DAEMON_PORT
    probe = ["python3", "-c", "import socket, sys; sys.exit("
             f'0 if socket.socket().connect_ex(("127.0.0.1", {int(port)})) '
             "== 0 else 1)"]
    listening = run(
        [WMF_SBX, "--upstream", "exec", name, "--"] + probe,
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if listening.returncode == 0:
        return True
    daemon = " ".join(
        shlex.quote(a)
        for a in setup_mod.daemon_argv(setup_mod.SANDBOX_HOME, port))
    print(f"+ (starting {name}'s git daemon on port {port})", file=sys.stderr)
    result = run(
        [WMF_SBX, "--upstream", "exec", name, "--", "sh", "-c",
         f"setsid {daemon} </dev/null >/dev/null 2>&1 &"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print(
            color_mod.error(
                f"warning: could not start {name!r}'s git daemon "
                f"(exit {result.returncode}): {(result.stderr or '').strip()}"
            ),
            file=sys.stderr,
        )
        return False
    again = run(
        [WMF_SBX, "--upstream", "exec", name, "--"] + probe,
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if again.returncode != 0:
        print(
            color_mod.error(
                f"warning: {name!r}'s git daemon is still not listening on "
                f"port {port}; the host can fetch nothing from it"),
            file=sys.stderr,
        )
        return False
    return True


def amend_workspace_claude_md(name, run=subprocess.run):
    """Run the sandbox's `wmf-sbx-setup --claude-md` step over `sbx exec`,
    and print what it reported (sbx/NOTES.md §95).

    The same step runs at every container start, but its output goes only
    to /var/log/sbx-kit-startup.log, and it does not block the exec that
    started the container (§46). Here it runs where the engineer sees the
    result. That matters most after an sbx upgrade, which can change the
    text the edits apply to. The step is idempotent and writes the file
    with a rename, so it can run at the same time as the startup step.

    A sandbox created before §95 has no edits file, and older copies of
    wmf-sbx-setup do not know the flag, so that sandbox is left alone
    with no message. Best-effort: a create or a resume must not fail on
    this. Returns the status dict, or None."""
    edits = kit_mod.SANDBOX_CLAUDE_MD_EDITS
    probe = run(
        # --upstream: real `sbx exec` (sbx/NOTES.md "wmf-sbx redirects").
        [WMF_SBX, "--upstream", "exec", name, "--", "test", "-f", edits],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        return None
    result = run(
        [WMF_SBX, "--upstream", "exec", name, "--", "sudo", "python3",
         kit_mod.SANDBOX_SETUP_SCRIPT, "--claude-md", edits],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print(
            f"warning: the CLAUDE.md edit failed inside {name!r} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()}",
            file=sys.stderr,
        )
        return None
    return report_setup_problems(
        name, run=run, quiet=True,
        status_path=setup_mod.claude_md_status_path(),
        label="CLAUDE.md edit", step="CLAUDE.md edit",
        consequence="sbx's CLAUDE.md is unchanged, so its git sections are "
                    "still wrong, and only ~/.claude/CLAUDE.md corrects them",
        followup=(
            f"(to fix: run `wmf-sbx refresh-claude-md {name}` in the "
            f"wmf-claude checkout, then change "
            f"sbx/patches/sbx-claude-md/edits.json until the helper passes)"
        ),
    )


def wait_for_daemon(name, sandbox_port, run=subprocess.run, attempts=20,
                    delay=0.5, sleep=time.sleep):
    """Block until the sandbox's git daemon is actually listening, or give
    up after `attempts` tries. True if it came up.

    Starting a sandbox is not the same as its daemon being ready: the
    kit's startup command is `background: true`, and `sbx exec ... true`
    returns as soon as the container is up and the startup commands have
    been *launched*. MEASURED on the host 2026-09-08 (sbx/NOTES.md §36.1)
    -- a `git ls-remote` fired immediately after the start failed with
    `fatal: read error: Connection reset by peer`, and the identical
    command a moment later succeeded.

    The probe runs *inside* the sandbox rather than against the published
    host port, because the host side can't tell the difference: Docker's
    port proxy accepts the connection whether or not anything is
    listening behind it, and only then resets -- which is exactly the
    error above."""
    probe = (
        "import socket,sys; "
        f'sys.exit(socket.socket().connect_ex(("127.0.0.1", {int(sandbox_port)})))'
    )
    for attempt in range(attempts):
        result = run(
            # --upstream: real `sbx exec` (sbx/NOTES.md "wmf-sbx redirects").
            [WMF_SBX, "--upstream", "exec", name, "--", "python3", "-c", probe],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
        if attempt + 1 < attempts:
            sleep(delay)
    return False


def ensure_published_host_port(name, sandbox_port, run=subprocess.run, quiet=False):
    """The host port sandbox_port is published on, publishing it if it
    isn't. Look first, publish second: the generated kit declares the
    daemon's port in its own `ports:` block, so at create time sbx has
    usually published it already, and `--publish`-ing a second time would
    be asking for a duplicate mapping."""
    port = lookup_published_host_port(name, sandbox_port, run=run)
    if port is not None:
        return port
    return publish_daemon_port(name, sandbox_port, run=run, quiet=quiet)


def candidates_from_state(state, host_port):
    """sync_remotes() candidates rebuilt from a saved state, with `url`
    re-derived from a freshly looked-up host port.

    The sandbox-side half of a remote never changes -- the parallel tree
    is at the same path for the life of the sandbox -- so the recorded
    `sandboxPath` is all we need to rebuild the URL. Only the host port
    moves, and it moves on every container start (see refresh_host_port).
    `state["remotes"]` holds exactly the remotes we actually added, so
    ':ro' bind mounts and repos we skipped stay skipped."""
    candidates = []
    for entry in state.get("remotes", []):
        sandbox_path = entry.get("sandboxPath")
        if not sandbox_path:
            # Pre-sandboxPath state, or a partial write. Re-pointing blind
            # would risk aiming a remote at the wrong tree.
            continue
        rel = os.path.relpath(sandbox_path, setup_mod.SANDBOX_HOME)
        candidates.append({
            "hostDir": entry["hostDir"],
            "remote": entry["remote"],
            "url": f"git://127.0.0.1:{host_port}/{rel}",
            "sandboxPath": sandbox_path,
        })
    return candidates


def refresh_host_port(name, state, run=subprocess.run, env=None, save=True,
                      quiet=False):
    """Re-point a sandbox's host remotes at wherever its git daemon is
    published *now*, returning the current host port (or None).

    sbx re-applies the publish itself on every container start, but it
    picks a **new ephemeral host port each time** -- MEASURED on the host
    2026-09-08 (sbx/NOTES.md §34): 32783 before `sbx stop`, 32784 after
    the restart, with the daemon itself back on the sandbox's own 9977.
    So the URL recorded at create time is stale from the first restart
    onward, and every `git fetch <name>` fails with `errno=
    Connection refused` -- which is what made `wmf-sbx-rm`'s
    unfetched-work guard unusable.

    A recorded port is therefore never trusted; it is looked up. If
    nothing is published (the mapping was lost rather than moved), we
    publish again. `sync_remotes` re-points rather than re-adds anything
    carrying our marker, so this is safe to run on every attach."""
    daemon_port = state.get("daemonPort") or kit_mod.DEFAULT_DAEMON_PORT
    host_port = ensure_published_host_port(name, daemon_port, run=run, quiet=quiet)
    if host_port is None:
        return None
    candidates = candidates_from_state(state, host_port)
    if not candidates:
        return host_port
    remotes, skipped = remotes_mod.sync_remotes(name, candidates, run=run, warn=_warn)
    if not save:
        return host_port
    state["hostPort"] = host_port
    if remotes:
        state["remotes"] = remotes
        state["skipped"] = skipped
    try:
        state_mod.save(state, env=env)
    except (OSError, state_mod.StateError) as e:
        # The remotes are already re-pointed and usable; only our record
        # of the port is stale, and nothing trusts that record anyway.
        _warn(f"re-pointed the remotes but could not save sandbox state ({e}).")
    return host_port


def parallel_tree_remotes(host_home, name, resolved, port, readonly_dirs=()):
    """One remote candidate dict per repo in `resolved` ([(canonical,
    path, needs_clone), ...]) that wmf-sbx-setup places under the sandbox's
    own $HOME (see wmf_sbx_setup.parallel_path) -- i.e. every repo actually
    reachable via the git daemon it starts.

    ':ro' opt-outs are included, flagged `readOnly`, rather than dropped:
    the daemon does serve them (it serves the whole parallel tree
    regardless of clone-vs-bind-mount), but their sandbox path *is* the
    host directory, bind-mounted, so a remote there would point a repo at
    itself over the loopback. wmf_sbx_remotes.sync_remotes skips them;
    keeping them here lets it record why.

    `port` is the *host*-side port (see publish_daemon_port) the daemon's
    sandbox-internal port was published to, not the sandbox-internal port
    itself -- the two differ now that publishing goes through
    `wmf-sbx ports` rather than a `sbx create`-time `HOST:SANDBOX` pair.
    """
    remotes = []
    for _canonical, path, _needs_clone in resolved:
        dest = setup_mod.parallel_path(host_home, setup_mod.SANDBOX_HOME, path)
        if dest is None:
            continue
        rel = os.path.relpath(dest, setup_mod.SANDBOX_HOME)
        remotes.append({
            "hostDir": path,
            "remote": remotes_mod.remote_name_for(name),
            "url": f"git://127.0.0.1:{port}/{rel}",
            "sandboxPath": dest,
            "readOnly": path in readonly_dirs,
        })
    return remotes


def print_remote_add_reminder(candidates):
    """The --no-remotes fallback: hand over the commands we'd have run."""
    candidates = [c for c in candidates if not c.get("readOnly")]
    if not candidates:
        return
    print(
        "\nTo fetch these sandboxed clones from their host originals, run:",
        file=sys.stderr,
    )
    for cand in candidates:
        print(
            "  " + color_mod.highlight(
                f"git -C {cand['hostDir']} remote add --no-tags "
                f"{cand['remote']} {cand['url']}"
            ),
            file=sys.stderr,
        )


def _warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def prune_removed_sandboxes(run=subprocess.run):
    """Best-effort cleanup of remotes left behind by sandboxes that no
    longer exist. Swallows its own failures: this is housekeeping bolted
    onto someone else's `create`, and it must never be the reason a launch
    fails."""
    try:
        live = existing_sandbox_names(run=run)
        pruned = remotes_mod.prune_dead(live, run=run, warn=_warn)
    except (OSError, state_mod.StateError) as e:
        _warn(f"could not clean up remotes for removed sandboxes: {e}")
        return []
    for name, results in pruned:
        removed = [r for r in results if r[2] == "removed"]
        if removed:
            print(
                f"cleaned up {len(removed)} stale remote(s) from removed "
                f"sandbox {name!r}.",
                file=sys.stderr,
            )
    return pruned


def add_host_remotes(name, candidates, daemon_port, host_port, run=subprocess.run,
                      primary_dir=None):
    """Add the `<name>` remotes and record them, so `wmf-sbx-rm`
    can take them out again. Returns the saved state, or None if nothing
    was recorded.

    Called from main() only after the real interactive `sbx create ...
    claude ...` attach has already returned 0 -- that attach IS the
    sandbox's first conversation, so the state we create here must record
    attached=True. Otherwise the next `wmf-sbx-resume` sees a fresh
    attached=False state and strips `--continue`, even though a session
    already ran. MEASURED, cananian, 2026-09-08.

    `primary_dir` is recorded so a later `wmf-sbx-resume`/`-rm`/`-start`/
    `-exec` can take a path shortcut (`.`, `..`, an absolute path) in
    place of this sandbox's name -- see wmf_sbx.state.resolve_name_arg."""
    remotes, skipped = remotes_mod.sync_remotes(name, candidates, run=run, warn=_warn)
    if not remotes:
        print_remote_add_reminder(candidates)
        return None
    state = state_mod.new_state(
        name,
        daemon_port=daemon_port,
        host_port=host_port,
        created=datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        attached=True,
        primary_dir=primary_dir,
    )
    state["remotes"] = remotes
    state["skipped"] = skipped
    try:
        state_mod.save(state)
    except (OSError, state_mod.StateError) as e:
        # The remotes exist but we can't record them, so `wmf-sbx-rm`
        # won't find them later. Say exactly how to clean up by hand
        # rather than leaving that to be discovered when a recycled port
        # makes a stale remote serve the wrong tree.
        _warn(
            f"added the remotes but could not save sandbox state ({e}) -- "
            f"`wmf-sbx-rm` will not clean them up. Remove them by hand with "
            f"`git -C DIR remote remove {remotes_mod.remote_name_for(name)}`."
        )
        return None
    print(
        f"\nAdded remote {remotes[0]['remote']!r} to {len(remotes)} host "
        f"repo(s); fetch a sandbox branch with e.g.\n"
        "  " + color_mod.highlight(
            f"git -C {remotes[0]['hostDir']} fetch {remotes[0]['remote']}"
        ),
        file=sys.stderr,
    )
    return state


def is_raw_path(spec):
    """True if spec is a literal filesystem path rather than a repo name
    or scheme:path spec -- real Gerrit/GitLab project paths never start
    with these characters, so this is unambiguous. Bare '.' and '..' are
    included too (not just './' and '../') since a shell never appends a
    trailing separator to those on its own."""
    return spec in (".", "..") or spec.startswith(("/", "~", "./", "../"))


def resolve_repo(spec, config_path):
    """Resolve one repo spec. Returns (canonical, realpath'd local dir,
    needs_clone) -- realpath'd per sbx/NOTES.md #1, since sbx does not
    resolve symlinks in bind-mount source paths.

    A spec that looks like a literal filesystem path (see is_raw_path)
    bypasses repo resolution entirely -- this is how container
    directories (e.g. a Skins/ or Extensions/ folder holding many
    checkouts) or other one-off reference directories that don't fit the
    one-canonical-repo model get mounted; canonical is None for these."""
    if is_raw_path(spec):
        path = os.path.realpath(os.path.expanduser(spec))
        if not os.path.isdir(path):
            raise resolve_mod.ResolutionError(f"{spec!r} is not an existing directory.")
        return None, path, False
    canonical, path, _rule, needs_clone = resolve_mod.resolve(spec, config_path)
    return canonical, os.path.realpath(os.path.expanduser(path)), needs_clone


def do_clone(canonical, path, run=subprocess.run):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = clone_url(canonical)
    print(color_mod.dim(f"+ git clone {url} {path}"), file=sys.stderr)
    result = run(["git", "clone", url, path])
    if result.returncode != 0:
        raise LaunchError(f"git clone {url} {path} failed (exit {result.returncode}).")


def find_nested_mount_conflict(dirs):
    """sbx refuses to put the primary workspace inside another bind mount
    (cananian hit this trying to mount ~/Wikimedia read-only as an extra
    while working out of ~/Wikimedia/wmf-claude) -- and the same
    ancestor/descendant overlap would be just as broken between any two
    mounts. dirs must already be realpath'd. Returns the (ancestor,
    descendant) pair, or None if there's no conflict."""
    for a in dirs:
        for b in dirs:
            if a == b:
                continue
            if b.startswith(a + os.sep):
                return a, b
    return None


def expand_dependencies(resolved, split, config, config_path, include_dev=True,
                        include_suggests=True, fetch=None, warn=None):
    """Grow the explicitly-named repo list into its full MediaWiki
    dependency closure -- see sbx/DESIGN-dependency-walk.md. Returns
    (resolved, split, origins_by_path, unreachable) -- `unreachable` being
    the repos whose manifests couldn't be fetched, i.e. the reason to
    distrust the closure.

    Discovery reads manifests from local checkouts where they exist and
    from gitiles otherwise, so nothing is cloned until the whole plan is
    known and `--dry-run` can show the real closure rather than the part
    of it that happens to already be on disk.

    Discovered repos are always extras (the primary stays whatever was
    named first, and `sbx create` requires the primary be read/write
    anyway) and never ':ro' -- you may well need to edit a dependency.
    """
    warn = warn or _warn
    # Looked up here rather than as a default argument so tests (and
    # anything else) can substitute the network call by patching the module.
    fetch = fetch or deps_mod.fetch_manifest
    rules = config.get("rules", [])

    def local_dir(canonical):
        try:
            path, _rule, needs_clone = resolve_mod.resolve_directory(canonical, rules)
        except resolve_mod.ResolutionError:
            return None
        return None if needs_clone else path

    roots = [canonical for canonical, _path in canonicals_for_kit(resolved, rules)]
    unreachable = set()
    discovered, origins = deps_mod.walk(
        roots,
        deps_mod.make_manifest_for(local_dir, fetch=fetch, warn=warn,
                                   unreachable=unreachable),
        include_dev=include_dev,
        include_suggests=include_suggests,
        overrides=config.get("dependency_overrides") or {},
        warn=warn,
    )

    resolved = list(resolved)
    split = list(split)
    origins_by_path = {}
    for canonical in discovered:
        chain = deps_mod.origin_chain(canonical, origins, roots)
        try:
            entry = resolve_repo(canonical, config_path)
        except resolve_mod.ResolutionError as e:
            # Loud, not silent: a dropped dependency doesn't announce
            # itself -- it surfaces much later as failing tests inside the
            # sandbox, which is a far worse experience than an error here.
            raise resolve_mod.ResolutionError(
                f"{e}\n{canonical} was pulled in as a dependency "
                f"({' -> '.join(chain)}). Add a repos.yaml rule for it, or "
                f"re-run with --no-deps."
            ) from None
        resolved.append(entry)
        split.append((canonical, False))
        origins_by_path[entry[1]] = chain
    return resolved, split, origins_by_path, unreachable


def link_plan(resolved_for_kit, overrides=None, warn=None):
    """{local_dir: (link_name, link_dir)} for every repo that should be
    symlinked into the core clone -- see sbx/DESIGN-dependency-walk.md §1
    and §7. Computed here, on the host, because it needs both the canonical
    names (which only exist here) and the parsed manifests.

    A repo with no manifest and no place in mediawiki/{extensions,skins}
    isn't linked at all: that's a container directory or a service, not
    something `wfLoadExtension` can find.
    """
    warn = warn or _warn
    links = {}
    for canonical, path in resolved_for_kit:
        manifest = deps_mod.read_manifest(path, warn=warn) if os.path.isdir(path) else None
        section = deps_mod.link_section(canonical, manifest)
        if section is None:
            continue
        if canonical is None:
            # An unidentified raw path: the directory the user typed is the
            # only name we have, and it's the same thing the basename rule
            # would pick for a canonical anyway.
            name = os.path.basename(path.rstrip(os.sep))
        else:
            name = deps_mod.link_name(canonical, manifest, overrides=overrides, warn=warn)
        links[path] = (name, section)
    return links


def canonicals_for_kit(resolved, rules):
    """resolved: [(canonical_or_None, path, needs_clone), ...]. A raw-path
    argument (canonical is None -- see is_raw_path) is run through
    resolve_mod.reverse_resolve (checking its .gitreview, then the config's
    exact rules) so a known repo passed by literal path -- e.g. cananian's
    own invocations, which predate this config existing -- still gets
    identified for kit environment-variable wiring (MW_CORE_REPO and
    friends), not just one passed by resolvable name. Returns
    [(canonical_or_None, path), ...] for wmf_sbx_kit.build_kit_spec."""
    result = []
    for canonical, path, _needs_clone in resolved:
        if canonical is None:
            # is_raw_path arguments are already existence-checked by
            # resolve_repo before we ever get here; don't re-stat.
            canonical, _rule = resolve_mod.reverse_resolve(path, rules, exists=lambda p: True)
        result.append((canonical, path))
    return result


# This module now lives in sbx/src/wmf_sbx/; the wmf-sbx wrapper it
# shells out to still lives in sbx/bin/ -- three levels up, then across.
#
# realpath, not abspath: the checkout is routinely reached through a
# symlink (`~/Wikimedia` -> `~/Projects/Wikimedia`), and a symlinked path
# is the wrong thing to hand to anything that bind-mounts. sbx mounts the
# host directory at the path you name; it does not recreate the symlinks
# that path went through, so inside the sandbox the link's own name
# doesn't exist. Every host path this module emits is realpath'd for that
# reason (sbx/NOTES.md #1, §66) -- including this one, which is where
# MCP_REPO_ROOT comes from and thus which checkout's MCP servers get
# registered, permanently, by `sbx mcp add`.
_SBX_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))))
WMF_SBX = os.path.join(_SBX_ROOT, "bin", "wmf-sbx")


# --- Host-side MCP registration (Route 1) ---------------------------------
#
# The Phabricator and Gerrit MCP servers run on the *host*, registered with
# `sbx mcp add`, and reach the sandbox through sbx's MCP gateway. That is
# the entire point of the arrangement: whatever those servers authenticate
# with -- a Phabricator username today, a Gerrit HTTP password the day
# anyone wants to post a review -- stays on the host, unreadable from the
# sandbox. Installing the servers *in* the sandbox would mean shipping
# their credentials in with them. See sbx/DESIGN-plugin-integration.md §5
# step 4 and sbx/NOTES.md §60-62.
#
# The in-sandbox half is wmf_sbx.kit's: wmf-sbx-mcp-proxy on PATH, one
# `claude mcp add` per server, each with its own --tools allowlist. This
# half makes sure the host has something for the gateway to mount.

# The wmf-claude checkout the servers live in. _SBX_ROOT is <repo>/sbx.
MCP_REPO_ROOT = os.path.dirname(_SBX_ROOT)

# mcp-phabricator's cheerio -> undici needs this, and `npm` does not
# enforce `engines`, so an older node installs cleanly and fails only when
# the server is *run*, with `ReferenceError: File is not defined`
# (sbx/NOTES.md §60.2). `sbx mcp ls` reports such a server as `✓ ready`
# regardless -- "ready" means the command path resolved, not that the
# server starts -- so if this isn't preflighted here it isn't preflighted.
#
# Measured, and deliberately *higher* than what mcp-phabricator itself
# declares: its own package.json says `engines: {"node": ">=20.0.0"}`,
# but the floor that actually bites comes from two levels down, where
# cheerio and undici both say >=20.18.1 (§60.2). required_node_version
# takes the larger of the two for exactly that reason -- reading the
# server's `engines` *instead* of this would lower the bar back to a
# version that fails at run time.
MIN_NODE_VERSION = (20, 18, 1)


def format_version(version):
    return "v" + ".".join(str(part) for part in version)


def declared_node_engine(root):
    """mcp-phabricator's own `engines.node` floor, or None.

    Only `>=X[.Y[.Z]]` and a bare `X.Y.Z` are understood; a range, an
    `||`, or anything else returns None rather than a guess. This can
    only ever *raise* the floor (see required_node_version), so failing
    to read it is safe and mis-reading it downward is impossible."""
    path = os.path.join(root, "mcp-phabricator", "package.json")
    try:
        with open(path, encoding="utf-8") as f:
            engines = (json.load(f) or {}).get("engines") or {}
    except (OSError, ValueError, AttributeError):
        return None
    spec = engines.get("node")
    if not isinstance(spec, str):
        return None
    match = re.fullmatch(r"\s*(?:>=\s*)?v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*",
                         spec)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def required_node_version(root=MCP_REPO_ROOT):
    """The node floor to hold the host to: the measured one, raised if
    the checked-out server declares something stricter (a submodule bump
    may well). Never lowered -- see MIN_NODE_VERSION."""
    declared = declared_node_engine(root)
    return max(MIN_NODE_VERSION, declared) if declared else MIN_NODE_VERSION


def node_version(node, run=subprocess.run):
    """(major, minor, patch) of `node`, or None if it won't say."""
    try:
        result = run([node, "--version"],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (result.stdout or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def wmf_claude_config(path=None):
    """The checkout installer's per-user answers, or {}.

    `bin/wmf-claude-setup` writes this; we only ever read it. Any problem
    at all -- absent, unreadable, not JSON, not an object -- is "no
    answers stored", because nothing here is required and a create must
    not fail over a config file it does not own.
    """
    if path is None:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        path = os.path.join(base, "wmf-claude", "config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def phabricator_username(run=subprocess.run, config_path=None):
    """The username mcp-phabricator filters "my tasks" by.

    Three sources, most explicit first:

    1. `$PHABRICATOR_USERNAME`, for a host that never ran the checkout
       installer, or a one-off;
    2. `~/.config/wmf-claude/config.json`, which `bin/wmf-claude-setup`
       writes -- the answer's actual home;
    3. the `claude mcp get phabricator` output, where the answer used to
       live before (2) existed. Kept for a host whose last install
       predates it; nothing writes it any more.

    Not prompted for: wmf-sbx-create runs non-interactively often enough,
    and the server works without it -- anonymous, public data only, the
    username is a default filter.
    """
    override = os.environ.get("PHABRICATOR_USERNAME")
    if override:
        return override
    stored = wmf_claude_config(config_path).get("phabricatorUsername")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    try:
        result = run(["claude", "mcp", "get", "phabricator"],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"PHABRICATOR_USERNAME=([^\s\"']+)", result.stdout or "")
    return match.group(1) if match else None


class McpProblem(str):
    """A reason one server was left out.

    A plain string everywhere it's printed, with three attributes for the
    code that has to decide what to do about it: `server`; `stop` --
    whether this is bad enough to fail the create rather than shrug and
    carry on without that server; and `advice`, the lines to print under
    it when it does stop, for a problem whose way out is not the node
    floor's. See ensure_host_mcp_servers.
    """

    def __new__(cls, message, server=None, stop=False, advice=None):
        problem = super().__new__(cls, message)
        problem.server = server
        problem.stop = stop
        problem.advice = advice
        return problem


def host_mcp_plan(run=subprocess.run, root=MCP_REPO_ROOT):
    """({server name: argv for `env`}, [reasons a server was left out]).

    `sbx mcp add` has no `--env` (sbx/NOTES.md §58.2), so every server is
    registered as `--command env --args "KEY=V,...,cmd,args"` -- which
    resolves to /usr/bin/env and works for both of ours (§61.1). `--args`
    is comma-split, so an argument containing a comma has nowhere to hide;
    a checkout under such a path drops the server rather than registering
    a mangled command."""
    plan, problems = {}, []

    phab_entry = os.path.join(root, "mcp-phabricator", "src", "index.js")
    node = shutil.which("node")
    version = node_version(node, run=run) if node else None
    required = required_node_version(root)
    if not os.path.exists(phab_entry):
        # Not `stop`: nothing has been written anywhere, the fix is
        # printed, and the next create picks the server up by itself once
        # the checkout is built. Contrast the node floor below.
        problems.append(McpProblem(
            f"phabricator: {phab_entry} is missing -- run "
            f"`git submodule update --init` and `bin/wmf-claude-build` in "
            f"{root}", server="phabricator"))
    elif node is None:
        problems.append(McpProblem(
            "phabricator: no `node` on PATH, so the Phabricator MCP "
            "server cannot be registered", server="phabricator", stop=True))
    elif version is None:
        problems.append(McpProblem(
            f"phabricator: `{node} --version` did not say which version "
            f"it is, so the {format_version(required)} its dependencies "
            f"need cannot be checked", server="phabricator", stop=True))
    elif version < required:
        # The path, not just the version: on a host that manages node
        # with nave/nvm/asdf, "which one is this?" is the actual
        # question, and the answer is what tells the engineer whether
        # their version-manager shell is active.
        problems.append(McpProblem(
            "phabricator: {} is {}, below the {} its dependencies need "
            "(sbx/NOTES.md §60.2)".format(node, format_version(version),
                                          format_version(required)),
            server="phabricator", stop=True))
    else:
        # Absolute, from `which`, because `sbx mcp add` resolves the
        # command once at registration time: a newer node arriving earlier
        # on PATH later does not retroactively fix a stored registration.
        args = [node, phab_entry]
        username = phabricator_username(run=run)
        if username:
            args.insert(0, f"PHABRICATOR_USERNAME={username}")
        plan["phabricator"] = args

    gerrit_dir = os.path.join(root, "gerrit-mcp-server")
    python = os.path.join(gerrit_dir, ".venv", "bin", "python")
    main = os.path.join(gerrit_dir, "gerrit_mcp_server", "main.py")
    if not os.path.exists(python) or not os.path.exists(main):
        problems.append(McpProblem(
            f"gerrit: {gerrit_dir}/.venv is not built -- run "
            f"`git submodule update --init` and `bin/wmf-claude-build` in "
            f"{root}", server="gerrit"))
    else:
        # The venv's interpreter by absolute path: no PATH, no `activate`.
        # PYTHONDONTWRITEBYTECODE keeps the gateway from scattering
        # __pycache__ through the engineer's checkout.
        plan["gerrit"] = [f"PYTHONPATH={gerrit_dir}",
                          "PYTHONDONTWRITEBYTECODE=1", python, main, "stdio"]

    for name in sorted(plan):
        if any("," in arg for arg in plan[name]):
            problems.append(McpProblem(
                f"{name}: a comma in {root} -- `sbx mcp add --args` is "
                f"comma-split, so the command cannot be spelled",
                server=name))
            del plan[name]
    return plan, problems


def mcp_add_command(name, args):
    return [WMF_SBX, "--upstream", "mcp", "add", name, "--command", "env",
            "--args", ",".join(args)]


def host_mcp_registrations(run=subprocess.run):
    """The names already in the host's MCP store, or None if it can't be
    read (a daemon that isn't running, an sbx too old for `mcp ls
    --json`).

    `{"servers": [{"name": "gerrit", "transport": "local stdio",
    "status": "ready", "type": "local"}, ...]}` (MEASURED §67). There is
    no fallback to the human table: it is a layout, and every failure
    here is safe -- None means "unreadable", which registers nothing
    rather than re-adding over what is there."""
    try:
        result = run([WMF_SBX, "--upstream", "mcp", "ls", "--json"],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        servers = (json.loads(result.stdout or "") or {}).get("servers")
    except ValueError:
        return None
    if not isinstance(servers, list):
        # Exit 0 but nothing parseable: that is not "no servers", and
        # treating it as such would re-add over an existing registration.
        return None
    # A server with no name is not something to guess at; it also cannot
    # collide with a name we are about to register.
    return {s["name"] for s in servers
            if isinstance(s, dict) and s.get("name")}


def registered_phabricator_username(run=subprocess.run):
    """The username baked into the host's existing `phabricator`
    registration, or None if there is no telling.

    `sbx mcp inspect --json` hands back the stored argv as a list
    (MEASURED, §75.2), which is the whole reason this is worth doing:

        {"name": "phabricator", "type": "local",
         "command": ["env", "PHABRICATOR_USERNAME=cscott",
                     "/…/node", "/…/src/index.js"],
         "requires_oauth": false, "resolved_command": "/usr/bin/env"}

    One element either is the assignment or is not -- no regex over a
    human layout, and no way for a path that happens to contain the
    string to be mistaken for it.

    None is "no telling", and covers four different things on purpose:
    an sbx whose `mcp inspect` has no `--json`, a daemon that is not
    answering, output that does not parse, and a registration that names
    no username at all -- which an engineer's own wrapper script may
    legitimately not, passing it some other way. Only a username that
    can be read and compared is worth stopping a create over, so unlike
    host_mcp_registrations there is no fallback to the human output:
    failing to read this is safe, and the text form is a layout.
    """
    try:
        result = run([WMF_SBX, "--upstream", "mcp", "inspect", "phabricator", "--json"],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        command = (json.loads(result.stdout or "") or {}).get("command")
    except ValueError:
        return None
    if not isinstance(command, list):
        return None
    prefix = "PHABRICATOR_USERNAME="
    for arg in command:
        if isinstance(arg, str) and arg.startswith(prefix):
            return arg[len(prefix):] or None
    return None


def phabricator_username_problem(run=subprocess.run):
    """A stop problem if the host's stored `phabricator` registration
    filters by a different username than this host now answers with,
    else None.

    `sbx mcp add` has no update-in-place, and we never overwrite a
    registration (see ensure_host_mcp_servers), so an engineer who
    changes their Phabricator username otherwise keeps getting sandboxes
    that quietly filter "my tasks" by the old one. Nothing about such a
    sandbox looks wrong: the tool answers, it just answers about
    somebody else. Rare, and silent, which is what makes it worth a
    check rather than a note in a document.

    Stopping is as far as this goes. Re-registering would mean removing
    the running server first, and that server belongs to the host, not
    to this create -- any sandbox with a session open against it loses
    its Phabricator tools the moment it goes. That is the engineer's
    call to make, so this says what to run and stops.
    """
    wanted = phabricator_username(run=run)
    if not wanted:
        return None
    registered = registered_phabricator_username(run=run)
    if not registered or registered == wanted:
        return None
    return McpProblem(
        f"phabricator: the host's registration filters \"my tasks\" by "
        f"`{registered}`, but this host now answers `{wanted}`",
        server="phabricator", stop=True,
        advice=[
            "",
            "The username is stored in the registration when it is made, and",
            "`wmf-sbx mcp add` has neither an update nor an overwrite -- so",
            "every sandbox created from here would keep filtering by",
            f"`{registered}`, without anything looking wrong.",
            "",
            "Re-registering is yours to do rather than this script's, because",
            "removing the server cuts it off from any sandbox using it right",
            "now. When no session needs it:",
            "",
            "    wmf-sbx mcp rm phabricator",
            "",
            "and re-run this, which registers it again with the username this",
            "host answers with.",
            "",
            f"If `{registered}` was right after all, put it back instead --",
            "`phabricatorUsername` in ~/.config/wmf-claude/config.json (what",
            "`wmf-claude-setup` writes), or `PHABRICATOR_USERNAME` in the",
            "environment for a one-off.",
        ])


class McpPreflightError(Exception):
    """The create cannot go ahead: a server that would have to be
    registered on this host cannot be, in a way the engineer has to
    choose how to fix (see ensure_host_mcp_servers)."""


# The way out of the node floor, and the default for a stop problem that
# does not carry advice of its own.
NODE_FLOOR_ADVICE = [
    "",
    "This is not something to work around by registering it anyway.",
    "`wmf-sbx mcp add` would succeed and `wmf-sbx mcp ls` would report",
    "`✓ ready` -- \"ready\" only means the command path resolved -- and",
    "the server would then fail inside every sandbox, at the moment a",
    "tool is called (sbx/NOTES.md §60.2). Worse, the registration is",
    "resolved once and stored, so fixing `node` afterwards does not fix",
    "it: it would have to be removed and re-added by hand.",
    "",
    "Either:",
    "  - put a new enough `node` on PATH and re-run -- activate whatever",
    "    version manager you use (nave, nvm, fnm, asdf, volta ...), or",
    "    upgrade the system node; 22 LTS matches the sandbox image's,",
    "  - or pass --no-mcp to create the sandbox without the Phabricator",
    "    and Gerrit tools (everything else works; the agent is told they",
    "    are absent and falls back to the web UI).",
]


def mcp_preflight_advice(problems):
    """The stderr block for a create stopped by a `stop` problem.

    `problems` is the blocking list, so its first entry is the one
    McpPreflightError carries and the one whose advice belongs under it.
    """
    lines = ["error: the sandbox's MCP servers cannot be set up on this host:",
             ""]
    lines += [f"  {problem}" for problem in problems]
    lines += (problems[0].advice if problems and problems[0].advice
              else NODE_FLOOR_ADVICE)
    return "\n".join(lines)


def ensure_host_mcp_servers(run=subprocess.run, root=MCP_REPO_ROOT,
                            quiet=False, register=True):
    """Register the MCP servers this host can actually run, and return the
    names the generated kit should put a proxy in front of.

    Most failures don't fail the create. An unbuilt submodule, a comma in
    the checkout path, or an sbx whose MCP store can't be listed gets a
    sandbox with no MCP servers and a sentence saying why, which is a
    working sandbox. Registering one anyway is the worse outcome twice
    over: `sbx create --static-mcp` rejects a name sbx doesn't know (`400
    Bad Request: unknown --static-mcp server(s): gerrit` -- §59.2, which
    failed a whole create), and a proxy with nothing behind it is a
    failed MCP server in every session of that sandbox.

    The exception is a `stop` problem -- today, the node floor -- raised
    as McpPreflightError. The difference is what the engineer can do
    about it and how the failure would otherwise present. An unbuilt
    submodule is a state of this checkout: the printed command fixes it,
    and the next create picks the server up on its own. A node below the
    floor is a property of the shell wmf-sbx-create was launched from,
    the fix depends on how that host manages node, and the symptom if
    it's merely warned about is a sandbox that looks fine until a tool
    call 500s. So it stops, and says what to do.

    `register=False` does every check and prints what it *would* add
    without adding it -- what --dry-run wants, and a real preflight,
    since the stop conditions are evaluated identically.

    Anything already registered is left exactly as it is, not re-added:
    the engineer's own `sbx mcp add` may carry a username, a token or a
    path this has no business overwriting, and nothing distinguishes
    theirs from ours. The single exception is a *read*: a `phabricator`
    registration whose username disagrees with this host's stops the
    create, because leaving it alone silently means every sandbox from
    here answers "my tasks" about the wrong person."""
    plan, problems = host_mcp_plan(run=run, root=root)
    existing = host_mcp_registrations(run=run)
    if existing is None:
        problems.append(McpProblem(
            "could not read the host's MCP store (`wmf-sbx mcp ls`), so "
            "nothing was registered"))
        plan = {}
    # Nothing we could not work out about a server matters if the host
    # has already registered it: that registration carries its own
    # resolved command and nothing here is about to touch it. Both halves
    # of this matter. The server still reaches the kit even though this
    # invocation could not have built its command -- the case that bites
    # is a host whose default `node` is too old but whose phabricator was
    # registered from a version-manager shell, which would otherwise
    # silently lose the Phabricator tools it has (§68). And no stop
    # condition fires for something nobody is about to add.
    already = set(existing or ())
    problems = [problem for problem in problems
                if problem.server is None or problem.server not in already]
    # The one thing about an existing registration that is checked rather
    # than left alone. Not touched -- just read, and reported if it
    # disagrees with the username this host would register today.
    if "phabricator" in already:
        mismatch = phabricator_username_problem(run=run)
        if mismatch is not None:
            problems.insert(0, mismatch)
    blocking = [problem for problem in problems if problem.stop]
    if blocking:
        if not quiet:
            print(color_mod.error(mcp_preflight_advice(blocking)), file=sys.stderr)
        raise McpPreflightError(blocking[0])
    registered = []
    for name in sorted(set(plan) | (already & set(kit_mod.MCP_SERVER_TOOLS))):
        if name in already:
            registered.append(name)
            continue
        cmd = mcp_add_command(name, plan[name])
        if not quiet:
            prefix = "+ " if register else "+ (would run) "
            print(color_mod.dim(prefix + " ".join(cmd)), file=sys.stderr)
        if not register:
            registered.append(name)
            continue
        result = run(cmd)
        if result.returncode == 0:
            registered.append(name)
        else:
            problems.append(McpProblem(
                f"{name}: `wmf-sbx mcp add` failed (exit "
                f"{result.returncode}); the sandbox will not have it",
                server=name))
    if problems and not quiet:
        for problem in problems:
            _warn(f"MCP: {problem}")
        print(
            "  (the sandbox works without these; it just won't have the "
            "Phabricator/Gerrit tools the skills and the gerrit-reviewer "
            "agent call by name)",
            file=sys.stderr,
        )
    return registered


def build_sbx_command(name, kit, primary_dir, extra_dirs, static_mcp=(),
                      skills_flag=None):
    # Invoke the wmf-sbx wrapper, never sbx directly -- see its header
    # comment and sbx/NOTES.md #8: sbx forwards the host SSH agent into
    # every sandbox whenever SSH_AUTH_SOCK is set in the invoking shell,
    # and that forwarding appears sticky to the sbx daemon's own
    # environment rather than scoped per-invocation, so this wrapper is
    # the only thing that reliably keeps it off.
    #
    # EVERY extra gets ':ro', not just the ones the user suffixed --
    # nothing is ever supposed to write to a host-mirrored original (the
    # writable copy is the parallel-tree clone wmf-sbx-setup makes; see
    # sbx/DESIGN-parallel-clone-tree.md §2), and create-time ':ro' is the only
    # layer that actually enforces that. wmf-sbx-setup's own remount is an
    # accident guard only: the sandboxed agent has passwordless sudo, so
    # `sudo mount -o remount,rw` lifts it and writes reach the host's real
    # files. A mount sbx created read-only is backed read-only below the
    # layer the sandbox can reach, so the same remount reports success --
    # /proc/mounts even says rw -- while writes still fail. See
    # sbx/NOTES.md #22 and docker/sbx-releases#556.
    #
    # The user's ':ro' suffix therefore no longer decides anything here;
    # it survives only as the parallel-tree strategy switch (bind mount
    # instead of clone), via wmf_sbx_kit.build_kit_spec's readonly_dirs.
    #
    # The primary is the exception, and it's sbx's, not ours: `sbx create`
    # rejects ':ro' on the primary workspace outright ("ERROR: primary
    # workspace must be read/write"). main() warns about that separately.
    #
    # No port-publish flag here: `sbx create` has no create-time
    # `-p`/`--publish` option against a real `sbx` binary (see
    # sbx/DESIGN-parallel-clone-tree.md §3, §6) -- the git daemon's port is
    # published separately, after creation, via publish_daemon_port.
    #
    # --static-mcp mounts the named host-side servers into the sandbox's
    # gateway at creation, and -- the reason it is here rather than left
    # to the gateway's dynamic `mcp-add` -- *deletes* `mcp-add`,
    # `mcp-find` and `mcp-config-set` from the gateway (sbx/NOTES.md
    # §61.3). Removing the capability to mount an arbitrary MCP server
    # beats hiding it behind the `mcp__mcp-gateway` deny, which is kept
    # too: `code-mode` and `mcp-exec` survive static mode, and `code-mode`
    # is an arbitrary-JS path no `Bash(...)` rule touches.
    #
    # Only names sbx already knows: an unregistered one fails the whole
    # create with `400 Bad Request: unknown --static-mcp server(s): ...`.
    # ensure_host_mcp_servers returns exactly the confirmed ones.
    #
    # Create-time only. `sbx run` takes the flag but says the set "cannot
    # be changed when re-attaching to an existing sandbox" (§58.1), so
    # wmf-sbx-resume has nothing to add.
    # skills_flag comes from supported_skills_flag() -- see there for why
    # it's probed rather than hardcoded, and why `off` rather than
    # `readonly`. None means this sbx has neither spelling, which is
    # 0.42.1: the create proceeds, because the shared store is a
    # cross-sandbox risk and not a reason to refuse to work, and
    # wmf-sbx-setup remounts the mount read-only from inside as the
    # partial cover it is (sbx/SECURITY.md §7.6).
    # --upstream: this module IS wmf-sbx-create, so without it a plain
    # `wmf-sbx create` here would redirect straight back to this same
    # module and recurse forever (sbx/NOTES.md "wmf-sbx redirects").
    cmd = [WMF_SBX, "--upstream", "create", "--name", name]
    if kit:
        cmd += ["--kit", os.path.realpath(os.path.expanduser(kit))]
    if skills_flag:
        cmd += [skills_flag]
    if static_mcp:
        cmd += ["--static-mcp", ",".join(static_mcp)]
    cmd += ["claude", primary_dir]
    cmd += [f"{d}:ro" for d in extra_dirs]
    return cmd


def build_sbx_run_command(name):
    # wmf-sbx-resume, not `wmf-sbx run --name`: re-attaching has host-side
    # work to redo first (the daemon's published port moves on every
    # container start -- refresh_host_port), and `sbx run` does none of it.
    # It ends in `wmf-sbx run --name NAME` -- `--name`, not the bare
    # positional, which sbx 0.39 deprecated ("use `sbx run --name NAME`
    # instead"): the positional is now the workspace path.
    full_path = os.path.join(os.path.dirname(WMF_SBX), "wmf-sbx-resume")
    # Prefer the bare name, but only if PATH would actually find *this*
    # script -- an unrelated or stale wmf-sbx-resume shadowing ours
    # earlier on PATH must not get recommended instead.
    found = shutil.which("wmf-sbx-resume")
    if found and os.path.realpath(found) == os.path.realpath(full_path):
        return ["wmf-sbx-resume", name]
    return [full_path, name]


def print_run_reminder(name):
    # `sbx create`'s own output tells the user to run `sbx run --name NAME`, but
    # a bare `sbx` bypasses the wmf-sbx wrapper's SSH_AUTH_SOCK stripping
    # (see build_sbx_command) -- so always point back at our own launcher here.
    run_cmd = build_sbx_run_command(name)
    print(
        f"\nTo (re)connect to this sandbox later, run:\n"
        f"  {color_mod.highlight(' '.join(run_cmd))}\n"
        f"(not `sbx run --name {name}`: that skips the SSH_AUTH_SOCK "
        f"stripping -- sbx/NOTES.md #8 -- and leaves the host remotes "
        f"pointing at the port the daemon had before the restart.)",
        file=sys.stderr,
    )


def main(argv=None, run=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "primary", help="Repo for the sandbox's primary workspace; append ':ro' for read-only"
    )
    parser.add_argument(
        "extra", nargs="*",
        help="Additional repos to make available in the sandbox; append ':ro' for read-only"
    )
    parser.add_argument("--name", help="Sandbox name (default: derived from the primary repo)")
    parser.add_argument(
        "--kit",
        help="Path to the sbx kit (default: 'kit:' in the config if set, "
        "else a MediaWiki kit is generated -- see sbx/DESIGN-kit-generation.md)",
    )
    parser.add_argument(
        "--kit-out", metavar="DIR",
        help="Write the generated kit to DIR and leave it there, instead of "
        "a temporary directory that is deleted after `sbx create` (for "
        "`sbx kit validate DIR`). No effect with an explicit --kit",
    )
    parser.add_argument("--config", default=resolve_mod.DEFAULT_CONFIG)
    parser.add_argument(
        "--no-deps", action="store_true",
        help="Mount only the repos named on the command line -- no MediaWiki "
        "dependency walk, no implicit core/Vector",
    )
    parser.add_argument(
        "--no-dev", action="store_true",
        help="Skip 'dev-requires' when walking dependencies",
    )
    parser.add_argument(
        "--no-suggests", action="store_true",
        help="Skip 'suggests' when walking dependencies",
    )
    parser.add_argument(
        "--reset-all", action="store_true",
        help="Reset every clone to upstream master, including the repos you "
        "named on the command line (by default those keep the branch your "
        "host checkout was on)",
    )
    parser.add_argument(
        "--no-remotes", action="store_true",
        help="Don't touch any host .git/config: print the `git remote add` "
        "commands instead, and skip the cleanup pass for removed sandboxes",
    )
    parser.add_argument(
        "--no-mcp", action="store_true",
        help="Don't register the Phabricator/Gerrit MCP servers on the host "
        "and don't wire them into the generated kit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the resolution plan and the sbx command; clone nothing, run nothing"
    )
    args = parser.parse_args(argv)

    settings_values = settings_mod.read_settings(WMF_SBX, run=run)
    if settings_values is None:
        _warn(
            "could not read sbx settings (`wmf-sbx settings list --json` "
            "failed); skipping the ssh-forwarding/kit/proxy preflight checks"
        )
    else:
        settings_warnings, settings_errors = settings_mod.preflight(
            settings_values, check_kit=args.kit is None,
        )
        for w in settings_warnings:
            _warn(w)
        if settings_errors:
            for e in settings_errors:
                print(color_mod.error(f"error: {e}"), file=sys.stderr)
            return 1

    try:
        config = resolve_mod.load_config(args.config)
        kit = args.kit or config.get("kit")
        split = [split_ro_suffix(spec) for spec in [args.primary] + args.extra]
        resolved = [resolve_repo(spec, args.config) for spec, _is_ro in split]
        origins_by_path = {}
        unreachable = set()
        if not args.no_deps:
            resolved, split, origins_by_path, unreachable = expand_dependencies(
                resolved, split, config, args.config,
                include_dev=not args.no_dev,
                include_suggests=not args.no_suggests,
            )
    except resolve_mod.ResolutionError as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1

    if args.reset_all and kit is not None:
        # The flag rides in the generated plan, and an explicit --kit brings
        # its own; there is nothing here to put it in.
        print(
            "warning: --reset-all has no effect with an explicit --kit (the "
            "kit carries its own wmf-sbx-plan.json)",
            file=sys.stderr,
        )

    if args.no_mcp and kit is not None:
        # Nothing to opt out of: the host-side registration and the proxy
        # entries both hang off the generated kit.
        print(
            "warning: --no-mcp has no effect with an explicit --kit (that "
            "kit decides its own MCP wiring)",
            file=sys.stderr,
        )

    if args.kit_out and kit is not None:
        print(
            "warning: --kit-out has no effect with an explicit --kit (there "
            "is no generated kit to write)",
            file=sys.stderr,
        )

    for (spec, is_ro), (canonical, path, needs_clone) in zip(split, resolved):
        ro_note = " (read-only)" if is_ro else ""
        chain = origins_by_path.get(path)
        # Name where a repo the user never typed came from -- a bare extra
        # line in the plan is not self-explanatory.
        via = f" [via {' -> '.join(c.rsplit('/', 1)[-1] for c in chain)}]" if chain else ""
        if canonical is None:
            print(f"  {spec} -> {path} (raw path){ro_note}{via}", file=sys.stderr)
        else:
            status = "needs clone" if needs_clone else "exists"
            print(f"  {spec} -> {canonical} -> {path} ({status}){ro_note}{via}", file=sys.stderr)
    if origins_by_path:
        print(
            f"  ({len(origins_by_path)} of these {len(resolved)} were "
            f"discovered by the dependency walk; --no-deps skips it)",
            file=sys.stderr,
        )

    if unreachable:
        # A rate-limited or offline walk produces a plausible-looking
        # closure that is quietly missing repos -- which surfaces much
        # later as inexplicable failures inside the sandbox. The plan above
        # is printed either way (that's what --dry-run is for), but a real
        # create stops here rather than building something half-wired.
        listed = "\n".join(f"    {canonical}" for canonical in sorted(unreachable))
        print(
            f"warning: {len(unreachable)} manifest(s) could not be fetched, so the "
            f"plan above may be missing repos:\n{listed}",
            file=sys.stderr,
        )
        if not args.dry_run:
            print(
                color_mod.error(
                    "error: refusing to create a sandbox from an incomplete dependency "
                    "closure. Gerrit rate-limits bursts of manifest fetches (HTTP 429, "
                    "retry-after 60) -- wait a minute and re-run, or pass --no-deps to "
                    "mount only the repos you name."
                ),
                file=sys.stderr,
            )
            return 1

    primary_canonical, primary_dir, _ = resolved[0]
    extra_dirs = [path for _canonical, path, _needs_clone in resolved[1:]]
    readonly_dirs = {
        path for (_spec, is_ro), (_canonical, path, _needs_clone) in zip(split, resolved) if is_ro
    }
    # Typed by hand, as opposed to found by the dependency walk --
    # expand_dependencies records an origin chain only for what it
    # discovered, so an empty chain is exactly "you asked for this one".
    # These keep their host branch instead of being reset to upstream
    # master; see wmf_sbx_setup.repos_to_leave_alone.
    requested_dirs = {
        path for _canonical, path, _needs_clone in resolved if path not in origins_by_path
    }

    # `sbx create`'s primary-workspace positional is unconditionally
    # read/write (see this module's docstring), so a ':ro' on the primary
    # can only ever be the in-sandbox remount -- which the sandboxed agent
    # can undo with `sudo mount -o remount,rw` (see build_sbx_command).
    # Say so rather than letting it look enforced.
    if primary_dir in readonly_dirs:
        print(
            f"warning: ':ro' on the primary workspace ({primary_dir}) cannot be "
            "enforced -- `sbx create` always mounts the primary read/write, so "
            "this is only an accident guard the sandbox can lift (see "
            "sbx/NOTES.md #22). Pass it as an extra instead if it must be "
            "genuinely read-only.",
            file=sys.stderr,
        )

    conflict = find_nested_mount_conflict([primary_dir] + extra_dirs)
    if conflict is not None:
        ancestor, descendant = conflict
        print(
            color_mod.error(
                f"error: {descendant!r} is inside {ancestor!r} -- sbx does not support "
                "mounting one workspace/bind mount inside another. Narrow one of the "
                "paths, or drop the redundant one."
            ),
            file=sys.stderr,
        )
        # The conflicting path may be one the user never typed -- e.g.
        # mounting ~/Wikimedia/Extensions wholesale while depending on
        # Translate discovers ULS *inside* that mount. Silently dropping
        # the discovered repo would surface later as failing tests in the
        # sandbox, so this fails; the least it can do is explain itself.
        for path in (descendant, ancestor):
            chain = origins_by_path.get(path)
            if chain:
                print(
                    f"  {path} wasn't named on the command line: it was pulled "
                    f"in as a dependency ({' -> '.join(chain)}). "
                    f"--no-deps mounts only what you name.",
                    file=sys.stderr,
                )
        return 1

    # No explicit --kit and no 'kit:' in the config: generate a MediaWiki
    # kit rather than requiring one -- see sbx/DESIGN-kit-generation.md.
    # canonicals_for_kit also identifies raw-path arguments (is_raw_path)
    # that happen to be a known repo via the config's exact rules, so kit
    # environment variables like MW_CORE_REPO still get wired up for
    # invocations that predate repos.yaml existing.
    # realpath: every repo directory below is realpath'd, and host_home is
    # what they are made relative to when the parallel tree is laid out
    # (parallel_path). A symlinked $HOME against realpath'd repos would
    # put every repo "outside host_home" and silently lose the parallel
    # path. Same reason the repos are realpath'd -- sbx/NOTES.md §66.
    host_home = os.path.realpath(os.path.expanduser("~"))
    port = None
    generated_kit_dir = None
    mcp_servers = []
    if kit is None:
        # First, because what the host will actually serve decides both
        # what the kit registers and what --static-mcp may name. Under
        # --dry-run this only *reads* the host: what it would register,
        # without registering it.
        if args.no_mcp:
            print(color_mod.dim(
                "+ (--no-mcp: no host-side MCP servers, no proxy in the kit)"),
                  file=sys.stderr)
        else:
            # --dry-run runs the identical preflight and stops on the
            # identical conditions; it just doesn't `mcp add`. That's the
            # point -- "would this create work?" has to include this.
            try:
                mcp_servers = ensure_host_mcp_servers(
                    run=run, register=not args.dry_run)
            except McpPreflightError:
                return 1
            if args.dry_run:
                print(color_mod.dim(
                    f"+ (would register on the host: "
                    f"{', '.join(mcp_servers) or 'nothing'})"),
                      file=sys.stderr)

        # Fixed, not picked: this only has to be free inside this one
        # sandbox's own network namespace, never across sandboxes -- see
        # publish_daemon_port for how the *host*-side port (which does need
        # to avoid cross-sandbox collisions) gets chosen, separately.
        port = kit_mod.DEFAULT_DAEMON_PORT
        resolved_for_kit = canonicals_for_kit(resolved, config.get("rules", []))
        plan = kit_mod.build_plan(
            resolved_for_kit,
            host_home=host_home,
            daemon_port=port,
            readonly_dirs=readonly_dirs,
            links=link_plan(resolved_for_kit, overrides=config.get("link_overrides") or {}),
            primary=primary_dir,
            upstreams=upstream_plan(resolved_for_kit, run=run),
            requested=requested_dirs,
            reset_all=args.reset_all,
        )
        spec = kit_mod.build_kit_spec(
            resolved_for_kit,
            extra_environment=config.get("extra_environment", {}),
            extra_packages=config.get("extra_packages", []),
            readonly_dirs=readonly_dirs,
            host_home=host_home,
            daemon_port=port,
            plan=plan,
            mcp_servers=mcp_servers,
        )
        if args.dry_run:
            print(
                color_mod.dim(
                    "+ (no --kit given and no 'kit:' in the config; would generate "
                    "this MediaWiki kit:)"
                ),
                file=sys.stderr,
            )
            print(color_mod.dim(kit_mod.dump_kit_yaml(spec)), file=sys.stderr, end="")
            print(
                color_mod.dim("+ (and this wmf-sbx-plan.json alongside it:)")
                + "\n" + json.dumps(plan, indent=2),
                file=sys.stderr,
            )
            if mcp_servers:
                # The spec above only names wmf-sbx-mcp.json; the file is
                # where the --tools allowlist actually lives, so a dry run
                # that wants to check what the agent may call has to be
                # able to see it without generating a kit.
                print(
                    color_mod.dim(
                        "+ (and this wmf-sbx-mcp.json, the proxy registrations "
                        "-- `--tools` is the allowlist:)"
                    ) + "\n" + json.dumps(kit_mod.mcp_config(mcp_servers),
                                          indent=2, sort_keys=True),
                    file=sys.stderr,
                )
        if args.kit_out:
            # Written even under --dry-run, and never cleaned up: the point
            # of the flag is a kit directory that outlives `sbx create`, so
            # `sbx kit validate` has something to read.
            kit = os.path.abspath(os.path.expanduser(args.kit_out))
            kit_mod.write_kit_dir(spec, kit, plan=plan, mcp_servers=mcp_servers)
            print(color_mod.dim(f"+ (generated kit written to {kit})"), file=sys.stderr)
        elif not args.dry_run:
            generated_kit_dir = tempfile.mkdtemp(prefix="wmf-sbx-kit-")
            kit_mod.write_kit_dir(spec, generated_kit_dir, plan=plan,
                                  mcp_servers=mcp_servers)
            kit = generated_kit_dir

    name_source = primary_canonical if primary_canonical is not None else os.path.basename(primary_dir)
    # Collision-check only the derived default -- an explicit --name is a
    # deliberate choice, and `sbx create` already reports a clear error if
    # that one turns out to be taken.
    name = args.name or unique_sandbox_name(default_sandbox_name(name_source), existing_sandbox_names())
    skills_flag = supported_skills_flag(run=run)
    if skills_flag is None:
        # Warn rather than fail: this is 0.42.1's real state, the fallback
        # (wmf-sbx-setup's read-only remount) is already in place, and
        # refusing to create a sandbox over a cross-sandbox risk the user
        # can't do anything about from here would be theatre. Loud,
        # though -- the gap is invisible otherwise, and it's the one a
        # reader of SECURITY.md §7.6 needs to know is still open.
        print(
            "! this sbx has no --skills/--no-share-skills flag, so the "
            "shared agent-skills store will be mounted writable "
            "(sbx/SECURITY.md §7.6). wmf-sbx-setup remounts it read-only "
            "from inside, which the agent can lift; upgrade to 0.43+ for "
            "the host-side fix (docker/sbx-releases#506).",
            file=sys.stderr,
        )
    cmd = build_sbx_command(name, kit, primary_dir, extra_dirs,
                            static_mcp=mcp_servers, skills_flag=skills_flag)

    if args.dry_run:
        print(color_mod.dim("+ " + " ".join(cmd)), file=sys.stderr)
        if port is not None:
            print(
                "\n" + color_mod.dim(
                    f"+ wmf-sbx ports {name} --publish {port}"
                    "  (after creation, to expose the git daemon on an "
                    "auto-assigned host port -- see "
                    "sbx/DESIGN-parallel-clone-tree.md §3)"
                ),
                file=sys.stderr,
            )
        return 0

    try:
        for canonical, path, needs_clone in resolved:
            if needs_clone:
                do_clone(canonical, path)
    except LaunchError as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1

    if not args.no_remotes:
        # Opportunistic: sweep out remotes belonging to sandboxes that were
        # destroyed by something other than `wmf-sbx-rm` (a plain `sbx rm`,
        # `sbx logout`). Cheap, never prompts, and it's what keeps a leaked
        # remote from outliving its sandbox long enough for the host port to
        # be recycled onto an unrelated one -- the failure mode
        # sbx/DESIGN-host-remotes.md §2 is written around.
        prune_removed_sandboxes(run=run)

    print(color_mod.dim("+ " + " ".join(cmd)), file=sys.stderr)
    env = os.environ.copy()
    # Defense in depth on top of the wmf-sbx wrapper (see build_sbx_command
    # and sbx/NOTES.md #8): still strip SSH_AUTH_SOCK here too, in case
    # this script is ever changed to call sbx directly again.
    env.pop("SSH_AUTH_SOCK", None)
    result = color_mod.run_with_color(cmd, env=env, run=run)
    if generated_kit_dir is not None:
        # sbx create only reads --kit at creation time (see CLAUDE.md
        # "Gotchas"); nothing inside the running sandbox needs it afterward.
        shutil.rmtree(generated_kit_dir, ignore_errors=True)
    if port is not None:
        # Only for a kit we generated -- an explicit --kit has no
        # wmf-sbx-setup in it and so no report to miss.
        #
        # Before the returncode check, not after: a create that failed *in*
        # the setup step is exactly when its report matters most. quiet on
        # that path, though -- a create that died earlier may have left no
        # sandbox to exec into, and "couldn't read the report" is not the
        # news then.
        report_setup_problems(name, run=run, quiet=result.returncode != 0)
    if result.returncode != 0:
        return result.returncode
    if port is not None:
        # The startup step has done this already, or is doing it now; this
        # run is what shows the engineer the result (§95).
        amend_workspace_claude_md(name, run=run)

    if port is not None:
        # The generated kit declares this port itself, so this is usually
        # just a lookup; an explicit --kit that doesn't declare it still
        # gets the publish (see ensure_published_host_port).
        host_port = ensure_published_host_port(name, port, run=run)
        if host_port is not None:
            candidates = parallel_tree_remotes(
                host_home, name, resolved, host_port, readonly_dirs
            )
            if args.no_remotes:
                print_remote_add_reminder(candidates)
            else:
                add_host_remotes(
                    name, candidates, port, host_port, run=run,
                    primary_dir=primary_dir,
                )
        else:
            print(
                f"warning: could not determine the published host port for "
                f"the sandbox's git daemon (sandbox port {port}) -- run "
                f"`wmf-sbx ports {name} --json` yourself to find it.",
                file=sys.stderr,
            )
    print_run_reminder(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
