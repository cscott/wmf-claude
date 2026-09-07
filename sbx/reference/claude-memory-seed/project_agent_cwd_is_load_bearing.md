---
name: project-agent-cwd-is-load-bearing
description: "In wmf-sbx, never move the sandboxed agent's starting directory off the real project path — cananian deferred a design over it (2026-09-14)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 1c38e171-1e24-4c67-b927-f7e6c71d49c8
  modified: 2026-09-14T21:25:38.059Z
---

The sandboxed agent's working directory must stay the host repo's own
path (e.g. `/home/cananian/Projects/Wikimedia/wmf-claude`). This is why
`sbx/NOTES.md` §39 does the `mount --move` + bind-over-the-literal-path
dance instead of just using the `/home/agent/<rel>` parallel clone.

**Why:** cananian, 2026-09-14 — earlier attempts to start the agent
somewhere else failed: the agent has no `/cd`, so "every startup began
with confusion about where it was and where the code it wanted to work
on was, and that confusion persisted through every restart when the
directory would be reset to the primary workspace." Claude Code also
keys its per-project state off that path (`~/.claude.json`'s `projects`
map, and `~/.claude/projects/-home-…/memory/`), so moving cwd would
break the memory-restore steps in `sbx/NOTES.md` §0.

**How to apply:** treat any proposal that changes where the agent wakes
up as a significant design change needing cananian's sign-off, not a
detail. A concrete casualty: the throwaway-scratch-primary design
(NOTES §82.3) that would have closed `SECURITY.md` §3 was deferred for
exactly this (§82.5); the route being tried instead is an upstream
feature request to allow `:ro` on sbx's first positional workspace
(§82.6). Mounting the right files at a wrong-named path is not a fix —
the *name* is what matters. See [[project-wmf-claude-sbx-redesign]].
