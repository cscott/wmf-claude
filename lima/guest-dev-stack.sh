#!/bin/bash
# PHP, Composer, MariaDB and headless Chromium in the Lima guest, so PHPUnit,
# the maintenance scripts and the chrome-devtools MCP work as on the host,
# without Docker, on any host architecture. lima/guest-install.sh runs this
# as root, on create and on every update. Each step is idempotent.
#
# - Debian packages only, signed by the distribution: PHP with the
#   extensions MediaWiki core requires or suggests, Composer, MariaDB,
#   Chromium.
# - MariaDB is a system service (user mysql) on 127.0.0.1 only. A wiki user
#   with a fixed password owns the wiki database. The VM has no port
#   forwards, so nothing outside it reaches the port, and the agent reads
#   the password in LocalSettings.php anyway. root@localhost keeps
#   unix_socket authentication: OS root only, which the agent cannot become.
# - Chromium runs headless as its own user, wmfchrome (lima/guest-chrome.sh),
#   which the nftables table wmf_claude_chrome keeps to loopback: the browser
#   shows the local wiki and nothing else. docs/lima-vm.md#a-browser-in-the-vm.
# - The agent joins no group here. docs/lima-vm.md#phpunit-in-the-vm.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

[[ "$(id -u)" == 0 ]] || { echo "error: guest-dev-stack.sh runs as root" >&2; exit 1; }

DB_NAME=wikidb
DB_USER=wikiuser
DB_PASS=wikipass
CHROME_USER=wmfchrome

# core's composer.json: ext-calendar, ctype, fileinfo, iconv, libxml, openssl
# and pdo come with php-cli; php-xml has dom, simplexml, xml, xmlreader;
# php-mysql has mysqli; php-sqlite3 is the SQLite database type. The rest
# are suggested: apcu, bcmath, curl, gd. unzip and zip are for Composer.
# Chromium without its recommends: no setuid chromium-sandbox, the user
# namespace sandbox serves. fonts-liberation, so headless screenshots have
# a font.
PKGS=(php-cli php-xml php-mbstring php-intl php-mysql php-sqlite3 php-curl php-gd php-apcu php-bcmath php-zip unzip composer mariadb-server mariadb-client chromium fonts-liberation)
MISSING=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
if (( ${#MISSING[@]} > 0 )); then
  apt-get update -q
  apt-get install -y -q --no-install-recommends "${MISSING[@]}"
fi

# Loopback only, whatever the packaged defaults say. Restart the service
# only when this file changes: an update must not cut a running session's
# connections for nothing.
CNF=/etc/mysql/mariadb.conf.d/90-wmf-claude.cnf
NEWCNF=$(mktemp)
cat > "$NEWCNF" <<'CNF'
# Written by wmf-claude (lima/guest-dev-stack.sh). Do not edit.
[mysqld]
bind-address = 127.0.0.1
skip-networking = 0
CNF
systemctl enable mariadb >/dev/null 2>&1
if ! cmp -s "$NEWCNF" "$CNF"; then
  install -m 0644 "$NEWCNF" "$CNF"
  systemctl restart mariadb
else
  systemctl start mariadb
fi
rm -f "$NEWCNF"

# The wiki user, for TCP from a session with --local-db (127.0.0.1) and for
# the socket (localhost), which nono does not mediate on Linux. Rights on
# the wiki database only: PHPUnit keeps its tables there, under a prefix.
mariadb --protocol=socket <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\`;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
CREATE USER IF NOT EXISTS '$DB_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

# The browser user: a system account, no login shell, in no other group.
# Root never writes under its home; lima/guest-chrome.sh uses a fresh
# temporary directory as HOME each run.
if ! id -u "$CHROME_USER" >/dev/null 2>&1; then
  adduser --system --group --home "/var/lib/$CHROME_USER" --shell /usr/sbin/nologin \
    --gecos 'wmf-claude headless browser' "$CHROME_USER" >/dev/null
fi
for g in sudo docker; do
  if getent group "$g" >/dev/null && id -nG "$CHROME_USER" | grep -qw "$g"; then
    gpasswd -d "$CHROME_USER" "$g" >/dev/null
  fi
done

# The browser's firewall: nothing the browser user sends leaves a loopback
# interface. Its own table, in its own file and unit, so the template's
# boot-time rewrite of /etc/nftables.conf and a restart of nftables.service
# both leave it in place (PartOf loads it again after a restart).
CHROME_UID=$(id -u "$CHROME_USER")
install -d -m 0755 /etc/wmf-claude
NEWNFT=$(mktemp)
cat > "$NEWNFT" <<NFT
#!/usr/sbin/nft -f
# Written by wmf-claude (lima/guest-dev-stack.sh). Do not edit.
# The headless browser (uid $CHROME_UID, $CHROME_USER) reaches loopback only.
table inet wmf_claude_chrome
delete table inet wmf_claude_chrome
table inet wmf_claude_chrome {
  chain output {
    type filter hook output priority filter; policy accept;
    meta skuid $CHROME_UID oifname != "lo" counter reject
  }
}
NFT
if ! cmp -s "$NEWNFT" /etc/wmf-claude/chrome.nft; then
  install -m 0644 "$NEWNFT" /etc/wmf-claude/chrome.nft
fi
rm -f "$NEWNFT"
cat > /etc/systemd/system/wmf-claude-chrome-net.service <<'UNIT'
[Unit]
Description=wmf-claude headless browser firewall (loopback only)
After=nftables.service
PartOf=nftables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/wmf-claude/chrome.nft

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable wmf-claude-chrome-net.service >/dev/null 2>&1
systemctl restart wmf-claude-chrome-net.service

echo "php $(php -r 'echo PHP_VERSION;'), mariadb $(mariadb --protocol=socket -N -e 'SELECT VERSION()'), 127.0.0.1:3306, $DB_USER/$DB_PASS, database $DB_NAME; $(chromium --version 2>/dev/null | awk '{print tolower($1), $2}') headless as $CHROME_USER, loopback only"
