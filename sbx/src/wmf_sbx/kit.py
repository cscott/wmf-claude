#!/usr/bin/env python3
"""What the Lima backend keeps of the old Docker kit generator (the
sbx-docker-final branch keeps the rest):

- BASE_PACKAGES and the helper scripts, for the image (image.py);
- the testing guide, for the session files (session.py);
- the setup plan (mediawiki.py, setup.py --lima);
- the MediaWiki environment variables (session.py, D4).
"""

import os

# The one sibling import that goes this direction: setup.py imports
# nothing (it runs in the VM alone), but this module runs on the host.
# Reused for parse_install_params, so the session's environment and the
# .env that setup.py writes can't disagree.
from . import setup as setup_mod

# This module lives in sbx/src/wmf_sbx/; SBX_ROOT is sbx/, REPO_ROOT the
# wmf-claude checkout that contains it. realpath, like create._SBX_ROOT:
# the checkout is commonly reached through a symlink, and a symlinked
# host path is the wrong thing to hand to anything that mounts (§66).
SBX_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))))
REPO_ROOT = os.path.dirname(SBX_ROOT)


# The image build installs these into /usr/local/bin (image.helper_files),
# so they are on PATH for every user. These are bash scripts, not part of
# this python package, and they come from two directories, so each name
# names its own source:
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


PLAN_VERSION = 1

CORE_CANONICAL = setup_mod.CORE_CANONICAL


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
    "php-sqlite3", "php-zip", "php-gd", "php-imagick",
    "php-mysql", "composer", "imagemagick", "librsvg2-bin", "diffutils",
    "git-review", "php-wikidiff2",
]


# Where mw-install-browser links the Chrome it installs, and what the
# selenium harness reads. The same path Quibble uses.
CHROME_BIN = "/usr/bin/chromium"


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


def build_plan(resolved, readonly_dirs=None, links=None, primary=None):
    """The plan that setup.py --lima runs from, as a plain dict.

    resolved: [(canonical_or_None, local_dir), ...], in command-line order
    (the first is the primary unless `primary` says otherwise).
    links: {local_dir: (link_name, link_dir)} for the repos that should be
    symlinked into the core clone -- computed host-side, where the parsed
    manifests are (see sbx/DESIGN-dependency-walk.md §1 on why the link
    name is not the manifest's `name` field).
    """
    readonly_dirs = set(readonly_dirs or [])
    links = links or {}
    repos = []
    for canonical, path in resolved:
        link_name, link_dir = links.get(path, (None, None))
        repos.append({
            "path": path,
            "canonical": canonical,
            "readOnly": path in readonly_dirs,
            "linkName": link_name,
            "linkDir": link_dir,
        })
    if primary is None and repos:
        primary = repos[0]["path"]
    return {"version": PLAN_VERSION, "primary": primary, "repos": repos}
