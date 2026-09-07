# Making `--kit` optional: templated MediaWiki kit generation

**Status: implemented** (2026-09-07) — `sbx/bin/wmf_sbx_kit.py`, called
from `wmf_sbx_create.main()`. Written against sbx 0.39.0; see the
0.42.1 addendum at the end for what that version adds.

`sbx create --kit PATH` points at a directory holding a `spec.yaml` --
environment variables, allowed network domains, and an apt-get install
list -- that gets baked into the sandbox at creation time. Before this,
every `wmf-sbx-create` user needed their own hand-maintained kit
directory, and the one cananian actually used
(`~/Wikimedia/mediawiki-kit/spec.yaml`) hardcoded their personal disk
layout (`/home/cananian/Projects/Wikimedia/...`), a stale legacy
`MW_INSTALL_PATHX` workaround, and personal preferences (`GERRITUA`,
`CLAUDE_CODE_DISABLE_1M_CONTEXT`, an extra `nano` install) inline with the
genuinely-shared MediaWiki dependency list -- none of which upstreams to
another engineer.

This splits that one file into three layers, each additive, implemented
in `sbx/bin/wmf_sbx_kit.py`:

## 1. Upstreamed, shared defaults (hardcoded in `wmf_sbx_kit.py`)

- `BASE_PACKAGES` -- the apt packages every MediaWiki dev sandbox needs
  (php + extensions, composer, imagemagick, git-review, ...). `nano` is
  *not* here -- it was cananian's personal editor preference, not a
  MediaWiki dependency, so it moves to layer 3.
- `EXTRA_DOMAINS` -- just `github.com` today. Composer pulls a number of
  MediaWiki dependencies straight from GitHub; this isn't in
  `profiles/wmf-engineer.json` (see layer 2) because that profile is for
  the unrelated nono-based sandbox launcher, so it's a deliberate
  kit-specific addition rather than something with another source of
  truth to draw from.
- `REPO_ENVIRONMENT_VARS` -- a canonical Gerrit path -> env var name(s)
  map: `gerrit:mediawiki/core` -> `MW_INSTALL_PATH`, `MW_CORE_REPO`;
  `gerrit:mediawiki/services/parsoid` -> `PARSOID`;
  `gerrit:mediawiki/vendor` -> `MW_VENDOR_REPO`. `MW_INSTALL_PATHX` is
  gone -- it was a temporary workaround pointing at a second `core`
  checkout (`mediawiki-core-clean`); per cananian, `MW_INSTALL_PATH` and
  `MW_CORE_REPO` should both simply point at wherever `mediawiki/core`
  itself was cloned or mounted.

## 2. Network domains sourced dynamically from `profiles/wmf-engineer.json`

The old kit's `network.allowedDomains` was a hand-copied subset of the
wiki-family domains already listed in `profiles/wmf-engineer.json`'s
`network.allow_domain` (used by the *other*, nono-based sandbox launcher)
-- two lists that can only drift apart. `wiki_family_domains()` reads
`profiles/wmf-engineer.json` directly and keeps only the plain hostname
strings matching `*.<label>.org` where `<label>` contains `wik` as a
substring -- covers `wikipedia`, `wikidata`, `wikibooks`, `wikiquote`,
`wikivoyage`, `wikisource`, `wikinews`, `wikiversity`, `wikifunctions`,
`wiktionary` (spelled without a second "i", but still contains `wik`),
and `mediawiki` itself, without needing to enumerate every TLD by hand.
Plain-string filtering also naturally drops the profile's
endpoint-scoped `{domain, endpoints}` entries (doc sites like
`docs.python.org`) -- `sbx` kit specs don't support path/method-scoped
access, and those entries aren't MediaWiki-specific regardless.

Confirmed this isn't just theoretical: `profiles/wmf-engineer.json`
already has `*.wikinews.org`, which the old hand-copied kit spec.yaml was
missing -- exactly the kind of drift this closes.

If `profiles/wmf-engineer.json` can't be read (e.g. `wmf_sbx_kit.py` run
outside a `wmf-claude` checkout), `wiki_family_domains()` warns to stderr
and falls back to a small static list rather than failing kit generation
outright.

## 3. Per-user extras from `~/.config/wmf-sbx/repos.yaml`

Two new keys, alongside the existing `rules` and `extra_dependencies`
(see `sbx/DESIGN-repo-resolution.md`) -- defaulted to empty by
`wmf_sbx_resolve.load_config`:

```yaml
extra_environment:
  GERRITUA: "MW-CI-Tools/1.0 (...)"
  CLAUDE_CODE_DISABLE_1M_CONTEXT: "1"

extra_packages:
  - nano
```

`extra_environment` is applied *after* `REPO_ENVIRONMENT_VARS`, so a user
can also override an auto-wired variable if they ever need to.
`extra_packages` is appended after `BASE_PACKAGES` into a single
`apt-get install` line (the old spec.yaml's separate `apt-get install -y
nano` line was just organic growth, not a meaningful structural split).

## Identifying repos passed as raw paths

`wmf_sbx_create.is_raw_path` lets an argument like `~/Wikimedia/core`
skip name resolution entirely and mount as a literal, already-resolved
path (see `sbx/NOTES.md` #14) -- this is exactly how cananian's own
invocations pass `mediawiki/core`, `mediawiki/services/parsoid`, etc.
today, predating `repos.yaml` existing at all. A raw path has no
canonical Gerrit path attached, so on its own it can't be matched against
`REPO_ENVIRONMENT_VARS`.

`wmf_sbx_resolve.exact_rule_canonicals(rules)` closes this without a
network round-trip or shelling out to `git`: for every *exact* rule in
the config (no `**`/`{name}` -- e.g. `gerrit:mediawiki/core ->
~/Wikimedia/core`), the rule's `match` string already *is* its canonical
path, so it builds a reverse map of `realpath(expanded path) ->
canonical`. `wmf_sbx_create.canonicals_for_kit` looks up any raw-path
argument's resolved directory in this map before handing the resolved
list to `build_kit_spec` -- so once a user's `repos.yaml` has an exact
rule for `mediawiki/core`, passing that repo by its literal path still
wires up `MW_CORE_REPO`, no need to switch to passing it by name.

## Generation flow in `wmf_sbx_create.main()`

`kit = args.kit or config.get("kit")` is unchanged as the explicit-override
path (still useful for a non-MediaWiki kit, or testing). When `kit` is
still `None`:

- **`--dry-run`**: builds the spec dict and prints the resulting YAML to
  stderr as a preview -- nothing is written to disk, matching dry-run's
  existing "touch nothing" contract. The final printed `sbx create ...`
  line has no `--kit` flag in this case, since no real path was ever
  created.
- **Real run**: writes the generated `spec.yaml` into a fresh
  `tempfile.mkdtemp(prefix="wmf-sbx-kit-")` directory and uses that as
  `--kit`. `sbx create` only reads `--kit` at creation time (see this
  repo's top-level `CLAUDE.md` "Gotchas" for the general principle), so
  the temp directory is removed immediately after the `sbx create`
  subprocess returns, whether it succeeded or failed.

## Not done here

- No `--no-kit` flag to opt out of a kit entirely -- not asked for, and
  an empty user-supplied `--kit` directory already covers that case.
- No support for a *second* built-in kit template (e.g. a non-MediaWiki
  one) -- `wmf_sbx_kit.py` hardcodes one kit shape today. If a second
  ever appears, the config's existing `kit:` override (a fixed directory)
  still works as an escape hatch in the meantime.
- `sbx/reference/mediawiki-kit-spec.yaml` (the old hand-maintained,
  personal-path snapshot) is left in place as a historical record of what
  this replaces -- not deleted, not treated as a template.

## Addendum: what sbx 0.42.1 adds here (2026-09-08)

Everything above was designed and measured against 0.39.0. Two of 0.42's
kit-spec additions bear directly on this design, and neither is used yet:

- **`args:` in the spec, supplied as `--kit-arg name=value`.** The reason
  this generator writes a fresh `spec.yaml` into a temp directory on every
  invocation is that the spec embeds per-invocation content: the env vars,
  the repo list, the setup argv. With kit arguments, most of that becomes
  parameters over one *stable* kit directory. That is also the cleanest
  route to `sbx/DESIGN-template-caching.md`'s caching question — a fixed
  kit plus arguments beats a cache key computed over generated YAML. See
  that doc's §8 for where this is tracked as future work.
- **`sandbox.resources`** (CPU/memory limits). A MediaWiki sandbox running
  composer, npm, and phpunit is exactly the workload worth bounding.

Also relevant, though not to this file's generator: under 0.42 a kit
reference is the agent positional (`sbx run <kit-ref>`) and `--kit` is
reserved for **mixins**. Our generated spec is already `kind: mixin` with
`requires: {agent: claude}`, and `build_sbx_command` already emits
`--kit DIR claude ...`, so the deprecation does not touch us. See
`sbx/NOTES.md` §47 for the 0.42.1 upgrade's full re-measurement.
