#!/usr/bin/env python3
"""`wmf-sbx create`: make a sandbox, a Lima VM (lima-port/HANDOFF-LIMA.md).

This module resolves the repos named on the command line (resolve.py),
walks the MediaWiki dependencies (deps.py), clones what is missing, and
then makes the VM: main() finds or builds the golden image (image.py),
generates the instance config (template.py), saves the state and boots
the VM (vm.py).

The helpers here that are not main() are what later phases need: the
upstream and link plans for the in-VM setup (phase 5), and the
Phabricator username for the session (phase 6). The Docker sbx code is
gone (C1a); the sbx-docker-final branch keeps it.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

from . import color as color_mod
from . import deps as deps_mod
from . import resolve as resolve_mod
from . import state as state_mod
from . import image as image_mod
from . import lima as lima_mod
from . import mediawiki as mediawiki_mod
from . import remotes as remotes_mod
from . import repos as repos_mod
from . import session as session_mod
from . import template as template_mod
from . import vm as vm_mod

CLONE_URL_BUILDERS = {
    "gerrit": lambda path: f"https://gerrit.wikimedia.org/r/{path}",
    "gitlab": lambda path: f"https://gitlab.wikimedia.org/{path}.git",
}


class LaunchError(Exception):
    """A user-facing failure building or running the sbx command."""


def clone_url(canonical):
    scheme, path = resolve_mod.split_scheme(canonical)
    try:
        builder = CLONE_URL_BUILDERS[scheme]
    except KeyError:
        raise LaunchError(f"Don't know how to clone {scheme!r} repos.") from None
    return builder(path)


def host_upstream_url(host_dir, run=subprocess.run):
    """The URL the host's own checkout at `host_dir` already has configured
    as `origin` -- or None if it isn't a git repo, has no `origin`, or that
    `origin` isn't something the sandbox can actually fetch.

    This is upstream_plan's fallback for a repo whose canonical name isn't
    known -- most commonly a raw filesystem-path argument (is_raw_path)
    that canonicals_for_kit couldn't identify, because it's a GitLab clone
    (no .gitreview -- that's Gerrit-only) with no exact repos.yaml rule
    either. Reading the host's own git config instead needs no
    forge-specific knowledge at all: it works for any already-cloned
    directory, whatever it was cloned from.

    Only an http(s) URL is used: the sandbox has no SSH agent by design
    (sbx/NOTES.md #8), so an ssh://, git@host:path, or bare local-path
    origin on the host -- all common for an engineer's own checkout --
    would just be unreachable from inside the sandbox. `local` (the host
    mirror, over the loopback git daemon) stays the fallback for those,
    same as for an unidentifiable repo."""
    try:
        result = run(
            ["git", "-C", host_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    return url


def upstream_plan(resolved, run=subprocess.run,
                  gitlab_upstream=resolve_mod.gitlab_upstream):
    """{local_dir: upstream_clone_url} for the repos we can name an upstream
    for, so the sandbox clone can call the real upstream remote `origin`
    and the host mirror `local` (sbx/DESIGN-setup-steps.md §8.1).

    Two ways to learn the upstream, tried in order: a resolved canonical
    (gerrit:/gitlab:) via clone_url() -- what do_clone would have cloned
    from -- or, failing that, whatever `origin` the host's own checkout
    already has configured (host_upstream_url). The second needs no
    project identification at all, so it covers any already-cloned
    directory clone_url()/canonical resolution can't name.

    A gitlab: canonical that is a fork is followed to its root project
    (gitlab_upstream), so `origin` in the sandbox is the real upstream and
    not the engineer's personal fork. If GitLab does not answer, the
    canonical's own URL is used (sbx/NOTES.md §100).

    A repo with neither is simply absent: the clone keeps the host mirror
    as `origin` and git-safe-reset falls back to it."""
    plan = {}
    for canonical, path in resolved:
        url = None
        if canonical is not None:
            try:
                if canonical.startswith("gitlab:"):
                    try:
                        canonical = "gitlab:" + gitlab_upstream(
                            canonical[len("gitlab:"):])
                    except resolve_mod.ResolutionError:
                        pass
                url = clone_url(canonical)
            except LaunchError:
                url = None
        if url is None:
            url = host_upstream_url(path, run=run)
        if url is not None:
            plan[path] = url
    return plan


def default_sandbox_name(primary_canonical):
    _, path = resolve_mod.split_scheme(primary_canonical)
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", path.rsplit("/", 1)[-1]).strip("-").lower()
    return f"sbx-{slug}" if slug else "sbx-sbx"


def unique_sandbox_name(name, existing, prompt=input):
    """If `name` collides with a name in `existing` (see
    existing_sandbox_names), ask the user to pick a different one -- see
    sbx/NOTES.md §19. An empty response accepts the first
    free auto-suffixed alternative ("name-2", "name-3", ...); the loop
    repeats -- re-suggesting a fresh suffix each time -- until whatever the
    user settles on doesn't collide."""
    while name in existing:
        suffix = 2
        suggestion = f"{name}-{suffix}"
        while suggestion in existing:
            suffix += 1
            suggestion = f"{name}-{suffix}"
        response = prompt(
            f"A sandbox named {name!r} already exists. Enter a different "
            f"name, or press Enter to use {suggestion!r}: "
        ).strip()
        name = response or suggestion
    return name


def split_ro_suffix(spec):
    """Splits a trailing ':ro' opt-out suffix off a repo argument -- see
    sbx/DESIGN-parallel-clone-tree.md §2. The suffix is stripped here so the
    rest of the pipeline (repo resolution, realpath'ing, nested-mount
    checks) sees a plain path.

    What the suffix still controls is only the *parallel tree* strategy:
    it's threaded through to the generated kit's wmf-sbx-setup install
    step (wmf_sbx_kit.build_kit_spec's readonly_dirs), which picks a
    read-only bind mount instead of a writable clone. It no longer decides
    whether the host-mirrored original is mounted read-only -- every extra
    is, unconditionally, at create time (see build_sbx_command).

    Returns (spec_without_suffix, is_ro)."""
    if spec.endswith(":ro"):
        return spec[: -len(":ro")], True
    return spec, False


def _warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def is_raw_path(spec):
    """True if spec is a literal filesystem path rather than a repo name
    or scheme:path spec -- real Gerrit/GitLab project paths never start
    with these characters, so this is unambiguous. Bare '.' and '..' are
    included too (not just './' and '../') since a shell never appends a
    trailing separator to those on its own."""
    return spec in (".", "..") or spec.startswith(("/", "~", "./", "../"))


def resolve_repo(spec, config_path):
    """Resolve one repo spec. Returns (canonical, realpath'd local dir,
    needs_clone) -- realpath'd per sbx/NOTES.md #1, since sbx does not
    resolve symlinks in bind-mount source paths.

    A spec that looks like a literal filesystem path (see is_raw_path)
    bypasses repo resolution entirely -- this is how container
    directories (e.g. a Skins/ or Extensions/ folder holding many
    checkouts) or other one-off reference directories that don't fit the
    one-canonical-repo model get mounted; canonical is None for these."""
    if is_raw_path(spec):
        path = os.path.realpath(os.path.expanduser(spec))
        if not os.path.isdir(path):
            raise resolve_mod.ResolutionError(f"{spec!r} is not an existing directory.")
        return None, path, False
    canonical, path, _rule, needs_clone = resolve_mod.resolve(spec, config_path)
    return canonical, os.path.realpath(os.path.expanduser(path)), needs_clone


def do_clone(canonical, path, run=subprocess.run):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = clone_url(canonical)
    print(color_mod.dim(f"+ git clone {url} {path}"), file=sys.stderr)
    result = run(["git", "clone", url, path])
    if result.returncode != 0:
        raise LaunchError(f"git clone {url} {path} failed (exit {result.returncode}).")


def find_nested_mount_conflict(dirs):
    """sbx refuses to put the primary workspace inside another bind mount
    (cananian hit this trying to mount ~/Wikimedia read-only as an extra
    while working out of ~/Wikimedia/wmf-claude) -- and the same
    ancestor/descendant overlap would be just as broken between any two
    mounts. dirs must already be realpath'd. Returns the (ancestor,
    descendant) pair, or None if there's no conflict."""
    for a in dirs:
        for b in dirs:
            if a == b:
                continue
            if b.startswith(a + os.sep):
                return a, b
    return None


def expand_dependencies(resolved, split, config, config_path, include_dev=True,
                        include_suggests=True, fetch=None, warn=None):
    """Grow the explicitly-named repo list into its full MediaWiki
    dependency closure -- see sbx/DESIGN-dependency-walk.md. Returns
    (resolved, split, origins_by_path, unreachable) -- `unreachable` being
    the repos whose manifests couldn't be fetched, i.e. the reason to
    distrust the closure.

    Discovery reads manifests from local checkouts where they exist and
    from gitiles otherwise, so nothing is cloned until the whole plan is
    known and `--dry-run` can show the real closure rather than the part
    of it that happens to already be on disk.

    Discovered repos are always extras (the primary stays whatever was
    named first, and `sbx create` requires the primary be read/write
    anyway) and never ':ro' -- you may well need to edit a dependency.
    """
    warn = warn or _warn
    # Looked up here rather than as a default argument so tests (and
    # anything else) can substitute the network call by patching the module.
    fetch = fetch or deps_mod.fetch_manifest
    rules = config.get("rules", [])

    def local_dir(canonical):
        try:
            path, _rule, needs_clone = resolve_mod.resolve_directory(canonical, rules)
        except resolve_mod.ResolutionError:
            return None
        return None if needs_clone else path

    roots = [canonical for canonical, _path in canonicals_for_kit(resolved, rules)]
    unreachable = set()
    discovered, origins = deps_mod.walk(
        roots,
        deps_mod.make_manifest_for(local_dir, fetch=fetch, warn=warn,
                                   unreachable=unreachable),
        include_dev=include_dev,
        include_suggests=include_suggests,
        overrides=config.get("dependency_overrides") or {},
        warn=warn,
    )

    resolved = list(resolved)
    split = list(split)
    origins_by_path = {}
    for canonical in discovered:
        chain = deps_mod.origin_chain(canonical, origins, roots)
        try:
            entry = resolve_repo(canonical, config_path)
        except resolve_mod.ResolutionError as e:
            # Loud, not silent: a dropped dependency doesn't announce
            # itself -- it surfaces much later as failing tests inside the
            # sandbox, which is a far worse experience than an error here.
            raise resolve_mod.ResolutionError(
                f"{e}\n{canonical} was pulled in as a dependency "
                f"({' -> '.join(chain)}). Add a repos.yaml rule for it, or "
                f"re-run with --no-deps."
            ) from None
        resolved.append(entry)
        split.append((canonical, False))
        origins_by_path[entry[1]] = chain
    return resolved, split, origins_by_path, unreachable


def link_plan(resolved_for_kit, overrides=None, warn=None):
    """{local_dir: (link_name, link_dir)} for every repo that should be
    symlinked into the core clone -- see sbx/DESIGN-dependency-walk.md §1
    and §7. Computed here, on the host, because it needs both the canonical
    names (which only exist here) and the parsed manifests.

    A repo with no manifest and no place in mediawiki/{extensions,skins}
    isn't linked at all: that's a container directory or a service, not
    something `wfLoadExtension` can find.
    """
    warn = warn or _warn
    links = {}
    for canonical, path in resolved_for_kit:
        manifest = deps_mod.read_manifest(path, warn=warn) if os.path.isdir(path) else None
        section = deps_mod.link_section(canonical, manifest)
        if section is None:
            continue
        if canonical is None:
            # An unidentified raw path: the directory the user typed is the
            # only name we have, and it's the same thing the basename rule
            # would pick for a canonical anyway.
            name = os.path.basename(path.rstrip(os.sep))
        else:
            name = deps_mod.link_name(canonical, manifest, overrides=overrides, warn=warn)
        links[path] = (name, section)
    return links


def canonicals_for_kit(resolved, rules):
    """resolved: [(canonical_or_None, path, needs_clone), ...]. A raw-path
    argument (canonical is None -- see is_raw_path) is run through
    resolve_mod.reverse_resolve (checking its .gitreview, then the config's
    exact rules) so a known repo passed by literal path -- e.g. cananian's
    own invocations, which predate this config existing -- still gets
    identified for kit environment-variable wiring (MW_CORE_REPO and
    friends), not just one passed by resolvable name. Returns
    [(canonical_or_None, path), ...] for wmf_sbx_kit.build_kit_spec."""
    result = []
    for canonical, path, _needs_clone in resolved:
        if canonical is None:
            # is_raw_path arguments are already existence-checked by
            # resolve_repo before we ever get here; don't re-stat.
            canonical, _rule = resolve_mod.reverse_resolve(path, rules, exists=lambda p: True)
        result.append((canonical, path))
    return result


# --- Host-side MCP registration (Route 1) ---------------------------------
#
# The Phabricator and Gerrit MCP servers run on the *host*, registered with
# `sbx mcp add`, and reach the sandbox through sbx's MCP gateway. That is
# the entire point of the arrangement: whatever those servers authenticate
# with -- a Phabricator username today, a Gerrit HTTP password the day
# anyone wants to post a review -- stays on the host, unreadable from the
# sandbox. Installing the servers *in* the sandbox would mean shipping
# their credentials in with them. See sbx/DESIGN-plugin-integration.md §5
# step 4 and sbx/NOTES.md §60-62.
#
# The in-sandbox half is wmf_sbx.kit's: wmf-sbx-mcp-proxy on PATH, one
# `claude mcp add` per server, each with its own --tools allowlist. This
# half makes sure the host has something for the gateway to mount.


def wmf_claude_config(path=None):
    """The checkout installer's per-user answers, or {}.

    `bin/wmf-claude-setup` writes this; we only ever read it. Any problem
    at all -- absent, unreadable, not JSON, not an object -- is "no
    answers stored", because nothing here is required and a create must
    not fail over a config file it does not own.
    """
    if path is None:
        # The same path that bin/wmf-claude-setup writes. It does not use
        # $XDG_CONFIG_HOME, so this must not either.
        path = os.path.expanduser("~/.config/wmf-claude/config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def phabricator_username(run=subprocess.run, config_path=None, env=None):
    """The username mcp-phabricator filters "my tasks" by.

    Three sources, most explicit first:

    1. `$PHABRICATOR_USERNAME`, for a host that never ran the checkout
       installer, or a one-off;
    2. `~/.config/wmf-claude/config.json`, which `bin/wmf-claude-setup`
       writes -- the answer's actual home;
    3. the `claude mcp get phabricator` output, where the answer used to
       live before (2) existed. Kept for a host whose last install
       predates it; nothing writes it any more.

    Not prompted for: wmf-sbx-create runs non-interactively often enough,
    and the server works without it -- anonymous, public data only, the
    username is a default filter.
    """
    env = os.environ if env is None else env
    override = env.get("PHABRICATOR_USERNAME")
    if override:
        return override
    stored = wmf_claude_config(config_path).get("phabricatorUsername")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    try:
        result = run(["claude", "mcp", "get", "phabricator"],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"PHABRICATOR_USERNAME=([^\s\"']+)", result.stdout or "")
    return match.group(1) if match else None


def lima_sandbox_names(lima, env=None):
    """Names that are taken: every wmf-sbx state file, and every Lima
    instance named wmf-sbx-NAME."""
    names = set(state_mod.list_names(env))
    names |= {n[len(vm_mod.PREFIX):] for n in lima.instances() if n.startswith(vm_mod.PREFIX)}
    return names


def main(argv=None, run=subprocess.run, lima=None, env=None, build=None,
         inputs_fn=None, prompt=input, now=None):
    """`wmf-sbx create` on Lima (lima-port/HANDOFF-LIMA.md §4, phase 3).

    1. resolve the repos and walk the dependencies (unchanged);
    2. find the golden image in the cache, or build it (image.py);
    3. clone the missing repos on the host, and find each repo's git dir;
    4. generate the sandbox's Lima config (template.py), which mounts the
       git dirs read-only (D10);
    5. save the state, then create and boot the VM, which copies the
       golden image (D2) and runs the provisioning;
    6. check the security invariants;
    7. clone the repos in the VM at their host paths (D8), and add the
       host remotes, which also suspend gc (repos.py, remotes.py);
    8. the session setup (session.py): wmf-claude-setup registers the MCP
       servers, as the agent;
    9. the MediaWiki setup and the session files, as the agent
       (mediawiki.py, setup.py --lima).

    `wmf-sbx resume NAME` then starts Claude Code (resume.py)."""
    parser = argparse.ArgumentParser(description="Create a wmf-sbx sandbox (a Lima VM).")
    parser.add_argument(
        "primary", help="Repo for the sandbox's primary workspace; append ':ro' for read-only"
    )
    parser.add_argument(
        "extra", nargs="*",
        help="Additional repos to make available in the sandbox; append ':ro' for read-only"
    )
    parser.add_argument("--name", help="Sandbox name (default: derived from the primary repo)")
    parser.add_argument("--config", default=resolve_mod.DEFAULT_CONFIG)
    parser.add_argument(
        "--no-deps", action="store_true",
        help="Use only the repos named on the command line -- no MediaWiki "
        "dependency walk, no implicit core/Vector",
    )
    parser.add_argument("--no-dev", action="store_true",
                        help="Skip 'dev-requires' when walking dependencies")
    parser.add_argument("--no-suggests", action="store_true",
                        help="Skip 'suggests' when walking dependencies")
    parser.add_argument("--reset-all", action="store_true",
                        help="Reset every clone to upstream master, the repos "
                        "named on the command line too")
    parser.add_argument("--no-remotes", action="store_true",
                        help="Don't touch any host .git/config: no remote "
                        "to fetch the sandbox's work, and gc is not suspended")
    parser.add_argument("--vm-type", choices=sorted(template_mod.MOUNT_TYPES),
                        help="Lima driver (default: vz on macOS, qemu elsewhere)")
    parser.add_argument("--cpus", type=int, default=template_mod.DEFAULT_CPUS)
    parser.add_argument("--memory", default=template_mod.DEFAULT_MEMORY,
                        help=f"default {template_mod.DEFAULT_MEMORY}")
    parser.add_argument("--disk", default=template_mod.DEFAULT_DISK,
                        help=f"default {template_mod.DEFAULT_DISK}")
    parser.add_argument("--image", metavar="KEY",
                        help="use this cached golden image (`wmf-sbx image ls`) "
                        "instead of the one for the current inputs")
    parser.add_argument("--sudo", action="store_true",
                        help="give the agent sudo (not yet: track A, D9/D11)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan, the image key and the Lima config; build, clone "
        "and create nothing"
    )
    args = parser.parse_args(argv)
    lima = lima or lima_mod.Limactl(run=run)
    env = os.environ if env is None else env

    def say(msg):
        print(color_mod.dim(msg), file=sys.stderr)

    if args.sudo:
        print(color_mod.error(
            "error: --sudo is not ported yet: it needs the QEMU egress proxy "
            "(lima-port/HANDOFF-LIMA.md D9, D11, track A). Use the "
            "sbx-docker-final branch for a root agent until then."), file=sys.stderr)
        return 1

    try:
        config = resolve_mod.load_config(args.config)
        split = [split_ro_suffix(spec) for spec in [args.primary] + args.extra]
        resolved = [resolve_repo(spec, args.config) for spec, _is_ro in split]
        origins_by_path = {}
        unreachable = set()
        if not args.no_deps:
            resolved, split, origins_by_path, unreachable = expand_dependencies(
                resolved, split, config, args.config,
                include_dev=not args.no_dev,
                include_suggests=not args.no_suggests,
            )
    except resolve_mod.ResolutionError as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1

    for (spec, is_ro), (canonical, path, needs_clone) in zip(split, resolved):
        ro_note = " (read-only)" if is_ro else ""
        chain = origins_by_path.get(path)
        via = f" [via {' -> '.join(c.rsplit('/', 1)[-1] for c in chain)}]" if chain else ""
        if canonical is None:
            print(f"  {spec} -> {path} (raw path){ro_note}{via}", file=sys.stderr)
        else:
            status = "needs clone" if needs_clone else "exists"
            print(f"  {spec} -> {canonical} -> {path} ({status}){ro_note}{via}", file=sys.stderr)
    if origins_by_path:
        print(f"  ({len(origins_by_path)} of these {len(resolved)} were discovered "
              f"by the dependency walk; --no-deps skips it)", file=sys.stderr)
    if unreachable:
        listed = "\n".join(f"    {c}" for c in sorted(unreachable))
        print(f"warning: {len(unreachable)} manifest(s) could not be fetched, so the "
              f"plan above may be missing repos:\n{listed}", file=sys.stderr)
        if not args.dry_run:
            print(color_mod.error(
                "error: refusing to create a sandbox from an incomplete dependency "
                "closure. Wait a minute and re-run, or pass --no-deps."), file=sys.stderr)
            return 1

    _primary_canonical, primary_dir, _ = resolved[0]
    conflict = find_nested_mount_conflict([path for _c, path, _n in resolved])
    if conflict is not None:
        ancestor, descendant = conflict
        print(color_mod.error(
            f"error: {descendant!r} is inside {ancestor!r} -- narrow one of the "
            "paths, or drop the redundant one."), file=sys.stderr)
        return 1

    try:
        taken = lima_sandbox_names(lima, env)
        name_source = (resolved[0][0] if resolved[0][0] is not None
                       else os.path.basename(primary_dir))
        name = args.name or unique_sandbox_name(
            default_sandbox_name(name_source), taken, prompt=prompt)
        state_mod.validate_name(name)
        vm_mod.instance_name(name)
        if args.name and name in taken:
            raise vm_mod.VmError(f"a sandbox named {name!r} exists already")

        # The golden image: the cache entry for the current inputs, built
        # when it is missing (phase 2), or the one --image names.
        if args.image:
            key = args.image
            image_mod.verify_entry(key, env)
            with open(os.path.join(image_mod.entry_dir(key, env), "manifest.json"),
                      encoding="utf-8") as f:
                arch = json.load(f)["inputs"]["arch"]
        else:
            inputs = (inputs_fn or image_mod.image_inputs)()
            key = image_mod.cache_key(inputs)
            arch = inputs["arch"]
            cached = os.path.isdir(image_mod.entry_dir(key, env))
            say(f"+ golden image {key} ({'in the cache' if cached else 'to build'})")
            if not args.dry_run:
                (build or image_mod.build)(inputs, lima=lima, run=run, env=env, log=say)
        golden = image_mod.golden_path(key, env)

        if not args.dry_run:
            for canonical, path, needs_clone in resolved:
                if needs_clone:
                    do_clone(canonical, path)
        repos = [repos_mod.host_repo(path, run=run, predicted=args.dry_run and needs_clone)
                 for _c, path, needs_clone in resolved]

        tmpl = template_mod.sandbox_template(
            golden, arch, gitdirs=repos_mod.git_dirs(repos), vm_type=args.vm_type,
            cpus=args.cpus,
            memory=args.memory, disk=args.disk,
            proxy_ports=template_mod.loopback_proxy_ports(env),
            ca_files=image_mod.extra_ca_files(env))
    except (state_mod.StateError, vm_mod.VmError, image_mod.ImageError,
            template_mod.TemplateError, lima_mod.LimaError, repos_mod.RepoError,
            LaunchError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        return 1

    if args.dry_run:
        say(f"+ would create sandbox {name!r} (Lima instance {vm_mod.instance_name(name)}) "
            f"with this config:")
        print(color_mod.dim(image_mod.yaml.safe_dump(tmpl, sort_keys=False)),
              file=sys.stderr, end="")
        return 0

    # The state first: a create that fails half-way leaves a VM that
    # `wmf-sbx rm NAME` can then remove.
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(timespec="seconds")
    readonly = {path for (_spec, is_ro), (_c, path, _n) in zip(split, resolved) if is_ro}
    state = state_mod.new_state(
        name, created=stamp, primary_dir=primary_dir, image=key,
        vm_type=tmpl["vmType"], repos=[path for _c, path, _n in resolved],
        read_only=sorted(readonly))
    state_mod.save(state, env)
    try:
        vm_mod.create(name, tmpl, lima=lima, log=say)
    except (vm_mod.VmError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        print(f"  the state is kept; `wmf-sbx rm {name}` removes what was made.",
              file=sys.stderr)
        return 1

    from . import start as start_mod
    print(f"{name}: created; checking it", file=sys.stderr)
    if not start_mod.check(name, lima=lima):
        print(color_mod.error(f"error: {name} breaks a security invariant; do not use "
                              f"it. `wmf-sbx rm {name}`."), file=sys.stderr)
        return 1

    # The clones in the VM, then the host remotes. Requested repos keep
    # the host's branch; dependencies go to upstream master, and
    # --reset-all resets every repo (DESIGN-setup-steps.md §8.1).
    requested = {path for _c, path, _n in resolved if path not in origins_by_path}
    keep = set() if args.reset_all else requested | {primary_dir}
    # Raw paths get their canonical names here (.gitreview, the config's
    # rules), for the upstream URLs and the MediaWiki roles (core, links).
    resolved_for_kit = canonicals_for_kit(resolved, config.get("rules", []))
    try:
        upstreams = upstream_plan(resolved_for_kit, run=run)
        repos_mod.clone_in_vm(name, repos, upstreams, keep, readonly=readonly,
                              lima=lima, env=env)
    except (repos_mod.RepoError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        print(f"  `wmf-sbx rm {name}` removes what was made.", file=sys.stderr)
        return 1
    if args.no_remotes:
        print("warning: --no-remotes: the host cannot fetch from this sandbox, and "
              "gc stays on in the host repos, so a host `git gc --prune=now` can "
              "break the clones in the VM.", file=sys.stderr)
    else:
        added, skipped = remotes_mod.sync_remotes(
            name, repos_mod.remote_candidates(name, repos), run=run,
            warn=lambda m: print(f"warning: {m}", file=sys.stderr))
        state["remotes"], state["skipped"] = added, skipped
        state_mod.save(state, env)

    plan = mediawiki_mod.build_plan(
        resolved_for_kit,
        links=link_plan(resolved_for_kit, overrides=config.get("link_overrides") or {}),
        readonly=readonly, primary=primary_dir)
    plan["session"] = session_mod.session_plan(name, resolved_for_kit, readonly)
    try:
        home = env.get("HOME") or os.path.expanduser("~")
        session_mod.wmf_claude_setup(
            name, phabricator_username(
                run=run, env=env,
                config_path=os.path.join(home, ".config", "wmf-claude", "config.json")),
            lima=lima, env=env)
        mediawiki_mod.run_setup(name, plan, lima=lima, env=env)
    except (mediawiki_mod.SetupError, session_mod.SessionError, lima_mod.LimaError) as e:
        print(color_mod.error(f"error: {e}"), file=sys.stderr)
        print(f"  The VM, the clones and the host remotes are kept; fix the "
              f"problem with `wmf-sbx exec {name} -- ...`, or `wmf-sbx rm {name}`.",
              file=sys.stderr)
        return 1

    print(f"\nSandbox {name} is ready (Lima instance {vm_mod.instance_name(name)}).\n"
          f"  wmf-sbx resume {name}          start Claude Code in it\n"
          f"  wmf-sbx exec {name} -- CMD     run a command as the agent\n"
          f"  wmf-sbx status {name}          check it\n"
          f"  wmf-sbx stop|start {name}\n"
          f"  wmf-sbx rm {name}\n"
          + ("" if args.no_remotes else
             f"  git fetch {name}               (on the host, in a repo) the agent's work\n"),
          file=sys.stderr, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
