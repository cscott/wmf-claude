#!/bin/bash
# Launcher invoked inside the nono sandbox: prints the WMF Claude banner
# (after nono's capability table, immediately before Claude Code's banner)
# and execs claude with the remaining args.
#
# Suppress the banner with WMF_CLAUDE_QUIET=1.
if [[ -z "${WMF_CLAUDE_QUIET:-}" ]] && [[ -t 2 ]]; then
  slack_url='https://wikimedia.enterprise.slack.com/archives/C0ATKE72JG6'
  printf '\n' >&2
  printf '  \033[1;36m╭─\033[0m \033[1;31mWMF\033[0m \033[1mClaude\033[0m \033[1;36m──────────────────────────────────────────╮\033[0m\n' >&2
  printf '  \033[1;36m│\033[0m  Use \033[1;35m/wmf-claude:init-project\033[0m in a repo to bootstrap  \033[1;36m│\033[0m\n' >&2
  printf '  \033[1;36m│\033[0m  Questions? Ask in \033]8;;%s\033\\\033[1;35m#ai-coding\033[0m\033]8;;\033\\                         \033[1;36m│\033[0m\n' "$slack_url" >&2
  printf '  \033[1;36m╰───────────────────────────────────────────────────────╯\033[0m\n' >&2
  printf '\n' >&2
fi
exec claude "$@"
