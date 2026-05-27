#!/bin/bash
# SessionStart hook for the wmf-claude plugin.
# stdout from this hook is added as context for Claude (per Claude Code docs),
# so use it to remind the model about the available skills, agents, and the
# nono sandbox boundary it's running inside.
cat <<'CONTEXT'
You are running inside the wmf-claude environment for Wikimedia Foundation engineering work:

- The session is sandboxed by nono (OS-level enforcement). Network, filesystem, and process access is restricted to the wmf-engineer profile. Do not suggest workarounds to escape the sandbox; if a path or domain is denied, surface that to the user.
- The wmf-claude Claude Code plugin is loaded. Available namespaced skills include /wmf-claude:write-commit-msg, /wmf-claude:write-phab-task, /wmf-claude:review-patch, /wmf-claude:vuln-audit, /wmf-claude:init-project, /wmf-claude:check-nono-update, /wmf-claude:trace-prod-route, and (for MediaWiki repos) /wmf-claude:run-tests, /wmf-claude:test-coverage, /wmf-claude:lint, /wmf-claude:manual-test, /wmf-claude:compare-rebase, /wmf-claude:perf-audit. Agents include gerrit-reviewer, jupyter-notebook, mediawiki-dev, mediawiki-explore, test-writer.
- Phabricator and Gerrit MCP servers are registered. Prefer mcp__phabricator__phabricator_get_task to verify Bug: references in commit messages, and mcp__gerrit__get_commit_message to preserve Change-Id when amending Gerrit-pushed commits.
- Verifying a change against the local wiki is tiered to save context. Prefer Tier 1 first: curl + `/w/api.php` for HTTP status, redirects, rendered HTML, and API responses. It loads no MCP and its output is discardable. Tier 1 needs the session launched with `bin/claude --local-web` (opens localhost web ports; a plain session can't reach the wiki at all). Escalate to the chrome-devtools MCP only for pixels, the accessibility tree, console errors, network traces, or real interaction. See /wmf-claude:manual-test for the full ladder.
- chrome-devtools MCP is opt-in per session. The points below only apply when the engineer launched with `bin/claude --chrome` (mcp__chrome-devtools__* tools present); plain `bin/claude` sessions can ignore them.
  - Prefer the chrome-devtools MCP tools over Bash(playwright)/Bash(agent-browser) when they're present.
  - The MCP attaches to a Chrome started outside the sandbox by `bin/launch-test-chrome` (Chrome can't run inside — IOKit denied). If the engineer hasn't started it, the MCP will fail to connect — tell them to run `bin/launch-test-chrome` in another terminal.
  - CDP on 127.0.0.1:9222 is unauthenticated. Any credentials submitted via fill_form / evaluate_script are exposed to other local processes — use throwaway dev-wiki accounts only.
  - The attached Chrome's network is NOT restricted by nono. Only navigate to URLs the engineer has explicitly authorized (their local wiki, Wikimedia domains) — never arbitrary external sites, and don't issue outbound fetch() calls via evaluate_script either.
- Wikimedia commit-message convention: subject is "component: Subject"; trailers in order are Assisted-by:, Bug:, Change-Id: (added by the Gerrit commit-msg hook). The Assisted-by: trailer uses Linux-kernel short form — just the model name, e.g. "Assisted-by: Claude Opus 4.7". No email angle brackets, no "(1M context)" or other parentheticals. Do not use Co-Authored-By: in commit bodies for Wikimedia repos.
- After invoking a code-drafting subagent (mediawiki-dev, test-writer, jupyter-notebook), run a fresh-context code review on its diff before reporting work as done. This catches assumption-level problems (misread requirements, wrong API assumptions) that the drafter's own self-review misses because it shares the drafter's mental model.
  - Read `git diff` (and `git diff --cached`) and critique for: misread requirements, wrong API assumptions, suboptimal approach, security issues, missed edge cases, project-convention violations.
  - Apply fixes for clear issues by editing files directly. Do NOT re-invoke the drafting subagent just to apply review comments.
  - Surface judgment calls to the user rather than guessing.
  - Run exactly once per drafting cycle — do NOT re-review the fix diff.
  - If the review surfaces a fundamentally wrong approach, stop and escalate to the user rather than iterating on it.
CONTEXT
