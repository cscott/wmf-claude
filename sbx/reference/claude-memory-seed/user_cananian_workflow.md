---
name: user-cananian-workflow
description: "C. Scott Ananian's WMF engineering role, MediaWiki multi-repo workflow, and git/Gerrit habits"
metadata: 
  node_type: memory
  type: user
  originSessionId: 0ff08caf-2396-4724-8f03-17f027863277
  modified: 2026-09-05T20:03:40.337Z
---

cananian (C. Scott Ananian, git email claude@cscott.net) is a Wikimedia
engineer who works across MediaWiki core, extensions, skins, and Parsoid
simultaneously — patches often span core + an extension, or Parsoid + core.
They treat **Gerrit as the repository of record for work-in-progress**, not
local disk: day-to-day pattern is `git review -d <task-number>` to pull a
change locally, and `git safe-reset` (a personal script — checks a clean
tree, runs `git-review-check` if `.gitreview` exists, then resets to
`origin/main`/`master`) to safely switch tasks without losing WIP.

Local directory conventions (their own layout — see
[[project_wmf_claude_sbx_redesign]]'s per-user config design for why this
must NOT be assumed for other engineers): `core` for `mediawiki/core`,
`Wikimedia/Extensions/<Name>` for `mediawiki/extensions/<Name>`,
`Wikimedia/Skins/<Name>` for `mediawiki/skins/<Name>`, `Wikimedia/Parsoid`
for `mediawiki/services/parsoid`. `~/Wikimedia` is a symlink to
`~/Projects/Wikimedia` — matters for any tool that bind-mounts paths into a
sandbox, since `sbx` does not resolve symlinks in mount sources and needs
`realpath`-resolved paths.

Cross-sandbox git pattern they currently use (motivating cleaner tooling):
create patches inside a sandbox → cherry-pick/reset them onto the host
working tree → clean up in an editor (including manually adding a missing
Gerrit Change-Id footer today) → `git review` from the host shell (SSH
push isn't done from inside the sandbox) → reset the sandbox to bring the
reviewed version back in for further agent turns.

**How to apply:** When designing WMF tooling with cananian, default to
Gerrit project paths as the canonical repo identifier, keep any
directory-layout assumption in a per-user config rather than hardcoded, and
remember their sandboxes are per-task and expected to stay isolated from
each other (so multi-repo work happens via multiple clones/mounts *within*
one task's sandbox, not by sharing a sandbox across tasks).

Offered standing help: cananian will run `wget -r -p -np -x -k <url>` on
the host to mirror docs.docker.com pages that WebFetch can't render — see
[[feedback_docker_docs_fetching]]. Ask again whenever `sbx` CLI behavior
needs re-verifying, not just when first blocked.
