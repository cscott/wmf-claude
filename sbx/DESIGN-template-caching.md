# Sandbox template caching for `wmf-sbx-create`

**Status: designed, not implemented.** Self-contained plan, ready for
pickup — see "Open questions" (§6) before starting; two items there are
still unverified against a real `sbx` binary. This design was originally
written up as sequenced *after* Layer C (MediaWiki setup/DB-initialization
running inside the kit) landed, on the theory that Layer C would widen
what counts as install-affecting content in a way that would force a
redesign. **That sequencing turns out to be based on a mistaken premise —
see the correction in §3, "Why the Layer C caveat doesn't hold."** Layer C
landed and was measured (`sbx/NOTES.md` §30, §32) without this design
needing to change. This work can be picked up on its own schedule now,
not gated on anything else.

## 1. Problem

Every `wmf-sbx-create` invocation that doesn't pass an explicit `--kit`
generates a MediaWiki kit on the fly (`wmf_sbx_kit.build_kit_spec`) and
hands it to `sbx create --kit <dir>`, which runs `apt-get update &&
apt-get install -y <~17 packages>` inside the new sandbox before it's
usable. That's slow and identical work repeated on every single sandbox
creation, even back-to-back ones for unrelated repos. `NOTES.md` §30
measured this at 64.8s of a 64.9s total for a single-repo sandbox; §32
measured it at 65.3s for a five-repo sandbox — a large, and largely
avoidable, fixed cost on every invocation.

## 2. What `sbx template`/`-t` actually do

Confirmed via the docs.docker.com mirror
(`reference/cli/sbx/{create,run,template,template/*}/index.html`):

- `sbx create`/`sbx run` both take `-t, --template IMAGE`: "Container
  image to use for the sandbox (default: agent-specific image)." This is
  a pure base-image swap at `create` time — it does not replace or
  subsume any mount/workspace flag. `--clone`, the workspace positionals,
  `--name`, `-e`/`--env`, etc. are all still required and behave exactly
  as they do against the default image.
- `sbx template save SANDBOX TAG [-o FILE]` snapshots an existing
  sandbox's container into the local image store under an arbitrary tag
  (e.g. `myimage:v1.0`); `-o` also exports a shareable tar (not needed
  here — this is a single-host cache).
- `sbx template ls [--json]` lists images in the store; `sbx template rm
  TAG|ID` removes one. The store is host-local, not scoped to a
  particular sandbox/agent/workdir, so one template can legitimately back
  many different sandboxes.
- `--kit` is marked **experimental** in both `create` and `run`; `sbx
  template` is not. The design below leans on the more stable primitive
  (`-t`) for the hot path and treats `--kit` as the (already-existing,
  already-tolerated) cold-path fallback.
- **Caveat, confirmed empirically (§6 item 3): "pure base-image swap"
  describes what `-t` does at `create` time, not a guarantee about what's
  inside the image.** `sbx template save` bakes the entire `--clone`
  workspace's on-disk content into the saved image, not just
  `commands.install`'s side effects. A template built carelessly (e.g.
  straight from a real working sandbox) can carry stale repo content at
  old workspace paths into every future sandbox that reuses it. §4's
  cache-miss flow builds templates from a disposable placeholder sandbox
  specifically to keep this true in practice — this is the mechanism the
  §3 correction below depends on.

## 3. Key design insight: cache on kit *content*, not on the resolved repo list

The obvious framing is "canonicalize `wmf-sbx-create`'s arguments (primary
+ extra repos, `--kit`, `--name` is irrelevant) and hash that." But
`wmf_sbx_kit.build_kit_spec` shows the generated kit's
`commands.install` (the slow part: the package list) and
`network.allowedDomains` **do not depend on which repos were passed** —
they come entirely from `BASE_PACKAGES`/`EXTRA_DOMAINS`/
`wiki_family_domains()` plus the user's own
`~/.config/wmf-sbx/repos.yaml` `extra_packages`/`extra_environment`. The
only per-invocation part of the generated kit is `environment.variables`
(`MW_CORE_REPO`, `PARSOID`, `MW_VENDOR_REPO`, `MW_INSTALL_PATH` — each set
to that invocation's *local host path* for the matching repo), which is
cheap to (re)apply and never affects what's installed in the image.

So: **hash only the install-affecting subset of the kit spec**
(`packages`, `allowedDomains`), not the resolved repo list and not
`environment.variables`. Consequence: with today's kit generator, *every*
`wmf-sbx-create` invocation for a given user's config produces the same
cache key and shares one template, regardless of which extension/skin/
repo combination is being mounted — a much bigger win than
per-repo-combination caching, and the literal argument list never even
needs to be canonicalized for this purpose.

If `extra_packages`/`extra_environment` (or the base list) changes later,
that's a new hash automatically — no separate invalidation logic needed
for config changes.

**Caveat that still holds: per-repository extra packages.** This is a
future feature, not yet built: today `extra_packages` is only a global,
user-level `repos.yaml` setting. If per-repo package requirements are
added later (e.g. an extension that needs a native library
`apt-get`-installed), the resolved repo list would become
install-affecting again, and `cache_key`/`template_tag` (§5) would need
to fold in the per-repo package contributions of whichever repos are
actually mounted, not just the global list. Not built on either the
kit-generation or the caching side today — see §7.

### Why the Layer C caveat doesn't hold

The original version of this design carried a second caveat, and treated
it as the reason to defer this whole addendum until after Layer C
(MediaWiki setup/DB-initialization moving into the kit) landed: once
`commands.install`/`commands.setup` runs extension- and skin-specific
code to initialize the database, the *set of installed extensions and
skins* would determine the resulting database schema baked into the
container filesystem — making that set install-affecting content in the
same sense `packages`/`allowedDomains` are, and requiring it to be added
to `cache_key`.

**That reasoning doesn't survive a close look at this design's own §4.**
The cache-miss flow never builds the shared template from a real,
provisioned sandbox — it always builds from a disposable *builder*
sandbox, created against an empty placeholder repo, specifically because
`sbx template save` bakes the entire `--clone` workspace's content into
the image (§2's caveat, confirmed empirically). That builder-sandbox
indirection means the shared template's filesystem layer **never**
contains any real extension/skin content or a provisioned wiki, no
matter which repos a given `wmf-sbx-create` invocation names, Layer C or
no Layer C. The actual MediaWiki setup/DB-initialization work that Layer
C added happens per-invocation, against each sandbox's own `--reference`
clones in the parallel tree (`sbx/DESIGN-parallel-clone-tree.md`), which
are never part of what gets cached — only the apt-get/package layer is.

In other words, the caching boundary this design draws (packages/domains,
baked into a shared template) and the boundary Layer C's work lives on
the other side of (per-invocation clone + composer + npm + MediaWiki
install, never baked into anything shared) were already separated by the
builder-sandbox mechanism *before* Layer C existed, for an unrelated
reason (keeping stale repo content out of the shared image). Layer C
landing doesn't erode that separation — it just adds more work on the
per-invocation side, which this design was never trying to cache in the
first place. `NOTES.md` §32's measurement (77.8s of per-invocation
`wmf-sbx-setup` install-step work, on top of 65.3s of apt-get, for a
five-repo sandbox) confirms empirically that the per-invocation side is
real and non-trivial — but it was always going to be per-invocation work
under this design, cached or not.

**Practical upshot:** this design can be implemented as originally
scoped, against today's `packages`/`allowedDomains` hash, without
widening `cache_key` to include the resolved extension/skin set. If that
turns out to be wrong once this is actually implemented and measured
against a real Layer-C sandbox, that's new information to bring back
here — but nothing in the design as written requires waiting for it.

## 4. Flow

Unless `--no-cache` (new flag) or an explicit `--kit`/`kit:` is in play:

1. Build the kit spec dict as today (`build_kit_spec`), but don't write it
   to a tempdir yet.
2. Compute `tag = template_tag(packages, domains)` from the spec (§5).
3. `sbx template ls --json` (via `WMF_SBX`, never bare `sbx`) and check
   whether `tag` is present.
4. **Cache hit:** skip `write_kit_dir` entirely. Build the `sbx create`
   command with `-t <tag>` instead of `--kit`, plus `-e KEY=VALUE` for
   every entry in the kit's `environment.variables` (replaying what the
   kit would have set, without paying for a kit application/install
   pass). No `template save` afterward — the template already exists.
   **Also required, not optional:** for every entry in the kit's
   `network.allowedDomains`, run `wmf-sbx policy allow network --sandbox
   <name> <domain>` after `create`. A template carries no network policy
   at all (§6 item 3) — skipping this would silently give cache-hit
   sandboxes a narrower network policy than a cache-miss sandbox gets,
   for the exact same kit/config.
5. **Cache miss:** build the template from a disposable *builder*
   sandbox, never from the sandbox the user is actually about to work in,
   then fall through to the cache-hit path above for the sandbox the user
   gets. See "Why a builder sandbox, not the user's real sandbox" below
   for why this isn't optional.
   1. `write_kit_dir` into a tempdir as today.
   2. Create a throwaway builder sandbox against a placeholder workspace,
      not any real repo: `wmf-sbx create --name <builder-name> --kit
      <tempdir> --clone claude <scratch-empty-git-repo>`. The placeholder
      must be a real git repo (`--clone` requires one to clone) but must
      contain no content that matters if it leaks — an empty repo with a
      single dummy commit, created fresh in a tempdir per build.
   3. `wmf-sbx stop <builder-name>` — required. `template save` refuses
      to run against a running sandbox; its only response is an
      *interactive* "Sandbox ... is running and must be stopped before
      saving. Stop it now? (y/N):" prompt with no flag to skip it (§6
      item 1). Stopping explicitly beforehand avoids that prompt outright
      rather than trying to script past it.
   4. `wmf-sbx template save <builder-name> <tag>`.
   5. `wmf-sbx rm --force <builder-name>`; delete the scratch placeholder
      repo dir. The flag is load-bearing, not decorative: a bare `sbx rm`
      prompts `Remove sandbox 'NAME'? This cannot be undone. (y/N):` and
      would stall this automated step. `--force` skips it; only that long
      spelling is verified, so don't write `-f`. Declining the prompt
      exits **0**, so this step must not treat a zero exit as proof the
      builder was removed (see `sbx/DESIGN-host-remotes.md` §5, §8).
   6. Treat any failure in steps 2–5 as a warning, not fatal: fall
      through to today's uncached `--kit <tempdir>` behavior for *this*
      invocation if the template never got saved. Caching is an
      optimization; sandbox creation must still succeed.
6. Continue to the existing `--no-run`/exec-`wmf-sbx run` tail unchanged.

**Why a builder sandbox, not the user's real sandbox:** `sbx template
save` bakes the *entire* `--clone` workspace's current on-disk content
into the saved image — not just `commands.install`'s side effects
(confirmed empirically, §6 item 3). A sandbox created later from that
template, with a *different* `--clone` target at a *different* path,
still has the **old** repo's files sitting at the **old** path inside its
filesystem, invisible to the new workspace but present on disk. Read-only
bind-mounted "extra" directories, by contrast, do **not** get baked in —
a later sandbox without that mount sees only an empty placeholder
directory at the old path, no content (confirmed in the same test).

If the cache-miss flow saved the template directly from a real working
sandbox, every subsequent cache-hit sandbox — for any unrelated repo —
would silently carry a full copy of whichever repo happened to trigger
that particular cache miss. Building the template from a disposable
placeholder repo instead keeps the shared cache image's workspace paths
permanently empty/inert, so real repo content only ever arrives via each
sandbox's own clone/bind-mount at `create` time. This is also the
mechanism the §3 correction above depends on: it's what keeps
extension/skin-specific content out of the shared template regardless of
whether Layer C's MediaWiki setup runs per-invocation.

`--dry-run` prints the computed tag and hit/miss (via the same read-only
`sbx template ls --json` check) and the resulting command, without
calling `template save` or `sbx create`.

`--no-cache`: skip steps 2–5 entirely; always generate+use `--kit`, never
read or write the template store. Useful for debugging a suspect cached
image, or for deliberately picking up a rebuilt package set without
waiting for a `CACHE_SCHEMA_VERSION` bump.

**v1 scope limitation:** apply this caching only to the auto-generated
kit path (`kit is None` in today's `main()`). An explicit `--kit PATH`
(or `kit:` in `repos.yaml`) bypasses caching entirely for v1 — behaves
exactly as today, always cold. Splitting an arbitrary user-supplied kit
(which `wmf_sbx_kit` doesn't control the shape of, and which may be a ZIP
or OCI reference, not a directory) into install-affecting vs.
per-invocation parts is a fair amount of extra generality for a case that
doesn't come up in practice today.

## 5. Cache key and template naming

```python
CACHE_SCHEMA_VERSION = 1  # bump whenever a wmf-sbx-create/wmf_sbx_kit change
                          # alters what ends up installed in the image for a
                          # given packages/domains input, so stale templates
                          # from before the change stop matching
```

```python
def cache_key(packages, domains):
    payload = json.dumps(
        {"schema_version": CACHE_SCHEMA_VERSION,
         "packages": sorted(packages), "domains": sorted(domains)},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]

def template_tag(packages, domains):
    return f"wmf-sbx-{CACHE_SCHEMA_VERSION}-{cache_key(packages, domains)}"
```

The `wmf-sbx-` prefix makes auto-created templates recognizable in `sbx
template ls` output, distinguishable from anything hand-created with `sbx
template save` directly, and greppable for cleanup. The embedded schema
version means a bumped `CACHE_SCHEMA_VERSION` naturally produces
unreachable-but-still-named `wmf-sbx-1-*` templates after a bump to `2` —
recognizable as stale by prefix alone, not just by the hash no longer
matching anything current.

New/changed surface in `wmf_sbx_create.py` (sketch, not final):

- `existing_templates(run=subprocess.run)` — `wmf-sbx template ls --json`,
  same best-effort-empty-set-on-failure shape as `existing_sandbox_names`:
  no `wmf-sbx`/`sbx` on `PATH`, or a parse failure, means "cache miss,"
  never a crash.
- `cache_key(packages, domains)` / `template_tag(packages, domains)` —
  pure functions, trivially unit-testable without any subprocess.
- `save_template(name, tag, run=subprocess.run)` — best-effort, logs a
  warning on failure rather than raising.
- `build_sbx_command` grows two optional params: `template` (→ `-t TAG`,
  mutually exclusive with `kit`) and `env_vars` (list of `KEY=VALUE` → one
  `-e` per entry).
- `allow_domains(name, domains, run=subprocess.run)` — one `wmf-sbx policy
  allow network --sandbox <name> <domain>` call per domain (or a single
  comma-joined call) after a cache-hit `create`. Best-effort like
  `save_template`: a failure here means "cache-hit sandbox has a narrower
  network policy than intended," worth a loud warning, not worth failing
  sandbox creation over.
- `main()` gains `--no-cache` (`store_true`). The existing `--kit`/`kit:`
  branch gets an `if kit is None and not args.no_cache:` split between
  the cache-hit and cache-miss sub-paths above.

## 6. Open questions — verify against a real `sbx` binary before implementing

1. **RESOLVED empirically, opposite of the original assumption.** `sbx
   template save` requires the sandbox to be **stopped**, not running:
   attempting it against a running sandbox produces an interactive
   `Sandbox <name> is running and must be stopped before saving. Stop it
   now? (y/N):` prompt, with **no flag** to skip it. A separate `sbx stop
   SANDBOX [SANDBOX...]` command exists, takes no confirmation, and is
   the fix: stop explicitly before `template save` and the prompt never
   fires (§4 step 5.3).
2. **Is kit provisioning (`commands.install`) fully synchronous within
   `sbx create`, finishing before it returns?** Needed for `template
   save` right after `create` to capture the fully-provisioned state
   rather than a partial one. Not yet verified.
3. **RESOLVED empirically — confirms the working assumption: a template
   is a base filesystem image only.** Test: sandbox A created with
   `--kit` setting one env var (`CLAUDE_TEST_ENV_VAR`) and one allowed
   domain (`info.cern.ch`); checked from inside A: env var set, `curl
   https://info.cern.ch` → `200`. Saved as a template, A removed. Sandbox
   B created from that template with `-t` and **no** `--kit`. Inside B:
   the `commands.install`-written marker *file* survived (filesystem
   content) — but the env var came back empty and `curl
   https://info.cern.ch` → `403` (blocked by default-deny). Neither
   environment variables nor network policy are part of the image; both
   are pure `create`-time configuration and are lost on a `-t`-only cache
   hit unless explicitly reapplied. `-e` already covers the env-var case
   (§4 step 4). For domains: `sbx create` has **no** `--allow-domain`-
   style flag (only `--deny-network`, which narrows, never widens), but
   `sbx policy allow network --sandbox SANDBOX RESOURCES` exists precisely
   for this — applied after `create`, without touching `--kit` or
   re-running `commands.install` at all. §4 step 4 calls this for every
   domain in the kit spec on every cache hit; this was a real gap in the
   original design (cache-hit sandboxes would have silently gotten a
   narrower network policy than cache-miss ones), not just an open
   question. Not yet empirically verified that `policy allow network
   --sandbox` reaches a sandbox created via this exact flow
   (docs-confirmed only) — worth a quick real-`sbx` spot check before
   relying on it, same as item 5 below.
4. **Exact `sbx template ls --json` schema** (field names for tag, image
   ID, size, created-at) is not documented on the reference pages —
   capture a real sample (`wmf-sbx template ls --json`, on the host,
   after at least one `sbx template save`) before writing the parser.
5. **Does `sbx create -t <tag> --clone claude <dirs...>` behave
   identically to the default-image case** in every other respect (agent
   CLI version inside the snapshot vs. freshly pulled, any first-run
   setup the default image does that a snapshot might skip)? Spot-check a
   real cache-hit sandbox before trusting it for daily use.
6. **Concurrent cache-miss races**: two `wmf-sbx-create` invocations
   computing the same miss at once could both attempt `template save
   <tag>`. Treat a "tag already exists" failure on the second as success,
   not a warning — low-priority given this is a personal single-user
   devbox tool, but worth a one-line guard so it doesn't print a scary
   message on an otherwise harmless race.
7. **RESOLVED empirically — `sbx template save` bakes `--clone` workspace
   content into the image; bind mounts are not baked in.** Test: sandbox
   A created with `--clone claude <primary-a>` (a throwaway repo with a
   marker commit) plus `<extra>:ro` (a plain directory with a marker
   file); saved as a template; removed. Sandbox B then created from that
   template with `-t`, `--clone claude <primary-b>` (a *different*
   throwaway repo, different path) and **no** extra mount at all. Inside
   B: `primary-b` was freshly cloned as expected — the primary workspace
   is not stale. But the **old** `primary-a` path was still present and
   still contained `primary-a`'s committed file — content from a sandbox
   that no longer exists, baked into the shared template image and now
   sitting in every sandbox built from it. The old `extra` path existed
   only as an empty directory (no marker file) — bind-mounted content is
   not part of the image layer, only true mounts reproduce it, and only
   when the new `sbx create` invocation re-mounts them. Consequence: a
   cache-miss flow must never `template save` a sandbox that has a real
   repo cloned into its `--clone` workspace — see "Why a builder sandbox"
   in §4, the direct mitigation for this finding, and the mechanism §3's
   correction depends on.

## 7. Testing strategy

Same dependency-injection conventions as the rest of `wmf_sbx_create.py`
(`existing_sandbox_names`, `do_clone`): `existing_templates`/
`save_template` take `run=subprocess.run` and get fake-`run`-based unit
tests (parses `--json` output once the real schema is known from §6 item
4; empty-set/no-op on command failure or missing binary). `cache_key`/
`template_tag` are pure and get direct unit tests, including one
asserting the key is **identical** across two different resolved-repo
lists that produce the same packages/domains (the core design claim in
§3) and **different** when `CACHE_SCHEMA_VERSION` or the package list
changes. `main()`-level tests mock `existing_templates`/`save_template`
to confirm the hit/miss branch wiring without a real `sbx` binary.

## 8. Deferred / future work, not v1

- Automatic pruning of stale `wmf-sbx-<old-version>-*` templates (a
  `wmf-sbx-template-gc` helper, or a `--prune-cache` flag) once
  `CACHE_SCHEMA_VERSION` has been bumped a few times and old ones are
  visibly accumulating in `sbx template ls`. Not needed for v1; the
  naming convention alone makes manual `wmf-sbx template rm` easy once
  the real schema is known.
- Generalizing caching to explicit `--kit` directories (§4's v1 scope
  limitation), by parsing any kit's `spec.yaml` the same way regardless
  of source, if that turns out to matter in practice.
- Sharing a saved template across machines via `template save --output` /
  `template load` — not useful for a personal single-host dev tool.
- Per-repository extra packages (§3's still-live caveat) — not built at
  all yet, on either the kit-generation or the caching side. If/when it
  is, `cache_key` needs to hash the per-repo package contributions of the
  resolved repo list, not just the global `extra_packages`.
- **Kit `args:` blocks (sbx 0.42's `--kit-arg name=value`), noted here
  ahead of implementing this design rather than after.** The reason
  `wmf_sbx_kit` writes a fresh `spec.yaml` into a temp directory on every
  invocation is that the spec embeds per-invocation content: the env
  vars, the repo list, the setup argv. With kit arguments, most of that
  becomes parameters over one *stable* kit directory — which is a
  cleaner route to this whole caching problem than hashing generated
  YAML: a fixed kit plus arguments beats a cache key computed over
  content that's regenerated (and re-hashed) on every call. Worth
  evaluating before implementing §4–§5 as written, not after; see
  `sbx/DESIGN-kit-generation.md`'s 0.42.1 addendum for where this was
  first noted.
