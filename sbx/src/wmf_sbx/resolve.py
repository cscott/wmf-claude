#!/usr/bin/env python3
"""Resolve a repo name to a canonical Gerrit/GitLab path and a local
directory to use (or clone into) as an sbx workspace.

Implements the two-step design in sbx/DESIGN-repo-resolution.md:

  1. name -> canonical "scheme:path" string (Gerrit REST lookup for a bare
     short name; explicit scheme or an already-qualified path is used as-is).
  2. canonical path -> local directory, via the user's
     ~/.config/wmf-sbx/repos.yaml rules (longest-match-first among existing
     directories; longest match outright when nothing exists yet to clone).

Requires PyYAML only for reading the config file; the resolution/matching
logic itself has no dependencies, so it's directly unit-testable.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when yaml is absent
    yaml = None

GERRIT_URL = "https://gerrit.wikimedia.org/r/projects/"
DEFAULT_CONFIG = os.path.expanduser("~/.config/wmf-sbx/repos.yaml")
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(.*)$")


class ResolutionError(Exception):
    """A user-facing resolution failure (ambiguous name, no matching rule,
    unreachable Gerrit, etc.) -- as opposed to a programming error."""


# --------------------------------------------------------------------------
# Step 1: name -> canonical "scheme:path"
# --------------------------------------------------------------------------

def split_scheme(spec):
    """"gerrit:mediawiki/core" -> ("gerrit", "mediawiki/core").
    No scheme present -> (None, spec)."""
    m = _SCHEME_RE.match(spec)
    if m:
        return m.group(1), m.group(2)
    return None, spec


def _strip_xssi(body):
    # Gerrit prefixes every JSON response with a )]}' XSSI guard line.
    if body.startswith(")]}'"):
        return body.split("\n", 1)[1]
    return body


_ARCHIVED_MARKER = "[ARCHIVED]"


def gerrit_search(substring):
    """Query Gerrit's REST API for projects whose path contains `substring`
    anywhere (the server does the substring match; we still filter the
    result down to an exact final-segment match ourselves).

    Returns (names, archived): `names` is every matching full Gerrit project
    path, and `archived` is the subset of those names whose description
    contains "[ARCHIVED]" (WMF's convention for retired repos -- e.g.
    gerrit:mediawiki/extensions/Parsoid, a defunct extension that shares a
    final path segment with the live gerrit:mediawiki/services/parsoid, or
    gerrit:analytics/asana-stats, which has no live namesake at all).
    Archived projects are never excluded here -- a bare name whose *only*
    match is archived still needs to resolve (see sbx/NOTES.md); callers use
    `archived` to warn the user, not to filter results."""
    params = {"m": substring}
    # "d" (description) is a bare flag in Gerrit's REST API, not a
    # key=value param -- we always ask for it since we need descriptions to
    # flag archived projects.
    url = GERRIT_URL + "?" + urllib.parse.urlencode(params) + "&d"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as e:
        raise ResolutionError(f"Could not reach Gerrit ({url}): {e}") from e
    try:
        data = json.loads(_strip_xssi(body))
    except json.JSONDecodeError as e:
        raise ResolutionError(f"Unexpected response from Gerrit ({url}): {e}") from e
    archived = {
        name for name, info in data.items()
        if _ARCHIVED_MARKER in (info.get("description") or "")
    }
    return list(data.keys()), archived


def resolve_canonical_path(spec, search=gerrit_search, warn=None):
    """Step 1. `search` is injectable for testing -- it takes a substring
    and returns (names, archived), see gerrit_search. `warn`, if given, is
    called with a message when the resolved bare name turns out to be an
    archived Gerrit project -- it's still resolved either way, never
    silently dropped (see sbx/NOTES.md).

    Among case-insensitively-matching candidates, a non-archived one is
    preferred by default over an archived one sharing the same final
    segment (e.g. bare "Parsoid" resolves to the live
    gerrit:mediawiki/services/parsoid over the archived
    gerrit:mediawiki/extensions/Parsoid) -- pass the full gerrit:... path
    instead of the bare name if you specifically want the archived one."""
    scheme, rest = split_scheme(spec)
    if scheme is not None:
        # Already fully qualified: used as-is, no lookup.
        return f"{scheme}:{rest}"
    if "/" in rest:
        # A full Gerrit path was given, just without the "gerrit:" prefix.
        return f"gerrit:{rest}"

    # Bare short name: narrow via Gerrit's substring search, then require
    # a case-insensitive match on the final path segment (Gerrit's "m=" is
    # a plain substring match, so e.g. "Cite" also returns "CiteDrawer").
    candidates, archived = search(rest)
    exact = sorted(
        name for name in candidates
        if name.rsplit("/", 1)[-1].casefold() == rest.casefold()
    )
    if not exact:
        raise ResolutionError(f"No Gerrit project found matching {rest!r}.")
    non_archived = [name for name in exact if name not in archived]
    pool = non_archived or exact
    if len(pool) > 1:
        listing = "\n".join(f"  gerrit:{name}" for name in pool)
        raise ResolutionError(
            f"{rest!r} is ambiguous, matches:\n{listing}\n"
            "Pass one of these full paths instead of the bare name."
        )
    chosen = pool[0]
    if warn is not None and chosen in archived:
        warn(f"warning: gerrit:{chosen} is marked {_ARCHIVED_MARKER} in Gerrit.")
    return f"gerrit:{chosen}"


# --------------------------------------------------------------------------
# Step 2: canonical path -> local directory
# --------------------------------------------------------------------------

def _segments(scheme_path):
    scheme, rest = split_scheme(scheme_path)
    if scheme is None:
        raise ValueError(f"{scheme_path!r} has no scheme (gerrit:/gitlab:).")
    return scheme, (rest.split("/") if rest else [])


def match_rule(pattern, canonical):
    """Return a dict of captured names if `pattern` matches `canonical`,
    else None. Both are "scheme:a/b/c" strings. Grammar: a segment is a
    literal, "**" (zero or more segments, at most one per pattern), or
    "{name}" (final segment only, captures exactly one segment)."""
    pat_scheme, pat_segs = _segments(pattern)
    can_scheme, can_segs = _segments(canonical)
    if pat_scheme != can_scheme:
        return None

    if pat_segs and pat_segs[-1].startswith("{") and pat_segs[-1].endswith("}"):
        name_var = pat_segs[-1][1:-1]
        prefix_pat = pat_segs[:-1]
    else:
        name_var = None
        prefix_pat = pat_segs

    if name_var is None:
        return {} if prefix_pat == can_segs else None

    if not can_segs:
        return None  # nothing left to capture as {name}
    body, captured = can_segs[:-1], can_segs[-1]

    if "**" in prefix_pat:
        if prefix_pat.count("**") > 1:
            raise ValueError(f"Pattern {pattern!r} has more than one '**'.")
        wi = prefix_pat.index("**")
        before, after = prefix_pat[:wi], prefix_pat[wi + 1:]
        if len(body) < len(before) + len(after):
            return None
        if body[:len(before)] != before:
            return None
        if after and body[-len(after):] != after:
            return None
    else:
        if body != prefix_pat:
            return None

    return {name_var: captured}


def specificity(pattern):
    """Number of literal segments -- used to rank overlapping rules."""
    _, segs = _segments(pattern)
    return sum(1 for s in segs if s != "**" and not (s.startswith("{") and s.endswith("}")))


def matching_rules(canonical, rules):
    """rules: [{"match": pattern, "path": template}, ...]. Returns
    [(rule, captures), ...] sorted most-specific first; ties keep the
    original config order."""
    scored = []
    for idx, rule in enumerate(rules):
        captures = match_rule(rule["match"], canonical)
        if captures is not None:
            scored.append((specificity(rule["match"]), idx, rule, captures))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(rule, captures) for _, _, rule, captures in scored]


def expand_path(template, captures):
    return os.path.expanduser(template.format(**captures))


def exact_rule_canonicals(rules):
    """Reverse map: realpath'd directory -> canonical "scheme:path" string,
    for every *exact* rule (no "**"/"{name}") in `rules`. An exact rule's
    `match` already *is* its canonical path, so this needs no Gerrit/GitLab
    lookup -- it's how wmf_sbx/kit.py identifies which known repo a raw
    filesystem-path argument (see wmf_sbx_create.is_raw_path) actually is,
    so kit environment variables like MW_CORE_REPO can be wired up even
    when a repo was passed as a literal path rather than a resolved name."""
    mapping = {}
    for rule in rules:
        _scheme, segs = _segments(rule["match"])
        if any(s == "**" or (s.startswith("{") and s.endswith("}")) for s in segs):
            continue
        mapping[os.path.realpath(expand_path(rule["path"], {}))] = rule["match"]
    return mapping


def gitreview_project(path):
    """Return the Gerrit project path recorded in <path>/.gitreview's
    "project=" line (any trailing ".git" stripped), or None if the file
    doesn't exist or has no such line."""
    try:
        with open(os.path.join(path, ".gitreview"), encoding="utf-8") as f:
            contents = f.read()
    except OSError:
        return None
    for line in contents.splitlines():
        line = line.strip()
        if line.startswith("project="):
            project = line[len("project="):].strip()
            if project.endswith(".git"):
                project = project[:-len(".git")]
            return project
    return None


def reverse_resolve(path, rules, exists=os.path.isdir, gitreview_project=gitreview_project):
    """The inverse of Step 2: given a local directory, identify the
    canonical "scheme:path" it corresponds to and the repos.yaml rule (if
    any) that maps to it. Returns (canonical, rule); either or both may be
    None if nothing can be identified (see below).

    A ".gitreview" file's "project=" line, if present, is authoritative --
    repos.yaml rules are hand-maintained and can go stale (see
    resolve_directory's belt-and-suspenders check above), so it's used even
    if it disagrees with what a rule would have predicted for this path. In
    that case `rule` is whichever configured rule's expansion actually
    produces this exact directory, or None if none does (report this case
    as "<no rule>" -- the .gitreview project is still authoritative, it's
    just not one repos.yaml knows how to reproduce).

    Without a .gitreview (not a Gerrit checkout, or not cloned yet), falls
    back to the config's *exact* (no "**"/"{name}") rules only -- see
    exact_rule_canonicals. A wildcard rule can't be inverted from a bare
    directory alone: many different canonicals can collapse to the same
    local path template (e.g. "gerrit:**/{name}" -> "~/Wikimedia/{name}"
    loses which Gerrit namespace "{name}" came from), so a .gitreview (or
    an exact rule, which has only one possible canonical by construction)
    is required to resolve that ambiguity."""
    expanded = os.path.expanduser(path)
    if not exists(expanded):
        raise ResolutionError(f"{path!r} is not an existing directory.")
    recorded = gitreview_project(expanded)
    if recorded is not None:
        canonical = f"gerrit:{recorded}"
        for rule, captures in matching_rules(canonical, rules):
            if os.path.realpath(expand_path(rule["path"], captures)) == os.path.realpath(expanded):
                return canonical, rule
        return canonical, None
    canonical = exact_rule_canonicals(rules).get(os.path.realpath(expanded))
    if canonical is None:
        return None, None
    rule = next((r for r in rules if r["match"] == canonical), None)
    return canonical, rule


def resolve_directory(canonical, rules, exists=os.path.isdir, gitreview_project=gitreview_project):
    """Step 2. Returns (path, rule, needs_clone).

    Belt-and-suspenders check: a Gerrit canonical whose candidate directory
    already exists AND has a .gitreview file is only accepted if that
    file's "project=" line agrees with what we expect there -- repos.yaml
    rules are hand-maintained and directories get renamed/repurposed, so an
    existing directory at the expected path isn't proof it's the right
    checkout. A directory with no .gitreview (or a scheme other than
    gerrit, which .gitreview doesn't apply to) is accepted as before,
    trusting the rule. On a mismatch we keep looking at the next
    (less-specific) candidate, exactly like a plain missing directory --
    UNLESS the most-specific candidate (the one that would otherwise become
    the fresh-clone target) is itself an existing-but-mismatched directory:
    "needs_clone" means "safe to git clone into this path", which isn't
    true of a directory that already holds the wrong checkout, so that's a
    hard error instead of a silent needs_clone."""
    candidates = matching_rules(canonical, rules)
    if not candidates:
        raise ResolutionError(f"No config rule matches {canonical!r}.")
    can_scheme, can_rest = split_scheme(canonical)
    for rule, captures in candidates:
        path = expand_path(rule["path"], captures)
        if not exists(path):
            continue
        if can_scheme == "gerrit":
            recorded = gitreview_project(path)
            if recorded is not None and recorded != can_rest:
                continue
        return path, rule, False
    rule, captures = candidates[0]
    path = expand_path(rule["path"], captures)
    if exists(path):
        recorded = gitreview_project(path) if can_scheme == "gerrit" else None
        raise ResolutionError(
            f"{path} already exists, but its .gitreview says {recorded!r}, "
            f"not {can_rest!r} -- refusing to treat it as a fresh-clone "
            "target. Fix or remove it, or adjust repos.yaml."
        )
    return path, rule, True


# --------------------------------------------------------------------------
# Config loading and CLI
# --------------------------------------------------------------------------

def load_config(path=DEFAULT_CONFIG):
    if not os.path.exists(path):
        return {"rules": [], "extra_dependencies": {}, "extra_environment": {}, "extra_packages": []}
    if yaml is None:
        raise ResolutionError(
            f"PyYAML is required to read {path} but isn't installed.\n"
            "Install it in a venv, e.g.:\n"
            "  python3 -m venv ~/.venvs/wmf-sbx && ~/.venvs/wmf-sbx/bin/pip install pyyaml\n"
            "and run this script with that venv's python3."
        )
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("rules", [])
    data.setdefault("extra_dependencies", {})
    data.setdefault("extra_environment", {})
    data.setdefault("extra_packages", [])
    return data


def resolve(spec, config_path=DEFAULT_CONFIG, search=gerrit_search, exists=os.path.isdir,
            warn=lambda msg: print(msg, file=sys.stderr)):
    """End-to-end convenience entry point. Returns (canonical, path, rule,
    needs_clone)."""
    config = load_config(config_path)
    canonical = resolve_canonical_path(spec, search=search, warn=warn)
    path, rule, needs_clone = resolve_directory(canonical, config["rules"], exists=exists)
    return canonical, path, rule, needs_clone


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "name", nargs="?", default=None,
        help="Bare name, full Gerrit path, or scheme:path (e.g. Cite, "
        "mediawiki/extensions/Cite, gerrit:mediawiki/extensions/Cite)"
    )
    parser.add_argument(
        "--path", help="Reverse lookup: given a local directory, identify the "
        "canonical Gerrit/GitLab project and repos.yaml rule it corresponds "
        "to, instead of resolving NAME to a directory"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to repos.yaml")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)

    if (args.name is None) == (args.path is None):
        parser.error("give exactly one of NAME or --path")

    if args.path is not None:
        try:
            config = load_config(args.config)
            canonical, rule = reverse_resolve(args.path, config["rules"])
        except ResolutionError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if canonical is None:
            print(
                f"error: no canonical project found for {args.path!r} "
                "(no .gitreview, and no exact repos.yaml rule matches this "
                "directory).",
                file=sys.stderr,
            )
            return 1
        # realpath so this reports the path wmf-sbx-create would actually
        # mount, not the symlink the engineer typed -- see create._SBX_ROOT.
        path = os.path.realpath(os.path.expanduser(args.path))
        if args.json:
            print(json.dumps({
                "canonical": canonical,
                "path": path,
                "rule": rule["match"] if rule else None,
                "needs_clone": False,
            }))
        else:
            print(f"canonical: {canonical}")
            print(f"path:      {path} (exists)")
            print(f"rule:      {rule['match'] if rule else '<no rule>'}")
        return 0

    try:
        canonical, path, rule, needs_clone = resolve(args.name, args.config)
    except ResolutionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "canonical": canonical,
            "path": path,
            "rule": rule["match"],
            "needs_clone": needs_clone,
        }))
    else:
        print(f"canonical: {canonical}")
        print(f"path:      {path}{' (needs clone)' if needs_clone else ' (exists)'}")
        print(f"rule:      {rule['match']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
