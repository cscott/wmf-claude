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

# Done
SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
  zsh)  RC_FILE="~/.zshrc" ;;
  bash) RC_FILE="~/.bashrc" ;;
  *)    RC_FILE="your shell config" ;;
esac

echo ""
echo "  $(green "Done.") Add this to $RC_FILE:"
echo ""
echo "    alias claude='$SCRIPT_DIR/bin/claude'"
echo ""
echo "  $(dim "This shadows the system 'claude' binary so 'claude' is sandboxed by default.")"
echo "  $(dim "Bypass with \\claude or 'command claude' when you need the unsandboxed binary.")"
echo ""
