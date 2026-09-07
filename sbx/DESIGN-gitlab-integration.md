# GitLab integration: authentication, and which side of the boundary it lives on

**Status: auth is design only; the anonymous resolver half (bare-name search, fork upstream, origin-URL identification) is implemented, see NOTES.md §100** (2026-09-27; first written 2026-09-11). Split out of
`sbx/DESIGN-plugin-integration.md`, where this started life as a section
on "can we reuse a GitLab MCP server's auth config" — the answer turned
out to be orthogonal to the MCP question, so it lives here.

Feeds three open `sbx/NOTES.md` "Still to do" items, all of them about
handling GitLab repos as well as we currently handle Gerrit ones:

1. **Bare-name search** — a GitLab equivalent of §10's Gerrit search, so
   `gitlab:` repos don't always need a full explicit path
   (`GET /api/v4/projects?search=…`, `GET /api/v4/search?scope=projects&search=…`).
2. **True upstream of a fork** — GitLab and GitHub use fork-and-MR, so
   `origin` does not mean what it means on Gerrit
   (`GET /api/v4/projects/:id` → `forked_from_project`, qualified by
   `mr_default_target_self`; see
   [GitLab/Workflows/Making a merge request](https://www.mediawiki.org/wiki/GitLab/Workflows/Making_a_merge_request)).
3. **`local` remotes for every cloned repo**, not just Gerrit ones —
   which is the item that actually bites today: this very sandbox
   (`wmf-claude-sbx`) has no `local` remote, because its upstream is
   GitLab.

This document does not design the resolution logic for those. It answers
the question all three run into first: **when we call the GitLab API,
where does the token come from, and who is allowed to see it?**

---

## 1. There is nothing to reuse from the parent package

The hope was that `wmf-claude` already authenticated to GitLab
somewhere, and that the sbx kit could inherit the configuration. It does
not. Audited in full:

| Component | Auth |
| --- | --- |
| `mcp-phabricator/` | **anonymous** — public data only; the username it takes is a subscriber filter, not a credential |
| `gerrit-mcp-server/` | **anonymous** — read-only against a public Gerrit |
| `chrome-devtools-mcp` | local CDP, no network identity |
| `profiles/wmf-engineer.json` | grants `*.wikimedia.org`; declares no credential of any kind |
| `bin/claude` / nono | handles exactly one secret: the Claude API key |

There is no GitLab skill and no GitLab MCP server in the package. So
GitLab auth is new work, not a port.

What *is* reusable is a shape, and it comes from the sbx base image
rather than from us — see §3.2.

## 2. Host side: repo resolution

**This is where the three to-dos above live, and it involves no sbx
credential machinery at all.** `wmf_sbx.resolve` and `wmf_sbx.create`
run on the engineer's host, before any sandbox exists. The token is an
ordinary host credential; the sandbox never sees it and must never be
able to.

Precedence, mirroring how the rest of `repos.yaml` already behaves:

1. `$GITLAB_TOKEN` / `$CI_JOB_TOKEN` from the environment;
2. `glab auth token`, if `glab` is installed — it is the tool the WMF
   merge-request workflow documentation assumes;
3. `gitlab.token` / `gitlab.tokenCommand` in
   `~/.config/wmf-sbx/repos.yaml`, alongside the existing
   `extra_environment` / `extra_packages` keys;
4. **nothing — and this must stay a fully working path.**

Point 4 is the important one. `gitlab.wikimedia.org`'s public projects
answer all three queries anonymously; a token buys private projects and
a higher rate limit, not basic function. Resolution must degrade to
anonymous with a warning, the same way `wiki_family_domains()` degrades
when the profile is unreadable — never fail, never prompt. A user who
has never heard of `GITLAB_TOKEN` should see `wmf-sbx-create Translate`
work exactly as it does today.

Three rules for the token itself, all of which follow from the fact that
the agent can read its own sandbox:

- never log it, or interpolate it into a command that gets printed;
- never write it into a generated kit spec (those land in a temp
  directory) or into `wmf-sbx-plan.json`;
- never pass it through `--env`.

Use `stdin=DEVNULL` on any `tokenCommand` subprocess, per the standing
rule for non-interactive helpers — a token command that decides to
prompt should fail fast rather than hang invisibly.

## 3. Sandbox side: the agent talking to GitLab

A separate question, and not needed by the three to-dos. It matters when
the *agent* should read an issue, search projects, or open a merge
request from inside the sandbox.

### 3.1 The token still must not enter the sandbox

sbx has the right primitive:

```console
$ sbx secret set-custom \
    --host gitlab.wikimedia.org \
    --env GITLAB_TOKEN \
    --command 'glab auth token'
```

Inside, `$GITLAB_TOKEN` holds a generated placeholder (`sbx-cs-<rand>`).
When a request to `gitlab.wikimedia.org` carries that placeholder
anywhere, the host proxy substitutes the real value on the wire. So
`curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN"` works, as does anything else
that interpolates the variable, and the real token never exists inside
the boundary. This is the same posture as the existing GitHub
credential injection, and it is why "don't set API keys manually inside
the sandbox" is a standing rule rather than a preference.

Nothing needs adding to the network policy: `wiki_family_domains()`
already yields `*.wikimedia.org` from `profiles/wmf-engineer.json`, so
`gitlab.wikimedia.org` is reachable from every generated kit today.

### 3.2 GitLab serves its own MCP endpoint

The sbx base image ships the official marketplace's `gitlab` plugin, and
it is nothing but:

```json
{"gitlab": {"type": "http", "url": "https://gitlab.com/api/v4/mcp"}}
```

That is GitLab's own first-party MCP server, served by the GitLab
instance. The sibling `github` plugin adds the other half of the
pattern — `"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"`,
proving `${VAR}` expansion works inside a plugin `.mcp.json`.

The two compose with §3.1 exactly:

```json
{"gitlab": {"type": "http",
            "url": "https://gitlab.wikimedia.org/api/v4/mcp",
            "headers": {"Authorization": "Bearer ${GITLAB_TOKEN}"}}}
```

`${GITLAB_TOKEN}` expands to the placeholder, the proxy swaps in the
real token, and the agent gets GitLab tools without the credential ever
being inside the sandbox. Delivery is `DESIGN-plugin-integration.md`'s
Route A plus its plugin-`.mcp.json` option — which is the *only* real
coupling between the two documents.

**Verify before relying on this**: the MCP endpoint is a recent GitLab
feature and self-managed instances can have it disabled.
`curl -I https://gitlab.wikimedia.org/api/v4/mcp` from the host settles
it.

**Measured 2026-09-27 (NOTES.md §100):** anonymously, `/api/v4/mcp` and
`/api/v4/search?scope=projects` both answer 401. `/projects`,
`/groups/<g>/projects` and `/projects/<path>` work with no token.

### 3.3 The declarative alternative

A `credentials:` entry in the generated `spec.yaml` — `service:
gitlab-wikimedia`, `apiKey.name: GITLAB_TOKEN`, `proxyManaged: true`,
`inject: [{domain: gitlab.wikimedia.org, scheme: bearer}]` — plus
`sbx secret set gitlab-wikimedia` on the host. This is the documented,
non-experimental mechanism and it has the advantage of living in the
kit rather than in per-user state.

The catch: our generated kit is a *third-party* `schemaVersion: "2"`
kit, so it needs an approved binding in
`~/.config/sbx/credentials.yaml`, established interactively on first
run — and in a non-interactive create the credential is silently
withheld, which is exactly the failure mode that is hardest to debug.

Recommendation: start with the custom secret (§3.1 — nothing in the kit
changes, nothing to approve), and move to `credentials:` once the shape
has stopped moving. If first-run approval proves awkward, `wmf-sbx-create`
can write the binding itself.

## 4. Order of work

1. **Host-side token plumbing** (§2) — the precedence chain, the
   anonymous fallback, and the three never-leak rules. Unblocks all
   three open to-dos, and is independent of everything else in this
   document. *~1 day.*
2. **The three to-dos themselves** — bare-name search, `forked_from_project`
   / `mr_default_target_self`, and `local` remotes for non-Gerrit
   clones. Not designed here; each needs its own live-API research pass,
   and the third also implies a state file, daemon restart, and host-side
   remote rewrite, per its NOTES.md bullet.
3. **Sandbox-side access** (§3) — only once something actually needs the
   agent to talk to GitLab, and only if §5's `curl -I` says the MCP
   endpoint exists. *~1 day.*

Step 3 depends on `DESIGN-plugin-integration.md` having landed; steps 1
and 2 do not depend on it at all.

## 5. Open questions

Three of the four needed no host at all — they are questions about what
`gitlab.wikimedia.org` returns to a caller with no credential, and a
sandbox is a caller with no credential. Measured 2026-09-11 from
`wmf-claude-sbx` (`NOTES.md` §57.5).

1. Does `gitlab.wikimedia.org` answer `GET /api/v4/projects?search=…`
   anonymously? **Yes — 200, real results.** Lookup by URL-encoded path
   (`/api/v4/projects/repos%2F…%2Fwmf-claude`) works anonymously too,
   which is the call resolution actually wants. §2's no-token path is
   real, and stays a fully working path.
2. Does the WMF instance serve GitLab's MCP endpoint at all?
   **Yes, and it is token-gated.** `/api/v4/mcp` answers **401** — not
   404 — to both GET and POST. The endpoint exists; there is nothing
   anonymous to be had from it.
3. Is `glab` installed on the host, and authenticated? **Answered
   (2026-09-11): installed — 1.36.0 — but it knows only `gitlab.com` and
   has no token** (`x No token provided`). So it contributes nothing for
   `gitlab.wikimedia.org` today and §2's chain effectively starts at
   `$GITLAB_TOKEN`. Keep the `glab` step — it costs nothing and a user
   may authenticate later — but do not lean on it, and remember it needs
   `GITLAB_HOST` (or `--hostname`) to be asked about the WMF instance at
   all. (`glab` is *not* in the sbx base image — measured.)
4. Does `forked_from_project` appear for anonymous callers on a public
   fork? **Yes.** A public fork of our own repo returns
   `forked_from_project.path_with_namespace =
   repos/product-safety-and-integrity/wmf-claude` with no credential, so
   fork detection needs no token either.

## 6. Testing

- The precedence chain is a pure function over (environment, config,
  a `run=` hook): assert which source wins, and that the absent-token
  case returns cleanly rather than raising.
- **Assert the leak rules directly**: generate a kit and a plan with a
  token present in the environment, and assert the token string appears
  in neither the spec, nor `wmf-sbx-plan.json`, nor any command the
  code prints. This is the test most worth having.
- API-shape handling (search results, `forked_from_project`,
  `mr_default_target_self`) is table-driven against recorded JSON, as
  the Gerrit resolution tests already are — no live calls in the suite.

## 7. Non-goals

- No GitHub equivalent. sbx already injects GitHub credentials at the
  proxy, and nothing in the WMF workflow needs more than that.
- No write operations on the engineer's behalf from the host. Opening a
  merge request is the agent's job, from inside the sandbox, with the
  proxy holding the credential.
- No vendoring of `glab` into the kit. If in-sandbox GitLab access is
  wanted, §3.2's MCP endpoint is the lighter answer; revisit only if it
  turns out not to exist.
