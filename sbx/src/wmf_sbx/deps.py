#!/usr/bin/env python3
"""The MediaWiki dependency walk: turn `wmf-sbx-create <one extension>`
into the whole set of repos that extension needs in order to actually
run. See sbx/DESIGN-dependency-walk.md.

Three things here are easy to get wrong, and the design doc's evidence is
worth keeping next to the code:

**The link name is the directory, not the `name` field.**
`wfLoadExtension('X')` loads `$IP/extensions/X/extension.json` -- the
directory is the identifier -- while `requires.extensions` keys are
matched against the `name` field (core's ExtensionProcessor keys its
credits array by `$credits['name']`, and VersionChecker looks dependencies
up in that array). Those are two different namespaces: over cananian's 298
local manifests, **44 have `name` != directory**, including `AbuseFilter`
whose name is `Abuse Filter` -- a string that cannot be a directory at
all. Inside `mediawiki/extensions/` and `mediawiki/skins/` the basename is
authoritative; outside them (`mediawiki/services/parsoid`, which must link
as `extensions/Parsoid`) the `name` field is the only thing that gets it
right.

**Dependency key -> Gerrit project is a heuristic.** The key is a credits
name; we need a project path. It holds across the whole local corpus --
every dependency key that resolves locally resolves to a directory that
*is* that key, zero counterexamples -- but it is still a guess, so keys
that cannot be a project path are refused and a fetched manifest whose
`name` disagrees with the key that pulled it in gets a warning.

**A malformed manifest must not abort the walk.** One of those 298 files
(`AllowExternalImages/extension.json`) does not parse as JSON. One broken
extension in a closure should not take down sandbox creation, so every
parse failure is a warning and a leaf.
"""

import base64
import collections
import json
import os
import time
import urllib.error
import urllib.request

MANIFEST_FILENAMES = ("extension.json", "skin.json")

CORE = "gerrit:mediawiki/core"
DEFAULT_SKIN = "gerrit:mediawiki/skins/Vector"
EXTENSIONS_PREFIX = "gerrit:mediawiki/extensions/"
SKINS_PREFIX = "gerrit:mediawiki/skins/"

# `?format=TEXT` returns the file base64-encoded. Verified live from a
# sandbox 2026-09-07 for Translate, Vector's skin.json, parsoid, and Echo.
GITILES_URL = "https://gerrit.wikimedia.org/g/{project}/+/refs/heads/master/{filename}?format=TEXT"
GITILES_TIMEOUT = 15

# Wikimedia's edge (Varnish, not gitiles) rate-limits: a burst of manifest
# fetches earns `HTTP 429` with `retry-after: 60`. Observed live from a
# sandbox 2026-09-07 while measuring closure sizes.
GITILES_ATTEMPTS = 3
RETRY_AFTER_DEFAULT = 5
RETRY_AFTER_MAX = 60
RETRYABLE_STATUS = (429, 500, 502, 503, 504)

# fetch_manifest returns this -- rather than None -- when it could not find
# out whether a manifest exists. None means "no manifest, this repo is a
# leaf"; UNREACHABLE means "the closure computed from here is incomplete",
# which is a thing the caller must be able to tell the user about.
class _Unreachable:
    def __repr__(self):
        return "UNREACHABLE"


UNREACHABLE = _Unreachable()

# Only `extensions` and `skins` name repos; `MediaWiki` and `platform` are
# version/PHP-ability constraints with nothing to clone behind them.
DEPENDENCY_SECTIONS = ("extensions", "skins")

Manifest = collections.namedtuple("Manifest", "filename data")


def _noop(_msg):
    pass


def parse_manifest(text, filename, source, warn=None):
    """A Manifest, or None if the text isn't a usable manifest. Never
    raises: see the module docstring on AllowExternalImages."""
    warn = warn or _noop
    try:
        data = json.loads(text)
    except ValueError as e:
        warn(f"{source}: {filename} is not valid JSON ({e}) -- treating it as a leaf.")
        return None
    if not isinstance(data, dict):
        warn(f"{source}: {filename} is not a JSON object -- treating it as a leaf.")
        return None
    return Manifest(filename, data)


def read_manifest(directory, warn=None):
    """The extension.json or skin.json at the top level of a checkout, or
    None. A local checkout is always preferred over gitiles: no network,
    and it is the tree that will actually be mounted."""
    for filename in MANIFEST_FILENAMES:
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        return parse_manifest(text, filename, directory, warn=warn)
    return None


def gerrit_project(canonical):
    """The Gerrit project path for a `gerrit:`-scheme canonical, or None
    for anything else (gitlab:, raw paths). Only Gerrit is served by the
    gitiles endpoint we fetch manifests from."""
    if not canonical or not canonical.startswith("gerrit:"):
        return None
    return canonical[len("gerrit:"):]


def _gitiles_filenames(canonical):
    # Skins are the minority, so ordering by the canonical's own tree
    # saves a wasted round trip on nearly every fetch.
    if canonical.startswith(SKINS_PREFIX):
        return ("skin.json", "extension.json")
    return MANIFEST_FILENAMES


def _retry_after(error):
    """The Retry-After delay to honour, in seconds. Only the integer form
    is understood (that's what the Wikimedia edge sends); an HTTP-date or a
    missing header falls back to a short wait."""
    try:
        seconds = int((error.headers or {}).get("retry-after", ""))
    except (ValueError, TypeError, AttributeError):
        seconds = RETRY_AFTER_DEFAULT
    return max(1, min(seconds, RETRY_AFTER_MAX))


def _fetch_file(url, opener, warn, sleep, attempts):
    """(status, body) where status is 'ok', 'missing' (no such file, or a
    project we're not allowed to see) or 'unreachable'."""
    for attempt in range(1, attempts + 1):
        try:
            with opener(url, timeout=GITILES_TIMEOUT) as response:
                return "ok", response.read()
        except urllib.error.HTTPError as e:
            # 401 is the interesting one: gitiles hides project existence
            # behind auth, so unknown and forbidden look identical.
            if e.code in (401, 403, 404):
                return "missing", None
            retryable = e.code in RETRYABLE_STATUS
            delay = _retry_after(e)
            reason = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            retryable = True
            delay = RETRY_AFTER_DEFAULT
            reason = str(e)
        if not retryable or attempt == attempts:
            warn(f"{url}: {reason}.")
            return "unreachable", None
        # Say why we're about to sit still for a minute.
        warn(f"{url}: {reason} -- retrying in {delay}s ({attempt}/{attempts - 1}).")
        sleep(delay)
    return "unreachable", None


def fetch_manifest(canonical, opener=urllib.request.urlopen, warn=None,
                   sleep=time.sleep, attempts=GITILES_ATTEMPTS):
    """Fetch a manifest from Gerrit's gitiles without cloning, so the walk
    can complete before anything is cloned and `--dry-run` can be honest.

    Returns a Manifest, None (no manifest -- this repo is a leaf), or
    UNREACHABLE (we could not find out; the closure is incomplete).

    Note: **a nonexistent project returns 401, not 404** -- gitiles hides
    existence behind auth -- so an unknown project and a forbidden one are
    indistinguishable from here. Either way the repo becomes a leaf.

    gitiles serves `refs/heads/master` while the clone gets whatever the
    default branch is. For MediaWiki repos those agree; if they ever
    diverge the cloned tree is the truth, and the only consequence is a
    wrong guess about *which* repos to mount, never wrong content.
    """
    warn = warn or _noop
    project = gerrit_project(canonical)
    if project is None:
        return None
    for filename in _gitiles_filenames(canonical):
        url = GITILES_URL.format(project=project, filename=filename)
        status, body = _fetch_file(url, opener, warn, sleep, attempts)
        if status == "missing":
            continue
        if status == "unreachable":
            warn(f"{canonical}: could not fetch {filename} -- its dependencies are unknown.")
            return UNREACHABLE
        try:
            text = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as e:
            warn(f"{canonical}: {filename} did not decode ({e}) -- treating it as a leaf.")
            return None
        return parse_manifest(text, filename, canonical, warn=warn)
    return None


def dependency_keys(manifest, include_dev=True, include_suggests=True):
    """[(section, key), ...] in a stable order -- section is 'extensions'
    or 'skins'. `requires` is always read; the other two are switched by
    --no-dev / --no-suggests, both of which default to *on*."""
    fields = ["requires"]
    if include_dev:
        fields.append("dev-requires")
    if include_suggests:
        fields.append("suggests")
    found = []
    for field in fields:
        block = manifest.data.get(field)
        if not isinstance(block, dict):
            continue
        for section in DEPENDENCY_SECTIONS:
            entries = block.get(section)
            if not isinstance(entries, dict):
                continue
            for key in entries:
                if (section, key) not in found:
                    found.append((section, key))
    return found


def key_to_canonical(section, key, overrides=None, warn=None):
    """Map one dependency key to a canonical, or None if it can't be one.

    `requires: {"Abuse Filter": "*"}` is legal MediaWiki and would produce
    a nonsense Gerrit project, so anything that can't be a single project
    path segment is refused rather than guessed at. `dependency_overrides`
    in repos.yaml is the escape hatch for exactly those.
    """
    warn = warn or _noop
    if overrides and key in overrides:
        return overrides[key]
    if not isinstance(key, str) or not key.strip():
        warn(f"ignoring an empty dependency key in {section!r}.")
        return None
    if key != key.strip() or "/" in key or " " in key:
        warn(
            f"dependency {key!r} cannot be a Gerrit project path (the key is a "
            f"credits name, not a directory) -- skipping it. Add a "
            f"`dependency_overrides:` entry in repos.yaml to map it by hand."
        )
        return None
    return f"gerrit:mediawiki/{section}/{key}"


def link_name(canonical, manifest=None, overrides=None, warn=None):
    """The directory name to link this repo in as -- see the module
    docstring on why this is not the `name` field."""
    warn = warn or _noop
    if overrides and canonical in overrides:
        return overrides[canonical]
    basename = canonical.rsplit("/", 1)[-1]
    if canonical.startswith(EXTENSIONS_PREFIX) or canonical.startswith(SKINS_PREFIX):
        return basename
    name = (manifest.data.get("name") if manifest else None)
    if not isinstance(name, str) or not name.strip():
        return basename
    name = name.strip()
    if "/" in name or " " in name:
        warn(
            f"{canonical}: the manifest's name {name!r} can't be a directory "
            f"name -- linking as {basename!r} instead."
        )
        return basename
    if name != basename:
        # Outside mediawiki/{extensions,skins} the name field is the only
        # thing that gets e.g. services/parsoid -> extensions/Parsoid
        # right, but a surprising link name should be visible.
        warn(f"{canonical}: linking as {name!r} (from the manifest's name field), not {basename!r}.")
    return name


def link_section(canonical, manifest=None):
    """'extensions', 'skins', or None for a repo that isn't linked into
    core at all (core itself, a container directory, a service with no
    manifest)."""
    if canonical == CORE:
        return None
    if canonical and canonical.startswith(SKINS_PREFIX):
        return "skins"
    if canonical and canonical.startswith(EXTENSIONS_PREFIX):
        return "extensions"
    if manifest is None:
        return None
    return "skins" if manifest.filename == "skin.json" else "extensions"


def walk(roots, manifest_for, include_dev=True, include_suggests=True,
         overrides=None, warn=None):
    """Expand `roots` (canonicals; None entries -- raw paths that reverse
    resolution couldn't identify -- are kept as roots but not walked) into
    the full closure.

    Returns (discovered, origins): `discovered` is the canonicals *not*
    already in roots, sorted; `origins` maps each discovered canonical to
    the one whose manifest pulled it in, so an error about a repo the user
    never typed can explain where it came from (see origin_chain).

    Two implicit edges, both from the design: anything with a manifest
    needs `core`, and `core` needs a skin (`Vector`). That makes a
    guaranteed cycle -- Vector has a skin.json, so Vector needs core, and
    core needs Vector -- which is why the visited set isn't optional.
    """
    warn = warn or _noop
    root_set = [r for r in roots if r]
    seen = set()
    origins = {}
    frontier = collections.deque(root_set)
    while frontier:
        canonical = frontier.popleft()
        if canonical in seen:
            continue
        seen.add(canonical)

        def add(dep, why=canonical):
            if dep and dep not in seen:
                origins.setdefault(dep, why)
                frontier.append(dep)

        if canonical == CORE:
            add(DEFAULT_SKIN)
            continue
        manifest = manifest_for(canonical)
        if manifest is None:
            continue
        add(CORE)
        for section, key in dependency_keys(manifest, include_dev, include_suggests):
            dep = key_to_canonical(section, key, overrides=overrides, warn=warn)
            if dep is None:
                continue
            if dep == canonical:
                continue
            add(dep)
            dep_manifest = manifest_for(dep)
            if dep_manifest is None:
                warn(
                    f"{canonical} requires {key!r}, which resolved to {dep} -- "
                    f"no manifest found there. It may not be a real project "
                    f"(gitiles answers 401 for both unknown and forbidden)."
                )
                continue
            dep_name = dep_manifest.data.get("name")
            if isinstance(dep_name, str) and dep_name.strip() != key:
                # The key is a credits name; a mismatch means we guessed a
                # project that exists but isn't the extension asked for.
                warn(
                    f"{canonical} requires {key!r} but {dep}'s manifest calls "
                    f"itself {dep_name.strip()!r} -- the guessed project may be "
                    f"the wrong one."
                )
    discovered = sorted(seen - set(root_set))
    return discovered, origins


def origin_chain(canonical, origins, roots):
    """['gerrit:...Translate', 'gerrit:...ULS'] -- how a discovered repo
    got pulled in, for an error message about a path the user never
    typed. Loop-safe: the closure genuinely contains cycles."""
    chain = [canonical]
    seen = {canonical}
    root_set = set(roots)
    while chain[0] not in root_set:
        parent = origins.get(chain[0])
        if parent is None or parent in seen:
            break
        seen.add(parent)
        chain.insert(0, parent)
    return chain


def make_manifest_for(resolve, fetch=fetch_manifest, warn=None, cache=None,
                      unreachable=None):
    """A `manifest_for(canonical)` for walk().

    `resolve(canonical)` returns a local directory that already exists, or
    None. Local checkouts win: no network, and it is the tree that gets
    mounted. Results (including misses) are cached for the process
    lifetime -- diamond dependencies are the norm, not the exception
    (UniversalLanguageSelector appears three ways in CommunityRequests'
    closure).

    Repos whose manifest could not be fetched are collected into
    `unreachable` (a set, if you pass one). walk() can't tell those apart
    from genuine leaves, but the caller must: a rate-limited walk produces
    a plausible-looking closure that is quietly missing repos.
    """
    cache = {} if cache is None else cache

    def manifest_for(canonical):
        if canonical in cache:
            return cache[canonical]
        manifest = None
        directory = resolve(canonical)
        if directory:
            manifest = read_manifest(directory, warn=warn)
        if manifest is None and not directory:
            manifest = fetch(canonical, warn=warn)
            if manifest is UNREACHABLE:
                if unreachable is not None:
                    unreachable.add(canonical)
                manifest = None
        cache[canonical] = manifest
        return manifest

    return manifest_for
