# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

> **Sandboxing**: Claude Code is launched via the `claude` alias from
> [wmf-claude](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude), which
> runs it inside a [nono](https://github.com/always-further/nono) sandbox.
> The sandbox is the security boundary; Claude Code's built-in `sandbox`
> setting is intentionally disabled in `.claude/settings.json` to avoid
> redundant restrictions on top of nono.

## Project overview

<!-- Describe what this repository contains and the key code paths. -->

## Available skills

These come from the [wmf-claude](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude) plugin, namespaced as `/wmf-claude:<name>`:

| Skill | Purpose |
|-------|---------|
| `/wmf-claude:write-commit-msg` | Draft a commit message for staged changes (Wikimedia format with `Bug:`/`Assisted-by:`/`Change-Id:`) |
| `/wmf-claude:write-phab-task [component] [description]` | Generate a Phabricator task in the WMF format |
| `/wmf-claude:review-patch [change-id]` | Fetch and review a Gerrit change |

## Available agents

| Agent | When it's used |
|-------|----------------|
| `gerrit-reviewer` | Reviews Gerrit patches for code quality, security, project conventions |
| `jupyter-notebook` | Crafts notebooks for WMF analytics infrastructure (stat1010, `wmfdata`) |

## Conventions

<!-- Document this project's specific conventions: code style, architecture
     patterns, where things live, what to avoid. -->

## Performance

Code in this repo runs against Wikimedia production scale. Read these before writing anything on a hot path.

**Performance rules** (quote these in reviews and findings):

- [Performance budgeting](https://www.mediawiki.org/wiki/Performance_budgeting) — canonical source for current numeric budgets
- [Backend performance practices](https://wikitech.wikimedia.org/wiki/MediaWiki_Engineering/Guides/Backend_performance_practices) — MediaWiki-focused but the principles (no sync HTTP in the request path, batch DB reads, cache by hit-rate model, defer to job queue) apply to any Wikimedia service
- [Frontend performance practices](https://wikitech.wikimedia.org/wiki/MediaWiki_Engineering/Guides/Frontend_performance_practices)

**Architectural context** (useful background, not a source of perf findings):

- [Architectural principles](https://www.mediawiki.org/wiki/Wikimedia_Engineering_Architecture_Principles)
- [Architecture guidelines](https://www.mediawiki.org/wiki/Architecture_guidelines)

For larger or cross-cutting features, request a performance review from a Principal engineer or other performance-expert colleague before merge.

## Commit messages

Follow Wikimedia format. Use `/wmf-claude:write-commit-msg` to draft one. Body lines wrap at 72 chars; trailers go in this order: `Assisted-by:` (Linux-kernel style, no email), `Bug: TXXXXX`, `Change-Id:` (added by the Gerrit commit-msg hook).

## Writing style

- **[ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English) for code comments and commit messages.** Short sentences, active voice, present tense, one instruction per sentence, approved vocabulary. This applies to comments and commit text only — not to chat replies, review comments, task descriptions, or other prose.
- **Plain language in all other prose.** Everyday words over jargon: "temporary" not "transient"; state the actual risk ("could re-send mail to people who already got it") rather than naming the concept ("mail() is not idempotent"). Keep real proper nouns (SMTP, ResourceLoader); cut jargon that has a plain equivalent and trivia the linked bug already records.
- **Chat replies: answer first, keep them short.** Add only the context that changes the engineer's next action — cut preamble, restated background, and options you will not pursue. This is softened plain language, not full ASD-STE100: keep nuance, disagreement, and calibrated uncertainty where they matter.

## Linting and tests

<!-- How to run tests and linters for this project. -->
