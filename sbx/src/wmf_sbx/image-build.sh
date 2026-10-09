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
#
# Proxy variables (http_proxy, https_proxy, no_proxy), if set, are used
# for every download. The last step seals the identity, so that each
# sandbox gets its own machine-id and SSH host keys at first boot. After
# this script, the builder must only be stopped, never booted again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 022

: "${WMF_SBX_STAGE:?}" "${WMF_SBX_PACKAGES:?}" "${WMF_SBX_NONO_VERSION:?}"
: "${WMF_SBX_CLAUDE_VERSION:?}" "${WMF_SBX_CLAUDE_PLATFORM:?}" "${WMF_SBX_CLAUDE_SHA256:?}"
: "${WMF_SBX_TREE_REV:?}" "${WMF_SBX_BUILDER_USER:?}"

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
apt-get clean

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
# Give the freed blocks back to the qcow2 file (the drive has discard=on).
fstrim -av || true
sync
echo "image-build.sh: done"
