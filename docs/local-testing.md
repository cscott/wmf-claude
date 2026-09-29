# Local testing and dev containers

How to verify a change against your local wiki, and how to run dev tools when
that wiki lives in Docker.

Everything here is opt-in per session, because each mode opens a localhost port
the sandbox otherwise denies. The security trade-offs are summarised in
[`SECURITY.md`](../SECURITY.md#weakening-it-knowingly).

## Testing against a local wiki

The `manual-test` skill drives this ladder and always starts at Tier 1, so a
full browser session isn't spent on a check a plain HTTP request can answer.

### Tier 1 — curl and the API

```bash
claude --local-web            # opens localhost 80, 443, 8080
claude --local-web=443        # or narrow it; comma-separate for several
```

The session can now curl your wiki and hit `/w/api.php` for HTTP status,
redirects, rendered HTML, and API responses. No browser, no MCP loaded, and the
output is plain text you can `/compact` away.

`--open-port` is localhost-only, so this does **not** widen external network
access. It does mean the session can reach any other local service on those
ports, not just the wiki.

**On Linux, add `--landlock-only`.** nono denies every localhost connect in
this profile's proxy mode, so `--open-port` has no effect
([nolabs-ai/nono#1786](https://github.com/nolabs-ai/nono/issues/1786)).
`bin/claude` refuses `--local-web`, `--chrome`, `--local-db`, `--docker`, and
`--ide` on Linux without it. The flag weakens egress for the session, and it
refuses ports 80 and 443 (see [SECURITY.md](../SECURITY.md)), so point it at
your wiki's port:

```bash
claude --local-web --landlock-only      # Linux: opens 8080 only
```

### Tier 2 — chrome-devtools MCP

For screenshots, console errors, accessibility snapshots, and click-through
checks. Chrome runs outside the sandbox (it calls IOKit at startup and can't run
inside), so start it in its own terminal first:

```bash
bin/launch-test-chrome   # terminal 1: Chrome outside the sandbox, throwaway profile
claude --chrome          # terminal 2: implies --local-web, loads the MCP
```

`setup.sh` builds the MCP unless `WMF_CLAUDE_SKIP_CHROME` is set. The MCP
attaches over `127.0.0.1:9222`.

That port is **unauthenticated** and reachable by any local process, and the
attached Chrome's network is not restricted by the sandbox. Use throwaway
dev-wiki accounts only — never real credentials — and only navigate to URLs
you've authorized.

## Running dev tools in Docker

If your wiki runs in Docker (MediaWiki-Docker, MWDD, …) the host has no
PHP/composer/npm, so a sandboxed session can't run phpcs, phpunit, or composer
directly. Handing the sandbox the Docker socket is not an option — socket access
is root on the host and would defeat the sandbox — so the socket stays outside
behind a small, locked-down broker.

Launch with the service name of your MediaWiki container:

```bash
claude --docker=mediawiki                    # detect the compose file, start + own the broker
claude --docker=auto                         # pick the service from the compose file
claude --docker=mediawiki:/var/www/html/w    # set the in-container working directory
```

The broker starts automatically and stops when the session exits. Claude then
runs dev tools through the `mwdocker` shim — `mwdocker composer phpcs`,
`mwdocker vendor/bin/phpunit <path>` — which the `run-tests` and `lint` skills
do for you.

Only an allowlisted set of binaries is permitted (`composer`, `php`, `npm`,
`vendor/bin/{phpunit,phpcs,phpcbf,phan}`), the broker requires a per-session
token, and it refuses to serve a privileged or socket-mounting container.

### Custom compose layouts

If your compose file lives elsewhere, start the broker yourself and attach with
bare `--docker`:

```bash
bin/launch-docker-broker --service mediawiki --compose-file path/to/compose.yml
claude --docker
```

### Restricting the container's network

By default the container's network is **not** restricted by the sandbox: code
Claude runs in it (composer scripts, `php -r`) can reach any host. Two compose
overrides close that down:

| Override | Effect |
|---|---|
| `egress-none.yml` | The PHP containers get no route out at all. Lint, tests, and maintenance scripts work; `composer install` / `npm install` do not. |
| `egress-allowlist.yml` + `squid-allowlist.conf` | The PHP containers reach only allowlisted package-registry hosts through a squid sidecar, so installs work too. |

`setup.sh` installs them to `~/.config/wmf-claude/`, which the sandboxed agent
cannot write, and never overwrites a copy you've customized. Recreate your
containers with the override, then launch with the matching mode:

```bash
docker compose -f docker-compose.yml \
  -f ~/.config/wmf-claude/egress-none.yml up -d

claude --docker=mediawiki --egress=none      # or --egress=allowlist
```

`--egress` makes the broker verify the isolation at startup and refuse to serve
when the override isn't applied. Without it, the launcher prints a warning and
asks for a one-time acknowledgment.

Full threat model for both modes:
[`docs/security-rationale.md`](security-rationale.md#chrome-devtools-mcp) and
[the broker section](security-rationale.md#docker-exec-broker).
