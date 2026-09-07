# Design: the rest of the in-sandbox MediaWiki setup

**Status: implemented** (2026-09-07) — §1–§8 are in
`sbx/bin/wmf_sbx_setup.py` (`mediawiki_setup()` and the step functions
around it), with the kit half in `wmf_sbx_kit.py`. **Run end-to-end in a
real sandbox on 2026-09-08** (`wmf-sbx-create Translate`): the whole
chain took 65s and produced a wiki serving `Special:Version` with the
closure loaded. Three bugs came out of that run — see `sbx/NOTES.md` §30
for all of them and for the two follow-up questions it raised
(`MW_SCRIPT_PATH`, and what `git safe-reset` should reset *to*); the one
that belongs to this document is §4.1's, below. The deltas from the design are
noted in place; the only substantive one is §4.1's kit-environment half,
which now reads core's own `composer.json` host-side
(`wmf_sbx_kit.mediawiki_env_vars`) rather than carrying copied values.

Companion to `sbx/DESIGN-dependency-walk.md` (which ends at the symlinks)
and `sbx/DESIGN-parallel-clone-tree.md` (which ends at the parallel tree
and the git daemon). This covers what the brief asks for after that:

> in the parallel clone of every repository *except the primary* do
> `git safe-reset` and, if the directory contains a `composer.json`, then
> do `composer update`. Then create the symlinks in mediawiki/core as we
> discussed earlier, then create a `/path/to/mediawiki/core/.env` file
> matching `mediawiki-core-clean/.env`. Then run `npm install` in
> mediawiki/core, then run `composer mw-install:sqlite`.

Everything below runs inside `wmf_sbx_setup.py`, i.e. as **root**, in the
kit's `commands.install` step, at `sbx create` time.

Five things turned up in research that change the shape of this, and each
is called out in place below. In summary:

1. `git safe-reset`'s `origin` in a parallel clone is the **read-only
   host-mirrored original**, not Gerrit (§2.1).
2. `git-safe-reset` shells out to **`git-review-check`** — not
   `git-check-reviews`, which is a stale backup in `~/bin` (§2.2).
3. `composer mw-install:sqlite` **does not pass `--with-extensions`**, so
   as written it would produce a `LocalSettings.php` with every skin and
   **no extensions** — which would make the whole dependency-walk and
   symlink exercise inert. Fixed by appending the flag:
   `composer mw-install:sqlite -- --with-extensions` (§6.1).
4. `.env` is read by `docker compose`, **not by MediaWiki or by core's own
   test harnesses**, which read `process.env` directly. A `.env` alone
   does nothing outside MediaWiki-Docker (§4.1).
5. `composer update` and `npm install` need `repo.packagist.org` and
   `registry.npmjs.org`, and **neither is in the kit's `allowedDomains`**
   (§7.1). Node itself may not be in the image either (§7.2).

---

## 1. What the setup script needs to know, and how it gets it

**Implemented** (2026-09-07), minus the `mediawiki` block, which lands
with §4/§6. `wmf_sbx_kit.build_plan()` builds it, `write_kit_dir(...,
plan=)` writes `files/home/wmf-sbx-plan.json`, and
`wmf_sbx_setup.load_plan()` reads it; the positional form is normalised
into the same dict by `plan_from_argv()` so `main()` has one code path.
`write_kit_dir` raises if a spec whose install step names the plan file is
written without one — otherwise the failure surfaces only inside the
sandbox, as a setup script that clones nothing.

`wmf_sbx_setup.py`'s argv is `HOST_HOME PORT REPO[:ro] ...` today. The new
steps need four more things it has no way to derive:

- **which repo is the primary** (excluded from `git safe-reset`);
- **which parallel path is `mediawiki/core`** (canonical names live
  host-side; the setup script only ever sees literal paths);
- **the symlink plan** — `link_name` → work path, per
  `DESIGN-dependency-walk.md` §1, which needs the parsed manifests;
- **the `.env` values**, which depend on the install parameters (§4).

Encoding all of that positionally does not scale — the dependency closure
routinely reaches a dozen repos, and `link_name` can contain spaces.

**Recommendation: a JSON plan file.** `wmf_sbx_kit.write_kit_dir` writes
`files/home/wmf-sbx-plan.json` next to the setup script it already copies,
and `commands.install` becomes:

```
python3 /home/agent/wmf-sbx-setup /home/agent/wmf-sbx-plan.json
```

Shape:

```json
{
  "version": 1,
  "hostHome": "/home/cananian",
  "daemonPort": 9418,
  "primary": "/home/cananian/Wikimedia/Extensions/Translate",
  "repos": [
    {"path": "/home/cananian/Wikimedia/Extensions/Translate",
     "canonical": "gerrit:mediawiki/extensions/Translate",
     "readOnly": false, "linkName": "Translate", "linkDir": "extensions"},
    {"path": "/home/cananian/Wikimedia/core",
     "canonical": "gerrit:mediawiki/core", "readOnly": false,
     "linkName": null, "linkDir": null}
  ],
  "mediawiki": {"install": true, "serverPort": 4000, "scriptPath": "",
                "adminUser": "Admin", "adminPassword": "adminpassword"}
}
```

One delta from the shape above: `daemonPort` is written as a **string**,
matching what the positional argv form has always handed `start_daemon`
(and what `git daemon --port=` wants), so the two entry points converge on
one type rather than one of them needing a cast.

`mediawiki.*` are fallbacks for `.env` generation only — core's own
`composer.json` `mw-install:sqlite` script is the authority for the
server, script path, and admin password (§4.2, §6.1).

The existing `HOST_HOME PORT REPO...` argv stays supported for one release
so the tests and any hand invocation keep working; the plan file wins when
given. Everything in the plan is computed host-side by
`wmf_sbx_create.py`, which is where the canonical names, the primary, and
the parsed manifests already live — and where it is all unit-testable
without a sandbox.

## 2. Step: `git safe-reset` in every clone the engineer didn't name

> The heading said "every non-primary clone" until 2026-09-08; the rule
> now covers every repo named on the command line, and `--reset-all`
> turns it off. See §8.2, which supersedes this section's primary-only
> phrasing; the rationale below is unchanged and is what §8.2 generalises.

For each repo in the plan where `readOnly` is false and which is not in
`repos_to_leave_alone(plan)`, in the **parallel** path:

```
sudo -u agent -H git safe-reset --force <remote>
```

where `<remote>` is `origin` (Gerrit) or `local` (the host mirror),
depending on what `configure_remotes` managed to set up — see §2.1, which
§8.1 supersedes.

`-H` (or an explicit `env HOME=/home/agent`) matters: this script runs as
root, and without it `git`, `composer`, and `npm` all write caches and
config into `/root`, and the files they create in the clone come out
root-owned, undoing `clone_into_parallel_tree`'s `chown`.

Rationale for excluding the primary is the brief's and it is right, and
it is worth stating positively rather than as an exception (cananian,
2026-09-07): **the working assumption is that the host's copy of the
primary has work in progress, and the sandbox is there to continue it.**
Starting the primary at the same git position as the host is the point,
not a concession — `git clone --reference` already lands the parallel
clone on whatever branch and commit the host had checked out, and
`safe-reset` would throw exactly that away.

Every *other* repo is a dependency, and a dependency should be at
`master`, not at whatever half-finished branch the host happened to have
checked out in it.

Note this cuts the other way for `composer update` (§3), which touches
only `vendor/` and so has no reason to skip the primary.

### 2.1 `origin` is the host original, not Gerrit — SUPERSEDED by §8.1

> Kept because the analysis below is still what makes the host mirror
> useful, and because it explains what the first real sandbox actually
> did. But its conclusion ("not v1") was reversed on 2026-09-08 after
> that run: the remotes are now swapped at setup time, `origin` means
> Gerrit, and the mirror is `local`. See §8.1 and `sbx/NOTES.md` §31.2.

`clone_into_parallel_tree` runs (`--shared` since 2026-09-08, see
`NOTES.md` §30):

```
git clone --shared <literal_path> <dest>
```

so the parallel clone's `origin` is, *before `configure_remotes` runs*,
**the read-only, host-mirrored original's literal path**, not
`ssh://gerrit...`. Consequences, all of
which are fine but none of which are obvious:

- `git remote update` and `git pull` are **local, offline, and need no
  SSH agent** — which is exactly what we want, since the sandbox
  deliberately has no agent (`NOTES.md` #8).
- `git reset --hard origin/master` resets to the **host's local `master`
  branch**, which may lag Gerrit. A clone's remote-tracking refs mirror
  the source repo's *local branches*, not its own remote-tracking refs.
  "safe-reset" in the sandbox therefore means "match the host", not
  "match Gerrit". That is the right default for a sandbox — it is
  reproducible, offline, and identical to what the engineer sees — but it
  should be said out loud in the created-sandbox output.
- Fetching from a read-only mount is fine; a fetch only writes to the
  destination.
- `git submodule update` (line 35) resolves the *submodule's* recorded
  URL, which for WMF repos is a Gerrit SSH URL and would fail. In
  practice this is a non-issue: `mediawiki/core` has no `.gitmodules`, and
  neither do Translate, Cite, Parsoid, or Vector. It is a real failure
  mode for the rare repo that does have one, so the step must not treat a
  non-zero exit as fatal (§8).

If we ever want true "reset to Gerrit" semantics, the lever is a second
remote (`https://gerrit.wikimedia.org/r/<project>` — HTTPS, no agent)
plus an explicit remote argument to `git safe-reset`. ~~Not v1.~~ **It is
v1**: the first real sandbox made the wart concrete (a dependency clone
sitting on the host's WIP commit rather than master), and cananian's call
was to go further and swap the *names* too — §8.1.

### 2.2 `--force`, and the `git-review-check` name

`git-safe-reset` line 22–29: if the repo has a `.gitreview` — every
MediaWiki repo does — it runs **`git-review-check`** and exits 1 unless
`--force`. (The brief called it `git-check-reviews`; `~/bin` has a live
`git-review-check` and a stale `git-check-reviews~` backup. The live name
is the one `git-safe-reset` invokes, so it is the one we ship.)

`git-review-check` asks Gerrit's REST API whether HEAD's commit exists as
a change. In a parallel clone HEAD is whatever the host had checked out,
so **any unpushed commit in any dependency repo would abort setup**.

That check exists to stop you from destroying your own unpushed work. In
a parallel clone there is no work to destroy — the original is mounted
read-only and untouched, and this clone was created seconds ago. So
`--force` is not a workaround here, it is the correct semantics. It also
saves one HTTPS round-trip to Gerrit per repo.

`git-safe-reset`'s *other* guard — refusing on a dirty tree (line 21) —
stays live and is worth keeping: a fresh clone is always clean, so it
only fires on re-run under `wmf-sbx-resume`, which is exactly when you do
want it to fire.

### 2.3 Shipping the two scripts

**Done as designed.** `sbx/reference/git-safe-reset` already existed
(byte-identical to `~/bin/git-safe-reset`) but `reference/` is a record of
the pre-existing setup, not a shipped artifact. Moved, with its helper
added:

- `sbx/bin/git-safe-reset`, `sbx/bin/git-review-check` — the shipped
  copies; delete `sbx/reference/git-safe-reset` so there is one source of
  truth.
- `write_kit_dir` copies both into `files/home/bin/`.
- The setup script `install -m 0755` them into `/usr/local/bin` early, so
  they are on `PATH` for root, for `agent`, and for the agent's own later
  use. Both names must be on `PATH`: `git-safe-reset` so `git safe-reset`
  resolves as a git subcommand, and `git-review-check` because
  `git-safe-reset` calls it by bare name.

`git-review-check` is worth having in the sandbox on its own merits —
`gerrit.wikimedia.org` is an allowed domain, so it works, and it is the
right thing for the agent to run before it thinks it is done.

## 3. Step: `composer update`

For each repo whose work path (see `sbx/NOTES.md` §63.1: the literal
host path, except in the `clone` fallback) contains a `composer.json`:

```
sudo -u agent -H composer update --no-interaction
```

**Run this in the primary too** (confirmed by cananian, 2026-09-07):
gate it on `composer.json` alone, not on the primary exclusion. The brief
scoped both steps with "every repository except the primary", but only
one of them has a reason to be. `safe-reset` skips the primary to
preserve the host's work in progress (§2); `composer update` touches
nothing but `vendor/`, so there is no work for it to disturb. Skipping it
would mean that when the primary *is* `mediawiki/core` — the common case
for core work — there is no `vendor/` at all, and §6's install fails
outright.

Notes:

- `composer update` rather than `install` is right for MediaWiki, and it
  does not dirty the tree: `composer.lock` is **gitignored in core**.
- `--no-interaction` matters; there is no tty in `commands.install`.
- Consider `--no-progress` too: the output is going into `sbx create`'s
  log, and composer's progress bar renders badly there.
- Core's `post-update-cmd` runs `ComposerVendorHtaccessCreator`, which is
  fine and expects nothing of the environment.
- This is the step that needs `repo.packagist.org` (§7.1).

**Core's `composer.local.json`** [added 2026-09-18, `sbx/NOTES.md` §89].
Before core's `composer update`, the setup writes core's
`composer.local.json` with quibble's `CreateComposerLocal` globs,
`extensions/*/composer.json` and `skins/*/composer.json`. Core's update
then installs every extension's and skin's libraries into core's
`vendor/`, which is the one that phan and the wiki read. Every other repo
keeps its own `composer update` too, as in CI: its `composer test` and
`composer phan` run from its own `vendor/bin`. One addition to
quibble's file, only when Parsoid is a workspace clone:
`autoload.exclude-from-classmap` lists `vendor/wikimedia/parsoid/`.
Without a clone, the wiki uses that vendor copy, so it stays. The Parsoid checkout is linked into
`extensions/` and so is merged, while core also requires
`wikimedia/parsoid`; without the exclusion, each update prints one
warning per Parsoid class (359). An existing file that differs is left
alone, as for `.env`. The symlinks (§8 step 5) must exist
first, because the globs find the extensions through them.

## 4. Step: `.env` in the core clone

Written after the symlinks, before `npm install`, at
`<core work path>/.env`. `.env` is gitignored in core, so writing it
never dirties the tree.

### 4.1 `.env` alone does nothing outside MediaWiki-Docker

The linked page is *Selenium/How-to/Run tests targeting MediaWiki-Docker*,
and that is precisely its scope: `docker compose` auto-loads `.env` from
the project directory and injects the variables into the container.
Nothing else reads it. Verified in core:

- `tests/selenium/wdio-mediawiki/wdio-defaults.conf.js:23` and
  `Gruntfile.js:9` read **`process.env.MW_SERVER` / `MW_SCRIPT_PATH`**
  directly and throw if they are unset.
- `dotenv` is still in `package.json` devDependencies but **nothing in
  core requires it any more** (only a `wdio-mediawiki` changelog entry
  mentions it).
- `api-testing` uses `.api-testing.config.json`, not `.env`.

So write the file — it is the documented artifact, it costs nothing, and
it is what an engineer will look for — but **also put the same values in
the kit's `environment.variables`**, which `build_kit_spec` already
emits, so `npm run selenium-test` and `grunt qunit` actually work in a
sandbox that is not running MediaWiki-Docker. That is the part that makes
this step do anything.

**As implemented**, both halves go through one function:
`wmf_sbx_kit.mediawiki_env_vars()` calls
`wmf_sbx_setup.parse_install_params()` against the *host's* core checkout
to fill `MW_SERVER`/`MW_SCRIPT_PATH`, and the setup script calls the same
parser against the *clone* for `.env`. That's the one place anything
imports the setup script (it can't import anything itself — it ships as a
single standalone file), and it means the kit environment and the `.env`
cannot disagree. A user's `extra_environment` still overrides both.

**Confirmed live 2026-09-08**, with one correction. `MW_SCRIPT_PATH`
comes out as the **empty string**, and that is right: `/w` is the
docker-compose layout (core mounted at `/var/www/html/w`), while
`composer serve`'s docroot *is* the core directory, so `index.php` sits
at the root. `index.php?title=Special:Version` returns 200. Nobody should
"fix" this to `/w` — and nobody has to guess, because the value is parsed
out of core's own `mw-install:sqlite` script rather than written down
here.

The correction: the *other* variables in this block —
`REPO_ENVIRONMENT_VARS`' `MW_INSTALL_PATH`, `MW_CORE_REPO`, `PARSOID`,
`MW_VENDOR_REPO` — were being set to the **host** path each repo resolved
to. sbx mounts the host repo at that same literal path inside the
sandbox, so the variable pointed at something real: the read-only mirror.
Every maintenance script then ran against the host's checkout and its
`LocalSettings.php`. Fixed 2026-09-08 — they now go through
`parallel_path()`, like everything else that names a repo inside the
sandbox. See `sbx/NOTES.md` §30.

### 4.2 The values disagree with the installer — pick one server

`mediawiki-core-clean/.env` describes a MediaWiki-Docker wiki:
`MW_SERVER=http://localhost:8080`, `MW_SCRIPT_PATH=/w`,
`MEDIAWIKI_PASSWORD=dockerpass`. But `composer mw-install:sqlite`
installs `--server=http://localhost:4000 --scriptpath= --pass
adminpassword`, and `composer serve` starts `php -S 127.0.0.1:4000`.
Copied verbatim, the `.env` would point the test harnesses at a port
nothing listens on, with the wrong password.

**Recommendation: keep the file's shape, take the server values from the
install**, which §6.1 settles by staying on core's own
`mw-install:sqlite`. `composer serve` (`php -S 127.0.0.1:4000`) is how a
wiki actually gets served in this sandbox — there is no
docker-in-sandbox; see the root `SECURITY.md` on why the Docker socket
never enters a sandbox — and it agrees with those values by
construction, since both live in core's `composer.json`:

```
MW_SCRIPT_PATH=
MW_SERVER=http://localhost:4000
MW_DOCKER_PORT=4000
MEDIAWIKI_USER=Admin
MEDIAWIKI_PASSWORD=adminpassword
XDEBUG_CONFIG=
XDEBUG_ENABLE=true
XHPROF_ENABLE=true
MW_DOCKER_UID=<agent uid>
MW_DOCKER_GID=<agent gid>
```

Empty `MW_SCRIPT_PATH` is explicitly supported —
`Gruntfile.js:167` says so in as many words ("empty string is valid, e.g.
for docroot installs"). uid/gid come from `pwd.getpwnam("agent")`, not a
hardcoded 1000.

The alternative — serve at `8080` under `/w` to match the file verbatim —
means abandoning `mw-install:sqlite` for a hand-written install command
*and* giving up `composer serve` (which hardcodes 4000 and a docroot
layout) for a hand-rolled router. Not worth it.

Since these values now come from core's `composer.json` rather than from
us, the plan file's `mediawiki` block carries them as **cached copies for
`.env` generation, with core as the authority**. Cheapest robust option:
parse `scripts."mw-install:sqlite"` out of the core clone's
`composer.json` at setup time and pull `--server`/`--scriptpath`/`--pass`
from it, so the `.env` cannot drift if core changes the script. Fall back
to the plan file's values if the script is missing or unparseable.

### 4.3 Where the content lives

**Inline in `wmf_sbx_setup.py`** as a template constant. The argument is
concrete rather than aesthetic: `write_kit_dir` copies the setup script as
a single standalone file into the kit, so a separate data file needs new
plumbing, and the file needs substitution (port, password, uid/gid)
anyway. The two shell scripts in §2.3 are different — they are
executables, not templates, so they ship as files.

## 5. Step: `npm install` in core

```
sudo -u agent -H npm install --no-audit --no-fund
```

in the core work path. Notes:

- Core's `package-lock.json` **is** tracked, so `npm install` can modify
  it and leave the clone dirty — which then makes a later `git safe-reset`
  refuse. `npm ci` avoids that and is faster and lockfile-exact.
  **Recommendation: `npm ci`**, falling back to `npm install` if there is
  no lockfile. The brief says `npm install`; this is the one place the
  distinction has a downstream consequence, so it is worth flagging rather
  than silently choosing.
- No `postinstall` browser downloads: core's devDependencies are
  grunt/jest/qunit/wdio/codex — `karma-chrome-launcher` and the wdio
  packages do not fetch a browser at install time.
- Needs `registry.npmjs.org` (§7.1) and a node (§7.2).

## 6. Step: the install

### 6.1 `mw-install:sqlite` installs no extensions

Core's script is:

```
@php maintenance/run.php install --server=http://localhost:4000 \
  --dbtype sqlite --with-developmentsettings --dbpath cache/ \
  --scriptpath= --pass adminpassword MediaWiki Admin
```

`includes/Installer/CliInstaller.php:124-152` is unambiguous about what
that produces:

- **skins** — the `else` branch runs `findExtensions('skins')`, so all
  skins present in `skins/` are detected and enabled **by default**;
- **extensions** — only if `--extensions=A,B` or `--with-extensions` is
  given. Neither is. `_Extensions` is never set, and the generated
  `LocalSettings.php` gets **no `wfLoadExtension` lines at all**.

Fix by appending the flag to the composer script rather than replacing it:

```
sudo -u agent -H composer mw-install:sqlite -- --with-extensions
```

Composer appends extra arguments to the script's last command, so this
runs core's own `mw-install:sqlite` verbatim with `--with-extensions` on
the end, after the `MediaWiki Admin` positionals. That parses correctly:
`MaintenanceParameters::loadWithArgv` (`maintenance/includes/`, lines
366–391) walks argv in order and treats any `--foo` as an option wherever
it appears — only a *literal* bare `--` terminates option parsing, and
composer's `--` is consumed by composer itself and never reaches PHP.

Staying on the composer script rather than hand-rolling
`maintenance/run.php install ...` means core owns the install parameters
and we do not have to track changes to them. The consequence is that
`--server=http://localhost:4000`, `--scriptpath=`, `--dbpath cache/` and
`--pass adminpassword` are **facts to read, not knobs to set** — which is
what §4.2 keys the `.env` off.

Ordering follows from this: the symlinks must exist before the install,
because `findExtensions` scans `extensions/` and `skins/` on disk. That is
the brief's ordering already.

### 6.2 Related details

- `--dbpath cache/` puts `my_wiki.sqlite` inside the core clone.
  `cache/` is gitignored, so the tree stays clean.
- `LocalSettings.php` is gitignored too.
- If `LocalSettings.php` already exists the installer refuses; under
  `wmf-sbx-resume` this step must be skipped when the file is present
  unless something explicitly asks for a reinstall (§8).
- `--with-developmentsettings` pulls in `DevelopmentSettings.php`
  (`wgShowExceptionDetails` etc.) — right for a sandbox, keep it.

## 7. Network and image prerequisites

### 7.1 Package registries — RESOLVED, no kit change needed

`wmf_sbx_kit.EXTRA_DOMAINS` is `["github.com"]`, the wiki-family domains
come from `profiles/wmf-engineer.json`, and neither list mentions
packagist or npm — so the question was whether sbx's own base policy
covers them. It does. `wmf-sbx policy ls wmf-claude-sbx --type network
--wide` on the host (cananian, 2026-09-07) shows five `local`-source
policies that apply to **`all`** sandboxes, alongside the `kit`-source row
for our own domains. The one that matters here is
**`default-package-managers`**, which allows (among ~60 entries):

```
**.packagist.org:443   packagist.org:443   packagist.com:443
registry.npmjs.org:443 npmjs.org:443       npmjs.com:443   **.yarnpkg.com:443
nodejs.org:443         nodesource.com:443
```

Confirmed live from inside a real sandbox, not just read off the table —
`wmf-sbx exec wmf-claude-sbx curl -sSI` returns `HTTP/2 200` for both
`https://repo.packagist.org/packages.json` (matched by the
`**.packagist.org` wildcard) and `https://registry.npmjs.org/npm`.

`default-code-and-containers` allows `github.com:443`,
`**.github.com:443`, and `**.githubusercontent.com:443` — which covers
composer's GitHub tarball fallback (`codeload.github.com`) without a
separate entry. `default-os-packages` covers the `apt-get` step. So §3
and §5 work as-is, and **`EXTRA_DOMAINS`' `github.com` entry is
redundant** with `default-code-and-containers`.

Two things to keep in view rather than act on:

- These policies are **`source: local`**, not `org` — sbx's shipped local
  defaults on *this* machine. Another engineer, or a machine under a
  stricter org policy, could have a narrower set, and the kit would then
  silently stop being able to install dependencies. Declaring
  `packagist.org` and `registry.npmjs.org` in `EXTRA_DOMAINS` anyway
  would make the kit's requirements explicit and self-contained at no
  real cost — it cannot *widen* anything beyond what the local and org
  layers already permit (per §3's "can only narrow, never widen"), so it
  is a documentation gesture, not a governance hole. Worth doing;
  low priority.
- Leave `github.com` in `EXTRA_DOMAINS` for the same reason. It is
  redundant today, not wrong.

This resolves the "whatever `sbx`'s own default policy is" unknown that
`NOTES.md` §9 has been carrying — see `NOTES.md` §24 for the full dump
and the other things it settles.

### 7.2 Node — RESOLVED, also no kit change needed

`BASE_PACKAGES` installs php + composer and **no node or npm**, so the
question was whether the base image supplies one. It does. Measured from
inside a live `wmf-claude-sbx` sandbox, 2026-09-07:

```
/usr/bin/node   v22.22.1     (nodejs 22.22.1+dfsg+~cs22.19.15-1ubuntu1)
/usr/bin/npm    9.2.0        (npm 9.2.0~ds3-1)
Ubuntu 26.04 LTS, from archive.ubuntu.com resolute/universe
```

Both are already at their apt candidate version, so `apt-get install
nodejs` would be a no-op — no `BASE_PACKAGES` entry, no nodesource, no
pinned tarball. Node 22 is what MediaWiki CI targets.

The npm 9.2.0 / node 22 pairing looks odd (node 22 normally bundles npm
10) because Ubuntu unbundles npm and ships it separately. It does not
matter for §5: core's `package-lock.json` is `lockfileVersion: 3`, which
npm 9 reads and writes natively, so `npm ci` is fine.

While in there, the rest of the toolchain checks out for §3 and §6 too:
PHP **8.5.4** against core's `"php": ">=8.3.0"` and
`PHPVersionCheck.php`'s `$minimumVersion = '8.3.0'` — a floor with no
ceiling, so 8.5 is fine — Composer 2.9.5, and every extension the
installer needs already loaded (`sqlite3`, `pdo_sqlite`, `intl`,
`mbstring`, `xml`, `apcu`), plus `git-review`, `convert`, and
`rsvg-convert` from `BASE_PACKAGES`.

## 8. Ordering, failure policy, idempotence

Order inside `wmf_sbx_setup.main`:

1. `install` the two git helper scripts into `/usr/local/bin` (§2.3).
2. Existing `setup_repo` loop — the parallel tree.
3. **Start the git daemon here**, not at the end. Today it is last;
   moving it in front of the slow MediaWiki phase means the host can fetch
   from and inspect the sandbox while composer and npm are still running,
   instead of after. It has no dependency on any of what follows.
   (`configure_remotes` (§8.1) actually runs *inside* step 2, per clone,
   before this — it's part of making a clone, not a MediaWiki step, and
   it must precede step 4 in any case.)
4. `git safe-reset --force <remote>` per non-primary clone (§2, §8.1).
5. Symlinks into core (`DESIGN-dependency-walk.md` §7), then core's
   `composer.local.json` (§3). Before 2026-09-18 the symlinks came after
   composer; the merge needs them first.
6. `composer update` per clone with a `composer.json` (§3).
7. `.env` (§4).
8. `npm ci` in core (§5).
9. Install (§6).

Steps 4–9 run only when core is present *and* cloned rather than `:ro` —
the same precondition the symlink step already has, for the same reason.

**Failure policy: warn and continue for steps 4–6, fail the sandbox for
the core steps (7–9).** (Before the reorder, the symlinks were a core
step in this list, but `link_into_core` has always only warned.) A single extension whose
`composer update` fails should not cost you the whole sandbox; you can fix
it in place. A failed install means there is no working wiki, which is the
thing the sandbox was created for, and it should be loud. Every skipped or
failed step gets one line of output naming the repo and the reason, and
the set of them is summarised at the end — `sbx create` output is long,
and a warning in the middle of a composer log is a warning nobody reads.

**Idempotence**, for `wmf-sbx-resume` re-running this:

- `git safe-reset` self-guards on a dirty tree; leave that alone and let
  it refuse.
- `composer update` / `npm ci` are naturally idempotent, just slow.
- Symlinks: replace-if-different, warn on a real directory
  (`DESIGN-dependency-walk.md` §7).
- `composer.local.json`: write only if absent; leave a different one
  alone and say so.
- `.env`: overwrite only if absent or unmodified from our template;
  otherwise leave it and say so. An engineer who edited it means it.
- Install: skip when `LocalSettings.php` exists.

### 8.1 `configure_remotes`: `origin` = Gerrit, `local` = the host mirror

**Added 2026-09-08** (cananian), superseding §2.1's "not v1". Reasoning
and the measured problem it fixes are in `sbx/NOTES.md` §31.2; the
mechanics, since they belong to this document:

Per non-`:ro` clone, immediately after `git clone --shared` and its
`chown`, as the `agent` user:

```
git remote rename origin local
git remote add origin https://gerrit.wikimedia.org/r/<project>
git fetch --quiet origin
```

The URL is **not** derived in the sandbox. `wmf_sbx_create.clone_url`
already maps `gerrit:`/`gitlab:` canonicals to anonymous https URLs, and
`wmf_sbx_setup.py` may not import its siblings (it is copied into the kit
alone), so the URL travels in the plan file as a per-repo `upstreamUrl`
— optional, and absent for a repo with no canonical, an unknown forge,
*and* no discoverable host origin.

That third case matters: a raw-path repo argument (`is_raw_path`) with no
exact `repos.yaml` rule has no canonical at all, and until 2026-09-11
`upstream_plan` simply gave up on it — which in practice meant every
GitLab clone, since the only re-identification `canonicals_for_kit` had
was `.gitreview` parsing, a Gerrit-only convention GitLab checkouts never
have (`sbx/DESIGN-repo-resolution.md`'s `reverse_resolve`). Fixed by
`upstream_plan`'s `host_upstream_url` fallback
(`sbx/src/wmf_sbx/create.py`): when there's no canonical-derived URL, it
runs `git -C <host_dir> remote get-url origin` on the host's own
checkout and uses that, if and only if it's `http(s)://` (same no-SSH-
agent reasoning as below). This needs no forge-specific knowledge at all
— it works for any already-cloned directory regardless of what it was
cloned from, which is exactly the "any cloned directory" the mechanism
was supposed to cover in the first place.

`configure_remotes` returns the remote name step 4 should use, and step 4
passes it explicitly rather than relying on `git-safe-reset`'s own
`origin` default: which remote is right depends on whether the swap
completed. The fallbacks, all warn-and-continue:

| situation | mirror is | `origin` is | safe-reset uses |
| --- | --- | --- | --- |
| swap succeeded | `local` | Gerrit | `origin` |
| no `upstreamUrl` | `origin` | — | `origin` |
| rename failed | `origin` | — | `origin` |
| add or fetch failed | `local` | absent/unfetched | `local` |

Two deliberate choices. The rename happens **only** when there is an
upstream to install as `origin`, so `local` means the same thing in every
clone that has one. And the URL is https, not `ssh://`: the sandbox has
no SSH agent by design (`NOTES.md` §8), and nothing pushes from inside it
— the host fetches from the sandbox's daemon.

The `fetch` is the only step here that needs the network. It is cheap:
the clone's alternates line already makes every object the host had
reachable, so it transfers only what upstream has moved on by. If the
sandbox is created offline it fails, warns, and the clone falls back to
matching the host — the pre-2026-09-08 behaviour.

A fourth command, added after the first run that used this:

```
git config checkout.defaultRemote <the remote safe-reset will use>
```

Two remotes both carrying a `master`, and a clone whose HEAD was a topic
branch (so there is no *local* `master` either), is enough to make
`git checkout master` — `git-safe-reset`'s first command — refuse:
`fatal: 'master' matched multiple (2) remote tracking branches`. That is
exactly what happened to Parsoid in the `mw-translate` sandbox, and it is
a bug this section introduced: before the swap there was only one remote,
so DWIM was never ambiguous. The config above is git's own suggested
remedy (it prints it in the hint), and it is set to whichever remote the
table lands on, so the branch git creates tracks the same remote the very
next `git reset --hard` uses.

### 8.2 Which clones keep the host's branch

**Revised 2026-09-08** (cananian): §2's rule was "everything but the
primary gets reset". It is now **everything the engineer did not name on
the command line**. Naming a repo is how you say *this is what I'm here
to work on*; a repo that arrived through the dependency walk is there to
build against, and upstream `master` is the right thing to build against.

`wmf_sbx_create` can tell the two apart for free: `expand_dependencies`
records an origin chain per *discovered* path, so an empty chain is
precisely "you typed this". Those paths travel in the plan as a per-repo
`"requested": true`, and `wmf_sbx_setup.repos_to_leave_alone` reads them.
The primary is added to that set unconditionally as a belt-and-braces
guard, so a plan carrying no flags at all still can't destroy work in
progress.

`wmf-sbx-create --reset-all` empties the set — primary included, since an
"all" that quietly exempted one repo would be a trap. It is for the case
cananian described: list a dozen repos, reset the lot, then hand-pick from
inside the two or three you actually have work in progress in. The flag
rides in the plan as `"resetAll"`, so it warns and does nothing when an
explicit `--kit` is supplying its own plan.

## 9. Cost, and why this makes template caching load-bearing

For a Translate-sized closure this adds a `composer update` per repo
(tens of seconds each) plus core's `npm ci` and install — minutes of
`sbx create`, every time, all of it network-bound and none of it
repo-specific. `sbx/DESIGN-template-caching.md` (template caching on kit
content) was already the plan; this is what turns it from an optimisation
into the thing that makes the design usable. This section's worry — that
the repo *set* determines the symlinks and the install, so the cache key
would need to cover more than that design's §3 assumes — turns out not to
hold: these steps run per-invocation, against each sandbox's own
`--reference` clones, and are never baked into the shared template. See
that design's §3, "Why the Layer C caveat doesn't hold," which covers
exactly this.

## 10. Files

| Path | Change |
| --- | --- |
All done, 2026-09-07.

| Path | Change |
| --- | --- |
| `sbx/bin/git-safe-reset` | new — moved from `sbx/reference/` |
| `sbx/bin/git-review-check` | new — copied from `~/bin` |
| `sbx/reference/git-safe-reset` | deleted (one source of truth) |
| `sbx/bin/wmf_sbx_setup.py` | plan-file argv; helper install; safe-reset/composer/env/npm/install steps; daemon moved earlier |
| `sbx/bin/wmf_sbx_kit.py` | write `files/home/wmf-sbx-plan.json` and `files/home/bin/*`; `EXTRA_DOMAINS` (§7.1); `.env` vars into `environment.variables` |
| `sbx/bin/wmf_sbx_create.py` | build the plan dict (primary, canonicals, link names, install params) |
| `sbx/bin/tests/test_wmf_sbx_setup.py` | extend — every step is a `run=` injection point, so all of this is testable in-sandbox |

## 11. Decisions taken here, for confirmation

1. `git safe-reset --force` in parallel clones (§2.2) — the review check
   protects work that cannot exist in a seconds-old disposable clone.
2. `composer update` in the primary too, not just non-primary (§3) —
   confirmed by cananian. The primary is excluded from `safe-reset` only,
   and for a specific reason: its host copy is assumed to hold work in
   progress that the sandbox is there to continue (§2).
3. `.env` values follow the installer (`localhost:4000`, empty script
   path, `adminpassword`), not `mediawiki-core-clean/.env` verbatim
   (§4.2), and the same values also go into the kit's environment (§4.1).
4. `npm ci` over `npm install` (§5), because `package-lock.json` is
   tracked in core.
5. `composer mw-install:sqlite -- --with-extensions` (§6.1) — confirmed by
   cananian. Skins need no flag; extensions do, and without it no
   extension is enabled and the dependency walk buys nothing.

**Nothing is blocked any more.** §7.1 (packagist / npmjs egress) is
resolved by sbx's own `default-package-managers` policy, and §7.2 (node)
by the base image shipping node 22.22.1 / npm 9.2.0. Both were expected
to need kit changes; neither does. Implementation can start.
