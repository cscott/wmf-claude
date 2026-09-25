#!/bin/bash
# Claude Code status line for wmf-claude sessions. Claude Code runs it inside
# the sandbox with the session JSON on stdin and shows the first line of
# stdout. bin/claude exports the WMF_CLAUDE_* facts; wiring/settings-merge.json
# points statusLine here through $WMF_CLAUDE_HOME.
#
# Prints the sandbox segment, then whatever the engineer's own status line
# prints, so ours is added rather than replacing it. Their command comes from
# WMF_CLAUDE_STATUSLINE, which bin/claude read from ~/.claude/settings.json at
# launch. This script reads no settings file itself: inside the sandbox those
# files are agent-writable, and a command taken from one would run on the next
# tick with no prompt.

# A snapshot that leads back here must not recurse.
[[ -n "${WMF_CLAUDE_STATUSLINE_NESTED:-}" ]] && exit 0
export WMF_CLAUDE_STATUSLINE_NESTED=1

input=$(cat)

seg="WMF nono sandbox"
[[ -n "${WMF_CLAUDE_PROFILE:-}" && "$WMF_CLAUDE_PROFILE" != wmf-engineer ]] && seg+=" ($WMF_CLAUDE_PROFILE)"
[[ -n "${WMF_CLAUDE_SESSION:-}" ]] && seg+=" · $WMF_CLAUDE_SESSION"
[[ -n "${WMF_CLAUDE_UPDATE:-}" ]] && seg+=" · update available (${WMF_CLAUDE_UPDATE} behind main)"

theirs=""
cmd="${WMF_CLAUDE_STATUSLINE:-}"
if [[ -n "$cmd" && "$cmd" != *WMF_CLAUDE_HOME* ]] \
   && ! [[ -n "${WMF_CLAUDE_HOME:-}" && "$cmd" == *"$WMF_CLAUDE_HOME/bin/statusline.sh"* ]]; then
  # Output is kept when their command exits non-zero.
  theirs=$(printf '%s' "$input" | bash -c "$cmd" 2>/dev/null)
fi

first=${theirs%%$'\n'*}
rest=""
[[ "$theirs" == *$'\n'* ]] && rest=${theirs#*$'\n'}
printf '%s' "$seg"
[[ -n "$first" ]] && printf ' │ %s' "$first"
printf '\n'
[[ -n "$rest" ]] && printf '%s\n' "$rest"
exit 0
