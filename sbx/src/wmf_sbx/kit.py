#!/usr/bin/env python3
"""What the Lima backend keeps of the old Docker kit generator.

The Docker sbx kit (a spec.yaml with install and startup commands, a
port for the git daemon, the MCP proxy) is gone (C1a; the
sbx-docker-final branch keeps it). What is left is backend-neutral and
used by image.py and the later phases (lima-port/HANDOFF-LIMA.md §10):

- BASE_PACKAGES, EXTRA_DOMAINS and the helper scripts (image.py; D3);
- the plugin tree: overlay, patches, state files, the settings and
  ~/.claude.json patches (the session, phase 6);
- the home and workspace CLAUDE.md text (phase 6);
- the setup plan and the MediaWiki environment variables (phase 5; D4).
"""

import datetime
import json
import os
import shutil
import subprocess
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when yaml is absent
    yaml = None

# The one sibling import that goes this direction: the setup script can't
# import anything (it's copied into the kit as a single standalone file --
# see its docstring), but this module runs on the host, where the whole
# tree is present. Reused here for parse_install_params, so the kit's
# environment and the .env the setup script writes can't disagree.
from . import setup as setup_mod

# This module lives in sbx/src/wmf_sbx/; SBX_ROOT is sbx/, REPO_ROOT the
# wmf-claude checkout that contains it. realpath, like create._SBX_ROOT:
# the checkout is commonly reached through a symlink, and a symlinked
# host path is the wrong thing to hand to anything that mounts (§66).
SBX_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))))
REPO_ROOT = os.path.dirname(SBX_ROOT)


# Shipped into files/home/bin/, from where wmf-sbx-setup installs them into
# /usr/local/bin so they are on PATH for root and for the agent (see
# sbx/DESIGN-setup-steps.md §2.3). These are bash scripts, not part of this
# python package, and they come from two directories, so each name names its
# own source:
#
#  - sbx/bin/ holds the git helpers, because engineers run them on the host
#    too. Both names are needed: `git-safe-reset` so `git safe-reset`
#    resolves as a git subcommand, and `git-review-check` because
#    git-safe-reset invokes it by bare name.
#  - sbx/helpers/ holds the scripts that only run inside a sandbox.
#    `mw-install-browser` installs Chrome for the karma and selenium suites
#    (sbx/DESIGN-testing-instructions.md §5.3). `mw-install-cypress`
#    installs a repo's Cypress binary and the packages it needs
#    (sbx/NOTES.md §91).
HELPER_SCRIPT_DIR = os.path.join(SBX_ROOT, "bin")
SANDBOX_HELPER_DIR = os.path.join(SBX_ROOT, "helpers")
HELPER_SCRIPTS = {
    "git-safe-reset": HELPER_SCRIPT_DIR,
    "git-review-check": HELPER_SCRIPT_DIR,
    "mw-install-browser": SANDBOX_HELPER_DIR,
    "mw-install-cypress": SANDBOX_HELPER_DIR,
}

# The in-sandbox testing guide, shipped to ~/MEDIAWIKI-TESTING.md. It goes
# beside CLAUDE.md, not in it and not in ~/.claude/: it is reference
# material to read when the agent tests, not context for every session. The
# CLAUDE.md "Running tests" section points at it
# (sbx/DESIGN-testing-instructions.md §3).
TESTING_GUIDE = "MEDIAWIKI-TESTING.md"
TESTING_GUIDE_SOURCE = os.path.join(SBX_ROOT, "templates", TESTING_GUIDE)


# The JSON plan that setup script reads, written next to it by
# write_kit_dir. Positional argv can't carry the symlink plan -- link names
# contain spaces, and canonical names only exist host-side -- so anything
# beyond "clone these and start a daemon" goes through this file. Shape:
# sbx/DESIGN-setup-steps.md §1.
SANDBOX_PLAN_FILE = setup_mod.SANDBOX_PLAN_FILE
PLAN_VERSION = 1

CORE_CANONICAL = setup_mod.CORE_CANONICAL

# --- The wmf-claude plugin, shipped in the kit (Route A) ------------------
#
# sbx/DESIGN-plugin-integration.md §3. `~/.claude/skills` is a mount sbx
# owns, so a kit cannot put skills there; `~/.claude/plugins/` is an
# ordinary directory, so the way in is the plugin wiring the nono pack
# already knows how to write -- and that carries agents and the
# SessionStart hook too, which the skills store cannot (sbx/NOTES.md
# §55.1).
#
# These four names are the same ones package.json's `wiring` block uses
# on the host. They are Claude Code *internal* state, not a public
# contract, which is why plugin_check_startup_command asserts the result
# rather than trusting the write.
PLUGIN_NAME = "wmf-claude"
MARKETPLACE_NAME = "wikimedia"
PLUGIN_MANIFEST = os.path.join(REPO_ROOT, ".claude-plugin", "plugin.json")
WIRING_DIR = os.path.join(REPO_ROOT, "wiring")

# What gets copied into the sandbox as "the plugin". Directories are taken
# whole, so a skill or agent added to the repo is in the next kit with no
# list to update -- the fourth-hand-maintained-copy problem the parent
# package already had once (sbx/NOTES.md §56.4). templates/ is here
# because skills/init-project reads ${CLAUDE_PLUGIN_ROOT}/templates/.
#
# bin/ is deliberately *not* taken whole: it is mostly host-side launchers
# (bin/claude, bin/launch-docker-broker, bin/wmf-claude-setup) that mean
# nothing inside an sbx sandbox and would only invite the agent to run
# them. Only the file hooks.json actually invokes comes along.
PLUGIN_TREE_DIRS = (".claude-plugin", "agents", "hooks", "skills", "templates")
PLUGIN_TREE_FILES = ("bin/session-start.sh",)
PLUGIN_EXECUTABLES = frozenset(["bin/session-start.sh"])

# The two ways sbx text reaches the shipped plugin tree without a line of
# it in the shared tree, which would have to be undone to merge with
# upstream (sbx/DESIGN-testing-instructions.md §6).
#
#   plugin-overlay/  whole files, laid out under the path they need inside
#                    the plugin tree, for text only an sbx sandbox reads.
#   patches/plugin/  unified diffs against files the nono backend shares,
#                    applied to the copy the kit installs.
PLUGIN_OVERLAY_DIR = os.path.join(SBX_ROOT, "plugin-overlay")
PLUGIN_PATCH_DIR = os.path.join(SBX_ROOT, "patches", "plugin")


# Written to files/home/.claude/CLAUDE.md -- Claude Code loads a user-level
# CLAUDE.md from there automatically, with no plugin/hook install step
# needed (confirmed: this loads even though the wmf-claude plugin itself
# isn't wired into the generated kit at all yet).
#
# It no longer asks Claude to *move*: wmf-sbx-setup mounts each writable
# clone over the host path it came from, so the starting cwd already is
# the clone (sbx/NOTES.md §39). It used to say "run `/cd /home/agent/...`
# first thing", which was unachievable -- `/cd` is a slash command the
# *user* types, and the model duly reported that it had no such tool
# (§30). What's left is a description of the layout, which Claude needs
# because the parallel tree, the read-only originals and the swapped
# remotes are all still there and all still surprising.
#
# It also has to *contradict* a second CLAUDE.md. sbx writes its own
# boilerplate at the top of the workspace tree, and its "Git workspace
# mode" section offers two modes, chosen by `[ -d /run/sandbox/source ]`.
# We are in neither: wmf-sbx-setup makes the clone itself and bind-mounts
# it over the host path, so that probe answers "direct mode" and direct
# mode's text -- "your edits, commits, and branches appear on the host
# immediately" -- is false here. That is not a cosmetic error: an agent
# that believes the host can already see its files has no reason to
# commit, which is exactly the "changes made, waiting for instructions to
# commit" ending that leaves the work unreachable from the host. Two
# documents disagreeing in context is worse than one being wrong, so name
# the other one and say which wins. Since §95, a startup step also edits
# the wrong sections out of that file, so this text is the fallback for a
# sandbox where the edits no longer apply.
#
# It is a template: home_claude_md() puts the path of the other file in
# place of WORKSPACE_CLAUDE_MD_TOKEN.
WORKSPACE_CLAUDE_MD_TOKEN = "@WORKSPACE_CLAUDE_MD@"
HOME_CLAUDE_MD = """\
## This sandbox's repo layout

Your working directory is a **private, writable git clone**, mounted over
the path the host's own checkout occupies. So the paths here match the
paths the engineer uses, but nothing you write touches their files.

- The same clone is also at `/home/agent/<path below your home>` -- e.g.
  `/home/agent/Projects/Wikimedia/core`. One directory, two paths.
- The host's checkout is still readable, at
  `/home/agent/.sbx-originals/<that same relative path>`. Look there for
  the engineer's uncommitted state; you cannot write to it.
- `origin` is Gerrit, `local` is the host's checkout. Branch and commit
  freely -- the engineer fetches your work out of this sandbox over
  `git://`, so committing is how you hand it over.

If your working directory turns out to be read-only (a write fails with
"Read-only file system"), the clone did not get mounted over it: your
writable copy is the `/home/agent/...` path above. Work there, and say so.

## The other CLAUDE.md

sbx writes a second CLAUDE.md, at `@WORKSPACE_CLAUDE_MD@`. It is already
in your context, so do not look for it. It is in no repo: do not edit it
or commit it. Its advice on the network policy and the shell environment
is correct. Its sections on git were written for other sandboxes, and
wmf-sbx edits them when the sandbox starts.

If that file still has a "Git workspace mode" section that tells you to
pick between "direct mode" and "clone mode" with
`[ -d /run/sandbox/source ]`, the edit did not happen. That directory does
not exist here, so the probe says direct mode -- and direct mode's
promise, that your edits and commits "appear on the host immediately", is
**false in this sandbox**. You are in neither of its modes: this clone was
made by wmf-sbx, not by sbx's own `--clone`, and nothing you write is
visible on the host until the engineer fetches it. That section also
names the host's remote `sandbox-<name>`. The remote is named `<name>`
exactly -- this sandbox's own name, with no prefix added.

Where the two files disagree, this one is right.

## End your turn with a commit

Committing is the only way your work leaves this sandbox. The engineer
already has a remote pointing at it in each writable repo, and fetches
with:

```bash
git -C <that repo on the host> fetch <this sandbox's name>
```

Get the name from `$SANDBOX_NAME` (or `hostname`) and quote it back
literally when you report -- do **not** guess it from the branch, the
directory, or the repo name, which are unrelated to it.

So commit before you stop, on a branch, even when the work is
unfinished, and say which branch and commit you left. "Changes made,
waiting for instructions to commit" is not a stopping point: it leaves
the engineer unable to see, run, or review any of it. A commit is cheap
to take back -- `git commit --amend`, or `git reset --soft HEAD^` -- on
your next turn.

## Running tests

This sandbox holds its own set of repos; `ls "$MW_INSTALL_PATH"/extensions
"$MW_INSTALL_PATH"/skins` and `echo "$PARSOID"` show which. Everything is
installed: `vendor/` and `node_modules/` in every repo, and a SQLite wiki at
`$MW_INSTALL_PATH` with every checked-out extension and skin loaded.
`~/MEDIAWIKI-TESTING.md` is the full guide. Read it before your first test
run in a session, not after a failure.

Before your change, run `git log --oneline origin/master..HEAD` and
`git status --short` in the repo, and run its suite once, so you know which
failures were already there.

*If you are working on mediawiki-core*, run PHPUnit from `$MW_INSTALL_PATH`
through composer, `composer phpunit:entrypoint -- <path>`, and lint with
`composer test` and `npm test`. Jest is `npm run jest`.

*If you are working on an extension or a skin*, PHPUnit still runs **from
core**, with a path relative to it:
`composer phpunit:entrypoint -- extensions/<Name>/tests/phpunit`. Lint runs
from the repo: `composer test`, `npm test`. Jest runs from the repo, and only
if its `package.json` has a script for it; core's Jest never runs extension
tests. QUnit runs from core: `npx grunt karma:chrome --qunit-component=<Name>`.

*If you are working on Parsoid*, it has its own suites, run from `$PARSOID`:
`composer phpunit`, `composer parserTests`, `composer lint`. The wiki, and
core's and extensions' PHPUnit runs, use `$PARSOID` and not core's vendor
copy; `MEDIAWIKI_HAS_INTEGRATION_TESTS=1` in the environment does that for
unit tests. Do not unset it.

`vendor/bin/phpunit <path>` fails with `Class "MediaWikiUnitTestCase" not
found` until something generates `phpunit.xml`; the composer script does
that first.

QUnit and selenium need a browser, which is not installed by default: run
`mw-install-browser` once (about 1 minute, 420 MB), then start the wiki with
`composer serve`. Do not change `MW_SCRIPT_PATH`: it is `/` on purpose, as
in CI.

Cypress e2e tests (Cite's `selenium-test`, for one) can run here, but the
binary is not installed: `mw-install-cypress <repo>` installs it (about
800 MB). Install it only when your change is likely to be covered by that
repo's Cypress specs. Many specs also need extensions that this sandbox may
not have, and without them the run skips them and passes with 0 tests. If
you skip Cypress, say so.

`composer phan` reports undeclared classes from sibling extensions that
`.phan/config.php` names and this sandbox does not have. Those errors are
not yours.
"""


def workspace_claude_md_path(plan):
    """Where sbx writes its own CLAUDE.md: in the parent directory of the
    primary workspace (MEASURED for two parent directories, sbx/NOTES.md
    §95). None without a plan that names the primary workspace."""
    primary = (plan or {}).get("primary")
    if not primary:
        return None
    return os.path.join(os.path.dirname(primary.rstrip("/")), "CLAUDE.md")


def home_claude_md(plan=None):
    """HOME_CLAUDE_MD with the path of sbx's CLAUDE.md filled in. Without
    a plan, the text says where to find it instead."""
    path = workspace_claude_md_path(plan)
    if path is None:
        return HOME_CLAUDE_MD.replace(
            f"at `{WORKSPACE_CLAUDE_MD_TOKEN}`",
            "in the parent directory of the directory you started in")
    return HOME_CLAUDE_MD.replace(WORKSPACE_CLAUDE_MD_TOKEN, path)

# The fixed sandbox-internal port every generated kit's git daemon listens
# on. The generated kit declares it in its own top-level `ports:` block, so
# sbx publishes it on every container start; wmf_sbx_create only has to
# *look up* the (ephemeral, ever-changing) host port it landed on. Every
# real wmf-sbx-create invocation passes this same value explicitly as
# daemon_port, so this constant only matters as a fallback for kit specs
# built directly (e.g. in tests).
DEFAULT_DAEMON_PORT = 9977


# Canonical "gerrit:..." path (as produced by wmf_sbx/resolve.py, or
# inferred for a raw filesystem path via wmf_sbx_resolve.exact_rule_canonicals)
# -> kit environment variable(s) that should point at that repo's checkout,
# wherever this sandbox invocation put it.
REPO_ENVIRONMENT_VARS = {
    "gerrit:mediawiki/core": ["MW_INSTALL_PATH", "MW_CORE_REPO"],
    "gerrit:mediawiki/services/parsoid": ["PARSOID"],
    "gerrit:mediawiki/vendor": ["MW_VENDOR_REPO"],
}

BASE_PACKAGES = [
    "php", "php-intl", "php-mbstring", "php-xml", "php-apcu", "php-curl",
    "php-sqlite3", "php-zip", "php-gd", "php-imagick", "php-ast",
    "php-mysql", "composer", "imagemagick", "librsvg2-bin", "diffutils",
    "git-review", "php-wikidiff2",
]

# Not in profiles/wmf-engineer.json's allow_domain (that profile is for the
# unrelated nono-based sandbox launcher) -- composer pulls a number of
# MediaWiki dependencies straight from GitHub, so this is a deliberate,
# MediaWiki-kit-specific addition rather than something sourced dynamically.
#
# All three are redundant on this machine: sbx's own `default-package-
# managers` and `default-code-and-containers` local policies already allow
# them (verified live, sbx/NOTES.md §24). They're declared anyway because
# those policies are source:local -- another machine, or one under a
# stricter org policy, could have a narrower set, and the kit would then
# silently stop being able to install its dependencies. A kit's domain list
# can only narrow what the local and org layers permit, never widen it, so
# this states a requirement rather than granting anything.
EXTRA_DOMAINS = [
    "github.com", "packagist.org", "registry.npmjs.org",
    # mw-install-browser (sbx/helpers/) resolves the current stable Chrome
    # release at the first host and downloads it from the second.
    "googlechromelabs.github.io", "storage.googleapis.com",
    # mw-install-cypress downloads the Cypress binary from the first host,
    # which redirects to the second. Unlike the entries above, no local
    # default allows these two (both gave 403), so this row is what lets
    # them through. Not *.cypress.io: at run time Cypress also calls
    # api.cypress.io and cloud.cypress.io, the tests pass without them,
    # and they would get run data (sbx/NOTES.md §91, §92).
    "download.cypress.io", "cdn.cypress.io",
]

# Where mw-install-browser links the Chrome it installs, and what the
# selenium harness reads. The same path Quibble uses.
CHROME_BIN = "/usr/bin/chromium"


def plugin_version(manifest_path=PLUGIN_MANIFEST):
    """The plugin's version, read from .claude-plugin/plugin.json. It is
    part of two paths Claude Code resolves (the cache directory and
    installed_plugins.json's installPath), so it has to come from the
    manifest rather than be repeated here."""
    with open(manifest_path, encoding="utf-8") as f:
        version = json.load(f).get("version")
    if not version:
        raise RuntimeError(f"{manifest_path} has no version")
    return str(version)


def plugin_skills(root=REPO_ROOT):
    """The skill names, read off the skills/ directory.

    Deliberately not package.json's `artifacts` list: keeping a separate
    list is how `standalone-vuln-audit` came to be shipped by the plugin
    and named by neither the pack manifest nor the session-start hook
    (sbx/NOTES.md §56.4). A directory cannot drift from itself."""
    skills_dir = os.path.join(root, "skills")
    try:
        names = os.listdir(skills_dir)
    except OSError:
        return []
    return sorted(
        name for name in names
        if os.path.isfile(os.path.join(skills_dir, name, "SKILL.md"))
    )


def _walk_plugin_dir(abs_dir, rel_to):
    """The files under abs_dir that belong in a sandbox, relative to
    rel_to. Hidden files are skipped -- except for the `.claude-plugin`
    directory itself, which is the manifest -- and so are `__pycache__`
    and editor droppings."""
    found = []
    for dirpath, dirnames, filenames in os.walk(abs_dir):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d != "__pycache__"
        )
        for name in filenames:
            if name.startswith(".") or name.endswith((".pyc", "~")):
                continue
            found.append(os.path.relpath(os.path.join(dirpath, name), rel_to))
    return found


def plugin_overlay_files(overlay_dir=None):
    """The overlay's files, as plugin-tree paths, sorted.

    The overlay holds plugin text that only an sbx sandbox reads, laid
    out under the path it needs inside the plugin tree. It exists so the
    kit can ship that text without a line of it in the shared tree --
    see sbx/DESIGN-testing-instructions.md §6.1."""
    if overlay_dir is None:
        overlay_dir = PLUGIN_OVERLAY_DIR
    if not os.path.isdir(overlay_dir):
        return []
    return sorted(_walk_plugin_dir(overlay_dir, overlay_dir))


def plugin_file_sources(root=REPO_ROOT, overlay_dir=None):
    """Every file of the plugin tree, as {plugin-tree path: source path}.

    The tree is markdown, JSON and one shell script -- no build step and
    no dependencies -- so shipping it is a copy. The overlay wins on a
    collision: a path it names is the sbx text for that path, and the
    repo's version is not shipped."""
    if overlay_dir is None:
        overlay_dir = PLUGIN_OVERLAY_DIR
    sources = {}
    for rel_dir in PLUGIN_TREE_DIRS:
        for rel in _walk_plugin_dir(os.path.join(root, rel_dir), root):
            sources[rel] = os.path.join(root, rel)
    for rel in PLUGIN_TREE_FILES:
        if os.path.isfile(os.path.join(root, rel)):
            sources[rel] = os.path.join(root, rel)
    for rel in plugin_overlay_files(overlay_dir):
        sources[rel] = os.path.join(overlay_dir, rel)
    return sources


def plugin_files(root=REPO_ROOT, overlay_dir=None):
    """Every file of the plugin tree, as paths relative to `root`, sorted.
    The overlay's paths are in here too, since they ship as part of the
    same tree."""
    return sorted(plugin_file_sources(root, overlay_dir))


def plugin_patches(patch_dir=None):
    """The patches to apply to the shipped plugin tree, in apply order.

    Sorted by name, which is why they are named `NN-short-name.patch`:
    two patches can touch the same file, and then the order is the one
    they were made in. See sbx/DESIGN-testing-instructions.md §6.2."""
    if patch_dir is None:
        patch_dir = PLUGIN_PATCH_DIR
    if not os.path.isdir(patch_dir):
        return []
    return [
        os.path.join(patch_dir, name)
        for name in sorted(os.listdir(patch_dir))
        if name.endswith(".patch")
    ]


def _patch_targets(patch_path):
    """The plugin-tree paths a patch touches, read out of its own diff
    headers. Raises if it creates or deletes a file: a patch corrects
    text that ships, and a new file belongs in the overlay."""
    targets = []
    with open(patch_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith(("new file mode", "deleted file mode")):
                raise RuntimeError(
                    f"{os.path.basename(patch_path)} creates or deletes a "
                    "file; put new plugin text in sbx/plugin-overlay/ instead"
                )
            if line.startswith("+++ "):
                target = line[4:].strip()
                if target == "/dev/null":
                    raise RuntimeError(
                        f"{os.path.basename(patch_path)} deletes a file; "
                        "a patch may only change text that ships"
                    )
                # `git diff` writes b/<path>; -p1 strips that one component.
                targets.append(target.split("/", 1)[1] if "/" in target else target)
    return targets


def apply_plugin_patches(tree_dir, shipped, patch_dir=None,
                         run=subprocess.run):
    """Applies every patch in patch_dir to the plugin tree staged in
    tree_dir. `shipped` is the set of paths that tree holds.

    A patch that no longer applies raises: that is the case where
    upstream rewrote the lines we correct, and a person has to decide
    whether the correction still stands. Shipping the file unpatched
    would hide it."""
    applied = []
    for patch_path in plugin_patches(patch_dir):
        name = os.path.basename(patch_path)
        for target in _patch_targets(patch_path):
            if target not in shipped:
                raise RuntimeError(
                    f"{name} patches {target}, which the plugin tree does "
                    "not ship; a patch may only change a shipped file"
                )
        result = run(
            ["git", "apply", "-p1", "--whitespace=nowarn", patch_path],
            cwd=tree_dir, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{name} does not apply to the plugin tree:\n"
                f"{(result.stderr or '').strip()}\n"
                "Refresh the patch against the current file, or drop it if "
                "upstream has made it unnecessary."
            )
        applied.append(name)
    return applied


def _now_iso():
    """The timestamp format Claude Code's own known_marketplaces.json uses
    -- ISO 8601, milliseconds, UTC, `Z` rather than `+00:00` (observed in
    a stock sandbox's claude-plugins-official entry)."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _expand_wiring(value, home, now):
    """$HOME / $NOW substitution, the same two variables the nono pack's
    `json_merge` directives expand. Recurses into dicts and lists so the
    wiring JSON stays the single source of truth for these files' shape."""
    if isinstance(value, str):
        return value.replace("$HOME", home).replace("$NOW", now)
    if isinstance(value, dict):
        return {k: _expand_wiring(v, home, now) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_wiring(v, home, now) for v in value]
    return value


def _read_wiring(name, wiring_dir=WIRING_DIR):
    with open(os.path.join(wiring_dir, name), encoding="utf-8") as f:
        return json.load(f)


def plugin_state_files(home=None, now=None, wiring_dir=WIRING_DIR, version=None):
    """{relative path under $HOME: JSON content} for the three files that
    tell Claude Code the plugin is installed -- the marketplace manifest
    and the two plugin state files.

    Content comes from wiring/, expanded, rather than being rewritten
    here: the host pack and the kit then cannot disagree about a format
    neither of them owns."""
    if home is None:
        home = setup_mod.SANDBOX_HOME
    if now is None:
        now = _now_iso()
    if version is None:
        version = plugin_version()
    marketplace = os.path.join(
        ".claude", "plugins", "marketplaces", MARKETPLACE_NAME,
        ".claude-plugin", "marketplace.json",
    )
    installed = _expand_wiring(
        _read_wiring("installed-plugin.json", wiring_dir), home, now
    )
    check_state_version(installed, version)
    return {
        marketplace: _read_wiring("marketplace.json", wiring_dir),
        os.path.join(".claude", "plugins", "known_marketplaces.json"):
            _expand_wiring(_read_wiring("known-marketplaces.json", wiring_dir), home, now),
        os.path.join(".claude", "plugins", "installed_plugins.json"): installed,
    }


def check_state_version(installed, version):
    """wiring/installed-plugin.json spells the version out twice (in
    `version` and at the end of `installPath`) and has no $VERSION to
    expand, while the tree is copied to a directory named by
    .claude-plugin/plugin.json. Bumping the manifest without the wiring
    would put the plugin in a cache directory installed_plugins.json does
    not name -- so say so here rather than ship a kit whose startup check
    fails inside the sandbox."""
    for entry in (installed.get("plugins") or {}).get(
        f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", []
    ):
        stated = entry.get("version")
        install_path = entry.get("installPath") or ""
        if stated != version or os.path.basename(install_path) != version:
            raise RuntimeError(
                f"wiring/installed-plugin.json names version {stated!r} at "
                f"{install_path!r}, but .claude-plugin/plugin.json says "
                f"{version!r}; update the wiring to match"
            )


def settings_patch(wiring_dir=WIRING_DIR):
    """What `wmf-sbx-setup --settings` merges into the sandbox's
    ~/.claude/settings.json: `enabledPlugins`, plus the permission denies
    ported from wiring/settings-merge.json.

    Only `deny` is ported. `permissions.allow` is redundant under sbx's
    `defaultMode: bypassPermissions`, `ask` likewise, and
    `sandbox: {enabled: false}` turns off *nono's* in-process sandbox and
    means nothing here. Deny rules are honoured in bypass mode -- measured,
    sbx/DESIGN-plugin-integration.md §6 Q5 -- so the denies buy something
    real; the rest would be noise in a file the engineer may well read.

    One trap that finding also turned up: a single-slash absolute path
    pattern matches nothing, so a filesystem-absolute `Read`/`Edit` rule
    needs `//`. The nono list uses that form today (`Read(//**/*.pem)`,
    `Read(//**/.env*)`, ...), and those rules apply in the sandbox too: the
    agent cannot Read or Edit `<core>/.env` through its tools.
    wmf-sbx-setup writes that file as root, not through a tool, so setup
    is not affected. `Bash(/usr/bin/security:*)` is a command rule, not a
    path, and its single slash is correct.
    """
    patch = dict(_read_wiring("enabled-plugin.json", wiring_dir))
    deny = list(
        (_read_wiring("settings-merge.json", wiring_dir).get("permissions") or {})
        .get("deny") or []
    )
    patch["permissions"] = {"deny": deny}
    return patch


def claude_json_patch():
    """The one key the kit sets in ~/.claude.json.

    Claude Code 2.1.269 asks, on the first start of a fresh install that
    is in bypass mode, "Make auto mode your default permission mode?" --
    with "No, keep bypass permissions" as the second option, so it is a
    one-time nudge and not, as it first looked, something suppressing the
    `defaultMode: bypassPermissions` sbx sets (MEASURED, NOTES.md §72.3).
    It records the answer as `hasSeenAutoDefaultNudge` in ~/.claude.json,
    which is per-*install* state -- and every new sandbox is a new install,
    so without this the engineer answers it once per sandbox, before the
    session they created it for can start.

    Seeding it changes no permission: bypass is what the sandbox already
    runs in and what the nudge's own "no" keeps. Drop this entry to have
    the prompt back.
    """
    return {"hasSeenAutoDefaultNudge": True}


def mediawiki_env_vars(resolved):
    """What the MediaWiki test harnesses read out of the environment,
    taken from the host's own mediawiki/core checkout (its composer.json
    mw-install:sqlite script is the authority -- see
    sbx/DESIGN-setup-steps.md §4.2).

    wmf-sbx-setup writes the same values into <core>/.env, but that file is
    read by `docker compose` and nothing else: core's Gruntfile and
    wdio-defaults read process.env.MW_SERVER / MW_SCRIPT_PATH directly and
    throw when they're unset. These variables are what actually makes
    `npm run selenium-test` and `grunt qunit` work here (§4.1).

    Empty for a sandbox with no core.

    MW_SCRIPT_PATH is `/`, not the install's own empty --scriptpath=. The
    empty string is legitimate for a docroot install, and core's Gruntfile
    says so, but wdio-mediawiki's own guard rejects it and stops every
    selenium run before the first test. Quibble exports `/` for the same
    reason. See sbx/DESIGN-testing-instructions.md §2.6.

    CHROME_BIN names the path mw-install-browser links, which is the path
    Quibble uses too. It is set even before that helper has run: a
    variable naming a browser that is not installed yet costs nothing, and
    a missing variable makes the selenium harness fail with a worse
    message.

    MEDIAWIKI_USER / MEDIAWIKI_PASSWORD are what selenium logs in with,
    and they repeat what the install created. The wiki is local to this
    sandbox, so the password is not a secret.

    API_TESTING_CONFIG_FILE is absolute, so a test run started in an
    extension finds the config that wmf-sbx-setup writes into core
    (§5.5).
    """
    core_dir = next(
        (path for canonical, path in resolved if canonical == CORE_CANONICAL), None
    )
    if core_dir is None:
        return {}
    params = setup_mod.parse_install_params(core_dir)
    return {
        "MW_SERVER": params.get("server", ""),
        "MW_SCRIPT_PATH": params.get("scriptPath") or "/",
        "CHROME_BIN": CHROME_BIN,
        "MEDIAWIKI_USER": params.get("adminUser", ""),
        "MEDIAWIKI_PASSWORD": params.get("adminPassword", ""),
        "API_TESTING_CONFIG_FILE": os.path.join(
            core_dir, setup_mod.API_TESTING_CONFIG_NAME),
    }


def parsoid_env_vars(resolved, readonly_dirs=None):
    """MEDIAWIKI_HAS_INTEGRATION_TESTS=1 when the wiki loads a Parsoid
    clone, else nothing.

    wmf-sbx-setup points LocalSettings.php at a writable Parsoid clone
    (see setup.PARSOID_CANONICAL), so the wiki uses the clone and not
    core's vendor/wikimedia/parsoid. But core's tests/phpunit/bootstrap.php
    does not load LocalSettings.php when it thinks a run holds unit tests
    only (a path under /unit/, or --testsuite=core:unit). Those runs then
    use the vendor copy, and a core or extension unit test that needs a
    new Parsoid feature fails. The variable turns that guess off, as
    https://www.mediawiki.org/wiki/Parsoid says to do for a Parsoid
    developer checkout.

    Not for a :ro Parsoid bind mount, which setup does not wire in, and
    not without core, which has no bootstrap to read it.
    """
    readonly_dirs = set(readonly_dirs or [])
    canonicals = {canonical for canonical, _path in resolved}
    if CORE_CANONICAL not in canonicals:
        return {}
    if not any(canonical == setup_mod.PARSOID_CANONICAL and path not in readonly_dirs
               for canonical, path in resolved):
        return {}
    return {"MEDIAWIKI_HAS_INTEGRATION_TESTS": "1"}


def build_plan(resolved, host_home=None, daemon_port=None, readonly_dirs=None,
               links=None, primary=None, upstreams=None, requested=None,
               reset_all=False):
    """The plan wmf-sbx-setup runs from, as a plain dict.

    resolved: [(canonical_or_None, local_dir), ...], in command-line order
    (the first is the primary unless `primary` says otherwise).
    links: {local_dir: (link_name, link_dir)} for the repos that should be
    symlinked into the core clone -- computed host-side, where the parsed
    manifests are (see sbx/DESIGN-dependency-walk.md §1 on why the link
    name is not the manifest's `name` field).
    upstreams: {local_dir: anonymous_clone_url} -- the repo's real upstream
    (Gerrit/GitLab over https), which becomes the clone's `origin` while
    the host mirror is renamed to `local`. Computed host-side because the
    canonical -> URL mapping lives in wmf_sbx_create.clone_url, which the
    setup script can't import. Optional per repo: without it the clone
    keeps `origin` pointing at the host mirror, which is what the whole
    field exists to stop being the default.
    requested: the subset of local_dirs the engineer named on the command
    line, as opposed to the ones the dependency walk found. Those keep the
    host's branch instead of being reset to upstream master -- see
    wmf_sbx_setup.repos_to_leave_alone. reset_all turns that off wholesale.
    """
    if host_home is None:
        # realpath, to match the realpath'd repo directories this is used
        # to take relative paths against -- see create.main's host_home.
        host_home = os.path.realpath(os.path.expanduser("~"))
    if daemon_port is None:
        daemon_port = DEFAULT_DAEMON_PORT
    readonly_dirs = set(readonly_dirs or [])
    links = links or {}
    upstreams = upstreams or {}
    requested = set(requested or [])
    repos = []
    for canonical, path in resolved:
        link_name, link_dir = links.get(path, (None, None))
        repos.append({
            "path": path,
            "canonical": canonical,
            "readOnly": path in readonly_dirs,
            "linkName": link_name,
            "linkDir": link_dir,
            "upstreamUrl": upstreams.get(path),
            "requested": path in requested,
        })
    if primary is None and repos:
        primary = repos[0]["path"]
    return {
        "version": PLAN_VERSION,
        "hostHome": host_home,
        "daemonPort": str(daemon_port),
        "primary": primary,
        "resetAll": bool(reset_all),
        "repos": repos,
    }


def plugin_tree_dests(version=None):
    """The two places under $HOME the plugin tree has to appear, relative
    to files/home.

    On the host, package.json's wiring makes both of these symlinks to the
    one checkout. A kit ships a directory of files, so here they are two
    copies of a few hundred KB of markdown -- which also means the kit has
    no link back to a host path that would not exist in the sandbox.

    Both are needed and neither is redundant: the marketplace copy is what
    `./plugins/wmf-claude` in marketplace.json resolves against (so
    `claude plugin list` can describe the plugin at all), and the cache
    copy is installed_plugins.json's installPath, which is what
    ${CLAUDE_PLUGIN_ROOT} ends up pointing at when a skill or the
    SessionStart hook actually runs.
    """
    if version is None:
        version = plugin_version()
    return (
        os.path.join(".claude", "plugins", "marketplaces", MARKETPLACE_NAME,
                     "plugins", PLUGIN_NAME),
        os.path.join(".claude", "plugins", "cache", MARKETPLACE_NAME,
                     PLUGIN_NAME, version),
    )


def write_plugin_tree(files_home, root=REPO_ROOT, version=None,
                      overlay_dir=None, patch_dir=None):
    """Copies the plugin tree into both of plugin_tree_dests() under
    files_home, and writes the marketplace manifest and the two Claude Code
    state files. Returns the list of paths written, relative to files_home.

    See sbx/DESIGN-plugin-integration.md §5 step 3: ~/.claude/skills is an
    sbx virtiofs mount, so a kit cannot drop skills there; ~/.claude/plugins
    is an ordinary directory, so the marketplace route is the way in."""
    if version is None:
        version = plugin_version()
    written = []
    sources = plugin_file_sources(root, overlay_dir)
    # Stage the tree once and patch it there, not once per destination:
    # the two copies are then identical by construction. The staging
    # directory is outside any git work tree on purpose -- `git apply`
    # inside one applies relative to that repo's root, not to cwd.
    staged = tempfile.mkdtemp(prefix="wmf-sbx-plugin-")
    try:
        for rel, source in sources.items():
            dest = os.path.join(staged, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy(source, dest)
        apply_plugin_patches(staged, set(sources), patch_dir)
        for dest_rel in plugin_tree_dests(version):
            for rel in sorted(sources):
                dest = os.path.join(files_home, dest_rel, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy(os.path.join(staged, rel), dest)
                # copy() takes the mode too, but the repo's own bit is not
                # something to depend on for a file the hook must be able to
                # exec -- say it.
                os.chmod(dest, 0o755 if rel in PLUGIN_EXECUTABLES else 0o644)
                written.append(os.path.join(dest_rel, rel))
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    for rel, content in plugin_state_files(version=version).items():
        dest = os.path.join(files_home, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(content, f, indent=2, sort_keys=False)
            f.write("\n")
        written.append(rel)
    return written


