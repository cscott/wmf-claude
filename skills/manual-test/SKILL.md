---
description: Verify changes against a local MediaWiki dev wiki, cheapest tier first. Tier 1 is curl + /w/api.php (no browser, no MCP) for HTTP status, redirects, rendered HTML, and API responses; escalate to the chrome-devtools MCP only when you need pixels, the accessibility tree, console errors, or real interaction. Use when the user wants to manually test or verify a change on their local wiki.
argument-hint: "[feature-description or url-path]"
allowed-tools:
  - Bash
  - Read
  - mcp__chrome-devtools__*
---

# Manual Test on a Local Wiki

Verify changes against the engineer's local MediaWiki dev instance. **Start with the cheapest tier that can answer the question and only escalate when it genuinely can't.** The chrome-devtools MCP is powerful but expensive: loading it puts ~25 tool definitions in context for the whole session, and every screenshot or accessibility snapshot is a large payload that stays resident until `/compact`. Most "did it render / is the value right / does the hook fire" checks never need it.

## Pick the tier

| Need | Tier | Cost |
|---|---|---|
| HTTP status, redirects, headers, rendered HTML, API responses, "does the hook fire", "does Special:X load" | **Tier 1 — curl + api.php** | cheap; plain `bin/claude --local-web`, no MCP, output is discardable Bash text |
| JS-rendered DOM state, `mw.config` values, client-side assertions | **Tier 2 — `evaluate_script`** returning JSON (not screenshots) | medium; `bin/claude --chrome` |
| Pixels, visual regressions, accessibility tree, console errors, network traces, click-through flows | **Tier 2 — screenshots / snapshots** | expensive; `bin/claude --chrome` |

Default to Tier 1. Escalate only when the answer requires a real browser.

---

## Tier 1 — curl + the action API

Requires the session launched with **`bin/claude --local-web`** (or `--chrome`, which implies it). A plain `bin/claude` session cannot reach the local wiki at all — nono blocks localhost web ports by default. If curl to the wiki fails instantly (connect refused in ~0ms) and the wiki is definitely running, the session wasn't launched with `--local-web`; tell the engineer to relaunch.

### Find the base URL

Check the project's `CLAUDE.md` for a "Local development" / "Manual testing" section. If absent, ask for the base URL. Common patterns: `https://en.mediawiki.localhost` (MWDD), `http://localhost:8080` (docker), `http://dev.wiki.local.wmftest.net:8080`.

### Reach a vhost wiki

Dev wikis are usually name-based vhosts on `127.0.0.1`, and outbound-by-hostname is routed through the profile's egress proxy (which only allows Wikimedia domains, so `*.localhost` gets a `403`). So connect to the loopback IP directly and carry the vhost in a `Host` header, which bypasses the proxy and hits the port `--local-web` opened:

```bash
# self-signed/mkcert cert -> -k; vhost via --resolve keeps SNI + Host correct
curl -sS -k --resolve en.mediawiki.localhost:443:127.0.0.1 \
  -o /dev/null -w "%{http_code}\n" https://en.mediawiki.localhost/wiki/Main_Page
```

If `--resolve` still gets a proxy `403`, fall back to a raw-IP request with an explicit Host header:

```bash
curl -sS -k -H "Host: en.mediawiki.localhost" https://127.0.0.1/wiki/Main_Page
```

Probe connectivity once before a batch of checks so you fail fast with a clear message rather than mid-workflow. `-k` (skip cert verification) is for the loopback dev wiki only — never use it against an external host.

### What Tier 1 is good for

- **HTTP behavior:** `-w "%{http_code} %{redirect_url}\n"` for status and redirects; `-D -` to dump headers (cache, `Content-Type`, a header a hook sets).
- **Rendered HTML:** `curl ... /wiki/PageName | grep` for an element your change emits. Enough to confirm a hook fired or a parser tag rendered.
- **The action/REST API** (`/w/api.php`, some setups `/api.php`, REST under `/w/rest.php`): drive logged-out or token flows, assert JSON with `jq`.

```bash
curl -sS -k --resolve en.mediawiki.localhost:443:127.0.0.1 \
  "https://en.mediawiki.localhost/w/api.php?action=query&meta=siteinfo&siprop=general&format=json" \
  | jq '.query.general.generator'
```

For authenticated API testing, use `action=query&meta=tokens` plus a login/edit flow over a cookie jar (`-c`/`-b`). Use a `mktemp` jar (it holds a live session token) and don't write it into the workdir. Throwaway dev-wiki account only — see the credentials note below.

---

## Tier 2 — chrome-devtools MCP (use frugally)

Only when Tier 1 can't answer it: visual output, accessibility tree, console errors, network waterfalls, or interaction that depends on rendered/JS state.

### Prerequisites

1. **MCP loaded.** Only present when launched with `bin/claude --chrome`. If `mcp__chrome-devtools__*` tools aren't available, stop and tell the engineer to relaunch with `--chrome` (and to have run `setup.sh`'s chrome-devtools step).
2. **Chrome running in attach mode.** Chrome can't run inside the sandbox (IOKit denied — it segfaults). The engineer must run `bin/launch-test-chrome` in another terminal first; it starts a dedicated Chrome with a fresh `mktemp -d` user-data-dir on `--remote-debugging-port=9222`. If the MCP can't connect, ask them to start it.
3. **A running local wiki** and its base URL + credentials (project `CLAUDE.md`, else ask).

### Frugality rules (this is where the token cost lives)

- **DOM/`evaluate_script` over screenshots.** A screenshot is a large image that never leaves context; `evaluate_script` returning JSON is a few hundred tokens. Reserve `take_screenshot` for when *pixels* are the actual question (visual regression, layout, CSS). For "is this element present / what's its text / what's `mw.config`", evaluate JS instead:

  ```javascript
  () => ({
    title: document.title,
    user: mw.config.get('wgUserName'),
    heading: document.querySelector('#firstHeading')?.textContent?.trim(),
    errors: Array.from(document.querySelectorAll('.error')).map(e => e.textContent.trim())
  })
  ```

- **Investigate before interacting.** One structural probe (form/button/input counts plus a slice of relevant HTML) instead of repeated snapshots.
- **Batch interactions in a single call.** Wrap multi-step DOM work in one `evaluate_script` IIFE returning the final state, rather than a tool call per click.
- **`take_snapshot` only when you need element refs** to drive `click`/`fill_form` where JS-built refs aren't predictable. Re-snapshot only after the DOM actually changes — refs aren't stable across navigations/mutations.
- **`list_console_messages` / `list_network_requests`** when the question is errors or request behavior — far cheaper than screenshots and often more informative.
- **`/compact` after a screenshot-heavy stretch** to flush the image payloads before continuing.

### Authenticate (when needed)

Navigate to `<BASE_URL>/wiki/Special:UserLogin`, `take_snapshot` for the username/password refs, `fill_form` both, then `click` submit. Or log in over the API in Tier 1 and only bring the browser up for the part that needs it.

---

## Security notes (both tiers)

- **`--local-web` opens localhost web ports only.** It does not widen external network access — outbound to the internet stays gated by the profile's domain allowlist. It does let the sandboxed agent reach *any* local service on 80/443/8080, so don't point curl at local services the engineer didn't ask about.
- **The attached Chrome's network is NOT sandboxed.** Pages it loads (and `evaluate_script` `fetch()`) can reach anywhere. Only navigate to URLs the engineer authorized — their local wiki, Wikimedia domains. Never arbitrary external sites, and no outbound `fetch()` the engineer didn't ask for.
- **CDP on `127.0.0.1:9222` is unauthenticated**, and anything you submit via `fill_form` / `evaluate_script` (passwords, tokens) is in the clear on localhost. **Use throwaway dev-wiki credentials only** — never a real Wikimedia password.
- The test Chrome uses a fresh per-launch user-data-dir (no cookies/logins/history from the engineer's real browsing) and is removed on exit. Don't suggest logging into personal accounts in it.
- If the local wiki uses a self-signed cert and Chrome blocks the page, surface that to the engineer (they need `mkcert`/system trust) rather than working around it. The MCP exposes no `--insecure` toggle.

## Report

Summarize what was tested at which tier, what passed, what failed. Quote relevant console errors, failed requests, or non-2xx statuses. Note anything unexpected.

## Input

`$ARGUMENTS`
