#!/bin/bash
# Build the wmf-sbx golden image, inside the builder VM, as root.
# wmf_sbx/image.py copies this script and a staging directory into the
# builder, and runs it with `sudo env VAR=... bash image-build.sh`. Read
# lima-port/HANDOFF-LIMA.md §5 (D1: the image comes from a builder VM,
# never from a sandbox that ran an agent).
#
# The inputs are environment variables. image.py puts every one of them
# in the cache key, so the same inputs give the same image:
#
#   WMF_SBX_STAGE           directory with tree.tgz, helpers/ and image.json
#   WMF_SBX_PACKAGES        apt packages, separated by spaces
#   WMF_SBX_NONO_VERSION    nono release, as in .nono-version
#   WMF_SBX_CLAUDE_VERSION  Claude Code release, for example 2.1.286
#   WMF_SBX_CLAUDE_PLATFORM linux-x64 or linux-arm64
#   WMF_SBX_CLAUDE_SHA256   checksum of that binary, from the release manifest
#   WMF_SBX_TREE_REV        the wmf-claude revision in tree.tgz
#   WMF_SBX_BUILDER_USER    Lima's user in the builder; removed at the end
#   WMF_SBX_AGENT_UID       the host user's uid, for `agent` (D10)
#   WMF_SBX_AGENT_GID       the host user's gid, for `agent`
#
# Proxy variables (http_proxy, https_proxy, no_proxy), if set, are used
# for every download. The last step seals the identity, so that each
# sandbox gets its own machine-id and SSH host keys at first boot. After
# this script, the builder must only be stopped, never booted again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 022

: "${WMF_SBX_STAGE:?}" "${WMF_SBX_PACKAGES:?}" "${WMF_SBX_NONO_VERSION:?}"
: "${WMF_SBX_NODE_VERSION:?}" "${WMF_SBX_NODE_FILE:?}" "${WMF_SBX_NODE_SHA256:?}"
: "${WMF_SBX_CLAUDE_VERSION:?}" "${WMF_SBX_CLAUDE_PLATFORM:?}" "${WMF_SBX_CLAUDE_SHA256:?}"
: "${WMF_SBX_TREE_REV:?}" "${WMF_SBX_BUILDER_USER:?}"
: "${WMF_SBX_AGENT_UID:?}" "${WMF_SBX_AGENT_GID:?}"

[[ "$(id -u)" == 0 ]] || { echo "image-build.sh: run as root" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

# apt does not read https_proxy for every transport; give it the setting.
APT_OPTS=()
if [[ -n "${https_proxy:-}" ]]; then
  APT_OPTS+=(-o "Acquire::https::Proxy=$https_proxy")
fi
if [[ -n "${http_proxy:-}" ]]; then
  APT_OPTS+=(-o "Acquire::http::Proxy=$http_proxy")
fi

step "apt packages"
read -r -a PKGS <<<"$WMF_SBX_PACKAGES"
apt-get "${APT_OPTS[@]}" update -q
apt-get "${APT_OPTS[@]}" install -y -q --no-install-recommends "${PKGS[@]}"

# The kernel. Debian's cloud kernel (the genericcloud image has it) has
# no 9p and no virtiofs, so the git-dir mounts (D10) do not mount: the
# fstab entries are there, the mount units stay dead (RAN, phase 4).
# Install the generic kernel and remove every cloud kernel, so that GRUB
# can boot only the generic one. The running kernel is a cloud kernel;
# debconf would stop its removal without the answer below.
step "kernel"
KARCH=$(dpkg --print-architecture)
apt-get "${APT_OPTS[@]}" install -y -q --no-install-recommends "linux-image-$KARCH"
echo 'linux-base linux-base/removing-running-kernel boolean false' | debconf-set-selections
apt-get "${APT_OPTS[@]}" purge -y -q "linux-image-cloud-$KARCH" 'linux-image-*-cloud-*'
mapfile -t KVERS < <(ls /lib/modules)
if [[ ${#KVERS[@]} -ne 1 || "${KVERS[0]}" == *cloud* ]]; then
  echo "image-build.sh: want one generic kernel, have: ${KVERS[*]}" >&2
  exit 1
fi
for mod in 9p 9pnet_virtio virtiofs; do
  modinfo -k "${KVERS[0]}" "$mod" >/dev/null
done
echo "  ${KVERS[0]}: 9p, 9pnet_virtio, virtiofs"
apt-get clean

# Node, pinned, from nodejs.org, checked against the checksum in the image
# inputs (image.py, NODE_VERSION). One copy for all users, owned by root;
# /usr/local/bin is before /usr/bin on every PATH. The build of the
# wmf-claude tree below uses it too. Debian's nodejs is not installed.
step "Node $WMF_SBX_NODE_VERSION"
NODE_DIR="/opt/node/v$WMF_SBX_NODE_VERSION"
TMP=$(mktemp -d)
curl -fsSL --retry 3 -o "$TMP/$WMF_SBX_NODE_FILE" \
  "https://nodejs.org/dist/v$WMF_SBX_NODE_VERSION/$WMF_SBX_NODE_FILE"
echo "$WMF_SBX_NODE_SHA256  $TMP/$WMF_SBX_NODE_FILE" | sha256sum -c --strict --quiet -
rm -rf "$NODE_DIR"
install -d -m 0755 "$NODE_DIR"
tar -xJf "$TMP/$WMF_SBX_NODE_FILE" -C "$NODE_DIR" --strip-components=1 --no-same-owner
rm -rf "$TMP"
chmod -R u+rwX,go+rX,go-w "$NODE_DIR"
for b in node npm npx corepack; do
  ln -sfn "$NODE_DIR/bin/$b" "/usr/local/bin/$b"
done
if dpkg -s nodejs >/dev/null 2>&1; then
  echo "image-build.sh: Debian's nodejs is installed; it must not be" >&2
  exit 1
fi
# npm's "new version available" notice is noise for the agent, which
# cannot update this npm anyway.
npm config --global set update-notifier false
[[ "$(command -v node)" == /usr/local/bin/node ]]
[[ "$(node --version)" == "v$WMF_SBX_NODE_VERSION" ]]
echo "  node $(node --version), npm $(npm --version)"

# nono, pinned and checked against the release's SHA256SUMS.txt, as
# lima/guest-install.sh does it.
step "nono $WMF_SBX_NONO_VERSION"
ARCH=$(dpkg --print-architecture)
DEB="nono-cli_${WMF_SBX_NONO_VERSION}_${ARCH}.deb"
BASE="https://github.com/nolabs-ai/nono/releases/download/v${WMF_SBX_NONO_VERSION}"
TMP=$(mktemp -d)
curl -fsSL --retry 3 -o "$TMP/$DEB" "$BASE/$DEB"
curl -fsSL --retry 3 -o "$TMP/SHA256SUMS.txt" "$BASE/SHA256SUMS.txt"
[[ "$(grep -cE "[[:space:]]${DEB}\$" "$TMP/SHA256SUMS.txt")" == 1 ]] \
  || { echo "SHA256SUMS.txt has no entry for $DEB" >&2; exit 1; }
(cd "$TMP" && grep -E "[[:space:]]${DEB}\$" SHA256SUMS.txt | sha256sum -c --strict --quiet -)
dpkg -i "$TMP/$DEB"
rm -rf "$TMP"
[[ "$(nono --version | awk '{print $2}')" == "$WMF_SBX_NONO_VERSION" ]]

# Claude Code, pinned to one release and checked against the checksum in
# that release's manifest. Not the install.sh pipe: it installs the
# newest release into one user's home, and checks nothing that we pin.
# One copy for all users, outside every home; the launcher turns off the
# auto-updater (bin/launch-claude.sh).
step "Claude Code $WMF_SBX_CLAUDE_VERSION"
CC_DIR="/opt/claude-code/$WMF_SBX_CLAUDE_VERSION"
install -d -m 0755 "$CC_DIR"
curl -fsSL --retry 3 -o "$CC_DIR/claude" \
  "https://downloads.claude.ai/claude-code-releases/$WMF_SBX_CLAUDE_VERSION/$WMF_SBX_CLAUDE_PLATFORM/claude"
echo "$WMF_SBX_CLAUDE_SHA256  $CC_DIR/claude" | sha256sum -c --strict --quiet -
chmod 0755 "$CC_DIR/claude"
ln -sfn "$CC_DIR/claude" /usr/local/bin/claude
claude --version

# The wmf-claude tree, built. Owned by root and not writable by any other
# user. The per-sandbox setup gives the agent the one file it writes
# (gerrit-mcp-server/server.log). Chrome runs on demand, so it is not
# built here.
step "wmf-claude $WMF_SBX_TREE_REV"
TREE="/opt/wmf-claude.$WMF_SBX_TREE_REV"
rm -rf "$TREE"
install -d -m 0755 "$TREE"
tar -xzf "$WMF_SBX_STAGE/tree.tgz" -C "$TREE"
[[ "$(cat "$TREE/.wmf-claude-rev")" == "$WMF_SBX_TREE_REV" ]]
WMF_CLAUDE_SKIP_CHROME=1 "$TREE/bin/wmf-claude-build"
chown -R root:root "$TREE"
chmod -R u+rwX,go+rX,go-w "$TREE"
ln -sfn "$TREE" /opt/wmf-claude

step "helpers"
for f in "$WMF_SBX_STAGE"/helpers/*; do
  install -m 0755 "$f" "/usr/local/bin/$(basename "$f")"
  echo "  /usr/local/bin/$(basename "$f")"
done

# What went in, for `wmf-sbx status` and for a person who finds the disk.
install -m 0644 "$WMF_SBX_STAGE/image.json" /etc/wmf-sbx-image.json

# Seal (HANDOFF-LIMA.md §5.2, RAN). cloud-init clean does not remove the
# SSH host keys. The builder's user goes too: each sandbox gets its users
# from its own cloud-init data, and the agent's uid differs per host
# (D10). userdel -f, because this script runs in that user's session.
step "seal"
rm -rf "$WMF_SBX_STAGE"
rm -f /etc/ssh/ssh_host_*
rm -f /etc/sudoers.d/90-cloud-init-users
userdel -r -f "$WMF_SBX_BUILDER_USER" 2>/dev/null || userdel -f "$WMF_SBX_BUILDER_USER"
cloud-init clean --logs --seed --machine-id
# CA certificates that the builder got for the build only ($WMF_SBX_CA_CERTS
# in image.py; cloud-init's names on Debian). The image must not trust them.
rm -f /usr/local/share/ca-certificates/cloud-init-ca-cert-*.crt
update-ca-certificates --fresh >/dev/null

# The agent, with the host user's uid and gid (D10), so that files on the
# read-only mounts are its own, and files it makes have the host's owner.
# After the builder's user is gone, so that the uid is free. A group with
# the gid can exist already (macOS gives users gid 20, which is `dialout`
# on Debian): then the agent uses it. No sudo, no extra groups; the
# per-sandbox provisioning sets the rest (HANDOFF-LIMA.md §5.3).
step "agent (uid $WMF_SBX_AGENT_UID, gid $WMF_SBX_AGENT_GID)"
if getent passwd "$WMF_SBX_AGENT_UID" >/dev/null; then
  echo "uid $WMF_SBX_AGENT_UID is in use: $(getent passwd "$WMF_SBX_AGENT_UID")" >&2
  exit 1
fi
getent group "$WMF_SBX_AGENT_GID" >/dev/null || groupadd -g "$WMF_SBX_AGENT_GID" agent
useradd -m -u "$WMF_SBX_AGENT_UID" -g "$WMF_SBX_AGENT_GID" -s /bin/bash \
  -c 'wmf-sbx agent (runs Claude Code)' agent
chmod 0750 /home/agent
# Give the freed blocks back to the qcow2 file (the drive has discard=on).
fstrim -av || true
sync
echo "image-build.sh: done"
