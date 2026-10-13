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

Usage (from sbx/src):
  python3 -m wmf_sbx.ci_deps > ../reference/ci-extra-dependencies.yaml
  python3 -m wmf_sbx.ci_deps --dir DIR   # a local integration/config/zuul
"""

import argparse
import base64
import json
import os
import sys
import urllib.request

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


def render(edges, source):
    lines = [
        "# settings.yaml `extra_dependencies:` from WMF CI's dependency maps:",
        f"# {source}. Made by `python3 -m wmf_sbx.ci_deps` in sbx/src.",
        "# Copy the block into ~/.config/wmf-sbx/settings.yaml (it replaces",
        "# `extra_dependencies: {}`). Each entry is what CI checks out when that",
        "# repo is under test; `wmf-sbx create` adds it for the repos you name,",
        "# not for their dependencies. `# phan`: only CI's phan job has it.",
        "extra_dependencies:",
    ]
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
    args = parser.parse_args(argv)
    if args.dir:
        texts = [open(os.path.join(args.dir, f), encoding="utf-8").read() for f in FILES]
        source = f"{PROJECT}, {os.path.abspath(args.dir)}"
    else:
        texts = [_fetch(f) for f in FILES]
        source = _head()
    tests, phan = (image_mod.yaml.safe_load(t) or {} for t in texts)
    out.write(render(extra_dependencies(tests, phan), source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
