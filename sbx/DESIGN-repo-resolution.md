# Repo name resolution and per-user directory config

Turns an input like `Cite`, `mediawiki/extensions/Cite`, or
`gerrit:mediawiki/extensions/Cite` into (a) a canonical Gerrit project path
and (b) a local directory to use, or clone into, as an `sbx` workspace.
Two independent steps: name → canonical path, then canonical path → local
directory.

## Step 1: name → canonical path

- A fully-qualified input (`gerrit:mediawiki/extensions/Cite`, or a bare
  `mediawiki/extensions/Cite` — the `gerrit:` prefix can be inferred when
  there's no ambiguity with a future `gitlab:` source) is used as-is, no
  lookup needed.
- A bare short name (`Cite`) is resolved by querying Gerrit's REST API
  (confirmed reachable unauthenticated and part of the kit's
  `allowedDomains` — see `sbx/NOTES.md` §10) and keeping only projects
  whose **final** `/`-separated path component equals the query, case
  sensitively. Confirmed against the live API: `Cite` must resolve to only
  `mediawiki/extensions/Cite`, not `CiteDrawer`/`CiteThisPage`/`SemanticCite`
  (which a naive substring match would also return).
- Zero matches, or more than one match: error out and list the candidate
  full paths, asking the user to type one of them instead of the bare name.
- `gitlab:` sources: only when Gerrit has no match, the bare name is
  searched in GitLab's `repos` group, then `toolforge-repos`
  (`gitlab_search`, anonymous; personal-namespace forks are never
  searched). The same final-segment, non-archived-first and ambiguity
  rules apply. A name that is in both forges must be written `gitlab:...`.
  See NOTES.md §100.

## Step 2: canonical path → local directory

Per-user config, so this isn't hardcoded to any one person's disk layout
(needed for upstreaming — see cananian's goal (d)).

### Config file

`~/.config/wmf-sbx/repos.yaml`:

```yaml
rules:
  # Exact, one-off mappings (no wildcard) always win over any wildcard rule
  # for the same path.
  - match: "gerrit:mediawiki/core"
    path: "~/Wikimedia/core"
  - match: "gerrit:mediawiki/services/parsoid"
    path: "~/Wikimedia/Parsoid"

  # {name} captures the final path segment.
  - match: "gerrit:mediawiki/extensions/{name}"
    path: "~/Wikimedia/Extensions/{name}"
  - match: "gerrit:mediawiki/skins/{name}"
    path: "~/Wikimedia/Skins/{name}"

  # A more specific rule for a prefix that would otherwise collide with the
  # catch-all below (cananian's own example: a bare "refinery" directory
  # already existed, so analytics/* projects get a disambiguating prefix).
  - match: "gerrit:analytics/{name}"
    path: "~/Wikimedia/analytics-{name}"

  # ** matches zero or more leading segments. This is the fallback: any
  # project not covered above lands flat in ~/Wikimedia/<final-name>.
  - match: "gerrit:**/{name}"
    path: "~/Wikimedia/{name}"

  # gitlab: is a separate namespace so a future GitLab-hosted project never
  # collides with a Gerrit one under the same rule set.
  - match: "gitlab:**/{name}"
    path: "~/Wikimedia/{name}"

# Dependencies not captured by extension.json's `requires` field — the
# few CI-only oddballs tracked by integration/config's utils/mw-requires.py
# (see sbx/NOTES.md §11), or an in-progress dependency a user is adding to
# an extension.json that hasn't landed yet. Keyed by canonical path.
extra_dependencies:
  # gerrit:mediawiki/extensions/Example: ["gerrit:mediawiki/extensions/OtherThing"]
```

### Matching semantics

- Match patterns and canonical paths are both `/`-separated segment lists.
- A segment is one of: a literal (must match exactly), `**` (matches zero
  or more whole segments — only meaningful as a prefix, immediately before
  the final segment), or `{name}` (only valid as the *final* segment;
  captures exactly one segment for substitution into `path`).
- **Specificity** = count of literal segments in the pattern. An exact
  rule with no `{name}`/`**` has specificity equal to its own length and
  can only match that one literal path — it's the most specific possible
  rule by construction.
- Rules are pre-filtered to those that match the canonical path at all,
  then sorted by specificity descending (ties keep config file order —
  first-listed wins).

### Resolution algorithm

Given a canonical path and the sorted, matching rule list:

1. **Existing checkout wins, at the longest matching prefix first**: walk
   the sorted rules, expand each rule's `path` template, and return the
   first one that already exists as a directory on disk.
2. **No existing checkout found**: clone into the path produced by the
   single most specific matching rule (first in the sorted list) — never
   the loosest one, even if a looser candidate happens to not exist either
   (there's nothing to prefer it over).
3. No matching rule at all is a hard error: the user's config doesn't cover
   this project, and guessing would be worse than asking them to add a
   rule.

This directly implements cananian's stated preference: "check each
possibility, starting with the longest matching prefix, and accepting the
first directory that exists"; "if creating/cloning a new repo, then use the
longest match."

## Open follow-ups (not blocking the first implementation)

- `extra_dependencies` consumption — read but not yet wired into any
  dependency walk (Layer C work). Still genuinely open.
- ~~`skin.json`'s dependency shape is assumed to mirror
  `extension.json`'s.~~ **Settled 2026-09-07**: `skin.json` uses the same
  `requires` object, and `wmf_sbx_deps.MANIFEST_FILENAMES` is
  `("extension.json", "skin.json")` with no per-file special-casing.
  Spot-checked against a real Vector checkout.
- No live validation yet that Gerrit's project list is genuinely complete
  when paginated (`?S=`/`?n=`) rather than capped at the ~500-entry default
  page — matters once the resolver needs the *full* project set rather
  than a `?p=`-scoped prefix query.
