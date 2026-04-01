#!/bin/bash
# Setup script for wmf-nono.
# Installs the nono profile, MCP server dependencies, and registers
# MCP servers globally in Claude Code.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== wmf-nono setup ==="
echo ""

# Check dependencies
for cmd in nono node npm python3 claude; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "Error: $cmd is required but not found."
    exit 1
  fi
done

# Install nono profile
echo "--- Installing nono profile ---"
mkdir -p ~/.config/nono/profiles
cp "$SCRIPT_DIR/profiles/"*.json ~/.config/nono/profiles/
echo "Installed profiles to ~/.config/nono/profiles/"

# Install mcp-phabricator dependencies
echo ""
echo "--- Setting up mcp-phabricator ---"
(cd "$SCRIPT_DIR/mcp-phabricator" && npm install --silent)
echo "mcp-phabricator ready"

# Install gerrit-mcp-server dependencies
echo ""
echo "--- Setting up gerrit-mcp-server ---"
if command -v uv &>/dev/null; then
  (cd "$SCRIPT_DIR/gerrit-mcp-server" && uv venv -q --allow-existing && uv pip install -q -r requirements.txt)
else
  (cd "$SCRIPT_DIR/gerrit-mcp-server" && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt)
fi
echo "gerrit-mcp-server ready"

# Register MCP servers globally in Claude Code
echo ""
echo "--- Registering MCP servers ---"
echo ""
read -rp "Phabricator username: " PHAB_USER
if [[ -z "$PHAB_USER" ]]; then
  echo "Error: Phabricator username is required."
  exit 1
fi

claude mcp remove --scope user phabricator 2>/dev/null || true
claude mcp add --scope user phabricator \
  -e "PHABRICATOR_USERNAME=$PHAB_USER" \
  -- node "$SCRIPT_DIR/mcp-phabricator/src/index.js"
echo "Registered phabricator MCP server"

claude mcp remove --scope user gerrit 2>/dev/null || true
claude mcp add --scope user gerrit \
  -e "PYTHONPATH=$SCRIPT_DIR/gerrit-mcp-server/" \
  -- "$SCRIPT_DIR/gerrit-mcp-server/.venv/bin/python" \
  "$SCRIPT_DIR/gerrit-mcp-server/gerrit_mcp_server/main.py" stdio
echo "Registered gerrit MCP server"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Run Claude Code with:"
echo "  $SCRIPT_DIR/bin/claude"
echo ""

# Detect shell and suggest alias
SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
  zsh)  RC_FILE="~/.zshrc" ;;
  bash) RC_FILE="~/.bashrc" ;;
  *)    RC_FILE="your shell config" ;;
esac

echo "To create a global alias, add this to $RC_FILE:"
echo ""
echo "  alias wmf-claude='$SCRIPT_DIR/bin/claude'"
