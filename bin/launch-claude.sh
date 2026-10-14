#!/bin/bash
# Launcher invoked inside the nono sandbox: sets the in-sandbox environment
# and execs claude with the remaining args.

# Disable Claude Code's auto-updater inside the sandbox. The version-check
# endpoint isn't in the wmf-engineer profile's allow_domain (network is
# allowlist-only), so the updater can only ever fail ("Failed to fetch versions"
# in `claude doctor`) — and we deliberately don't allow it: an in-sandbox
# updater that downloads and execs new binaries would undercut the sandbox.
# Updates happen out-of-band via ./setup.sh, which runs unsandboxed.
# Set here (inside the sandbox) rather than via the profile's allow_vars so
# it can't be overridden from the host environment.
export DISABLE_AUTOUPDATER=1

# The sandbox grants write but not read on bare /tmp; /tmp/claude-$UID is
# r+w. Route tempfiles there so read-after-write (heredocs, mktemp, ...)
# doesn't trip. TMPPREFIX is zsh-specific and not derived from TMPDIR.
TMPDIR="/tmp/claude-$(id -u)"
mkdir -p "$TMPDIR"
export TMPDIR
export TMPPREFIX="$TMPDIR/zsh"

# Keep the Phabricator MCP in scraper mode. A token selects Conduit mode, which
# sends each read as a POST to /api/<method>, and the profile refuses that.
# The MCP loads mcp-phabricator/.env with dotenv, and dotenv does not replace
# a variable that exists. An empty value thus disables a token in .env.
export PHABRICATOR_API_TOKEN=

# Contained mode (bin/claude, WMF_CLAUDE_MODEL_PROXY_PORT): the model API is reachable only
# through the host-side proxy, forwarded to this localhost port. The proxy
# replaces the OAuth token, so the sandbox holds a placeholder, never the
# real one. The connection to 127.0.0.1 must not go through nono's proxy.
# Non-essential traffic (telemetry, MCP registry) has no route anyway.
if [[ -n "${WMF_CLAUDE_MODEL_PROXY_PORT:-}" ]]; then
  export ANTHROPIC_BASE_URL="http://127.0.0.1:$WMF_CLAUDE_MODEL_PROXY_PORT"
  export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-wmf-claude-placeholder
  unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
  export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="$NO_PROXY"
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
fi

exec claude "$@"
