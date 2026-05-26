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
dim "    - Install a 'claude' alias (~/.zshrc, ~/.bashrc, or fish conf.d)"; echo ""
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
  echo ""
  dim "    Install the missing dependencies, then re-run ./setup.sh:"; echo ""
  HINTS=""
  for cmd in "${MISSING[@]}"; do
    case "$cmd" in
      nono)     HINT="brew install nono" ;;
      claude)   HINT="curl -fsSL https://claude.ai/install.sh | bash" ;;
      node|npm) HINT="brew install node" ;;
      python3)  HINT="brew install python3" ;;
      git)      HINT="brew install git   (or xcode-select --install)" ;;
      *)        HINT="install $cmd" ;;
    esac
    # Dedupe: node + npm map to the same hint.
    case $'\n'"$HINTS"$'\n' in
      *$'\n'"$HINT"$'\n'*) ;;
      *) HINTS+="$HINT"$'\n'; printf '      %s\n' "$HINT" ;;
    esac
  done
  echo ""
  exit 1
fi

# gerrit-mcp-server requires a recent Python (see requires-python in its
# pyproject.toml). An older python3 silently builds an incompatible venv and only
# fails deep in a pip resolve (e.g. "Could not find a version that satisfies
# click==8.3.1"), so check the version up front. uv fetches its own Python, so
# this only matters for the python3 -m venv fallback below.
if ! command -v uv &>/dev/null; then
  PY_REQ="$(sed -n 's/^requires-python *= *"[^0-9]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$SCRIPT_DIR/gerrit-mcp-server/pyproject.toml")"
  if [[ -z "$PY_REQ" ]]; then
    fail "Could not read requires-python from gerrit-mcp-server/pyproject.toml"
    exit 1
  fi
  PY_REQ_MAJOR="${PY_REQ%%.*}"
  PY_REQ_MINOR="${PY_REQ##*.}"
  PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  PY_MAJOR="${PY_VER%%.*}"
  PY_MINOR="${PY_VER##*.}"
  if (( PY_MAJOR < PY_REQ_MAJOR || (PY_MAJOR == PY_REQ_MAJOR && PY_MINOR < PY_REQ_MINOR) )); then
    echo ""
    fail "python3 is $PY_VER, but gerrit-mcp-server needs >= ${PY_REQ}."
    dim "    Install a newer Python (e.g. 'brew install python@${PY_REQ}')"; echo ""
    dim "    and put it first on PATH, or install 'uv' (which fetches its own Python)."; echo ""
    exit 1
  fi
fi

# Update Claude Code itself. The sandboxed `claude` (the alias installed below)
# runs with the wmf-engineer profile's "minimal" network, which deliberately
# does not allow Claude's version-check/download endpoints — so its in-sandbox
# auto-updater is disabled (see bin/launch-claude.sh) and reports nothing to
# `claude doctor`. setup.sh runs UNSANDBOXED, so it's the right place to keep
# the binary current: re-run ./setup.sh to update. Non-fatal — a failed or
# offline update must never block the rest of setup. Skip with
# WMF_CLAUDE_SKIP_UPDATE=1.
step "Updating Claude Code"
if [[ -n "${WMF_CLAUDE_SKIP_UPDATE:-}" ]]; then
  dim "    Skipped (WMF_CLAUDE_SKIP_UPDATE set)"; echo ""
elif claude update; then
  ok "Claude Code is up to date"
else
  fail "Update check failed (offline?) — continuing with the installed version"
fi

# nono 0.44 moved the claude-code profile to a registry pack — older versions
# can't pull packs and won't resolve the base profile our profile inherits from.
# The recommended/tested version lives in .nono-version (bumped via the
# /wmf-claude:check-nono-update skill).
NONO_MIN="0.44.0"
NONO_RECOMMENDED=$(tr -d '[:space:]' < "$SCRIPT_DIR/.nono-version" 2>/dev/null || echo "")
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
if [[ -n "$NONO_RECOMMENDED" && "$NONO_VER" != "$NONO_RECOMMENDED" ]]; then
  if [[ "$(printf '%s\n%s\n' "$NONO_RECOMMENDED" "$NONO_VER" | sort -V | head -n1)" != "$NONO_RECOMMENDED" ]]; then
    dim "    (running nono $NONO_VER; .nono-version recommends $NONO_RECOMMENDED — newer is usually fine)"; echo ""
  else
    dim "    (running nono $NONO_VER; .nono-version recommends $NONO_RECOMMENDED — consider upgrading)"; echo ""
  fi
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

# Recall the previous username from the existing registration, if any, so
# upgrades don't force the engineer to retype it. Greps the public `mcp get`
# output rather than parsing ~/.claude.json (whose schema isn't a contract).
DEFAULT_PHAB_USER=""
if EXISTING_MCP=$(claude mcp get phabricator 2>/dev/null); then
  DEFAULT_PHAB_USER=$(printf '%s\n' "$EXISTING_MCP" \
    | grep -o 'PHABRICATOR_USERNAME=[^[:space:]"]*' \
    | head -n1 | cut -d= -f2-)
fi

if [[ -n "$DEFAULT_PHAB_USER" ]]; then
  read -rp "    Phabricator username [$DEFAULT_PHAB_USER]: " PHAB_USER
  PHAB_USER="${PHAB_USER:-$DEFAULT_PHAB_USER}"
else
  read -rp "    Phabricator username: " PHAB_USER
fi
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

# Optional: chrome-devtools MCP for browser-based testing.
#
# Per-session opt-in by design. We install the MCP under chrome-devtools-mcp/
# (pinned via package-lock.json, reproducible with `npm ci --ignore-scripts`)
# and write an mcp-config.json with absolute paths. The MCP is NOT registered
# globally with `claude mcp add` — `bin/claude --chrome` passes the config
# via `--mcp-config` only when the engineer asks for it. That keeps the
# CDP-controlled-browser attack surface out of every other claude session.
echo ""
dim "    The chrome-devtools MCP drives Chrome for browser-based testing"; echo ""
dim "    (DOM inspection, screenshots, console errors, network traces)."; echo ""
echo ""
dim "    Chrome runs OUTSIDE the sandbox via bin/launch-test-chrome —"; echo ""
dim "    Chrome itself can't run inside (IOKit denied). The launcher creates"; echo ""
dim "    a fresh --user-data-dir per launch so your real Chrome profile is"; echo ""
dim "    untouched. The sandboxed MCP attaches over 127.0.0.1:9222."; echo ""
echo ""
dim "    Opt-in per session: launch with 'bin/claude --chrome' to enable;"; echo ""
dim "    plain 'bin/claude' sessions do not load the MCP."; echo ""
echo ""
# Remove only a user-scope registration that points at *this repo's* MCP
# binary — leftovers from earlier setup.sh versions when the per-session
# opt-in model didn't exist yet. We don't touch unrelated chrome-devtools
# registrations the engineer may have configured for non-WMF projects.
EXISTING_CDP="$(claude mcp get --scope user chrome-devtools 2>/dev/null || true)"
if [[ -n "$EXISTING_CDP" ]] && grep -qF "$SCRIPT_DIR/chrome-devtools-mcp" <<<"$EXISTING_CDP"; then
  claude mcp remove --scope user chrome-devtools >/dev/null 2>&1 || true
fi

# Sweep stale per-launch user-data dirs from earlier `bin/launch-test-chrome`
# runs that exited via SIGKILL (which bypasses the cleanup trap). Bounded to
# the dedicated mktemp prefix so we never touch unrelated tmp dirs.
find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'wmf-claude-chrome.*' -type d -mtime +1 -print 2>/dev/null \
  | while read -r d; do rm -rf "$d"; done

read -rp "    Install chrome-devtools MCP? [Y/n] " REG_CDP
case "$REG_CDP" in
  ""|y|Y|yes|Yes|YES)
    CDP_DIR="$SCRIPT_DIR/chrome-devtools-mcp"
    CDP_BIN="$CDP_DIR/node_modules/.bin/chrome-devtools-mcp"
    PINNED_VERSION="$(jq -r '.dependencies["chrome-devtools-mcp"]' "$CDP_DIR/package.json")"

    # Reproducible install from the tracked lockfile. --ignore-scripts blocks
    # any postinstall hook from running with the engineer's full privileges
    # (an npm-supply-chain mitigation; chrome-devtools-mcp itself does not
    # need install scripts to function).
    INSTALLED_VERSION=""
    if [[ -f "$CDP_DIR/node_modules/chrome-devtools-mcp/package.json" ]]; then
      INSTALLED_VERSION="$(jq -r .version "$CDP_DIR/node_modules/chrome-devtools-mcp/package.json")"
    fi
    if [[ "$INSTALLED_VERSION" == "$PINNED_VERSION" && -x "$CDP_BIN" ]]; then
      ok "chrome-devtools-mcp@$INSTALLED_VERSION (already installed)"
    else
      (cd "$CDP_DIR" && npm ci --ignore-scripts --silent)
      if [[ ! -x "$CDP_BIN" ]]; then
        fail "npm ci did not produce $CDP_BIN"
        exit 1
      fi
      ok "chrome-devtools-mcp@$PINNED_VERSION installed"
    fi

    # Supply-chain invariant: chrome-devtools-mcp ships as a self-contained
    # tarball that bundles its runtime deps (puppeteer-core, etc.). The
    # tracked package-lock.json reflects that — only one top-level entry.
    # If a future upstream version stops bundling, npm ci will populate
    # transitive deps that nono / Claude permission settings have never
    # reviewed. Catch that drift here rather than silently letting it ship.
    TOP_LEVEL_PKGS="$(find "$CDP_DIR/node_modules" -mindepth 1 -maxdepth 1 -type d \
                       -not -name '.*' | wc -l | tr -d ' ')"
    if [[ "$TOP_LEVEL_PKGS" != "1" ]]; then
      fail "expected 1 top-level package under node_modules/, got $TOP_LEVEL_PKGS"
      fail "  upstream may have stopped bundling deps — review the new tree before bumping the pin"
      exit 1
    fi
    ok "supply-chain invariant: 1 top-level package"

    # Write the MCP config pointing at the installed binary. bin/claude --chrome
    # passes this via --mcp-config so the MCP is only active for that session.
    # Flags chosen for the sandboxed/attach-mode setup:
    #   --browserUrl       attach to the Chrome started by bin/launch-test-chrome
    #   --no-usage-statistics  don't phone home to Google with usage data
    #   --no-performance-crux  don't send URLs to Google's CrUX field-data API
    #   --redactNetworkHeaders redact sensitive headers from MCP responses
    # jq does the JSON quoting so a SCRIPT_DIR with spaces/backslashes is safe.
    jq -n --arg cmd "$CDP_BIN" '{
      mcpServers: {
        "chrome-devtools": {
          command: $cmd,
          args: [
            "--browserUrl", "http://127.0.0.1:9222",
            "--no-usage-statistics",
            "--no-performance-crux",
            "--redactNetworkHeaders"
          ]
        }
      }
    }' > "$CDP_DIR/mcp-config.json"
    ok "wrote $CDP_DIR/mcp-config.json"
    dim "      Use 'bin/claude --chrome' to launch with chrome-devtools enabled."; echo ""
    dim "      Run 'bin/launch-test-chrome' in another terminal first."; echo ""
    ;;
  *)
    ok "chrome-devtools skipped"
    ;;
esac

# Install shell alias
step "Installing shell alias"
SHELL_NAME="$(basename "$SHELL")"
# Fish uses an `abbr` in its own conf.d file (expands inline so the sandbox
# path is visible). For zsh/bash we append a plain `alias` to the user's rc.
case "$SHELL_NAME" in
  zsh)  RC_PATH="$HOME/.zshrc"  ; RC_DISPLAY="~/.zshrc"  ; ALIAS_LINE="alias claude='$SCRIPT_DIR/bin/claude'" ;;
  bash) RC_PATH="$HOME/.bashrc" ; RC_DISPLAY="~/.bashrc" ; ALIAS_LINE="alias claude='$SCRIPT_DIR/bin/claude'" ;;
  fish) RC_PATH="$HOME/.config/fish/conf.d/wmf-claude.fish" ; RC_DISPLAY="~/.config/fish/conf.d/wmf-claude.fish" ; ALIAS_LINE="abbr -a claude $SCRIPT_DIR/bin/claude" ;;
  *)    RC_PATH=""              ; RC_DISPLAY="your shell config" ; ALIAS_LINE="alias claude='$SCRIPT_DIR/bin/claude'" ;;
esac

if [[ -z "$RC_PATH" ]]; then
  fail "Unsupported shell ($SHELL_NAME). Add this to $RC_DISPLAY:"
  echo "      $ALIAS_LINE"
elif [[ -f "$RC_PATH" ]] && grep -Fxq "$ALIAS_LINE" "$RC_PATH"; then
  ok "Alias already present in $RC_DISPLAY"
elif [[ -f "$RC_PATH" ]] && grep -Eq "^[[:space:]]*(alias[[:space:]]+claude=|abbr[[:space:]]+-a[[:space:]]+claude([[:space:]]|$))" "$RC_PATH"; then
  fail "A different 'claude' alias is already in $RC_DISPLAY — leaving it alone."
  fail "Replace it manually with:"
  echo "      $ALIAS_LINE"
else
  # mkdir -p handles fish's conf.d not existing yet; no-op for ~/.zshrc, ~/.bashrc.
  mkdir -p "$(dirname "$RC_PATH")"
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
