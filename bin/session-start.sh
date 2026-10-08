#!/bin/bash
# SessionStart hook for the wmf-claude plugin.
# stdout from this hook is added as context for Claude (per Claude Code docs),
# so use it to remind the model about the available skills, agents, and the
# sandbox boundary it's running inside.
#
# Some bullets are true only for one sandbox backend. A wrong SessionStart
# block is worse than a missing one: the model then believes false things
# about its own environment. Thus the backend bullets are not in this script.
# They are in hooks/context/<backend>/:
#
#   sandbox.txt      what enforces the boundary, and what to do when it stops you
#   environment.txt  which MCP servers exist, and how to test a local wiki
#
# WMF_CLAUDE_SANDBOX_BACKEND selects the directory. The default is `nono`.
# The nono files hold the text that this script had inline, so the default
# output does not change. For an unknown backend, the hook emits neither
# file and writes a warning to stderr. It does not fall back to text about a
# different sandbox.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT_DIR="$SCRIPT_DIR/../hooks/context"

BACKEND="${WMF_CLAUDE_SANDBOX_BACKEND:-nono}"

# Emit one backend context file. If the file is not there, emit nothing and
# write a warning to stderr. The engineer sees stderr; the model does not read
# it as context. A backend name is one path segment, so that it cannot select
# a file outside hooks/context/.
emit_context() {
  local name="$1" file="$CONTEXT_DIR/$BACKEND/$1.txt"
  if [[ "$BACKEND" =~ ^[a-z0-9][a-z0-9_-]*$ && -r "$file" ]]; then
    cat "$file"
  else
    echo "wmf-claude session-start: no $name context for backend '$BACKEND'" \
         "(expected $file); omitting that block" >&2
  fi
}

cat <<'CONTEXT'
You are running inside the wmf-claude environment for Wikimedia Foundation engineering work:

CONTEXT

emit_context sandbox

cat <<'CONTEXT'
- The wmf-claude Claude Code plugin is loaded. Available namespaced skills include /wmf-claude:write-commit-msg, /wmf-claude:stage-hunks, /wmf-claude:write-phab-task, /wmf-claude:review-patch, /wmf-claude:vuln-audit, /wmf-claude:standalone-vuln-audit, /wmf-claude:init-project, /wmf-claude:check-nono-update, /wmf-claude:trace-prod-route, and (for MediaWiki repos) /wmf-claude:run-tests, /wmf-claude:test-coverage, /wmf-claude:lint, /wmf-claude:manual-test, /wmf-claude:compare-rebase, /wmf-claude:perf-audit. Agents include gerrit-reviewer, jupyter-notebook, mediawiki-dev, mediawiki-explore, test-writer.
- Questions about wmf-claude go to #ai-coding in Wikimedia Slack (https://wikimedia.enterprise.slack.com/archives/C0ATKE72JG6); bugs and feature requests go to the #WMF-Claude workboard (https://phabricator.wikimedia.org/tag/wmf-claude/). If a wmf-claude bug blocks the engineer, point them at the workboard. When a repo has no CLAUDE.md, suggest /wmf-claude:init-project to bootstrap one.
CONTEXT

emit_context environment

cat <<'CONTEXT'
- `git add -p` and other interactive git commands cannot work as the Bash tool has no TTY on stdin, so their hunk loops read EOF and abort having staged nothing. To reorganise code into atomic commits, never use destructive commands like `git reset --hard` or `git clean`, and never re-type changes from memory to rebuild them as that discards reviewed, tested work. Stage arbitrary hunks non-destructively with a `git diff` → edit the patch → `git apply --cached` round-trip, to only touch the index and leave the working tree intact. See /wmf-claude:stage-hunks for the full recipe.
- Wikimedia commit-message convention: subject is "component: Subject"; trailers in order are Assisted-by:, Bug:, Change-Id: (added by the Gerrit commit-msg hook). The Assisted-by: trailer uses Linux-kernel short form — just the model name, e.g. "Assisted-by: Claude Opus 4.7". No email angle brackets, no "(1M context)" or other parentheticals. Do not use Co-Authored-By: in commit bodies for Wikimedia repos.
CONTEXT

# WMF_CLAUDE_STE=0 leaves out the STE writing rule, for an engineer who
# prefers plain prose in comments and commit messages.
if [[ "${WMF_CLAUDE_STE:-1}" != 0 ]]; then
  cat <<'CONTEXT'
- Write code comments and commit messages in ASD-STE100 Simplified Technical English: short sentences, active voice, present tense, one instruction per sentence, approved vocabulary. This applies to comments and commit text only — not to chat replies, review comments, task descriptions, or other prose.
CONTEXT
fi

cat <<'CONTEXT'
- After invoking a code-drafting subagent (mediawiki-dev, test-writer, jupyter-notebook), run a fresh-context code review on its diff before reporting work as done. This catches assumption-level problems (misread requirements, wrong API assumptions) that the drafter's own self-review misses because it shares the drafter's mental model.
  - Read `git diff` (and `git diff --cached`) and critique for: misread requirements, wrong API assumptions, suboptimal approach, security issues, missed edge cases, project-convention violations.
  - Apply fixes for clear issues by editing files directly. Do NOT re-invoke the drafting subagent just to apply review comments.
  - Surface judgment calls to the user rather than guessing.
  - Run exactly once per drafting cycle — do NOT re-review the fix diff.
  - If the review surfaces a fundamentally wrong approach, stop and escalate to the user rather than iterating on it.
CONTEXT

# Emitted only when this session is attached to a Docker dev container (the
# broker exports WMF_DOCKER_BROKER_URL into the sandbox). This is the reliable,
# always-present signal that routes PHP/composer/npm through the container — it
# applies to ad-hoc commands, not just the run-tests/lint skills, and on every
# machine regardless of whether the project's CLAUDE.md mentions it.
if [[ -n "${WMF_DOCKER_BROKER_URL:-}" ]]; then
  cat <<'DOCKER'

- This wiki runs inside Docker: PHP, composer, npm, and vendor/bin/* are NOT on the host — they live in a container reached through a broker. ALWAYS run those commands by prefixing them with `mwdocker`, whether or not you go through a skill. Examples: `mwdocker composer phpcs`, `mwdocker vendor/bin/phpunit <path>`, `mwdocker php maintenance/run.php update.php`, `mwdocker npm run lint`. Running them without the prefix hits the host, where they are absent and will fail. git, file reads/edits, grep, and other host-side work stay unprefixed as usual. Only the allowlisted binaries (composer, php, npm, vendor/bin/phpunit|phpcs|phpcbf|phan) are permitted through the broker.
DOCKER
fi
