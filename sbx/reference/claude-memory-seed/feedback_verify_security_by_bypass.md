---
name: feedback-verify-security-by-bypass
description: "Test a security property by attempting the bypass, not by observing current state"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2abac7a6-4a1f-4443-b5dc-3c2cf501b189
  modified: 2026-09-07T07:06:53.441Z
---

When checking whether a restriction actually holds, attempt to defeat it —
don't just observe that it is currently in effect. Observing state tells you
what the config *is*, not whether anything stops it being changed.

Concretely, for read-only mounts in [[project-wmf-claude-sbx-redesign]]: a
`touch` that fails proves only the current mount flags. Run
`sudo mount -o remount,rw <path>` and *then* try to write. Note that both
`mount` and `/proc/mounts` will report `rw` after a remount that changed
nothing real, so neither is evidence — only an attempted write distinguishes
an sbx create-time `:ro` (enforced host-side, below the sandbox's reach) from
one we remounted ourselves (escapable, since the agent has passwordless sudo).

**Why:** I twice reported "read-only enforcement genuinely works" from `touch`
sweeps and was wrong both times; cananian caught it. The gap let a real
sandbox-escape hole (host files writable despite `:ro`) sit undetected.

**How to apply:** Before reporting any isolation/permission property as
working, name the attacker (here: the sandboxed agent, which has sudo), then
try the thing that attacker would do. Same for the CVE-2022-24765 lesson in
[[feedback-commit-proactively-in-sbx]]'s sibling work — a single piece of
indirect evidence (`/proc/<pid>/fd` showing no socket) is not a diagnosis;
find a working control to compare against before writing it up as confirmed.
