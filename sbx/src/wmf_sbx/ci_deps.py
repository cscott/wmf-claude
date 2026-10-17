#!/usr/bin/env python3
"""WMF CI's dependency lists, for `wmf-sbx create`'s dependency walk.

CI (quibble) does not read extension.json for its repo list: Zuul's
`set_mw_dependencies` (integration/config, zuul/parameter_functions.py)
reads zuul/dependencies.yaml for the test jobs, closed over the map as
`get_dependencies` does, and zuul/phan_dependencies.yaml for the phan
jobs, direct entries only. `create` fetches both from gitiles once
(fetch_maps: two requests, about 90 KB) and adds, for each repo named on
the command line, what CI checks out when that repo is under test
(edges; deps.walk applies them to the roots only). `--no-phan-deps`
leaves out what only the phan map has; `--no-ci-deps` skips the lookup.
"""

import base64

from . import deps as deps_mod
from . import image as image_mod

PROJECT = "integration/config"
TESTS_FILE = "zuul/dependencies.yaml"
PHAN_FILE = "zuul/phan_dependencies.yaml"

EXTENSIONS = "gerrit:mediawiki/extensions/"
SKINS = "gerrit:mediawiki/skins/"
PARSOID = "gerrit:mediawiki/services/parsoid"


def canonical(name):
    """A map name as a canonical: 'Foo' is an extension, 'skins/Foo' a
    skin, and 'parsoid' Parsoid (parameter_functions.remap_parsoid)."""
    if name == "parsoid":
        return PARSOID
    if name.startswith("skins/"):
        return SKINS + name[len("skins/"):]
    return EXTENSIONS + name


def map_name(canonical_name):
    """The inverse of canonical(), or None for a repo the maps cannot
    name (core, a GitLab repo, a library)."""
    if canonical_name == PARSOID:
        return "parsoid"
    if canonical_name.startswith(SKINS):
        return "skins/" + canonical_name[len(SKINS):]
    if canonical_name.startswith(EXTENSIONS):
        return canonical_name[len(EXTENSIONS):]
    return None


def _direct(mapping, key):
    value = mapping.get(key)
    if value is None:
        return []
    return value["dependencies"] if isinstance(value, dict) else value


def ci_closure(key, mapping, recurse=True):
    """parameter_functions.get_dependencies: an entry's own `recurse:`
    decides whether its dependencies are expanded; a legacy entry (a
    plain list) inherits its parent's."""
    resolved = set()

    def resolve(name, recurse):
        resolved.add(name)
        deps = set()
        value = mapping.get(name)
        if isinstance(value, dict) and "recurse" in value:
            recurse = value["recurse"]
        for dep in _direct(mapping, name):
            deps.add(dep)
            if recurse and dep not in resolved:
                deps |= resolve(dep, recurse)
        return deps

    return resolve(key, recurse)


def edges(roots, tests, phan, include_phan=True):
    """{root: [canonical, ...]} for the roots that the maps name: CI's
    test closure, then (with include_phan) the phan map's other entries,
    each sorted. In the shape of deps.extra_edges."""
    out = {}
    for root in roots:
        key = map_name(root) if root else None
        if key is None:
            continue
        test_deps = ci_closure(key, tests) - {key}
        phan_deps = (set(_direct(phan, key)) - test_deps - {key}) if include_phan else set()
        deps = ([canonical(d) for d in sorted(test_deps)]
                + [canonical(d) for d in sorted(phan_deps)])
        if deps:
            out[root] = deps
    return out


def fetch_maps(opener=None, warn=None, sleep=None, attempts=None):
    """(tests, phan): the two maps, from integration/config's master on
    gitiles, with deps.fetch_manifest's retries and GERRITUA. None, with
    a warning, if either cannot be fetched or parsed."""
    warn = warn or (lambda msg: None)
    opener = opener or deps_mod.gerrit_opener(deps_mod.gerrit_user_agent())
    sleep = sleep or deps_mod.time.sleep
    attempts = attempts or deps_mod.GITILES_ATTEMPTS
    maps = []
    for filename in (TESTS_FILE, PHAN_FILE):
        url = deps_mod.GITILES_URL.format(project=PROJECT, filename=filename)
        status, body = deps_mod._fetch_file(url, opener, warn, sleep, attempts)
        if status != "ok":
            return None
        try:
            data = image_mod.yaml.safe_load(base64.b64decode(body).decode("utf-8")) or {}
        except (ValueError, image_mod.yaml.YAMLError) as e:
            warn(f"{url}: not the YAML it should be ({e}).")
            return None
        if not isinstance(data, dict):
            warn(f"{url}: not a map.")
            return None
        maps.append(data)
    return tuple(maps)
