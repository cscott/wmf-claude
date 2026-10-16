#!/usr/bin/env python3
"""Remove a sandbox: its Lima VM, its state, and the host-side `<name>`
git remotes and gc settings that wmf-sbx added for it.

Only a sandbox that wmf-sbx owns (it has a state file) is removed, so
that `rm` can never delete another Lima VM. `limactl delete` does not
ask, so this asks first, unless --force. The guard against unfetched
commits runs first, as before.

Usage:
  wmf-sbx-rm [--dry-run] [--keep-remotes] [-f|--force] NAME [NAME ...]
  wmf-sbx-rm --prune [--dry-run]
  wmf-sbx-rm --scan DIR [--dry-run]

--prune cleans up after a sandbox whose VM is gone but whose state file
is not. --scan DIR is for the reverse, a lost state file: in every git
repo under DIR, it removes the host remotes and gc entries of sandboxes
that no state file knows, and turns gc back on.
"""

import argparse
import os
import subprocess
import sys

from . import cli as cli_mod
from . import egress as egress_mod
from . import lima as lima_mod
from . import prompt as prompt_mod
from . import remotes as remotes_mod
from . import state as state_mod
from . import vm as vm_mod


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def remove_sandbox(name, force=False, lima=None, confirm=None):
    """Delete the sandbox's VM after a y/N question (unless `force`).
    Returns True if the VM is gone. `confirm` is input()-like; the default
    asks on the terminal, and with no terminal the answer is no."""
    # Destructive: Enter is no.
    if not force and not prompt_mod.Prompt(ask=confirm).confirm(
            f"Remove sandbox {name!r} and its VM {vm_mod.instance_name(name)}? "
            f"This cannot be undone.", default=False):
        return False
    print(f"+ limactl delete --force {vm_mod.instance_name(name)}", file=sys.stderr)
    vm_mod.delete(name, lima=lima)
    return vm_mod.status(name, lima=lima) is None


def live_sandbox_names(lima=None):
    """The sandboxes that have a VM: Lima instances named wmf-sbx-NAME."""
    lima = lima or lima_mod.Limactl()
    return {n[len(vm_mod.PREFIX):] for n in lima.instances() if n.startswith(vm_mod.PREFIX)}


def check_unfetched(state, run=subprocess.run, env=None, lima=None, confirm=None):
    """True if it's safe to destroy this sandbox. Refuses on unfetched
    commits, and equally on remotes we couldn't probe -- 'can't check'
    must not read as 'nothing to lose'. On a terminal, it offers to fetch
    the unfetched commits (`confirm`: see remove_sandbox), then checks
    again.

    A sandbox with no host remotes has nothing to check. Otherwise its VM
    must run, because the remotes reach it through `limactl shell`
    (git-remote-wmfsbx, lima-port/HANDOFF-LIMA.md §6.2)."""
    if not state.get("remotes"):
        return True
    vm_mod.ensure_running(state["name"], lima=lima,
                          log=lambda m: print(m, file=sys.stderr))
    findings, unreachable = remotes_mod.unfetched_tips(state, run=run)
    if findings and not unreachable:
        report_unfetched(findings)
        # A fetch only adds to the host: yes is the default.
        if not prompt_mod.Prompt(ask=confirm).confirm("Fetch them now?", default=True):
            return False
        for host_dir, remote, _shas in findings:
            print(f"+ git -C {host_dir} fetch {remote}", file=sys.stderr)
            run(["git", "-C", host_dir, "fetch", remote], stdin=subprocess.DEVNULL)
        findings, unreachable = remotes_mod.unfetched_tips(state, run=run)
    if not findings and not unreachable:
        return True
    if findings:
        report_unfetched(findings)
    if unreachable:
        print(
            "error: could not reach the sandbox's repositories, so there is "
            "no way to tell whether it holds unfetched work. Check "
            f"`wmf-sbx status {state['name']}`, or pass --force:",
            file=sys.stderr,
        )
        for host_dir, remote, reason in unreachable:
            print(f"  {host_dir} ({remote}): {reason}", file=sys.stderr)
    return False


def report_unfetched(findings):
    print(
        "error: the sandbox has commits the host has never fetched. "
        "removing the VM is unrecoverable -- fetch them first, or pass "
        "--force to destroy them:",
        file=sys.stderr,
    )
    for host_dir, remote, shas in findings:
        print(f"  {host_dir}: {len(shas)} unfetched tip(s) on {remote}", file=sys.stderr)
        for sha in shas:
            print(f"    git -C {host_dir} fetch {remote} {sha}", file=sys.stderr)


def cleanup_remotes(state, dry_run=False, run=subprocess.run):
    results = remotes_mod.remove_remotes(state, run=run, warn=warn, dry_run=dry_run)
    prefix = "would remove" if dry_run else "removed"
    for host_dir, remote, outcome in results:
        if outcome == "removed":
            print(f"  {prefix} {remote} from {host_dir}", file=sys.stderr)
    return results


def prune(dry_run=False, run=subprocess.run, env=None, lima=None):
    """Clean up after sandboxes whose VM went away without us (a plain
    `limactl delete`). Never deletes a VM, so it never asks. Returns the
    pruned names."""
    live = live_sandbox_names(lima)
    pruned = remotes_mod.prune_dead(live, run=run, warn=warn, dry_run=dry_run, env=env)
    prefix = "would remove" if dry_run else "removed"
    for name, results in pruned:
        print(f"pruning state for removed sandbox {name!r}:", file=sys.stderr)
        for host_dir, remote, outcome in results:
            if outcome == "removed":
                print(f"  {prefix} {remote} from {host_dir}", file=sys.stderr)
    return [name for name, _results in pruned]


def remove_one(name, args, run=subprocess.run, env=None, lima=None, confirm=None):
    """Returns 0 on success, non-zero on failure."""
    original_name = name
    try:
        name, state = vm_mod.resolve(name, env=env)
    except (state_mod.StateError, vm_mod.VmError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if name != original_name:
        print(f"+ resolved {original_name!r} to sandbox {name!r}", file=sys.stderr)

    if not args.force and not args.keep_remotes:
        if not check_unfetched(state, run=run, env=env, lima=lima, confirm=confirm):
            return 1

    if args.dry_run:
        print(f"+ would remove sandbox {name!r} (VM {vm_mod.instance_name(name)})",
              file=sys.stderr)
        if not args.keep_remotes:
            cleanup_remotes(state, dry_run=True, run=run)
            print(f"  would delete {state_mod.state_path(name, env)}", file=sys.stderr)
        return 0

    try:
        gone = remove_sandbox(name, force=args.force, lima=lima, confirm=confirm)
    except lima_mod.LimaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not gone:
        print(
            f"sandbox {name!r} is not removed -- leaving its host remotes and "
            f"state alone.",
            file=sys.stderr,
        )
        return 1

    # The VM is gone, so its egress CA and proxy config serve nothing.
    egress_mod.remove(name, env=env)
    if args.keep_remotes:
        return 0
    cleanup_remotes(state, run=run)
    state_mod.delete(name, env=env)
    return 0


def main(argv=None, run=subprocess.run, env=None, lima=None, confirm=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", nargs="*", help="Sandbox(es) to remove")
    parser.add_argument(
        "--dry-run", action=argparse.BooleanOptionalAction, default=False,
        help="Print what would be removed; change nothing",
    )
    parser.add_argument(
        "--keep-remotes", action=argparse.BooleanOptionalAction, default=False,
        help="Remove the sandbox but leave its host remotes and state in place",
    )
    parser.add_argument(
        "-f", "--force", action=argparse.BooleanOptionalAction, default=False,
        help="Skip both the unfetched-commit guard and the confirmation",
    )
    parser.add_argument(
        "--prune", action=argparse.BooleanOptionalAction, default=False,
        help="Clean up state and remotes for sandboxes that no longer exist",
    )
    parser.add_argument(
        "--scan", metavar="DIR", action="append", default=[],
        help="In every git repo under DIR, remove the host remotes and gc "
        "entries of sandboxes that have no state file (a lost state file)",
    )
    args, _config = cli_mod.parse_args(parser, argv, "rm")

    if not args.name and not args.prune and not args.scan:
        parser.error("give a sandbox name, --prune or --scan DIR")

    status = 0
    for root in args.scan:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            print(f"error: --scan {root}: not a directory", file=sys.stderr)
            status = 1
            continue
        # A warm snapshot is a borrower too (snapshot.py): keep its entries.
        from . import snapshot as snapshot_mod
        known = list(state_mod.list_names(env=env)) + [
            snapshot_mod.borrower_name(k) for k in snapshot_mod.list_keys(env)]
        found = remotes_mod.scan_orphans(root, known, run=run,
                                         warn=warn, dry_run=args.dry_run)
        prefix = "would remove" if args.dry_run else "removed"
        for host_dir, what in found:
            print(f"  {prefix} {what} in {host_dir}", file=sys.stderr)
        if not found:
            print(f"nothing of an unknown sandbox under {root}.", file=sys.stderr)
    if args.prune:
        try:
            pruned = prune(dry_run=args.dry_run, run=run, env=env, lima=lima)
        except lima_mod.LimaError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not pruned:
            print("nothing to prune.", file=sys.stderr)
    for name in args.name:
        status = remove_one(name, args, run=run, env=env, lima=lima,
                            confirm=confirm) or status
    return status


if __name__ == "__main__":
    sys.exit(main())
