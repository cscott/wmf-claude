#!/usr/bin/env bash
# Phan with CI's phan dependencies and the `../../extensions` links
# (setup.link_phan_paths).
# This script runs INSIDE a sandbox, as the agent, in the repo under test
# (exec's default: the primary clone). The host only pipes it in:
#
#   wmf-sbx exec NAME -- bash -s links   < sbx/checks/phan-links.sh
#   wmf-sbx exec NAME -- bash -s outside < sbx/checks/phan-links.sh
#   wmf-sbx exec NAME -- bash -s nono "$(cat ~/.local/state/wmf-sbx/sandboxes/NAME.json)" \
#     < sbx/checks/phan-links.sh
#   wmf-sbx exec NAME -- bash -s control < sbx/checks/phan-links.sh
#
# links    the `../../extensions` and `../../skins` links beside the repo,
#          and each sibling that .phan/config.php names: there or missing.
# outside  `composer phan`, outside nono.
# nono     `composer phan` under nono with the session's own flags
#          (bin/claude, as `wmf-sbx resume` runs it, from the sandbox's
#          state, which the VM does not have: the second argument); a
#          `nono` shim on PATH replaces Claude Code with phan, so no
#          tokens are used.
# control  `composer phan` outside nono with the two links moved aside
#          (put back on exit): it should report the siblings' classes as
#          undeclared, which shows that the links are what phan uses.
#
# WMF_SBX_CLAUDE_DIR: the wmf-claude tree with bin/claude and sbx/src
# (default /opt/wmf-claude, the image's).
#
# Every phase ends with `== RESULT ...` lines. Send all the output.
set -uo pipefail

WMF_SBX_CLAUDE_DIR=${WMF_SBX_CLAUDE_DIR:-/opt/wmf-claude}
WORK=/tmp/claude-$(id -u)/phan-links
REPO=$PWD

result() { echo "== RESULT $*"; }

# The session's environment (MW_INSTALL_PATH, ...) is in the agent's
# settings `env`; Claude Code would set it, and the shim runs no Claude Code.
session_env() {
  [ -r "$HOME/.claude/settings.json" ] || return 0
  eval "$(jq -r '.env // {} | to_entries[] | "export \(.key)=\(.value|@sh)"' "$HOME/.claude/settings.json")"
}

# The names .phan/config.php expects as ../../extensions/X or ../../skins/X.
siblings() {
  grep -oE "\.\./\.\./(extensions|skins)/[A-Za-z0-9_.-]+" .phan/config.php 2>/dev/null \
    | sed 's|^\.\./\.\./||' | sort -u
}

phan_test() {
  local label=$1 repo=$2 out t0 rc
  session_env
  export TMPDIR="/tmp/claude-$(id -u)"; mkdir -p "$TMPDIR"
  cd "$repo" || { echo "== RESULT $label: no repo $repo"; return 1; }
  out=$TMPDIR/phan-$label.log
  echo "== $label: $(id -un) in $repo, MW_INSTALL_PATH=${MW_INSTALL_PATH:-unset}"
  t0=$SECONDS
  composer phan > "$out" 2>&1; rc=$?
  echo "-- composer phan: exit $rc in $((SECONDS - t0)) s; $(grep -cE '^[^ ].*:[0-9]+ Phan' "$out") findings"
  grep -E ':[0-9]+ Phan' "$out" | sed -E 's/^.*:[0-9]+ (Phan[A-Za-z]+).*/\1/' | sort | uniq -c \
    | sort -rn | sed 's/^/   | /'
  echo "   -- first findings:"; grep -E ':[0-9]+ Phan' "$out" | head -15 | sed 's/^/   | /'
  if [ $rc != 0 ]; then
    echo "   -- errors:"
    grep -m8 -iE "error|warning|exception|fatal|denied|not permitted" "$out" \
      | grep -v ' Phan[A-Za-z]* ' | sed 's/^/   | /'
  fi
  echo "   -- tail:"; tail -8 "$out" | sed 's/^/   | /'
  [ $rc = 0 ] && echo "== RESULT $label: clean" || echo "== RESULT $label: exit $rc"
  return $rc
}

links() {
  local d
  for d in ../../extensions ../../skins; do
    printf -- '-- %s: ' "$d"
    if [ -L "$d" ]; then echo "link -> $(readlink "$d")"
    elif [ -d "$d" ]; then echo "a real directory (left alone)"
    else echo "missing"; fi
  done
  local missing=0 n
  while read -r n; do
    [ -n "$n" ] || continue
    if [ -e "../../$n/extension.json" ] || [ -e "../../$n/skin.json" ] || [ -d "../../$n" ]; then
      echo "-- $n: there"
    else
      echo "-- $n: MISSING"; missing=$((missing + 1))
    fi
  done < <(siblings)
  [ -n "$(siblings)" ] || echo "-- .phan/config.php names no ../../ siblings"
  [ $missing = 0 ] && result "links: every sibling there" || result "links: $missing missing"
}

under_nono() {
  local state=$1 shim=$WORK/shim
  [ -n "$state" ] || { result "nono: give the sandbox's state JSON as the second argument"; exit 2; }
  mkdir -p "$shim"
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    declare -f session_env phan_test
    printf '%q ' phan_test nono "$REPO"; echo
  } > "$WORK/test.sh"
  cat > "$shim/nono" <<SHIM
#!/usr/bin/env bash
# Only \`nono run ... -- CMD\` is changed; anything else (--version, ...) passes.
[[ \${1:-} == run && " \$* " == *" -- "* ]] || exec /usr/bin/nono "\$@"
args=()
for a in "\$@"; do [[ \$a == -- ]] && break; args+=("\$a"); done
exec /usr/bin/nono "\${args[@]}" -- bash "$WORK/test.sh"
SHIM
  chmod +x "$shim/nono"
  # The session's flags, as `wmf-sbx resume` builds them (session.py).
  local flags
  flags=$(PYTHONPATH="$WMF_SBX_CLAUDE_DIR/sbx/src" python3 - "$state" <<'PY'
import json, shlex, sys
import wmf_sbx.session as s
argv = s.launcher_argv(json.loads(sys.argv[1]), proxy=s.upstream_proxy())
i = argv.index(s.LAUNCHER)
j = argv.index("--", i)
print(shlex.join(argv[i + 1:j]))
PY
) || { result "nono: could not compute the session's flags"; exit 1; }
  echo "-- session flags: $flags"
  eval "set -- $flags"
  WMF_CLAUDE_NO_PAUSE=1 PATH="$shim:$PATH" \
    "$WMF_SBX_CLAUDE_DIR/bin/claude" "$@" -- 2>&1 | tee "$WORK/nono.log"
  grep -q "== RESULT nono: clean" "$WORK/nono.log" \
    && result "phan under nono: clean" || result "phan under nono: NOT clean (see above)"
}

MOVED=()
control() {
  local d
  for d in ../../extensions ../../skins; do
    if [ -L "$d" ]; then mv "$d" "$d.phan-off" && MOVED+=("$(cd "$(dirname "$d")" && pwd)/${d##*/}"); fi
  done
  [ ${#MOVED[@]} -gt 0 ] || { result "control: no links to move aside"; return 1; }
  trap 'for d in "${MOVED[@]}"; do mv "$d.phan-off" "$d"; done' EXIT
  echo "-- moved aside: ${MOVED[*]}"
  phan_test control "$REPO" && result "control: clean WITHOUT the links (they did not matter)" \
    || result "control: not clean without the links (expected)"
}

[[ $(id -u) != 0 ]] || { echo "run as the agent, not root"; exit 1; }
mkdir -p "$WORK"
case "${1:-}" in
  links) links ;;
  outside) phan_test outside "$REPO" ;;
  nono) under_nono "${2:-}" ;;
  control) control ;;
  *) sed -n '2,/^set -/{/^set -/d;s/^# \{0,1\}//;p;}' "${BASH_SOURCE[0]:-/dev/stdin}"; exit 2 ;;
esac
