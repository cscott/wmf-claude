#!/usr/bin/env python3
"""Remove a sandbox: its Lima VM, its state, and the host-side `<name>`
git remotes and gc settings that wmf-sbx added for it (see
sbx/DESIGN-host-remotes.md §5).

Only a sandbox that wmf-sbx owns (it has a state file) is removed, so
that `rm` can never delete another Lima VM. `limactl delete` does not
ask, so this asks first, unless --force. The guard against unfetched
commits runs first, as before.

Usage:
  wmf-sbx-rm [--dry-run] [--keep-remotes] [-f|--force] NAME [NAME ...]
  wmf-sbx-rm --prune [--dry-run]
"""

import argparse
import subprocess
import sys

from . import lima as lima_mod
from . import remotes as remotes_mod
from . import state as state_mod
from . import vm as vm_mod


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def remove_sandbox(name, force=False, lima=None, confirm=input):
    """Delete the sandbox's VM after a y/N question (unless `force`).
    Returns True if the VM is gone."""
    if not force:
        answer = confirm(f"Remove sandbox {name!r} and its VM "
                         f"{vm_mod.instance_name(name)}? This cannot be undone. (y/N): ")
        if answer.strip().lower() not in ("y", "yes"):
            return False
    print(f"+ limactl delete --force {vm_mod.instance_name(name)}", file=sys.stderr)
    vm_mod.delete(name, lima=lima)
    return vm_mod.status(name, lima=lima) is None


def live_sandbox_names(lima=None):
    """The sandboxes that have a VM: Lima instances named wmf-sbx-NAME."""
    lima = lima or lima_mod.Limactl()
    return {n[len(vm_mod.PREFIX):] for n in lima.instances() if n.startswith(vm_mod.PREFIX)}


def check_unfetched(state, run=subprocess.run, env=None, lima=None):
    """True if it's safe to destroy this sandbox. Refuses on unfetched
    commits, and equally on remotes we couldn't probe -- 'can't check'
    must not read as 'nothing to lose'.

    A sandbox with no host remotes has nothing to check. Otherwise its VM
    must run, because the remotes reach it through `limactl shell`
    (git-remote-wmfsbx, lima-port/HANDOFF-LIMA.md §6.2)."""
    if not state.get("remotes"):
        return True
    vm_mod.ensure_running(state["name"], lima=lima,
                          log=lambda m: print(m, file=sys.stderr))
    findings, unreachable = remotes_mod.unfetched_tips(state, run=run)
    if not findings and not unreachable:
        return True
    if findings:
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


def remove_one(name, args, run=subprocess.run, env=None, lima=None, confirm=input):
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
        if not check_unfetched(state, run=run, env=env, lima=lima):
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

    if args.keep_remotes:
        return 0
    cleanup_remotes(state, run=run)
    state_mod.delete(name, env=env)
    return 0


def main(argv=None, run=subprocess.run, env=None, lima=None, confirm=input):
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
        help="Skip both the unfetched-commit guard and the confirmation",
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
