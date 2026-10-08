# Design: telling a sandbox Claude how to run MediaWiki tests

**Status: done, except §5.6** (the dependency walk, now a `sbx/NOTES.md`
to-do of its own). §3–§6 are implemented, five blind acceptance runs
passed (§9.5–§9.8), and the bugs found outside wmf-claude are written up
for upstream in `sbx/upstream/`. The measurements are done: every command in this
document and in `sbx/templates/MEDIAWIKI-TESTING.md` was run end to end in a
live `wmf-sbx-create gerrit:mediawiki/extensions/Translate
gerrit:mediawiki/extensions/Cite gerrit:mediawiki/services/Parsoid
wmf-claude` sandbox on 2026-09-15, against the `core`, `Vector` and
`UniversalLanguageSelector` checkouts the dependency walk added. All five
repos were at their fetched `origin/master`. Nothing below is inferred from
documentation unless it says so.

The brief: a new sandbox hands Claude several repos and a working wiki and
says nothing about how to test any of it. The starting point was a report
another agent wrote while testing VisualEditor in the *nono* sandbox
(formerly `sbx/reference/MEDIAWIKI-TESTING.md`, deleted now that this plan
supersedes it). Its two hardest steps, getting a browser and what `grunt`
actually runs, were the parts that generalised worst.

A constraint on the implementation: **it modifies nothing in wmf-claude outside
`sbx/`.** Upstream wmf-claude uses nono rather than sbx, and keeping our
changes inside `sbx/` keeps merges with upstream mechanical. Where the plan
needs a plugin file (a hook's context text, a skill) to say something
different inside our sandbox, the content moves under `sbx/`: a whole file
in an overlay when only our sandbox reads it, a patch when the nono backend
shares the file. The kit applies both to the copy it installs, never to the
file in the repo (§6).

A constraint on the instructions: **every sandbox holds a different set of
repos.** One is core alone, the next is two extensions and Parsoid. The
instructions therefore cannot say "run Translate's tests"; they say "*if you
are working on an extension*, then …", and the agent works out which cases
apply. Generating a per-sandbox section instead is a NOTES.md to-do (§8).

## 1. What the sandbox can do, measured

| Runner | Where it runs | Command | Result |
|---|---|---|---|
| PHPUnit, extension | core | `composer phpunit:entrypoint -- extensions/Translate/tests/phpunit` | 1090 tests, 4.1 s |
| PHPUnit, extension unit | core | `… -- extensions/Cite/tests/phpunit/unit` | 140 tests, 2.0 s |
| PHPUnit, extension integration | core | `… -- extensions/Cite/tests/phpunit/integration` | 112 tests, 0.9 s |
| PHPUnit, skin | core | `… -- skins/Vector/tests/phpunit` | 155 tests, 0.8 s |
| PHPUnit, core structure | core | `… --testsuite=structure` | 1712 tests, 30 s |
| Parser tests, extension | core | `… -- tests/phpunit/gen/ParserTest_Cite_1_*.php` | 1151 tests, 29 s, clean |
| PHPUnit, Parsoid | `$PARSOID` | `composer phpunit` | 9314 tests, 2.7 s |
| parserTests, Parsoid | `$PARSOID` | `composer parserTests` | 31031 tests, 70 s, 0 unexpected |
| PHP lint/phpcs/minus-x | each repo | `composer test` | Cite 0.8 s, Parsoid `composer lint` 2.8 s |
| phan | each repo | `composer phan` | Cite 21 s, **32 structural false positives** (§2.5) |
| eslint/stylelint/banana | each repo | `npm test` | Translate 9.4 s, Cite 12 s, Parsoid 7.5 s |
| Jest | core / skin | `npm run jest` / `npm run test:unit` | core 178 tests 23 s, Vector 79 tests 7.8 s |
| QUnit (karma) | core | `npx grunt karma:chrome --qunit-component=Translate` | 9 tests, 3.0 s |
| Selenium (wdio) | core | `npx wdio ./tests/selenium/wdio.conf.js --spec …` | 6 tests, 15 s with Chrome download |
| api-testing (mocha) | core | `npx mocha --timeout 0 tests/api-testing/action/Edit.js` | 23 tests, 3 s |
| Cypress e2e | — | — | **impossible**, CDN 403 (superseded: the kit now allows the two download hosts, and `mw-install-cypress` installs it; `NOTES.md` §91, §92) |

The QUnit, selenium and api-testing rows were re-run with the environment
§5.4 proposes (`MW_SCRIPT_PATH=/`, `CHROME_BIN=/usr/bin/chromium`,
`API_TESTING_CONFIG_FILE`, no `CI`, no `CHROMIUM_FLAGS`) and with
`googlechromelabs.github.io` allowed by the sandbox policy.

So everything except cypress works, including QUnit and selenium, which the
current sandbox docs describe as out of reach. Several rows need a step that
is not guessable, which is what the new documentation is for.

## 2. What a fresh Claude gets wrong

Each was hit for real during the measurement run.

### 2.1 `vendor/bin/phpunit <path>` fails on a fresh sandbox

There is no `phpunit.xml` (core ships `phpunit.xml.template`, not a `.dist`),
so PHPUnit loads no bootstrap and dies with `Class "MediaWikiUnitTestCase"
not found`, which reads as a broken checkout. `composer
phpunit:entrypoint --` generates the config first. **The failing form is
what `skills/run-tests/SKILL.md` currently tells Claude to do** (§6.3,
correction 3).

### 2.2 `npm ci` fails wherever cypress is a devDependency

Cite, and plenty of others: the cypress CDN is 403, npm aborts, and
`node_modules/.bin` is left empty, so the *next* command fails with `sh: 1:
grunt: not found`, pointing at the wrong problem. `CYPRESS_INSTALL_BINARY=0
npm ci` works.

### 2.3 Node dependencies exist only in core

`mediawiki_setup` runs `npm_install` on core alone (`setup.py:2044`).

### 2.4 No browser, and none in apt

`chromium` has no candidate; `chromium-browser` and `firefox` are snap
stubs. Chrome for Testing works, after sixteen shared libraries via `sudo
apt-get`.

The first draft of this plan pinned an exact Chrome version, read out of
`puppeteer-core`'s `revisions.js`, because `googlechromelabs.github.io`
(where `@puppeteer/browsers` resolves `stable` to a version number) was
blocked. That was the right workaround only if MediaWiki's tests expect a
particular browser. They do not:

- **CI uses whatever Chromium its image has.** Quibble sets
  `CHROME_BIN=/usr/bin/chromium`, and `wdio-mediawiki` under `CI` uses
  `/usr/bin/chromium` and `/usr/bin/chromedriver` as found.
- **Outside CI, wdio asks for the current stable release.** wdio 9 resolves
  `stable` through `@puppeteer/browsers` and downloads Chrome and a matching
  chromedriver into `$TMPDIR`.
- **karma takes whatever `$CHROME_BIN` points at.** Nothing in core's
  `Gruntfile.js` or `package.json` names a version.

With `googlechromelabs.github.io` allowed (it now is, in this machine's
policy), plain `npx wdio …` with no `CI` downloaded Chrome 153 stable and
passed. So the kit should allow that host and install `chrome@stable`
(§5.3, §5.4). The version pin is gone from the plan.

Two things that turned out *not* to matter, for the record: `--no-sandbox`
(Chrome's own sandbox works in this container, so neither karma nor wdio
needed `CHROMIUM_FLAGS` or `CI`), and `/.dockerenv` (which `chromeOptions.js`
tests for; creating it changed nothing). One thing that did: a long
`$TMPDIR` makes wdio's Chrome die with `Chrome instance exited`, because
Chrome puts a Unix socket there and socket paths are limited to about 108
bytes. The template documents that.

### 2.5 A failure on the tree you were handed

`wmf-sbx` clones the engineer's own checkout for each named repo and resets
only the dependency repos to upstream. So a primary repo can hold local
commits or uncommitted changes, and nothing tells Claude this. Independently,
core, Parsoid and each extension sit at tips CI never builds together. Both
make "the sandbox is broken" the wrong first reading of a red test. The
documentation: `git log --oneline origin/master..HEAD` and `git status
--short` first, and run the suite once before the change.

**phan is a separate, structural problem.** An extension's
`.phan/config.php` names sibling extensions it expects on disk; Cite's adds
`../../extensions/cldr`, `../../extensions/CommunityConfiguration` and
`../../extensions/Gadgets` to `directory_list`. The dependency walk follows
`extension.json`, which does not require them, so they are absent and phan
emits 32 `PhanUndeclared*` errors on a pristine tree. That is fixable in the
kit (§5.6). Until it is fixed, the template explains it, and the explanation
comes out when the fix lands (NOTES.md to-do).

### 2.6 Selenium rejects an empty script path

`wdio-defaults.conf.js:23` guards with `!process.env.MW_SCRIPT_PATH`, which
is true for the empty string, and throws *"MW_SERVER or MW_SCRIPT_PATH not
defined"*. The wiki's `$wgScriptPath` *is* empty (a docroot install), and the
kit currently exports `MW_SCRIPT_PATH` to match.

The first draft claimed CI never hits this because CI uses `/w`. **That was
wrong.** Quibble installs MediaWiki the same way we do, as a docroot install
served by `php -S` from core, and sets `MW_SCRIPT_PATH='/'`. The
`http://127.0.0.1:9412//index.php` double slash that produces is harmless to
PHP's built-in server. `MW_SCRIPT_PATH=/` was measured here against every
consumer:

- `assert-mw-env` in core's Gruntfile: passes (it rejects only `undefined`).
- karma: `karmaProxy['/']` proxies everything to the wiki. 9/9 Translate
  QUnit tests pass.
- wdio: passes the guard; `baseUrl` is `http://localhost:4000/`. 6/6 pass.

So the fix is to export `MW_SCRIPT_PATH=/` from the kit (§5.4), exactly as
CI does, and the upstream bug stops mattering to us (§6.5).

**Could `composer serve` use `/w` instead?** Investigated, and no. `composer
serve` is `php -S 127.0.0.1:4000` with no docroot and no router argument, so
`/w/api.php` resolves only if core contains a `w -> .` symlink. Measured with
one in place: `/w/api.php` returns 200, but

- `git status` shows `?? w` in core, which the engineer then has to ignore
  or not commit;
- `find -L` reports a filesystem loop, and any tool that follows symlinks
  over core descends `w/w/w/…`;
- the install would also need `--scriptpath=/w`, or the wiki's generated
  `load.php` URLs miss karma's proxy, which is keyed on the script path;
- and none of it would match CI, which does not use `/w`.

`/` gets the same benefit (every harness accepts it) with none of that.

### 2.7 Selenium does not need `CI=true`

The first draft required `CI=true`, because without it wdio resolved a
chromedriver version from the blocked host. With the host allowed (§2.4),
the default path works. `CI=true` remains useful to reuse the
`/usr/bin/chromium` that `mw-install-browser` installs instead of a second
download into `$TMPDIR`; it also raises `maxInstances` to 75% of the CPUs.

### 2.8 api-testing has no config file, and `base_uri` is the wiki root

Not `…/api.php`. Get it wrong and every test dies in a `before all` hook
with `Cannot read properties of undefined (reading 'tokens')`, which looks
like an auth problem.

The library looks for `.api-testing.config.json` in the current directory
**unless** `API_TESTING_CONFIG_FILE` is set, which Quibble does. An absolute
path in that variable works from any repo, not only from core: measured,
23/23 with `API_TESTING_CONFIG_FILE` naming a file outside the repo and
`base_uri` `http://localhost:4000/`, the same trailing-slash form Quibble
writes. The plan puts the file in core, which already gitignores that name,
and points the variable at it (§5.5).

### 2.9 Jest is a separate, per-repo runner

[T334642](https://phabricator.wikimedia.org/T334642) settled on Jest for
new Vue.js code; QUnit stays for existing code. [mediawiki.org/wiki/Jest](https://www.mediawiki.org/wiki/Jest)
documents the core side. What a fresh Claude needs to know:

- **Core**: `npm run jest` (`jest --config tests/jest/jest.config.js`),
  with `mw` mocked by `tests/jest/jest.setup.js`. A new suite must be added
  to `roots` in that config. 178 tests, 23 s.
- **Core's config ignores `/extensions/` and `/skins/`.** So running Jest
  from core never reaches an extension's tests.
- **Extensions and skins wire their own**, with their own jest dependency,
  config and script name: Vector `npm run test:unit` (79 tests, 7.8 s);
  others use `test:jest` or call it from `npm test`. The only reliable
  discovery is `package.json`'s `scripts`.
- **A `tests/jest/` directory does not prove a runner exists.** Translate
  has four Jest test files, no jest dependency and no script, and CI does
  not run them. Forced through core's config, some fail. The template tells
  Claude to report that rather than claim a run.

## 3. Deliverable A — `sbx/templates/MEDIAWIKI-TESTING.md`

Written already, as the in-sandbox guide. It is organised around the
repo-set constraint in the introduction: §0 what setup did; §1 find out
what you were given; then **§2 "If you are working on mediawiki-core"**, **§3 "If you are
working on an extension or a skin"**, **§4 "If you are working on Parsoid"**;
§5 the shared wiki and browser machinery; §6 traps; §7 a quick-reference
table.

It describes the sandbox **as it will be once §5 lands** (`npm ci`
everywhere, `mw-install-browser`, the new environment variables), so it
ships together with those changes, not before. Until then it is a spec for
them.

Ships to `files/home/MEDIAWIKI-TESTING.md`, i.e. `~/MEDIAWIKI-TESTING.md`
inside the sandbox. Not `~/.claude/`, and not appended to `CLAUDE.md`: it is
reference material to read when testing, not context to carry in every
session.

## 4. Deliverable B — the `CLAUDE.md` section

Appended to `HOME_CLAUDE_MD` in `sbx/src/wmf_sbx/kit.py`, which is written
to `files/home/.claude/CLAUDE.md` and so loads in every session. Static
text, not generated (§8). Kept short: the entry point, the things that fail
silently, and a pointer.

```markdown
## Running tests

This sandbox holds its own set of repos; `ls "$MW_INSTALL_PATH"/extensions
"$MW_INSTALL_PATH"/skins` and `echo "$PARSOID"` show which. Everything is
installed: `vendor/` and `node_modules/` in every repo, and a SQLite wiki at
`$MW_INSTALL_PATH` with every checked-out extension and skin loaded.
`~/MEDIAWIKI-TESTING.md` is the full guide. Read it before your first test
run in a session, not after a failure.

Before your change, run `git log --oneline origin/master..HEAD` and
`git status --short` in the repo, and run its suite once, so you know which
failures were already there.

*If you are working on mediawiki-core*, run PHPUnit from `$MW_INSTALL_PATH`
through composer, `composer phpunit:entrypoint -- <path>`, and lint with
`composer test` and `npm test`. Jest is `npm run jest`.

*If you are working on an extension or a skin*, PHPUnit still runs **from
core**, with a path relative to it:
`composer phpunit:entrypoint -- extensions/<Name>/tests/phpunit`. Lint runs
from the repo: `composer test`, `npm test`. Jest runs from the repo, and only
if its `package.json` has a script for it; core's Jest never runs extension
tests. QUnit runs from core: `npx grunt karma:chrome --qunit-component=<Name>`.

*If you are working on Parsoid*, it has its own suites, run from `$PARSOID`:
`composer phpunit`, `composer parserTests`, `composer lint`.

`vendor/bin/phpunit <path>` fails with `Class "MediaWikiUnitTestCase" not
found` until something generates `phpunit.xml`; the composer script does
that first.

QUnit and selenium need a browser, which is not installed by default: run
`mw-install-browser` once (about 1 minute, 420 MB), then start the wiki with
`composer serve`. Do not change `MW_SCRIPT_PATH`: it is `/` on purpose, as
in CI. Cypress e2e tests cannot run here at all; say so rather than
substituting another harness.

`composer phan` reports undeclared classes from sibling extensions that
`.phan/config.php` names and this sandbox does not have. Those errors are
not yours.
```

The last paragraph goes when §5.6 lands.

## 5. Deliverable C — sandbox configuration changes

Ordered by value per line of code.

### 5.1 `npm ci` in every repo, not just core — `setup.py`

`mediawiki_setup` runs `composer_update` over every clone but `npm_install`
over core only. Extend it: for each clone with a `package.json`, run
`CYPRESS_INSTALL_BINARY=0 npm ci`, non-fatally (append to `warnings`, like
`composer_update` does; a repo whose JS deps fail should not cost the whole
sandbox). Measured cost for the four repos in the test sandbox: 29 s total,
against a `sbx create` that already takes minutes.

`npm_install` (`setup.py:1700`) already chooses `npm ci` over `npm install`
deliberately, so the lockfile stays clean; reuse it with a repo argument and
the extra env var rather than adding a second function.

### 5.2 `composer phpunit:config` once, at create time — `setup.py`

One command after `install_mediawiki`, and trap §2.1 stops existing: both
`vendor/bin/phpunit` and the `run-tests` skill then work as written. Keep the
documentation of the trap anyway: the file is gitignored, and regenerating
it after an extension changes is still on Claude.

### 5.3 `mw-install-browser` — a new helper in `sbx/helpers/`

`sbx/helpers/` holds scripts that run only *inside* a sandbox.
`sbx/bin/` is what the README tells engineers to put on their **host**
`PATH`, so a sandbox-only script does not belong there.
`wmf-sbx-mcp-proxy` and `wmf-sbx-gateway-tools` already moved to helpers;
`kit.py`'s `MCP_PROXY_SOURCE` points at it. `git-safe-reset` and
`git-review-check` stay in `sbx/bin/`: the kit copies them into the sandbox,
but engineers run them on the host too.

Install on demand rather than at create time: 420 MB is real, most sandboxes
never run a browser test, and the whole thing takes under a minute when it
is wanted. Ship it to `files/home/bin/` the way `write_kit_dir` ships the
two git scripts, so `install_helper_scripts` (`setup.py:1439`) puts it on
`PATH`. `kit.py`'s `HELPER_SCRIPTS` pairs its names with the single
`HELPER_SCRIPT_DIR` (`sbx/bin/`), so this needs a second list or a per-name
source directory; do not move the helper into `bin/` to avoid that.
`setup.py` keeps its own `HELPER_SCRIPTS`, which a test holds equal to the
kit's, so it gains the name too. Kit for now; it is not
MediaWiki-specific and could move to the plugin later.

What it does, all steps verified by hand:

1. `node <core>/node_modules/@puppeteer/browsers/lib/cjs/main-cli.js install
   chrome@stable --path ~/.cache/puppeteer`, and the same for
   `chromedriver@stable`. `@puppeteer/browsers` is already in core's
   `node_modules`, so no second npm install. `stable` needs
   `googlechromelabs.github.io` (§5.4).
2. `sudo apt-get install -y` the ten libraries `ldd` reports missing (13 s).
   Four of them are `…t64`-suffixed on this image, and the plain name is a
   virtual package with no candidate; the other six keep the plain name that
   Puppeteer's docs give. So the helper tries the plain name and falls back
   to `<name>t64` (NOTES.md §86, which lists them; the earlier count of
   sixteen was a hand count, and wrong).
3. Link the two paths WMF CI uses: `/usr/bin/chromium` (what `CHROME_BIN`
   names) and `/usr/bin/chromedriver`.
4. Check the result: `/usr/bin/chromium --headless --dump-dom about:blank`.

A 403 from either Chrome host means the host's policy is narrower than the
kit's; report it, since only the engineer can change it. Running it again
upgrades to the new stable release.

Alternative considered and rejected: installing at create time. It would make
`CHROME_BIN` always valid and remove a step, at 420 MB and about 25 s on
every sandbox. Revisit if browser tests turn out to be common.

### 5.4 Kit environment and policy — `kit.py`, `setup.py`

- `MW_SCRIPT_PATH=/` (§2.6). Today the kit's env builder (`kit.py:808`) and
  the `.env` template (`setup.py:265`) both copy the install's `scriptPath`,
  which is `""`. Keep `--scriptpath=` for the install and export `/` for the
  harnesses, as Quibble does: `params.get("scriptPath") or "/"` in both
  places. The docstring at `kit.py:797` that calls the empty value
  legitimate needs rewriting to match.
- `CHROME_BIN=/usr/bin/chromium`, the same value as Quibble.
- `MEDIAWIKI_USER=Admin`, `MEDIAWIKI_PASSWORD=adminpassword`. Selenium reads
  these; they match what `mw-install:sqlite` created, and the wiki is
  sandbox-local.
- `API_TESTING_CONFIG_FILE=$MW_INSTALL_PATH/.api-testing.config.json`,
  expanded to an absolute path when the kit writes it (§5.5).
- `EXTRA_DOMAINS`: add `googlechromelabs.github.io` (version resolution)
  and `storage.googleapis.com` (the download), for the same
  state-the-requirement reason the comment there already gives for
  packagist and npm. Do **not** add the cypress CDN: a 300 MB binary for a
  test class we cannot drive is not worth the surface. (Superseded on
  2026-09-18: Cypress can be driven here, so the kit now allows
  `download.cypress.io` and `cdn.cypress.io`, and the binary installs on
  demand. `NOTES.md` §91, §92.)
- `BASE_PACKAGES`: leave alone. The Chrome libraries belong to the optional
  helper, not to every sandbox.

### 5.5 The api-testing config at create time — `setup.py`

Written after `install_mediawiki`, since that is when `$wgSecretKey` exists,
to `.api-testing.config.json` in the core checkout. Core's `.gitignore`
already lists that exact name, so `git status` never shows it, and it sits
next to `LocalSettings.php`, the file whose values it repeats. Running from
core, the library finds it without help. `API_TESTING_CONFIG_FILE` (§5.4)
names it by absolute path, so an extension's own `npm run api-testing` finds
it too. Contents: `base_uri` `http://localhost:4000/`, `main_page`,
`root_user` Admin/adminpassword, and `secret_key`. Removes trap §2.8.

Do not write it into an extension or Parsoid checkout: their `.gitignore`
files do not reliably list the name. The measurement in §2.8 used a file
outside every repo; the core path is read by the same absolute-path lookup,
and the implementation's test run should confirm it.

### 5.6 Clone the extensions phan asks for — the dependency walk

`.phan/config.php` is a second, independent declaration of what an extension
needs on disk, and nothing reads it. Cite names `cldr`,
`CommunityConfiguration` and `Gadgets`; without them `composer phan` emits 32
errors on a pristine tree, which is enough noise to make the tool useless
without a baseline.

Proposal: in the dependency walk, also parse each repo's `.phan/config.php`
for `directory_list` entries matching `../../extensions/<name>` or
`../../skins/<name>`, and add those to the clone set. A regex over the file
is enough (these config files are conventional and the entries are literal
strings), and a name that does not resolve to a Gerrit repo should warn,
not fail. Shallow clones suffice: phan only reads their PHP, and
`exclude_analysis_directory_list` means it never analyses them.

**But `.phan/config.php` is not the source of truth either.** What is
guaranteed to work is what CI checks out, and CI is quibble, whose repo
lists live in `gerrit:integration/config`: `zuul/dependencies.yaml` for the
test jobs and `zuul/phan_dependencies.yaml` for the phan jobs. Translate's
phan config names exactly the seven repos its CI phan entry names, so the
checked-in config is a copy of the CI map and can drift from it. The
manifests name one repo, and CI's test entry names five. A second
complication comes with that: the relative paths in `.phan/config.php`
assume quibble's layout, and a sandbox mirrors the engineer's layout, so
the paths do not resolve through our symlinks even when the repo is there.

Either file can be the source, because CI enforces the agreement: a phan
config that names a directory the CI map does not check out fails there.
Reading the YAML is probably the more convenient of the two, since a python
script can read it directly and `.phan/config.php` is PHP with no
structured form. That is a later decision, not this one.

So this to-do is really "use the dependency set WMF CI uses", and it is
tracked in `sbx/NOTES.md` with the measurements and the open decisions.
When it lands, the phan explanation comes out of the template (§3 and trap
4 in §6) and out of the `CLAUDE.md` section.

## 6. Deliverable D — sbx-only plugin text, kept under `sbx/`

Three things this plan needs are wrong in the plugin tree today: two
sentences in `hooks/context/sbx/environment.txt` and the PHPUnit form in
`skills/run-tests/SKILL.md`. Both files live outside `sbx/`, and by the
implementation constraint in the introduction neither is edited there.

Two mechanisms, for two different kinds of file:

- **An overlay (§6.1)** for a file only our sandbox has any use for. The
  file's whole content moves under `sbx/`, and the kit ships it into the
  plugin tree. Upstream carries none of it.
- **A patch (§6.2)** for a file the plugin genuinely shares with the nono
  backend. The kit still starts from the repo's own file and applies our
  correction to the copy it installs.

Both keep the merge with upstream clean in both directions: upstream
improvements reach our sandbox, our sbx-specific text does not leak
upstream, and a patch that no longer applies stops the kit build rather
than shipping quietly.

### 6.1 The overlay — `sbx/plugin-overlay/`, shipped by `write_plugin_tree`

`hooks/context/sbx/{sandbox,environment}.txt` exist only for us:
`bin/session-start.sh` reads `hooks/context/<backend>/` and the kit is the
only thing that sets `WMF_CLAUDE_SANDBOX_BACKEND=sbx` (`kit.py:903`). So
there is nothing to patch against. The files move to
`sbx/plugin-overlay/hooks/context/sbx/{sandbox,environment}.txt`, keeping
the path they need inside the plugin tree, and the kit lays them down.

- **Layout.** `sbx/plugin-overlay/<path relative to the plugin tree>`. A
  file in the overlay either adds a path the repo does not have (the case
  here) or replaces one it does; the second form is a whole-file
  replacement, so prefer a patch (§6.2) unless the repo's version has no
  other reader.
- **Ship step.** `plugin_files()` returns the repo's paths; add the
  overlay's paths to that list, with the overlay winning on a collision,
  and give each path its source directory so `write_plugin_tree` copies
  from the right root. The overlay is a source of plugin files, not a
  second copy step.
- **What moves out of the shared tree.** `hooks/context/sbx/` goes away in
  the repo. `tests/test-templates.sh` walks `hooks/context/*/` and asserts
  each backend has both files, so it keeps passing with `nono` alone, and
  `bin/session-start.sh` stays backend-aware and still fails closed on a
  backend it has no text for: it emits nothing and warns on stderr. Neither
  file outside `sbx/` changes.
- **Tests,** in `sbx/tests/test_wmf_sbx_kit.py`: the overlay's paths appear
  in `plugin_files()`; both plugin-tree destinations hold the overlay's
  content; an overlay file that shadows a repo file wins.

### 6.2 The patch mechanism — `sbx/patches/plugin/`

For a shared file, `skills/run-tests/SKILL.md` being the one case here.

- **Layout.** One unified diff per concern in `sbx/patches/plugin/`, named
  `NN-short-name.patch` so the apply order is the sort order. Paths are
  relative to the repo root (`a/skills/run-tests/SKILL.md`), which is also
  the layout `plugin_files()` returns, so a patch applies with `-p1` to the
  repo checkout and to a shipped copy alike. Make one with
  `git diff -- <file> > sbx/patches/plugin/NN-name.patch` on a scratch edit,
  then `git checkout -- <file>`.
- **Apply step.** `write_plugin_tree` (`kit.py:1153`) copies
  `plugin_files(root)` to both destinations today. Change it to copy once
  into a staging directory made with `tempfile.mkdtemp()` (outside any git
  work tree), run `git apply -p1 <patch>` there for each patch in order, and
  then copy the staged tree to both destinations. Applying once, not per
  destination, keeps the two copies identical by construction. `git apply` is
  already a dependency of every host that runs `wmf-sbx-create`, unlike
  `patch`, and it refuses fuzzy matches by default.
- **Failure.** If `git apply` fails, raise with the patch name and git's
  message. Do not skip the patch and ship the unpatched file: that is the
  case in which upstream changed the lines we correct, and a person has to
  decide whether the correction still applies.
- **Scope check.** Reject a patch that touches a path `plugin_files()` does
  not return, or that creates or deletes a file. A patch corrects shipped
  text; a new file belongs in the overlay (§6.1).
- **Tests,** in `sbx/tests/test_wmf_sbx_kit.py`: every patch in
  `sbx/patches/plugin/` applies to the current repo tree; both destinations
  hold the patched text; a patch with a stale context line raises; a patch
  outside the plugin tree is rejected. The first test is the one that fails
  after an upstream merge, before any sandbox is created.
- **Where the rule lives.** Add a line to `sbx/README.md`: plugin-tree
  changes for sbx go in `sbx/plugin-overlay/` or `sbx/patches/plugin/`,
  never in `hooks/` or `skills/` directly.

### 6.3 The three corrections

1. **`hooks/context/sbx/environment.txt` says `/w/api.php`.** This wiki is
   a docroot install; the path is `/api.php`. One word, and it is in the
   global session prompt. An overlay edit (§6.1).
2. **The same file says "There is no browser MCP in this backend, so
   pixels, the accessibility tree, console errors, and real interaction are
   not available".** Still true of MCP, but once `mw-install-browser`
   exists, headless Chrome *is* available, so the sentence should say what
   can be done (drive it via the selenium harness, which screenshots
   failures to `tests/selenium/log/`) rather than implying nothing can. An
   overlay edit, and it depends on §5.3, so it lands with it.
3. **`skills/run-tests/SKILL.md` teaches the `vendor/bin/phpunit` form**
   (§2.1), and its `allowed-tools` list only permits that and `mwdocker`.
   Add the `composer phpunit:entrypoint` form and allow it; keep the direct
   form for setups where `phpunit.xml` is committed. A patch (§6.2):
   `test-coverage/SKILL.md`, `agents/test-writer.md` and
   `templates/mediawiki/` teach the same form, so the correction is
   plausibly right for every backend, and it goes upstream as a merge
   request. The patch stays until upstream takes it.

### 6.4 Audit of the shared files this work already changed

The rest of this plan stays inside `sbx/`: §3 and §4 write `sbx/templates/`
and `sbx/src/`; §5 changes `sbx/src/` and `sbx/helpers/`; §5.6 changes the
dependency walk in `sbx/src/`. Two earlier commits did change shared files,
and the audit of them splits three ways.

**Moves to the overlay.** `hooks/context/sbx/{sandbox,environment}.txt`,
added by 45aa2c8 and edited by e85d460 (the paragraph about the MCP proxy
and its read-only tools). Reverting the files out of the shared tree and
shipping them from `sbx/plugin-overlay/` carries the edit with them, so the
refactor is a move plus a deletion, not a rewrite. This is one commit of
its own, taken with the §6.1 implementation.

**Stays in the shared tree, and goes upstream.** The rest of e85d460:
`wiring/settings-merge.json` and the matching `SECURITY.md` text. Two bug
fixes, neither sbx-specific:

- `Bash(find:* -exec*)` and its five siblings are a spelling Claude Code
  rejects, and a rejected rule is skipped — so `find -exec` was denied in
  **no** backend, nono included. `Bash(find *-exec*)` is the spelling that
  matches.
- The eight `Write(...)` denies are never consulted: `Edit(path)` is the
  rule that covers every file-editing tool. Removing them removes dead
  weight, not a lock.

Neither file is shipped into a sandbox, so neither mechanism above can even
reach them; the kit reads `wiring/settings-merge.json` at build time
(`kit.py:534`) and gets the fix from the repo. Keeping the fixes means nono
gets them too, which is the point.

**Update, rebase onto upstream `main` (nono 0.78):** upstream made both
fixes itself, more thoroughly (`find * -exec*` and `find -exec*`, plus
`-fls`). The rebase takes upstream's `wiring/settings-merge.json` and
drops this branch's version and its `SECURITY.md` text. The task and patch
for it are deleted from `sbx/upstream/`. Only one part is still open:
whether the "Linux glob caveat" is stale (`sbx/upstream/PHAB-TASK-4.md`).
The one fork-only piece is `SECURITY.md`'s new opening paragraph, which
points the reader at `sbx/SECURITY.md`; it is a paragraph to drop when
merging, not a mechanism to build.

**Stays, and should not be reverted.** The rest of 45aa2c8: the
backend-aware `bin/session-start.sh`, `WMF_CLAUDE_DOCKER_MODE`, the
`standalone-vuln-audit` registration it fixed, and the
`tests/test-templates.sh` assertions that keep the three skill lists in
agreement. That commit is the seam that makes a second backend possible at
all, it leaves nono's output byte-for-byte what it was, and its by-products
are improvements upstream wants. A patch cannot express it, and hiding it
under `sbx/` would keep the fixes from the backend that also needs them.

### 6.5 Not ours to change

**The `wdio-defaults.conf.js` guard is still an upstream bug**, but no
longer ours to work around once §5.4 exports `/`. Core's own Gruntfile says
an empty `MW_SCRIPT_PATH` is valid "for docroot installs … This includes
`composer serve` (Quickstart)", and `wdio-mediawiki` rejects that same
value. The one-line fix (`=== undefined`, as the Gruntfile does) is
written up as `sbx/upstream/PHAB-TASK-2.md`; low priority.

The VisualEditor report this plan grew from is deleted
(`sbx/reference/MEDIAWIKI-TESTING.md`). Two documents with the same name
that disagreed about which sandbox they describe was the failure mode
`HOME_CLAUDE_MD`'s comment warns about.

## 7. Sequencing

- **Done:** the move of the in-sandbox scripts to `sbx/helpers/`, the
  deletion of the VisualEditor report (§6.5), the overlay (§6.1) and the
  patch mechanism (§6.2), and the refactor of §6.4: the sbx context files
  are in the overlay, correction 1 of §6.3 came with them, and correction 3
  is `sbx/patches/plugin/01-run-tests-composer-entrypoint.patch`. Also
  §5.1, §5.2, §5.5 and all of §5.4: the setup script runs `npm ci` in every
  clone, generates `phpunit.xml`, and writes core's
  `.api-testing.config.json`; the kit exports `MW_SCRIPT_PATH=/`,
  `CHROME_BIN`, the two selenium credentials and
  `API_TESTING_CONFIG_FILE`, and allows the two Chrome download hosts.
  Also §5.3, §3, §4 and correction 2 of §6.3: `sbx/helpers/mw-install-browser`
  installs Chrome and chromedriver, `HELPER_SCRIPTS` names a source
  directory per helper so a sandbox-only script ships too, the kit copies
  `templates/MEDIAWIKI-TESTING.md` to `~/MEDIAWIKI-TESTING.md`, `CLAUDE.md`
  carries the "Running tests" section, and the sbx environment text says
  what the selenium harness can do instead of only what MCP cannot. Both
  browser suites were run against the live wiki (NOTES.md §86).
- **Done, beyond the plan:** Cypress, which §1 had found impossible, installs
  on demand: the kit allows `download.cypress.io` and `cdn.cypress.io`, and
  `sbx/helpers/mw-install-cypress` installs the binary and its apt packages
  (NOTES.md §91–§93).
- **Done:** the blind acceptance runs in fresh sandboxes (§9), five of them
  on three repo sets.
- **Done:** the upstream write-ups: `sbx/upstream/PHAB-TASK-1.md` to `-5.md`
  (4 is now cut down and 5 is gone; see §6.3 and §6.4),
  each with a tested patch, for the engineer to file (§6.3, §6.4, §6.5, and
  two bugs the acceptance runs found).
- **Not done:** §5.6 (the dependency walk). It is a `sbx/NOTES.md` to-do,
  "Use the dependency set WMF CI uses", and needs a design of its own.

## 8. Decisions taken

- **Browser version:** track `stable`, allow the resolution host (§2.4).
- **Script path:** export `MW_SCRIPT_PATH=/`, matching Quibble; do not add a
  `/w` prefix to `composer serve` (§2.6).
- **Where `mw-install-browser` lives:** `sbx/helpers/`, shipped by the kit
  (§5.3). `git-safe-reset` and `git-review-check` stay in `sbx/bin/`,
  because engineers run them on the host too.
- **Where the api-testing config lives:** core's checkout, whose
  `.gitignore` already lists `.api-testing.config.json`, named by absolute
  path in `API_TESTING_CONFIG_FILE` (§5.5).
- **Changes to shared plugin files:** none in the repo. A file only our
  sandbox reads moves whole into `sbx/plugin-overlay/` (§6.1); a file the
  nono backend shares keeps a patch in `sbx/patches/plugin/`, applied to
  the shipped copy, and a failed apply stops the build (§6.2).
- **Bug fixes in shared files that are not sbx-specific:** they stay in the
  shared file and get offered upstream. The `find` deny spelling in
  `wiring/settings-merge.json` was broken for nono too, so hiding the fix
  under `sbx/` would have kept it from the backend that needs it (§6.4).
- **Static or generated testing section:** static, phrased conditionally
  ("*if you are working on …*"). The kit knows which repos a sandbox holds
  and could emit each one's real entry points (from `package.json` and
  `composer.json` scripts, `tests/` subdirectories, `QUnitTestModule`), but
  that adds a generator to keep correct against upstream conventions.
  Tracked as a to-do in `sbx/NOTES.md`.
- **Translate's unwired Jest files:** documented as a pattern ("check
  `package.json`; a `tests/jest/` directory is not proof"), not as a
  Translate-specific claim. Whether to wire them up is a question for the
  Translate maintainers, not for this kit.

## 9. Acceptance test — a blind run in a fresh sandbox

Every measurement in §1 and §2 was taken by an agent that knew what it was
looking for. That agent cannot judge whether the documentation works,
because it already knows the answers. So the last step of this plan is a
**blind run**: create a new sandbox from the implemented branch, give a
fresh Claude nothing but its own `CLAUDE.md` and a task list, and read what
it reports.

The test measures the documentation and the setup changes together. It is
not a unit test and does not replace one: §5, §6.1 and §6.2 carry their own
tests in `sbx/tests/`.

### 9.1 Create the sandbox

Run on the host, from the wmf-claude checkout, with the implementation
branch checked out, because `wmf-sbx-create` generates the kit from the
source tree it runs out of:

```bash
cd ~/Projects/Wikimedia/wmf-claude
git checkout <the branch that implements §3-§6>
wmf-sbx-create --name sbx-testverify --reset-all \
  gerrit:mediawiki/extensions/Translate \
  gerrit:mediawiki/extensions/Cite \
  gerrit:mediawiki/services/Parsoid
```

Four things about that command:

- **The repo set is the one §1 was measured against.** The dependency walk
  adds `core`, `Vector` and `UniversalLanguageSelector`. Translate gives an
  extension with unwired Jest files, Cite gives one with cypress in its
  `devDependencies` and a phan config that names absent siblings, Vector
  gives a skin with a real Jest suite, and Parsoid gives the separate
  suites of §4.
- **`--reset-all`.** Every clone starts at upstream master, so a red test is
  the plan's fault and not the engineer's work in progress. It also means
  the report can be compared against §1 directly.
- **No `wmf-claude` repo.** The measurement sandbox mounted it; this one
  must not. The kit installs the plugin and the guide by itself, and a
  mounted wmf-claude checkout would put this design document, the template's
  source and the patches in front of the agent, which spoils the test.
- **A distinct `--name`.** The sandbox is disposable; remove it with
  `wmf-sbx-rm -f sbx-testverify` after reading the report.

### 9.2 Give it `TESTING.md` and a one-line prompt

Written as `sbx/acceptance/TESTING.md`, with the host runbook beside it in
`sbx/acceptance/README.md`. Both were removed after the fifth run; read
them with `git show 54941cd:sbx/acceptance/README.md` (and `TESTING.md`,
`TASK-php-change.md`, `TASK-preview-change.md`). Copy it into the sandbox's home
directory (`wmf-sbx-exec sbx-testverify` reaches it, or transfer it the way
any other file gets in). It is a task list, not a second manual, and **it
must not name a single one of the traps in §2.** Naming them would tell the
agent the answers, and the answers are what the test is for. Every item is
phrased as "do this, report what happened":

1. **Report the workspace.** Which repos are here, which branch and which
   commit each one is on, and whether any has uncommitted changes.
2. **Read your own instructions first.** `~/.claude/CLAUDE.md` and whatever
   it points you at. Say what you read and whether the pointers resolved.
3. **Run the tests, following the instructions you already have.** For each
   repo in the workspace, run every test suite the instructions say applies
   to it: PHP tests, PHP lint and static analysis, JS lint, JS unit tests,
   browser tests and API tests. Report, per suite, the exact command, the
   pass and fail counts, the run time, and the first failure in full if
   there is one.
4. **Say which suites do not apply**, and why, for each repo.
5. **Start the wiki and check it answers**, over HTTP, both a page and the
   action API.
6. **Do the browser suites too.** If a browser is not installed, follow the
   instructions to install one, then run them.
7. **Do not work around anything silently.** If a command in your
   instructions fails, or you had to do something the instructions do not
   mention, that is the most valuable thing in your report. Record the
   command, the output and what you did instead.
8. **Write `REPORT.md`** in your home directory, then commit it to a branch
   in the primary repo so the engineer can fetch it (the `CLAUDE.md` rule
   about ending a turn with a commit applies).

The prompt to hand the sandbox can then be one line: *"Read
`~/TESTING.md` and do what it says."*

### 9.3 What `REPORT.md` must contain

Ask for it in a fixed shape, so two runs are comparable and so the answer to
"did the plan work?" does not need a conversation:

- a table of every suite run: repo, command, result, time;
- every command that failed, with its output;
- every step the agent had to invent, i.e. anything it did that its
  instructions did not tell it to do;
- anything in `~/MEDIAWIKI-TESTING.md` or `~/.claude/CLAUDE.md` that was
  wrong, missing, or misleading, quoted;
- the time from the start of the session to the first green test run;
- a plain answer to "could you have done this without inventing any step?"

### 9.4 How to read the result

- **The pass and fail counts should match §1's table**, allowing for
  upstream drift in the suites themselves. A large difference in a count is
  a finding about the sandbox, not about the documentation.
- **Every invented step is a documentation bug.** The template is supposed
  to cover the whole route. An invented step means either the template is
  missing something or the setup change (§5) that removes the need for it did
  not land.
- **phan's undeclared-symbol errors are expected** until §5.6 lands, and the
  template says so. A report that flags them as a real failure means the
  explanation in the template is not being read; a report that never mentions
  phan means the agent did not run it.
- **A 403 from the network policy is a finding for the engineer**, not for
  the agent, and the report should say which rule and which host.

Anything the report flags gets fixed in this branch, and the fix is
verified by a second blind run in a new sandbox. Do not reuse the first
sandbox: its agent has already read the answers.

### 9.5 First run: result

Run on 2026-09-17 in `sbx-testverify` (NOTES.md §87). Every applicable
suite in all six repos ran; the first green suite came 1 min 38 s after
the agent's first message. The agent answered "no" to "could you have done
this without inventing any step?", for two reasons, both now fixed:

- **Core's `npm test` failed on a clean checkout** because §5.5's
  `.api-testing.config.json` was indented with spaces, and core's eslint
  lints every JSON file, gitignored or not. The file is now indented with
  a tab, and setup replaces an old space-indented copy.
- **Parallel PHPUnit runs against one core dropped each other's SQLite
  test tables.** The template now says to run them one at a time.

### 9.6 Second run: result

Run on 2026-09-18 in a new sandbox (NOTES.md §88). Both fixes from the
first run held. Three gaps remained, all in the template, all now fixed:
`composer serve` needs a readiness poll, not one curl; phan also reports
an extension's own Composer libraries as undeclared, because CI merges
them into core's `vendor/` and the sandbox does not (a setup change for
that is a NOTES.md to-do); and Parsoid has an `npm run api-testing`
suite. The agent again answered "no" to "could you have done this
without inventing any step?", but for the readiness wait and the phan
diagnosis only.

### 9.7 Third and fourth runs: result

Run on 2026-09-18 (NOTES.md §90): the usual repo set again, and a second
set with Flow and TemplateData and no Parsoid, to check that the guide is
not fitted to one set. Both ran every suite. The Composer merge (NOTES.md
§89) held: no `\Spyc` error. Most of run 3's gaps were already in the
guide and the agent missed them. The new ones, now in the template: a
second `composer serve` that fails to bind, hidden by the first one's 200;
17 REST api-testing failures from a core bug with `php -S` (not the
sandbox); a repo npm script that names a binary it does not install
(Flow's `mocha`: install it with `npm install --no-save` and run the
suite); the sibling-extension gap outside phan (a structure test,
skipped selenium tests, an unused phan suppression); core's own phan; and
the Bash tool's per-call working directory. Separately, the kit now sets
`MEDIAWIKI_HAS_INTEGRATION_TESTS=1` with a Parsoid clone, so unit tests
use it too.

### 9.8 Fifth run: result

Run on 2026-09-18 (NOTES.md §93), on a third repo set for Cypress: Cite,
Popups, TextExtracts and PageImages. Part A was the full task list; part B
gave two narrow tasks to fresh sessions, to test the "install Cypress only
when it helps" advice. The kit's two Cypress hosts were enough, and
`mw-install-cypress` took 15 s and 812 MB. Cite's reference-preview spec
passed 3/3. In part B, the agent skipped Cypress for a PHP maintenance
script and installed it for a change to the reference-preview fade, as
the kit asks. New in the template: Popups' node-qunit tests crash on Node
22 and still exit 0 (fix: `NODE_OPTIONS=--no-experimental-global-navigator`);
Popups has a wdio suite; the phan sibling counts for more repos; how to
find a repo's suites. The acceptance files were then removed (§9.2).
