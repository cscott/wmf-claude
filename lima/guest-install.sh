#!/bin/bash
# Install or refresh wmf-claude inside the Lima guest. bin/wmf-claude-vm
# uploads the checkout's HEAD (submodules included, no .git) and runs this
# script as the engineer from that staged copy. Rerun it to update; each step
# is idempotent.
#
#   1. nono: the .deb that .nono-version pins, checked against the release's
#      SHA256SUMS.txt before dpkg installs it.
#   2. The tree: /opt/wmf-claude.<rev>, owned by the engineer, built with
#      bin/wmf-claude-build (WMF_CLAUDE_SKIP_CHROME=1: Chrome runs on the
#      host, which the VM cannot reach). /opt/wmf-claude is a symlink to it,
#      swapped after the build. The agent reads the tree and writes one file
#      in it: gerrit-mcp-server/server.log.
#   3. PHP, Composer and MariaDB (lima/guest-dev-stack.sh, as root), so
#      PHPUnit and the maintenance scripts run in the VM as on the host.
#   4. The agent: Claude Code from the official installer, then
#      bin/wmf-claude-setup with WMF_CLAUDE_SKIP_BUILD=1. That pulls the nono
#      base pack, asks for the Phabricator username and registers the MCP
#      servers, all as `agent`.
#
#   guest-install.sh STAGED_TREE
set -euo pipefail

STAGE="${1:?usage: guest-install.sh STAGED_TREE}"
# shellcheck source=../bin/lib-output.sh
source "$STAGE/bin/lib-output.sh"

AGENT=agent
INSTALL=/opt/wmf-claude
umask 022

die() { fail "$1"; exit 1; }

[[ -f "$STAGE/.nono-version" && -f "$STAGE/.wmf-claude-rev" ]] \
  || die "$STAGE is not a staged wmf-claude tree"
[[ "$(id -un)" != root && "$(id -un)" != "$AGENT" ]] \
  || die "run this as the engineer, not as root or $AGENT"
sudo -n true 2>/dev/null || die "passwordless sudo is required"
id -u "$AGENT" >/dev/null 2>&1 \
  || die "user $AGENT does not exist; the VM provisioning did not finish"

# 1. nono, pinned. The .deb and SHA256SUMS.txt come from the same GitHub
# release, so the check catches a corrupt or replaced asset, not a
# compromised release.
step "nono"
NONO_WANT=$(tr -d '[:space:]' < "$STAGE/.nono-version")
NONO_HAVE=$(nono --version 2>/dev/null | awk '{print $2}' || true)
if [[ "$NONO_HAVE" == "$NONO_WANT" ]]; then
  ok "nono $NONO_HAVE (pinned by .nono-version)"
else
  ARCH=$(dpkg --print-architecture)
  DEB="nono-cli_${NONO_WANT}_${ARCH}.deb"
  BASE="https://github.com/nolabs-ai/nono/releases/download/v${NONO_WANT}"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL --retry 3 -o "$TMP/$DEB" "$BASE/$DEB"
  curl -fsSL --retry 3 -o "$TMP/SHA256SUMS.txt" "$BASE/SHA256SUMS.txt"
  if [[ "$(grep -cE "[[:space:]]${DEB}\$" "$TMP/SHA256SUMS.txt")" != 1 ]]; then
    die "SHA256SUMS.txt of nono v$NONO_WANT has no entry for $DEB"
  fi
  (cd "$TMP" && grep -E "[[:space:]]${DEB}\$" SHA256SUMS.txt | sha256sum -c --strict --quiet -) \
    || die "checksum mismatch for $DEB"
  sudo dpkg -i "$TMP/$DEB" >/dev/null
  rm -rf "$TMP"
  trap - EXIT
  ok "nono $NONO_WANT installed ($DEB, checksum verified)"
fi

# 2. The tree. A new directory per revision, then an atomic symlink swap, so
# a failed build never replaces a working install.
step "Installing wmf-claude"
REV=$(tr -d '[:space:]' < "$STAGE/.wmf-claude-rev")
NEW="$INSTALL.$REV"
CURRENT=$(readlink -f "$INSTALL" 2>/dev/null || true)
if [[ "$CURRENT" == "$NEW" && -x "$NEW/bin/claude" ]]; then
  ok "already at $REV"
else
  sudo rm -rf "$NEW"
  sudo install -d -m 0755 -o "$(id -un)" -g "$(id -gn)" "$NEW"
  cp -a "$STAGE"/. "$NEW"/
  # World-readable, owner-writable. The agent reads the tree through nono's
  # --read grant and must never write it.
  chmod -R u+rwX,go+rX,go-w "$NEW"
  ok "copied HEAD $REV to $NEW"
fi
WMF_CLAUDE_SKIP_CHROME=1 "$NEW/bin/wmf-claude-build"
# The Gerrit MCP appends to this file from inside the sandbox (bin/claude
# grants it with --allow-file), so the agent owns it.
# A rerun at the same revision finds it owned by the agent already.
LOGF="$NEW/gerrit-mcp-server/server.log"
[[ -e "$LOGF" ]] || : > "$LOGF"
[[ "$(stat -c %U "$LOGF")" == "$AGENT" ]] || sudo chown "$AGENT:$AGENT" "$LOGF"
if [[ "$CURRENT" != "$NEW" ]]; then
  sudo ln -sfn "$NEW" "$INSTALL.new"
  sudo mv -T "$INSTALL.new" "$INSTALL"
  for old in "$INSTALL".*; do
    if [[ -d "$old" && ! -L "$old" && "$old" != "$NEW" ]]; then
      sudo rm -rf "$old"
    fi
  done
  ok "$INSTALL -> $NEW"
fi

# Debian's nftables.service flushes the whole ruleset when it stops, Docker's
# chains included, and Docker recreates them only when it restarts. The
# rules files here replace their own tables, so a stop needs no flush: keep
# the other tables. Then repair Docker if an earlier flush removed its chains.
step "Firewall: keep Docker's rules across nftables restarts"
sudo install -d -m 0755 /etc/systemd/system/nftables.service.d
printf '[Service]\nExecStop=\n' | sudo tee /etc/systemd/system/nftables.service.d/wmf-claude.conf >/dev/null
sudo systemctl daemon-reload
if systemctl is-active --quiet docker && ! sudo iptables -t filter -nL DOCKER-FORWARD >/dev/null 2>&1; then
  sudo systemctl restart docker
  ok "Docker restarted: its firewall chains were missing"
else
  ok "nftables.service no longer flushes the ruleset on stop"
fi

# PHP, Composer and MariaDB, for PHPUnit without Docker. Root runs the
# script from the new tree, which the agent cannot write.
step "PHP, Composer and MariaDB"
dev_stack=$(sudo bash "$NEW/lima/guest-dev-stack.sh" | tail -n1)
ok "$dev_stack"

# Contained VM: the network phase script, root-owned, and the unit that loads the
# recorded phase at boot, before the network starts. A copy outside
# /opt/wmf-claude, which the engineer owns: root runs it.
if [[ -f /etc/wmf-claude/contained ]]; then
  step "Contained VM network phase"
  sudo install -m 0755 -o root -g root "$NEW/lima/guest-contained-net.sh" /usr/local/sbin/wmf-claude-contained-net
  sudo tee /etc/systemd/system/wmf-claude-contained-net.service >/dev/null <<'UNIT'
[Unit]
Description=wmf-claude contained VM network phase (no egress when locked)
DefaultDependencies=no
After=local-fs.target nftables.service
# Debian's nftables.service flushes the whole ruleset when it stops. PartOf
# restarts this unit with it, which loads the lock again.
PartOf=nftables.service
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/wmf-claude-contained-net apply

[Install]
WantedBy=sysinit.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable wmf-claude-contained-net.service >/dev/null 2>&1
  ok "wmf-claude-contained-net installed; phase: $(sudo /usr/local/sbin/wmf-claude-contained-net status)"
fi

# 4. The agent. Claude Code's official installer puts the binary in
# ~/.local/bin. It is not pinned or checksummed; accepted for now, and noted
# in docs/lima-vm.md.
step "Claude Code for $AGENT"
# As the agent, with a fixed PATH and no login shell: the agent's dotfiles
# never run here. ~/.local/bin is last, as in lima/guest-claude.sh.
AGENT_HOME=$(getent passwd "$AGENT" | cut -d: -f6)
as_agent() {
  sudo -H -u "$AGENT" env -C "$AGENT_HOME" \
    PATH="/usr/local/bin:/usr/bin:/bin:$AGENT_HOME/.local/bin" "$@"
}
if [[ -x "$AGENT_HOME/.local/bin/claude" ]]; then
  ok "installed ($(as_agent claude --version 2>/dev/null || echo 'version unknown'))"
else
  as_agent bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
  ok "installed"
fi

# WMF_CLAUDE_SKIP_BUILD: the tree belongs to the engineer, and step 2 built
# it. Interactive: it asks for the Phabricator username, as ./setup.sh does
# on the host, and recalls the previous answer.
step "Per-user setup for $AGENT"
# A contained VM (root-owned marker, written by bin/wmf-claude-vm create --contained)
# gets no MCP servers: it has no egress for them.
CONTAINED_ENV=()
[[ -f /etc/wmf-claude/contained ]] && CONTAINED_ENV=(WMF_CLAUDE_CONTAINED=1)
as_agent WMF_CLAUDE_SKIP_BUILD=1 WMF_CLAUDE_IN_VM=1 "${CONTAINED_ENV[@]}" "$INSTALL/bin/wmf-claude-setup"
as_agent mkdir -p "$AGENT_HOME/work"

echo ""
echo "  $(green "Done.") wmf-claude $REV is installed in the VM."
echo ""
