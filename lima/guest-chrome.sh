#!/bin/bash
# Headless Chromium in the Lima guest, as the browser user (wmfchrome: no
# sudo, no docker, nothing of value in its home). lima/guest-claude.sh starts
# it for --chrome; the chrome-devtools MCP inside nono attaches over CDP on
# 127.0.0.1:9222; it stops with the session. The smoke test uses the same
# flags through dump-dom.
#
#   guest-chrome.sh cdp                      # serve CDP on 127.0.0.1:9222 until stopped
#   guest-chrome.sh dump-dom URL             # print the DOM of URL, then exit
#   guest-chrome.sh dump-dom-unproxied URL   # tests only: the same without the
#                                            # proxy and resolver flags, to show
#                                            # the firewall alone holds
#
# The browser reaches loopback only, in three layers. The firewall is the
# one that holds: a CDP client can open a browser context with its own proxy
# (Target.createBrowserContext), so flags alone would not.
#   - nftables table wmf_claude_chrome (lima/guest-dev-stack.sh) rejects every
#     packet the browser user sends out of a non-loopback interface
#   - --host-resolver-rules fails every name but localhost, browser-wide, so
#     no name leaves through the system resolver
#   - --proxy-server points the default context at a closed loopback port
# A fresh user-data-dir per run, removed on exit, as bin/launch-test-chrome.
set -euo pipefail

MODE="${1:-}"
URL="${2:-}"
PORT=9222
CHROME=/usr/bin/chromium

case "$MODE" in
  cdp) [[ -z "$URL" ]] || { echo "usage: guest-chrome.sh cdp" >&2; exit 2; } ;;
  dump-dom|dump-dom-unproxied) [[ -n "$URL" ]] || { echo "usage: guest-chrome.sh $MODE URL" >&2; exit 2; } ;;
  *) echo "usage: guest-chrome.sh cdp | dump-dom URL | dump-dom-unproxied URL" >&2; exit 2 ;;
esac
[[ -x "$CHROME" ]] || { echo "error: $CHROME is missing; run claude-vm update" >&2; exit 1; }

DATA_DIR=$(mktemp -d /tmp/wmf-claude-chrome.XXXXXX)
CHROME_PID=""
cleanup() {
  if [[ -n "$CHROME_PID" ]]; then kill "$CHROME_PID" 2>/dev/null || true; wait "$CHROME_PID" 2>/dev/null || true; fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT
trap 'exit 143' INT TERM

# The flags of bin/launch-test-chrome, plus headless and the loopback-only
# network. HOME is the data dir: nothing is written anywhere else.
FLAGS=(
  --headless=new
  --disable-gpu
  --disable-dev-shm-usage
  --user-data-dir="$DATA_DIR"
  --no-default-browser-check
  --no-first-run
  --password-store=basic
  --use-mock-keychain
  --no-pings
  --disable-background-networking
  --disable-sync
  --disable-default-apps
  --disable-component-update
)
# The resolver rule catches IP literals too, so loopback is excluded by
# name and by address. The bypass list names loopback for the proxy.
NET_FLAGS=(
  --proxy-server=127.0.0.1:9
  --proxy-bypass-list="127.0.0.1;localhost;[::1]"
  --host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1, EXCLUDE [::1]"
)
export HOME="$DATA_DIR"

case "$MODE" in
  cdp)
    # The address pin keeps the unauthenticated CDP socket on loopback.
    "$CHROME" "${FLAGS[@]}" "${NET_FLAGS[@]}" \
      --remote-debugging-port="$PORT" --remote-debugging-address=127.0.0.1 about:blank &
    CHROME_PID=$!
    wait "$CHROME_PID"
    ;;
  dump-dom)
    "$CHROME" "${FLAGS[@]}" "${NET_FLAGS[@]}" --dump-dom "$URL"
    ;;
  dump-dom-unproxied)
    "$CHROME" "${FLAGS[@]}" --dump-dom "$URL"
    ;;
esac
