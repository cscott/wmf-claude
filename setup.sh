#!/bin/bash
# Git-checkout installer: preflight dependency check (offering `brew install`
# for missing deps when Homebrew is present), then hand off to
# bin/wmf-claude-setup, which runs the build and per-user config.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=bin/lib-output.sh
source "$SCRIPT_DIR/bin/lib-output.sh"

echo ""
bold "  wmf-claude setup"; echo ""
dim "  Security sandbox for Claude Code at WMF"; echo ""
echo ""
dim "  This will:"; echo ""
dim "    - Pull the always-further/claude nono pack (Claude Code integration)"; echo ""
dim "    - Vendor MCP server dependencies (npm + pip/uv) in this checkout"; echo ""
dim "    - Register phabricator + gerrit MCP servers globally in Claude Code"; echo ""
dim "    - Install a 'claude' alias (~/.zshrc, ~/.bashrc, or fish conf.d)"; echo ""
echo ""
read -rp "  Press Enter to continue, Ctrl-C to abort: " _

# Check everything up front with install hints rather than failing halfway.
step "Checking dependencies"
MISSING=()
for cmd in nono node npm python3 claude git jq; do
  if command -v "$cmd" &>/dev/null; then
    ok "$cmd $(dim "($(command -v "$cmd"))")"
  else
    fail "$cmd not found"
    MISSING+=("$cmd")
  fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo ""
  fail "Missing: ${MISSING[*]}"
  echo ""

  # If Homebrew is here, offer to install the missing deps with it.
  if command -v brew &>/dev/null; then
    BREW_FORMULAE=(); BREW_CASKS=()
    for cmd in "${MISSING[@]}"; do
      case "$cmd" in
        nono)     BREW_FORMULAE+=(nono) ;;
        node|npm) BREW_FORMULAE+=(node) ;;
        python3)  BREW_FORMULAE+=(python3) ;;
        git)      BREW_FORMULAE+=(git) ;;
        jq)       BREW_FORMULAE+=(jq) ;;
        claude)   BREW_CASKS+=(claude-code) ;;
      esac
    done
    # node maps in twice if both node and npm are missing.
    if [[ ${#BREW_FORMULAE[@]} -gt 0 ]]; then
      BREW_FORMULAE=($(printf '%s\n' "${BREW_FORMULAE[@]}" | sort -u))
    fi
    dim "    Homebrew is available. I can install these for you:"; echo ""
    [[ ${#BREW_FORMULAE[@]} -gt 0 ]] && printf '      brew install %s\n' "${BREW_FORMULAE[*]}"
    [[ ${#BREW_CASKS[@]} -gt 0 ]]    && printf '      brew install --cask %s\n' "${BREW_CASKS[*]}"
    echo ""
    read -rp "    Run these now? [Y/n] " RUN_BREW || RUN_BREW="n"  # EOF (non-tty) -> decline
    case "$RUN_BREW" in
      ""|y|Y|yes|Yes|YES)
        [[ ${#BREW_FORMULAE[@]} -gt 0 ]] && brew install "${BREW_FORMULAE[@]}"
        [[ ${#BREW_CASKS[@]} -gt 0 ]]    && brew install --cask "${BREW_CASKS[@]}"
        hash -r  # let this shell see the newly installed binaries
        STILL_MISSING=()
        for cmd in "${MISSING[@]}"; do command -v "$cmd" &>/dev/null || STILL_MISSING+=("$cmd"); done
        # Don't expand an empty array under `set -u` (errors on bash 3.2).
        MISSING=()
        [[ ${#STILL_MISSING[@]} -gt 0 ]] && MISSING=("${STILL_MISSING[@]}")
        [[ ${#MISSING[@]} -eq 0 ]] && ok "All dependencies installed"
        ;;
    esac
  fi

  # Anything still missing (brew absent, declined, or a failed install): print
  # hints and stop.
  if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    dim "    Install the missing dependencies, then re-run ./setup.sh:"; echo ""
    HINTS=""
    for cmd in "${MISSING[@]}"; do
      case "$cmd" in
        nono)     HINT="brew install nono   (or .deb / .rpm from https://github.com/always-further/nono/releases)" ;;
        claude)   HINT="brew install --cask claude-code   (or: curl -fsSL https://claude.ai/install.sh | bash)" ;;
        node|npm) HINT="brew install node   (or: sudo apt install nodejs npm)" ;;
        python3)  HINT="brew install python3   (or: sudo apt install python3)" ;;
        git)      HINT="brew install git   (or: sudo apt install git, xcode-select --install)" ;;
        jq)       HINT="brew install jq   (or: sudo apt install jq)" ;;
        *)        HINT="install $cmd" ;;
      esac
      case $'\n'"$HINTS"$'\n' in
        *$'\n'"$HINT"$'\n'*) ;;
        *) HINTS+="$HINT"$'\n'; printf '      %s\n' "$HINT" ;;
      esac
    done
    echo ""
    exit 1
  fi
fi

# wmf-claude-setup runs the build, then the per-user config.
exec "$SCRIPT_DIR/bin/wmf-claude-setup"
