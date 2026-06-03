# shellcheck shell=bash
# Shared terminal-output helpers for wmf-claude install scripts.
# Sourced (not executed) by setup.sh, bin/wmf-claude-build, bin/wmf-claude-setup.
# No shebang, no `set -e`: the sourcing script owns shell options.

bold()  { printf '\033[1m%s\033[0m' "$*"; }
dim()   { printf '\033[2m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }

step() {
  printf '\n  %s %s\n' "$(bold ">")" "$(bold "$1")"
}

ok() {
  printf '    %s %s\n' "$(green "+")" "$1"
}

fail() {
  printf '    %s %s\n' "$(red "!")" "$1"
}
