---
name: install-missing-test-tools
description: "When a test tool (mocha etc.) is missing, install it and run the tests; never skip the suite. Report the missing dependency too."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: ee1a7641-c07b-449a-884c-2304ba223aed
  modified: 2026-09-18T12:31:09.527Z
---

If a repo's test script fails because a tool is missing (e.g. Flow's `npm run api-testing` → `mocha: not found`), install it (`npm install --no-save <pkg>`, which leaves package.json and the lock file alone) and run the tests. Also flag the missing dependency, but don't let it block testing. This applies to guidance written for agents too (sbx `MEDIAWIKI-TESTING.md`).

**Why:** the user said skipping expected tests because a tool wasn't installed leads to buggy code; "no user wants buggy code written just because the agent couldn't be bothered to install the needed testing tool."

**How to apply:** lean toward installing packages so expected tests run, rather than skipping and reporting. Cypress is supported: the kit allows `download.cypress.io` and `cdn.cypress.io`, and `mw-install-cypress <repo>` installs it on demand (wmf-claude sbx/NOTES.md §92, acceptance run §93). Also check the pass count, not only exit status: Popups' mw-node-qunit on Node 22 exits 0 with 0 tests (fix: NODE_OPTIONS=--no-experimental-global-navigator).
