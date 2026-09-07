---
name: feedback-negative-findings-name-the-surface
description: "A negative finding is a claim about the method — say which surface you searched, don't generalize to 'it isn't there'"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1c38e171-1e24-4c67-b927-f7e6c71d49c8
  modified: 2026-09-14T18:05:02.730Z
---

"I did not find X" becomes "X is not there" only when the search covered
where X would be. Report the surface searched, not just the conclusion.

Three times in one thread on [[project-wmf-claude-sbx-redesign]] I wrote a
narrow absence as a general one:

- Surveyed `sbx settings` (29 keys), concluded "there is nothing for it" —
  but create-time flags are a different surface, and upstream's issue named
  one (`--no-share-skills`).
- WebFetched a GitHub issue's rendered page, saw no maintainer reply, wrote
  "open and unanswered for three weeks". There were three comments including
  the maintainer's accepted design. The rendered page does not carry the
  comment thread; `curl -s
  https://api.github.com/repos/<org>/<repo>/issues/<n>/comments` does, with
  author, date and body. (`author_association` is an unreliable maintainer
  signal — it read `NONE` for an assignee-maintainer; the assignee field is
  better.)
- Read a directory with plain `ls`, which hides dotfiles — an empty `ls`
  means "no non-hidden entries".

**Why:** each time, a real thing existed that the conclusion said did not,
and cananian had to correct it. The cost is not just the wrong sentence —
downstream reasoning gets built on it (e.g. "upstream isn't engaged, so
waiting isn't a strategy").

**How to apply:** when writing an absence, name the surface in the same
sentence — "the issue *body* does not mention a fix; I did not read the
comments" — and before generalizing, ask what other surface the thing could
live on. Sibling of [[feedback-verify-security-by-bypass]]: that one is
about positive claims tested the lazy way, this one about negative claims
scoped too widely. See also [[feedback-docker-docs-fetching]] for the
docs.docker.com stubs that render empty and invite the same mistake.
