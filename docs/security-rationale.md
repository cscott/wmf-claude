# Security rationale

Long-form reasoning behind the choices summarised in [`SECURITY.md`](../SECURITY.md).

Read [`SECURITY.md`](../SECURITY.md) first — it is the audit surface, and it states
what is allowed and denied, and links the relevant [nono docs](https://nono.sh/docs). This file explains *why*: the trade-offs taken, the failure modes that
forced them, and the residual risk accepted.

- [Localhost port opening](#localhost-port-opening)
- [Method-restricted (read-only) domains](#method-restricted-read-only-domains)
- [Artifact content host](#artifact-content-host)
- [Browser-profile denies](#browser-profile-denies)
- [Expected denies](#expected-denies)
- [Why each find deny needs two rules](#why-each-find-deny-needs-two-rules)
- [Linux glob caveat](#linux-glob-caveat)
- [Keychain access](#keychain-access)
- [Update notifier](#update-notifier)
- [chrome-devtools MCP](#chrome-devtools-mcp)
- [Docker exec broker](#docker-exec-broker)

## Localhost port opening

`bin/claude --local-web`, `--chrome` and `--local-db` pass `--open-port`
per-invocation rather than putting any real port in the static profile
(MariaDB's 3306 was the last one there). `/login` binds an OS-assigned callback
port, which needs a bind grant nono emits only when some port grant exists. On
macOS `bin/claude` passes a bind-only `--listen-port` (Seatbelt cannot filter
bind by port, so the value is nominal and no outbound rule is added); Linux
filters bind per port, where a fixed number would not cover an ephemeral
callback, so it is left as it was. Removing the old static `open_port: [3306]`
had silently removed that bind and broke `/login`. The per-invocation ports are
kept off the static profile, so a plain `bin/claude` session cannot reach a
leftover test Chrome or a local web service. `--local-web=PORT` (comma-separated
for several) narrows to just the ports a given setup needs; values are validated
as TCP ports before reaching the nono command.

`--open-port` is **localhost-only** (`nono run --help`: "Allow bidirectional
localhost TCP on a port"), so it does not widen external egress — outbound to
the internet stays gated by the `allow_domain` proxy. Verified with 80/443 open:
a raw-IP connect to an external host on those ports still fails, and a
non-allowlisted domain is still rejected with a CONNECT-tunnel `403`.

Residual risk: while set, the sandboxed agent can reach *any* local service on
80/443/8080, not just the wiki. Because `--chrome` implies `--local-web`,
browser sessions carry this same local-service reach on top of the CDP port.
Bounded, and kept off the static profile so it applies only to opt-in sessions.

## Method-restricted (read-only) domains

Every static host that does not need to write is allow-listed for read methods
(`GET` and `HEAD`) only, not as a plain CONNECT tunnel: the documentation
sites, the content wikis, the listed `*.wikimedia.org` subdomains, and
codesearch. Each is an object entry in `allow_domain` carrying endpoint rules:

```json
{ "domain": "www.php.net", "endpoints": [
  { "method": "GET", "path": "/**" },
  { "method": "HEAD", "path": "/**" }
] }
```

Any write or body-carrying request (POST/PUT/PATCH/DELETE), plus OPTIONS, is
rejected with `403` by nono's proxy before it leaves the sandbox, so a
proxy-routed agent cannot POST to these hosts (data exfiltration, unexpected
writes) while still reading them. GET and HEAD are both permitted because HEAD
is a safe read (no body, strictly less capable than GET), so denying it would
break `curl -I` and link-checkers without adding any security. OPTIONS stays
blocked because non-browser clients never need it.

The wikis are the channel that matters most. MediaWiki accepts anonymous and
temporary-account edits, and every write action must be POSTed, so a plain
tunnel lets an agent publish data to a public page. Read-only rules close that.
Reads still work: `api.php` queries take GET. A client that POSTs a long query
gets a `403`; switch it to GET or launch with `--allow-post`.

Hosts that need POST get only the paths that reads need:

- `phabricator.wikimedia.org`: the Phabricator MCP runs without an API token,
  so it scrapes HTML instead of calling Conduit. Its full-text task search
  gets a CSRF token from `GET /search/` and then submits `POST /search/`
  (`mcp-phabricator/src/scraper/search.js`), so `POST /search` is open. nono
  strips a trailing slash before it matches, so the rule has no trailing
  slash. Conduit (`/api/*`) and every other write form stay closed; the MCP is
  anonymous anyway. A session that sets `PHABRICATOR_API_TOKEN` switches the
  MCP to Conduit, which is POST even for reads, and needs `/api/<method>`
  rules for the methods it calls.
- `gitlab.wikimedia.org`: git smart-HTTP fetch is `GET /info/refs` followed by
  `POST .../git-upload-pack`, so `POST /**/git-upload-pack` is open. Push
  (`git-receive-pack`) stays closed.

There is no `*.wikimedia.org` wildcard. An endpoint route for a host wins over
a plain entry for the same host, so a wildcard can only be all-methods (opening
every chapter wiki to POST) or read-only (breaking Gerrit). The subdomains are
listed one by one; a subdomain not in the list is refused, so add it
explicitly. `gerrit.wikimedia.org` is the one plain tunnel: the Gerrit MCP
encodes file paths as `%2F`, and nono rejects any `%2F` path on a host with
endpoint rules, even for GET. The MCP is anonymous, so Gerrit refuses its
writes.

`phab.wmfusercontent.org` is read-only too. Phabricator serves file
attachments (task screenshots, pasted logs) from it, on a separate domain so
that uploaded content cannot run in the Phabricator origin. Without it an agent
reads a task's text but not its images. Uploads go through
`phabricator.wikimedia.org`, so GET/HEAD here opens no write path.

There is no `network_profile`. `minimal` is nono's `llm_apis` group: the three
Anthropic hosts plus thirteen third-party LLM APIs (OpenAI, OpenRouter, Groq,
DeepSeek, xAI, …) as plain tunnels. An injected prompt can carry its own API
key and POST data to an account that the attacker reads back. The Anthropic
hosts are listed in `allow_domain` directly, and a non-empty `allow_domain`
keeps the proxy in allowlist mode. We drop the group rather than `deny_domain`
its extra hosts, so that a host nono adds to the group later is not opened
without review.

`bin/claude --allow-post=HOST[,HOST]` re-opens all methods on read-only hosts
for one session. It passes nono `--allow-domain https://HOST/**`, which nono
turns into an any-method route named `_ep_HOST`. For a host that is itself a
profile entry, the route has the same name as the profile's GET/HEAD route and
replaces it: nono appends CLI entries after the profile's, and its route store
keeps the last route of a name (`proxy_runtime.rs`, `route.rs` `RouteStore`).
For a host under a profile wildcard (`en.wikipedia.org` under
`*.wikipedia.org`), the names differ and nono ORs the routes: a route whose
rules do not match is skipped, and the request gets `403` only when no route
allows it (`tls_intercept/handle.rs` `select_intercept_route`). The launcher
accepts only
hosts that match a read-only (GET/HEAD) profile entry, exactly or under its
`*.` wildcard, so the flag cannot add a host. It refuses phabricator and
gitlab, whose entries already allow some POST paths: an any-method route there
would open writes such as `git push` (`git-receive-pack`).

Important scoping caveat: the rule only covers egress that transits the nono
proxy, i.e. Bash-driven `curl`/`wget`/Node/Python. `WebFetch` egresses from
Anthropic's servers, not the laptop, so it never reaches these endpoint rules.
That opens no POST hole, because `WebFetch` only issues GETs and runs in `ask`
mode, but it does mean the read-only guarantee is specific to in-sandbox proxy
traffic, not a blanket no-POST property of the agent.

**Trade-off: endpoint rules force TLS interception.** ([nono networking docs](https://nono.sh/docs/cli/features/networking).)
To read the method and path
of an HTTPS request nono must terminate TLS itself (any entry with endpoint
rules takes the `requires_intercept` path). It mints a cert from an ephemeral
CA and injects that CA into the child's trust env
(`SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`/`CURL_CA_BUNDLE`), so curl, Node, and
Python trust it with no flag and no prompt. Four consequences: nono sees the
plaintext of traffic to these hosts, including all wiki and GitLab traffic;
Go tools that use the macOS system trust store (`gh`, `terraform`) reject the
minted cert unless launched with `--trust-proxy-ca`; PHP's curl extension
reads none of those variables, so in-sandbox CLI PHP fails with `self-signed
certificate in certificate chain` on these hosts (deliberately left so: PHP in
the sandbox has no need to reach production, and the local dev wiki is not
intercepted; run `php -d openssl.cafile="$SSL_CERT_FILE" …` if a script must,
since the curl extension reads `openssl.cafile` before `curl.cainfo`); and nono
rejects ambiguous paths (`%2F`, `%2E`, `;`, or a `%` left after one decode,
such as `%25` for a literal `%` in a file name) on these hosts with a `403`,
even for GET. The query string is exempt. The Wikimedia REST API encodes a
slash in a page title as `%2F` (`/api/rest_v1/page/html/AC%2FDC`), and the
GitLab API encodes project paths the same way, so those requests fail. Use
`api.php?titles=…` for such titles.

Left as plain tunnels on purpose (no endpoint rules, no interception):

- `api.anthropic.com`, `claude.ai`, `platform.claude.com`: the model API is POST,
  so intercepting would `403` every model call and route Claude's own
  conversation and tokens through nono in plaintext.
- `*.claudeusercontent.com`: see [Artifact content host](#artifact-content-host).
- `gerrit.wikimedia.org`: see above (`%2F` paths, anonymous MCP).
- `*.local.wmftest.net`: the local dev wiki. The `manual-test` flow logs in
  and edits there, and the traffic stays on the machine.
- `api.minimax.io` is **not** in the static profile. It was, which put a
  third-party LLM API within reach of every session; `bin/claude --minimax` now
  adds it per session with nono's `--allow-domain`, the same pattern as the
  localhost ports.

### Verifying the method filtering

`tests/test-profile.sh` asserts the structure (each read-only host is an
endpoint-restricted GET/HEAD object, phabricator and gitlab open only their
POST paths, and the model API domains and Gerrit stay plain). It sends no POST
to a production host. The rules for every read-only host use the same
mechanism, so the live check below exercises one docs host and does not POST
to a wiki. Run it **outside** the sandbox (nested nono cannot write its audit
dir):

```bash
URL='https://www.php.net/manual/en/function.array-keys.php'
PROFILE=./profiles/wmf-engineer.json

# Reads: expect 200 (allowed; served by php.net through nono's interception).
nono run --profile "$PROFILE" --allow-cwd -- \
  curl -sS -o /dev/null -w 'GET  -> %{http_code}\n' "$URL"
nono run --profile "$PROFILE" --allow-cwd -- \
  curl -sS -o /dev/null -w 'HEAD -> %{http_code}\n' -I "$URL"

# Write: expect 403 (nono rejects before the request reaches php.net).
nono run --profile "$PROFILE" --allow-cwd -- \
  curl -sS -o /dev/null -w 'POST -> %{http_code}\n' -X POST "$URL"

# Opt-in: the same POST with the session's all-methods route is NOT a nono 403.
# It reaches php.net, which answers a real POST itself, proving the 403 above
# is rule-specific (method filtering) and that --allow-post's route opens it.
nono run --profile "$PROFILE" --allow-cwd --allow-domain 'https://www.php.net/**' -- \
  curl -sS -o /dev/null -w 'POST(opt-in) -> %{http_code}\n' -X POST "$URL"
```

Expected: `GET -> 200`, `HEAD -> 200`, `POST -> 403`, `POST(opt-in) -> ` a
non-403 upstream code. A `403` on the read paths means interception broke (CA
not trusted, or the rule too narrow); a `403` on the opt-in POST means the
`--allow-post` route does not combine with the profile rule.

A `403` here is nono's signature (php.net would answer a real POST with `405`),
emitted with a `tls_intercept: endpoint rules denied POST ... no rule matched`
log line and recorded in `~/.nono/audit/<session>/`.

## Artifact content host

`*.claudeusercontent.com`.

Claude Code's `Artifact` tool publishes to `claude.ai`, but *reads* the
published page from a per-artifact subdomain of `claudeusercontent.com`. With
only `claude.ai` allowlisted, the `claude.ai`-side calls (publish, `list`)
worked while every content read (`read`, `list` scope `files`, asset fetch)
failed with a CONNECT-tunnel `403`. Because the tool refuses to publish over
an artifact the conversation has not read, that made an already-published
artifact impossible to update (T437718).

Allowed as a plain tunnel, not an endpoint-restricted read-only entry, for two
reasons:

- Method rules would buy close to nothing. The point of `GET`/`HEAD` rules is
  to close a body-carrying exfiltration channel, but `claude.ai` is already a
  plain tunnel *and* is the channel that writes artifacts — an agent that
  wanted to exfiltrate would publish an artifact through `claude.ai`, not POST
  to a static content CDN. Restricting the read host does not close that.
- Interception has a real cost here. Endpoint rules force nono to terminate
  TLS, which would put artifact bodies — up to 16 MB, often binary assets —
  through nono in plaintext, and adds a CA-trust dependency on whatever HTTP
  client the `Artifact` tool uses.

Residual risk: `claudeusercontent.com` serves *user-generated* content, and is
a separate domain from `claude.ai` precisely so that untrusted artifact content
is sandboxed away from the app origin. Reading an artifact that someone else
authored is therefore a prompt-injection surface. Three things bound it:

- Artifacts the engineer owns hold content Claude itself wrote — the common
  case, and the one T437718 was about.
- For an artifact merely *shared* with the engineer, the `Artifact` tool
  returns an isolated summary rather than raw HTML, and its contract treats
  what comes back as data, never instructions.
- This is the same trust class we already accept for `*.wikipedia.org` and the
  rest of the wiki family, which Claude reads routinely and which any reader
  can edit.

The allowlist entry does not make artifacts readable on its own — the engineer
must still be signed in to the Claude account that owns or was shared the
artifact.

## Browser-profile denies

The profile denies the Chromium-based browser profile dirs
`~/Library/Application Support/{Chromium,BraveSoftware,Vivaldi,com.operasoftware.Opera}`.

This is defense in depth for the optional `chrome-devtools` MCP — the sandboxed
MCP server could in principle read those paths directly off disk, and these
denies remove that vector. The base `claude-code` profile we extend already
inherits the `deny_browser_data_macos` group, which covers Chrome, Firefox,
Edge, Arc, Brave Browser, and Safari; we add only the Chromium derivatives that
group misses.

`BraveSoftware` is also a path correction: the upstream group denies
`~/Library/Application Support/Brave Browser`, but the actual macOS storage path
is `BraveSoftware/Brave-Browser`. Both are listed for belt-and-suspenders
coverage of older and current Brave installs.

## Expected denies

`filesystem.suppress_save_prompt`.

Claude Code itself probes
`Library/Application Support/{Google/Chrome,Google/Chrome Beta,Google/Chrome Canary,Chromium,Microsoft Edge,BraveSoftware/Brave-Browser}/DevToolsActivePort`
at startup to auto-discover a running Chromium-family browser with
`--remote-debugging-port` open (classic `chrome-launcher` /
`chrome-remote-interface` behaviour). The Claude binary also embeds `/home/...`
paths from its CI builder, which Bun's runtime stats during identifier
resolution.

All of these reads are correctly denied by the profile — but without
intervention nono would offer to save them as grants on every first run, which
is a confusing first-time-user experience for a denial that is working as
designed. `filesystem.suppress_save_prompt` silences the save-profile dialog for
these paths; the sandbox still denies the reads and the denial diagnostic still
prints to stderr. Arc, Vivaldi, and Opera are listed alongside the
Chromium-family paths because the base `deny_browser_data_macos` group sometimes
surfaces them in the same batched prompt even though the current Claude binary
doesn't appear to probe them directly.

`~/.local/state/claude/locks` and `~/.CFUserTextEncoding` are also suppressed.
Both are listed as granted (`r+w` and `r` respectively) in the runtime
capability set, but nono still reports them as denied at shutdown and offers to
save them as grants — most likely a child process at teardown not inheriting the
dynamic grants, or a read that races with `Applying sandbox...`. The reads
aren't breaking anything we can observe; suppressing keeps the first-run prompt
clean while the underlying inherited-grant question gets investigated
separately.

## Why each find deny needs two rules

`-exec`, `-execdir`, `-delete`, `-ok`, `-okdir`, `-fprint*` and `-fls*` are
denied for `find`, which leaves only the read-only traversal forms. `-exec` is
otherwise effectively shell escape, and `-delete`/`-fprint*`/`-fls*` write.

Each action takes **two** rules, `Bash(find -exec*)` and `Bash(find * -exec*)`,
because the glob syntax has no alternation and each form alone is wrong:

- `Bash(find:* -exec*)` is rejected at load. Claude Code requires `:*` to
  end the pattern ("The :\* pattern must be at the end"), so the rule is
  dropped with a warning and nothing is denied.
- `Bash(find * -exec*)` alone needs a path operand. GNU find lets you omit
  it, so `find -exec rm {} \;` slips through to a prompt.
- `Bash(find *-exec*)` alone becomes a substring match. It denies a
  read-only `find ./pre-delete -name x`, and a deny gives no prompt to
  override.

`tests/test-templates.sh` fails if an action loses either form.

A related shape note: there are no `Write(...)` rules anywhere in
`wiring/settings-merge.json`. File-permission checks never match them (Claude
Code warns at startup), and `Edit(path)` already covers Write and NotebookEdit.

## Linux glob caveat

On Linux, Claude Code ignores glob patterns in `Read` and `Edit` rules ("On
Linux, glob patterns in Edit/Read rules will be ignored"). `Bash` glob rules are
not affected, so the `find` denies still apply. What does not apply is every
`Read(//**/…)` credential rule and every `Edit` twin beside it.

Two things fill the gap only in part:

- The non-glob fallbacks are 10 exact `Read` names, `.env` through
  `.env.staging.local`. They cover no other spelling — `.envrc` is not among
  them — and there is **no** non-glob `Edit` twin, so on Linux nothing at the
  tool layer stops the agent overwriting a `.env`.
- The nono OS-level sandbox still denies `~/.ssh` and the other credential
  paths on every platform, but it grants the workdir read+write.

So the gap is workdir-local: on Linux an unusually-named or deep-nested
`.key`/`.pem`/`.secret`/`.credential` file inside the workdir is blocked by
neither layer, for reads or for writes, whereas on macOS the tool glob blocks
both. Avoid keeping secrets in the workdir on Linux; `bin/wmf-claude-setup`
prints this warning on Linux hosts.

The `//` prefix on the macOS rules anchors at the filesystem root; a bare `**/`
rule is relative to the session cwd. The cwd-relative forms stay beside them for
the Linux path.

## Keychain access

The keychain daemons are not denied and the base `claude-code` profile grants
`~/Library/Keychains`: Claude Code reads its login token through them at
startup, and denying them breaks `/login`. This is the largest residual exfil
surface (a prompt-injected agent could read other stored credentials); it is
bounded by the egress allowlist and the env-var allowlist.

Closing it with nono 0.78's sandboxed OAuth capture was attempted and reverted.
Findings, for whoever tries next:

- The grant comes from the `claude_code_macos` group *and* the pack profile
  body with `bypass_protection`; a child can exclude the group but cannot
  remove the body grant, and because a grant exists nono skips its own five
  keychain-daemon denies, so a profile must restate all five.
- Capture works, but the login route injects the token only at launch: a
  `/login` inside a session got `407` afterwards. `claude auth login` cannot be
  the pre-step (it binds an ephemeral port, denied; no manual mode). Without
  `env_var`/`base_url_env_var` the provider hosts are unrouted (`403` at
  startup); with them and allow-by-default, the client opened a direct
  CONNECT to `api.anthropic.com`, which nono correctly refuses for a route
  upstream. Why it bypasses the injected base URL is the open question.

Upstream asks: the pack should not grant `~/Library/Keychains` (with capture it
needs no keychain), or nono should let a child subtract an inherited
`bypass_protection`; and guidance for the interactive client behind an
`oauth_capture` route.

## Update notifier

`bin/claude` runs unsandboxed, so in an interactive terminal it does a
throttled (once/24h) background `git fetch origin` of the install checkout to
notify the engineer when wmf-claude (vs `origin/main`) or nono is out of date.

It does not update silently. When the install is on `main` with a clean tree
and a tty on both stderr and stdin, it prints the commits that would land and
asks; anything else prints the command and changes nothing. Saying yes runs
`git merge --ff-only` against the exact ref those commits were listed from —
not `git pull`, which re-fetches and follows the branch's configured upstream
and so could land commits that were never shown. That keeps the original
property in a weaker but honest form: no *unreviewed* code is fetched and run.
It is skipped for non-interactive runs (`claude -p`, the VS Code extension)
where stderr is not a terminal. The fetch uses
`ssh -o BatchMode=yes` (no credential prompts) and writes a single timestamp
under `${XDG_CACHE_HOME:-~/.cache}/wmf-claude`. Opt out with
`WMF_CLAUDE_SKIP_UPDATE=1`.

## chrome-devtools MCP

Attach mode.

> **Local-dev only.** chrome-devtools support is wired up by `setup.sh`, not
> by the signed nono pack — `wiring/` does not install `bin/launch-test-chrome`,
> the `chrome-devtools-mcp/` install dir, or any chrome-related Claude Code
> settings. Engineers who installed via `nono pull` will see the `manual-test`
> skill listed but neither `bin/claude --chrome` nor the MCP itself; the
> SessionStart hook is conditional on `mcp__chrome-devtools__*` tools being
> present so it stays quiet for those sessions. If/when the pack ships
> chrome-devtools support, revisit this section and the wiring directives.

The optional `chrome-devtools` MCP runs the *server* inside the sandbox but
its *Chrome* outside. Chrome cannot run inside the wmf-engineer profile —
it calls `IONotificationPortCreate(kIOMainPortDefault)` during early init,
and when IOKit is denied at the Mach layer (which the base `claude-code`
profile does, since LLM API talkers don't need driver access) the call
returns NULL and Chrome segfaults before it ever paints a pixel. Allowing
IOKit broadly to fix this would hand any prompt-injected agent driver-
level capability — too big a grant for one MCP.

Instead: `bin/launch-test-chrome` starts Chrome unsandboxed with a fresh
`mktemp -d` user-data-dir and `--remote-debugging-port=9222
--remote-debugging-address=127.0.0.1`. The address pin matters: with the
port flag alone, some Chrome builds bound the CDP listener on 0.0.0.0,
which would expose the unauthenticated debugging socket to the LAN.

**Per-session opt-in.** The MCP is **not** registered globally with `claude
mcp add`. Instead, `bin/claude --chrome`:

- adds `--open-port 9222` to the nono invocation so the sandbox can reach
  the CDP socket only for this session
- passes `--mcp-config` so the chrome-devtools tools are only present when
  asked for
- grants the sandbox read+write on the install dir

Sessions launched without `--chrome` have no chrome-devtools MCP loaded
*and* cannot reach 127.0.0.1:9222 even via raw `Bash(curl:*)`. The CDP-
controlled-browser attack surface is present only when an engineer is
actively running a manual test in *this* session.

**Supply-chain pinning.** `chrome-devtools-mcp` is pinned to a single
version in `chrome-devtools-mcp/package.json` and `package-lock.json`
(both tracked in git). `bin/wmf-claude-build` runs `npm ci --ignore-scripts`,
which reproduces node_modules from the lockfile and refuses any `postinstall`
hook from the package or its deps. The package currently bundles its
runtime dependencies (puppeteer-core, etc.) into the published tarball,
so pinning the one version pins the whole tree. `bin/wmf-claude-build` enforces
this as a checked invariant: after `npm ci`, it asserts exactly one
top-level package directory under `node_modules/` and fails the install
if upstream ever stops bundling — review the new tree before bumping
the pin.

**What this preserves:**

- Fresh per-launch `mktemp -d` user-data-dir at mode 0700 — the testing
  Chrome has no cookies, saved passwords, or history from the engineer's
  regular browsing. The dir is removed when Chrome exits via the cleanup
  trap, so cookies / Service Workers / localStorage / IndexedDB set
  during one session don't carry into the next. (SIGKILL bypasses the
  trap and leaks the dir; `bin/wmf-claude-setup` sweeps stale `wmf-claude-chrome.*`
  dirs older than a day at install time as a safety net.)
- The MCP server is still sandboxed. Anything *it* tries to do (reading
  files, hitting the network, spawning processes) is bounded by the same
  rules as the rest of the agent.

**What this gives up — and what's left as residual risk:**

- Pages loaded in the attached Chrome have **unrestricted outbound
  network**. The nono network allowlist applies to the MCP server, not to
  the externally-launched Chrome. A page loaded by Chrome (or arbitrary
  JS run via `mcp__chrome-devtools__evaluate_script`) can fetch from any
  domain. Mitigations: prompt discipline (the `manual-test` skill says to
  only navigate to authorized URLs), per-session opt-in (the MCP is gone
  in the next session), and Chrome hardening flags
  (`--disable-background-networking`, `--no-pings`, `--disable-sync`,
  `--disable-component-update`) that disable Chrome's own background
  chatter even if the agent isn't navigating anywhere yet.
- The CDP endpoint on `127.0.0.1:9222` is **unauthenticated**. Any
  process running as the engineer can connect and drive the test Chrome
  (run JS, exfiltrate cookies, navigate). Chrome's `--remote-debugging-
  port` does enforce a `Host:` header check that mitigates DNS rebinding
  from a page in the engineer's *other* browser, but this is a hardening
  layer rather than an absolute boundary. `chrome-devtools-mcp` doesn't
  expose a pipe transport (`--browserUrl`/`--wsEndpoint`/`--autoConnect`
  only), so we can't move CDP off a listening socket without changing
  upstream. Rationale for accepting the risk: the local-process trust
  boundary here is roughly the same as for the rest of the sandbox
  (anything running as the engineer can already do a lot).
- Chrome itself runs with the engineer's normal user privileges and full
  Mach/IOKit access. nono no longer adds a layer on top while Chrome is
  running. Chrome's own renderer sandbox still bounds malicious *page*
  content from escaping the renderer, but it does **not** bound the
  agent — CDP gives the agent more authority over the browser than any
  renderer ever has.

The trade is deliberate: the network restriction we lose was a defense
against a malicious *page* exfiltrating; the fresh-per-launch user-data-
dir we keep is a defense against a malicious *agent* using the browser
to read the engineer's authenticated sessions. The latter is the bigger
threat in the wmf-claude threat model. Per-session opt-in further bounds
the window during which any of these costs are paid.

## Docker exec broker

Local-dev only. The broker ships with the checkout (`bin/launch-docker-broker`,
`bin/mwdocker`, `bin/claude --docker`), not with the signed nono pack. Without
`--docker` there is no broker, no open port, and `mwdocker` is not on `PATH`.

A sandboxed session on a Docker-based wiki has no host PHP/composer/npm. The
Docker socket can't go in the sandbox (it is root-equivalent on the host: `docker
run -v /:/host` reads/writes the whole filesystem as root), so the socket stays
outside the sandbox behind the broker. `bin/claude --docker=SERVICE[:WORKDIR]`
starts the broker, opens only its ephemeral localhost port (not in the static
profile), and stops it on exit. Bare `--docker` attaches to a manually started
broker. `--docker=auto` resolves the service from the compose file.

What the broker enforces:

- No shell. argv is built as a list and run with `shell=False`.
- Pinned docker flags: `<compose> -f <file> exec -T [-w <wd>] <service> <bin>
  <args>`. The caller controls only `<bin>` (allowlisted) and its `<args>`. It
  cannot set `-u`, `-v`, `--privileged`, `--entrypoint`, switch `exec` to `run`,
  or choose the compose file or service.
- A 256-bit per-session bearer token from a `0600` handshake file, compared
  constant-time.
- Refuses a container that is a path to host root: `--privileged`, host-root caps
  (`SYS_ADMIN`, `SYS_PTRACE`, ...), device passthrough, host `PidMode`/`IpcMode`,
  or a bind/mount of the socket or a host-root path (`/`, `/var/run`, `/run`,
  `/var/lib/docker`, `/proc`, `/sys`). Also refuses a bind/mount of host
  credentials — a `$HOME` dotfile/dotdir (`~/.ssh`, `~/.aws`, `~/.config/*`,
  `~/.gitconfig`, ...) or the forwarded `$SSH_AUTH_SOCK` — which the agent could
  read and exfiltrate over the container network. Inspects every replica.
  Best-effort: it can't inspect a container that isn't up, and a container
  recreated after the check is a TOCTOU gap.
- Logs every exec and applies a per-command timeout (default 900s). Caps the
  request body (1 MiB) and the output relayed per stream (8 MiB, spooled to
  disk, truncation noted), so one command can't pin the broker's memory.

The managed broker stops on session exit. A SIGKILL leaks the broker and its
handshake/shim files; the next `--docker` launch sweeps artifacts whose launcher
PID is gone and reaps the orphaned broker.

Residual risk:

- The container's network is not nono-restricted: `composer`/`npm` fetch and
  run scripts over the network, and `php -r` is full code execution, so by
  default running dev tools in the container gives the agent an outbound
  channel. nono cannot reach the container's network namespace; the restriction
  has to happen at the compose layer. The `templates/docker-egress/` overrides
  do that: `egress-none.yml` removes the PHP containers' route out entirely,
  and `egress-allowlist.yml` funnels them through a squid sidecar that permits
  only package-registry hosts (exfiltration to an allowed host remains
  possible). Keep the override copies outside the checkout (the agent can edit
  checkout files; it cannot apply them — containers must be recreated — but an
  unwritable copy closes even the proposal vector). `--egress=none|allowlist`
  makes the broker verify the running container's isolation at startup and
  refuse to serve when the override is not applied; without `--egress` the
  launcher prints an egress warning and asks for a one-time acknowledgment.
- The allowlist (`composer`, `php`, `npm`, `vendor/bin/phpunit|phpcs|phpcbf|phan`)
  is not an RCE boundary: `composer`/`npm` run agent-writable scripts and `php`
  runs arbitrary code. It blocks reaching raw `docker`; it does not sandbox the
  container from its own source. The boundary is the host.
- The localhost port is shared with other processes running as the engineer; the
  token keeps them out. It does not gate the sandboxed agent, which holds the
  token by design.

Stock MediaWiki-Docker (`docker-compose up -d` in the core checkout) mounts only
the checkout (`.:/var/www/html/w`), runs as your UID, and mounts no socket, home
dir, or privileged config. There a rogue agent can read and exfiltrate the
checkout (dev `LocalSettings.php`, `.env`) over the container network and reach
the dev database, but does not get host root, your home dir, SSH keys, or git
credentials (not mounted). It cannot escape to the host short of a kernel
exploit. The container-safety check refuses the common credential-widening
mounts — a `$HOME` dotfile/dotdir (`~/.ssh`, `~/.aws`, `~/.composer`, ...) or
`$SSH_AUTH_SOCK` — but it is a denylist, not an allowlist: a non-dot secret path
mounted by name, a non-dot sibling dir it can't distinguish from a code mount,
or a `user: root` container still slip through. Keep secrets out of dev checkouts
and mounts limited to the checkout.
