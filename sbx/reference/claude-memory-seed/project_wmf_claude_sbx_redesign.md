---
name: project-wmf-claude-sbx-redesign
description: "Designing an sbx (Docker Sandboxes)-based replacement for wmf-claude's nono-based sandboxing, in the wmf-claude repo's sbx/ directory"
metadata:
  node_type: memory
  type: project
  originSessionId: 0ff08caf-2396-4724-8f03-17f027863277
  modified: 2026-09-11T17:16:19.853Z
---

cananian is designing a new sandbox system for Wikimedia MediaWiki
development, to run under Docker's `sbx` ("Docker Sandboxes", microVM-based)
instead of the existing nono-based wrapper in the `wmf-claude` repo. All of
it lives under `sbx/` in the `wmf-claude` checkout.

**Read `sbx/NOTES.md` §0 first** — the orientation section for a fresh
Claude instance, plus its "Still to do" list at the end. It points at
everything else: `sbx/SECURITY.md` (the sbx threat model — separate from the
repo-root `SECURITY.md`, which is about the nono backend), the
`sbx/DESIGN-*.md` docs, `sbx/src/wmf_sbx/` (the importable package behind
the tooling — `create`, `resolve`, `resume`, `start`, `exec`, `rm`, plus
`ls_remotes`, `state`, `kit`, `deps`, `remotes`), `sbx/bin/` (bash plus
thin `wmf-sbx-<command>` wrappers, safe to add to `$PATH`), and
`sbx/tests/`. `sbx/RESUME.md` used to be the first read whenever a sandbox
recreate was pending (an sbx upgrade, etc.) — it's a recurring-but-
transient sibling, written before such a recreate and folded back into
`NOTES.md` and deleted once the recreate is done
and measured (most recently 2026-09-15, after the 0.42.1 → 0.43.0-rc3
upgrade in `NOTES.md` §83-§84); its absence just means nothing is in
flight right now.

**Why:** cananian wants (a) to streamline their own multi-repo MediaWiki dev
workflow (currently long, error-prone hand-written `sbx create` command
lines), (b) to import the best parts of `wmf-claude` (MCP servers, skills)
into the new setup, (c) to audit and track security-posture differences
against the nono-based approach, and (d) to upstream as much as possible so
other WMF engineers can replicate it — hence the emphasis on never
hardcoding cananian's own directory layout.

**How to apply:** Treat the files in the working tree as the source of
truth; this memory is a pointer, not a substitute for reading them. Add new
measurements to `sbx/NOTES.md` as numbered sections with a date and a
`[who, where, when]` attribution, and never state a sandbox behaviour as
fact without either measuring it here or getting a host transcript —
see [[feedback_test_image_yourself_in_sandbox]] and
[[feedback_verify_security_by_bypass]]. Commit finished work without being
asked ([[feedback_commit_proactively_in_sbx]]); cananian rebases away
exploratory commits before anything goes upstream, so frequent small
commits are welcome.

**Never invoke bare `sbx`** — always `sbx/bin/wmf-sbx`. `sbx` forwards the
host SSH agent into every sandbox when `SSH_AUTH_SOCK` is set, and the
forwarding is sticky to the daemon (`sbx/NOTES.md` §8). The wrapper unsets
it, and also refuses `--cloud`.

Status as of 2026-09-11: Layers A, B, and C are all built and have been run
end to end in a real sandbox — one command (`wmf-sbx-create Translate`)
produces a working wiki serving `Special:Version` in ~65s, with a parallel
writable tree per repo, host-side `sandbox-<name>` git remotes fed by a
read-only in-sandbox git daemon, and a generated kit spec. The
0.39.0 → 0.42.1 upgrade is done and fully
re-measured (`NOTES.md` §47) — confirmed on `v0.42.1`, no code changes
needed beyond what already landed ahead of it. 613 unit tests
(`python3 -m unittest discover -s sbx/tests`) plus 126 template tests
(`./tests/test-templates.sh`). Recent additions: `wmf-sbx-start` (recover
a stopped sandbox's git remotes without attaching an agent, §50) and
`wmf-sbx-exec` (same restore, then a one-off `sbx exec`, §51); all python
moved out of `sbx/bin` into a proper `sbx/src/wmf_sbx/` package (§49).
