---
name: feedback-docker-docs-fetching
description: How to get real content out of docs.docker.com when WebFetch returns only nav-shell stubs
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 0ff08caf-2396-4724-8f03-17f027863277
  modified: 2026-09-05T20:03:03.694Z
---

When `docs.docker.com` pages (especially deep CLI reference pages like
`reference/cli/sbx/create`) return only JS-shell nav content via WebFetch,
try two tiers before giving up:

1. Append `.md` to the URL (e.g. `docs.docker.com/ai/sandboxes.md`). Works
   for most top-level/mid-level pages. Fails (empty stub, just a heading)
   for deep CLI reference sub-pages.
2. Ask the user (cananian) to run `wget -r -p -np -x -k <url>` on the host
   and make the mirrored directory available. The `.md` siblings at that
   depth can be empty stubs, but the mirrored `index.html` files are
   server-rendered with full content — strip HTML tags programmatically
   (e.g. a small `python3 -c "...re.sub(r'<[^>]+>', ...)..."` snippet) to
   get readable text out of them.

**Why:** cananian explicitly said (2026-09-05, in the `wmf-claude` sbx
redesign project): "you can ask me to `wget` any missing information from
docs.docker.com in the future, for example if the CLI options change and
you need to re-fetch the docs to determine what has changed." This is a
standing offer, not a one-off — re-ask whenever `sbx` CLI flags might have
drifted from what's recorded in [[project_wmf_claude_sbx_redesign]], not
just when a page was unreachable the first time.

**How to apply:** Default to tier 1 first since it needs no round-trip.
Escalate to asking the user for a `wget` mirror only when tier 1 comes back
empty/stub-only, and be specific about which URL(s) need mirroring.
