#!/usr/bin/env python3
"""Copy sbx's own CLAUDE.md text out of a sandbox, and check the edits
against it.

Every generated kit edits the CLAUDE.md that sbx writes in the parent
directory of the primary workspace (`wmf-sbx-setup --claude-md`, see
sbx/NOTES.md §95). The edits name sections by heading, so they survive
small changes to sbx's text. A larger change makes them fail, and
`wmf-sbx create`, `resume` and `start` then print the problems. This
command is the next step:

    wmf-sbx refresh-claude-md NAME [--dry-run]
    wmf-sbx refresh-claude-md --file PATH [--dry-run]

It reads the text sbx wrote in sandbox NAME (the copy that
`--claude-md` kept before it edited the file, or the file itself if it
is not edited), writes it to sbx/patches/sbx-claude-md/upstream.md, shows
how it differs from the previous snapshot, and applies
sbx/patches/sbx-claude-md/edits.json to it. It exits 1 if an edit does
not apply. Change edits.json until it exits 0, then run the unit tests:
one of them applies the edits to the snapshot.

--file reads the text from a local file instead of a sandbox. --dry-run
does not write the snapshot.
"""

import argparse
import difflib
import hashlib
import json
import subprocess
import sys

from . import create as create_mod
from . import kit as kit_mod
from . import setup as setup_mod

BEGIN = "===wmf-sbx-refresh-claude-md begin==="
END = "===wmf-sbx-refresh-claude-md end==="


def exec_cat(name, path, run=subprocess.run):
    """The exact contents of `path` in sandbox `name`, or None. The
    sentinels keep any text that `sbx exec` prints itself out of the
    result. The newline before END is ours, so a file that has no final
    newline keeps that."""
    result = run(
        # --upstream: real `sbx exec` (sbx/NOTES.md "wmf-sbx redirects").
        [create_mod.WMF_SBX, "--upstream", "exec", name, "--", "sh", "-c",
         f'printf "%s\\n" "{BEGIN}" && cat -- "$1" && printf "\\n%s\\n" "{END}"',
         "sh", path],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    out = result.stdout or ""
    start, end = out.find(BEGIN + "\n"), out.rfind("\n" + END)
    if start < 0 or end < start + len(BEGIN):
        return None
    return out[start + len(BEGIN) + 1:end]


def read_upstream(name, run=subprocess.run):
    """(text, where) for sbx's own text in sandbox `name`. Raises
    RuntimeError if there is no unedited text to read."""
    saved = exec_cat(name, setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM, run=run)
    if saved is not None:
        return saved, setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM
    plan_text = exec_cat(name, setup_mod.SANDBOX_PLAN_FILE, run=run)
    try:
        plan = json.loads(plan_text or "")
    except ValueError:
        plan = None
    path = kit_mod.workspace_claude_md_path(plan if isinstance(plan, dict) else None)
    if path is None:
        raise RuntimeError(
            f"could not read {setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM} or "
            f"{setup_mod.SANDBOX_PLAN_FILE} in {name!r}; is it a wmf-sbx "
            f"sandbox, and is it running?")
    live = exec_cat(name, path, run=run)
    if live is None:
        raise RuntimeError(f"could not read {path} in {name!r}")
    if live.startswith(setup_mod.CLAUDE_MD_MARKER):
        raise RuntimeError(
            f"{path} in {name!r} is already edited, and the copy of sbx's "
            f"text ({setup_mod.SANDBOX_CLAUDE_MD_UPSTREAM}) is missing")
    return live, path


def main(argv=None, run=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("name", nargs="?", help="Sandbox to read sbx's text from")
    source.add_argument("--file", help="Read sbx's text from this local file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do not write the snapshot")
    args = parser.parse_args(argv)

    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        where = args.file
    else:
        try:
            text, where = read_upstream(args.name, run=run)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    print(f"sbx's text: {where} ({len(text.encode('utf-8'))} bytes, "
          f"sha256 {digest})")

    snapshot = kit_mod.CLAUDE_MD_UPSTREAM_SNAPSHOT
    try:
        with open(snapshot, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = ""
    if old == text:
        print(f"no change from {snapshot}")
    else:
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(keepends=True), text.splitlines(keepends=True),
            fromfile="upstream.md (snapshot)", tofile="upstream.md (sandbox)"))
        if args.dry_run:
            print(f"(--dry-run: {snapshot} is unchanged)")
        else:
            with open(snapshot, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"wrote {snapshot}")

    with open(kit_mod.CLAUDE_MD_EDITS_SOURCE, encoding="utf-8") as f:
        data = json.load(f)
    _new, failures = setup_mod.apply_claude_md_edits(text, data["edits"])
    if failures:
        print(f"\n{len(failures)} edit(s) in {kit_mod.CLAUDE_MD_EDITS_SOURCE} "
              f"do not apply:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("Change the edits to match the new text, and run this again.",
              file=sys.stderr)
        return 1
    print(f"all {len(data['edits'])} edit(s) apply. If sbx's text changed, "
          f"set \"upstream\" in {kit_mod.CLAUDE_MD_EDITS_SOURCE} to the sbx "
          f"version, and check that the edited text still makes sense.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
