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
  dim "    Install the missing dependencies, then re-run ./setup.sh:"; echo ""
  HINTS=""
  for cmd in "${MISSING[@]}"; do
    case "$cmd" in
      nono)     HINT="brew install nono   (or .deb / .rpm from https://github.com/always-further/nono/releases)" ;;
      claude)   HINT="curl -fsSL https://claude.ai/install.sh | bash" ;;
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

# Initialize submodules — handles colleagues who cloned without --recurse-submodules.
# Must run before the Python-version check below, which reads a file from the
# gerrit-mcp-server submodule.
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

# Claude Code's in-process sandbox (`sandbox.enabled: true`) uses Apple seatbelt
# to restrict its own tool calls. bin/claude already runs Claude inside a nono
# seatbelt sandbox, and nesting the two deadlocks tool calls — the inner
# sandbox-exec fights the outer profile. The signed nono pack pins this to
# false via wiring/settings-merge.json, but setup.sh doesn't apply that file,
# so detect the conflict and warn. We don't auto-fix: the user may legitimately
# want sandbox.enabled: true for unsandboxed claude sessions outside this repo.
# settings.local.json takes precedence over settings.json; either can hold the
# offending key. bin/claude has a matching runtime guard that hard-fails.
step "Checking Claude sandbox config"
# Read the raw value (true / false / null) from each file. We deliberately
# don't use jq's `//` operator here: `// "null"` would coerce an explicit
# `false` to "null", making an intentional override indistinguishable from a
# missing key, and breaking precedence (a settings.local.json saying false
# should override a settings.json saying true).
SANDBOX_MAIN="null"
SANDBOX_LOCAL="null"
if [[ -f "$HOME/.claude/settings.json" ]]; then
  SANDBOX_MAIN=$(jq -r '.sandbox.enabled' "$HOME/.claude/settings.json" 2>/dev/null || echo null)
fi
if [[ -f "$HOME/.claude/settings.local.json" ]]; then
  SANDBOX_LOCAL=$(jq -r '.sandbox.enabled' "$HOME/.claude/settings.local.json" 2>/dev/null || echo null)
fi
SANDBOX_SOURCE=""
if [[ "$SANDBOX_LOCAL" == "true" ]]; then
  SANDBOX_SOURCE="~/.claude/settings.local.json"
elif [[ "$SANDBOX_LOCAL" != "false" && "$SANDBOX_MAIN" == "true" ]]; then
  SANDBOX_SOURCE="~/.claude/settings.json"
fi
if [[ -n "$SANDBOX_SOURCE" ]]; then
  fail "sandbox.enabled is true in $SANDBOX_SOURCE"
  dim "    Claude Code's in-process sandbox can't nest inside the nono sandbox"; echo ""
  dim "    that bin/claude uses — tool calls will deadlock. Set sandbox.enabled"; echo ""
  dim "    to false in $SANDBOX_SOURCE (or run /config inside an"; echo ""
  dim "    unsandboxed claude session) before launching bin/claude."; echo ""
  dim "    Setup will continue; bin/claude will refuse to launch until this is fixed."; echo ""
else
  ok "sandbox.enabled is unset or false"
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

# Claude Code on Linux silently ignores glob patterns in Read/Edit/Write deny
# rules (e.g. Read(**/*.env)). The explicit non-glob entries in settings-merge.json
# cover common sensitive files (.env, .env.local, ~/.ssh/id_rsa, etc.), but
# deep-nested or unusually-named .key/.pem/.secret/.credential files within the
# workdir are NOT blocked by the Claude Code permission layer on Linux. The nono
# OS-level sandbox is the primary control for those.
if [[ "$(uname -s)" == "Linux" ]]; then
  step "Checking platform compatibility"
  fail "Glob patterns in Claude Code permission deny rules are ignored on Linux."
  dim "    The nono OS-level sandbox blocks ~/.ssh and other sensitive paths."; echo ""
  dim "    Explicit deny rules cover common .env files (.env, .env.local, etc.)."; echo ""
  dim "    However, deep-nested or unusually-named .key/.pem/.secret/.credential"; echo ""
  dim "    files inside your workdir are NOT blocked by the Claude Code permission"; echo ""
  dim "    layer — only by the nono sandbox. Avoid storing secrets in your workdir."; echo ""
fi

# Floor normally tracks the previous .nono-version pin, but here it matches the
# pin: the always-further/claude base pack hard-requires 0.61.0, so an older
# nono can't `nono pull` it. Keep in sync with package.json `min_nono_version`.
# Recommended version lives in .nono-version.
NONO_MIN="0.61.0"
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

# nono refuses to launch if ~/.nono/sessions is group/world accessible
# ("must not be group/world accessible; chmod 700 and retry") — session
# state can be sensitive. A umask of 002 (common on Linux) makes nono create
# it group-writable on first run, so pre-create it 700 here. chmod also
# repairs a dir an earlier run already created with loose perms.
step "Securing nono sessions directory"
mkdir -p ~/.nono/sessions
chmod 700 ~/.nono/sessions
ok "~/.nono/sessions is owner-only (700)"

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
  # `|| true`: grep exits 1 when no PHABRICATOR_USERNAME is present (e.g. a
  # server registered without it). Under `set -e`/`pipefail` that failed
  # pipeline in an assignment would kill the script silently, before the
  # username prompt below ever prints. A missing default is expected, not fatal.
  DEFAULT_PHAB_USER=$(printf '%s\n' "$EXISTING_MCP" \
    | grep -o 'PHABRICATOR_USERNAME=[^[:space:]"]*' \
    | head -n1 | cut -d= -f2- || true)
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
