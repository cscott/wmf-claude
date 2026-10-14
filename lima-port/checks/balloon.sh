#!/usr/bin/env bash
# Free-page reporting (qemu_wrapper.BALLOON): does a sandbox's QEMU give
# memory back to the host when the guest frees it? Runs ON THE HOST, as
# the engineer, against a running QEMU sandbox that `wmf-sbx start` (or
# create, or resume) started with this checkout's code:
#
#   lima-port/checks/balloon.sh NAME
#
# It reads the host's resident memory (RSS) of the sandbox's QEMU, makes
# the guest touch and then free 3 GiB (a Python process, as the agent),
# and reads the RSS again, at once and after 30 s. With free-page
# reporting the RSS falls back by about 3 GiB within seconds; without, it
# stays up. Changes nothing but the guest's memory use for a moment.
set -uo pipefail

NAME=${1:?usage: balloon.sh NAME}
[[ $NAME =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { echo "not a sandbox name: $NAME"; exit 2; }
DIR="${LIMA_HOME:-$HOME/.lima}/wmf-sbx-$NAME"
PID=$(cat "$DIR/qemu.pid" 2>/dev/null) || { echo "no $DIR/qemu.pid: is $NAME running?"; exit 1; }
WMF_SBX=$(cd "$(dirname "$0")/../.." && pwd)/sbx/bin/wmf-sbx

rss() { echo $(( $(ps -o rss= -p "$PID") / 1024 )); }

echo "== $NAME: QEMU pid $PID"
args=$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || ps -o command= -p "$PID")
if grep -q 'free-page-reporting=on' <<<"$args"; then
  echo "-- QEMU has free-page reporting"
else
  echo "-- QEMU has NO free-page reporting: was $NAME started by this checkout's wmf-sbx?"
  echo "== RESULT balloon: not on"; exit 1
fi
echo "-- guest: $("$WMF_SBX" exec "$NAME" -- sh -c 'ls /sys/bus/virtio/drivers/virtio_balloon 2>/dev/null | grep -c virtio || true') balloon device(s) bound"
before=$(rss)
echo "-- RSS before: $before MiB"
"$WMF_SBX" exec "$NAME" -- python3 -c '
n = 3 << 30
b = bytearray(n)
b[::4096] = b"\x01" * (n // 4096)   # touch every page
print("guest: touched 3 GiB")
' 2>&1 | sed 's/^/   | /'
after=$(rss)
echo "-- RSS just after the guest freed it: $after MiB"
sleep 30
later=$(rss)
echo "-- RSS 30 s later: $later MiB"
if (( later < after - 1500 )); then
  echo "== RESULT balloon: ok (gave back $(( after - later )) MiB)"
else
  echo "== RESULT balloon: memory NOT given back ($before -> $after -> $later MiB)"
fi
