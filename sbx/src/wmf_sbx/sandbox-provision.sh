#!/bin/bash
# Root provisioning for a wmf-sbx sandbox. Lima runs it at every boot, so
# each step is idempotent and cheap when there is nothing to do.
# template.py puts it in the instance config, and fills in two values:
# ENGINEER (Lima's user, the template's `user.name`), PROXY_PORTS
# (host TCP ports of a loopback proxy, separated by spaces, or empty) and
# EPHEMERAL_PORTS (template.EPHEMERAL_PORTS, "LO HI").
#
# The security rules are Kosta's (lima/wmf-claude.yaml): two users, a
# host and LAN block, the agent limited to TCP 80 and 443, and no TIOCSTI.
# The golden image already has `agent`, with the host uid (D10); this
# script only checks it. HANDOFF-LIMA.md §5.3.
set -euo pipefail
ENGINEER="@ENGINEER@"
AGENT=agent
PROXY_PORTS="@PROXY_PORTS@"
EPHEMERAL_PORTS="@EPHEMERAL_PORTS@"

id -u "$AGENT" >/dev/null 2>&1 || { echo "wmf-sbx: no user $AGENT in the image" >&2; exit 1; }

# The agent has no sudo and no docker. The engineer can read the agent's
# tree (cp and the git helper read it); the agent cannot read the
# engineer's.
for g in sudo docker; do
  if getent group "$g" >/dev/null && id -nG "$AGENT" | grep -qw "$g"; then
    gpasswd -d "$AGENT" "$g" >/dev/null
  fi
done
usermod -aG "$(id -gn "$AGENT")" "$ENGINEER"
chmod 0750 "/home/$AGENT" "$(getent passwd "$ENGINEER" | cut -d: -f6)"

# Host and LAN block (Kosta's rule). Lima's user-mode network puts the
# host at the gateway and NATs the rest through the host, so the host's
# LAN, the LAN and any VPN are reachable. This refuses new connections to
# private and link-local addresses out of the default interface; DNS and
# DHCP to Lima's subnet pass. A loopback proxy on the host (the host's
# http_proxy names 127.0.0.1) is reachable on exactly its port; Lima
# gives the guest that proxy as 192.168.5.2. The agent gets TCP 80 and
# 443 only, plus that port.
DEV=$(ip -4 route show default | awk '{print $5; exit}')
NET=$(ip -4 -o route show dev "$DEV" scope link | awk '{print $1; exit}')
AGENT_UID=$(id -u "$AGENT")
PROXY_RULE=""
if [[ -n "$PROXY_PORTS" ]]; then
  PROXY_RULE="ip daddr 192.168.5.2 tcp dport { ${PROXY_PORTS// /, } } accept"
fi
if [[ -n "$DEV" && -n "$NET" ]]; then
  cat > /etc/nftables.conf <<NFT
#!/usr/sbin/nft -f
# Written by the wmf-sbx provisioning at boot. Do not edit.
table inet wmf_sbx_hostblock
delete table inet wmf_sbx_hostblock
table inet wmf_sbx_hostblock {
  set private4 {
    type ipv4_addr; flags interval
    elements = { 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16 }
  }
  set private6 {
    type ipv6_addr; flags interval
    elements = { fc00::/7, fe80::/10 }
  }
  chain block {
    ct state established,related accept
    ip daddr $NET udp dport { 53, 67 } accept
    ip daddr $NET tcp dport 53 accept
    $PROXY_RULE
    ip daddr @private4 counter reject
    meta l4proto { tcp, udp } ip6 daddr @private6 counter reject
  }
  chain agent_egress {
    ct state established,related accept
    tcp dport { 80, 443 } accept
    $PROXY_RULE
    counter reject
  }
  chain output {
    type filter hook output priority filter; policy accept;
    oifname "$DEV" jump block
    oifname "$DEV" meta skuid $AGENT_UID jump agent_egress
  }
  chain forward {
    type filter hook forward priority filter; policy accept;
    oifname "$DEV" jump block
  }
}
NFT
  nft -f /etc/nftables.conf
  systemctl enable nftables >/dev/null 2>&1 || true
else
  echo "wmf-sbx: no default route yet; host block not written this boot" >&2
fi

# A process on the engineer's terminal (sudo -u agent shares it) must not
# push input into the engineer's shell.
echo 'dev.tty.legacy_tiocsti = 0' > /etc/sysctl.d/90-wmf-sbx.conf
sysctl -q -w dev.tty.legacy_tiocsti=0 2>/dev/null || true

# The ephemeral ports: a window that a session grants whole, for the test
# tools that listen on a random port (template.EPHEMERAL_PORTS).
cat > /etc/sysctl.d/91-wmf-sbx-ports.conf <<SYSCTL
net.ipv4.ip_local_port_range = $EPHEMERAL_PORTS
net.ipv4.tcp_tw_reuse = 1
SYSCTL
sysctl -q -p /etc/sysctl.d/91-wmf-sbx-ports.conf

install -d -m 0755 /run/wmf-sbx
touch /run/wmf-sbx/provisioned
