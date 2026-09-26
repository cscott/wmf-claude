Title: Popups: `npm run test:unit` runs no tests on Node 21 and later, and exits 0

## Summary

Popups pins `@wikimedia/mw-node-qunit` to exactly 7.0.0, whose `src/dom.js` does `global.navigator = global.window.navigator`. Node 21 and later define a read-only global `navigator`, so the setup throws `TypeError: Cannot set property navigator of #<Object> which has only a getter`, no test runs, and the output ends `0 passing (NaNms)`. The script pipes into `tap-mocha-reporter`, whose exit status is the one npm sees, so `test:unit`, `coverage` and `npm test` all exit 0.

## Technical notes

Popups' own `.nvmrc` asks for Node 24.14.1, so any run on the intended Node version hits this. Measured on Popups 7aaa530 with Node 22.22.1. With `NODE_OPTIONS=--no-experimental-global-navigator`, 203 tests pass in 2 s, which confirms the cause.

mw-node-qunit 7.10.0 (2026-04-13) fixes `dom.js` with `Object.defineProperty`. The attached patch (PHAB-ATTACHMENT-3.patch) bumps the pin to 7.10.0; regenerate `package-lock.json` with `npm install`. With the bump, the suite runs without the flag, but one test fails every time: `ext.popups/settingsDialogRenderer > #render` expects `$( el ).css( 'display' )` to be `none` after `hide()` and gets `block` (`tests/node-qunit/ui/settingsDialogRenderer.test.js:70`). This is likely a jsdom or jQuery change between the two mw-node-qunit versions, and the bump needs it fixed. `ext.popups/wait > it should resolve after waiting` failed once in three runs, which looks like timing flakiness.

A failing test does make the reporter exit 1; only a crash before any TAP output exits 0. `set -o pipefail` at the start of the script would catch that too (dash 0.5.12 and bash both support it).

## Acceptance criteria

- [ ] `npm run test:unit` runs all node-qunit tests on the Node version in `.nvmrc`, and they pass.
- [ ] A crash in the node-qunit setup makes `npm test` exit non-zero.
