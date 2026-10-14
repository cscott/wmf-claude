#!/bin/bash
# The network phase of a contained VM. Root only. lima/guest-install.sh installs
# it as /usr/local/sbin/wmf-claude-contained-net, with a systemd unit that runs
# `apply` at boot, before the network starts.
#
#   wmf-claude-contained-net lock     # no egress: the engagement phase
#   wmf-claude-contained-net open     # normal egress: install packages, images, updates
#   wmf-claude-contained-net apply    # load the recorded phase (boot)
#   wmf-claude-contained-net status   # print locked, open or broken
#
# Locked, the table below drops every new packet that leaves the VM, for
# every user and every container, except on loopback (the ssh -R forward of
# the host's model API proxy arrives there), on the Docker bridges (between
# the guest and its containers), DHCP, and replies to connections that came
# in (the host's SSH session). It does not depend on an interface name, so
# it works before the network is up. A missing or unknown phase is locked.
set -euo pipefail

# The override exists for tests/test-lima.sh only. sudo resets the
# environment, and the agent cannot use sudo.
DIR="${WMF_CLAUDE_GUEST_CONTAINED_DIR:-/etc/wmf-claude}"
PHASE_FILE="$DIR/net-phase"
MARKER="$DIR/contained"
TABLE=wmf_claude_contained_lock

die() { printf 'wmf-claude-contained-net: %s\n' "$1" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "run as root"
[[ -f "$MARKER" ]] || die "not a contained VM ($MARKER is missing)"

load_lock() {
  # Priority -10: before Docker's own chains. A drop in any base chain is
  # final, so an accept elsewhere (Docker's, the host block's) cannot undo it.
  nft -f - <<NFT
table inet $TABLE
delete table inet $TABLE
table inet $TABLE {
  chain output {
    type filter hook output priority -10; policy drop;
    oifname "lo" accept
    ct state established,related accept
    oifname "docker0" accept
    oifname "br-*" accept
    udp sport 68 udp dport 67 accept
    counter reject
  }
  chain forward {
    type filter hook forward priority -10; policy drop;
    ct state established,related accept
    oifname "docker0" accept
    oifname "br-*" accept
    counter drop
  }
}
NFT
}

unload_lock() {
  if nft list table inet "$TABLE" >/dev/null 2>&1; then
    nft delete table inet "$TABLE"
  fi
}

phase() {
  local p=""
  [[ -f "$PHASE_FILE" ]] && p=$(tr -d '[:space:]' < "$PHASE_FILE")
  [[ "$p" == open ]] && echo open || echo locked
}

set_phase() {
  install -d -m 0755 "$(dirname "$PHASE_FILE")"
  printf '%s\n' "$1" > "$PHASE_FILE.new"
  chmod 0644 "$PHASE_FILE.new"
  mv "$PHASE_FILE.new" "$PHASE_FILE"
}

case "${1:-}" in
  lock)
    load_lock
    set_phase locked
    # Connections opened before the lock stay up until they close. Flushing
    # conntrack would also cut the host's SSH sessions, this one included,
    # so lock when no session runs (bin/wmf-claude-vm does).
    echo locked ;;
  open)
    set_phase open
    unload_lock
    echo open ;;
  apply)
    if [[ "$(phase)" == open ]]; then unload_lock; echo open; else load_lock; echo locked; fi ;;
  status)
    loaded=false
    nft list table inet "$TABLE" >/dev/null 2>&1 && loaded=true
    case "$(phase),$loaded" in
      locked,true) echo locked ;;
      open,false)  echo open ;;
      *)           echo broken ;;
    esac ;;
  *) die "usage: wmf-claude-contained-net lock|open|apply|status" ;;
esac
