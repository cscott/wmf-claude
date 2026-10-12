#!/usr/bin/env bash
# The checks that phase 7 left for real hardware (sbx/NOTES.md §112):
# core's QUnit, core's full wdio suite, and Cypress. On QEMU without KVM
# they did not complete, and Cypress could not download its binary.
#
# Run on a host with hardware virtualisation: Linux x86_64 with KVM, or
# an Intel Mac. Not an Apple Silicon Mac: Chrome for Testing has no Linux
# arm64 build, so mw-install-browser cannot work in an aarch64 VM.
#
#   lima-port/checks/real-hardware.sh create   # make the sandbox (~10-20 min)
#   lima-port/checks/real-hardware.sh all      # browser, qunit, wdio, cypress
#   lima-port/checks/real-hardware.sh qunit    # or one check at a time
#   lima-port/checks/real-hardware.sh rm       # remove the sandbox
#
# Each check runs through `wmf-sbx exec`, as the agent but outside nono,
# so a failure is about the VM and the tools, not the sandbox profile.
# Logs go to ./real-hardware-logs/. The sandbox holds Cite, Popups,
# TextExtracts and PageImages (Cite's Cypress specs need the other three),
# at upstream master, plus core and its dependencies.
set -uo pipefail

NAME=${WMF_SBX_CHECK_NAME:-hw-check}
LOGS=${WMF_SBX_CHECK_LOGS:-real-hardware-logs}
WMF_SBX=${WMF_SBX:-wmf-sbx}
# WMF_SBX_CHECK_IMAGE=KEY reuses a cached golden image (`wmf-sbx image ls`).
IMAGE_ARGS=(${WMF_SBX_CHECK_IMAGE:+--image "$WMF_SBX_CHECK_IMAGE"})
mkdir -p "$LOGS"

# The session's environment (MW_SERVER, CHROME_BIN, ...) is in the agent's
# settings `env` (D4); a check exports it, starts the wiki, runs CMD in
# DIR (expanded in the VM), and stops the wiki again.
in_vm() {  # DIR CMD
  "$WMF_SBX" exec "$NAME" -- bash -c '
    eval "$(jq -r ".env // {} | to_entries[] | \"export \(.key)=\(.value|@sh)\"" ~/.claude/settings.json)"
    export TMPDIR=$HOME/.cache/hw-check-tmp; mkdir -p "$TMPDIR"
    cd "$MW_INSTALL_PATH" || exit 1
    composer serve > "$TMPDIR/serve.log" 2>&1 & serve=$!
    trap "kill $serve 2>/dev/null" EXIT
    for i in $(seq 60); do
      curl -sf --noproxy "*" -o /dev/null "$MW_SERVER/index.php?title=Special:Version" && break
      sleep 1
    done
    dir=$1; shift
    eval "cd \"$dir\"" && time bash -c "$*"
  ' _ "$@"
}

check() {  # NAME DIR CMD
  local name=$1; shift
  echo "== $name: $*"
  in_vm "$@" > "$LOGS/$name.log" 2>&1
  local rc=$?
  echo "   rc=$rc; tail of $LOGS/$name.log:"
  grep -v -i -E 'dbus|oom score' "$LOGS/$name.log" | tail -8 | sed 's/^/   | /'
  return $rc
}

case "${1:-}" in
  create)
    "$WMF_SBX" create --reset-all ${IMAGE_ARGS[@]+"${IMAGE_ARGS[@]}"} --name "$NAME" \
      gerrit:mediawiki/extensions/Cite gerrit:mediawiki/extensions/Popups \
      gerrit:mediawiki/extensions/TextExtracts gerrit:mediawiki/extensions/PageImages
    ;;
  browser)
    check browser '$MW_INSTALL_PATH' 'mw-install-browser'
    ;;
  qunit)
    # Phase 7 (TCG): 0 tests, a ping timeout, or ~490 tests then
    # "Disconnected, because no message in 60000 ms". Want: a summary line.
    check qunit-core '$MW_INSTALL_PATH' 'npx grunt karma:chrome --qunit-component=MediaWiki'
    check qunit-cite '$MW_INSTALL_PATH' 'npx grunt karma:chrome --qunit-component=Cite'
    ;;
  wdio)
    # Phase 7 (TCG): page.js 6/6, but the full suite had 7 of 13 specs
    # failing before the session ended. Want: the per-spec results.
    check wdio-core '$MW_INSTALL_PATH' 'CI=true npm run selenium-test'
    check wdio-popups '$MW_INSTALL_PATH/extensions/Popups' 'CI=true npm run selenium-test'
    ;;
  cypress)
    # Phase 7: not run (the cloud proxy refused download.cypress.io).
    # Want: the install, then Cite's specs, with reference previews run.
    check cypress-install '$MW_INSTALL_PATH/extensions/Cite' 'mw-install-cypress .'
    check cypress-cite '$MW_INSTALL_PATH/extensions/Cite' 'npm run selenium-test'
    ;;
  all)
    "$0" browser && { "$0" qunit; "$0" wdio; "$0" cypress; }
    ;;
  rm)
    "$WMF_SBX" rm "$NAME"
    ;;
  *)
    sed -n '2,20p' "$0" >&2
    exit 2
    ;;
esac
