#!/bin/bash
# SessionStart hook for the wmf-claude plugin.
# stdout from this hook is added as context for Claude (per Claude Code docs),
# so use it to remind the model about the available skills, agents, and the
# nono sandbox boundary it's running inside.
cat <<'CONTEXT'
You are running inside the wmf-claude environment for Wikimedia Foundation engineering work:

- The session is sandboxed by nono (OS-level enforcement). Network, filesystem, and process access is restricted to the wmf-engineer profile. Do not suggest workarounds to escape the sandbox; if a path or domain is denied, surface that to the user.
- The wmf-claude Claude Code plugin is loaded. Available namespaced skills include /wmf-claude:write-commit-msg, /wmf-claude:write-phab-task, /wmf-claude:review-patch, /wmf-claude:init-project, and (for MediaWiki repos) /wmf-claude:run-tests, /wmf-claude:test-coverage, /wmf-claude:lint, /wmf-claude:manual-test, /wmf-claude:compare-rebase. Agents include gerrit-reviewer, jupyter-notebook, mediawiki-dev, mediawiki-explore, test-writer.
- Phabricator and Gerrit MCP servers are registered. Prefer mcp__phabricator__phabricator_get_task to verify Bug: references in commit messages, and mcp__gerrit__get_commit_message to preserve Change-Id when amending Gerrit-pushed commits.
- chrome-devtools MCP is opt-in per session. The points below only apply when the engineer launched with `bin/claude --chrome` (mcp__chrome-devtools__* tools present); plain `bin/claude` sessions can ignore them.
  - Prefer the chrome-devtools MCP tools over Bash(playwright)/Bash(agent-browser) when they're present.
  - The MCP attaches to a Chrome started outside the sandbox by `bin/launch-test-chrome` (Chrome can't run inside — IOKit denied). If the engineer hasn't started it, the MCP will fail to connect — tell them to run `bin/launch-test-chrome` in another terminal.
  - CDP on 127.0.0.1:9222 is unauthenticated. Any credentials submitted via fill_form / evaluate_script are exposed to other local processes — use throwaway dev-wiki accounts only.
  - The attached Chrome's network is NOT restricted by nono. Only navigate to URLs the engineer has explicitly authorized (their local wiki, Wikimedia domains) — never arbitrary external sites, and don't issue outbound fetch() calls via evaluate_script either.
- Wikimedia commit-message convention: subject is "component: Subject"; trailers in order are Assisted-by:, Bug:, Change-Id: (added by the Gerrit commit-msg hook). Do not use Co-Authored-By: in commit bodies for Wikimedia repos.
CONTEXT
