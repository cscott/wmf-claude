#!/usr/bin/env python3
"""Write settings.yaml `extra_dependencies:` from WMF CI's dependency maps.

CI (quibble) does not read extension.json for its repo list: Zuul's
`set_mw_dependencies` (integration/config, zuul/parameter_functions.py)
reads zuul/dependencies.yaml for the test jobs, closed over the map as
`get_dependencies` does, and zuul/phan_dependencies.yaml for the phan
jobs, direct entries only. This writes, for each repo in either map,
the union of the two, as the dependencies of that repo when it is the
one under test (deps.walk applies them to the roots only). A repo that
only the phan map names is marked `# phan`.

`--trim` leaves out what `wmf-sbx create`'s manifest walk finds anyway:
from the repo itself, or from another of its extras that is kept. It
fetches every manifest in those closures from gitiles (several hundred).

Usage (from sbx/src):
  python3 -m wmf_sbx.ci_deps > ../reference/ci-extra-dependencies.yaml
  python3 -m wmf_sbx.ci_deps --dir DIR   # a local integration/config/zuul
  python3 -m wmf_sbx.ci_deps --trim
"""

import argparse
import base64
import json
import os
import sys
import urllib.request

from . import deps as deps_mod
from . import image as image_mod

PROJECT = "integration/config"
FILES = ("dependencies.yaml", "phan_dependencies.yaml")
GITILES = "https://gerrit.wikimedia.org/r/plugins/gitiles/" + PROJECT + "/+/refs/heads/master/"


def canonical(name):
    """A map name as a canonical: 'Foo' is an extension, 'skins/Foo' a
    skin, and 'parsoid' Parsoid (parameter_functions.remap_parsoid)."""
    if name == "parsoid":
        return "gerrit:mediawiki/services/parsoid"
    if name.startswith("skins/"):
        return "gerrit:mediawiki/" + name
    return "gerrit:mediawiki/extensions/" + name


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


def extra_dependencies(tests, phan):
    """{canonical: [(canonical, phan_only), ...]}: CI's test closure,
    sorted, then the phan map's other entries, sorted."""
    out = {}
    for key in sorted(set(tests) | set(phan), key=canonical):
        test_deps = ci_closure(key, tests) - {key}
        phan_deps = set(_direct(phan, key)) - test_deps - {key}
        deps = ([(canonical(d), False) for d in sorted(test_deps, key=canonical)]
                + [(canonical(d), True) for d in sorted(phan_deps, key=canonical)])
        if deps:
            out[canonical(key)] = deps
    return out


def trim(edges, manifest_for):
    """`edges` without what deps.walk finds from manifests (create's
    defaults: dev-requires and suggests walked). An extra that another
    kept extra's walk reaches goes; of two that reach each other, the
    first in the list stays. An unknown manifest covers nothing, so a
    failed fetch only leaves more in."""
    def closure(canonical):
        return set(deps_mod.walk([canonical], manifest_for)[0])

    out = {}
    for key, deps in edges.items():
        base = closure(key)
        left = [(dep, phan) for dep, phan in deps if dep not in base]
        reach = {dep: closure(dep) for dep, _ in left}
        order = {dep: i for i, (dep, _) in enumerate(left)}

        def covered(dep):
            return any(dep in reach[other] and (other not in reach[dep] or order[other] < order[dep])
                       for other in reach if other != dep)

        kept = [(dep, phan) for dep, phan in left if not covered(dep)]
        if kept:
            out[key] = kept
    return out


def render(edges, source, trimmed=False):
    lines = [
        "# settings.yaml `extra_dependencies:` from WMF CI's dependency maps:",
        f"# {source}. Made by `python3 -m wmf_sbx.ci_deps` in sbx/src.",
        "# Copy the block into ~/.config/wmf-sbx/settings.yaml (it replaces",
        "# `extra_dependencies: {}`). Each entry is what CI checks out when that",
        "# repo is under test; `wmf-sbx create` adds it for the repos you name,",
        "# not for their dependencies. `# phan`: only CI's phan job has it.",
    ]
    if trimmed:
        lines.append("# Trimmed (--trim): without what create's extension.json/skin.json walk finds,"
                     "\n# with dev-requires and suggests (so not with --no-dev or --no-suggests).")
    lines.append("extra_dependencies:")
    for key, deps in edges.items():
        lines.append(f"  {key}:")
        lines += [f"    - {dep}" + ("  # phan" if phan else "") for dep, phan in deps]
    return "\n".join(lines) + "\n"


def _fetch(name):
    with urllib.request.urlopen(GITILES + "zuul/" + name + "?format=TEXT", timeout=30) as r:
        return base64.b64decode(r.read()).decode("utf-8")


def _head():
    url = "https://gerrit.wikimedia.org/r/plugins/gitiles/" + PROJECT + "/+log/refs/heads/master?n=1&format=JSON"
    with urllib.request.urlopen(url, timeout=30) as r:
        text = r.read().decode("utf-8")
    entry = json.loads(text.split("\n", 1)[1])["log"][0]
    return f"{PROJECT} {entry['commit'][:10]}, {entry['committer']['time']}"


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", help="read the maps from this directory (zuul/)")
    parser.add_argument("--trim", action="store_true",
                        help="leave out what the manifest walk finds (fetches the manifests)")
    args = parser.parse_args(argv)
    if args.dir:
        texts = [open(os.path.join(args.dir, f), encoding="utf-8").read() for f in FILES]
        source = f"{PROJECT}, {os.path.abspath(args.dir)}"
    else:
        texts = [_fetch(f) for f in FILES]
        source = _head()
    tests, phan = (image_mod.yaml.safe_load(t) or {} for t in texts)
    edges = extra_dependencies(tests, phan)
    if args.trim:
        unreachable = set()

        def warn(msg):
            print(msg, file=sys.stderr)

        edges = trim(edges, deps_mod.make_manifest_for(lambda _c: None, warn=warn,
                                                      unreachable=unreachable))
        if unreachable:
            warn(f"{len(unreachable)} manifests could not be fetched; their entries are kept untrimmed.")
    out.write(render(edges, source, trimmed=args.trim))
    return 0


if __name__ == "__main__":
    sys.exit(main())
