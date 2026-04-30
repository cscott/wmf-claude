# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

> **Sandboxing**: Claude Code is launched via the `claude` alias from
> [wmf-claude](https://gitlab.wikimedia.org/kharlan/wmf-claude), which
> runs it inside a [nono](https://github.com/always-further/nono) sandbox.
> The sandbox is the security boundary; Claude Code's built-in `sandbox`
> setting is intentionally disabled in `.claude/settings.json` to avoid
> redundant restrictions on top of nono.

## Project overview

<!-- Describe what this repository contains and the key code paths. -->

## Available skills

These come from the [wmf-claude](https://gitlab.wikimedia.org/kharlan/wmf-claude) plugin, namespaced as `/wmf-claude:<name>`:

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

## Commit messages

Follow Wikimedia format. Use `/wmf-claude:write-commit-msg` to draft one. Body lines wrap at 72 chars; trailers go in this order: `Assisted-by:` (Linux-kernel style, no email), `Bug: TXXXXX`, `Change-Id:` (added by the Gerrit commit-msg hook).

## Linting and tests

<!-- How to run tests and linters for this project. -->
