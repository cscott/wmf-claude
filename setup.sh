#!/bin/bash
# Setup script for wmf-claude.
# Installs the nono profile, MCP server dependencies, and registers
# MCP servers globally in Claude Code.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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

echo ""
bold "  wmf-claude setup"; echo ""
dim "  Security sandbox for Claude Code at WMF"; echo ""
echo ""
dim "  This will:"; echo ""
dim "    - Pull the always-further/claude nono pack (Claude Code integration)"; echo ""
dim "    - Copy the nono profile to ~/.config/nono/profiles/"; echo ""
dim "    - Install MCP server dependencies (npm + pip/uv) in this checkout"; echo ""
dim "    - Register phabricator + gerrit MCP servers globally in Claude Code"; echo ""
dim "    - Append a 'claude' alias to ~/.zshrc or ~/.bashrc (skipped if present)"; echo ""
echo ""
read -rp "  Press Enter to continue, Ctrl-C to abort: " _

# Check dependencies
step "Checking dependencies"
MISSING=()
for cmd in nono node npm python3 claude git; do
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
  exit 1
fi

# nono 0.44 moved the claude-code profile to a registry pack — older versions
# can't pull packs and won't resolve the base profile our profile inherits from.
NONO_MIN="0.44.0"
NONO_VER=$(nono --version 2>/dev/null | awk '{print $2}')
if [[ -z "$NONO_VER" ]]; then
  fail "Could not determine nono version from 'nono --version'"
  exit 1
fi
if [[ "$(printf '%s\n%s\n' "$NONO_MIN" "$NONO_VER" | sort -V | head -n1)" != "$NONO_MIN" ]]; then
  fail "nono $NONO_VER is too old; this repo requires nono $NONO_MIN or newer."
  fail "Upgrade nono and re-run setup."
  exit 1
fi
ok "nono $NONO_VER (>= $NONO_MIN)"

# Initialize submodules — handles colleagues who cloned without --recurse-submodules
step "Initializing submodules"
if [[ -f "$SCRIPT_DIR/mcp-phabricator/package.json" && -f "$SCRIPT_DIR/gerrit-mcp-server/requirements.txt" ]]; then
  ok "Submodules present"
elif [[ -e "$SCRIPT_DIR/.git" ]]; then
  (cd "$SCRIPT_DIR" && git submodule update --init --recursive --quiet)
  ok "Submodules ready"
else
  fail "Submodules missing and this isn't a git checkout."
  fail "Re-clone with: git clone --recurse-submodules <url>"
  exit 1
fi

# Pull the claude nono pack (provides Claude Code integration: hooks, base policies)
step "Pulling always-further/claude nono pack"
if nono list --installed --silent --json 2>/dev/null | grep -q '"always-further/claude":'; then
  ok "Already installed"
else
  if nono pull always-further/claude --silent; then
    ok "Pulled always-further/claude"
  else
    fail "Failed to pull always-further/claude — check network access to the nono registry"
    exit 1
  fi
fi

# Install nono profile
step "Installing nono profile"
mkdir -p ~/.config/nono/profiles
cp "$SCRIPT_DIR/profiles/"*.json ~/.config/nono/profiles/
ok "Copied to ~/.config/nono/profiles/"

# Install mcp-phabricator dependencies
step "Setting up mcp-phabricator"
(cd "$SCRIPT_DIR/mcp-phabricator" && npm install --silent --ignore-scripts)
ok "Dependencies installed"

# Install gerrit-mcp-server dependencies
step "Setting up gerrit-mcp-server"
if command -v uv &>/dev/null; then
  (cd "$SCRIPT_DIR/gerrit-mcp-server" && uv venv -q --allow-existing && uv pip install -q -r requirements.txt)
else
  (cd "$SCRIPT_DIR/gerrit-mcp-server" && python3 -m venv .venv && .venv/bin/pip install -q --no-deps -r requirements.txt)
fi
ok "Dependencies installed"

GERRIT_CONFIG="$SCRIPT_DIR/gerrit-mcp-server/gerrit_mcp_server/gerrit_config.json"
if [[ ! -f "$GERRIT_CONFIG" ]]; then
  cat > "$GERRIT_CONFIG" <<'CONF'
{
  "default_gerrit_base_url": "https://gerrit.wikimedia.org/r/",
  "gerrit_hosts": [
    {
      "name": "Wikimedia",
      "external_url": "https://gerrit.wikimedia.org/r/",
      "authentication": {
        "type": "none"
      }
    },
    {
      "name": "Wikimedia (without /r/)",
      "external_url": "https://gerrit.wikimedia.org/",
      "authentication": {
        "type": "none"
      }
    }
  ]
}
CONF
  ok "Created gerrit_config.json for Wikimedia Gerrit"
else
  ok "gerrit_config.json already exists"
fi

# Register MCP servers
step "Registering MCP servers"
echo ""
dim "    The Phabricator MCP server reads phabricator.wikimedia.org"; echo ""
dim "    anonymously — no auth token, no API key, public data only."; echo ""
dim "    Your username is used as the default subscriber filter, so"; echo ""
dim "    queries like \"show my tasks\" return yours by default."; echo ""
echo ""
read -rp "    Phabricator username: " PHAB_USER
if [[ -z "$PHAB_USER" ]]; then
  fail "Phabricator username is required."
  exit 1
fi

claude mcp remove --scope user phabricator >/dev/null 2>&1 || true
claude mcp add --scope user phabricator \
  -e "PHABRICATOR_USERNAME=$PHAB_USER" \
  -- node "$SCRIPT_DIR/mcp-phabricator/src/index.js" >/dev/null 2>&1
ok "phabricator registered"

claude mcp remove --scope user gerrit >/dev/null 2>&1 || true
claude mcp add --scope user gerrit \
  -e "PYTHONPATH=$SCRIPT_DIR/gerrit-mcp-server/" \
  -- "$SCRIPT_DIR/gerrit-mcp-server/.venv/bin/python" \
  "$SCRIPT_DIR/gerrit-mcp-server/gerrit_mcp_server/main.py" stdio >/dev/null 2>&1
ok "gerrit registered"

# Install shell alias
step "Installing shell alias"
SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
  zsh)  RC_PATH="$HOME/.zshrc"  ; RC_DISPLAY="~/.zshrc"  ;;
  bash) RC_PATH="$HOME/.bashrc" ; RC_DISPLAY="~/.bashrc" ;;
  *)    RC_PATH=""              ; RC_DISPLAY="your shell config" ;;
esac

ALIAS_LINE="alias claude='$SCRIPT_DIR/bin/claude'"

if [[ -z "$RC_PATH" ]]; then
  fail "Unsupported shell ($SHELL_NAME). Add this to $RC_DISPLAY:"
  echo "      $ALIAS_LINE"
elif [[ -f "$RC_PATH" ]] && grep -Fxq "$ALIAS_LINE" "$RC_PATH"; then
  ok "Alias already present in $RC_DISPLAY"
elif [[ -f "$RC_PATH" ]] && grep -Eq "^[[:space:]]*alias[[:space:]]+claude=" "$RC_PATH"; then
  fail "A different 'alias claude=' is already in $RC_DISPLAY — leaving it alone."
  fail "Replace it manually with:"
  echo "      $ALIAS_LINE"
else
  printf '\n# wmf-claude: sandbox claude by default (bypass with \\claude or `command claude`)\n%s\n' "$ALIAS_LINE" >> "$RC_PATH"
  ok "Added 'claude' alias to $RC_DISPLAY"
fi

echo ""
echo "  $(green "Done.") Restart your shell or run: $(bold "source $RC_DISPLAY")"
echo ""
echo "  $(bold "Sandboxed claude")"
echo "  $(dim "  The 'claude' alias shadows the system binary so it's sandboxed by default.")"
echo "  $(dim "  Bypass with \\claude or 'command claude' when you need the unsandboxed binary.")"
echo ""
echo "  $(bold "Plugin (skills + agents)")"
echo "  $(dim "  The 'claude' alias auto-loads the wmf-claude plugin via --plugin-dir.")"
echo "  $(dim "  Inside Claude, run") $(bold "/wmf-claude:init-project") $(dim "in any git repo to drop")"
echo "  $(dim "  a starter CLAUDE.md (add") $(bold "--mediawiki") $(dim "for MediaWiki conventions).")"
echo ""
