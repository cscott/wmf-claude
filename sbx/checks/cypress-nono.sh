#!/usr/bin/env bash
# Cypress under nono: does Cypress
# (Electron, and the Xvfb that Cypress starts) run with a session's grants
# (session.CYPRESS_GRANTS)? This script runs INSIDE a sandbox, as the
# agent. The host only pipes it in:
#
#   wmf-sbx exec NAME -- bash -s xvfb    < sbx/checks/cypress-nono.sh
#   wmf-sbx exec NAME -- bash -s cypress < sbx/checks/cypress-nono.sh
#
# xvfb     start Xvfb and connect an X client to it, under nono with the
#          session's own flags (bin/claude, as `wmf-sbx resume` runs it)
#          plus CY_GRANTS. A `nono` shim on PATH keeps bin/claude's nono
#          arguments and replaces the command after `--` with the test,
#          so no Claude Code and no tokens are involved.
# cypress  the same, but the test is `cypress verify` (its "verified"
#          mark removed first, so that it runs under nono) and the repo's
#          `npm run selenium-test`, or one spec (CY_SPEC), in CY_REPO
#          (default: the current directory). Starts the wiki (outside
#          nono) first. Needs `mw-install-cypress` done in the repo.
#
# CY_GRANTS: extra nono flags, one per word, to try (default: none).
# WMF_SBX_CLAUDE_DIR: the wmf-claude tree with bin/claude and sbx/src
# (default /opt/wmf-claude, the image's).
#
# Every phase ends with `== RESULT ...` lines. Send all the output.
set -uo pipefail

WMF_SBX_CLAUDE_DIR=${WMF_SBX_CLAUDE_DIR:-/opt/wmf-claude}
# In the session's TMPDIR (launch-claude.sh), which nono lets it read.
WORK=${CY_WORK:-/tmp/claude-$(id -u)/cypress-nono}
DISPLAY_NUM=${CY_DISPLAY:-99}

result() { echo "== RESULT $*"; }

# The test, written to a file that nono runs. Only its arguments and the
# environment come in.
xvfb_test() {
  local work=$1 num=$2 log t0
  export TMPDIR="/tmp/claude-$(id -u)"; mkdir -p "$TMPDIR"
  log=$TMPDIR/xvfb.log
  echo "== under nono: $(id -un), TMPDIR=$TMPDIR"
  for d in /tmp/.X11-unix /var/lib/xkb "$HOME/.config/Cypress"; do
    printf -- '-- %s: ' "$d"
    { [ -d "$d" ] && ls -ld "$d" 2>&1; } || echo "missing"
  done
  # A new file each time: `touch` of an existing file succeeds without
  # write access (Landlock does not cover timestamps).
  local probe="$HOME/.config/Cypress/.probe.$$"
  mkdir -p "$HOME/.config/Cypress" 2>&1 && : > "$probe" 2>&1 && rm -f "$probe" \
    && echo "-- ~/.config/Cypress: writable" || echo "-- ~/.config/Cypress: NOT writable"
  t0=$SECONDS
  Xvfb ":$num" -screen 0 1280x1024x24 -nolisten tcp > "$log" 2>&1 &
  local pid=$!
  for _ in $(seq 1 50); do
    [ -S "/tmp/.X11-unix/X$num" ] && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "-- Xvfb exited; its log:"; sed 's/^/   | /' "$log"
    echo "== RESULT xvfb: Xvfb did not start"; return 1
  fi
  echo "-- Xvfb up in $((SECONDS - t0)) s; socket: $(ls -l "/tmp/.X11-unix/X$num" 2>&1)"
  local rc=0
  if xkbcomp -w0 ":$num" "$TMPDIR/keymap.xkb" > "$TMPDIR/xkbcomp.log" 2>&1; then
    echo "-- X client (xkbcomp) connected: $(wc -c < "$TMPDIR/keymap.xkb") bytes of keymap"
  else
    echo "-- X client FAILED:"; sed 's/^/   | /' "$TMPDIR/xkbcomp.log"; rc=1
  fi
  kill "$pid"; wait "$pid" 2>/dev/null
  echo "-- Xvfb log:"; sed 's/^/   | /' "$log"
  [ $rc = 0 ] && echo "== RESULT xvfb: ok" || echo "== RESULT xvfb: FAILED"
  return $rc
}

# The session's environment (MW_SERVER, ...) is in the agent's settings
# `env`; Claude Code would set it, and the shim runs no Claude Code.
session_env() {
  [ -r "$HOME/.claude/settings.json" ] || return 0
  eval "$(jq -r '.env // {} | to_entries[] | "export \(.key)=\(.value|@sh)"' "$HOME/.claude/settings.json")"
}

cypress_test() {
  local repo=$1 spec=$2
  session_env
  export TMPDIR="/tmp/claude-$(id -u)"; mkdir -p "$TMPDIR"
  cd "$repo" || { echo "== RESULT cypress: no repo $repo"; return 1; }
  # The repo's own cache folder (mw-install-cypress uses the same).
  local cache
  cache=$(node -e '
    const s = require("./package.json").scripts || {};
    for (const v of Object.values(s)) {
      const m = /CYPRESS_CACHE_FOLDER=(\S+)/.exec(v); if (m) { console.log(m[1]); break; } }')
  export CYPRESS_CACHE_FOLDER=${cache:-$HOME/.cache/Cypress}
  echo "== under nono: $(id -un), cache $CYPRESS_CACHE_FOLDER"
  rm -f "$CYPRESS_CACHE_FOLDER"/*/binary_state.json
  local t0=$SECONDS
  if DEBUG=cypress:server:util:process_profiler,cypress:cli* npx cypress verify > "$TMPDIR/verify.log" 2>&1; then
    echo "-- cypress verify: ok in $((SECONDS - t0)) s"
  else
    echo "-- cypress verify FAILED; tail:"; tail -40 "$TMPDIR/verify.log" | sed 's/^/   | /'
    echo "== RESULT cypress: verify FAILED"; return 1
  fi
  t0=$SECONDS
  local run=(npm run selenium-test)
  [ -n "$spec" ] && run=(npx cypress run --spec "$spec")
  echo "-- ${run[*]}"
  if "${run[@]}" > "$TMPDIR/run.log" 2>&1; then
    echo "-- cypress run: ok in $((SECONDS - t0)) s"
    grep -E "Tests:|Passing:|Failing:|Pending:|Skipped:|All specs passed|✔|✖" "$TMPDIR/run.log" | tail -25 | sed 's/^/   | /'
    echo "== RESULT cypress: ok"
  else
    echo "-- cypress run FAILED in $((SECONDS - t0)) s; tail:"
    grep -E "Tests:|Passing:|Failing:|✔|✖" "$TMPDIR/run.log" | tail -25 | sed 's/^/   | /'
    echo "   -- tail:"; tail -40 "$TMPDIR/run.log" | sed 's/^/   | /'
    echo "== RESULT cypress: run FAILED"; return 1
  fi
}

under_nono() {
  local label=$1; shift
  local grants=${CY_GRANTS:-} shim=$WORK/shim
  mkdir -p "$shim"
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    declare -f session_env xvfb_test cypress_test
    printf '%q ' "$@"; echo
  } > "$WORK/test.sh"
  cat > "$shim/nono" <<SHIM
#!/usr/bin/env bash
# Only \`nono run ... -- CMD\` is changed; anything else (--version, ...) passes.
[[ \${1:-} == run && " \$* " == *" -- "* ]] || exec /usr/bin/nono "\$@"
args=()
for a in "\$@"; do [[ \$a == -- ]] && break; args+=("\$a"); done
printf '%q ' /usr/bin/nono "\${args[@]}" > "$WORK/nono-argv.txt"
exec /usr/bin/nono "\${args[@]}" -- bash "$WORK/test.sh"
SHIM
  chmod +x "$shim/nono"
  # The session's flags, as `wmf-sbx resume` builds them (session.py).
  local flags
  flags=$(PYTHONPATH="$WMF_SBX_CLAUDE_DIR/sbx/src" python3 - "$PWD" <<'PY'
import shlex, sys
import wmf_sbx.session as s
argv = s.launcher_argv({"primaryDir": sys.argv[1], "repos": []}, proxy=s.upstream_proxy())
i = argv.index(s.LAUNCHER)
j = argv.index("--", i)
print(shlex.join(argv[i + 1:j]))
PY
) || { result "$label: could not compute the session's flags"; exit 1; }
  echo "-- session flags: $flags"
  echo "-- extra grants: $(echo $grants)"
  eval "set -- $flags"
  WMF_CLAUDE_NO_PAUSE=1 PATH="$shim:$PATH" \
    "$WMF_SBX_CLAUDE_DIR/bin/claude" "$@" $grants -- 2>&1 | tee "$WORK/$label.log"
  local rc=${PIPESTATUS[0]}
  grep -q "== RESULT $label: ok" "$WORK/$label.log" \
    && result "$label under nono: ok with the grants above" \
    || result "$label under nono: FAILED (rc $rc)"
}

[[ $(id -u) != 0 ]] || { echo "run as the agent, not root"; exit 1; }
mkdir -p "$WORK"
case "${1:-}" in
  xvfb) under_nono xvfb xvfb_test "$WORK" "$DISPLAY_NUM" ;;
  cypress)
    # The wiki, outside nono, as the agent would start it in a session.
    session_env
    serve=""
    if [ -n "${MW_INSTALL_PATH:-}" ]; then
      (cd "$MW_INSTALL_PATH" && exec composer serve) > "$WORK/serve.log" 2>&1 &
      serve=$!
      for _ in $(seq 60); do
        curl -sf --noproxy '*' -o /dev/null "$MW_SERVER/index.php?title=Special:Version" && break
        sleep 1
      done
      echo "-- wiki: $(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "$MW_SERVER/index.php?title=Special:Version")"
    fi
    under_nono cypress cypress_test "${CY_REPO:-$PWD}" "${CY_SPEC:-}"
    # composer serve's php -S is its child; stop both.
    [ -n "$serve" ] && { pkill -P "$serve" 2>/dev/null; kill "$serve" 2>/dev/null; } ;;
  *) sed -n '2,/^set -/{/^set -/d;s/^# \{0,1\}//;p;}' "${BASH_SOURCE[0]:-/dev/stdin}"; exit 2 ;;
esac
