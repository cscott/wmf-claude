# MediaWiki dependency walk and in-sandbox wiring

**Status: implemented** (2026-09-07) — §2–§6 are
`sbx/bin/wmf_sbx_deps.py`, driven by
`wmf_sbx_create.expand_dependencies()`; §7 is
`wmf_sbx_setup.link_into_core()`, fed by `wmf_sbx_create.link_plan()`.
Four deltas from what's designed below:

- Gitiles fetches are retried on `HTTP 429`, honouring `retry-after`
  (bounded at 60s, three attempts). Wikimedia's edge really does
  rate-limit a burst of manifest fetches — see `sbx/NOTES.md` §27.
- A manifest that *couldn't be fetched* is distinguished from one that
  doesn't exist (the `UNREACHABLE` sentinel). The first makes the closure
  incomplete, so `wmf-sbx-create` lists the affected repos and refuses to
  create; `--dry-run` still prints the plan, marked possibly-short.
- The escape hatches are spelled `--no-deps`, `--no-dev`, `--no-suggests`.
- The link plan is computed on the *host* (`link_plan()`) and shipped to
  the sandbox in a JSON plan file rather than being recomputed in-sandbox
  — see §7 and `sbx/DESIGN-setup-steps.md` §1.

Today `wmf-sbx-create gerrit:mediawiki/extensions/Translate` gives you a
sandbox with exactly one repo in it, and a MediaWiki that cannot load.
The goal is for that command to be equivalent to

```
wmf-sbx-create gerrit:mediawiki/extensions/Translate \
               gerrit:mediawiki/extensions/UniversalLanguageSelector \
               gerrit:mediawiki/core \
               gerrit:mediawiki/skins/Vector
```

with the extensions and skins symlinked into the core checkout so the wiki
actually runs. Two halves: a **host-side dependency walk** in
`wmf-sbx-create` (§2–§6) and an **in-sandbox wiring step** in
`wmf_sbx_setup.py` (§7).

## 1. One divergence from the brief, up front

The instruction was to symlink `core/extensions/<Bar>` where `Bar` is the
`name` field from `extension.json`. **`name` is the wrong key for the link
name, and using it would break about 15% of extensions** — including
several that cannot work at all, because their `name` contains a space.

MediaWiki has two distinct namespaces here, and they are not the same
string:

- **`wfLoadExtension( 'X' )` loads `$IP/extensions/X/extension.json`.** The
  directory name is the identifier. It is conventionally the Gerrit project
  basename, and that is what the submodule paths in `mediawiki/extensions`
  use.
- **`requires.extensions` keys are matched against the `name` field.**
  Confirmed in core: `ExtensionProcessor` stores
  `$this->credits[$name] = $credits` keyed by `$credits['name']`
  (`includes/Registration/ExtensionProcessor.php:833`), that array becomes
  `VersionChecker`'s `$this->loaded`
  (`ExtensionRegistry.php:562`, `:468`), and
  `VersionChecker::handleExtensionDependency()` looks up
  `$this->loaded[$dependencyName]`. So a dependency key is a *credits
  name*, not a directory.

Measured over cananian's local checkout (298 `extension.json`/`skin.json`
files under `~/Wikimedia/Extensions` and `~/Wikimedia/Skins`), **44 have
`name` != directory basename.** A sample:

| directory (== Gerrit basename) | `name` field |
| --- | --- |
| `AbuseFilter` | `Abuse Filter` |
| `SyntaxHighlight_GeSHi` | `SyntaxHighlight` |
| `cldr` | `CLDR` |
| `intersection` | `DynamicPageList` |
| `timeline` | `EasyTimeline` |
| `PdfHandler` | `PDF Handler` |
| `TwnMainPage` | `Translatewiki.net main page` |

cananian's own `~/Wikimedia/core` settles it empirically — every symlink in
it is named after the **directory**, never the `name`:

```
extensions/SyntaxHighlight_GeSHi -> .../Extensions/SyntaxHighlight_GeSHi
extensions/Cite                  -> .../Extensions/Cite
skins/Vector                     -> .../Skins/Vector
```

and `LocalSettings.php` calls `wfLoadExtension( 'cldr' )`, lowercase, for
the extension whose `name` is `CLDR`.

But the Parsoid example in the brief is also real and basename alone gets
it wrong: `gerrit:mediawiki/services/parsoid` must be linked as
`extensions/Parsoid`, not `extensions/parsoid`. So neither rule is
universal. **Proposed rule, which satisfies both:**

```
link_name(canonical) =
    config override for this canonical, if any        # explicit escape hatch
    else basename(canonical)                          # mediawiki/extensions/*, mediawiki/skins/*
    else the `name` field                             # anything outside those two trees
```

Within `mediawiki/extensions/` and `mediawiki/skins/` the basename is
authoritative and matches all 298 local cases. Outside them — which today
means `mediawiki/services/parsoid` and whatever else gets added — fall back
to `name`, which is what makes the Parsoid case come out right. Warn when
the fallback fires, so a surprising link name is visible rather than
silent.

(Worth knowing: cananian's real `LocalSettings.php` doesn't symlink Parsoid
into `extensions/` at all — it sets `$PARSOID_INSTALL_DIR` and registers an
autoloader intercept. The symlink is still the right default for a fresh
sandbox; just don't expect it to match the host setup exactly.)

## 2. Parsing

For every workspace directory with an `extension.json` or `skin.json` at
top level, read `name`, `requires`, `dev-requires`, `suggests`. Both files
use the same schema (`docs/extension.schema.v2.json` in core; there is no
separate `skin.schema.json`).

Per that schema, `requires` and `dev-requires` accept
`MediaWiki` / `platform` / `extensions` / `skins`; `suggests` accepts the
same minus `MediaWiki`. **Only the `extensions` and `skins` sub-objects
generate dependencies** — `MediaWiki` and `platform` are version and
PHP-ability constraints with no repo behind them.

`skins` is in the schema and is worth honouring even though the brief only
mentioned extensions: a `requires.skins` key maps to
`gerrit:mediawiki/skins/<key>` exactly as extensions map to
`gerrit:mediawiki/extensions/<key>`.

Robustness, from the same corpus: **one of the 298 files
(`Extensions/AllowExternalImages/extension.json`) does not parse as
JSON.** Malformed or missing JSON must produce a warning and be treated as
a leaf, never abort the walk — one bad extension in a dependency closure
should not take down sandbox creation.

## 3. The walk

```
frontier  := the repos named on the command line
seen      := {}
while frontier:
    r := frontier.pop()
    if r in seen: continue
    seen.add(r)
    manifest := extension.json or skin.json at the top level of r, if any
    if manifest:
        frontier += gerrit:mediawiki/core                        # implicit
        for each key in the selected dependency fields:
            frontier += gerrit:mediawiki/{extensions,skins}/<key>
    if r == gerrit:mediawiki/core:
        frontier += gerrit:mediawiki/skins/Vector                # implicit
```

Field selection: `requires` always; `dev-requires` unless `--no-dev`;
`suggests` unless `--no-suggests`. Both default to *included*, per the
brief.

The `seen` set handles cycles, and there is a guaranteed one:
`Vector` has a `skin.json`, so it depends on `core`, and `core` implicitly
depends on `Vector`. Mutual `requires` between two extensions is also
possible. A visited set is not optional here.

**Dependency key → Gerrit project is a heuristic**, because the key is
semantically a credits name (§1) while the project path needs a directory
name. It holds well: across all 35 distinct dependency keys in the local
corpus, every key that resolves to a local checkout resolves to one whose
directory *is* that key — zero counterexamples. The 9 that don't resolve
locally (`Echo`, `EventLogging`, `VisualEditor`, `TemplateData`,
`UniversalLanguageSelector`, `ExtJSBase`, `OOJSPlus`, `ZeroBanner`,
`IPReputation`) are simply not cloned on this machine, and all are real
`mediawiki/extensions/<key>` projects. Two guards:

- Reject keys that cannot be a Gerrit project path — anything with a space
  or a slash. `requires: {"Abuse Filter": "*"}` is legal MediaWiki and
  would produce a nonsense project; warn and skip rather than trying.
- After a dependency is fetched, compare its `name` field against the key
  that pulled it in. A mismatch means the heuristic guessed a project that
  happens to exist but isn't the extension that was asked for. Warn.
- Both are overridable by a `dependency_overrides:` map in
  `~/.config/wmf-sbx/repos.yaml`, mapping a dependency key to a canonical.

Ordering: the result is a set, but the command line should be
deterministic — emit the explicitly-named repos first, in the order given
(so the primary workspace stays the primary), then the discovered ones
sorted by canonical.

## 4. Where the manifests come from

The awkward part: to walk `Translate` we need *its* `extension.json`, and
to recurse into `UniversalLanguageSelector` we need ULS's — but ULS may not
be cloned yet. Today `wmf_sbx_create.main()` resolves everything, prints
the plan, and only then calls `do_clone`. The walk breaks that ordering:
resolution becomes iterative (resolve → obtain manifest → resolve more).

Two ways to obtain a manifest for a not-yet-cloned repo:

**(a) Clone first, then parse.** Simple, no new dependency, and the clone
is needed eventually anyway. But it means `wmf-sbx-create` starts cloning
before it can show the full plan, and `--dry-run` becomes structurally
unable to complete the walk — it can only report the closure reachable
from what is already on disk, and must say so explicitly rather than
printing a plausible-looking short list.

**(b) Fetch the manifest over HTTP from Gerrit's gitiles**, no clone:

```
https://gerrit.wikimedia.org/g/<project>/+/refs/heads/master/extension.json?format=TEXT
```

returns the file base64-encoded. Verified live from this sandbox
(2026-09-07): `mediawiki/extensions/Translate` → 200, 80 KB of base64;
`mediawiki/services/parsoid` and `mediawiki/skins/Vector/skin.json` → 200;
`mediawiki/extensions/Echo` → 200. **A nonexistent project returns 401,
not 404** (gitiles hides existence behind auth), so "401 or 404 → unknown
project, warn and treat as a leaf" — we cannot distinguish absent from
forbidden.

**Recommendation: (b) for discovery, (a) for what actually gets mounted.**
It keeps the existing "resolve everything, print the plan, then clone"
control flow intact, makes `--dry-run` honest and complete, and avoids
cloning repos that the walk might later discover are unnecessary. Prefer a
local checkout's manifest when one exists (no network, and it matches what
will be mounted); fall back to gitiles only for repos not yet on disk.
Cache fetched manifests for the process lifetime — diamond dependencies
are common (`UniversalLanguageSelector` shows up via three different paths
in the `CommunityRequests` closure).

Caveat to document: gitiles serves `refs/heads/master`, while the clone
will check out whatever the default branch is. For MediaWiki repos these
agree; if the walk ever disagrees with the cloned tree, the cloned tree is
the truth. The mismatch is only ever a wrong *guess about which repos to
mount*, never wrong content.

## 5. Blast radius

Measured closures over the local corpus (all fields on; `+2` for the
implicit `core` and `Vector`):

| root | full closure | with `--no-suggests` |
| --- | --- | --- |
| `Cite` | 1 | 1 |
| `Translate` | 2 | 2 |
| `DiscussionTools` | 3 | 3 |
| `WikiLambda` | 3 | 3 |
| `BlueSpiceSocial` | 4 | 4 |
| `ContentTranslation` | 5 | 5 |
| `CommunityRequests` | **9** | 3 |

So the realistic worst case here is ~11 workspaces including core and
Vector, which is exactly the size of the sandbox already known to work.
Not alarming — but `CommunityRequests` triples under `suggests`, which is
what earns `--no-suggests` its place. Print the discovered set and its size
before creating, so a surprising closure is visible before 11 virtiofs
mounts and 11 `--reference` clones happen.

## 6. Interaction with existing `wmf-sbx-create` behaviour

- **`find_nested_mount_conflict` keeps erroring, discovered repos
  included.** Passing a container directory like `~/Wikimedia/Extensions`
  wholesale *and* depending on `Translate` will auto-discover
  `~/Wikimedia/Extensions/UniversalLanguageSelector` nested inside that
  explicit mount, and the command fails.

  An earlier draft of this section proposed silently dropping such a
  discovered repo, on the grounds that it is already reachable inside the
  sandbox. cananian's call, and it is the right one: **fail loudly
  instead.** The container-directory shortcut is a shortcut, not good
  practice, and a silently dropped dependency doesn't announce itself — it
  surfaces later as failing tests inside the sandbox, which is a
  considerably worse experience for the agent doing the work than an error
  at create time. The escape hatch for someone who genuinely wants a
  parent directory wholesale and no dependency resolution is explicit:
  `--no-deps --no-dev --no-suggests`.

  What the error must do is *explain itself*, since the conflicting path
  may be one the user never typed: name the discovered repo, name the root
  whose manifest pulled it in, and name the explicit mount it collides
  with, then point at `--no-deps`. A bare "X is inside Y" is not enough
  when X was inferred.
- **Discovered repos are extras, never the primary.** The primary stays
  whatever was named first; `sbx create` requires it be read/write anyway.
- **Discovered repos are always clones, never `:ro` opt-outs.** You may
  well need to edit a dependency. They still get create-time `:ro` on the
  host-mirrored original like every other extra — that is orthogonal (see
  `sbx/DESIGN-parallel-clone-tree.md` §2).
- **Cloning cost.** Each discovered repo that isn't on disk gets a full
  `git clone` from Gerrit at create time. `core` alone is large. Worth a
  progress line per clone, which `do_clone` already prints.
- **Kit wiring** (`canonicals_for_kit` → `MW_CORE_REPO` and friends) sees
  the expanded list for free, which is a bonus: pulling in `core`
  implicitly now also wires up its environment variable.

## 7. In-sandbox wiring (`wmf_sbx_setup.py`)

New step, after `setup_repo` has built the parallel tree.

**Implemented as designed**, with one addition: the *decision* of what to
link where is made on the host, not in the sandbox.
`wmf_sbx_create.link_plan()` computes `{host_dir: (link_name, section)}`
— it has the canonicals and can read the manifests off the host checkouts
— and `wmf_sbx_kit.build_plan()` writes them into the kit as
`linkName`/`linkDir` fields of `wmf-sbx-plan.json`
(`sbx/DESIGN-setup-steps.md` §1). `link_into_core()` then only creates
symlinks; it doesn't re-derive names. Two reasons: the setup script runs
against clones that don't exist yet when the plan is built, and a link
name can contain characters (spaces, in the `name`-field cases) that the
old positional argv form couldn't carry.

**Superseded in part by `sbx/DESIGN-setup-steps.md` §8**, which places
this step in a longer chain (`git safe-reset` → `composer update` →
*symlinks* → `.env` → `npm ci` → install) and moves the git daemon to
*before* that chain rather than after it, so the host can fetch while the
slow MediaWiki phase is still running. The symlink logic below is
unchanged; only its position moved.

**Preconditions.** Run only if `gerrit:mediawiki/core` is in the repo list
*and* it took the clone path, not the `:ro` bind-mount path. A `:ro` core is
the host's own tree mounted read-only — writing symlinks into it is both
impossible and wrong. Skip with a clear message. (This is the brief's own
condition, and it holds up.)

**What to link.** For each other repo in the list:

```
canonical under gerrit:mediawiki/extensions/  ->  <core>/extensions/<link_name>
canonical under gerrit:mediawiki/skins/       ->  <core>/skins/<link_name>
otherwise, if it has an extension.json         ->  <core>/extensions/<link_name>
otherwise                                      ->  no link
```

with `link_name` per §1. The last two lines are what places
`mediawiki/services/parsoid` at `extensions/Parsoid`.

**Link target: the parallel path, not the host-mirrored original.** This is
the point of the whole exercise —
`/home/agent/Wikimedia/core/extensions/Cite` must point at
`/home/agent/Wikimedia/Extensions/Cite` (the writable clone), not at
`/home/cananian/Wikimedia/Extensions/Cite` (the read-only original, which
is also mounted and would silently work while discarding nothing but also
accepting nothing). Absolute parallel paths; `wmf_sbx_setup.parallel_path`
already computes them.

**The core clone starts clean**, which makes this safe: `extensions/*` and
`skins/*` are gitignored in core (only `.gitignore`, `README`, and
`.vsls.json` are tracked), so a fresh `git clone --reference` has empty
`extensions/` and `skins/` directories with no stale host-path symlinks to
step around.

**Idempotency**, for when this runs again under `wmf-sbx-resume`: if the
path exists and is a symlink, replace it if the target differs; if it
exists and is a real directory, leave it and warn loudly — that is either a
real checkout or a name collision, and clobbering it would destroy work.

## 8. Files

| Path | Change |
| --- | --- |
| `sbx/bin/wmf_sbx_deps.py` | new — manifest parsing, `link_name()`, key→canonical mapping, gitiles fetch, the walk |
| `sbx/bin/wmf_sbx_create.py` | run the walk after initial resolution; `--no-dev`, `--no-suggests`; drop nested discovered repos; print the closure |
| `sbx/bin/wmf_sbx_setup.py` | new `link_into_core()` step, gated on core-present-and-cloned |
| `sbx/bin/wmf_sbx_kit.py` | pass the extension/skin link plan through to the setup invocation |
| `sbx/bin/tests/test_wmf_sbx_deps.py` | new |
| `sbx/bin/tests/test_wmf_sbx_{create,setup}.py` | extend |

Testable in-sandbox: manifest parsing, `link_name`, the walk (with an
injected manifest fetcher), and the symlink step (real dirs in `tmp`) are
all pure. Only the gitiles fetch needs the network, and it should be
injectable so tests never touch it. The 298-file local corpus under
`~/Wikimedia` is available as a realistic fixture source — but tests should
use small committed fixtures, not that tree, since it isn't in the repo.

## 9. Open questions

1. ~~Should the walk be on by default, or opt-in?~~ **On by default**, per
   the brief. `--no-deps` is not optional insurance but a required part of
   the design: together with `--no-dev --no-suggests` it is the supported
   way to mount a container directory wholesale without the walk fighting
   it (§6). It also covers the case where someone doesn't want
   `wmf-sbx-create SomeExtension` cloning `core` unasked.
2. **`link_name` for the non-`mediawiki/{extensions,skins}` fallback** —
   §1 proposes falling back to the `name` field, which gives `Parsoid`.
   Confirm that's the wanted behaviour rather than an override table with
   `mediawiki/services/parsoid` hardcoded.
3. **Does anything need `dev-requires` to be a *different* mount policy?**
   Right now a dev dependency is mounted identically to a real one. That
   seems right, but it is worth noticing that `--no-dev` is the only lever.
4. Not in scope here, but the known destination: **`LocalSettings.php` is
   generated, not written by us.** It is gitignored in core, so a fresh
   clone has none and the wiki will not run even with every symlink in
   place. cananian's plan is to end the setup chain with core's own
   `composer mw-install:sqlite`, which expands to

   ```
   php maintenance/run.php install --server=http://localhost:4000 \
       --dbtype sqlite --with-developmentsettings --dbpath cache/ \
       --scriptpath= --pass adminpassword MediaWiki Admin
   ```

   and writes a `LocalSettings.php` reflecting what is actually in
   `extensions/` and `skins/` — i.e. exactly what §7 links there. That
   makes the symlink step the input to installation rather than a
   standalone convenience, which is a good reason to get the link names
   right (§1) rather than approximately right.

   Several setup steps come before it, so this is a later design, not this
   one. Two things to check when it is written: the composer script does
   **not** pass `--with-extensions` (`maintenance/install.php:87`, "Detect
   and include extensions"), so confirm whether the symlinked extensions
   get enabled without it; and the installer bakes in
   `--server=http://localhost:4000`, a port outside the set the local-web
   testing ladder currently opens (80/443/8080 — see `CLAUDE.md`).

   **Both settled by the first real run, 2026-09-08** (`sbx/NOTES.md`
   §30). The extensions are *not* enabled without the flag, so
   `install_mediawiki()` passes `-- --with-extensions`; with it,
   `LocalSettings.php` came out carrying `wfLoadSkin( 'Vector' )`,
   `wfLoadExtension( 'Translate' )` and
   `wfLoadExtension( 'UniversalLanguageSelector' )`, and `Special:Version`
   lists all three. Port 4000 turned out not to matter here: the
   local-web ladder is the *nono* launcher's mechanism, and under sbx a
   port reaches the host only via `wmf-sbx ports NAME --publish 4000`,
   which is not restricted to that set. Inside the sandbox `curl
   http://localhost:4000/…` just works.
