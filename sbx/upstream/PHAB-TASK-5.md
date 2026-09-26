Title: wmf-claude: run-tests skill teaches `vendor/bin/phpunit`, which fails on a checkout with no `phpunit.xml`

## Summary

`skills/run-tests/SKILL.md` tells Claude to run `vendor/bin/phpunit <path>`, and its `allowed-tools` permits only that form. Core ships `phpunit.xml.template`, not a `phpunit.xml.dist`, so on a checkout that has never run `composer phpunit:entrypoint`, PHPUnit loads no bootstrap and stops with `Class "MediaWikiUnitTestCase" not found`, which reads like a broken checkout.

## Technical notes

`composer phpunit:entrypoint -- <path>` writes `phpunit.xml` when it is missing and then runs PHPUnit, so it works on a fresh checkout and on an old one alike. The attached patch (PHAB-ATTACHMENT-5.patch, which applies to `main`) makes the entrypoint the default form, allows it in `allowed-tools` (also with the `TMPDIR=` and `MW_SKIP_EXTERNAL_DEPENDENCIES=` prefixes), keeps the direct form for projects that commit their own `phpunit.xml`, and updates the `mwdocker` examples. The sbx backend ships this patch today (`sbx/patches/plugin/01-run-tests-composer-entrypoint.patch`) and can drop it when upstream takes it. `skills/test-coverage/SKILL.md`, `agents/test-writer.md`, `templates/mediawiki/CLAUDE.md` and `templates/mediawiki/settings.json` teach or allow the same direct form and need the same change.

## Acceptance criteria

- [ ] The run-tests skill runs a test file on a core checkout that has no `phpunit.xml`.
- [ ] test-coverage, test-writer and the mediawiki templates use and allow the same form.
