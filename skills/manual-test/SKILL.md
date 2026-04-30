---
description: Walk Claude through manually testing a feature on the engineer's local MediaWiki instance via a browser-automation CLI. Use when the user wants screenshots, accessibility snapshots, or click-through verification of changes. Requires the user to have a browser-automation tool installed and a local wiki running.
argument-hint: "[feature-description or url-path]"
allowed-tools:
  - Bash(agent-browser *)
  - Bash(playwright *)
  - Bash(npx playwright *)
---

# Manual Test on a Local Wiki

Drive a browser against the engineer's local MediaWiki dev instance to test a feature. The skill is intentionally generic about the local URL, the wiki credentials, and the browser-automation tool — those vary by setup.

## Inputs the engineer must supply

Before invoking, the engineer should have set up:

1. **A browser-automation CLI in PATH.** Common choices: `agent-browser`, `playwright` / `npx playwright`. The skill assumes one of these is installed; if not, ask which tool the engineer uses.
2. **A running local wiki.** Could be MediaWiki-Docker, MWDD, MediaWiki-Vagrant, a bare-metal Apache/PHP setup, or a remote dev wiki. The base URL varies — common patterns include `http://localhost:8080`, `https://default.mediawiki.mwdd.localhost`, `http://dev.wiki.local.wmftest.net:8080`, or a custom domain like `https://en.mediawiki.localhost`.
3. **Credentials and the base URL recorded in the project's `CLAUDE.md`.** Look for a "Local development" or "Manual testing" section. If those aren't documented, ask the engineer for the URL and any login details before proceeding.

## Workflow

1. **Find the local wiki URL and credentials.** Check the project's `CLAUDE.md` first. If not documented, ask the engineer:
   - "What's the base URL of your local MediaWiki dev instance?"
   - "Which browser-automation tool do you use (agent-browser / playwright / other)?"
   - If authenticated testing is needed: "What username and password should I log in with?"

2. **Open the target page.** Combine the base URL with the path from `$ARGUMENTS` (or use `/wiki/Main_Page` as a default). Example with agent-browser:
   ```
   agent-browser open "<BASE_URL><path>"
   ```

3. **Capture state.** Take a screenshot, an accessibility snapshot, or page text — whichever is most useful for the feature being tested:
   ```
   agent-browser screenshot
   agent-browser snapshot          # accessibility tree with ref IDs
   agent-browser get text
   ```

4. **Authenticate if needed.** If the test requires a logged-in user:
   ```
   agent-browser open "<BASE_URL>/wiki/Special:UserLogin"
   agent-browser fill "Username" "<USER>"
   agent-browser fill "Password" "<PASS>"
   agent-browser click "Log in"
   ```

5. **Interact and verify.** Use `click`, `fill`, `hover`, `scroll`, `find role/text/label` etc. to drive the test. After each meaningful step, capture state so the engineer can see what happened.

6. **Report.** Summarize what was tested, what passed, what failed, and link to screenshots or HTML snippets. Note any unexpected behavior.

## Notes

- The browser-automation command surface (`agent-browser`, `playwright`, etc.) varies across tools. The examples above use `agent-browser` syntax; adapt the verbs to whatever the engineer's tool actually supports.
- If the engineer's local wiki uses a self-signed cert, they will have configured cert trust at the system level (`mkcert` is common). Don't pass `-k`/`--insecure` blindly — ask if a cert error appears.
- Some setups expose the API at `/w/api.php` (typical) but a few use `/api.php` (containerized). Don't assume.
- For headless runs, prefer the tool's headless flag rather than starting a visible browser.

## Input

`$ARGUMENTS`
