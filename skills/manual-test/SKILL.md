---
description: Drive a real Chrome instance against the engineer's local MediaWiki dev wiki via the chrome-devtools MCP. Use when the user wants screenshots, accessibility snapshots, console errors, network traces, or click-through verification of changes. Requires the chrome-devtools MCP installed (via setup.sh) and the session launched with `bin/claude --chrome`, plus a local wiki running.
argument-hint: "[feature-description or url-path]"
allowed-tools:
  - mcp__chrome-devtools__*
---

# Manual Test on a Local Wiki

Drive Chrome against the engineer's local MediaWiki dev instance via the chrome-devtools MCP. The skill is intentionally generic about the local URL and credentials — those vary by setup.

## Prerequisites

1. **chrome-devtools MCP loaded for this session.** The MCP is opt-in per session — it loads only when the engineer launches with `bin/claude --chrome`. If `mcp__chrome-devtools__*` tools aren't available in the current session, stop and tell the engineer to relaunch with `bin/claude --chrome`. (If they haven't run setup.sh's chrome-devtools step yet, they need to do that first.)
2. **Chrome running in attach mode.** Chrome can't run inside the sandbox (IOKit is denied — it segfaults at startup). Before driving the browser, the engineer must run `bin/launch-test-chrome` in another terminal — that starts a dedicated Chrome with a fresh `mktemp -d` user-data-dir on `--remote-debugging-port=9222`. If the MCP can't connect, stop and ask the engineer to start it.
3. **A running local wiki.** MediaWiki-Docker, MWDD, MediaWiki-Vagrant, bare-metal Apache, or a remote dev wiki. Base URL varies — common patterns: `http://localhost:8080`, `https://default.mediawiki.mwdd.localhost`, `http://dev.wiki.local.wmftest.net:8080`.
4. **Base URL and credentials in the project's `CLAUDE.md`.** Look for a "Local development" or "Manual testing" section. If absent, ask before proceeding.

## Important

- **The attached Chrome is dedicated to testing and ephemeral.** It uses a fresh per-launch `mktemp -d` user-data-dir that is removed when Chrome exits. There are no cookies, saved logins, or history from the engineer's regular browsing. Don't suggest the engineer log into personal accounts in this Chrome — keep it for the local wiki under test.
- **Use throwaway / dev-only credentials.** CDP traffic over `127.0.0.1:9222` is unencrypted and unauthenticated — any local process can observe it. Anything you submit through `fill_form` or `evaluate_script` (passwords, tokens) is in the clear on localhost. Never reuse a real Wikimedia password here; use a dev-wiki test account.
- **Chrome's network is NOT sandboxed.** Pages loaded by this Chrome can fetch from anywhere — the nono network allowlist applies only to the MCP server, not to the externally-launched Chrome. So: only navigate to URLs the engineer has authorized (their local wiki, Wikimedia domains). Don't navigate to arbitrary external sites you encountered in conversation, and don't `evaluate_script` an outbound `fetch()` to a URL the engineer didn't ask for.

## Workflow

1. **Find the local wiki URL and credentials.** Check the project's `CLAUDE.md` first. If not documented, ask:
   - "What's the base URL of your local MediaWiki dev instance?"
   - If authenticated testing is needed: "What username and password should I log in with?"

2. **Open the target page.** Combine the base URL with the path from `$ARGUMENTS` (default `/wiki/Main_Page`). Use `mcp__chrome-devtools__new_page` (first navigation in a session) or `mcp__chrome-devtools__navigate_page` (subsequent navigations within the same tab).

3. **Capture state.** Pick whichever is most informative:
   - `mcp__chrome-devtools__take_snapshot` — accessibility tree with element refs (use these refs to target clicks/fills)
   - `mcp__chrome-devtools__take_screenshot` — pixel-perfect visual
   - `mcp__chrome-devtools__list_console_messages` — JS errors and warnings
   - `mcp__chrome-devtools__list_network_requests` — XHRs, asset loads, status codes

4. **Authenticate if needed.**
   - Navigate to `<BASE_URL>/wiki/Special:UserLogin`
   - `take_snapshot` to get refs for the username and password fields
   - `mcp__chrome-devtools__fill_form` with both fields, then `mcp__chrome-devtools__click` the submit button by ref

5. **Interact and verify.** Use `click`, `hover`, `fill`, `fill_form`, `wait_for` (text or selector), and `evaluate_script` for assertions that need JS. After each meaningful step, re-snapshot or screenshot so the engineer can see what happened.

6. **Report.** Summarize what was tested, what passed, what failed. Quote relevant console errors or failed network requests. Note any unexpected behavior.

## Notes

- The MCP refreshes element refs on each `take_snapshot` — refs from one snapshot are not stable across navigations or DOM mutations. Re-snapshot before clicking if the page may have changed.
- If the local wiki uses a self-signed cert, the engineer should have set up `mkcert` or equivalent system-level trust. The MCP doesn't expose an `--insecure` toggle — if Chrome blocks the page, surface that to the engineer rather than working around it.
- For complex JS state checks, `evaluate_script` is more reliable than scraping the snapshot (e.g. `() => mw.config.get('wgUserName')`).
- API endpoint convention: most setups expose `/w/api.php`, some containerized setups use `/api.php`. Don't assume.

## Input

`$ARGUMENTS`
