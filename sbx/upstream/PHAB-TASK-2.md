Title: wdio-mediawiki: An empty MW_SCRIPT_PATH is rejected, although it is correct for a docroot install

## Summary

`wdio-defaults.conf.js` throws `MW_SERVER or MW_SCRIPT_PATH not defined` when `MW_SCRIPT_PATH` is the empty string, because it tests `!process.env.MW_SCRIPT_PATH`. The empty string is the correct value for a wiki at the docroot, which includes `composer serve` (Quickstart), and core's `Gruntfile.js` accepts it for that reason.

## Technical notes

Core's Gruntfile tests `process.env.MW_SCRIPT_PATH === undefined`, with the comment "MW_SCRIPT_PATH= empty string is valid, e.g. for docroot installs … This includes composer serve (Quickstart)". The attached patch (PHAB-ATTACHMENT-2.patch, against core's `tests/selenium/wdio-mediawiki/`) makes wdio-mediawiki use the same test. `baseUrl` is `MW_SERVER + MW_SCRIPT_PATH`, which is correct with an empty script path. Measured with the patched file (wdio-mediawiki 6.5.2) and `MW_SCRIPT_PATH=` against `composer serve`: core's `tests/selenium/specs/page.js` passes, 6 tests. The workaround today is `MW_SCRIPT_PATH=/`.

## Acceptance criteria

- [ ] wdio-mediawiki accepts `MW_SCRIPT_PATH=` and still rejects an unset `MW_SCRIPT_PATH`.
- [ ] A wdio-mediawiki release with the fix is published to npm.
