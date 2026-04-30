#!/bin/bash
# SessionStart hook for the wmf-claude plugin.
# stdout from this hook is added as context for Claude (per Claude Code docs),
# so use it to remind the model about the available skills, agents, and the
# nono sandbox boundary it's running inside.
cat <<'CONTEXT'
You are running inside the wmf-claude environment for Wikimedia Foundation engineering work:

- The session is sandboxed by nono (OS-level enforcement). Network, filesystem, and process access is restricted to the wmf-engineer profile. Do not suggest workarounds to escape the sandbox; if a path or domain is denied, surface that to the user.
- The wmf-claude Claude Code plugin is loaded. Available namespaced skills include /wmf-claude:write-commit-msg, /wmf-claude:write-phab-task, /wmf-claude:review-patch, /wmf-claude:init-project, and (for MediaWiki repos) /wmf-claude:run-tests, /wmf-claude:test-coverage, /wmf-claude:lint, /wmf-claude:manual-test, /wmf-claude:curl-test, /wmf-claude:compare-rebase. Agents include gerrit-reviewer, jupyter-notebook, mediawiki-dev, mediawiki-explore, test-writer.
- Phabricator and Gerrit MCP servers are registered. Prefer mcp__phabricator__phabricator_get_task to verify Bug: references in commit messages, and mcp__gerrit__get_commit_message to preserve Change-Id when amending Gerrit-pushed commits.
- Wikimedia commit-message convention: subject is "component: Subject"; trailers in order are Assisted-by:, Bug:, Change-Id: (added by the Gerrit commit-msg hook). Do not use Co-Authored-By: in commit bodies for Wikimedia repos.
CONTEXT
