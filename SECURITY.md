# Security model

**The short version:** Claude Code runs under a kernel-enforced sandbox —
Seatbelt on macOS, Landlock on Linux. It reads and writes the directory you
launched it from, talks to Wikimedia sites and the Claude API, and little else.
SSH keys, shell configs, password managers, and browser profiles are denied, and
SSH push is blocked.

Two layers enforce that, and they are not equally strong:

| Layer | Defined in | Restricts | Enforced by |
|---|---|---|---|
| OS | `profiles/wmf-engineer.json` | Filesystem, network, env vars, signals, Mach services, for the whole process tree | nono → [Seatbelt](https://nono.sh/docs/cli/internals/seatbelt) / [Landlock](https://nono.sh/docs/cli/internals/landlock) |
| Tool | `wiring/settings-merge.json` | What Claude may invoke via `Bash`, `Read`, `Edit`, `Write`, `WebFetch` | Claude Code |

The OS layer is the real boundary. The tool layer is defense in depth, and it
covers one thing the OS layer cannot: `WebFetch` runs on Anthropic's servers,
not your laptop, so nono can't gate it. It is set to `ask`, which makes it the
most important rule in that layer.

`bin/claude` applies the tool layer per launch, passing
`--settings wiring/settings-merge.json` to the sandboxed Claude Code. It is
**not** installed into your `~/.claude/settings.json`, so these rules bind
wmf-claude sessions only and every other Claude Code session on the machine
stays exactly as you configured it. No pack is published today, and `package.json` no longer declares this file
as a pack wiring directive — the pack would otherwise have installed it
machine-wide, which is what per-launch replaced.

## How much we trust each control

| Control | Enforced by | How sure we are |
|---|---|---|
| Filesystem denies, network deny-by-default, env allowlist, SSH blocked, Spotlight blocked | The kernel, via nono | **High.** The process cannot opt out, and `tests/test-profile.sh` asserts the behaviour. |
| Read-only documentation domains | nono's TLS-intercepting proxy | **High** for in-sandbox traffic; does not apply to `WebFetch`, which egresses from Anthropic. |
| `WebFetch` gated, `.claude/` unwritable, credential globs, `find -exec` denied | Claude Code | **Medium.** In-process, not an OS boundary; applied per launch by `bin/claude`, so a session started any other way has none of these. On **Linux the glob rules are ignored entirely**, so workdir secrets are unprotected there. |
| Docker broker refusing unsafe containers | `bin/launch-docker-broker` | **Best-effort.** A denylist, not an allowlist; it can't inspect a container that isn't running, and a container recreated after the check is a TOCTOU gap. |
| Only navigating `--chrome` to authorized URLs | Prompt instructions in the `manual-test` skill | **Not enforced.** Guidance only. The attached Chrome has unrestricted network. |

A control being "High" means the mechanism is sound and tested, not that the
system is unbreakable — see [residual risks](#residual-risks).

## What Claude can reach

| | |
|---|---|
| **Files** | The directory you launched from and everything beneath, read-write. Plus whatever you pass with `--allow` / `--read`. |
| **Network** | Deny-by-default, with these allowed and nothing else: `api.anthropic.com`, `claude.ai`, `platform.claude.com`, `*.claudeusercontent.com`; the wiki family (`*.wikimedia.org`, `*.wikipedia.org`, and ten siblings); `codesearch{,-backend}.wmcloud.org`; `*.local.wmftest.net`; and eight documentation sites allowed **read-only** (`docs.python.org`, `docs.rs`, `doc.rust-lang.org`, `developer.mozilla.org`, `nodejs.org`, `pkg.go.dev`, `php.net`, `vuejs.org`). Enforced by nono's [filtering proxy](https://nono.sh/docs/cli/features/networking). |
| **Localhost** | No static port. On macOS `bin/claude` passes a listen-only `--listen-port` so `/login` can bind its OAuth callback (Seatbelt cannot filter bind by port, so the number is nominal); on Linux nothing is passed, since nono filters bind per port there and the callback uses an OS-assigned port. Real ports are per session: `--local-db`, `--local-web`, `--chrome`. |
| **Env vars** | [16 allowlisted names](https://nono.sh/docs/cli/features/environment) — `PATH`, `HOME`, `TERM`, locale, `CLAUDE_*`/`ANTHROPIC_*`/`NONO_*`, the non-credential MCP vars, and `WMF_DOCKER_*`, which carries the broker's URL and per-session bearer token in when you use `--docker`. Everything else, including `AWS_*`, `GH_TOKEN`, and `SSH_AUTH_SOCK`, is dropped. |
| **Keychain** | Yes — Claude Code reads its login token through it at startup. Closing it was attempted and parked; see [residual risks](#residual-risks). |

## What Claude cannot reach

| | |
|---|---|
| **Parent and sibling directories** | Unless you grant them explicitly. |
| **Credentials** | `~/.ssh`, `~/.git-credentials` (and `~/.config/git/credentials`), `~/.gitcookies` (Gerrit HTTP password), `~/.aws`, `~/.azure`, `~/.config/gcloud`, `~/.gnupg`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.kube/config`, `~/.docker/config.json`, `~/.config/gh`, `~/.config/hub`, `~/.config/glab-cli`, `~/.config/op` (1Password CLI), `~/.vault-token`, `~/.terraform.d`, cargo and gem credentials, the Linux keyring (`~/.local/share/keyrings`), composer auth, `~/.env` — plus any `*.env`, `*.key`, `*.pem`, `*.secret`, `*.credential`, `.git/config`, or `.git-credentials` anywhere on disk. |
| **Shell configs and history** | `~/.bashrc`, `~/.zshrc`, `~/.profile`, fish config — so it can't plant a command that runs next time you open a terminal. |
| **Password managers** | 1Password, Bitwarden, KeePassXC, `pass`, Enpass. |
| **Private comms** | Mail, Messages, Slack, Discord, Signal, Telegram, Thunderbird. |
| **Browser profiles** | Chrome, Firefox, Safari, Edge, Arc, Brave, Chromium, Vivaldi, Opera — cookies and saved passwords included. |
| **iCloud Drive** | `~/Library/Mobile Documents`. |
| **SSH and GitHub** | Port 22 and Gerrit's 29418; `github.com` is not allowlisted. |
| **Spotlight** | `mdfind` is blocked at the Mach layer, so it can't enumerate the filenames the denies above hide. |
| **Its own tooling** | In a normal session the MCP server checkouts are read-only (the repo is granted `--read`); only Gerrit's `server.log` is writable. Two exceptions: `--chrome` grants `chrome-devtools-mcp/` read-write (a pre-existing TODO), and a session launched from *inside* this checkout makes all of them writable through the workdir grant. |
| **Its own config** | `settings.json`, `settings.local.json`, `hooks/**`, `agents/**`, `skills/**`, `commands/**` at **both** scopes (`~/.claude/` and per-project `.claude/`), plus `~/.claude/plugins/**`, `~/.claude.json` (MCP servers and per-project allowed tools — it sits outside `~/.claude/`, so the directory rules miss it), `~/.claude/CLAUDE.md`, and a project `.mcp.json`. Claude Code loads config from all of these, so any one left writable is a way to widen its own permissions next session. `tests/test-templates.sh` asserts the full set. **These are tool-layer denies, not a boundary** — they stop the agent's `Edit`/`Write` tools, but `Bash` can still reach the same files through an interpreter, and the OS layer cannot deny `~/.claude` because Claude Code needs it. Treat them as raising the bar, not closing the hole. |

Two denies worth naming because their reason is not obvious:
`Bash(git config core.hooksPath:*)` stops commit hooks being redirected to an
attacker-controlled path, and `PHABRICATOR_API_TOKEN` is deliberately absent
from the env allowlist — MCP access to Phabricator is anonymous by design.

Profile posture, for completeness: `capability_elevation: false`,
`signal_mode: isolated`, `process_info_mode: isolated`,
`ipc_mode: shared_memory_only`. Beyond the workdir the profile hardcodes three
read grants — `~/.local/state/fnm_multishells`, `~/.local/state/claude/locks`,
and `~/.agents/skills` — and `bin/claude` adds `--read` for the bundled MCP
checkouts plus a single-file write grant on the Gerrit server's `server.log`.

The tool layer also **allows** some commands outright, which auto-approves them
with no prompt: `Bash(git:*)`, `Bash(curl:*)`, `Bash(ls:*)`, `Bash(grep:*)`,
`Bash(rg:*)`, `Bash(jq:*)`, a rebase form, and eight read-only `find` forms.
Two are worth noting: `curl` is auto-approved, which matters when `--local-web`
opens localhost ports, and `git` is auto-approved, which covers an HTTPS push.
It also sets `sandbox.enabled: false`, turning off Claude Code's own in-process
sandbox — deliberate, since nono is already the OS boundary, but it is a second
control switched off.

## Weakening it, knowingly

Nothing below happens on its own. Each is something you choose, and each is
scoped to the session you choose it in unless noted.

| What you run | What you give up |
|---|---|
| `--allow PATH` | Read **and write** on another tree. The most common way to widen the blast radius — scope it as tightly as the task allows. |
| `--read PATH` | Read on another tree. Anything secret in it becomes readable, including by a prompt-injected agent. |
| `--allow-command CMD` | One command the profile blocks by default. |
| `--override-deny PATH` + `--read-file PATH` | Lifts a specific deny. Use for one file, not a directory. |
| `--local-db[=PORT]` | Reach to the dev database — and anything else on that port. Was in the static profile; now only sessions that need it carry it. |
| `--local-web[=PORTS]` | Reach to **any** local service on 80/443/8080, not just your wiki. External egress does not widen. |
| `--chrome` | A Chrome running **outside** the sandbox with unrestricted network, driven over an unauthenticated debug port. Implies `--local-web`. **Throwaway dev-wiki accounts only.** |
| `--docker=SERVICE` without `--egress` | Code execution in a container whose network nono cannot restrict — an outbound channel. The launcher warns and asks for a one-time acknowledgment. |
| `--minimax` | Egress to `api.minimax.io`, a third-party LLM API, for this session. It used to be in the static profile; now only sessions that use MiniMax carry that channel. |
| `\claude` or `command claude` | **The sandbox entirely.** Claude Code runs with your full user privileges, and without the tool layer, which `bin/claude` applies per launch. |
| Answering `y` to the update prompt | Fast-forwards the install to `origin/main` and runs `setup.sh`, i.e. runs code you have seen listed but not read. Declining, or any non-tty launch, changes nothing. |
| `WMF_CLAUDE_PROFILE=<name>` | Swaps in `profiles/<name>.json`, whatever that profile allows. |
| Editing `profiles/wmf-engineer.json` or `wiring/settings-merge.json` | Whatever you change. `bin/claude` prints a warning at launch while either differs from the committed version — expected when you made the edit, a stop-and-look signal when you did not. Re-run `tests/test-profile.sh` afterwards. |
| Registering another MCP server | A tool surface neither layer was designed around. MCP tools are not covered by the `Bash`/`Read`/`Edit` deny rules. |

`bin/claude` **refuses** `--capability-elevation`, `--trust-override`, and
`--dangerously-skip-permissions`. There is no supported way to pass them.

### chrome-devtools MCP

Chrome can't run inside the sandbox — IOKit is denied and it segfaults during
init — so `bin/launch-test-chrome` starts it outside with a fresh throwaway
profile and the debug port pinned to `127.0.0.1`. The MCP server itself stays
sandboxed. [Full analysis](docs/security-rationale.md#chrome-devtools-mcp).

### Docker exec broker

The Docker socket is root on the host, so it never enters the sandbox. The
broker holds it outside and exposes a token-authenticated localhost endpoint;
the in-sandbox `mwdocker` shim is the only client. It builds argv as a list (no
shell), pins the docker flags so the caller picks only an allowlisted binary
(`composer`, `php`, `npm`, `vendor/bin/{phpunit,phpcs,phpcbf,phan}`) and its
arguments, and refuses containers that are a path to host root — privileged,
host-root caps, device passthrough, host PID/IPC namespace, or a bind of the
socket, a host-root path, or your `$HOME` credentials.

That allowlist is **not** an RCE boundary: `composer` and `npm` run scripts from
the checkout and `php` runs arbitrary code. The host is the boundary.
[Full analysis](docs/security-rationale.md#docker-exec-broker).

## Residual risks

What the sandbox does *not* protect against, even with no flags.

| Risk | What bounds it |
|---|---|
| **Keychain is reachable** — a prompt-injected agent could read other stored credentials | Egress allowlist and env allowlist; most engineers store no push credential there. Closing it needs a non-keychain login path; nono's OAuth capture was tried and parked — [findings](docs/security-rationale.md#keychain-access). |
| **HTTPS push to a Wikimedia host could still succeed** with a keychain-stored Gerrit HTTP password | SSH push is fully blocked, `~/.git-credentials` and `~/.gitcookies` are denied at the OS layer, and `git credential*` is denied at the tool layer (it would otherwise be auto-approved under `Bash(git:*)` and return the stored password). Most engineers have no HTTP password. |
| **On Linux, nothing protects secrets in your workdir** | Neither layer covers it. Don't keep them there. [Detail](docs/security-rationale.md#linux-glob-caveat) |
| **Reading a shared artifact is a prompt-injection surface** | The `Artifact` tool returns an isolated summary, treated as data — same trust class as reading a wiki page. [Detail](docs/security-rationale.md#artifact-content-host) |
| **LaunchServices is reachable** (inherited from the base profile) | Not yet narrowed — see below |

## Verifying

```bash
./tests/test-profile.sh     # sandbox behaviour (cannot run inside a sandbox)
./tests/test-templates.sh   # manifest, frontmatter, permission-rule shapes
```

This file describes intent. To audit what nono actually enforces — including
everything inherited from the base `claude-code` profile, which this file does
not repeat — ask nono directly:

```bash
nono profile show ./profiles/wmf-engineer.json --format manifest  # effective rules
nono profile diff claude-code ./profiles/wmf-engineer.json        # our delta vs. the base
nono audit list && nono audit show <session>                      # what a session did
```

The tool layer is applied per launch, so "is it actually live?" is worth
checking directly. This should be refused, not run:

```bash
# Denied — the layer is in effect.
command claude -p --permission-mode manual \
  --settings ./wiring/settings-merge.json \
  'Run exactly this bash command: ssh -V'

# Runs and prints the OpenSSH version — the control case.
command claude -p --permission-mode manual \
  --settings '{"permissions":{"allow":["Bash(ssh -V)"]}}' \
  'Run exactly this bash command: ssh -V'
```

Two things make this meaningful. `command claude`, not `claude`: the alias is
`bin/claude`, which injects its own `--settings`. And the control carries an
explicit allow: under `-p` an un-allowlisted command is not run but held for
approval, so without it both halves would print a refusal and prove nothing.
The first half must say *denied*; the second must actually print a version.

## Open follow-ups

- **Keychain access** — largest residual; a closure attempt with nono's OAuth capture is recorded in the [rationale](docs/security-rationale.md#keychain-access) and needs upstream input before another try.
- **LaunchServices** — narrow once we know which workflows depend on it.
- **Mach denies** still live in the `unsafe_macos_seatbelt_rules` escape hatch;
  migrate when nono promotes Mach control to a typed capability.

## Reference

Why each choice was made: [`docs/security-rationale.md`](docs/security-rationale.md).

nono's own docs: [security model](https://nono.sh/docs/cli/internals/security-model),
[networking](https://nono.sh/docs/cli/features/networking),
[environment](https://nono.sh/docs/cli/features/environment),
[profile authoring](https://nono.sh/docs/cli/features/profile-authoring),
[introspection](https://nono.sh/docs/cli/features/profile-introspection),
[audit](https://nono.sh/docs/cli/features/audit),
[trust](https://nono.sh/docs/cli/features/trust),
[flags](https://nono.sh/docs/cli/usage/flags).
