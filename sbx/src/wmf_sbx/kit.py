#!/usr/bin/env python3
"""Generates a Docker Sandboxes ("sbx") --kit spec.yaml for MediaWiki
development, so wmf-sbx-create doesn't require every user to hand-maintain
their own kit directory (see sbx/DESIGN-kit-generation.md).

Three additive layers:
  1. BASE_PACKAGES / EXTRA_DOMAINS / REPO_ENVIRONMENT_VARS below -- the
     parts every WMF engineer doing MediaWiki dev needs, upstreamed here
     so they don't drift per-user.
  2. Wiki-family and named Wikimedia network domains pulled dynamically
     from profiles/wmf-engineer.json's network.allow_domain (wiki_family_domains)
     -- one source of truth instead of a second hand-copied list that can
     go stale (see sbx/NOTES.md #8/#14).
  3. Per-user extras from ~/.config/wmf-sbx/repos.yaml's
     extra_environment / extra_packages keys -- e.g. a personal GERRITUA
     header, or an editor someone likes available in every sandbox.

A final `commands.install` step runs the static wmf-sbx-setup script (see
wmf_sbx/setup.py) -- copied verbatim by write_kit_dir into the kit's
files/home/ directory, so it's present at /home/agent/wmf-sbx-setup once
the sandbox boots -- passing it HOST_HOME, a daemon port, and every repo
directory so it can clone each one into a private, writable checkout under
the sandbox's own $HOME, lock the sbx-mounted original down to read-only,
and start a git daemon covering the whole parallel tree so it's fetchable
from the host. See sbx/DESIGN-parallel-clone-tree.md.
"""

import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
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
DEFAULT_PROFILE_PATH = os.path.join(REPO_ROOT, "profiles", "wmf-engineer.json")

# Copied verbatim (dropping the .py) into every generated kit's
# files/home/wmf-sbx-setup -- see write_kit_dir. setup.py is a sibling in
# this same package directory.
STATIC_SETUP_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                   "setup.py")

# Where the kit's static files/home/ tree lands once the sandbox boots --
# see the Docker Sandboxes kit file-injection docs (files/home/... maps to
# the sandbox's actual $HOME).
SANDBOX_SETUP_SCRIPT = "/home/agent/wmf-sbx-setup"

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

# The edits that `wmf-sbx-setup --claude-md` applies to sbx's own
# CLAUDE.md at every start, and the sbx text they were written against.
# `wmf-sbx refresh-claude-md` updates the snapshot (sbx/NOTES.md §95).
CLAUDE_MD_PATCH_DIR = os.path.join(SBX_ROOT, "patches", "sbx-claude-md")
CLAUDE_MD_EDITS_SOURCE = os.path.join(CLAUDE_MD_PATCH_DIR, "edits.json")
CLAUDE_MD_UPSTREAM_SNAPSHOT = os.path.join(CLAUDE_MD_PATCH_DIR, "upstream.md")
SANDBOX_CLAUDE_MD_EDITS = setup_mod.SANDBOX_CLAUDE_MD_EDITS

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

SANDBOX_CLAUDE_DIR = os.path.join(setup_mod.SANDBOX_HOME, ".claude")
SANDBOX_PLUGINS_DIR = os.path.join(SANDBOX_CLAUDE_DIR, "plugins")

# The settings.json patch, shipped as data and applied by
# `wmf-sbx-setup --settings`. It cannot be a files/home/ drop: sbx writes
# its own settings.json (permissions.defaultMode, model, theme), and a
# static file would replace it wholesale.
SANDBOX_SETTINGS_PATCH = os.path.join(SANDBOX_CLAUDE_DIR, "wmf-sbx-settings.json")
SANDBOX_SETTINGS_FILE = os.path.join(SANDBOX_CLAUDE_DIR, "settings.json")

# A second patch, for ~/.claude.json rather than settings.json -- same
# mechanism (`wmf-sbx-setup --settings PATCH TARGET`), different file,
# because the one key in it is Claude Code's own per-install state and not
# a setting. See claude_json_patch() and NOTES.md §72.3.
SANDBOX_CLAUDE_JSON_PATCH = os.path.join(
    SANDBOX_CLAUDE_DIR, "wmf-sbx-claude-json.json"
)

# The one deny the nono list has no need for. sbx gives every sandbox's
# agent the MCP gateway's meta-tools, two of which (`mcp-add`,
# `code-mode`) let it mount arbitrary MCP servers and run JavaScript on
# its own initiative. The whole-server form removes all of them from the
# model's view while the gateway itself stays connected -- and does not
# impede wmf-sbx-mcp-proxy's own HTTP calls, which are not tool calls.
# MEASURED, sbx/NOTES.md §60.4.
GATEWAY_MCP_DENY = "mcp__mcp-gateway"

# --- MCP: the proxy, and what each server is allowed to offer -------------
#
# sbx/DESIGN-plugin-integration.md §4, Route 1. The real servers run on
# the *host*, behind sbx's MCP gateway, so their credentials never enter
# the sandbox (§60.1 -- the criterion that decided the route). What the
# kit ships is one stdio shim per server, named after the server, so the
# tools keep the literal names skills/ and agents/ already write.
MCP_PROXY = "wmf-sbx-mcp-proxy"
MCP_PROXY_SOURCE = os.path.join(SBX_ROOT, "helpers", MCP_PROXY)

# ~/.local/bin is first on the agent's PATH (MEASURED in a live sandbox),
# and unlike /usr/local/bin it needs no root to write, so the kit can drop
# the proxy straight into files/home/.
SANDBOX_LOCAL_BIN = os.path.join(setup_mod.SANDBOX_HOME, ".local", "bin")

# The registration data, shipped beside the settings patch and applied by
# `wmf-sbx-setup --mcp`. Same reasoning as SANDBOX_SETTINGS_PATCH: the
# file the entries go into (~/.claude.json) is Claude Code's, holds a lot
# else, and already has sbx's own `mcp-gateway` entry in it -- so this is
# a per-entry reconcile through `claude mcp add`, not a file we write.
SANDBOX_MCP_FILE = os.path.join(SANDBOX_CLAUDE_DIR, "wmf-sbx-mcp.json")

# name -> the tools that server may offer this sandbox.
#
# `--tools` is REQUIRED by the proxy, not a convenience: the gateway
# serves every mounted server's tools in one flat namespace with no
# attribution of any kind (MEASURED, sbx/NOTES.md §62.2), so with both
# servers mounted "everything the gateway serves" would have made
# `mcp__gerrit__phabricator_get_task` a working tool name. The allowlist
# is the only thing that says which tools are whose -- and it doubles as
# the policy statement of what this sandbox may reach.
#
# The two lists are asymmetric on purpose:
#
# - phabricator's *entire* tool surface is read-only, against a public
#   tracker, so all four are here. `phabricator_get_task_comments`
#   matters in particular -- agents/mediawiki-dev.md asks for the task
#   body *and* its comments, "where the bug's real shape becomes clear".
# - gerrit serves 20 tools of which **15 write to Gerrit under the
#   engineer's own credential** (`abandon_change`, `post_review_comment`,
#   `create_change`, ...). So gerrit gets an explicit short list: the
#   five read-only tools skills/ and the gerrit-reviewer agent actually
#   name. Its other read-only tools (`query_changes`, `get_most_recent_cl`,
#   ...) are omitted for now -- adding one is a line here, on purpose,
#   because on this server the default has to be "no".
#
# test_wmf_sbx_kit.py asserts every `mcp__<server>__<tool>` the plugin
# tree names is in the matching list, so a skill cannot start naming a
# tool this sandbox silently does not serve.
MCP_SERVER_TOOLS = {
    "phabricator": [
        "phabricator_get_task",
        "phabricator_get_task_comments",
        "phabricator_search_tasks",
        "phabricator_get_project",
    ],
    "gerrit": [
        "get_change_details",
        "get_commit_message",
        "get_file_diff",
        "list_change_comments",
        "list_change_files",
    ],
}

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

# The label attached to that port. Purely cosmetic -- it shows up in
# `sbx ports NAME` output -- but a named port is much easier to recognise
# there than a bare number.
DAEMON_PORT_NAME = "git-daemon"

KIT_NAME = "mediawiki-kit"
KIT_DISPLAY_NAME = "MediaWiki Kit"
KIT_DESCRIPTION = "Docker sandbox kit with MediaWiki dependencies"

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

# The Wikimedia hosts the kit takes from profiles/wmf-engineer.json's
# allow_domain. Every wiki family contains "wik" (wikipedia, wikidata, ...,
# wiktionary, mediawiki) -- see sbx/DESIGN-kit-generation.md for why that is
# more robust than listing each TLD out. The profile names each
# *.wikimedia.org host instead of a wildcard, and phab.wmfusercontent.org
# serves Phabricator files, so both suffixes count too. A plain entry and an
# endpoint-scoped {domain, endpoints} entry count the same: sbx has no
# method or path rules, so a host that nono keeps read-only is fully open
# here (sbx/SECURITY.md §1).
_WIKI_DOMAIN_RE = re.compile(
    r"^(?:\*\.[\w-]*wik[\w-]*\.\w+"
    r"|(?:\*\.)?[\w.-]+\.(?:wikimedia|wmfusercontent)\.org)$"
)

# Used only if profiles/wmf-engineer.json can't be read, or yields no
# Wikimedia host, so kit generation degrades gracefully instead of leaving
# the sandbox with no wikis and no Gerrit.
_FALLBACK_WIKI_DOMAINS = frozenset([
    "*.wikipedia.org", "*.wikivoyage.org", "*.wikibooks.org", "*.wikiquote.org",
    "*.wikidata.org", "*.wikifunctions.org", "*.wiktionary.org", "*.wikinews.org",
    "*.wikiversity.org", "*.wikisource.org", "*.mediawiki.org",
    "gerrit.wikimedia.org", "phabricator.wikimedia.org", "gitlab.wikimedia.org",
    "commons.wikimedia.org", "meta.wikimedia.org", "upload.wikimedia.org",
    "doc.wikimedia.org", "integration.wikimedia.org", "phab.wmfusercontent.org",
])


def _allow_domain_host(entry):
    """The host of one allow_domain entry: the string itself, or the
    `domain` of an endpoint-scoped object. None for anything else."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("domain"), str):
        return entry["domain"]
    return None


def wiki_family_domains(profile_path=DEFAULT_PROFILE_PATH):
    """Pull the Wikimedia hosts out of the nono profile's
    network.allow_domain list: the wiki-family wildcards, and every named
    *.wikimedia.org and *.wmfusercontent.org host. Endpoint-scoped entries
    give their `domain`; sbx kit specs have no endpoint rules, so the host
    is allowed whole. Other hosts (doc sites, codesearch, the Anthropic
    API) are not MediaWiki-specific and are left out."""
    try:
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)
    except (OSError, ValueError) as e:
        print(
            f"warning: could not read {profile_path} ({e}); falling back to a "
            "static wiki-family domain list, which may be stale",
            file=sys.stderr,
        )
        return sorted(_FALLBACK_WIKI_DOMAINS)
    allow_domain = profile.get("network", {}).get("allow_domain", [])
    hosts = {_allow_domain_host(d) for d in allow_domain}
    found = sorted(h for h in hosts if h and _WIKI_DOMAIN_RE.match(h))
    if not found:
        print(
            f"warning: {profile_path} lists no Wikimedia host in "
            "network.allow_domain; falling back to a static wiki-family "
            "domain list, which may be stale",
            file=sys.stderr,
        )
        return sorted(_FALLBACK_WIKI_DOMAINS)
    return found


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
    if GATEWAY_MCP_DENY not in deny:
        deny.append(GATEWAY_MCP_DENY)
    patch["permissions"] = {"deny": deny}
    return patch


def settings_merge_argv():
    return ["python3", SANDBOX_SETUP_SCRIPT, "--settings", SANDBOX_SETTINGS_PATCH]


def settings_merge_install_step():
    """The install-time half. `setup.install` takes `command` as a shell
    string (the package step above is one), unlike `setup.startup`, which
    takes an argv array -- so the same command is spelled twice rather
    than shared, and settings_merge_argv is what keeps the two honest."""
    return {"command": " ".join(shlex.quote(a) for a in settings_merge_argv()) + "\n"}


def settings_merge_startup_command():
    """The every-start half. Runs as the agent, whose settings.json it is;
    the install-time run is root (setup.install's default) and chowns."""
    return {
        "description": "merge the wmf-claude plugin and permission denies into settings.json",
        "user": "1000",
        "command": settings_merge_argv(),
    }


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


def claude_json_argv():
    """`--settings` takes an optional target, which is the whole reason
    this needs no new code in wmf-sbx-setup: same deep merge, same
    idempotence (a second run writes nothing at all), other file."""
    return [
        "python3", SANDBOX_SETUP_SCRIPT, "--settings", SANDBOX_CLAUDE_JSON_PATCH,
        setup_mod.SANDBOX_CLAUDE_JSON,
    ]


def claude_json_install_step():
    """Install-time half -- a shell string, like its neighbours."""
    return {"command": " ".join(shlex.quote(a) for a in claude_json_argv()) + "\n"}


def claude_json_startup_command():
    """Every-start half, for the same reason as the settings merge (§46):
    install alone loses if anything rewrites ~/.claude.json afterwards.
    Runs as the agent, whose file it is.

    The rewrite risk cuts both ways -- Claude Code writes this file while
    it runs, and a merge reads it whole and writes it back -- but the
    merge writes *only* when the key is missing, so the window is the
    first start after a create and not every start."""
    return {
        "description": "seed the one ~/.claude.json key the kit owns",
        "user": "1000",
        "command": claude_json_argv(),
    }


def claude_md_startup_command():
    """The `setup.startup` entry that edits the wrong sections out of the
    CLAUDE.md sbx writes (sbx/NOTES.md §95).

    Startup only, not install: sbx writes that file after the install
    steps and before the startup steps (MEASURED, §95), so at install time
    there is nothing to edit. It does not rewrite the file on a restart,
    and the step is idempotent, so the later starts cost one read. Root,
    because the file is in the parent directory of the primary workspace,
    and because the status file is in /var/log. The host runs the same
    command after a create or a start, and reports what it says
    (create.amend_workspace_claude_md): this step's output goes only to
    /var/log/sbx-kit-startup.log."""
    return {
        "description": "edit the sections of sbx's CLAUDE.md that are wrong here",
        "user": "0",
        "command": ["python3", SANDBOX_SETUP_SCRIPT, "--claude-md",
                    SANDBOX_CLAUDE_MD_EDITS],
    }


def exec_bit_paths(version=None, mcp=True):
    """Every file this kit ships that something has to *run*, as absolute
    sandbox paths -- the argument list for `wmf-sbx-setup --exec-bits`.

    write_kit_dir chmods all of these 0755 in the staging tree and the bit
    does not reach the sandbox (sbx/NOTES.md §71), so the mode gets said
    again from inside, where we own the filesystem.

    The git helpers are deliberately absent: install_helper_scripts copies
    them onto PATH with `install -m 0755`, which states the mode already,
    and the ~/bin copies it leaves behind are never executed.
    """
    paths = [
        os.path.join(setup_mod.SANDBOX_HOME, dest_rel, rel)
        for dest_rel in plugin_tree_dests(version)
        for rel in sorted(PLUGIN_EXECUTABLES)
    ]
    if mcp:
        # Claude Code spawns this one as a bare command, with no
        # interpreter in front of it -- see mcp_config below.
        paths.append(os.path.join(SANDBOX_LOCAL_BIN, MCP_PROXY))
    return paths


def exec_bits_argv(version=None, mcp=True):
    return (["python3", SANDBOX_SETUP_SCRIPT, "--exec-bits"]
            + exec_bit_paths(version, mcp))


def exec_bits_install_step(version=None, mcp=True):
    """The install-time half -- a shell string, like its neighbours. This
    is the one that matters: it runs before the agent can start a
    session."""
    return {"command": " ".join(shlex.quote(a)
                                for a in exec_bits_argv(version, mcp)) + "\n"}


def exec_bits_startup_command(version=None, mcp=True):
    """The every-start half, for the same reason settings_merge has one:
    whether sbx re-drops `files/home` on a later container start is not
    something we have measured, and a chmod of three paths is cheap enough
    to not need the answer.

    Root (setup.startup's default), not the agent: chmod needs ownership,
    and the drop's ownership is sbx's business, not ours. Startup steps do
    not block the `sbx exec` that triggered the start (§46), so this half
    can lose a race with the first session -- which is why the install
    half is not optional."""
    return {
        "description": "restore the executable bit on the kit's programs",
        "command": exec_bits_argv(version, mcp),
    }


def mcp_config(servers=None):
    """{server name: {command, args}} for `wmf-sbx-setup --mcp`, one entry
    per host-side server this sandbox may reach.

    `servers` narrows MCP_SERVER_TOOLS to what is actually registered on
    the host -- wmf-sbx-create discovers that and passes it in. A server
    with no host registration must get no proxy entry: the entry would
    load, find nothing behind it, and show up as a failed MCP server in
    every session.

    `--no-add` because sandboxes are created `--static-mcp`, where the
    servers are already mounted and `mcp-add` is not there to call
    (sbx/NOTES.md §61.3). It is also correct in dynamic mode on every
    start after the first, since a mount is sandbox state and outlives
    the gateway session (§62.3).
    """
    if servers is None:
        servers = sorted(MCP_SERVER_TOOLS)
    config = {}
    for name in servers:
        tools = MCP_SERVER_TOOLS.get(name)
        if not tools:
            # Fail closed rather than register a proxy with no allowlist:
            # the proxy would serve nothing anyway, and an entry that
            # exists and offers nothing is harder to diagnose than one
            # that was never made.
            continue
        config[name] = {
            "command": os.path.join(SANDBOX_LOCAL_BIN, MCP_PROXY),
            "args": [name, "--tools", ",".join(tools), "--no-add"],
        }
    return config


def mcp_register_argv():
    return ["python3", SANDBOX_SETUP_SCRIPT, "--mcp", SANDBOX_MCP_FILE]


def mcp_register_install_step():
    """Install-time half -- a shell string, like its neighbours."""
    return {"command": " ".join(shlex.quote(a) for a in mcp_register_argv()) + "\n"}


def mcp_register_startup_command():
    """Every-start half. Same install-and-startup argument as the settings
    merge: startup alone loses the first session (§46), install alone
    loses if anything rewrites ~/.claude.json later. `claude mcp add`
    refuses an existing name (exit 1, MEASURED), so the step reconciles
    -- it compares what is registered and only touches an entry that is
    missing or out of date."""
    return {
        "description": "register the host-side MCP servers' in-sandbox proxy entries",
        "user": "1000",
        "command": mcp_register_argv(),
    }


def plugin_check_startup_command():
    """A `setup.startup` step that asserts Claude Code actually loaded the
    plugin, and says so loudly in /var/log/sbx-kit-startup.log when it did
    not.

    `installed_plugins.json` and `known_marketplaces.json` are internal
    state files with no format guarantee, so the kit writes them and then
    checks the result through the only supported reader there is:
    `claude plugin list`, grepped for the plugin name. That exits 0 even
    when nothing is installed, so the grep is the test, not the exit
    status.

    Runs as the agent (user 1000), not root: `claude` is installed at
    ~/.local/bin/claude, and the state files it reads are under the
    agent's own $HOME. Never fails the start -- a sandbox with no plugin
    is still a working sandbox, and `wmf-sbx-create`'s
    report_setup_problems is what surfaces the line.

    Names that binary absolutely, with `command -v` as the fallback, for
    the same reason claude_executable() does (§69.3): a bare `claude` is
    not on every PATH this step might run under, and the first version of
    this check piped `2>&1` straight into `grep -q`, so "claude: not
    found" and "the plugin is missing" were the same observation. It now
    distinguishes the two and quotes what it actually saw -- an error that
    names a cause it did not measure is worse than no error (§71.2).
    """
    check = f"""\
c={shlex.quote(setup_mod.SANDBOX_CLAUDE_BIN)}
[ -x "$c" ] || c=$(command -v claude || true)
if [ -z "$c" ]; then
  echo "error: no claude executable here ({setup_mod.SANDBOX_CLAUDE_BIN} \
is not executable and none is on PATH), so whether the {PLUGIN_NAME} \
plugin loaded is unknown." >&2
  exit 0
fi
out=$("$c" plugin list 2>&1); rc=$?
brief=$(printf '%s' "$out" | tr '\\n' ' ' | cut -c1-300)
if [ "$rc" -ne 0 ]; then
  echo "error: '$c plugin list' failed (exit $rc), so whether the \
{PLUGIN_NAME} plugin loaded is unknown. It said: $brief" >&2
else
  case "$out" in
    *{PLUGIN_NAME}*) ;;
    *)
      echo "error: the {PLUGIN_NAME} plugin did not load; '$c plugin list' \
does not name it. The kit wrote {SANDBOX_PLUGINS_DIR}/installed_plugins.json, \
which is Claude Code internal state -- its format may have changed. \
It listed: $brief" >&2
      ;;
  esac
fi
"""
    return {
        "description": f"check that the {PLUGIN_NAME} plugin loaded",
        "user": "1000",
        "command": ["sh", "-c", check],
    }


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


def build_kit_spec(resolved, extra_environment=None, extra_packages=None,
                    profile_path=DEFAULT_PROFILE_PATH, host_home=None,
                    readonly_dirs=None, daemon_port=None, plan=None,
                    mcp_servers=None):
    """resolved: [(canonical_or_None, local_dir), ...] -- every repo
    argument in this sandbox invocation, in order. host_home defaults to
    this (host-side) process's own $HOME -- see wmf_sbx/setup.py's
    parallel_path for what it's used for. readonly_dirs is the subset of
    those local_dirs the user opted out of a writable clone for (see
    wmf_sbx_create.split_ro_suffix) -- passed on to wmf-sbx-setup using the
    same ':ro' suffix convention, since it's the one that actually acts on
    it. daemon_port is the port wmf-sbx-setup starts its git daemon on
    (see sbx/DESIGN-parallel-clone-tree.md §3); it goes into the spec's
    top-level `ports:`, so sbx publishes it to some host port on every
    container start and callers only have to look that port up (see
    wmf_sbx_create.ensure_published_host_port). Defaults to
    DEFAULT_DAEMON_PORT if not given. mcp_servers is the subset of
    MCP_SERVER_TOOLS actually registered on this host (wmf-sbx-create
    finds out); an empty list means no proxy entries and no MCP steps at
    all. Returns a plain dict ready for yaml.safe_dump as a kit
    spec.yaml."""
    if host_home is None:
        # realpath, to match the realpath'd repo directories this is used
        # to take relative paths against -- see create.main's host_home.
        host_home = os.path.realpath(os.path.expanduser("~"))

    # Tells the wmf-claude plugin's SessionStart hook which sandbox it is
    # running in, so it describes *this* backend rather than nono's (the
    # hook's default). Inert until the plugin itself is shipped in the kit
    # -- see sbx/DESIGN-plugin-integration.md §3 -- but it costs one
    # variable to set now and means the hook is never wrong once it lands.
    # WMF_CLAUDE_DOCKER_MODE is deliberately left unset: it defaults to
    # `none`, which is correct here, since the kit installs PHP into the
    # container itself rather than reaching a second one through mwdocker.
    variables = {"WMF_CLAUDE_SANDBOX_BACKEND": "sbx"}
    for canonical, path in resolved:
        if canonical is None:
            continue
        # The *literal* host path, which is also the agent's own working
        # directory and -- since §39 -- the writable clone: wmf-sbx-setup
        # moves sbx's mirror aside to ~/.sbx-originals/<rel>, clones into
        # /home/agent/<rel>, and bind-mounts that clone back over the path
        # the mirror vacated. One directory, two paths.
        #
        # It used to be the /home/agent one, for a reason that has since
        # been fixed elsewhere: §39 picked it because the alias does not
        # survive `sbx stop` while /home/agent/<rel> always does, and §40's
        # `--restore` startup pass now rebuilds the alias on every
        # container start. With both paths live and naming one directory,
        # $IP should be the one the agent and the engineer both use.
        #
        # wmf-sbx-setup agrees: since §63.1 it runs composer, npm and the
        # sqlite install with this same cwd, so what they bake into
        # LocalSettings.php, .env and vendor/ names it too. See
        # wmf_sbx.setup.work_path.
        #
        # A repo outside host_home has no parallel path at all and always
        # kept its literal one, so that case is unchanged.
        #
        # What this does re-expose is SECURITY.md §4's restart window:
        # between a container start and `--restore` finishing, the literal
        # path is whatever sbx mounted there. That is a race to close in
        # one place, not a reason to name a second path for the same
        # directory everywhere else.
        for var in REPO_ENVIRONMENT_VARS.get(canonical, []):
            variables[var] = path
    variables.update(mediawiki_env_vars(resolved))
    variables.update(parsoid_env_vars(resolved, readonly_dirs))
    # Last, so a user's own repos.yaml extra_environment can override any
    # of it.
    variables.update(extra_environment or {})

    domains = sorted(set(wiki_family_domains(profile_path)) | set(EXTRA_DOMAINS))
    packages = list(BASE_PACKAGES) + list(extra_packages or [])

    if daemon_port is None:
        daemon_port = DEFAULT_DAEMON_PORT
    readonly_dirs = set(readonly_dirs or [])
    if plan is not None:
        # write_kit_dir must be given the same plan -- it's what puts the
        # file this command reads into the kit.
        setup_argv = [SANDBOX_SETUP_SCRIPT, SANDBOX_PLAN_FILE]
    else:
        repo_args = [
            f"{path}:ro" if path in readonly_dirs else path for _canonical, path in resolved
        ]
        setup_argv = [SANDBOX_SETUP_SCRIPT, host_home, str(daemon_port)] + repo_args
    setup_cmd = "python3 " + " ".join(shlex.quote(a) for a in setup_argv) + "\n"

    # No registered host-side server means no proxy entry to make. The
    # steps are dropped entirely rather than made no-ops, so a sandbox
    # without MCP has nothing about MCP in its spec to explain.
    mcp = mcp_config(mcp_servers)
    install_steps = [
        {"command": "apt-get update\napt-get install -y " + " ".join(packages) + "\n"},
        # Before the long MediaWiki step, not after: a create that dies in
        # composer still leaves a sandbox whose SessionStart hook runs.
        exec_bits_install_step(mcp=bool(mcp)),
        {"command": setup_cmd},
        # Install *and* startup, which is one job in two places on
        # purpose. Install-only loses if sbx rewrites settings.json on a
        # later container start (it writes the file; whether it rewrites
        # it every time is not something we have measured). Startup-only
        # loses the first session: startup commands do not block the `sbx
        # exec` that triggered the start (§46), so the agent can be
        # running before the merge lands. The merge is a deep merge of a
        # constant patch, so doing it twice costs a file read.
        settings_merge_install_step(),
        claude_json_install_step(),
    ]
    # Order matters: the daemon serves clones whose alternates the
    # restore pass is what makes resolvable again (§40).
    startup_steps = [
        restore_startup_command(),
        daemon_startup_command(daemon_port),
        settings_merge_startup_command(),
        claude_json_startup_command(),
        claude_md_startup_command(),
        exec_bits_startup_command(mcp=bool(mcp)),
    ]
    if mcp:
        install_steps.append(mcp_register_install_step())
        startup_steps.append(mcp_register_startup_command())
    startup_steps.append(plugin_check_startup_command())

    return {
        "schemaVersion": "2",
        "kind": "mixin",
        "name": KIT_NAME,
        "displayName": KIT_DISPLAY_NAME,
        "description": KIT_DESCRIPTION,
        # We always launch this mixin over the `claude` agent (see
        # wmf_sbx_create.build_sbx_command), and v2 enforces this at
        # composition time -- better than discovering the mismatch as a
        # missing binary halfway through an install step.
        "requires": {"agent": "claude"},
        "environment": {"variables": variables},
        "permissions": {"network": {"allow": domains}},
        # Declaring the port here is what makes it published on *every*
        # container start, not just the one `wmf-sbx ports --publish`
        # happened to run after. The *host* port is still ephemeral and
        # still moves on every restart (sbx/NOTES.md §34.2) -- that's what
        # refresh_host_port is for; this only removes the "published at
        # all?" failure mode.
        #
        # `protocol:` is left off. The reference says an empty protocol
        # publishes IPv4 only, which suits a daemon bound to 0.0.0.0. On
        # sbx 0.39 it published both 127.0.0.1 and ::1 anyway (MEASURED,
        # §36.3); 0.42.0 made `tcp4` the default for kit-declared ports,
        # so the ::1 half should now be gone. Either way our own URLs name
        # 127.0.0.1 literally and never dial it, and the reader that finds
        # the port (create_mod.ipv4_mapping) accepts both spellings.
        "ports": [{"container": int(daemon_port), "name": DAEMON_PORT_NAME}],
        "setup": {"install": install_steps, "startup": startup_steps},
    }


def restore_startup_command():
    """The `setup.startup` entry that puts the repo mounts back after
    `sbx stop` / `sbx run`.

    Everything wmf-sbx-setup mounts is mount-namespace state, and docker
    rebuilds the namespace from the container config on every start
    (sbx/NOTES.md §31.1, MEASURED in §40). Two things follow, and only the
    first is cosmetic:

    - the writable clone stops being visible at the host repo's path, and
      -- worse -- its `--shared` alternates point into `.sbx-originals`,
      which is an empty directory again, so the clone loses the objects it
      borrowed (measured: 6707 commits reachable before a stop, 5 after);
    - every read-only remount is gone, so a host repo sbx itself mounted
      read/write (the primary workspace always is) comes back **writable
      from inside the sandbox**. That one predates §39 and was simply
      never noticed.

    Runs as root -- `mount` needs it, and the default startup user is the
    agent (see daemon_startup_command). Not `background`: the daemon entry
    after it should serve clones whose objects resolve. Idempotent by
    construction (restore_repo checks each mount before making it), and a
    no-op on the very first container start, when the install step that
    writes the layout file hasn't run yet.

    It also re-locks the shared agent-skills store, which is a mount like
    any other and so comes back `rw` from the same rebuild -- and unlike
    the repos that one is worth locking on the *first* start too, before
    any layout exists (wmf_sbx_setup.lock_shared_skills, SECURITY.md §7.6).
    """
    return {
        "description": "restore the repo and skills mounts after a container restart",
        "user": "0",
        "command": ["python3", SANDBOX_SETUP_SCRIPT, "--restore"],
    }


def daemon_startup_command(port=None, sandbox_home=None):
    """The `setup.startup` entry that brings the parallel tree's git daemon
    back after `sbx stop` / `sbx run`.

    The install step starts a daemon too (wmf_sbx_setup.start_daemon), but
    install runs once, at create, and the process dies with the container
    -- which is what left `wmf-sbx-rm`'s unfetched-work guard unable to
    reach anything (sbx/NOTES.md §32.4). Startup commands are the
    documented "run on every container start" hook.

    Three details from the kit-spec v2 reference, all load-bearing:

    - `command` is an argv array run with no shell, so the guard needs an
      explicit `sh -c`.
    - `background: true` means "don't make later startup commands wait";
      a daemon that never exits would otherwise block the whole startup
      chain. (It never gates the agent's entrypoint either way.)
    - startup commands "must be idempotent ... they run on every sandbox
      start and replay on container restarts". Hence the probe: at create
      time this may run minutes after the install step already started
      one, and `--reuseaddr` is SO_REUSEADDR, which does not let a second
      listener share the port. connect_ex returns 0 when something is
      already listening, so the probe exits non-zero and the `if` skips
      the daemon -- and the command as a whole still exits 0, so a normal
      restart doesn't log a failure.

    The probe's own connection makes the running daemon log one "fatal:
    the remote end hung up unexpectedly" to /var/log/sbx-kit-startup.log
    per skipped start. Cosmetic, and cheaper than the alternatives (a
    bind probe needs a try/except no longer expressible in one line; a
    lock file would have to be taken by start_daemon too).
    """
    if port is None:
        port = DEFAULT_DAEMON_PORT
    if sandbox_home is None:
        sandbox_home = setup_mod.SANDBOX_HOME
    probe = (
        "python3 -c 'import socket,sys; "
        f'sys.exit(1 if socket.socket().connect_ex(("127.0.0.1", {int(port)})) == 0 '
        "else 0)'"
    )
    daemon = " ".join(shlex.quote(a) for a in setup_mod.daemon_argv(sandbox_home, port))
    return {
        "description": f"git daemon serving {sandbox_home} on port {port}",
        # The default user for a startup command is "1000" -- the agent --
        # which is exactly what the daemon must run as (start_daemon's
        # `sudo -u agent` exists for the same reason). Stated rather than
        # inherited: running this one as root reintroduces the
        # dubious-ownership incident in sbx/NOTES.md #21.
        "user": "1000",
        "command": ["sh", "-c", f"if {probe}; then exec {daemon}; fi"],
        "background": True,
    }


def _spec_uses_plan_file(spec):
    steps = (spec.get("setup") or {}).get("install") or []
    return any(SANDBOX_PLAN_FILE in (step.get("command") or "") for step in steps)


def dump_kit_yaml(spec):
    if yaml is None:
        raise RuntimeError("PyYAML is required to render a kit spec.yaml.")
    return yaml.safe_dump(spec, sort_keys=False)


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


def write_kit_dir(spec, dest_dir, plan=None, mcp_servers=None):
    """Writes dest_dir/spec.yaml, dest_dir/files/home/wmf-sbx-setup, and
    dest_dir/files/home/bin/{git-safe-reset,git-review-check} (plus
    wmf-sbx-plan.json when a plan is given), creating dest_dir if needed.
    Also lays down the wmf-claude plugin tree, the settings patch the
    spec's `--settings` steps apply, and -- when the spec registers them
    -- the MCP proxy and its registration data. `mcp_servers` must be the
    same list build_kit_spec was given. Returns dest_dir, so callers can
    pass this straight to `sbx create --kit`."""
    if plan is None and _spec_uses_plan_file(spec):
        # Otherwise this fails much later and much less clearly: the
        # sandbox boots, the install step can't read the plan, and no repo
        # gets cloned.
        raise RuntimeError(
            "this kit spec's install step reads "
            f"{SANDBOX_PLAN_FILE}, so write_kit_dir needs the plan that "
            "built it"
        )
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, "spec.yaml"), "w", encoding="utf-8") as f:
        f.write(dump_kit_yaml(spec))
    files_home = os.path.join(dest_dir, "files", "home")
    os.makedirs(files_home, exist_ok=True)
    shutil.copy(STATIC_SETUP_SCRIPT, os.path.join(files_home, "wmf-sbx-setup"))
    helper_bin = os.path.join(files_home, "bin")
    os.makedirs(helper_bin, exist_ok=True)
    for name, source_dir in HELPER_SCRIPTS.items():
        dest = os.path.join(helper_bin, name)
        shutil.copy(os.path.join(source_dir, name), dest)
        os.chmod(dest, 0o755)
    shutil.copy(TESTING_GUIDE_SOURCE, os.path.join(files_home, TESTING_GUIDE))
    if plan is not None:
        with open(os.path.join(files_home, os.path.basename(SANDBOX_PLAN_FILE)),
                  "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, sort_keys=False)
            f.write("\n")
    claude_dir = os.path.join(files_home, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    with open(os.path.join(claude_dir, "CLAUDE.md"), "w", encoding="utf-8") as f:
        f.write(home_claude_md(plan))
    shutil.copy(CLAUDE_MD_EDITS_SOURCE,
                os.path.join(claude_dir, os.path.basename(SANDBOX_CLAUDE_MD_EDITS)))
    # Shipped as data rather than applied here: settings.json is sbx's
    # file, and only wmf-sbx-setup --settings, running in the sandbox after
    # sbx has written it, can merge into it rather than over it.
    with open(os.path.join(claude_dir, os.path.basename(SANDBOX_SETTINGS_PATCH)),
              "w", encoding="utf-8") as f:
        json.dump(settings_patch(), f, indent=2, sort_keys=False)
        f.write("\n")
    with open(os.path.join(claude_dir, os.path.basename(SANDBOX_CLAUDE_JSON_PATCH)),
              "w", encoding="utf-8") as f:
        json.dump(claude_json_patch(), f, indent=2, sort_keys=False)
        f.write("\n")
    write_plugin_tree(files_home)
    if _spec_registers_mcp(spec):
        write_mcp_files(files_home, mcp_servers)
    return dest_dir


def _spec_registers_mcp(spec):
    """Whether this spec has the `--mcp` steps, so write_kit_dir ships the
    proxy for a spec that will use it and not for one that won't.

    A spec with no `setup.startup` at all is hand-written -- every
    WriteKitDirTests case is one -- and gets the proxy, which is the
    useful default for a caller that did not go through build_kit_spec.
    """
    startup = (spec.get("setup") or {}).get("startup")
    if not startup:
        return True
    return any(SANDBOX_MCP_FILE in (step.get("command") or []) for step in startup)


def write_mcp_files(files_home, servers=None):
    """Drops wmf-sbx-mcp-proxy into ~/.local/bin and the registration data
    beside the settings patch. Returns the config that was written.

    The proxy is stdlib-only Python by design -- no npm install, no venv,
    nothing to build in the sandbox -- so shipping it really is a copy.
    """
    config = mcp_config(servers)
    if not config:
        return config
    local_bin = os.path.join(files_home, os.path.relpath(SANDBOX_LOCAL_BIN,
                                                         setup_mod.SANDBOX_HOME))
    os.makedirs(local_bin, exist_ok=True)
    dest = os.path.join(local_bin, MCP_PROXY)
    shutil.copy(MCP_PROXY_SOURCE, dest)
    os.chmod(dest, 0o755)
    with open(os.path.join(files_home, os.path.relpath(SANDBOX_MCP_FILE,
                                                       setup_mod.SANDBOX_HOME)),
              "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, sort_keys=True)
        f.write("\n")
    return config
