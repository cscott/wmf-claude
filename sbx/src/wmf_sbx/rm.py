#!/usr/bin/env python3
"""Remove a sandbox *and* the host-side `<name>` git remotes (named the
same as the sandbox itself, e.g. `sbx-cite` -- see remotes.remote_name_for)
`wmf-sbx-create` added for it. See sbx/DESIGN-host-remotes.md §5.

Plain `sbx rm` destroys the sandbox and leaves those remotes behind in a
dozen host `.git/config` files. That is not merely untidy: host ports get
recycled (confirmed on the host, 2026-09-07), so a stale
`sbx-cite` remote can later point at a live daemon serving an
unrelated sandbox's tree, and `git fetch sbx-cite` will silently
succeed with the wrong objects.

Two behaviours of `sbx rm`, both confirmed on the host, shape this:

  * it **prompts** (`Remove sandbox 'NAME'? This cannot be undone.
    (y/N):`) rather than refusing on a running sandbox -- so we inherit
    stdio and let the irreversible confirmation come from the tool that
    actually does the destroying, instead of piping `y` at it;
  * its **exit status is uninformative in both directions**: a
    nonexistent sandbox exits 1, and *declining the prompt exits 0*. So
    the removal is confirmed by re-checking `wmf-sbx ls` afterward, never
    by the exit code. Trusting exit 0 would strip every remote off the
    host repos of a sandbox the engineer had just declined to delete.

Usage:
  wmf-sbx-rm [--dry-run] [--keep-remotes] [-f|--force] NAME [NAME ...]
  wmf-sbx-rm --prune [--dry-run]
"""

import argparse
import os
import subprocess
import sys

from . import create as create_mod
from . import remotes as remotes_mod
from . import state as state_mod


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def remove_sandbox(name, force=False, run=subprocess.run):
    """Runs `wmf-sbx rm NAME` with stdio inherited so its confirmation
    prompt reaches the terminal, then reports whether the sandbox is
    *actually* gone -- see this module's docstring on why the exit code
    can't be trusted. Returns True if it's gone.

    `--upstream`: since sbx/NOTES.md "wmf-sbx redirects", plain `wmf-sbx
    rm` reaches *this* module again, not the real `sbx rm` this function
    needs -- without it, this call recurses into itself forever."""
    cmd = [create_mod.WMF_SBX, "--upstream", "rm"]
    if force:
        # Long spelling only: `--force` is verified against a real sbx,
        # `-f` is not.
        cmd.append("--force")
    cmd.append(name)
    print("+ " + " ".join(cmd), file=sys.stderr)
    run(cmd)
    return name not in create_mod.existing_sandbox_names(run=run)


def check_unfetched(state, run=subprocess.run, env=None):
    """True if it's safe to destroy this sandbox. Refuses on unfetched
    commits, and equally on remotes we couldn't probe -- 'can't check'
    must not read as 'nothing to lose'.

    Always starts the sandbox and waits for its daemon before ever
    looking up the published port, rather than only doing so when the
    port lookup comes back empty. A *stopped* sandbox publishes no port
    at all, so that used to be a reliable enough signal -- but sbx's own
    idle-auto-stop (sbx/NOTES.md §81.2) restarts the container on the
    `sbx exec` inside `start_sandbox` itself, and the port mapping can be
    back before the git daemon inside is: probing in between gets
    `Connection reset by peer` from Docker's port proxy (§36.1), an
    "unreachable" that means nothing except that we asked too early,
    but that the port-already-published shortcut had no way to catch.
    `start_sandbox` is a no-op (one `sbx exec ... true`) on a sandbox
    that's already up, so doing this unconditionally costs nothing in
    the already-running case.

    The recorded remote URLs are refreshed after that: the daemon's host
    port changes on every container start (wmf_sbx_create.refresh_host_port),
    so probing the URL we wrote at create time would report every remote
    as unreachable and push the engineer straight to --force -- exactly
    the destroy-without-looking this guard exists to prevent."""
    if create_mod.start_sandbox(state["name"], run=run):
        create_mod.wait_for_daemon(
            state["name"],
            state.get("daemonPort") or create_mod.kit_mod.DEFAULT_DAEMON_PORT,
            run=run,
        )
    create_mod.refresh_host_port(state["name"], state, run=run, env=env)
    findings, unreachable = remotes_mod.unfetched_tips(state, run=run)
    if not findings and not unreachable:
        return True
    if findings:
        print(
            "error: the sandbox has commits the host has never fetched. "
            "`sbx rm` is unrecoverable -- fetch them first, or pass "
            "--force to destroy them:",
            file=sys.stderr,
        )
        for host_dir, remote, shas in findings:
            print(f"  {host_dir}: {len(shas)} unfetched tip(s) on {remote}", file=sys.stderr)
            for sha in shas:
                print(f"    git -C {host_dir} fetch {remote} {sha}", file=sys.stderr)
    if unreachable:
        print(
            "error: could not reach the sandbox's git daemon, so there is no "
            "way to tell whether it holds unfetched work. The daemon comes "
            "back on its own with the container (kit startup command, "
            "sbx/NOTES.md §33.2), and this already started the sandbox and "
            "waited for it -- check `wmf-sbx ls`, and "
            f"`wmf-sbx exec {state['name']} cat /var/log/sbx-kit-startup.log`, "
            "or pass --force:",
            file=sys.stderr,
        )
        for host_dir, remote, reason in unreachable:
            print(f"  {host_dir} ({remote}): {reason}", file=sys.stderr)
    return False


def cleanup_remotes(state, dry_run=False, run=subprocess.run):
    results = remotes_mod.remove_remotes(state, run=run, warn=warn, dry_run=dry_run)
    prefix = "would remove" if dry_run else "removed"
    for host_dir, remote, outcome in results:
        if outcome == "removed":
            print(f"  {prefix} {remote} from {host_dir}", file=sys.stderr)
    return results


def prune(dry_run=False, run=subprocess.run, env=None):
    """Clean up after sandboxes that went away without us. Never invokes
    `sbx rm` itself, so it never prompts. Returns the pruned names."""
    live = create_mod.existing_sandbox_names(run=run)
    pruned = remotes_mod.prune_dead(live, run=run, warn=warn, dry_run=dry_run, env=env)
    prefix = "would remove" if dry_run else "removed"
    for name, results in pruned:
        print(f"pruning state for removed sandbox {name!r}:", file=sys.stderr)
        for host_dir, remote, outcome in results:
            if outcome == "removed":
                print(f"  {prefix} {remote} from {host_dir}", file=sys.stderr)
    return [name for name, _results in pruned]


def remove_one(name, args, run=subprocess.run, env=None):
    """Returns 0 on success, non-zero on failure."""
    original_name = name
    try:
        name = state_mod.resolve_name_arg(name, env=env)
        state_mod.validate_name(name)
        state = state_mod.load(name, env=env)
    except state_mod.StateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if name != original_name:
        print(f"+ resolved {original_name!r} to sandbox {name!r}", file=sys.stderr)

    if state is None:
        warn(
            f"no wmf-sbx state recorded for {name!r} -- removing the sandbox, "
            f"but any host remotes it has must be cleaned up by hand "
            f"(`git remote remove {remotes_mod.remote_name_for(name)}`)."
        )

    if state is not None and not args.force and not args.keep_remotes:
        if not check_unfetched(state, run=run, env=env):
            return 1

    if args.dry_run:
        print(f"+ would remove sandbox {name!r}", file=sys.stderr)
        if state is not None and not args.keep_remotes:
            cleanup_remotes(state, dry_run=True, run=run)
            print(f"  would delete {state_mod.state_path(name, env)}", file=sys.stderr)
        return 0

    if not remove_sandbox(name, force=args.force, run=run):
        print(
            f"error: sandbox {name!r} is still present after `wmf-sbx rm` -- "
            f"leaving its host remotes and state alone.",
            file=sys.stderr,
        )
        return 1

    if state is None or args.keep_remotes:
        return 0
    cleanup_remotes(state, run=run)
    state_mod.delete(name, env=env)
    return 0


def main(argv=None, run=subprocess.run, env=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", nargs="*", help="Sandbox(es) to remove")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be removed; change nothing",
    )
    parser.add_argument(
        "--keep-remotes", action="store_true",
        help="Remove the sandbox but leave its host remotes and state in place",
    )
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="Skip both the unfetched-commit guard and `sbx rm`'s own confirmation",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="Clean up state and remotes for sandboxes that no longer exist",
    )
    args = parser.parse_args(argv)

    if not args.name and not args.prune:
        parser.error("give a sandbox name, or --prune")

    status = 0
    if args.prune:
        pruned = prune(dry_run=args.dry_run, run=run, env=env)
        if not pruned:
            print("nothing to prune.", file=sys.stderr)
    for name in args.name:
        status = remove_one(name, args, run=run, env=env) or status
    return status


if __name__ == "__main__":
    sys.exit(main())
