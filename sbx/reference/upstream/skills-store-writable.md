# The shared agent-skills store is writable from inside a sandbox — our reproduction record

**Status: DO NOT FILE — already filed, and already accepted.**
[`docker/sbx-releases#506`](https://github.com/docker/sbx-releases/issues/506),
*"sbx 0.39.0 enables back door communication between agents in separate
sandboxes by default"*, `cash`, 2026-08-25, against 0.39.0. Assigned to
the maintainer `rcjsuen`, who replied within the hour: the mount **will
be read-only by default**, with a `--shared-skills-rw` opt-in, "only
affect[ing] new sandboxes as we cannot change the bind mount behaviour
dynamically after the fact". Still open, and not shipped as of 0.42.1.

This page was written on 2026-09-14 as an issue to file, before we found
#506. It is kept because the reproduction below is the evidence behind
`SECURITY.md` §7.6 and `NOTES.md` §79 — and because two of its findings
were not in #506 and are now, contributed as a comment (cscott,
2026-09-14): that a writer reaches sandboxes on unrelated repositories,
in other projects, and not yet created; that modifying or deleting
another sandbox's skills interferes with it even without malice; and
that the change **outlives removal of the sandbox that made it**.

**The body below is superseded as a report** and kept as a record. Read
`NOTES.md` §79.4–§79.6 for the current state. One correction the body
does not carry: it says "I could not find a setting that disables,
narrows or read-only-mounts it", scoping that to `sbx settings`. Checked
since on the host, `sbx create --help` on 0.42.1 has no skills flag
either — neither the proposed `--shared-skills-rw` nor the
`--no-share-skills` that #506's own body refers to as existing (§79.6).
So the draft's claim turns out to hold more broadly than it was entitled
to claim at the time. Nothing below is a working exploit; it is `touch`,
`stat` and `rm`.

Everything below the line is that body, as drafted.

---

**Title:** Shared agent-skills directory is mounted writable into every sandbox, allowing one sandbox to plant or delete skills in all the others, with no setting to disable it

### Summary

`sbx` mounts one host directory —
`~/.local/state/sandboxes/sandboxes/agent-skills` — into **every** sandbox
on the machine, at `/home/agent/.claude/skills`, with `rw`. It is not
per-sandbox, and it is not container state: it outlives `sbx rm` of any
sandbox that wrote to it.

The contents of that directory are agent skills — that is, **instructions
a model reads and acts on** at session start. So a sandbox that can write
there can inject instructions into every other sandbox on the host,
including sandboxes working on unrelated repositories, sandboxes belonging
to a different project, and sandboxes that do not exist yet. It can also
**modify or delete** skills other sandboxes depend on.

Every other resource is isolated per sandbox. This one mutable directory
is shared, and it is the one whose contents are instructions.

I could not find a setting that disables, narrows or read-only-mounts it:
`sbx settings list --json` on 0.42.1 has 29 keys and none of them touch
the skills store.

### Environment

- `sbx` 0.42.1 (client and server), revision `cc6e400a…`, api 0.28.0
- host: Linux, sandboxes created with the `claude` agent plus a local kit
- two sandboxes, created independently from different kits, named
  `gerrit-probe` and `wmf-claude-sbx`

### Reproduction

Two sandboxes are enough; they need nothing in common.

**1. The mount is shared and `rw`.** Inside either sandbox:

```console
agent@gerrit-probe:~$ grep skills /proc/self/mountinfo
… /cananian/.local/state/sandboxes/sandboxes/agent-skills /home/agent/.claude/skills rw,nosuid,nodev,relatime - virtiofs none rw
```

`sbx create`'s own resolve output says the same thing, for every sandbox:

```
skills  /home/cananian/.local/state/sandboxes/sandboxes/agent-skills
        → /home/agent/.claude/skills · 0 folders
```

**2. It is writable — by attempting the write, not by reading the mode:**

```console
agent@gerrit-probe:~$ touch ~/.claude/skills/.probe && echo WRITABLE
WRITABLE
```

**3. The write is visible in a different sandbox.** In `wmf-claude-sbx`,
57 seconds later:

```console
agent@wmf-claude-sbx:~$ stat -c '%n  mtime=%y' ~/.claude/skills/.*
.crossprobe-from-wmf-claude-sbx  mtime=2026-09-14 13:13:24   ← written here
.probe                           mtime=2026-09-14 13:12:27   ← written in gerrit-probe
```

**4. And the second sandbox can delete the first sandbox's file:**

```console
agent@wmf-claude-sbx:~$ rm -f ~/.claude/skills/.probe
agent@wmf-claude-sbx:~$ echo $?
0
```

So the channel is bidirectional and not append-only.

A note for anyone reproducing this: use `ls -la`. Plain
`ls ~/.claude/skills/` printed nothing throughout the above, because the
probe files start with a dot — which is also exactly what a planted file
would do. An empty `ls` here means "no non-hidden entries", not "empty".

### Impact

A single compromised or prompt-injected agent escapes the sandbox
boundary in the direction that matters for an agent: not to the host
filesystem, but to **the other agents**. It can

- plant a skill that every other sandbox on the host loads at its next
  session start, including sandboxes created after the compromised one
  was removed;
- replace a skill another sandbox relies on, silently, with no signal on
  the receiving side;
- delete skills, breaking other sandboxes in ways that look like local
  corruption.

The persistence is the part I would emphasise. `sbx rm` of the
compromised sandbox does not remove what it wrote, so the usual response
to a suspect sandbox — throw it away and make a new one — produces a new
sandbox that loads the planted skill on its first start.

Nothing inside a sandbox can fix this, which is why it is a report and
not a patch: the agent is passwordless root in its own sandbox, so
file modes, tool-permission denies and read-only remounts applied from
inside are all on the wrong side of the boundary. We apply a read-only
remount anyway (see "Workaround" below) and it stops a careless write,
but `sudo mount -o remount,rw,bind ~/.claude/skills` lifts it in one
command, as it must.

### Suggested fix

Any one of these would close it; the first is the smallest change:

1. **Mount the store `ro` by default**, and require an explicit opt-in
   (a `sbx settings` key, or a per-sandbox flag) for a sandbox that is
   meant to author skills. Reading shared skills is the common case;
   writing them is not.
2. **Make it per-sandbox**, with the shared directory as a read-only
   lower layer — an overlay, or a copy at create time.
3. At minimum, **add a setting that turns the shared mount off**, so a
   host that does not want cross-sandbox skill sharing can decline it.
   Today there is no such key.

(1) also matches what `sbx` already does elsewhere: extra workspaces can
be mounted read-only, and the reasoning is the same.

### Workaround for anyone hitting this before it is fixed

Remount it read-only from inside the sandbox, on every container start —
the mount is namespace state, so a `sbx stop` discards the remount:

```bash
sudo mount -o remount,ro,bind /home/agent/.claude/skills
```

In a kit, that belongs in a `setup.startup` command running as root
(`"user": "0"`), not only in `commands.install`, because install runs
once and the mount comes back `rw` on every start. This is a second
layer, not a boundary — the agent can undo it — but it converts the
default from "writable unless someone noticed" to "writable on purpose".

### Related

`docker/sbx-releases#556` is the same shape one layer down: a read-only
remount made from inside a sandbox is not enforcement, because the agent
is root in there. The fix for both is that the host has to be the one
saying `ro`.
