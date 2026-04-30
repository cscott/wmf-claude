#!/bin/bash
# Launcher invoked inside the nono sandbox: prints the WMF Claude banner
# (after nono's capability table, immediately before Claude Code's banner)
# and execs claude with the remaining args.
#
# Suppress the banner with WMF_CLAUDE_QUIET=1.
if [[ -z "${WMF_CLAUDE_QUIET:-}" ]] && [[ -t 2 ]]; then
  printf '\n' >&2
  printf '  \033[1;36m╭─ WMF Claude ─────────────────────────────────────────╮\033[0m\n' >&2
  printf '  \033[1;36m│\033[0m  \033[1msandboxed by nono\033[0m · plugin: \033[1mwmf-claude\033[0m                \033[1;36m│\033[0m\n' >&2
  printf '  \033[1;36m│\033[0m  Use \033[1m/wmf-claude:init-project\033[0m in a repo to bootstrap   \033[1;36m│\033[0m\n' >&2
  printf '  \033[1;36m╰──────────────────────────────────────────────────────╯\033[0m\n' >&2
  printf '\n' >&2
fi
exec claude "$@"
