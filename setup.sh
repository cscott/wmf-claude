#!/bin/bash
# Git-checkout installer: preflight dependency check (offering `brew install`
# for missing deps when Homebrew is present, and the pinned nono .deb on
# Debian-based Linux), then hand off to bin/wmf-claude-setup, which runs the
# build and per-user config. It also offers to upgrade a nono that is below
# the minimum version.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=bin/lib-output.sh
source "$SCRIPT_DIR/bin/lib-output.sh"

# The nono version that .nono-version pins. CI installs the same one.
NONO_VERSION=$(tr -d '[:space:]' < "$SCRIPT_DIR/.nono-version" 2>/dev/null || echo "")

# True when $version is older than $minimum.
older_than() {
  local minimum="$1" version="$2"
  [[ "$(printf '%s\n%s\n' "$minimum" "$version" | sort -V | head -n1)" != "$minimum" ]]
}

# True when the pinned nono .deb can be installed on this machine. dpkg names
# the architecture; apt-get does the install.
nono_deb_available() {
  [[ "$(uname -s)" == "Linux" && -n "$NONO_VERSION" ]] \
    && command -v dpkg &>/dev/null && command -v apt-get &>/dev/null
}

# Ask, then download and install the pinned nono .deb. $intro is the line that
# introduces the prompt. Returns non-zero if the engineer declines, or if the
# install fails.
offer_nono_deb() {
  local intro="$1"
  local deb url tmp reply sudo="" rc=0
  deb="nono-cli_${NONO_VERSION}_$(dpkg --print-architecture).deb"
  url="https://github.com/nolabs-ai/nono/releases/download/v${NONO_VERSION}/${deb}"
  dim "    $intro"; echo ""
  printf '      %s\n' "$url"
  dim "      (the install step uses sudo)"; echo ""
  echo ""
  read -rp "    Download and install it now? [Y/n] " reply || reply="n"  # EOF (non-tty) -> decline
  case "$reply" in
    ""|y|Y|yes|Yes|YES) ;;
    *) return 1 ;;
  esac
  tmp=$(mktemp -d)
  # apt fetches the file as the `_apt` user. That user must read the directory
  # and the file, or apt downloads as root and prints a warning.
  chmod 755 "$tmp"
  if command -v curl &>/dev/null; then
    curl -fsSL -o "$tmp/$deb" "$url" || rc=1
  elif command -v wget &>/dev/null; then
    wget -q -O "$tmp/$deb" "$url" || rc=1
  else
    fail "curl or wget is necessary to download nono"; rc=1
  fi
  if [[ $rc -eq 0 ]]; then
    chmod 644 "$tmp/$deb"
    [[ $(id -u) -eq 0 ]] || sudo="sudo"
    $sudo apt-get install -y "$tmp/$deb" || rc=1
  fi
  rm -rf "$tmp"
  hash -r  # let this shell see the new binary
  if [[ $rc -eq 0 ]] && command -v nono &>/dev/null; then
    ok "nono $(nono --version 2>/dev/null | awk '{print $2}') is installed"
    return 0
  fi
  fail "The nono install did not complete"
  return 1
}

# Ask, then upgrade nono with Homebrew. $intro starts the line that introduces
# the prompt; the command completes it. Returns non-zero if the engineer
# declines, or if the upgrade fails.
offer_nono_brew() {
  local intro="$1"
  local reply
  local -a cmd
  cmd=(upgrade nono)
  # `brew upgrade` works only on a formula that brew installed.
  brew list --formula nono &>/dev/null || cmd=(install nono)
  dim "    $intro I can run:"; echo ""
  printf '      brew %s\n' "${cmd[*]}"
  echo ""
  read -rp "    Run it now? [Y/n] " reply || reply="n"  # EOF (non-tty) -> decline
  case "$reply" in
    ""|y|Y|yes|Yes|YES) ;;
    *) return 1 ;;
  esac
  if brew "${cmd[@]}"; then
    hash -r  # let this shell see the new binary
    ok "nono $(nono --version 2>/dev/null | awk '{print $2}') is installed"
    return 0
  fi
  fail "The nono upgrade did not complete"
  return 1
}

echo ""
bold "  wmf-claude setup"; echo ""
dim "  Security sandbox for Claude Code at WMF"; echo ""
echo ""
dim "  This will:"; echo ""
dim "    - Pull the nolabs-ai/claude nono pack (Claude Code integration)"; echo ""
dim "    - Vendor MCP server dependencies (npm + pip/uv) in this checkout"; echo ""
dim "    - Register phabricator, gerrit, and gitlab MCP servers globally in Claude Code"; echo ""
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

  # nono ships a Debian package on GitHub releases. Offer to install it.
  if [[ " ${MISSING[*]} " == *" nono "* ]] && nono_deb_available; then
    if offer_nono_deb "nono has a Debian package. I can install version $NONO_VERSION for you:"; then
      REMAINING=()
      for cmd in "${MISSING[@]}"; do [[ "$cmd" == "nono" ]] || REMAINING+=("$cmd"); done
      # Don't expand an empty array under `set -u` (errors on bash 3.2).
      MISSING=()
      if [[ ${#REMAINING[@]} -gt 0 ]]; then MISSING=("${REMAINING[@]}"); fi
      if [[ ${#MISSING[@]} -eq 0 ]]; then ok "All dependencies installed"; fi
    fi
    echo ""
  fi

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
        nono)     HINT="brew install nono   (or .deb / .rpm from https://github.com/nolabs-ai/nono/releases)" ;;
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

# A nono below the minimum version stops bin/wmf-claude-setup, so offer the
# upgrade first. package.json holds the minimum version; bin/wmf-claude-setup
# enforces it.
NONO_MIN=$(jq -r '.min_nono_version // empty' "$SCRIPT_DIR/package.json" 2>/dev/null || echo "")
NONO_HAVE=$(nono --version 2>/dev/null | awk '{print $2}') || NONO_HAVE=""
if [[ -n "$NONO_MIN" && -n "$NONO_HAVE" ]] && older_than "$NONO_MIN" "$NONO_HAVE"; then
  echo ""
  fail "nono $NONO_HAVE is too old"
  echo ""
  NONO_NOTE="This needs nono $NONO_MIN or newer."
  if nono_deb_available && ! older_than "$NONO_MIN" "$NONO_VERSION"; then
    offer_nono_deb "$NONO_NOTE I can upgrade to $NONO_VERSION:" || true
  elif command -v brew &>/dev/null; then
    offer_nono_brew "$NONO_NOTE" || true
  fi
  echo ""
fi

# wmf-claude-setup runs the build, then the per-user config.
exec "$SCRIPT_DIR/bin/wmf-claude-setup"
