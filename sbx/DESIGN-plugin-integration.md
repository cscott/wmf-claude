# Integrating the parent `wmf-claude` plugin into sbx sandboxes

**Status: design; step 1 of §5 implemented** (2026-09-11). Plans the
`sbx/NOTES.md` "Still to do" bullet: *"Integrate skills and MCP servers
from the parent `wmf-claude` package into the sbx kit."*

Supersedes the old plan's Phase 4 ("host-side MCP servers"), which was
written before we knew sbx ships its own MCP gateway and budgeted two
weeks for a transport that already exists and is governed; see §4 below
and `sbx/NOTES.md` §55.2.

GitLab authentication started here and moved out: it turned out to have
almost nothing to do with MCP, and lives in
`sbx/DESIGN-gitlab-integration.md`. The one surviving link is that if the
agent ever talks to GitLab's own MCP endpoint, it arrives by §3's Route A.

---

## 1. What has to arrive, and what "arriving" means

`sbx create` today launches a bare `claude`. The parent package's
`bin/claude` + nono wiring sets up five separate things, and each needs
its own answer — they are not one problem:

| Artifact | Count | How it reaches Claude today (nono) | Status under sbx |
| --- | --- | --- | --- |
| `skills/<name>/SKILL.md` | 15 | plugin, namespaced `/wmf-claude:<name>` | absent |
| `agents/*.md` | 5 | plugin, auto-invoked by description match | absent |
| `hooks/hooks.json` → `bin/session-start.sh` | 1 | plugin `SessionStart` hook | absent |
| MCP: `phabricator` (node), `gerrit` (python venv) | 2 | `claude mcp add --scope user` by `bin/wmf-claude-setup` | absent |
| `wiring/settings-merge.json` (permission denies) | 1 | `json_merge` into `~/.claude/settings.json` | absent |
| MCP: `chrome-devtools` | 1 | `--mcp-config` from `bin/claude --chrome` | deferred, not impossible — see §8 |

Two things already work and must not be broken: the kit's
`files/home/.claude/CLAUDE.md` (`wmf_sbx_kit.HOME_CLAUDE_MD`), and the
`agentInstructions` slot the kit spec offers but we don't use.

Noticed while counting those 15: `skills/standalone-vuln-audit/` was in
neither `package.json`'s `artifacts` nor the hook's skill list, so the
nono pack had been shipping without it. Fixed, and `tests/test-templates.sh`
now asserts the three lists agree (`NOTES.md` §56.4). Whatever enumerates
skills for the kit should still read the `skills/` directory rather than
becoming a fourth hand-maintained copy.

---

## 2. Measured facts about the sbx sandbox

All measured live in this dev sandbox (`wmf-claude-sbx`, sbx 0.42.1) on
2026-09-11, not read off documentation:

1. **`~/.claude/skills` is a mount, not a directory we own.**
   `/proc/mounts` shows `none /home/agent/.claude/skills virtiofs
   rw,nosuid,nodev` — sbx's shared agent-skills store. Anything a kit
   writes to `files/home/.claude/skills/` is shadowed at container start.
   **This rules out the obvious implementation.**
2. **`~/.claude.json` already carries `mcpServers`**, holding sbx's own
   gateway entry:
   `{"mcp-gateway": {"type": "http", "url":
   "http://mcp-gateway.docker.internal/mcp", "headers": {"Authorization":
   "Bearer proxy-managed"}}}`. A kit that ships a whole `.claude.json`
   clobbers the gateway. Any in-sandbox MCP registration must merge, at a
   point after sbx has written its entry.
3. **`~/.claude/plugins/` is a plain directory** (no mount), already
   populated with `marketplaces/claude-plugins-official/`. So the plugin
   route is open, and the marketplace layout the image itself uses is a
   working reference for the shape ours must take.
4. **Runtimes present in the base image**: `node` v22.22.1, `npm`,
   `python3` 3.14, `uv`, `jq`, `gh`. **Absent**: `sbx` (host-only, as
   expected) and `glab`.
5. **The generated kit already allows every WMF host we need.**
   `wiki_family_domains()` pulls `*.wikimedia.org` out of
   `profiles/wmf-engineer.json`, and sbx's `*` matches one label, so
   `gerrit.`, `phabricator.` and `gitlab.wikimedia.org` are all reachable
   from a generated sandbox today. No network change is needed for any
   in-sandbox option below. (PyPI is a different matter — see §4, Route
   2.)
6. **The image's official marketplace ships a `gitlab` plugin**, and it
   is nothing but an HTTP MCP endpoint:
   `{"gitlab": {"type": "http", "url": "https://gitlab.com/api/v4/mcp"}}`.
   That is GitLab's own first-party MCP server, served by the GitLab
   instance itself. Not pursued here — GitLab auth turned out to be
   orthogonal to this work and moved to
   `sbx/DESIGN-gitlab-integration.md`.

---

## 3. Skills, agents and the SessionStart hook

Three possible routes. They are not exclusive, but one has to be primary.

### Route A — ship the plugin in the kit's static files *(recommended)*

`write_kit_dir` already writes `files/home/.claude/CLAUDE.md`. Extend it
to write the full plugin wiring, which is exactly the four things
`package.json`'s `wiring` block does on the host, minus the symlinks
(copy the tree instead, since `files/` has no symlink primitive):

```
files/home/.claude/
  plugins/
    marketplaces/wikimedia/.claude-plugin/marketplace.json   # wiring/marketplace.json
    marketplaces/wikimedia/plugins/wmf-claude/               # copy of the plugin tree
    cache/wikimedia/wmf-claude/0.1.0/                        # copy of the plugin tree
    known_marketplaces.json                                  # wiring/known-marketplaces.json, $HOME/$NOW expanded
    installed_plugins.json                                   # wiring/installed-plugin.json, ditto
  settings.json                                              # enabledPlugins + settings-merge.json
```

The "plugin tree" is `.claude-plugin/`, `skills/`, `agents/`, `hooks/`,
and the `bin/` scripts the hook invokes — markdown and shell, a few
hundred KB, no build step. Copying it twice is wasteful but honest; the
host wiring uses two symlinks to the same directory for the same reason,
and deduplicating would need a `setup.install` step to make the link.

Keeps `/wmf-claude:<name>` namespacing, brings agents and the hook along
for free, and needs nothing from the host at create time.

**Three risks, all real:**

- `installed_plugins.json` / `known_marketplaces.json` are Claude Code
  *internal state files*, not a public contract. The format can change
  under us. Mitigation: assert the wiring at session start rather than
  trusting it — a kit `setup.startup` command that runs
  `claude plugin list` (or greps the marketplace dir) and writes a loud
  line to `/var/log/sbx-kit-startup.log` if the plugin is not loaded.
  We already read that log back after create (`report_setup_problems`).
- `settings.json` collides: sbx writes its own
  (`permissions.defaultMode: bypassPermissions`, `model`, theme, …) and
  a kit static file would replace it wholesale. This must be a
  **merge**, done by a `setup.install` step in Python against the file
  that is already there, not a `files/home/` drop.
- `wiring/settings-merge.json`'s `permissions.deny` list is worth
  bringing (defense in depth on `~/.ssh`, `.env`, `*.pem`), but its
  `"sandbox": {"enabled": false}` entry is nono-specific and its
  `permissions.allow` list is redundant under `bypassPermissions`. Port
  the `deny` list only. Deny rules **are** honoured in bypass mode —
  measured, §6 Q5 — so this buys something real, subject to the `//`
  absolute-path rule that finding also turned up.

  The sbx port also **adds** one entry the nono list has no need for:
  `"mcp__mcp-gateway"`. sbx gives every sandbox's agent the gateway's
  five meta-tools, two of which (`mcp-add`, `code-mode`) let it mount
  arbitrary MCP servers and run JavaScript on its own initiative. The
  whole-server deny form removes all five from the model's view while the
  gateway stays connected, and does not impede
  `wmf-sbx-mcp-proxy`'s own HTTP calls — so the agent's MCP surface
  becomes exactly the tools §4 lists (measured, `NOTES.md` §60.4).

### Route B — sbx's shared skills store

`sbx skills add <git-url>` / `sbx skills import` puts `SKILL.md` trees
into a host-side store that every sandbox mounts at
`~/.claude/skills`. It is the sanctioned mechanism and needs no kit
change at all.

**Rejected as primary**, for three reasons: it carries skills only (no
agents, no hook, no namespace); the store is mounted **read-write and
shared by every sandbox**, so one compromised sandbox can rewrite a
skill another later executes — the docs say so explicitly; and it is
marked experimental. Worth documenting as the escape hatch for a user
who wants one skill in a non-MediaWiki sandbox, nothing more.

### Route C — bind-mount the host `wmf-claude` checkout read-only

sbx mounts a host path at the same literal path inside, so
`/home/<user>/Projects/Wikimedia/wmf-claude:ro` would make the live
plugin tree — and its *built* MCP servers — available with zero copying,
and the wiring JSON could point at it. Tempting, and it is the only
route that also solves §4 for free.

**Not primary**: it couples every MediaWiki sandbox to the engineer's
tooling checkout (path, freshness, and `find_nested_mount_conflict`
interactions), and an agent editing the wmf-claude checkout in one
sandbox changes the tooling of the next. It was also the MCP fallback
(§4, Route 3), where the prebuilt-dependency argument seemed to be the
whole point — that use is dead twice over, on the venv and on
credentials, so what is left in reserve is only the skills/agents half.

### The SessionStart hook needs the Seam 1 work (done)

`bin/session-start.sh` asserts *"The session is sandboxed by nono
(OS-level enforcement)"* and describes `bin/claude --chrome` /
`--local-web` tiers that do not exist here. Shipping it unchanged tells
the model false things about its own environment — worse than shipping
no hook. Seam 1 (`WMF_CLAUDE_SANDBOX_BACKEND`, backend text in
`hooks/sandbox-context/<backend>.txt`) was therefore a **prerequisite**,
not a parallel nicety — landed 2026-09-11 — and the sbx text has to cover
what the sandbox
actually is: the parallel clone tree, the read-only originals, the
`local`/`origin` remote swap, and committing as the hand-off. Much of
that is already written, in `HOME_CLAUDE_MD` — the two should share a
source rather than drift.

---

## 4. MCP servers

Three routes. This is where §9 of the implementation plan is out of date.

**Decided 2026-09-12: Route 1, with a shim.** Two criteria, applied in
this order.

**First, credentials, which are not negotiable.** A route that runs the
MCP servers *inside* the sandbox puts every credential those servers need
inside the sandbox too, where the agent can read it — an environment
variable is in `env` and in `/proc/<pid>/environ`, a config file is a
file. `mcp-phabricator` takes **`PHABRICATOR_API_TOKEN`** (a full-account
Conduit token); `gerrit-mcp-server` takes an `auth_token` or a
`gitcookies_path` (default `~/.gitcookies` — push credentials for all of
Gerrit). Both read public data anonymously *today*, but authenticating is
the obvious next step for both, and a route that breaks on that step is
not worth adopting. **That rules Route 2 out** (`NOTES.md` §60.1).

**Second, tool naming**, which is an engineering problem and now has a
fix. `skills/`, the `gerrit-reviewer` agent and `bin/session-start.sh`
name tools literally (`mcp__phabricator__phabricator_get_task`,
`mcp__gerrit__get_commit_message`) and "do not fork `skills/`" is a
durable non-goal (`sbx/NOTES.md` §0, "Durable non-goals"), so the gateway's
`mcp__mcp-gateway__*` prefix had to go. `sbx/helpers/wmf-sbx-mcp-proxy` — a
stdlib-Python stdio MCP server in the sandbox, named after the host-side
server it fronts — restores the literal names without moving anything
across the boundary.

| route | credentials | the model sees | verdict |
| --- | --- | --- | --- |
| 1 — **gateway, servers on the host, one proxy each** | **stay on the host** | **`mcp__phabricator__phabricator_get_task`** | **chosen** |
| 2 — build in the sandbox | in the sandbox, agent-readable | `mcp__gerrit__get_commit_message` | rejected: credentials |
| 3 — `:ro`-mount the host build | in the sandbox | Node: keeps them. Python: does not run at all | half dead, and same credential flaw |

### Route 1 — the sbx MCP gateway, servers on the host, with a naming proxy *(chosen)*

```console
$ sbx mcp add phabricator --command env --args "…,<node>,<repo>/mcp-phabricator/src/index.js"
$ sbx mcp add gerrit --command env --args "…,<repo>/gerrit-mcp-server/.venv/bin/python,…,stdio"
```

…and inside the sandbox, one config entry per server:

```console
$ claude mcp add --scope user phabricator -- \
      wmf-sbx-mcp-proxy phabricator \
      --tools phabricator_get_task,phabricator_search_tasks
$ claude mcp add --scope user gerrit -- \
      wmf-sbx-mcp-proxy gerrit \
      --tools get_change_details,get_commit_message,get_file_diff,…
```

`--tools` is **required**, not a convenience — see "which tools are ours"
below.

This is precisely what the old plan's Phase 4 set out to build — an
authenticated host-side transport with a thin client inside — except
that sbx already ships the transport, with governance and an audit trail,
and the sandbox reaches it over
`http://mcp-gateway.docker.internal/mcp` (measured, §2.2). It removes
Node and a Python venv from the sandbox entirely, reuses the servers
`setup.sh` already builds on the host, adds nothing to sandbox create
time, and keeps the tokens on the host.

**How the proxy works** (`NOTES.md` §60.4). It speaks newline-delimited
JSON-RPC to Claude Code on stdin/stdout and streamable HTTP to the
gateway; on `initialize` it `mcp-add`s its server (dynamic mode) or finds
it already mounted (`--static-mcp`), then serves `tools/list` and
`tools/call` straight through, minus the gateway's five meta-tools and
minus anything outside the `--tools` allowlist. Claude derives a
tool's prefix from the *config entry* name, so the entry being called
`phabricator` is what makes the tool
`mcp__phabricator__phabricator_get_task`. Nothing but JSON-RPC crosses
the boundary. 37 unit tests against a fake gateway.

**Which tools are ours: `--tools` is mandatory** (`NOTES.md` §62.2–62.4).
The gateway serves every mounted server's tools in **one flat namespace
with no attribution**: `tools/list` entries carry no `_meta` and no
annotations, `mcp-find` returns server names only, `mcp-add` answers with
a count rather than names, and the tool names themselves follow each
server's own convention (phabricator prefixes, gerrit does not). So
"everything the gateway serves" cannot be a proxy's default — with both
servers mounted it would have made `mcp__gerrit__phabricator_get_task` a
working tool name. The allowlist is therefore required, and doubles as a
policy statement: gerrit serves 20 tools of which 15 write to Gerrit
under the engineer's credential (`abandon_change`,
`post_review_comment`, `create_change`, …), and the plugin names only
the 5 read-only ones. The proxy does use the one observable moment of
attribution — the names that appear across an `mcp-add` — but only to
warn about and drop an allowlisted tool this server does not serve:
mounts are sandbox-wide and outlive the gateway session (§62.3), so that
diff is non-empty exactly once per sandbox and cannot be a discovery
mechanism. With no allowlist at all the proxy fails closed and serves
nothing.

**Verified end to end for both servers, 2026-09-12** (`NOTES.md` §61.2,
§62.5), against the real gateway with real host-side servers, in exactly
the shipped shape — one proxy entry plus `deny: ["mcp__mcp-gateway"]`.
The `init` event carried `mcp__phabricator__phabricator_get_task` and
`mcp__phabricator__phabricator_search_tasks` and **no other `mcp__*`
tool** (two of the four phabricator tools the gateway served — the
`--tools` allowlist is enforced in the sandbox), both servers
`connected`, and the model fetched T1's real title with no permission
denial. The gerrit run was the same: exactly the five read-only
`mcp__gerrit__*` tools out of the 29 the gateway served, and
`get_commit_message` returned change 1338945's real subject line.

**It also shrinks the agent's MCP surface, which is the part to keep
regardless.** By default every sbx sandbox hands the model `mcp-add`
("its tools become available immediately") and `code-mode` ("create a
sandboxed JavaScript execution tool"), so the agent can mount servers
and evaluate JS unprompted. Measured: `permissions.deny:
["mcp__mcp-gateway"]` removes all five meta-tools from the session while
the gateway itself stays `connected`, and does not affect the proxy's own
HTTP calls. Ship that deny alongside the proxy entries and the agent's
MCP surface is exactly the tools we listed (§3 Route A's deny-list
merge).

**Use `--static-mcp phabricator,gerrit` at create time, and pass
`--no-add` to the proxies** (`NOTES.md` §61.3). Not for the reason §58.1
assumed. Dynamic mounting works fine — `mcp-find` with an *empty* query
enumerates host registrations and `mcp-add` mounts one into a sandbox
created days earlier (§60.3), so the flag is not *needed* — but a
static-mode gateway serves only `code-mode` and `mcp-exec`: `mcp-add`,
`mcp-find` and `mcp-config-set` are **absent**, not merely denied. That is
a capability removed rather than hidden, for two servers we know at
kit-generation time. Keep the deny as well, since `code-mode` survives.

Two caveats, neither of them blocking:

- The static set is fixed at creation. `sbx mcp load <name> --sandbox <s>`
  is the documented way to add one to a running sandbox (§58.4), but it is
  unmeasured against a *static-mode* gateway. If it turns out not to work
  there, fall back to dynamic mode plus the deny — measured working
  (`NOTES.md` §61.2).
- Static mode does **not** get us per-server config entries:
  `~/.claude.json` in a `--static-mcp` sandbox still holds exactly one
  `mcp-gateway` entry, so the tools would arrive as
  `mcp__mcp-gateway__phabricator_get_task`. The proxy is needed in both
  modes (`NOTES.md` §61.3).

Two earlier objections are settled rather than outstanding:

- **The node-18 failure was the host's, not the route's**
  (`NOTES.md` §60.2). `sbx mcp inspect phabricator` resolved `node` to
  `/usr/bin/node` = v18.19.1, and `mcp-phabricator`'s `cheerio`→`undici`
  needs ≥20.18.1 (`ReferenceError: File is not defined`). Fixed on the
  host 2026-09-12 (node 26.8.2, registered by absolute path); note that
  `sbx mcp ls` reported `✓ ready` throughout — it checks command
  resolution, not that the server starts.
- **`sbx mcp add`'s missing `--env`** (§58.2) is worked around and now
  **proven**: `--command env --args "KEY=V,…,cmd,args"` registers cleanly
  and resolves to `/usr/bin/env` (`NOTES.md` §61.1). `--args` is
  comma-split, so no argument may contain a comma.

What remains a real cost: those registrations are host-side state the kit
cannot declare — `wmf-sbx-create` has to make them (step 4).

And note the boundary this route deliberately crosses: local stdio
servers **run on the host, outside the sandbox**. Docker says it more
bluntly than we would. From `sbx mcp add --help`: *"Local servers are for
ad-hoc development only. They have no identity, no verifiable supply
chain, and no sandboxing. The process runs with your host user's full
permissions."* Quote that in `SECURITY.md` rather than paraphrasing it.
The trade is deliberate: a host process the agent can only reach through
a JSON-RPC allowlist, in exchange for tokens the agent cannot read at
all.

### Route 2 — build the servers inside the sandbox *(rejected: credentials)*

Clone the two servers and build them in `setup.install`: `npm ci
--omit=dev` for `mcp-phabricator`, `uv venv` + `uv pip install -r
requirements.txt` for `gerrit-mcp-server`. Node and `uv` are already in
the image (§2.4). This measured *well* — it was the recommendation for
about a day — and is rejected on the one criterion the measurements did
not cover: the servers' credentials would live in the sandbox with them
(see the decision above, and `NOTES.md` §60.1).

Measured end-to-end in a sandbox, 2026-09-12 (`NOTES.md` §59.4–59.5),
and worth keeping because it is the fallback if the gateway ever proves
unusable *and* the servers are still anonymous:

- **~3.2 s and 38 MB** for the venv (hash-pinned `requirements.txt`,
  `requires-python = ">=3.12"`, runs fine under the image's 3.14 — the
  server completes a stdio handshake and lists all 20 tools), **~3.1 s
  and 38 MB** for `npm ci`. The "every `wmf-sbx-create` pays the install
  time" objection is real but is six seconds.
- **The tool names are exact**: registered as in-sandbox stdio servers,
  the `init` event carries `mcp__gerrit__get_commit_message` and
  `mcp__phabricator__phabricator_get_task`, alongside — not instead of —
  sbx's own `mcp-gateway` entry.
- **The sources need not be in the kit.** `*.wikimedia.org` is already
  allowed, so the sandbox clones
  `gitlab.wikimedia.org/kharlan/gerrit-mcp-server` and
  `gitlab.wikimedia.org/egardner/mcp-phabricator` itself, anonymously.
- It would need `pypi.org` and `files.pythonhosted.org` in
  `EXTRA_DOMAINS` (a kit allows 15 domains today; `registry.npmjs.org`
  already covers the Node half).

Also note that sbx's proxy-managed secrets — the general answer to
"keep a credential out of a sandbox" — do not rescue this route: they
inject per provider at the network layer, while Conduit takes its token
as a POST form field rather than a header, and basic auth against
`gerrit.wikimedia.org` is not a provider sbx knows.

### Route 3 — mount the host checkout read-only and run the built servers in-sandbox *(dead)*

Route C from §3, used for MCP: a `:ro` mount of the wmf-claude checkout,
at the same literal path, to reuse the build `setup.sh` already did.
Tool names would stay as they are, because the servers are still stdio
children of Claude Code. It shares Route 2's fatal flaw — the servers
run in the sandbox, so their credentials would have to be reachable from
it — and it does not work anyway:

**Half dead as of 2026-09-12** (`NOTES.md` §59.1). `.venv/bin/python3` is
an *absolute* symlink to `/usr/bin/python3`, which inside the sandbox is
Python **3.14**, while `site-packages` lives in `lib/python3.12/` and
`pyvenv.cfg` sets `include-system-site-packages = false`. The image
carries no 3.12. Every import fails, and no read-only mount can fix a
version baked into a directory name. The Node half *would* work
(`node_modules` has no `.node` binaries) — but a half-route that also
leaks tokens is not a route.

### Registration, whichever route

Every route registers *something* in the sandbox — the proxy entries
under Route 1, the servers themselves under Routes 2 and 3 — and
whatever it is must not clobber sbx's `mcp-gateway` entry in
`~/.claude.json` (§2.2). Two options were on the table, and measuring
settled which (`NOTES.md` §57.1):

- A `setup.startup` step running `claude mcp add --scope user …` — the
  same commands `bin/wmf-claude-setup` already runs on the host, and the
  "tool names unchanged" path §9 asked for. **Use this.** Verified
  2026-09-12 that an in-sandbox stdio registration keeps the literal
  names *and* leaves sbx's `mcp-gateway` entry connected (`NOTES.md`
  §59.4). `claude --mcp-config <file>` (without `--strict-mcp-config`)
  merges the same way and touches no state file — the right tool for
  measuring this, and a fallback if mutating `~/.claude.json` from a
  startup step ever proves fragile.
- A `.mcp.json` at the *plugin* root. It does work — the server
  connects, `${CLAUDE_PLUGIN_ROOT}` expands in `args`, and a real call
  returns — but Claude Code registers it as `plugin:<plugin>:<server>`
  and the model's tool becomes
  `mcp__plugin_wmf-claude_phabricator__phabricator_get_task`. The one
  mechanism that needed no mutation of Claude's state files is the one
  that breaks every literal tool name in `skills/`.

Keep the plugin `.mcp.json` in mind for a *new* server that no skill
names literally: there it is strictly nicer than mutating `~/.claude.json`.

---

## 5. Order of work

Each step is independently useful and independently verifiable; none
requires the next.

1. **Seam 1 — backend-aware SessionStart.**
   Prerequisite for shipping the hook at all. **DONE** 2026-09-11
   (`NOTES.md` §56): `hooks/context/<backend>/{sandbox,environment}.txt`,
   selected by `WMF_CLAUDE_SANDBOX_BACKEND`, plus
   `WMF_CLAUDE_DOCKER_MODE` (removed 2026-10-09: upstream MR !132 has
   the seam without it); nono output byte-for-byte unchanged;
   generated kits now declare `WMF_CLAUDE_SANDBOX_BACKEND=sbx`. Rather
   than sharing text with `HOME_CLAUDE_MD`, the sbx paragraph points at
   it — repo layout is the kit's story to tell, the boundary is the
   hook's, and a pointer cannot drift.
2. **Measure, before building anything** — the §6 list. **DONE**
   2026-09-12 (`NOTES.md` §57–59). Eleven questions, all answered except
   §6 item 2's last corner, which no longer decides anything. Two rounds
   went to the host (`responses15/16.txt`); most of the rest were
   answerable — and more correctly answerable — from inside a sandbox.
   *Took ~a day, mostly on measurements the host never had to run.*
3. **Route A: plugin in the kit.** **DONE** 2026-09-12 (`NOTES.md` §64).
   `write_kit_dir` grows `plugin_files()` / `write_plugin_tree()`, which
   copy the tree to both the marketplace and the cache path and write the
   three state files from `wiring/`; the `settings.json` merge is
   `wmf-sbx-setup --settings` applied to a patch shipped as data, run at
   install *and* on every start (install takes a shell string, startup an
   argv array — not interchangeable); `plugin_check_startup_command()`
   greps `claude plugin list`, which exits 0 even when empty. Skills are
   enumerated from the `skills/` directory, not `package.json`. Measured
   live against this session's own config: 15 skills, 5 agents and the
   SessionStart hook load, `defaultMode: bypassPermissions` survives the
   merge, and the `mcp__mcp-gateway` deny takes effect mid-session.
   *Took ~half a day, on top of the measurement in step 2.*
4. **MCP, Route 1 + the proxy.** **DONE** 2026-09-12 (`NOTES.md` §65),
   host half included: it was unmeasured at first (§65.3, no `sbx mcp`
   inside a sandbox to exercise it against) and has since been run on a
   host four times — the MCP store's `--json` shape (§67), the
   registration and both node paths (§69), Route 1 answering a model's
   tool call from a kit-built sandbox (§72–73), and the username
   divergence check that now preflights a stale registration (§75.5).
   One coverage gap is left and tracked in `NOTES.md`: no tool call has
   gone through a *generated* kit's gerrit entry — §62.5's gerrit run
   used a hand-registered proxy entry. The kit ships the
   proxy and `wmf-sbx-setup --mcp`, which reconciles one `claude mcp add
   --scope user` per server against `~/.claude.json` (read to decide,
   CLI to mutate; `remove` + `add` when an entry changed, since there is
   no `mcp set`); `wmf-sbx-create` registers the servers on the host
   first and passes `--static-mcp` for exactly the ones that took.
   `MCP_SERVER_TOOLS` is hand-written and a test asserts it covers every
   `mcp__<server>__<tool>` the plugin names — deriving it from the tree
   would turn "never call `mcp__gerrit__abandon_change`" into a grant.
   Verified live in-sandbox: both proxies connect, the second run is a
   no-op, and a `tools/call` for a non-allowlisted tool is refused even
   though the gateway serves it. *Took ~half a day.*
   The original plan, for the record:
   Step 2 settled it, and the credential
   constraint settled step 2. Ship `sbx/helpers/wmf-sbx-mcp-proxy` in the
   kit's `files/home/.local/bin/`; a `setup.startup` step registering one
   `--no-add` proxy entry per server with `claude mcp add --scope user`,
   **each with its own `--tools` allowlist** — required, and the place the
   kit decides that gerrit's 15 write tools are not on offer
   (`NOTES.md` §62.2–62.4); the `mcp__mcp-gateway` deny in the settings
   merge (step 3); a
   host-side `sbx mcp add` for each server in `wmf-sbx-create` (via
   `--command env`), skipped when the registration already exists; and
   `--static-mcp phabricator,gerrit` in `build_sbx_command`, which
   deletes `mcp-add`/`mcp-find`/`mcp-config-set` from the gateway
   outright (`NOTES.md` §61.3). Host prerequisite: node ≥20.18.1
   (§60.2) — preflight it and say so rather than registering something
   that cannot start. Both servers are measured working end to end
   (`NOTES.md` §61.2, §62.5), so this step is wiring, not discovery.
   *~1–2 days.*
5. **`SECURITY.md`.** The MCP half is **DONE** 2026-09-12 (§7.1–7.2 as
   design, §7.4 as shipped):
   `sbx/SECURITY.md` §7 covers the host-side servers running outside the
   boundary (with Docker's own "ad-hoc development only … no sandboxing …
   host user's full permissions" warning quoted, and the proxy allowlist
   as the mitigation) and §7.2 the gateway's default meta-tools
   (`mcp-add`, `code-mode`) and the `mcp__mcp-gateway` deny that closes
   them. The rest is **DONE** 2026-09-14 (`NOTES.md` §76): §7.4 no longer
   claims the host side is unmeasured and records the divergence
   preflight as the one exception to "never touch an existing
   registration"; §7.5 is new and states the proxy-managed token's
   residual risk — unreadable in the sandbox, but spendable by any
   process in it that can reach the proxy, narrowed by the per-server
   allowlist and by the gateway's hostname not resolving at all (§73.3).
   The shared skills store is §7.6, and it was nearly dropped on the
   grounds that Route A means we never write to it — which does not
   follow, and §77.2 is the correction: the store is one host directory
   symlinked `rw` into *every* sandbox whether we use it or not, so the
   boundary this step asked for is real and unconditional. The probe
   §7.6 left open has since been run, and then some: the store is
   writable, and one sandbox's file is readable **and deletable** from
   another (`NOTES.md` §77.3–§77.4). §7.6 now carries both measurements,
   the read-only remount `wmf-sbx-setup` ships for it (§79 — a second
   layer, MEASURED to be liftable by the agent in one `sudo mount`), and
   the upstream report drafted at
   `reference/upstream/skills-store-writable.md`. This step is closed;
   what remains is filing that report, which is tracked in `SECURITY.md`
   §10, not here. Still owed, and not from this design: the
   read-only-lockdown gap (`SECURITY.md` §3, §10).

## 6. Open questions — all answered, 2026-09-11/12

Most were answerable *inside* a sandbox, which is the better place to ask
them: they are questions about what a sandbox's Claude Code build does,
and the host's build is a different install (and, as it turned out, a
version behind — 2.1.236 vs 2.1.269). Two rounds went to the host for
the genuinely host-side ones (`NOTES.md` §58, §59.1–59.3); the rest were
measured in `wmf-claude-sbx` (`NOTES.md` §57, §59.4–59.5).

1. Does `sbx create` accept `--static-mcp`, or is it `sbx run` only?
   **Answered: `create` takes it** (so does `run`), comma-separated or
   repeated, "chosen once at creation time" and unchangeable on
   re-attach. We do not strictly *need* it — `mcp-add` mounts a host-side
   registration into a *running* sandbox (§60.3) — but we want it anyway:
   a static-mode gateway has no `mcp-add`, `mcp-find` or `mcp-config-set`
   at all, which is a capability removed rather than denied. So it does
   belong in `build_sbx_command`, for a security reason rather than a
   mechanical one. (`NOTES.md` §58.1, §60.3, §61.3.)
2. **What are the tool names through the gateway?** **Answered, and then
   fixed.** In the default dynamic mode the gateway serves five
   *meta*-tools (`mcp-find`, `mcp-add`, `mcp-config-set`, `mcp-exec`,
   `code-mode`); a server's own tools are reached by calling `mcp-exec`,
   so the model sees `mcp__mcp-gateway__mcp-exec`. The prefix cannot
   improve *for the gateway entry*: Claude Code derives it from the config
   entry name. Probe packaged as `sbx/helpers/wmf-sbx-gateway-tools`.

   `--static-mcp` does not rescue it either — measured, a static-mode
   sandbox's `~/.claude.json` still holds exactly one `mcp-gateway` entry,
   so its tools would be `mcp__mcp-gateway__phabricator_get_task`
   (`NOTES.md` §61.3). That was the corner left open on 2026-09-11, and it
   closes against the gateway.

   So give it a different config entry: `sbx/helpers/wmf-sbx-mcp-proxy`
   fronts one gateway-hosted server as an in-sandbox stdio server named
   after it, and the tools arrive as
   `mcp__phabricator__phabricator_get_task` — measured end to end against
   a real host-side server, with a real call returning real data
   (`NOTES.md` §60.4, §61.2). Still worth raising upstream: per-server
   config entries would make the gateway drop-in compatible and this shim
   unnecessary.
3. Does `sbx mcp` require `sbx login`? **Answered: no.** `sbx mcp ls`
   exits 0 and reports `LOCAL · managed by you`. No new hard dependency.
   (`NOTES.md` §58.1.)
4. Does a plugin-root `.mcp.json` actually register servers?
   **Answered: yes — and it renames the tools.** The server connects
   (`${CLAUDE_PLUGIN_ROOT}` expands, the model called it and got a real
   answer) but arrives as `plugin:<plugin>:<server>`, so the tool is
   `mcp__plugin_wmf-claude_phabricator__phabricator_get_task`. Every
   literal name in `skills/`, the `gerrit-reviewer` agent and
   `session-start.sh` would be wrong. **Prefer `claude mcp add --scope
   user` in a startup step**; keep the plugin `.mcp.json` as the
   mechanism for a *new* server no skill names literally.
5. Are `permissions.deny` rules honoured under `bypassPermissions`?
   **Answered: yes**, for both `Read(**/*.pem)` and `Bash(cat:*)` — so
   the deny list is worth porting. One trap found: a **single-slash
   absolute pattern matches nothing**; Claude Code wants `//` for
   filesystem-absolute paths. `settings-merge.json` has none today, so
   it ports as-is, but anything added to it must follow that rule.
6. Does `npm ci` in the core clone fetch a headless browser?
   **Answered: no.** Default QUnit target is headless *Firefox*, not
   Chrome (`grunt qunit:chrome` is the separate target); both launchers
   merely `which` a system browser, and the only install scripts in the
   lockfile are drivers (`geckodriver`, `edgedriver`) plus `esbuild`.
   So §8's item costs "install a browser" exactly as written — and
   `grunt qunit` cannot run in a generated sandbox today either, which
   is a kit-setup gap, not a chrome-devtools one.
7. Do the two MCP submodules even have content in the host checkout?
   **Answered: yes** — both at their pinned SHAs, with `node_modules/`
   and the venv built. They are empty only in the sandbox's clone,
   because `git clone` does not recurse. (`NOTES.md` §58.1.)

Six things the rounds turned up that the list had not thought to ask:

8. **`sbx mcp add` has no `--env`**, and `bin/wmf-claude-setup`
   registers both servers with environment variables
   (`PHABRICATOR_USERNAME`, `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`).
   **Answered: `--command env --args "KEY=V,…,cmd,args"` works** — it
   registers cleanly and `sbx mcp inspect` shows
   `Resolved: /usr/bin/env`, and the server it fronts serves its tools
   through the gateway. Round 2's `ERROR: flag needs an argument: --args`
   was a line continuation in the probe, not sbx. The one constraint:
   `--args` is comma-split, so no argument may contain a comma.
   (`NOTES.md` §58.2, §61.1.)
9. **Does the Gerrit venv survive being mounted into the sandbox?**
   **Answered: no.** `.venv/bin/python3` is an *absolute* symlink to
   `/usr/bin/python3` — Python **3.14** in the sandbox — while the venv
   is `lib/python3.12/site-packages` built by the host's 3.12.3, with
   `include-system-site-packages = false`. The image has no 3.12. Route 3
   is dead for the Gerrit server; the Node half would work.
   (`NOTES.md` §59.1.)
10. **Is building in-sandbox actually expensive?** **Answered: no — ~6 s
    and 76 MB for both servers**, under the image's own Python 3.14 and
    node 22, with the literal tool names preserved and no host-side
    state. The sources need not even be shipped in the kit, since
    `*.wikimedia.org` is already an allowed domain. The only new
    requirement is `pypi.org` + `files.pythonhosted.org` in
    `EXTRA_DOMAINS`. (`NOTES.md` §59.4–59.5.) It measured well and lost
    anyway — see item 12.
11. **Why did the host-side phabricator server never start?** **Answered:
    the host's node is v18.19.1.** `mcp-phabricator`'s
    `cheerio`→`undici` needs ≥20.18.1 and dies with `ReferenceError: File
    is not defined`; `npm` does not enforce `engines`, so the install
    succeeded and only the run failed, and `sbx mcp ls` said `✓ ready`
    throughout (it checks command *resolution*). Fix on the host, then
    re-`add` — `Resolved:` is fixed at registration time.
    (`NOTES.md` §60.2.)
12. **Where does each server's credential live?** *The question the list
    never asked, and the one that decided everything.* `mcp-phabricator`
    reads `PHABRICATOR_API_TOKEN`; `gerrit-mcp-server` reads an
    `auth_token` or `~/.gitcookies`. Run either server in the sandbox and
    the agent can read its credential — so Routes 2 and 3 are out however
    well they measure, and Route 1 is in as soon as the tool names are
    fixed. sbx's proxy-managed secrets do not cover either server
    (Conduit's token is a form field, not a header; `gerrit.wikimedia.org`
    is not a known provider). (`NOTES.md` §60.1.)
13. **Once two servers share the gateway, which tools are whose?**
    *Also not on the list, and it is what made the proxy's allowlist
    mandatory.* **Answered: nothing in the gateway's API says.**
    `tools/list` carries no `_meta` and no annotations, `mcp-find`
    returns server names only, `mcp-add` answers with a count rather than
    names, and the tool names follow each server's own convention
    (phabricator prefixes, gerrit does not). The only observable
    attribution is the diff across a *first* `mcp-add`, and mounts are
    sandbox-wide and outlive the gateway session, so that fires once per
    sandbox. Hence `--tools` is required and the diff is a cross-check.
    (`NOTES.md` §62.2–62.4.)

## 7. Testing

Same shape as every other piece of this harness: the kit generator is
pure and unit-testable, and everything else is a `run=` injection point.

- `write_kit_dir` writes the plugin tree, the marketplace JSON with
  `$HOME`/`$NOW` expanded, and a `settings.json` merge step — assert the
  files exist and the JSON parses, and assert every directory under
  `skills/` is present (that is the drift guard).
- The `settings.json` merge is a function over two dicts: assert sbx's
  own keys survive.
- `build_sbx_command` / the host-side `sbx mcp add` calls: assert argv,
  as `test_wmf_sbx_create.py` already does throughout.
- End-to-end is a host job, and the acceptance test is small and
  concrete: create a sandbox, and inside it `/wmf-claude:write-commit-msg`
  resolves, the `gerrit-reviewer` agent is listed, and one
  `mcp__…__phabricator_get_task` call returns a real task.

## 8. Non-goals

- **Do not fork `skills/` or `agents/`.** Unchanged durable non-goal
  (`sbx/NOTES.md` §0, "Durable non-goals"). If a skill does not work under
  sbx, fix the environment or change the skill for both backends.
- **No chrome-devtools MCP in this phase** — deferred, not ruled out.
  Headless Chrome *does* run in an sbx sandbox: MediaWiki core's
  QUnit/Karma suite launches one
  ([Manual:JavaScript unit testing](https://www.mediawiki.org/wiki/Manual:JavaScript_unit_testing)),
  and `npm ci` in the core clone is already a setup step. What does not
  port is nono's *plumbing*, and the reason is that the two backends put
  the boundary in different places:

  - Under **nono**, sandbox and host share one network stack, and the
    profile closes localhost ports. The dev wiki and the Chrome being
    driven both run on the **host**, so `bin/claude` has to punch
    specific holes back to the host's loopback: `--local-web` opens
    80/443/8080 for `curl` (no CDP involved — that is the cheap Tier 1
    of `skills/manual-test`), and `--chrome` opens 9222 so the
    in-sandbox `chrome-devtools-mcp` can speak CDP to a Chrome the
    engineer started on the host with `bin/launch-test-chrome`.
  - Under **sbx**, the container has its own loopback and runs the wiki
    itself (`composer serve`, `NOTES.md` §30). There is nothing on the host to
    reach and nothing to open: in-sandbox `curl localhost` already
    works, so `--local-web` has no analogue at all, and a Chrome for
    CDP would be launched *inside* the container rather than attached to
    across a boundary.

  So the chrome-devtools work under sbx is "install a browser and point
  the MCP at a local one", which is a different task from everything
  else in this document and is sequenced after it. `skills/manual-test`
  will need its tier ladder rewritten for a backend where Tier 1 needs
  no flag; that is the Seam 1 family of work, not a fork.
- No `mwdocker` broker. sbx sandboxes run PHP natively; the broker
  exists because nono sandboxes cannot reach the Docker socket.
- Do not make the sbx kit depend on the nono pack being installed.
  `profiles/wmf-engineer.json` is read as a *file* today
  (`wiki_family_domains`) and that is the only coupling worth having.
