# Running MediaWiki tests in this sandbox

`~/.claude/CLAUDE.md` has the short version. This file is the detail: what
each runner needs, what it costs, and the things that waste your time if you
guess instead of reading.

Every sandbox holds a different set of repos. Read §1 to find out which ones
you have, then read the section for the kind of repo you are changing:

- **If you are working on mediawiki-core**, read §2, §5 and §6.
- **If you are working on an extension or a skin**, read §3, §5 and §6.
- **If you are working on Parsoid**, read §4 and §6. If your change can alter
  what Parsoid renders for an extension, read the parser-test part of §3 too.

Each command was run end to end in a `wmf-sbx create` sandbox holding
Translate, Cite and Parsoid, plus the `core`, `Vector` and
`UniversalLanguageSelector` checkouts the dependency walk added. The timings
come from that run, on a 16-core host, with caches warm. Where a command was
*not* measured, this file says so. The test counts move a little with every
upstream commit, so a count that is off by one is not a finding.

## 0. What the sandbox already did for you

At `sbx create` time:

- Each repo you were named was cloned from the engineer's own checkout, at
  the branch and commit they had. Repos pulled in only as dependencies were
  reset to upstream `master`.
- `composer update` ran in every checked-out repo, so each has a `vendor/`.
  Core's also holds every extension's and skin's libraries, merged in
  through `composer.local.json` as CI does.
- `npm ci` ran in every checked-out repo that has a `package.json`.
- Every extension and skin in the closure is symlinked into
  `$MW_INSTALL_PATH/extensions/` or `$MW_INSTALL_PATH/skins/`, and
  `LocalSettings.php` loads it.
- A SQLite wiki is installed, as a *docroot* install: URLs look like
  `http://localhost:4000/index.php?...`, with no `/w` prefix. The admin
  account is `Admin` / `adminpassword`.
- These are in the environment:

  | Variable | Value | Read by |
  |---|---|---|
  | `MW_INSTALL_PATH`, `MW_CORE_REPO` | the core checkout | you, and many scripts |
  | `PARSOID` | the Parsoid checkout | you |
  | `MW_SERVER` | `http://localhost:4000` | karma, wdio |
  | `MW_SCRIPT_PATH` | `/` | karma, wdio |
  | `MEDIAWIKI_USER`, `MEDIAWIKI_PASSWORD` | `Admin`, `adminpassword` | wdio |
  | `CHROME_BIN` | `/usr/bin/chromium` | karma |
  | `API_TESTING_CONFIG_FILE` | `$MW_INSTALL_PATH/.api-testing.config.json` | api-testing |
  | `MEDIAWIKI_HAS_INTEGRATION_TESTS` | `1`, only with a Parsoid clone (§4) | core's PHPUnit bootstrap |

  `MW_SCRIPT_PATH` is `/`, not the empty string, although the wiki's own
  `$wgScriptPath` is empty. This is what WMF CI (Quibble) does for its own
  docroot install. The resulting `http://localhost:4000//index.php` double
  slash is harmless. Do not change it: an empty value makes selenium refuse
  to start (§6).

If a `vendor/` or `node_modules/` is missing, the setup log
(`~/.wmf-sbx/wmf-sbx-setup.log`) says why. You do not need to install,
configure, or symlink anything else before you run a PHP test.

## 1. First: find out what you were given

```bash
cd <repo>
git log --oneline origin/master..HEAD   # local commits, if any
git status --short                      # uncommitted changes, if any
```

Then run the relevant suite **once, before your change**, so you know which
failures were there already. Core, Parsoid and each extension sit at their
own tip, in combinations CI never builds together. A failure on the tree you
were given is not proof that the sandbox is broken, and it is not proof that
you caused it.

Each Bash tool call starts again in your primary repo: a `cd` does not
carry over to the next call. Put the `cd` in every command that needs it,
as the examples here do.

## 2. If you are working on mediawiki-core

### PHPUnit

Run from `$MW_INSTALL_PATH`, through composer:

```bash
cd "$MW_INSTALL_PATH"
composer phpunit:entrypoint -- tests/phpunit/unit/includes/FooTest.php
composer phpunit:entrypoint -- --filter testBar tests/phpunit/integration/includes/BarTest.php
composer phpunit:entrypoint -- --testsuite=structure     # 1712 tests, 30 s
```

**Run PHPUnit suites one at a time against a core checkout.** Every
`composer phpunit:entrypoint` run clones its test tables into the same
SQLite database, under the same `unittest_` prefix. Two runs at once drop
each other's tables, and the second one fails with errors like
`no such table: unittest_searchindex` in `CloneDatabase::destroy`. That
looks like a bug in the suite you ran. Run it again by itself before you
believe it. Lint, phan, Jest and the tests of another repo's own runner do
not share that database, so they can run beside PHPUnit, and beside each
other in different repos.

**Do not reach for `vendor/bin/phpunit <path>` first.** Core ships
`phpunit.xml.template`, not a `.dist`. Without a generated `phpunit.xml`,
PHPUnit loads no bootstrap and dies with
`Class "MediaWikiUnitTestCase" not found`, which looks like a broken checkout.
`composer phpunit:entrypoint` runs `composer phpunit:config` first. After that
file exists `vendor/bin/phpunit <path>` works too, but the file hard-codes
the path of every extension's test directory: run `composer phpunit:config`
again after the set of extensions changes.

| Suite | What it covers | Cost |
|---|---|---|
| `--testsuite=core:unit` | core's `tests/phpunit/unit` | 18740 tests, 9 s |
| `--testsuite=structure` | structure checks on core *and* every loaded extension | 1712 tests, 30 s |
| `--testsuite=parsertests` | every parser-test file, core's and each extension's | 2404 tests, 13 s |

### Lint and phan

```bash
cd "$MW_INSTALL_PATH"
composer test     # parallel-lint, phpcs, minus-x
npm test          # grunt lint (eslint, stylelint, banana), then jsdoc, then Jest -- 60 s
composer phan     # 80 to 110 s; clean on an upstream tree, exit 0
```

Core's phan has none of the sibling-extension noise of §3: a finding in
core is real.

### Jest

Core's Jest suite runs in Node, with `mw` mocked by
`tests/jest/jest.setup.js`. No wiki and no browser:

```bash
cd "$MW_INSTALL_PATH"
npm run jest                                   # 178 tests, 23 s
npm run jest -- tests/jest/mediawiki.special.block
```

Jest is the recommended framework for new Vue.js code; QUnit stays for
existing code ([T334642](https://phabricator.wikimedia.org/T334642),
[mediawiki.org/wiki/Jest](https://www.mediawiki.org/wiki/Jest)). A new core
suite must also be added to `roots` in `tests/jest/jest.config.js`, or Jest
does not find it.

### QUnit, selenium and api-testing

All three drive the running wiki. See §5 for setup, then:

```bash
cd "$MW_INSTALL_PATH"
npx grunt karma:chrome --qunit-component=MediaWiki
CI=true npx wdio ./tests/selenium/wdio.conf.js --spec tests/selenium/specs/page.js   # 6 tests, 15 s
npx mocha --timeout 0 tests/api-testing/action/Edit.js                       # 23 tests, 3 s
```

Drop `--spec` (or the file argument) to run everything; `npm run
selenium-test` and `npm run api-testing` are the forms CI runs. Core's full
`npm run api-testing` has 17 failures here that are not yours: see §6.

## 3. If you are working on an extension or a skin

The PHP tests run **from core**. The JS tooling runs **from your repo**,
except QUnit, which runs from core. `<Name>` below is the directory name
under `extensions/` or `skins/` (`Translate`, `Cite`, `Vector`).

No list says which suites a repo has. Find out from the repo itself:
`node -p 'require("./package.json").scripts'`, `composer.json`'s
`scripts`, the directories under `tests/`, and `QUnitTestModule` in
`extension.json`. Each section below says what to look for.

### PHPUnit and parser tests — from core

```bash
cd "$MW_INSTALL_PATH"
composer phpunit:entrypoint -- extensions/<Name>/tests/phpunit
composer phpunit:entrypoint -- --filter testFoo extensions/<Name>/tests/phpunit/unit/FooTest.php
composer phpunit:entrypoint -- skins/<Name>/tests/phpunit
```

Measured: Cite unit 140 tests in 2.0 s, Cite integration 112 tests in 0.9 s,
all of Translate 1090 tests in 4.1 s, Vector 155 tests in 0.8 s. Extension
suites take seconds, so run the whole suite rather than hand-picking files.
Read §2 for why not `vendor/bin/phpunit`.

Also run `--testsuite=structure` (30 s). It checks *your* `extension.json`,
autoloading, i18n and ResourceLoader modules.

If the repo has `tests/parser/*.txt`, those are compiled into generated test
classes under core's `tests/phpunit/gen/`:

```bash
ls "$MW_INSTALL_PATH"/tests/phpunit/gen/ | grep <Name>
composer phpunit:entrypoint -- tests/phpunit/gen/ParserTest_Cite_1_citeParserTests_Test.php   # 1151 tests, 29 s
```

### Lint and phan — from your repo

```bash
cd <repo>
composer test     # parallel-lint, phpcs, minus-x   (Cite: 0.8 s)
composer fix      # phpcbf, minus-x fix
npm test          # usually grunt: eslint, stylelint, banana   (Translate: 9 s)
composer phan     # Cite: 21 s -- but read the phan note below first
```

**phan: expect undeclared-symbol errors that are not yours.** This is a
temporary gap in the sandbox, not a problem with your tree. An extension's
`.phan/config.php` can name sibling extensions it expects on disk. Cite's
adds `../../extensions/cldr`, `../../extensions/CommunityConfiguration` and
`../../extensions/Gadgets` to `directory_list`. The sandbox clones what
`extension.json` requires, and it does not require those, so they are
absent. Result: 32 `PhanUndeclared*` errors in Cite on a pristine upstream
tree (PageImages 5, Popups 4, Vector 4, TextExtracts 0). Read `.phan/config.php` to see which names it expects, and either
ignore errors that name exactly those classes or run phan once before your
change and diff. A missing sibling can also make a `@phan-suppress` unused:
Translate reports `UnusedPluginSuppression` at
`src/TtmServer/ElasticSearchTtmServer.php:177`, because without Elastica
phan cannot type the loop that defines the variable.

The same gap shows outside phan too. A module or a test that needs an
extension the sandbox does not hold fails or is skipped:

- Core's `--testsuite=structure` fails
  `ResourcesTest::testValidDependencies` when a loaded extension has a
  ResourceLoader module that depends on an absent one (Flow's
  `ext.flow.visualEditor` names VisualEditor's modules).
- A selenium spec can pass while it skips every test in it
  (TemplateData's needs VisualEditor). Read the skip count, not only the
  pass count.

Check each such failure against the pristine run from §1 before you
report it, and name the missing extension when you do.

Composer libraries are not a cause here. As in CI, core's
`composer.local.json` merges every `extensions/*/composer.json` and
`skins/*/composer.json` into core's `vendor/`, and phan reads core's
`vendor/`. If you add a library to an extension's `composer.json`, run
`composer update` in core too (and in the extension, for its own
`vendor/bin`), or phan reports the library's classes as undeclared.

### JS dependencies

`npm ci` already ran here at create time. If you must run it again (after a
`package.json` change, say), use:

```bash
cd <repo> && CYPRESS_INSTALL_BINARY=0 npm ci
```

Keep the variable where cypress is a devDependency (Cite, and many
others). Without it, `npm ci` downloads the Cypress binary, about 800 MB,
which most tasks never use. If you need Cypress, `mw-install-cypress`
installs it (§5.6).

### Jest — from your repo, only if `package.json` wires it

Core's Jest config ignores `/extensions/` and `/skins/`, so `npm run jest`
in core never runs your tests. A repo that uses Jest has its own config and
its own npm script. Find it before running anything:

```bash
cd <repo>
node -p 'require("./package.json").scripts'
```

Common names are `jest`, `test:unit` and `test:jest`; some repos call Jest
from `npm test`. Measured: Vector `npm run test:unit`, 79 tests, 7.8 s.

Some repos run QUnit tests under Node, not in a browser, with
`mw-node-qunit` (Popups: `npm run test:unit`, also part of its `npm test`).
**On this sandbox's Node 22 it crashes and still exits 0.** Node 22 has a
read-only global `navigator`, `mw-node-qunit` 7.0.0 assigns to it, and the
script pipes the output into `tap-mocha-reporter`, whose exit status is
the one you see. The log shows `Cannot set property navigator of
#<Object> which has only a getter` and ends with `0 passing (NaNms)`. Turn
the global off and the tests run:

```bash
NODE_OPTIONS=--no-experimental-global-navigator npm run test:unit   # Popups: 203 tests, 2 s
```

Read the count at the end, not only the exit status, of any script that
pipes into a reporter.

A `tests/jest/` directory is not proof that there is a runner. Translate has
four Jest test files but no Jest dependency and no script, and CI does not
run them. Forced through core's config, some of them fail. If you find that
situation, say so; do not report those tests as passing or as run.

### QUnit — from core

```bash
cd "$MW_INSTALL_PATH"
npx grunt karma:chrome --qunit-component=<Name>    # Translate: 9 tests, 3 s
```

This needs the browser and the running wiki from §5. The karma config lives
in core's `Gruntfile.js`; your repo's `Gruntfile.js` only lints. Your
tests run only if `extension.json` registers them under `QUnitTestModule`.
To confirm that the wiki sees them before you debug a zero-test run:

```bash
curl -s --noproxy '*' "$MW_SERVER$MW_SCRIPT_PATH/index.php?title=Special:JavaScriptTest/qunit/export&component=<Name>" | grep -c 'test\.'
```

That URL returns a small bootstrap that loads the real test modules over
ResourceLoader, so expect module names in it, not test source.

### Selenium and api-testing — from your repo, if it has them

CI runs `npm run selenium-test` and `npm run api-testing` in every repo that
defines them. Both need §5. Read the script before you run it: the name
does not tell you the harness. Parsoid has an api-testing suite of its own
(§4). Popups' `selenium-test` runs wdio; run it as §5.4 says, from the
repo:

```bash
cd <repo> && CI=true npx wdio tests/selenium/wdio.conf.js   # Popups: 3 tests, 7 s
```

Cite's `selenium-test` runs **Cypress**, which needs its own
install and often needs other extensions (§5.6).

**If a test's tool is missing, install it and run the tests.** Do not skip
a suite because the repo forgot a devDependency. Flow's `api-testing` runs
`mocha`, which is not in Flow's `package.json`, so after a clean `npm ci`
it fails with `sh: 1: mocha: not found`. Install it without a change to
`package.json` or the lock file, then run the script:

```bash
cd <repo>
npm install --no-save mocha
npm run api-testing
```

A later `npm ci` removes it again. Report the missing dependency too: it
belongs in the repo's `package.json`. Keep that out of your change unless
the task is about it.

## 4. If you are working on Parsoid

Parsoid has a real `phpunit.xml.dist` and its own suites. Run them from the
Parsoid checkout; they need nothing from core:

```bash
cd "$PARSOID"
composer phpunit        # 9314 tests, 2.7 s
composer parserTests    # 31031 tests, 70 s
composer lint           # parallel-lint, phpcs, minus-x, covers-validator, bin/*check.sh -- 3 s
composer test           # lint + phan + phpunit + toolcheck + parserTests; phan makes it minutes
npm test                # 7.5 s
npm run api-testing     # needs the wiki (§5.1): 87 passing, 48 pending, 3 s
```

`composer parserTests` compares against the committed known-failures lists.
A clean run ends with `--> NO UNEXPECTED RESULTS <--` and still reports
thousands of "failures". That line is the pass/fail signal; the counts are
not.

**Unit tests of core and of extensions use your Parsoid only because of
`MEDIAWIKI_HAS_INTEGRATION_TESTS=1`.** `LocalSettings.php` points the wiki
at `$PARSOID`, not at core's `vendor/wikimedia/parsoid`. But core's PHPUnit
bootstrap skips `LocalSettings.php` when it thinks a run holds unit tests
only (a path under `/unit/`, or `--testsuite=core:unit`), and those runs
then use the vendor copy. The variable, set in this sandbox's environment,
turns that guess off, as
[mediawiki.org/wiki/Parsoid](https://www.mediawiki.org/wiki/Parsoid) says to
do. Do not unset it. In a sandbox without a Parsoid clone it is not set,
and the vendor copy is the right one.

Parsoid is also loaded into the wiki as an extension, and core's and each
extension's parser tests run through it. So a Parsoid change can break
`tests/phpunit/gen/ParserTest_Cite_*` in core (§3) without breaking
anything in `$PARSOID`. If the sandbox holds extensions with parser tests,
run theirs too.

## 5. Tests that drive the wiki or a browser

QUnit, selenium and api-testing all talk to a running wiki over HTTP.
QUnit and selenium also need Chrome.

### 5.1 Start the wiki

```bash
cd "$MW_INSTALL_PATH"
url="$MW_SERVER/index.php?title=Special:Version"
if curl -s --noproxy '*' -o /dev/null "$url"; then
  echo "already running"                 # from earlier in the session: use it
else
  composer serve > "$TMPDIR/mw-serve.log" 2>&1 &
  pid=$!
  # The server takes a few seconds to answer the first time; a curl right
  # after the `&` gets "connection refused" (000, exit 7). Poll, do not sleep:
  for i in $(seq 30); do
    code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "$url")
    [ "$code" = 200 ] && break
    kill -0 "$pid" 2>/dev/null || { cat "$TMPDIR/mw-serve.log"; break; }   # it died
    sleep 1
  done
  echo "$code"   # 200
fi
```

Leave it running, and do not start a second one. A second `composer serve`
exits at once with `Failed to listen on 127.0.0.1:4000 (reason: Address
already in use)`, while a plain poll still gets 200 from the first. Its
log is `$TMPDIR/mw-serve.log`. Keep logs and scratch files in `$TMPDIR`:
the sandbox lets you write `/tmp` but not read it back.

### 5.2 Install Chrome, once per sandbox

```bash
mw-install-browser        # 20 to 50 s, about 420 MB
```

No browser is in the image. `mw-install-browser` installs the current
stable Chrome for Testing and its chromedriver in `~/.cache`, and checks
that they start. `/usr/bin/chromium` and `/usr/bin/chromedriver`, the paths
WMF CI uses, run them. You need no sudo, and the libraries are already in
the image: if the script says one is missing, report it and stop. MediaWiki
pins no browser version: CI uses whatever Chromium its image has, and wdio
asks for the current stable release.

`/usr/bin/chromium` always adds `--no-sandbox`. Chrome's own sandbox cannot
start inside this sandbox, which already contains the browser. A Chrome
that you start by another path stops with `No usable sandbox` unless you
pass `--no-sandbox` yourself.

Test that it works before you debug a browser suite:
`/usr/bin/chromium --headless --dump-dom about:blank`.

Chrome prints `ERROR:dbus/bus.cc ... Failed to connect to the bus` lines
on stderr, in this test and in every browser suite. They are harmless:
the sandbox has no D-Bus, and Chrome does not need it. Ignore them.

### 5.3 QUnit (karma)

```bash
cd "$MW_INSTALL_PATH"
npx grunt karma:chrome --qunit-component=<Name>   # MediaWiki for core
```

- Use `karma:chrome`, not `qunit`. `grunt qunit` is
  `[ 'assert-mw-env', 'karma:firefox' ]`, and there is no Firefox here.
- Without `--qunit-component` you get core's entire suite as well.
- karma finds Chrome through `$CHROME_BIN` only. If that is unset, it fails
  with `No binary for ChromeHeadless browser on your platform`.
- No `CHROMIUM_FLAGS` are necessary here: `$CHROME_BIN` adds `--no-sandbox`
  (§5.2).

### 5.4 Selenium (wdio)

```bash
cd "$MW_INSTALL_PATH"
CI=true npx wdio ./tests/selenium/wdio.conf.js --spec tests/selenium/specs/page.js
```

**Always set `CI=true` for wdio.** Then `wdio-mediawiki` uses the Chrome
and chromedriver from §5.2 and passes `--no-sandbox`. Without it, wdio
downloads its own Chrome into `$TMPDIR` and starts it without
`--no-sandbox`, and that Chrome stops at once (`No usable sandbox`, then
`Chrome instance exited`). `CI=true` also raises `maxInstances` to 75% of
the CPUs.

The environment already supplies the rest of what `wdio-mediawiki` reads.
It runs headless because `DISPLAY` is unset. Failed commands write
screenshots to `tests/selenium/log/`.

### 5.5 api-testing

```bash
cd "$MW_INSTALL_PATH"
npx mocha --timeout 0 tests/api-testing/action/Edit.js   # 23 tests, 3 s
```

The config is `.api-testing.config.json` in core, which setup wrote and
core's `.gitignore` hides. `$API_TESTING_CONFIG_FILE` names it by absolute
path, so it works from any repo, not only from core. Do not copy it into
another repo, whose `.gitignore` may not list it. If you ever write one yourself, `base_uri` is the wiki root,
`http://localhost:4000/`, **not** `.../api.php`. With the wrong value, every
test dies in a `before all` hook with
`Cannot read properties of undefined (reading 'tokens')`, which looks like
an authentication problem.

These tests edit the live dev wiki. That is expected.

### 5.6 Cypress, only when it covers your change

Some repos run Cypress, not wdio, as `npm run selenium-test` (Cite, for
one; read the script). The npm package is installed, but its binary is not.
It is about 800 MB, so **decide first if you need it.** Install it when
your change is likely to be covered by that repo's Cypress specs: look in
the repo's Cypress spec directory (Cite: `tests/cypress/e2e/`) for specs
about the code you changed. If there are none, skip Cypress and say so in
your report. Other suites (PHPUnit, QUnit, Jest) are not a reason to install
it.

Many specs need other extensions too. Cite's specs need Popups (reference
previews) or VisualEditor, and Cite's Cypress config drops a spec when its
extension is not loaded. **A run with no specs passes, with exit 0 and 0
tests.** That is not a pass: say which specs did not run and why. Before
you install, compare what the specs need with what the wiki loads:

```bash
curl -s --noproxy '*' "$MW_SERVER$MW_SCRIPT_PATH/api.php?action=query&meta=siteinfo&siprop=extensions&format=json" \
  | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>console.log(JSON.parse(d).query.extensions.map(e=>e.name).join(" ")))'
```

```bash
mw-install-cypress <repo>     # about 15 s and 800 MB the first time
cd <repo> && npm run selenium-test
```

`mw-install-cypress` puts the binary in the cache folder that the repo's scripts name (Cite:
`tests/cypress/.cache`, which git ignores), and runs `cypress verify`. The
install is per repo. The wiki from §5.1 must be running. Cypress uses its
bundled Electron and does not need §5.2. To use the §5.2 Chrome, give it
by path: `--browser /usr/bin/chromium`. `--browser chrome` does not find
it.

- Cite's `selenium-test` runs `cypress-parallel`, whose reporters do not
  show the error text. To see why a spec failed, run it alone:
  `npm run cypress:worker -- --spec tests/cypress/e2e/tests/<dir>/<file>.cy.js`.
- `cypress-parallel` rewrites the tracked file
  `tests/cypress/parallel-weights.json` on each run, with the timings it
  measured. That change is a side effect of the run, not part of your
  work: restore it with
  `git checkout tests/cypress/parallel-weights.json` before you commit.
- Popups adds no modules unless TextExtracts and PageImages are also
  loaded (with its default `$wgPopupsGateway`, `mwApiPlain`). Without
  them, Cite's reference-preview specs fail in
  `waitForModuleReady('ext.cite.referencePreviews')`, which gets
  `registered`, not `ready`.
- Cypress tries to reach `api.cypress.io` and `cloud.cypress.io`. The
  sandbox refuses them, and the tests do not need them.

## 6. Things that will mislead you

1. **`vendor/bin/phpunit` before `phpunit.xml` exists** — §2.
2. **`MW_SCRIPT_PATH` set to the empty string.** It matches the wiki's
   `$wgScriptPath`, and core's Gruntfile accepts it. But `wdio-mediawiki`
   tests `!process.env.MW_SCRIPT_PATH`, which rejects the empty string, and
   throws `MW_SERVER or MW_SCRIPT_PATH not defined`. Leave the environment's
   `/` alone.
3. **`npm ci` where cypress is a devDependency** — §3.
4. **phan errors about sibling extensions** — §3.
5. **`Chrome instance exited` from wdio**: you left out `CI=true` (§5.4),
   or you set a long `$TMPDIR`. Chrome puts a socket under `$TMPDIR`, and a
   Unix socket path has a length limit of about 108 bytes. Keep the
   session's own `$TMPDIR`, which is short; `/tmp` itself is not readable.
6. **A Cypress run with 0 specs that "passes"** — §5.6. Specs whose
   extension is not loaded do not run. Do not substitute a different
   harness for Cypress and report it as equivalent.
7. **17 REST failures in core's full `npm run api-testing`**, all in
   `tests/api-testing/REST/Creation.js` and `Update.js`: `expected 400 to
   equal 201` (or 403, 409), with `The "Content-Type" parameter must be
   set.` The api-testing client sends the header name in lower case. Core
   declares `Content-Type` as a header parameter of the page create and
   update handlers (since core commit `0ca91a896ad`, T412668), and
   `ParamValidatorCallbacks::getValue()` looks header parameters up by
   exact name. `php -S`, which `composer serve` runs, keeps header names as
   the client sent them, so the lookup misses. `curl -H 'content-type:
   application/json'` shows the same 400. It is a core bug, not your change
   and not the sandbox: expect exactly these failures, and compare with the
   run before your change.
8. **Other sibling-extension failures** — §3: a structure test, skipped
   selenium tests, an unused phan suppression.
9. **A script that pipes into a reporter exits 0 when the tests crash** —
   §3 (Popups' `mw-node-qunit` on Node 22).
10. **Do not build a jsdom or hand-made stand-in for a browser suite.** A
   harness that loads the sources directly has a different set of modules
   registered than the real page, and it quietly disagrees with the real
   page about real documents. Use one as a fast check while you iterate if
   you like, but gate on the real runner. (Jest is not such a stand-in: it
   is the suite's real runner, and CI runs it the same way.)

## 7. Quick reference

| You are working on | PHPUnit | Lint | Jest | Browser and API |
|---|---|---|---|---|
| mediawiki-core | from core: `composer phpunit:entrypoint -- <path>` | from core: `composer test`, `npm test` | from core: `npm run jest` | from core: `npx grunt karma:chrome --qunit-component=MediaWiki`, `CI=true npx wdio …`, `npx mocha …` |
| an extension or skin | from core: `composer phpunit:entrypoint -- extensions/<Name>/tests/phpunit` | from repo: `composer test`, `npm test` | from repo, only if `package.json` has a script | QUnit from core: `--qunit-component=<Name>`; selenium and api-testing from repo, if defined |
| Parsoid | from repo: `composer phpunit`, `composer parserTests` | from repo: `composer lint`, `npm test` | — | — |
