---
name: feedback-commit-proactively-in-sbx
description: "In this wmf-claude sbx sandbox, commit finished work locally without waiting to be asked each time"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 49f8a5ce-9015-42c8-a29e-039a39cb55ea
  modified: 2026-09-05T23:46:02.092Z
---

Commit finished work to git as a matter of course in this sandbox, rather
than waiting for an explicit "commit this" each time.

**Why:** cananian reviews work done inside an `sbx`-based sandbox from the
*host*, via the git daemon / remote that `wmf-sbx-create` prints at
creation time (e.g. `git://127.0.0.1:PORT/<repo>`, remote name
`sandbox-<sandbox-name>`) — see [[user_cananian_workflow]] and
`sbx/NOTES.md`. Uncommitted changes in the sandbox working tree are
invisible to that pull path, so leaving work uncommitted defeats the
point of the sandbox being reviewable from outside. This is narrower than
(and takes precedence over, in this project) the general default of only
committing when explicitly asked.

**How to apply:** After completing a coherent unit of work in this repo
inside an sbx sandbox, commit it (still following normal git hygiene —
meaningful message, don't bundle unrelated changes, don't force-push or
rewrite history without asking). This does not extend to pushing to a
remote or opening a PR — those still require an explicit ask.
