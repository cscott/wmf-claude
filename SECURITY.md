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
- `network.open_port: [3306]` — local MariaDB connections allowed.
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
