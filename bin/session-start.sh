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
# WMF_CLAUDE_STE=0 drops the ASD-STE100 writing bullet (see below).
#
# WMF_CLAUDE_SANDBOX_BACKEND selects the directory. The default is `nono`.
# The nono files hold the text for a plain `bin/claude` session. For an unknown backend, the hook emits neither
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

# Where the browser runs, for a backend that has a browser block: on the
# host, Chrome started by the engineer (chrome.txt); in a Lima VM, a headless
# Chromium that bin/claude-vm started for the session (chrome-vm.txt;
# lima/guest-claude.sh sets WMF_CLAUDE_CHROME_IN_VM), kept to loopback.
chrome=chrome
[[ -n "${WMF_CLAUDE_CHROME_IN_VM:-}" ]] && chrome=chrome-vm
[[ -r "$CONTEXT_DIR/$BACKEND/$chrome.txt" ]] && emit_context "$chrome"

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

# Emitted only when the engineer gave the session GERRITUA (wmf-sbx's
# settings.yaml `environment:`, or the launching shell). The hook names the
# variable and never expands it: the value is a secret.
if [[ -n "${GERRITUA:-}" ]]; then
  cat <<'GERRITUA'
- `$GERRITUA` is set. Send it as the User-Agent on your own requests to gerrit.wikimedia.org (gitiles, the REST API), e.g. `curl -A "$GERRITUA" ...` or `urllib.request.Request(url, headers={"User-Agent": os.environ["GERRITUA"]})`: Gerrit gives that User-Agent a higher rate limit than the default one, which earns `HTTP 429` with a 60-second `retry-after` after a burst. Its value is a secret: refer to it only as `$GERRITUA` or `os.environ["GERRITUA"]`, never print, log, or echo it, never write it into a file, a commit, or a commit message, and never send it to another host.
GERRITUA
fi

# Emitted only when wmf-sbx runs this session behind its host egress proxy
# (WMF_CLAUDE_SBX_EGRESS is the sandbox's name). That proxy, on the engineer's
# machine, is then the allowlist for every host.
# With no nono in between (lima-sbx-sudo), curl sees the proxy's own 403.
if [[ -n "${WMF_CLAUDE_SBX_EGRESS:-}" ]]; then
  refusal='`CONNECT tunnel failed, response 502` (this session'"'"'s nono reports the egress proxy'"'"'s refusal as 502)'
  [[ "$BACKEND" == lima-sbx-sudo ]] && refusal='`CONNECT tunnel failed, response 403`'
  cat <<EGRESS
- All traffic out of this VM goes through an egress proxy on the engineer's machine, and only hosts on its allowlist get through. A host off the list fails: curl says $refusal. Do not work around it: ask the engineer to run \`wmf-sbx allow ${WMF_CLAUDE_SBX_EGRESS} HOST\` on their machine, which takes effect at once, with no restart; \`wmf-sbx logs ${WMF_CLAUDE_SBX_EGRESS}\` shows what the proxy denied.
EGRESS
fi

# Emitted only in a sandbox that wmf-sbx made to run VMs of its own
# (`create --nested`). The sandbox itself is not nested in anything: it is
# for work on sandboxing tools, wmf-sbx or another, which the agent tests
# here.
if [[ -n "${WMF_CLAUDE_SBX_NESTED:-}" ]]; then
  cat <<'NESTED'
- This sandbox was made to run VMs of its own (`wmf-sbx create --nested`): you have KVM, QEMU and Lima, for work on sandboxing tools (wmf-sbx or another). Test such a tool here yourself; do not ask the engineer to run it on their machine, unless the test needs the host. A tool that you are developing is not installed: run it from your checkout, by its path. Each Bash call is a new shell, so `export PATH` does not persist. A VM image build can take 10 minutes or more: run it in the background, with a log in `$TMPDIR`.
NESTED
fi

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

# In a Lima VM (lima/guest-claude.sh sets WMF_CLAUDE_IN_VM) nothing serves the
# wiki unless Docker runs it. nono lets the session bind a port it opened,
# so PHP's built-in server runs inside the sandbox and stops with the session.
if [[ -n "${WMF_CLAUDE_IN_VM:-}" ]]; then
  cat <<'VM'

- This session runs in the wmf-claude Lima VM. No web server runs unless Docker provides one or you start one. With `--local-web` (ports 80, 443 and 8080; `--local-web=PORT` for another) the sandbox can bind those ports as well as connect to them, so when a task needs HTTP, start the project's own dev server in the background inside this session; it stops with the session. For a MediaWiki checkout, from the workspace root: `PHP_CLI_SERVER_WORKERS=4 php -S 127.0.0.1:8080 -t . > "$TMPDIR/php-server.log" 2>&1 &`, then `curl -sI http://127.0.0.1:8080/index.php?title=Main_Page`; the installed wiki expects that address (`$wgServer`, empty `$wgScriptPath`). A refused connection on a port before you start a server is just that, not a sandbox denial. PHP, Composer, MariaDB (127.0.0.1:3306, `wikiuser`/`wikipass`, database `wikidb`) and a headless Chromium are native in this VM; a project that needs none of them can ignore them.
VM
  # The mode: lima/guest-claude.sh sets the proxy port only in a contained VM.
  if [[ -n "${WMF_CLAUDE_MODEL_PROXY_PORT:-}" ]]; then
    cat <<'CONTAINED'
- This is a contained VM. The only network path is the model API, through a proxy on the engineer's machine. The web access and the Phabricator, Gerrit and GitLab MCP servers described above do not apply here, and neither `git fetch` nor package installs work. That is by design, not a fault: do not try to work around it. When a task needs outside data, ask the engineer to push it in as files (`claude-vm push`).
CONTAINED
  else
    cat <<'ISOLATED'
- This is an isolated VM. The network allowlist is the same as on the engineer's machine, and the MCP servers work.
ISOLATED
  fi
fi
