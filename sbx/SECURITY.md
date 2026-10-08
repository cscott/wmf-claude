# Security model — the `sbx` backend

Threat model for running `wmf-claude`'s content layer inside Docker
Sandboxes (`sbx`), via `sbx/bin/wmf-sbx-create` and its generated kit.

> **Not the supported configuration.** This is local-dev work in progress,
> owned by cananian, not covered by `wmf-claude` CI, and not shipped by the
> signed nono pack. The root `SECURITY.md` describes the backend that
> ships today. Read that one for what WMF engineers actually run; read this
> one only if you are running `sbx/bin/wmf-sbx-create`.

Written against **sbx 0.39.0**, with the 0.42.1 deltas marked. Every claim
labelled MEASURED was checked on a real sandbox; the `sbx/NOTES.md` section
in brackets has the transcript. Claims with no such label are design
intent, and the difference matters.

## The short version

Against nono, `sbx` is a much stronger *isolation* boundary and a much
weaker *egress* boundary.

| | nono (`profiles/wmf-engineer.json`) | sbx |
|---|---|---|
| Filesystem | the host's, minus a deny list | the VM's own, plus named mounts |
| Root | unusable (`PR_SET_NO_NEW_PRIVS`) | passwordless `sudo`, inside the VM |
| Escape lands | your uid, your kernel | a guest kernel, behind a hypervisor |
| Docker | host socket, via a broker | the sandbox's own engine |
| Network | deny by default, ~20 domains | **allow by default, hundreds of domains** |
| Method/path rules | 8 doc hosts, GET/HEAD only | none — hostname allowlist only |
| MCP servers | in the sandbox, on the host's FS | on the host, behind a gateway — see §7 |
| macOS keychain | reachable (documented residual risk) | not reachable |
| SSH keys | `~/.ssh` denied | **the live host agent, forwarded** — see below |

The last two rows are the ones to read twice.

## 1. Network: the allowlist widens, it does not restrict

MEASURED [§24]. `sbx policy ls NAME --type network --wide` on a real
sandbox lists six rules. Five are sbx's own shipped local defaults, applying
to *all* sandboxes: `default-ai-services`, `default-package-managers` (~60
entries), `default-code-and-containers` (`github.com`, `**.gitlab.com`,
`bitbucket.org`, the container registries), `default-cloud-infrastructure`
(`**.amazonaws.com`, `**.googleapis.com`, `unpkg.com`, `jsdelivr.net`, …),
and `default-os-packages`. The sixth is our kit's.

So:

- **The kit's `allowedDomains` is an addition to a large permissive base,
  not a restriction.** Any mental model that reads the kit's wiki-family
  list as "the network policy" is wrong by about two orders of magnitude.
- **Exfiltration egress is open.** A prompt-injected agent can POST to
  `github.com`, to an S3 bucket, or to several AI vendors' APIs. Under nono
  the equivalent attempt fails at the proxy. This is the single largest
  regression versus the nono profile, and it is not fixable in the kit —
  `allowedDomains` can only widen. **The lever is `--deny-network`**, which
  narrows, and we do not use it yet.
- **No method or path granularity.** Egress leaves through an HTTP
  `CONNECT` proxy that enforces on hostname at connect time (MEASURED: the
  `HTTP/1.0 200 Connection established` preamble, §24). The nono profile's
  read-only rules for the eight documentation hosts have no equivalent
  here, and nothing intercepts TLS — which also means none of nono's
  TLS-interception costs apply.
- **The Wikimedia hosts nono keeps read-only are fully open here.** Since
  nono 0.78, `profiles/wmf-engineer.json` scopes the wiki families and most
  named `*.wikimedia.org` hosts to GET/HEAD, and it has no
  `*.wikimedia.org` wildcard. `kit.wiki_family_domains()` takes every
  wiki-family wildcard and every named `*.wikimedia.org` and
  `*.wmfusercontent.org` host from that list, plain or endpoint-scoped,
  and the kit allows each host whole: a sandbox can POST to
  `en.wikipedia.org` or `phabricator.wikimedia.org`. The old wildcard
  allowed the same, and more. A chapter wiki or other `*.wikimedia.org`
  host that the profile does not name is no longer in the kit's list
  (sbx's own local defaults were not checked for it).
- The default rows are `source: local`, i.e. per-machine. Another
  engineer's, or an org policy's, may differ. Kit-declaring the registries
  we actually depend on is still worth doing as documentation; it can never
  widen past the local and org layers.

**0.42.0 fixes an egress bypass** where a sandbox could reach a disallowed
domain through a CDN by presenting a different TLS SNI inside a CONNECT
tunnel. That alone is a reason to be on 0.42.

**Re-verified on 0.43.0-rc3** (`NOTES.md` §84.3): same five default rules,
same shape. The one change is in our *own* sandbox-scoped rule — sbx now
merges Claude-agent-specific entries (`code.claude.com`,
`mcp-proxy.anthropic.com`, `bridge.claudeusercontent.com`) into it
alongside what the kit itself declared, so that rule's contents are no
longer only what we write. Does not change the conclusion above; if
anything it is one more source of widening this document did not
previously account for.

## 2. SSH agent forwarding — the compliance problem

MEASURED [§5, §8], and the most serious finding in this file.

`sbx` forwards the host's SSH agent into **every** sandbox whenever
`SSH_AUTH_SOCK` is set in the invoking shell, with no per-invocation
opt-out (`docker/sbx-releases#305`, dups #115 and #121). Inside such a
sandbox, `ssh-add -l` lists the engineer's real keys and
`ssh -T git@github.com` authenticates as them. `gerrit.wikimedia.org:29418`
completes a TCP connection and an SSH handshake — the network path to
Gerrit's SSH port is open; only key material stopped the push. The root
`README.md`'s "SSH push is unreachable, push from a normal shell" is
therefore only half true under sbx: it is an authentication gap, not a
network one, and forwarding closes it.

Worse, the forwarding is sticky to the sbx **daemon**, and any subcommand
can start that daemon implicitly. Stripping the variable around
`sbx create` alone is not sufficient once a daemon has ever been started
with it set.

**Mitigation:** `sbx/bin/wmf-sbx` is the only supported way to invoke
`sbx` in this project. It `unset`s `SSH_AUTH_SOCK` and execs the real
binary. Add `alias sbx=wmf-sbx` so a bare `sbx` typed out of habit still
comes through it. Re-verified after a full session driven only through the
wrapper: `SSH_AUTH_SOCK` unset, `ssh-add -l` fails, the daemon was not
retainted (§8, 2026-09-05).

**0.42.0 makes this configurable, and the setting is now named.**
MEASURED [§78]: `sbx settings list --json` carries

```
ssh.agentForwardingEnabled  bool    default true   requires_restart
ssh.agentSocketPath         string  default ""     requires_restart
```

`ssh.agentForwardingEnabled` is the second layer this section has been
waiting on. **Set to `false` on this host** 2026-09-14 (cananian):

```console
$ wmf-sbx settings set ssh.agentForwardingEnabled false
Setting "ssh.agentForwardingEnabled" updated: value=false source=override
This setting takes effect only after a daemon restart: run `sbx daemon restart`.
```

Setting it costs nothing we use — nothing in this project forwards an
agent on purpose — and `requires_restart` cuts our way for once: the
stickiness §2 complains about means a daemon started by anything *else*
on the machine picks the setting up too, which is the case the wrapper
cannot cover.

> **Do not run `sbx daemon restart` reflexively.** It kills every running
> sandbox without letting it save state — every live Claude session on
> the machine, not just yours in this repo [§8, and `sbx/bin/wmf-sbx`'s
> header]. The sandboxes themselves survive as `stopped` and come back
> with `wmf-sbx-start` / `wmf-sbx-resume`, but in-session conversation
> state does not, and §4 lists what else a restart discards. Schedule it;
> do not fire it off because a settings command suggested it.

**And here it was not needed at all**, which is worth stating because the
tool's own message implies otherwise. The running daemon was started
through `wmf-sbx`, so `SSH_AUTH_SOCK` was already absent from its
environment and it has no agent to forward — the restart would only be
re-establishing a property it already has. Meanwhile any daemon started
*after* the setting change drops forwarding regardless of how it is
launched. Both the current daemon and every future one are covered, by
different mechanisms, with no restart in between. The restart is only
required when the running daemon is already tainted.

The wrapper stays regardless. A config file the agent could reach is not
a boundary; a second layer that a `settings set` can silently undo is not
a boundary either. Two independent mechanisms, neither sufficient alone —
and as of this change the daemon-level one is finally in place.

### 2.1 The one other place this host is not at stock

`ssh.agentForwardingEnabled` is now an override, and so is exactly one
other key. MEASURED [§78.2] — of the 29 keys `sbx settings list --json`
reports, these two are the only ones whose `"source"` is not `"default"`:

```json
{"key": "claude.remoteControl", "source": "override", "value": true,
 "default": false,
 "description": "Allow Claude Code's /remote-control channel to
   authenticate with its own session token instead of the host
   credential."}
```

**Set deliberately by cananian** (confirmed 2026-09-14), to use Remote
Control — following a session from another device. It is recorded here
because this document describes the host's posture and should not be
silently wrong about the places that posture departs from stock, not
because it is a finding.

Which way the trade runs, plainly: it *narrows* what the channel
authenticates as, from the host credential to the session's own token,
and it *widens* the reach of a running session to anyone holding that
token. Nothing in this project depends on it, in either direction —
no `wmf-sbx` command uses Remote Control, and turning it off would change
nothing described anywhere else in this file.

## 3. Read-only mounts: two layers, only one of them holds

MEASURED [§22], and confirmed by attempting the bypass rather than reading
`/proc/mounts`.

The sandboxed agent has passwordless `sudo`. A read-only mount made *inside*
the sandbox is therefore not a boundary against the agent: `sudo mount -o
remount,rw` lifts it and writes reach the host's real files. A mount `sbx`
created read-only is backed read-only a layer below the namespace, where the
sandbox cannot reach — the same remount reports success, `/proc/mounts` even
says `rw`, and writes still fail. See also `docker/sbx-releases#556`.

Consequences, both load-bearing:

- **Every extra workspace is passed `:ro` at the `sbx create` layer**,
  whether or not the user typed it. Nothing should ever write to a
  host-mirrored original. A user's typed `:ro` survives only as the
  parallel-tree strategy switch (bind mount instead of writable clone).
- **The primary workspace cannot be read-only.** `sbx create` rejects
  `:ro` on the primary positional outright ("primary workspace must be
  read/write"). Its host directory is therefore writable from inside the
  sandbox, by design, and the in-sandbox remount is the only thing standing
  in front of it — an accident guard, not a boundary. Treat the primary as
  a directory the agent can write.

  **Two routes to closing this, one asked for and one shelved.** MEASURED
  on the host 2026-09-14 [§82.3]: a workspace list with *no* read/write
  member is rejected exactly as a single `:ro` primary is, so the rule is
  positional — the first path is the primary and the primary is
  read/write. It is not "the set must contain one writable member".

  - **The route being taken: upstream accepts `:ro` on the first
    positional.** **Asked for, 2026-09-14** —
    [`docker/sbx-releases#586`](https://github.com/docker/sbx-releases/issues/586),
    filed by cananian [§82.6, §82.7]. If it lands, the real repo stays at
    position 0, the agent still wakes up in its own project path, and
    nothing else in our layout moves. Most of the machinery is already
    upstream — `:ro` extras have been enforced below the namespace since
    0.39.0 (#556), and 0.42.0 can already create a sandbox with no
    workspace bind mount at all. **Nothing is closed until it ships**,
    and a feature request is not a mitigation.
  - **The route shelved: a throwaway empty directory as the primary**,
    with the real repo passed as a `:ro` extra like every other repo
    [§82.3]. It works on today's binary and closes the gap completely,
    and it was deferred 2026-09-14 [§82.5] because the agent's working
    directory is load-bearing rather than cosmetic: the agent cannot
    `/cd`, every restart resets cwd to the primary workspace, §39's mount
    dance exists precisely to keep cwd equal to the real project path,
    and Claude Code keys its per-project state and memory directory off
    that path. Mounting the clone over the scratch path fixes the
    contents but not the name. Reopen if the request above is declined.

  Until one of them lands, everything above stands: treat the primary's
  host directory as writable by the agent.

## 4. What a restart leaves behind

MEASURED [§40, §41, §46]. This is the subtlest finding in the project.

Everything `wmf-sbx-setup` mounts is mount-namespace state, and docker
rebuilds the namespace from the container config on every start. So a
`sbx stop` followed by any start discards:

- the writable clone's visibility at the host repo's path, and — worse —
  the `--shared` alternates it borrows objects through, which point into
  `.sbx-originals`; that directory is empty again after a restart, so the
  clone loses its objects (MEASURED: 6707 reachable commits before the
  stop, 5 after);
- **every read-only remount**, so a host repo sbx itself mounted
  read/write (the primary, always) comes back writable from inside the
  sandbox. That hole predates the alias layout and was simply never
  noticed until §40 went looking.

The kit registers a `wmf-sbx-setup --restore` startup command that puts the
layout back, and it **does** run on every container start — including a
start triggered by a bare `sbx exec` (MEASURED, §46: the dispatcher log
names the command and carries its per-repo narration on all five starts of
that session). What it does not do is finish before the `sbx exec` that
woke the container returns. Our entry runs fourth, after the claude kit's
three, one of which shells out to the MCP gateway.

**So the residual window is a few seconds at the start of a container's
life, not an indefinite exposure** — but during it the mirrors are
writable and the clones' objects are unreachable, and nothing on the plain
`sbx run --name` / `sbx exec` paths waits for it or says so.
`wmf-sbx-resume` waits (`wmf-sbx-setup --verify --wait=20`, polling inside
the sandbox) and falls back to re-running the restore itself. That waiting
is the whole of its security advantage; use it.

Anyone can check a live sandbox:

```bash
wmf-sbx exec NAME -- python3 /home/agent/wmf-sbx-setup --verify
```

It reads `layout.json` and asserts the alias (`samefile`), the alternates,
and the mount options out of `/proc/self/mountinfo` — so a mirror that came
back `rw` is a reported error rather than something a human has to remember
to test for. `--wait SECONDS` polls instead of answering once.

**0.43 makes this window routine rather than rare.** Sandboxes now stop
themselves automatically when idle (§9.1), and MEASURED (`NOTES.md` §84.6)
the idle timeout is on the order of 36–37 seconds — merely listing or
polling a sandbox's state from outside does not reset that clock, only
real exec/session activity does. So the restart this section describes,
and the few-second writable window that comes with it, now happens on
roughly a half-minute-of-inactivity cadence instead of only when someone
types `stop`. **Still to measure**: whether `wmf-sbx-resume --verify
--wait=20` reliably outruns this on the resume side, not only the restore
side — that timing has not been taken yet (`NOTES.md` §84.6, "Still
open").

## 5. The parallel tree and its git daemon

The agent works in a private clone of each repo, mounted over the host
repo's own path (`sbx/NOTES.md` §39). The host reads the agent's commits by
fetching from a `git daemon` inside the sandbox.

- **The daemon is read-only.** No `--enable=receive-pack`, so nothing
  pushes *in*. The host fetches; the sandbox never writes to a host repo
  through it.
- It runs as the agent (uid 1000), not root. Running it as root caused a
  live "dubious ownership" incident (§21) and would have made every
  `upload-pack` child root's.
- `--base-path=/home/agent --export-all` means it serves every git repo
  under the agent's home. Inside a single-user VM that is the intent, but
  note what it implies: any repo the agent creates under `$HOME` is
  fetchable by anything that can reach the port. `git daemon`'s
  `--base-path` request-path sanitization is the only thing scoping
  requests to that subtree, and we have **not** verified it independently
  (`sbx/DESIGN-parallel-clone-tree.md` §6, still open).
- `--listen=0.0.0.0` is inside the VM. On the host the port is published to
  **loopback only** and to an ephemeral port that moves on every container
  start (MEASURED, §34.2 — 32783 before a stop, 32784 after), which is what
  `wmf-sbx-resume`'s remote re-pointing is for. It is unauthenticated:
  anything running as the engineer can fetch from it, which is the same
  trust boundary the rest of the local tooling already assumes.
- **0.42.0 changes the publish default to `tcp4`**, so the port no longer
  listens on `::1` unless asked. Our URLs name `127.0.0.1` literally and
  the port lookup accepts both spellings (`wmf_sbx_create.ipv4_mapping`).

## 6. Cloud sandboxes are refused

`sbx --cloud` (new in 0.42.0) runs the sandbox on Docker-managed
infrastructure. Every assumption in this document stops holding there: the
mounts are local paths, the git daemon is reached over loopback, and the
boundary being reasoned about is a VM on the engineer's own machine. It
would also upload a WMF checkout, and whatever unpushed work is in it, to a
third party's compute.

`sbx/bin/wmf-sbx` refuses `--cloud` unless `WMF_SBX_ALLOW_CLOUD=1` is set —
a deliberate decision, not a flag typed in the wrong terminal. The escape
hatch still routes through the wrapper, so `SSH_AUTH_SOCK` stays stripped.

## 7. MCP: a host process by design, five tools we did not ask for, and a shared skills store

Two separate things here. One is a boundary we cross deliberately; the
other is a default we close.

### 7.1 The servers run on the host, and that is the lesser risk

`DESIGN-plugin-integration.md` §4 settles the MCP integration on Route 1:
`mcp-phabricator` and `gerrit-mcp-server` are registered with
`sbx mcp add` on the **host** and reached from the sandbox through sbx's
MCP gateway at `http://mcp-gateway.docker.internal/mcp`. So for the first
time this project runs something outside the boundary on the agent's
behalf. Docker's own warning, from `sbx mcp add --help`, verbatim:

> Local servers are for ad-hoc development only. They have no identity, no
> verifiable supply chain, and no sandboxing. The process runs with your
> host user's full permissions.

Taken at face value: a compromised or hostile MCP server here is a host
compromise, not a sandbox one, and the supply chain is whatever the two
submodules pull in. What makes the trade worth it is the alternative —
running those servers *inside* the sandbox means their credentials are
inside the sandbox too, where the agent can read them
(`PHABRICATOR_API_TOKEN` is a full-account Conduit token;
`gerrit-mcp-server` takes an `auth_token` or `~/.gitcookies`, which are
push credentials for all of Gerrit). A host process the agent can only
poke through a JSON-RPC allowlist is a smaller problem than a token the
agent can `cat`. MEASURED [§60.1].

Two mitigations, both cheap:

- `sbx/helpers/wmf-sbx-mcp-proxy` forwards only the tools it advertises. Its
  `--tools` allowlist is what the skills actually call, and a `tools/call`
  for anything else is refused in the sandbox, before it reaches the
  gateway. It is a naming shim first (§60.4) but it is the enforcement
  point too — and the allowlist **cannot be omitted**: `--tools` is a
  required argument, because the gateway serves every mounted server's
  tools in one flat namespace with no attribution, so there is no safe
  default [§62.2]. With no allowlist the proxy serves nothing.
- That allowlist is where the kit decides which *operations* are on
  offer, not just which names. `gerrit-mcp-server` exposes 20 tools, of
  which 15 write to Gerrit under the engineer's own credential —
  `abandon_change`, `post_review_comment`, `create_change`,
  `revert_submission`, `set_ready_for_review`, … The plugin names five,
  all read-only, and those five are the allowlist; the ability to abandon
  someone's change never reaches the model [§62.1, §62.5].
- The host registration names an absolute interpreter and script path.
  It is not `npx`, and nothing in the sandbox chooses what runs.

What this does **not** protect against: the host-side server is still a
process started on the engineer's behalf by `sbx`, with their permissions,
talking to the network. Treat the two submodules as code you audit, not as
a sandboxed dependency.

### 7.2 The gateway's meta-tools are an unprompted capability

MEASURED [§57.2, §60.4]. By default a sandbox's Claude Code sees the
gateway as one server offering five *meta*-tools, and two of them are
worth stopping to look at:

- **`mcp-add`** — "Add an MCP server to this session … its tools become
  available immediately." The agent can mount any server the host has
  registered, and make the gateway launch it, **on its own initiative**;
  the tools then appear without an agent restart.
- **`code-mode`** — "Create a sandboxed JavaScript execution tool." An
  arbitrary-JS path that is not `Bash` and therefore not subject to any
  `Bash(...)` deny rule.

Nothing in `wmf-claude` asked for either. Inside the VM the blast radius
argument of §7 applies, but `mcp-add` reaches *host* processes, which is
exactly the boundary §7.1 is trying to keep narrow.

There is a **second** reason to close this, found by accident [§61.4]:
when `wmf-sbx-mcp-proxy` calls `mcp-add`, the server is mounted into the
*sandbox-wide* gateway session, not into the proxy's private view. So the
gateway entry immediately offers `mcp__mcp-gateway__phabricator_get_task`
beside the proxy's `mcp__phabricator__phabricator_get_task` — two
differently-named routes to one host process, and the proxy's `--tools`
allowlist governs only one of them. Left open, the allowlist in §7.1 is
decorative.

Two mechanisms close it, and we use both.

**Deny the gateway server wholesale** — one line in the ported deny list,
measured to work [§60.4, §61.2]:

```json
"permissions": { "deny": ["mcp__mcp-gateway"] }
```

The whole-server form removes every one of that server's tools from the
model's view — under `bypassPermissions` — while the gateway itself still
reports `connected`, and it does not affect `wmf-sbx-mcp-proxy`, whose
HTTP calls are not Claude tool calls. Measured with a real host-side
server mounted: the session offered exactly the two proxy-named tools the
allowlist permitted and no `mcp__mcp-gateway__*` at all.

**Create with `--static-mcp`** [§61.3]. A static-mode gateway serves only
`code-mode` and `mcp-exec`; `mcp-add`, `mcp-find` and `mcp-config-set` are
**not there to call**. That is a capability removed rather than hidden, so
it holds even if the deny is misspelled or a future settings merge drops
it. It does not replace the deny — `code-mode` survives static mode — and
it does not change the tool names (a static-mode sandbox's
`~/.claude.json` still holds one `mcp-gateway` entry), so the proxy is
needed either way.

Note the shape of the finding, because it generalises: a deny list that
only names `Bash`, `Read` and `Write` patterns is incomplete on a backend
that ships its own tools.

### 7.3 The deny list now actually reaches the sandbox — and how it can fail to

DONE 2026-09-12 [§64]. Until step 3, `wiring/settings-merge.json` was
wiring the *nono pack* applies on the host; a generated sbx sandbox got
none of it. It now ships in the kit as data
(`files/home/.claude/wmf-sbx-settings.json`) and
`wmf-sbx-setup --settings` merges it into `~/.claude/settings.json` — at
install and again on every container start, because a startup command
does not block the `sbx exec` that triggered it (§4's shape, again).

Only `permissions.deny` is ported, plus `mcp__mcp-gateway`. `allow` and
`ask` are redundant under sbx's `bypassPermissions`, and `sandbox:
{enabled: false}` refers to nono's in-process sandbox, not this one. Deny
rules **are** honoured in bypass mode — measured, twice: once in a
purpose-built sandbox, and once by watching the gateway's 29 tools vanish
from a live session the moment the merge landed [§64].

Three ways this layer can be weaker than it looks, in descending order of
likelihood:

- **A `settings.json` that is unreadable or not a JSON object is left
  alone**, and the merge reports the failure to stderr and the setup log
  rather than failing the start. That is the right call — overwriting
  would throw away `defaultMode` and whatever the engineer added — but it
  means "the denies are present" is something `report_setup_problems`
  tells you, not something the design guarantees. The plugin-loaded
  startup check does not cover it.
- **The agent can edit the file.** `Edit(~/.claude/settings.json)`
  and `~/.claude/plugins/**` are themselves in the deny list, which is
  the usual self-referential limit: the deny holds against the *tools*,
  and the agent has passwordless root and `Bash` in a VM where `python3 -c`
  can write any file. This layer is defense in depth against mistakes and
  against a prompt-injected model reaching for a plausible tool, never a
  containment boundary. §8 is the containment boundary.
- **A single-slash absolute pattern matches nothing.** Claude Code wants
  `//` for a filesystem-absolute path. Nothing in the current list is
  absolute; anything added later must be, or it is a rule that reads as
  protection and is not. `kit.settings_patch`'s test asserts this.
- **A rule Claude Code rejects is skipped, not enforced** — the same
  failure with a louder warning. Six `Bash(find:* -exec*)`-style denies
  shipped for months in exactly that state (§70). `wmf_sbx.kit`'s tests
  now assert the two spellings that get a rule dropped (`:*` anywhere but
  the end, and a `Write(path)` that Claude Code ignores in favour of
  `Edit(path)`), but the authority is `claude --settings FILE doctor`,
  which lists every rule the running Claude Code rejected and needs no
  API call. Run it against a generated patch when changing the list.

The delivery itself adds no new exposure: the plugin tree is copied out
of the host checkout at kit-generation time, so a sandbox fetches nothing
over the network for it and no GitLab token is involved (§55's original
concern, answered by not needing an answer).

### 7.4 §7.1 and §7.2 are now shipped, and measured on a host

DONE 2026-09-12 [§65]. Both of the mitigations §7.1 describes and both
of the mechanisms §7.2 names are code:

- The kit ships `wmf-sbx-mcp-proxy` in `files/home/.local/bin/` and one
  `claude mcp add --scope user` per server, each with its `--tools`
  allowlist and `--no-add`, applied by `wmf-sbx-setup --mcp`.
- `wmf-sbx-create` registers the host-side servers before it builds the
  kit and passes `--static-mcp <those servers>` to `sbx create`.
- The `mcp__mcp-gateway` deny rides in the settings merge (§7.3).

The allowlist was checked at the point that matters — a `tools/call` for
`abandon_change` through the phabricator proxy is refused in the sandbox,
even though the gateway does serve that tool for gerrit — and a permitted
call reaches the host process and comes back [§65.2].

The host side — `sbx mcp ls` parsing, the `--command env` registration,
and `--static-mcp` itself — could not be exercised from inside a sandbox,
and was a code reading rather than an observation when this section was
written. It has since been run on a host four times: the MCP store and
its `--json` shape [§67], the registration and both node paths [§69],
Route 1 end to end from a kit-built sandbox [§72–73], and the username
divergence check [§75.5]. The fail-closed paths are still partly a code
reading — an unbuilt submodule, a host node below 20.18.1, an unreadable
MCP store and a failed `sbx mcp add` each drop that server so the sandbox
comes up without those tools, and only the node floor has actually been
tripped in anger [§68, §69.1]. One gap remains in what has been
*observed*: no tool call has gone through a **generated** kit's gerrit
entry. The phabricator half has [§73]; gerrit's end-to-end run [§62.5]
used a hand-registered proxy entry.

One deliberate non-action, with one exception. An MCP server already
registered on the host is left exactly as it is, never re-registered: it
may be the engineer's own, and nothing distinguishes it from ours. The
cost is that a stale registration (say, one pinned to a node that no
longer exists) stays stale, and `sbx mcp inspect <name>` is how you see
that. The exception is a *read*, added 2026-09-14 [§75]: a `phabricator`
registration whose `PHABRICATOR_USERNAME` disagrees with the one this
host would register today stops the create and says so, naming
`wmf-sbx mcp rm phabricator` rather than running it. Removing a
registration mid-session takes the Phabricator tools away from every
sandbox holding it open, so that call is the engineer's. It stops; it
does not fix.

### 7.5 The credential is not in the sandbox — but anything in the sandbox can spend it

The residual risk of proxy-managed secrets, stated plainly, because
"credentials never enter the sandbox" is true and is not the whole story.

The sandbox holds a sentinel, not a token: `MCP_SENTINEL_TOKEN_NAME=
proxy-managed`, sent as `Bearer proxy-managed`. The sandbox HTTP proxy
swaps it for the real credential on the way out. So the value cannot be
read, copied into a file, printed into a transcript, or exfiltrated by
anything running in here — which is the property §7.1 was bought for.

What *can* happen is that any process in the sandbox able to make an HTTP
request through the proxy gets the credential spent on its behalf. The
injection point authenticates the sandbox, not the caller inside it. A
compromised dependency in a test run reaches the same gateway the model
does, with the same authority.

Two things narrow it, and neither is an accident:

- **The allowlist is enforced in the sandbox, per server** — gerrit's 15
  write tools are refused before the request is made [§65.2], so the
  authority on offer is read-only even to a caller that bypasses Claude
  Code entirely and speaks to the proxy directly.
- **The gateway is reachable only through the HTTP proxy.** MEASURED
  [§73.3]: `mcp-gateway.docker.internal` does not resolve inside *any*
  sandbox, attached or not, and is not in `$no_proxy`. A client that
  bypasses the proxy to avoid the injection point loses DNS in the same
  move — it gets `[Errno -5] No address associated with hostname`, not an
  unauthenticated connection. DNS and credential fail together.

So the boundary holds where it was drawn: the secret is unreadable, and
the capability it grants is allowlisted and read-only. The honest residual
is that *capability* is sandbox-wide rather than model-scoped, and nothing
in the current design changes that.

### 7.5.1 Two settings §7.5 depends on, both at their defaults

`no_proxy.sandbox` and `proxy.sandbox` are both empty on this host
[§78.4]. They are the preconditions of everything above: an entry in
`no_proxy.sandbox` is an exception to credential injection for those
hosts, and the "DNS and credential fail together" property only holds
while the gateway's hostname is not excepted. Check them before trusting
this section on a machine that is not this one.

`wmf-sbx-create` now checks them itself: `wmf_sbx/settings.py`'s
preflight (see NOTES.md's "Add settings checks to `wmf-sbx-create`",
**DONE** 2026-09-14) hard-fails the create if either is non-empty, so
this is enforced on every host that runs it, not just documented here.

### 7.6 `~/.claude/skills` is shared by every sandbox on the host

Not an MCP issue; it sits here because it is the cross-sandbox boundary
the MCP design work went looking for and then talked itself out of.

`sbx` symlinks one host directory —
`~/.local/state/sandboxes/sandboxes/agent-skills` — into **every**
sandbox's `~/.claude/skills`, mounted `rw` [§55.1:
`none /home/agent/.claude/skills virtiofs rw,nosuid,nodev`]. It is not
per-sandbox and it is not container state: it survives `sbx rm` of any
sandbox that touched it, like everything else in §4.

We ship the plugin as a per-kit copy under `~/.claude/plugins/` rather
than through this store [§64], so nothing of ours is in it and it is
empty on this host. **That is not the same as it not being there** — an
earlier draft of this document reasoned from the first to the second and
was wrong [§76, corrected by §77.2].

The exposure, if the store is writable from inside a sandbox: a skill is
instructions to a model. An agent that can create a file there is writing
instructions that every other sandbox on the host loads at its next
start — including sandboxes working on unrelated repositories, and
sandboxes created later. That is a write channel out of a compromised
sandbox into a clean one, and unlike §5's git daemon it needs no network
and no cooperation from the other side.

**It is writable.** MEASURED [§77.3], by attempting the write rather than
reading the mode:

```console
agent@gerrit-probe:~$ touch ~/.claude/skills/.probe && echo WRITABLE
WRITABLE
```

The tool-layer deny for it already ships, and did before anyone
considered this threat: `Edit(~/.claude/skills/**)` has been in
`wiring/settings-merge.json` since the nono pack, and `settings_patch()`
ports the whole deny list into every generated kit [§7.3, and §70.2 for
why the rule says `Edit(` rather than `Write(`]. Keep it — it is a real
guard against the model being talked into the write by a prompt
injection, which is the likelier failure here.

**Do not mistake it for the mitigation.** It does not cover
`bash -c 'echo … > ~/.claude/skills/evil.md'`; a Bash deny is a prefix
match on a command string and a shell can spell that write a hundred
ways. More fundamentally, the agent is passwordless root in its own
sandbox (§8), so every lever we hold — the deny, file modes, a mount
option set from inside — is on the wrong side of the boundary we are
trying to draw. Against a compromised agent, none of it holds.

**What we ship anyway: the same read-only remount the repos get.**
`wmf-sbx-setup` now remounts `~/.claude/skills` `ro` — at install, before
any repo work, and again from the `--restore` startup command on every
container start, which is where it has to be because the remount is
mount-namespace state and `sbx stop` discards it exactly as it discards
the repos' (§4). `--verify` reports a store that came back writable, so
it fails the same way a mirror that came back `rw` does, rather than
quietly. MEASURED [§79], both directions, in a live sandbox:

```console
agent@wmf-claude-sbx:~$ sudo mount -o remount,ro,bind ~/.claude/skills
agent@wmf-claude-sbx:~$ touch ~/.claude/skills/.probe2
touch: cannot touch '…/.probe2': Read-only file system
agent@wmf-claude-sbx:~$ sudo mount -o remount,rw,bind ~/.claude/skills
agent@wmf-claude-sbx:~$ touch ~/.claude/skills/.probe2 && echo BYPASSED
BYPASSED
```

That second half is the whole caveat, and it is the same one §3 makes
about the repo mirrors: the remount is in the container's own mount
namespace, the agent is root there, and one command lifts it. **It is a
second layer, not a boundary.** It stops a careless write, an agent that
installs a skill without thinking about where the directory goes, and a
prompt-injected one that does not think to try `sudo`; it stops nothing
that means it. It is worth shipping because it costs nothing — Route A
means we never write there — and because it converts the default from
"writable unless someone noticed" to "writable only on purpose".

The fix that *is* a boundary is host-side, and it is upstream's: mount
`agent-skills` `ro` into sandboxes that have no reason to write it, or
make it per-sandbox. sbx gives each sandbox its own everything else and
then shares one mutable directory whose contents are instructions to a
model.

**Upstream has it, and has accepted it:
[`docker/sbx-releases#506`](https://github.com/docker/sbx-releases/issues/506)**,
filed 2026-08-25 against 0.39.0 by another user — same mount, same
reading of it as a back channel between agents. It is assigned to a
maintainer, who replied the same day: the concern is valid, **the mount
will be read-only by default**, with a `--shared-skills-rw` flag to opt
back in, and — their words — "this change will only affect new sandboxes
as we cannot change the bind mount behaviour dynamically after the
fact".

That is the fix we would have asked for, and two things follow. It **had
not shipped as of 0.42.1** — the mount measured above is `rw` — so the
remount stays our answer in the meantime. And when it does ship,
**upgrading is not enough**: bind-mount behaviour is set at create time,
so existing sandboxes stay `rw` until they are recreated (§9). Our
remount degrades correctly either way — it no-ops on a mount that is
already `ro`.

**There is no host-side opt-out on 0.42.1.** MEASURED on the host
[§79.6]: `sbx create --help` lists 21 flags and not one matches `skill`.
The proposed `--shared-skills-rw` has not shipped, as the open issue
implies — but neither is the **`--no-share-skills`** that #506's own
body describes as existing, and we cannot say why. So the remount above
is not a second layer standing behind a boundary; **it is the only thing
standing there**, and everything §7.6 says about it being liftable by
the agent applies without anything underneath. That is the residual risk
we are carrying until #506's fix ships and sandboxes are recreated.

Our own write-up stays at `reference/upstream/skills-store-writable.md`
as the reproduction record. The two findings it had that the issue did
not — the modify/delete channel, and persistence past `sbx rm` — are now
on the issue, added as a comment 2026-09-14.

**It is one directory, and both sandboxes can write it.** MEASURED
[§77.4]: a file touched in `gerrit-probe` was read in `wmf-claude-sbx`
57 seconds later, and then **deleted from `wmf-claude-sbx`**, exit 0. So
the channel is not append-only. A compromised sandbox can plant a skill
that every other sandbox on the host loads, and it can equally replace or
remove the skills another sandbox depends on, with no signal on the other
side.

A trap worth naming, because it nearly buried this: `ls ~/.claude/skills/`
prints nothing here, since a planted file would start with a dot — as
both probe files did. An empty `ls` means "no non-hidden entries", not "no
entries". Use `ls -la` when a directory's emptiness is the thing you are
relying on.

**Epilogue, 2026-09-14: closed for sandboxes recreated on 0.43.0-rc3.**
Everything above describes a sandbox created before the upgrade this
section spent most of its length asking for. The first sandbox built
after it (`wmf-claude-sbx`, recreated the same day; `NOTES.md` §82.9,
§83) has **no `~/.claude/skills` at all** — not `ro`, not present:
`grep skills /proc/self/mountinfo` finds nothing, the directory does not
exist, and `touch` fails with `ENOENT`. `--skills=off` did what #506's
maintainer promised. That makes the read-only remount described above
what it was always meant to be — an accident guard with nothing behind
it to fall back on — rather than, as this section still frames it for a
pre-upgrade sandbox, the only thing standing between a compromised
sandbox and every other one on the host. **The residual risk is now
scoped to sandboxes not yet recreated**: anything still running on its
0.42.1-or-earlier create still has the `rw` mount above and the remount
is still all it has. `lock_shared_skills` itself needed no change — it
already no-ops correctly on an absent mount — only this write-up did.

### 7.7 The sandbox can open URLs in the host's browser, unprompted

The egress proxy at `gateway.docker.internal:3128` — the one every
outbound request goes through — also serves a control endpoint,
`POST /_sbx/browser-open`, which makes the **host** `sandboxd` launch the
**host's** default browser. A pre-installed shim at
`/usr/local/bin/xdg-open` calls it, so anything in the sandbox that opens
a URL the ordinary way reaches it; but the shim is a convenience, not the
interface. MEASURED [§80]: plain `curl` from the sandbox reaches the
endpoint directly, with no token beyond what is already in the
environment.

**What gates it is the network allowlist, and nothing else.** Two probes,
neither of which opened anything:

```console
agent@wmf-claude-sbx:~$ curl -s -i --noproxy '*' -X POST \
    http://gateway.docker.internal:3128/_sbx/browser-open \
    -H 'Content-Type: application/json' -d '{}'
HTTP/1.1 400 Bad Request
only https URLs are supported

agent@wmf-claude-sbx:~$ … -d '{"url":"https://example.invalid/wmf-sbx-probe"}'
HTTP/1.1 403 Forbidden
Blocked by network policy: domain example.invalid:443
  detail: no matching allow rule — blocked by default deny policy
```

So: https only, and the URL is checked against the same policy that
governs egress — *the identical message* §1 documents for a blocked
request. There is no separate browser policy, no host confirmation
prompt, and on 0.42.1 no setting to turn the endpoint off. **Any domain
the sandbox may talk to, the sandbox may also open in cananian's
browser.**

That is a second job for the allowlist we had not accounted for. §1
already argues for narrowing it because of what the agent can *send*;
this is what the agent can make the host's own browser *fetch*, in a
session that carries cananian's live cookies for github.com,
gitlab.wikimedia.org, phabricator, and Google. The realistic abuse is
not "a tab appears" — it is a GET-shaped state change, or an OAuth or
account-linking flow, driven at a moment the user is not expecting one.
`#577`'s report makes the same argument at more length.

**Not tested, deliberately:** whether an *allowed* domain actually opens.
The test is one `curl` with `https://github.com/` in it, and it would put
a real tab on cananian's desktop — an outward-facing side effect on
someone else's machine, which is not ours to trigger for a measurement we
can reason about instead. The 403 above proves the endpoint is live and
enforcing; what it does on an allowed domain follows from #577's report
and from the shim existing at all. If we want it confirmed, cananian runs
it and expects a tab.

**Upstream:
[`docker/sbx-releases#577`](https://github.com/docker/sbx-releases/issues/577)**
asks for `sbx settings set browser.enabled false`; a maintainer answered
2026-09-10, "valid points and good suggestion. We'll look into adding
this as a setting." Not in 0.43.0-rc3's notes. §10 carries the watch.
Note the lineage: 0.42.0 fixed *a sandbox escape* on this same endpoint
(the D-Bus/arbitrary-command bug in §9), which means the control plane
here has already been wrong once in a way that reached the host.

**What we do about it today: nothing, and that is a choice.** Removing
`/usr/local/bin/xdg-open` inside the sandbox would stop the polite path
and not the endpoint — same shape as §7.6's remount, one layer below
where the decision lives. The real lever is §1's allowlist, which is on
the to-do list for other reasons and now has one more.

## 8. What the agent can do, plainly

Inside the sandbox: everything. Passwordless root, arbitrary code, arbitrary
`apt-get`, its own Docker engine, and network reach to every domain in §1.
That is the point of the design — the blast radius is the VM.

Reaching *out* of the VM, it has:

- read/write on the **primary** workspace's real host directory (§3);
- read on every extra workspace's real host directory, and write on them
  for the first seconds after a container start (§4);
- the host's SSH agent, if `sbx` was ever invoked without the wrapper (§2);
- an open HTTP egress path to a large allowlist (§1);
- **the host's default browser**, for any https URL on that same
  allowlist, with no confirmation (§7.7);
- the MCP gateway, and through it whichever host-side MCP servers we
  expose — tool by tool, if the §7.2 deny and the proxy allowlist are in
  place; everything the host has registered, plus arbitrary JS, if they
  are not (§7);
- the shared agent-skills store, and through it a write channel into
  every other sandbox on the host, present and future — read-only from
  this sandbox until it runs one `sudo mount` (§7.6).

It does **not** have: the host's filesystem outside those mounts, the host
Docker socket, the macOS keychain, the credentials the host-side MCP
servers use (§7.1), or a way to push to Gerrit or GitHub (absent a
forwarded agent).

## 9. Upgrading — what 0.42.1 bought, and what 0.43 brought

### 9.1 The 0.43.0-rc3 upgrade — taken 2026-09-14

**Done, and confirmed on both sides of the boundary.** The host:
`wmf-sbx version --json` reports client and server both at
`v0.43.0-rc3` (`api_version: 0.31.0`); the sandbox: the first one
recreated after it (`wmf-claude-sbx`) has the §7.6 fix in effect —
`~/.claude/skills` does not exist at all (`NOTES.md` §82.9, §83;
`SECURITY.md` §7.6 epilogue). The recreate itself hit a real instance of
the §47.7/§47.8 stdin-hang bug on a call site the original fix missed
(`NOTES.md` §82.9) — fixed the same day, and a reminder that this
upgrade needed the fix it was partly meant to test.

**The fix for §7.6 has shipped**, in
[v0.43.0-rc3](https://github.com/docker/sbx-releases/releases/tag/v0.43.0-rc3),
published 2026-09-09 and pointed at us by the maintainer on #506:
`sbx create`/`sbx run` take `--skills=off|readonly|readwrite`, the
default is **read-only**, and there is a `skills.defaultMode` setting.
We want `off` rather than `readonly` — read-only protects the store from
us, `off` protects us from whatever another sandbox put there, and we
need neither direction, since our plugin ships in the kit and not in the
shared store. `wmf-sbx-create` already passes it when the binary accepts
it (it probes `create --help`; on 0.42.1 it passes nothing and warns).

Two caveats that decide *how* the upgrade is done rather than whether:

- It is a **release candidate**. Running a pre-release on a development
  host is a judgement call, and it is the engineer's.
- **Recreating sandboxes is not optional** — see below.

0.43 also stops sandboxes automatically when they go idle, which makes
§4's residual restart window a routine event rather than a rare one, and
adds mount information to `inspect`, which is the first host-side view of
the mount table we have ever had — every `:ro` claim in this document was
measured from inside a sandbox, which is the wrong side of the boundary.
Both are on the post-upgrade measurement list.

What 0.43.0-rc3 does **not** fix: §3. The primary workspace is still
mounted read-write and `:ro` on it is still rejected.

The upgrade plan was `sbx/RESUME.md` — the recreate command, what to
measure before it, and what to measure after — folded back into
`NOTES.md` §0 and removed once that plan was
carried out. `NOTES.md` §81 is the full release-note reading; §82 is what
we changed in this tree because of it; §83 and §84 are the two rounds of
post-upgrade measurement — version, the skills mount (both sides of the
boundary now), `inspect`'s new mount view, the network policy dump,
name-length rejection, the per-agent `--help` residual, and the
idle-auto-stop timing above. Two threads remain open: whether to set
`skills.defaultMode` to `off` host-wide (belt-and-braces only, §84.1),
and the idle-stop resume-cost timing (§84.6, §4 above).

### 9.2 Upgrading to 0.42.1 (done, 2026-09-08)

Three of the fixes are security fixes we want:

- a sandboxed process could get the daemon to open a host D-Bus transport
  and **execute an arbitrary command on the host** — a full escape;
- a malicious sandbox could hijack another sandbox's OAuth login by
  pre-claiming its callback port;
- the CONNECT/SNI egress bypass in §1.

The first is reason enough on its own. See `NOTES.md` §47 for what the
upgrade changed on our side and the re-measurement after it.

**On every upgrade after this one, read the release notes** — for
[`#506`](https://github.com/docker/sbx-releases/issues/506), the shared
agent-skills store (§7.6), and for everything else. That practice found
the #506 fix in v0.43.0-rc3 (§9.1) and four unrelated changes that touch
our code, none of which we would have found by grepping for the thing we
were looking for.

**Taking it requires recreating sandboxes.** The bind mount is
configured at create time — upstream's own caveat is that the change
"will only affect new sandboxes" — so an sbx upgrade alone leaves every
existing sandbox with the `rw` mount it was born with. The upgrade
procedure for that release is therefore: upgrade, then recreate each
sandbox (`wmf-sbx-create` from its kit; §4 describes what a sandbox
loses), and verify with `grep skills /proc/self/mountinfo` inside a new
one. Until a given sandbox is recreated, §7.6's remount is still the
only thing there.

## 10. Open follow-ups

- ~~**Generate with `--static-mcp`** (§7.2).~~ **LANDED** 2026-09-12
  (§7.4): `build_sbx_command` passes it for every server the host
  actually registered. Both halves are in place — the deny hides the
  gateway's meta-tools, static mode removes three of them outright — and
  the host run this bullet used to ask for has happened: Route 1 answers
  a model's tool call from a kit-built sandbox, with the credentials on
  the host [§72–73]. One observation still missing, tracked in `NOTES.md`
  rather than here because it is a coverage gap and not a risk: no tool
  call has gone through a *generated* kit's gerrit entry (§7.4).
- **Watch each new sbx release for a fix to the shared skills store**
  (§7.6). One host directory, writable from inside a sandbox and shared
  by every sandbox on the host — both halves MEASURED [§77.3, §77.4],
  including deleting one sandbox's file from another. It outlives every
  sandbox that touched it, and its contents are instructions to a model.
  Nothing we can set from inside a sandbox *fixes* this, because the
  agent is root in there (§8). **Already reported upstream**, by another
  user:
  [`docker/sbx-releases#506`](https://github.com/docker/sbx-releases/issues/506)
  (2026-08-25, 0.39.0), **accepted by a maintainer the same day**, and
  **fixed in v0.43.0-rc3** — `--skills=off|readonly|readwrite`, read-only
  by default, plus a `skills.defaultMode` setting (§9.1). So this is no
  longer a watch; it is an upgrade, and the ask is now: take it, **and
  recreate every sandbox**, because the bind mount is configured at
  create time and an upgrade alone leaves existing sandboxes `rw`.
  **Until each sandbox is recreated there is nothing host-side to hold
  this** — checked on the host 2026-09-14: `sbx create` on 0.42.1 has no
  skills flag, neither the proposed `--shared-skills-rw` nor the
  `--no-share-skills` #506's body calls existing [§79.6]. So today the
  read-only remount in §7.6 is the whole of our answer, and the agent can
  lift it; this stays an open risk until the recreate, not a closed one.
  Our delete and persistence measurements are on the issue as of
  2026-09-14.
  **Update, 2026-09-14, same day: the upgrade is taken and `wmf-claude-sbx`
  is recreated on it** — confirmed `v0.43.0-rc3` on both host and
  sandbox, `~/.claude/skills` absent entirely in the new sandbox
  (`NOTES.md` §83; §7.6 epilogue above). Closed **for that sandbox**.
  Still open for any sandbox not yet recreated (e.g. `gerrit-probe`, if
  it still exists — `NOTES.md`'s "Still to do" list has the removal) —
  the ask narrows from "take the upgrade" to "recreate what's left".
- **Narrow the network policy** with `--deny-network` plus a deliberate
  allowlist, instead of inheriting sbx's permissive local defaults (§1).
  This is the biggest single improvement available and nothing depends on
  it landing first.
- ~~**Adopt 0.42's explicit SSH-forwarding disable** as a second layer
  under the wrapper (§2).~~ **DONE** 2026-09-14:
  `ssh.agentForwardingEnabled=false` [§78.1]. No daemon restart was
  needed — this host's daemon was started through the wrapper and has no
  agent to forward, and every future daemon reads the setting. The
  wrapper stays; see §2 before restarting a daemon for any reason.
- **Verify `git daemon --base-path` traversal scoping** rather than
  assuming it (§5).
- **Close the restart window** properly: everything that can start a
  container should wait for the layout, not just `wmf-sbx-resume` (§4).
  Wanting this to be sbx's job rather than ours is reasonable; it is not
  ours to fix upstream today.
- **Make the primary workspace read-only** (§3). The primary's host
  directory being writable from inside the sandbox is the largest
  structural gap in this document, and it is the one thing 0.43 does
  *not* fix. Two routes, and the order between them changed on
  2026-09-14:
  - **The upstream feature request is filed**:
    [#586](https://github.com/docker/sbx-releases/issues/586), cananian,
    2026-09-14 — let `sbx create` accept `:ro` on the first positional
    (`NOTES.md` §82.6–§82.7). This is the preferred route, because it
    closes the gap without moving the agent's working directory, and
    because most of what it asks for already exists upstream: `:ro`
    enforcement below the namespace since 0.39.0 (#556), and
    no-workspace sandboxes since 0.42.0. Now a watch item — track it the
    way §7.6 tracked #506, and read each release's notes.
  - **The scratch primary is designed and deferred** (`NOTES.md` §82.3,
    deferred §82.5). MEASURED on the host 2026-09-14: the read/write
    requirement is **positional**, so an empty scratch directory
    satisfies it as well as a real repo does, on 0.42.1 and on 0.43
    alike. It was shelved because it moves the agent's starting
    directory to a meaningless path, and cwd turns out to be
    load-bearing — see §3. Reopen if the request is declined.

  Either way the conversion is the same: the host checkout goes from
  "writable, with an in-sandbox remount in front of it that the agent can
  lift" to "read-only below the namespace".
