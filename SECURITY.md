# Security model

Threat model and design rationale for the wmf-engineer nono profile and the
bundled Claude Code permission settings.

## Two layers of restriction

1. **OS-level (nono / Seatbelt)** — `profiles/wmf-engineer.json`. Restricts
   filesystem, network, environment variables, signals, and Mach services for
   the entire Claude Code process tree.
2. **Tool-level (Claude Code permissions)** — `wiring/settings-merge.json`.
   Restricts what Claude itself can invoke via its tools (`Bash`, `Read`,
   `Edit`, `Write`, `WebFetch`). Applied by the nono pack at install time;
   **not** applied by `setup.sh` for local-dev installs (copy fragments by
   hand if you want them today).

When changing one layer, consider whether the other should change too.

## Network

- Deny by default — `network_profile: minimal` (Anthropic LLM API endpoints only).
- Allowlisted: `console.anthropic.com`, `claude.ai`, Wikimedia + wiki-family
  domains, `codesearch{,-backend}.wmcloud.org`, language doc sites
  (`php.net`, MDN, `docs.python.org`, `docs.rs`, `doc.rust-lang.org`,
  `nodejs.org`, `pkg.go.dev`), `api.minimax.io`.
- `network.open_port: [3306]` — local MariaDB connections allowed. Port 9222
  (chrome-devtools CDP) is **not** in the static profile; `bin/claude --chrome`
  passes `--open-port 9222` per-invocation so plain `bin/claude` sessions
  cannot reach a leftover test Chrome.
- SSH push (port 22, including Gerrit's 29418) is unreachable. HTTPS push to
  Wikimedia hosts is reachable but requires a Gerrit HTTP password most
  engineers don't have. GitHub push is unreachable (`github.com` not
  allowlisted).

## Filesystem denies (nono profile)

- Shell configs: `~/.bashrc`, `~/.bash_profile`, `~/.bash_history`, `~/.zshrc`, `~/.zprofile`, `~/.zsh_history`, `~/.profile`.
- Credentials: `~/.ssh`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.composer/auth.json`, `~/.config/composer/auth.json`, `~/.docker/config.json`, `~/.kube/config`, `~/.config/gh`, `~/.env`.
- Password managers: `~/.password-store`, `~/.config/{bitwarden,keepassxc}`, `~/Library/Application Support/{1Password,Bitwarden,Enpass}`.
- Private comms: `~/Library/{Mail,Messages}`, `~/.thunderbird`, `~/Library/Application Support/{Slack,Discord,Signal,Telegram}`.
- iCloud Drive: `~/Library/Mobile Documents`.
- Additional Chromium-based browser profile dirs: `~/Library/Application Support/{Chromium,BraveSoftware,Vivaldi,com.operasoftware.Opera}`. Defense in depth for the optional `chrome-devtools` MCP — the sandboxed MCP server could in principle read those paths directly off disk, and these denies remove that vector. The base `claude-code` profile we extend already inherits the `deny_browser_data_macos` group, which covers Chrome, Firefox, Edge, Arc, Brave Browser, and Safari; we add only the Chromium derivatives that group misses. (Note: `BraveSoftware` is also a path correction — the upstream group denies `~/Library/Application Support/Brave Browser`, but the actual macOS storage path is `BraveSoftware/Brave-Browser`. Both are listed for belt-and-suspenders coverage of older and current Brave installs.)

## Expected denies (`filesystem.suppress_save_prompt`)

Claude Code itself probes `Library/Application Support/{Google/Chrome,Google/Chrome Beta,Google/Chrome Canary,Chromium,Microsoft Edge,BraveSoftware/Brave-Browser}/DevToolsActivePort` at startup to auto-discover a running Chromium-family browser with `--remote-debugging-port` open (classic `chrome-launcher` / `chrome-remote-interface` behaviour). The Claude binary also embeds `/home/...` paths from its CI builder, which Bun's runtime stats during identifier resolution. All of these reads are correctly denied by the profile — but without intervention nono would offer to save them as grants on every first run, which is a confusing first-time-user experience for a denial that is working as designed. `filesystem.suppress_save_prompt` silences the save-profile dialog for these paths; the sandbox still denies the reads and the denial diagnostic still prints to stderr. Arc, Vivaldi, and Opera are listed alongside the Chromium-family paths because the base `deny_browser_data_macos` group sometimes surfaces them in the same batched prompt even though the current Claude binary doesn't appear to probe them directly.

## Tool-level denies (`wiring/settings-merge.json`)

- `WebFetch` is in `ask` (not `allow`). nono can't gate `WebFetch` because
  it's served by Anthropic's API, not the laptop — this is the primary egress
  barrier to attacker URLs and the most important rule in this list.
- `Edit`/`Write` blocked on `.claude/{settings.json,hooks/**}` (per-project)
  and `~/.claude/{settings*.json,hooks/**,plugins/**,agents/**,skills/**}`
  (user-global). Prevents Claude from tampering with hooks or rewriting its
  own allowlist for future sessions. The nono pack writes these paths via
  the nono CLI, which runs outside Claude's tool surface.
- `Read(**/*.{env,key,secret,credential,pem})` — credential files anywhere
  in the workdir.
- `Read(.git/config)` and `Read(.git/credentials*)` — credentials in remote
  URLs aren't leaked into context.
- `Read(~/.ssh/*)` — defense in depth alongside the profile's `~/.ssh`
  filesystem deny.
- `Bash(ssh:*)` — defense in depth alongside the network-layer port-22 deny.
- `Bash(find:* -exec*)`, `-execdir`, `-delete`, `-ok`, `-okdir`, `-fprint*` —
  `find` only allows read-only traversal forms; `-exec` is otherwise
  effectively shell escape.
- `Bash(git config core.hooksPath:*)` — no redirecting commit hooks to
  attacker-controlled paths.
- `sandbox.enabled: false` — nono is the OS boundary; Claude Code's softer
  in-process sandbox would add friction without restricting an already
  sandboxed session.

## Environment variables

`environment.allow_vars` is a deliberately narrow allowlist (15 entries):
`PATH`, `HOME`, `TERM`, locale (`LANG`, `LC_*`), Claude/Anthropic/nono
internals (`CLAUDE_*`, `CLAUDECODE`, `ANTHROPIC_*`, `NONO_*`), and the
non-credential MCP vars the Phabricator/Gerrit submodules read
(`PHABRICATOR_URL`/`USERNAME`/`CONTACT_*`, `GERRIT_BASE_URL`/`CONFIG_PATH`).

Everything else is dropped before the child sees it: `AWS_*`, `GH_TOKEN`,
`NPM_TOKEN`, `KUBECONFIG`, `GCLOUD_*`, `GIT_*`, `SSH_AUTH_SOCK`, `EDITOR`,
`TMPDIR`, `USER`, language version managers, GUI/X11 vars, etc. Git
identity comes from `~/.gitconfig` (the base profile grants read access).
`PHABRICATOR_API_TOKEN` is intentionally omitted — MCP usage is anonymous.

## Spotlight metadata recon

`com.apple.metadata.mds` is denied at the Mach layer via
`unsafe_macos_seatbelt_rules`. Spotlight indexes filenames and metadata
across the user's home regardless of nono's filesystem denies, so `mdfind`
would otherwise be a recon channel for sensitive paths (e.g. `*.pem` keys).
Verified by `tests/test-profile.sh`.

## Keychain (intentionally not denied)

Keychain Mach services (`com.apple.securityd`, `com.apple.SecurityServer`,
`com.apple.SecurityAgent`) are **not** denied. Claude Code stores its login
token in the login keychain (item: `Claude Code-credentials`) and reads it
via `securityd` on every startup; denying that service makes `/login` and
the persistent-session UI fail (the user sees "Not logged in" on every
launch). The base `claude-code` profile's `filesystem.allow` +
`filesystem.bypass_protection` on `~/Library/Keychains` is specifically for
this.

**Residual risk:** a prompt-injected agent could call
`security find-internet-password -s <wmf-host>` to extract credentials
stored for Wikimedia services. Mitigations are layered, not absolute:

- The network policy only permits egress to Wikimedia and LLM-vendor
  domains, so attacker-controlled exfil endpoints are unreachable.
- The env-var allowlist removes most credential-shaped material from the
  agent's process env.
- WMF engineers on the standard SSH-based git workflow typically don't have
  HTTPS push credentials in the keychain in the first place.

## Other hardening choices

- `capability_elevation: false` — no runtime prompts to escalate.
- `signal_mode: isolated`, `process_info_mode: isolated`,
  `ipc_mode: shared_memory_only` — set explicitly so the posture is visible
  in the profile, not implicit.
- No hardcoded filesystem grants in the profile. `bin/claude` adds `--allow`
  for the bundled MCP submodules; engineers can pass additional
  `--allow`/`--read` flags for other paths.
- `bin/claude` rejects `--capability-elevation`, `--trust-override`, and
  `--dangerously-skip-permissions` to prevent runtime sandbox weakening.

## chrome-devtools MCP — attach mode

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
(both tracked in git). `setup.sh` runs `npm ci --ignore-scripts`, which
reproduces node_modules from the lockfile and refuses any `postinstall`
hook from the package or its deps. The package currently bundles its
runtime dependencies (puppeteer-core, etc.) into the published tarball,
so pinning the one version pins the whole tree. `setup.sh` enforces
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
  trap and leaks the dir; `setup.sh` sweeps stale `wmf-claude-chrome.*`
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

## Open hardening follow-ups

- **Keychain access** is the largest residual exfil surface. Closing it
  cleanly needs either (a) per-binary Seatbelt rules that allow Claude Code
  itself to talk to `securityd` while denying tool-spawned subprocesses
  (`security`, `bash`-spawned children) — brittle and chains badly through
  Claude Code's tool framework, or (b) a credential-proxy mechanism that
  lets nono fetch the OAuth token outside the sandbox and inject it into
  the child without `securityd` access — Claude Code would need to support
  a non-keychain credential source.
- **LaunchServices** (`allow_launch_services: true`, inherited from base)
  is reachable. Could leak app-launch availability or open arbitrary apps.
  Worth narrowing once we know which engineer workflows depend on it.
- **Mach denies** live in the `unsafe_macos_seatbelt_rules` escape hatch.
  Migrate when nono promotes Mach control to a typed capability.
