# Handoff: core api-testing `ReparseDependentPages.js` fails with Parsoid read views

Found by the B4 blind run in a wmf-sbx sandbox (2026-10-14,
`sbx/NOTES.md`). Fix it in a sandbox of its own, then give the Gerrit
change and the Phabricator task to the wmf-sbx track, which records them
in `sbx/NOTES.md` and `sbx/upstream/README.md`.

## What fails

`npm run api-testing` in core: 18 failures. 17 are the known REST
`Content-Type` failures (`sbx/upstream/PHAB-TASK-1.md`). The 18th fails
every time (3 of 3 runs):

```
  18) Reparse of dependent pages
       should create a page with missing template and link:
     AssertionError: expected '<div class="mw-content-ltr mw-parser-…' to match /title=Template(:|%3A)TemplateXSpwgOUwgj&amp;action=edit&amp;redlink=1/
      at Context.<anonymous> (tests/api-testing/action/ReparseDependentPages.js:20:10)
```

What the wiki renders for a red-linked template:

```
href="http://localhost:4000/index.php/Template:TemplateNoSuchXyz?action=edit&amp;redlink=1"
```

Versions: core `977a18621a7`; Parsoid `ef5bb7151`.

## History

- `f1a7d0b5f6e` (2026-10-09, "Make tests agnostic to choice of parser
  (legacy or Parsoid)", T432762) changed the first assertion to
  `title=Template(:|%3A)${ template }&amp;action=edit&amp;redlink=1`.
  This accepts the encoded colon, but it still expects a `?title=` URL.
- `87bc95497cb` (merged 2026-10-12, "Set default for wgUseParsoidParser
  to true", T432762, T440814) made Parsoid render read views by default.

The legacy parser writes a red link as
`$wgScript?title=Template:X&action=edit&redlink=1`. In this sandbox,
Parsoid writes it as `$wgArticlePath` plus a query:
`/index.php/Template:X?action=edit&redlink=1`. The test's regex accepts
only the first form. The second assertion (the plain link,
`title=${ link }&amp;action=edit&amp;redlink=1`) has the same problem;
it does not run only because the first one fails.

## Find out first: why CI passes

`87bc95497cb` and the changes after it passed CI, so CI renders a form
that the regex accepts. Quibble's settings template
(`integration/quibble`, `quibble/mediawiki/local_settings.php.tpl`,
line 50) says "Parsoid does not yet work in Quibble", and it loads
Parsoid only when `services/parsoid` is cloned. So the likely answer: in
core's CI jobs the read view still comes from the legacy parser, and
this test has not run against Parsoid read views in CI. The other
possibility is the article path: the sandbox does not set
`$wgArticlePath`, and `composer serve` is PHP's built-in server, so it
is the path-info form `/index.php/$1`. Find out which before you write
the fix, and say it in the commit message. If CI does not run Parsoid
read views, this is a latent failure that CI will hit when Quibble
starts to use Parsoid, which is a better reason to fix it now.

The sandbox's settings are in `core/LocalSettings.php` (written by
wmf-sbx setup).

## Proposed fix

Make the two assertions accept both URL forms. A regex for the page part
of the link:

```js
const redlink = ( page ) => new RegExp(
	`(title=${ page }&amp;|/${ page }\\?)action=edit&amp;redlink=1`
);
assert.match( html, redlink( `Template(:|%3A)${ template }` ) );
assert.match( html, redlink( link ) );
```

An option that tests the meaning, not the form: parse the HTML, find the
`<a>` whose `title` attribute names the page, and check that it has the
`new` class and that its `href` has `action=edit` and `redlink=1`. Use
this if the reviewers prefer it.

## How to test

In a wmf-sbx sandbox (`$MW_INSTALL_PATH` is core):

```bash
cd "$MW_INSTALL_PATH"
(composer serve >/tmp/claude-$(id -u)/serve.log 2>&1 &)   # if the wiki is not running
npx mocha --timeout 0 tests/api-testing/action/ReparseDependentPages.js
# before the fix: 1 failing; after: passing
npm run api-testing   # 17 failures remain (the REST Content-Type ones)
```

(`npm run api-testing` is `mocha --timeout 0 --recursive --parallel
tests/api-testing`.) Also run it once with `$wgUseParsoidParser = false;` in
`LocalSettings.php`, to show that the legacy form still passes, and
remove the line again.

## Then

Give the wmf-sbx track the Gerrit change number and the Phabricator task
(T432762 may be the right parent). Until the fix is merged,
`sbx/templates/MEDIAWIKI-TESTING.md` §6 item 7 says to expect 18 failures
(`sbx/NOTES.md` has the to-do).
