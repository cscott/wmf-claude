#!/usr/bin/env python3
"""Static, kit-injected helper that runs once inside a freshly created
sandbox, as the final `commands.install` step of the generated MediaWiki
kit (see wmf_sbx/kit.py). `sbx create`'s own primary-workspace positional
is unconditionally read/write, and extras default to read/write too now
(see sbx/DESIGN-parallel-clone-tree.md) -- this script is what actually turns
that into "every repo gets a private, writable checkout, and the
sbx-mounted original is locked to read-only":

For each REPO under HOST_HOME, moves the sbx mount aside to
$HOME/.sbx-originals/<rel> (`mount --move`), clones it from there (with
--shared against the still-writable original, so the clone borrows its
objects rather than copying 685 MB of them across virtiofs) into the
sandbox's own $HOME at the equivalent relative path, remounts the moved
original read-only, and finally bind-mounts the clone over the path the
original vacated -- so /home/<user>/<rel>, which is where the agent
starts and what every host-shaped path means to it, *is* the writable
clone (sbx/NOTES.md §39). The move and the alias are best-effort: if
either fails the repo is left the way every sandbox before §39 had it,
the host mirror at its own path and the clone reachable only under
/home/agent. Neither survives `sbx stop` (docker rebuilds the mount
namespace from the container config), which is what setup.startup will
have to redo -- not implemented yet.

Unless REPO ends in ':ro' (the same opt-out suffix wmf-sbx-create's own
command line uses -- see its split_ro_suffix), in which case nothing is
moved and no clone is made at all: the literal path keeps showing the
pristine original, and the parallel path is a second read-only bind mount
of it, saving disk at the cost of a private working tree. A REPO outside
HOST_HOME has no sandbox-$HOME-relative home to put a parallel copy at
all, clone or bind mount, so it is just remounted read-only in place -- a
known stage-1 limitation (see sbx/DESIGN-parallel-clone-tree.md) rather
than an oversight.

Once every repo is in place, starts a single `git daemon` covering the
whole parallel tree (--base-path=SANDBOX_HOME --export-all), so every
parallel clone -- and every ':ro' bind-mounted one, which is just as
servable -- is fetchable from the host over git:// at PORT (see
sbx/DESIGN-parallel-clone-tree.md §3). Read-only by design (no
--enable=receive-pack): the host fetches from the sandbox to review/merge
work, nothing pushes into it over this channel.

Extensions and skins are then symlinked into the core clone
(link_into_core), which is what makes the wiki actually loadable -- see
sbx/DESIGN-dependency-walk.md §7, and the rest of the MediaWiki setup
chain runs on top of that (git safe-reset, composer update, .env, npm ci,
`composer mw-install:sqlite -- --with-extensions`) -- see
sbx/DESIGN-setup-steps.md, whose §8 fixes the ordering and the failure
policy: the per-repo steps warn and continue, the core ones are fatal,
and the daemon starts *before* all of it so the host can look inside
while the slow part is still running.

Everything this script prints is mirrored into /var/log/wmf-sbx-setup.log,
and the `error:`/`warning:` lines are distilled into
/var/log/wmf-sbx-setup.status, which wmf-sbx-create reads back with
`sbx exec` and prints once the sandbox is up (see SetupLog and
sbx/NOTES.md §32.1: `sbx create` collapses this whole step to one ✓ line,
so nothing printed here reaches the terminal on its own).

This file is copied byte-for-byte (dropping the .py extension) into a
generated kit's files/home/wmf-sbx-setup by wmf_sbx_kit.write_kit_dir, so
it must not import any sibling wmf_sbx_* module -- those aren't present in
the sandbox, only this one file is.

Usage: wmf-sbx-setup PLAN.json
       wmf-sbx-setup HOST_HOME PORT REPO[:ro] [REPO[:ro] ...]
       wmf-sbx-setup --restore [ORIGINALS_DIR]
       wmf-sbx-setup --claude-md EDITS.json [CLAUDE.md]

The third form is the kit's `setup.startup` entry, run as root on every
container start: none of the mounts above survive `sbx stop`, so it redoes
them from the layout the setup run recorded (see restore_mounts and
sbx/NOTES.md §40). It is not optional -- after a restart the clones'
alternates point into an .sbx-originals that is empty again, and any host
mirror sbx itself mounted read/write comes back writable.

Both the setup and the restore form also remount $HOME/.claude/skills
read-only (lock_shared_skills). That one is not a repo and not ours: sbx
mounts the same host directory into every sandbox on the machine, so it
is a write channel from this agent to every other one, and to agents in
sandboxes that do not exist yet. Read-only here is a second layer, not a
boundary -- the agent is root and can remount it rw -- so it ships with
an upstream bug report, not instead of one (SECURITY.md §7.6).

The second form is the original positional one, kept working for hand
invocations; it can't express a symlink plan (link names contain spaces
and canonical names live host-side), so it does everything except the
symlinks. See sbx/DESIGN-setup-steps.md §1 for the plan file's shape.
"""

import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import traceback
import urllib.parse

SANDBOX_HOME = "/home/agent"

# Where each repo's sbx bind mount is moved aside to (`mount --move`), so
# the writable clone can be bind-mounted over the path the host repo used
# to occupy and the agent's starting working directory simply *is* the
# clone -- see setup_repo and sbx/NOTES.md §30's third bullet / §39.
# Hidden, so the search tools that skip dotdirs don't turn every grep into
# two hits (the clone and the original).
ORIGINALS_DIR = os.path.join(SANDBOX_HOME, ".sbx-originals")

# What setup_repo did to each repo, written next to the moved-aside
# originals and read back by `--restore` on every container start (§40).
# It is not derivable from the plan: whether a repo ended up aliased
# depends on whether the mounts took, and restoring an alias that never
# happened would move a mirror a fallback clone still reads through.
LAYOUT_NAME = "layout.json"
LAYOUT_VERSION = 1

# Claude Code's user-level settings, which sbx has already written by the
# time anything here runs (permissions.defaultMode, model, theme...). The
# kit adds its plugin and its permission denies by *merging* into this
# file -- see merge_settings and wmf_sbx_kit.settings_patch.
SANDBOX_SETTINGS_FILE = os.path.join(SANDBOX_HOME, ".claude", "settings.json")

# The shared agent-skills store, which sbx mounts into *every* sandbox on
# the host from one directory under ~/.local/state/sandboxes (MEASURED,
# sbx/NOTES.md §77.2-§77.4: virtiofs rw, writable, and readable *and
# deletable* from a second sandbox, outliving the `sbx rm` of the one that
# wrote it). Nothing in this project puts anything there -- the plugin
# ships through ~/.claude/plugins instead (Route A, §64) -- so locking it
# read-only costs us nothing and removes the sandbox-to-sandbox write
# channel from everything short of a deliberate agent. See
# lock_shared_skills for why "short of a deliberate agent" is the honest
# limit, and SECURITY.md §7.6 for the upstream ask that would fix it.
SHARED_SKILLS_DIR = os.path.join(SANDBOX_HOME, ".claude", "skills")

# MCP registrations do *not* live in settings.json: `claude mcp add
# --scope user` writes them into this file's top-level "mcpServers"
# object, alongside sbx's own `mcp-gateway` entry. We read it to decide
# whether anything needs doing and mutate it only through the CLI -- the
# same file holds the session history and the onboarding state, so a
# botched rewrite is a broken Claude Code, not a missing MCP server.
SANDBOX_CLAUDE_JSON = os.path.join(SANDBOX_HOME, ".claude.json")

# Where the image puts the agent's `claude`, and *not* anywhere `sudo`
# will look for it. sudo replaces PATH with its own `secure_path`
# (MEASURED: /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# :/snap/bin), so the install-time run -- root, through
# `sudo -u agent -H` -- gets `sudo: claude: command not found` and exit 1
# even though the very same command works when the agent runs it. That
# failed a real create at the last install step (sbx/NOTES.md §69). Use
# the absolute path instead of trying to reinstate a PATH through sudo.
SANDBOX_CLAUDE_BIN = os.path.join(SANDBOX_HOME, ".local", "bin", "claude")

# Where this script's own narration ends up, and the machine-readable
# distillation wmf-sbx-create reads back out with `sbx exec` once the
# sandbox is up (see wmf_sbx_create.report_setup_problems).
#
# `sbx create` collapses each install command to a single line --
# `✓ python3 /home/agent/wmf-sbx-setup ... (77.8s)` -- so *nothing* this
# script prints reaches the engineer's terminal. That is how a silently
# skipped `git safe-reset` sat unnoticed in a real sandbox for a day
# (sbx/NOTES.md §32.1). /var/log is where the rest of the sandbox already
# reports for duty (the kit's own startup log is
# /var/log/sbx-kit-startup.log), so it's the first place anyone looks.
LOG_DIR = "/var/log"
SETUP_LOG_NAME = "wmf-sbx-setup.log"
SETUP_STATUS_NAME = "wmf-sbx-setup.status"

# The restart pass gets its own pair, rather than appending to or
# clobbering the create-time ones: `--restore` runs on every container
# start, and the setup report is the record of the one run that built the
# sandbox. Nothing reads these yet; they're where to look after a resume.
RESTORE_LOG_NAME = "wmf-sbx-restore.log"
RESTORE_STATUS_NAME = "wmf-sbx-restore.status"

# The plan that wmf_sbx_kit writes next to this script. The kit passes it
# to the setup step, and `--claude-md` reads the primary workspace from
# it.
SANDBOX_PLAN_FILE = os.path.join(SANDBOX_HOME, "wmf-sbx-plan.json")

# `--claude-md` edits the CLAUDE.md that sbx writes in the parent
# directory of the primary workspace (sbx/NOTES.md §95). The edits travel
# as data in the kit. Before the first edit, the script keeps a copy of
# sbx's own text, so that `wmf-sbx refresh-claude-md` can read it back.
# The status file is where the host reads the result: startup output goes
# only to /var/log/sbx-kit-startup.log.
SANDBOX_CLAUDE_MD_EDITS = os.path.join(SANDBOX_HOME, ".claude",
                                       "wmf-sbx-claude-md.json")
SANDBOX_CLAUDE_MD_UPSTREAM = os.path.join(SANDBOX_HOME, ".claude",
                                          "wmf-sbx-upstream-CLAUDE.md")
CLAUDE_MD_STATUS_NAME = "wmf-sbx-claude-md.status"
CLAUDE_MD_MARKER = "<!-- wmf-sbx: edited by wmf-sbx-setup --claude-md"

# The restore log *appends*, unlike the setup log. It runs on every
# container start -- and the sandbox is started by every `sbx exec`, so
# that is a lot of runs -- and the question it gets asked is "did this
# start restore anything, and what did the one before it do?". A
# truncating log answers neither, and makes an empty file ambiguous
# between "never ran" and "ran and was overwritten" (§46; §41 was read
# from a log that could have been either). Capped, since it grows for the
# life of the sandbox.
RESTORE_LOG_MAX_BYTES = 1 << 20

# Where the kernel says what is mounted where, and with which options.
# `os.path.ismount` answers "is something mounted here", which is half the
# question: the other half -- ro or rw -- is the whole of §40's fourth
# finding, and it is invisible from the path itself.
MOUNTINFO = "/proc/self/mountinfo"

# How long `--verify --wait` will sit there, and how often it looks. The
# startup dispatcher's restore pass takes well under a second once it
# runs (three repos, three mounts each); the wait is for the gap between
# the container starting and the dispatcher reaching our entry, which is
# behind three of the claude kit's own commands (§46).
DEFAULT_VERIFY_WAIT = 20.0
VERIFY_POLL_SECONDS = 0.25

# Bumped for an incompatible change to the .status file. Unlike the plan
# file, the two ends of this one *can* be different versions: the status is
# written by the kit-copied script inside the sandbox and read by whatever
# wmf-sbx-create the engineer runs later, which may have moved on.
STATUS_VERSION = 1

# Bumped only for an incompatible change to the plan file: the kit that
# writes the plan and the script that reads it are copied into the sandbox
# together, so the two can never actually be different versions -- this is
# a guard against a stale hand-written plan, not a compatibility layer.
PLAN_VERSION = 1

# The one canonical this script has to recognise: core is where everything
# else gets linked, and it's never itself a link.
CORE_CANONICAL = "gerrit:mediawiki/core"

# The second one it has to recognise: a writable Parsoid clone gets linked
# into LocalSettings.php by link_parsoid_checkout, same idea as
# CORE_CANONICAL but there's no symlink involved -- see mediawiki_setup.
PARSOID_CANONICAL = "gerrit:mediawiki/services/parsoid"

# The sandbox's own non-root user -- this script runs as root (it's a
# commands.install step), so anything it creates under SANDBOX_HOME (which
# the "agent" user actually works in) needs an explicit chown back, or git
# refuses to touch it ("detected dubious ownership").
SANDBOX_USER = "agent"

# Remote names inside a parallel-tree clone. `git clone` calls the thing it
# cloned from `origin`, which here is the host's read-only checkout -- but
# an engineer typing `git fetch origin` means Gerrit, so configure_remotes
# swaps them: the host mirror becomes `local`, and `origin` is the real
# upstream. See configure_remotes for the fallbacks.
MIRROR_REMOTE = "local"
UPSTREAM_REMOTE = "origin"

# The helpers the kit ships in files/home/bin/ (see
# wmf_sbx_kit.HELPER_SCRIPTS, which this list must agree with), installed
# onto PATH so `git safe-reset` resolves as a git subcommand, so
# git-safe-reset's own bare-name call to git-review-check resolves too, and
# so the agent can install a browser or Cypress on demand.
HELPER_SCRIPTS = ("git-safe-reset", "git-review-check", "mw-install-browser",
                  "mw-install-cypress")
HELPER_SOURCE_DIR = os.path.join(SANDBOX_HOME, "bin")
HELPER_INSTALL_DIR = "/usr/local/bin"

# Used only if core's own composer.json can't be read or parsed -- the
# install script there is the authority for all of these (see
# sbx/DESIGN-setup-steps.md §4.2), and the plan file's `mediawiki` block
# overrides these in turn.
DEFAULT_INSTALL_PARAMS = {
    "server": "http://localhost:4000",
    "scriptPath": "",
    "adminUser": "Admin",
    "adminPassword": "adminpassword",
}

# core's own composer script, which is what actually installs the wiki.
INSTALL_SCRIPT_NAME = "mw-install:sqlite"

# Shape from mediawiki-core-clean/.env. Read by `docker compose`, which
# isn't running here -- the values that make the test harnesses work are
# the ones build_kit_spec puts in the kit's environment.variables. This
# file is still written because it's the documented artifact and it's
# where an engineer will look. See sbx/DESIGN-setup-steps.md §4.1.
ENV_TEMPLATE = """\
MW_SCRIPT_PATH={script_path}
MW_SERVER={server}
MW_DOCKER_PORT={port}
MEDIAWIKI_USER={user}
MEDIAWIKI_PASSWORD={password}
XDEBUG_CONFIG=
XDEBUG_ENABLE=true
XHPROF_ENABLE=true
MW_DOCKER_UID={uid}
MW_DOCKER_GID={gid}
"""

# The api-testing library's config file (`api-testing/lib/config.js`).
# Written into the core clone: core's .gitignore already lists this exact
# name, so `git status` stays clean, and it sits next to
# LocalSettings.php, whose values it repeats. The kit also exports
# API_TESTING_CONFIG_FILE with the absolute path, so an extension's own
# `npm run api-testing` finds it from outside core. See
# sbx/DESIGN-testing-instructions.md §5.5.
API_TESTING_CONFIG_NAME = ".api-testing.config.json"

# Written into the core clone, which merges it into core's own
# composer.json through wikimedia/composer-merge-plugin. Core's .gitignore
# lists this exact name. See write_composer_local.
COMPOSER_LOCAL_NAME = "composer.local.json"

# quibble's CreateComposerLocal globs, verbatim: CI merges every
# extension's and skin's Composer dependencies into core's vendor/, and
# phan reads core's vendor/ and not the extension's own. Parsoid is an
# extension here: its symlink is in extensions/, so the glob finds it.
# In CI it is cloned to services/parsoid, which the glob does not reach.
COMPOSER_LOCAL_INCLUDE = ["extensions/*/composer.json", "skins/*/composer.json"]

# Not in quibble's file, and only when Parsoid is a workspace clone.
# Core requires wikimedia/parsoid, and the Parsoid checkout in extensions/
# declares the same classes, so each core composer update prints one
# "Ambiguous class resolution" warning per Parsoid class (359 of them).
# Composer already uses the checkout; this removes the vendor copy from
# the classmap, so the warnings stop. Without a checkout, the wiki uses
# the vendor copy, so it stays in the classmap.
COMPOSER_LOCAL_EXCLUDE_FROM_CLASSMAP = ["vendor/wikimedia/parsoid/"]

# api-testing asks the wiki for this page in its smoke tests. MediaWiki's
# installer creates it under this name in English.
API_TESTING_MAIN_PAGE = "Main Page"

# $wgSecretKey as the installer writes it. api-testing signs its login
# requests with the same value, so a config with the wrong one gets a
# rejected login rather than a clear error.
_SECRET_KEY_RE = re.compile(
    r"""^\s*\$wgSecretKey\s*=\s*["']([0-9a-fA-F]+)["']\s*;""", re.MULTILINE
)


def setup_log_path(log_dir=LOG_DIR):
    return os.path.join(log_dir, SETUP_LOG_NAME)


def setup_status_path(log_dir=LOG_DIR):
    """Where wmf-sbx-create looks (over `sbx exec`) for the report -- the
    one thing about this file both sides have to agree on, so both sides
    ask this function."""
    return os.path.join(log_dir, SETUP_STATUS_NAME)


def restore_status_path(log_dir=LOG_DIR):
    """Where the restart pass leaves its report -- read back by
    wmf_sbx_create.restore_sandbox_mounts the same way, and for the same
    reason: nothing a startup command prints reaches the engineer either."""
    return os.path.join(log_dir, RESTORE_STATUS_NAME)


def restore_log_path(log_dir=LOG_DIR):
    return os.path.join(log_dir, RESTORE_LOG_NAME)


class SetupLog:
    """A stand-in for sys.stderr that mirrors this script's own narration
    into a log file and remembers the lines that reported a problem.

    Only *this script's* output: the commands it runs (composer, npm, git)
    inherit the real fd 2 and go on straight to `sbx create`'s collapsed
    log, which is what we want -- their combined output runs to tens of
    thousands of lines, and the point of the status file is a list an
    engineer will actually read.

    A line counts as a problem if it starts with `error:` or `warning:`,
    the two prefixes this script already used everywhere before there was
    anywhere to collect them. `record` adds one without printing it, for
    problems that are reported some other way (the summary block at the
    end of main).

    Never fatal: a log file that can't be opened costs the report, not the
    sandbox.
    """

    def __init__(self, stream, path=None, append=False, max_bytes=None):
        self.stream = stream
        self.path = path
        self.problems = []
        self.file = None
        self._partial = ""
        if path is not None:
            mode = "a" if append else "w"
            if append and max_bytes:
                try:
                    if os.path.getsize(path) > max_bytes:
                        mode = "w"  # start over rather than grow forever
                except OSError:
                    pass
            try:
                self.file = open(path, mode, encoding="utf-8")
            except OSError as e:
                print(f"warning: could not open {path} for logging ({e})", file=stream)
                self.path = None

    def write(self, text):
        self.stream.write(text)
        if self.file is not None:
            self.file.write(text)
            # Unbuffered in effect: if the install step dies (or `sbx
            # create` kills it), the log has to hold everything up to the
            # last thing that happened.
            self.file.flush()
        self._scan(text)
        return len(text)

    def _scan(self, text):
        text = self._partial + text
        lines = text.split("\n")
        self._partial = lines.pop()
        for line in lines:
            if line.startswith("error:") or line.startswith("warning:"):
                self.record(line)

    def record(self, problem):
        if problem not in self.problems:
            self.problems.append(problem)

    def flush(self):
        self.stream.flush()
        if self.file is not None:
            self.file.flush()

    def isatty(self):
        return False

    def close(self):
        if self._partial:
            self._scan("\n")
        if self.file is not None:
            self.file.close()
            self.file = None


def write_status(path, exit_code, problems, log_path=None):
    """The distillation wmf-sbx-create reads back. Returns True if written.

    JSON on one line per key, not the log itself: the caller is on the far
    side of an `sbx exec` and shouldn't have to parse prose."""
    status = {
        "version": STATUS_VERSION,
        "exit": exit_code,
        "problems": list(problems),
        "log": log_path,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
            f.write("\n")
    except OSError as e:
        print(f"warning: could not write {path} ({e})", file=sys.stderr)
        return False
    return True


def parse_repo_arg(spec):
    """Splits a trailing ':ro' opt-out suffix off a repo argument --
    mirrors wmf_sbx_create.split_ro_suffix, duplicated here since this file
    can't import sibling wmf_sbx_* modules (see module docstring). Returns
    (literal_path, is_ro)."""
    if spec.endswith(":ro"):
        return spec[: -len(":ro")], True
    return spec, False


def parallel_path(host_home, sandbox_home, resolved_dir):
    """resolved_dir's equivalent path under sandbox_home, if resolved_dir
    is under host_home -- else None (nothing to translate; resolved_dir
    keeps its literal bind-mount location)."""
    rel = os.path.relpath(resolved_dir, host_home)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return os.path.normpath(os.path.join(sandbox_home, rel))


def work_path(entry):
    """Where everything *after* setup_repo should work in this repo.

    setup_repo leaves a writable repo with two names for one directory:
    `dest`, the parallel path the clone really lives at, and `literal`,
    which is bind-mounted onto it (mode "alias"). Either reaches the same
    files -- but only the literal one means anything to the engineer on
    the host or to the agent, whose working directory it is. So it is the
    one that should end up baked into `.env`, `LocalSettings.php`, the
    extension symlinks and composer's generated autoloader, instead of a
    `/home/agent` path nobody asked for (sbx/NOTES.md §63.1).

    The exception is mode "clone": there the alias did *not* take, the
    literal path is still the read-only host mirror, and `dest` is the
    only writable copy. Modes "bind" (':ro') and "inplace" have no clone
    at all, and their literal path is the original, which is what they
    mean.

    Only meaningful once the mounts are in place -- during setup_repo
    itself, and in restore_mounts, the parallel path is still the one to
    build against, because the alias does not exist yet.
    """
    if entry["mode"] == "clone":
        return entry["dest"]
    return entry["literal"]


def originals_path(host_home, literal_path):
    """Where literal_path's sbx mount gets moved aside to, or None if
    literal_path is outside host_home (nothing to move it out of the way
    *for*: those repos get no parallel clone either)."""
    rel = os.path.relpath(literal_path, host_home)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return os.path.normpath(os.path.join(ORIGINALS_DIR, rel))


def move_original(literal_path, orig, run=subprocess.run):
    """`mount --move` the host repo's sbx mount out of the way, so the
    writable clone can be mounted over the path it vacated.

    Warns and returns False rather than raising, unlike its neighbours: the
    alias is a convenience (it saves the agent a `cd`), and a create that
    dies because the working directory couldn't be made pretty is a worse
    outcome than one that starts in the read-only mirror the way every
    sandbox before this did. setup_repo falls back to that on a False."""
    if not os.path.ismount(literal_path):
        # A hand invocation, or a repo sbx didn't mount. Nothing to move,
        # and mounting the clone over it would *hide* a real directory
        # rather than a mirror. Deliberately not a `warning:` -- this is
        # the "nothing to do" case, and every hand invocation would
        # otherwise put a line per repo in the setup report (§38).
        print(
            f"{literal_path} is not a mount point; leaving the host mirror where it is",
            file=sys.stderr,
        )
        return False
    if os.path.ismount(orig):
        print(f"warning: {orig} is already a mount point; leaving it alone", file=sys.stderr)
        return False
    os.makedirs(orig, exist_ok=True)
    result = run(["sudo", "mount", "--move", literal_path, orig])
    if result.returncode != 0:
        print(
            f"warning: sudo mount --move {literal_path} {orig} failed "
            f"(exit {result.returncode}); leaving the host mirror where it is",
            file=sys.stderr,
        )
        return False
    return True


def bind_over(dest, literal_path, run=subprocess.run):
    """Mount the writable clone at the path the host repo used to occupy
    (already vacated by move_original), so that path -- which is where the
    agent starts, and what every host-shaped path in its scrollback says --
    is the clone. Warns and returns False, for the same reason
    move_original does."""
    if (
        os.path.exists(dest)
        and os.path.exists(literal_path)
        and os.path.samefile(dest, literal_path)
    ):
        return True
    os.makedirs(literal_path, exist_ok=True)
    result = run(["sudo", "mount", "--bind", dest, literal_path])
    if result.returncode != 0:
        print(
            f"warning: sudo mount --bind {dest} {literal_path} failed "
            f"(exit {result.returncode}); the clone is reachable at {dest} only",
            file=sys.stderr,
        )
        return False
    return True


def shallowest_missing_ancestor(path):
    """The topmost ancestor of path that does not yet exist -- i.e. the
    first directory os.makedirs(path) would have to create -- or None if
    path already exists. Used so a chown after the fact can cover exactly
    what this script created, rather than walking the whole tree back up
    to SANDBOX_HOME (which may be owned by something else entirely, e.g.
    another repo's own chown)."""
    if os.path.exists(path):
        return None
    return shallowest_missing_ancestor(os.path.dirname(path)) or path


def sandbox_ids():
    """(uid, gid) of the user who actually works in this sandbox.

    Not hardcoded 1000: the sandbox's agent uid is whatever the image gave
    it. os.get*id() is only reached off-sandbox (a hand invocation, or a
    unit test on a host with no `agent` user)."""
    try:
        agent = pwd.getpwnam(SANDBOX_USER)
        return agent.pw_uid, agent.pw_gid
    except KeyError:
        return os.getuid(), os.getgid()


def seed_commit_msg_hook(source, dest):
    """Copy the Gerrit `commit-msg` hook from `source`'s own `.git/hooks`
    into a freshly cloned parallel-tree repo at `dest`.

    `git clone` never copies hooks -- they live outside version control --
    so a clone of a Gerrit-backed repo comes up with no commit-msg hook at
    all, and every commit it makes is missing the `Change-Id:` trailer the
    write-commit-msg skill assumes is already there (sbx/NOTES.md, "One
    still-open gap..."). `source` is the engineer's own host mirror (or
    its moved-aside copy), which already has a working hook installed if
    Gerrit access was ever set up on the host -- copying it is simpler and
    needs no network, unlike fetching one fresh from Gerrit.

    Gated on `dest` having a `.gitreview` (a tracked file, so it's already
    there right after the clone if the repo is Gerrit-backed) and `source`
    actually having a hook to copy; does nothing otherwise, the same as a
    plain `git clone` would."""
    if not os.path.isfile(os.path.join(dest, ".gitreview")):
        return
    src_hook = os.path.join(source, ".git", "hooks", "commit-msg")
    if not os.path.isfile(src_hook):
        return
    dst_hook = os.path.join(dest, ".git", "hooks", "commit-msg")
    shutil.copyfile(src_hook, dst_hook)
    shutil.copymode(src_hook, dst_hook)


def clone_into_parallel_tree(literal_path, dest, run=subprocess.run):
    if os.path.exists(dest):
        print(f"{dest} already exists; leaving it alone", file=sys.stderr)
        return
    parent = os.path.dirname(dest)
    chown_root = shallowest_missing_ancestor(parent) or dest
    os.makedirs(parent, exist_ok=True)
    # `--shared`, not `--reference`. Both end up writing the same
    # .git/objects/info/alternates line pointing back at the host's
    # original, but `--reference` *also* runs git's local-clone object
    # copy: hardlinks when it can, and it cannot here, because the
    # original is a virtiofs mount from the host and the parallel tree is
    # the container's own writable layer. Measured on the first real
    # sandbox (2026-09-08): a 685 MB .git per clone, copied across
    # virtiofs, for objects the alternates line already made reachable.
    # `--shared` skips the copy and keeps the alternate.
    #
    # `--shared`'s documented danger -- the source repo pruning objects
    # the clone still needs -- is real here and is handled on the *host*
    # side, not this one. The read-only remount below only stops the
    # *sandbox* from writing; the engineer's own shell can still run `git
    # gc` in the original and collect objects this clone's alternates line
    # depends on. wmf_sbx_remotes.sync_remotes therefore suspends gc
    # (gc.auto=0, gc.pruneExpire=never) in every host repo it registers a
    # sandbox remote in, and restores the saved settings when the last
    # such remote goes away.
    result = run(["git", "clone", "--shared", literal_path, dest])
    if result.returncode != 0:
        raise RuntimeError(
            f"git clone --shared {literal_path} {dest} "
            f"failed (exit {result.returncode})"
        )
    # Before the chown below, not after: it recurses over chown_root, so
    # doing this first means the copied hook file lands owned by the
    # sandbox user along with everything else the clone created, instead
    # of needing a chown of its own.
    seed_commit_msg_hook(literal_path, dest)
    # This script runs as root (see module docstring), so the clone above
    # -- and any ancestor directories os.makedirs just created -- land
    # owned by root, making the checkout unusable (and unwritable) for the
    # "agent" user who actually works in it.
    result = run(["sudo", "chown", "-R", f"{SANDBOX_USER}:{SANDBOX_USER}", chown_root])
    if result.returncode != 0:
        raise RuntimeError(
            f"sudo chown -R {SANDBOX_USER}:{SANDBOX_USER} {chown_root} "
            f"failed (exit {result.returncode})"
        )


def bind_into_parallel_tree(literal_path, dest, run=subprocess.run):
    """The ':ro' opt-out's first step: propagate literal_path into the
    parallel tree via a plain bind mount rather than a git clone, saving a
    second copy of the objects. Still needs a second `remount_readonly`
    call on dest afterward -- `mount --bind -o ro` in one step is silently
    ignored by the kernel (see sbx/DESIGN-parallel-clone-tree.md §2)."""
    if os.path.ismount(dest):
        print(f"{dest} is already a mount point; leaving it alone", file=sys.stderr)
        return
    os.makedirs(dest, exist_ok=True)
    result = run(["sudo", "mount", "--bind", literal_path, dest])
    if result.returncode != 0:
        raise RuntimeError(
            f"sudo mount --bind {literal_path} {dest} failed (exit {result.returncode})"
        )


def remount_readonly(path, run=subprocess.run):
    result = run(["sudo", "mount", "-o", "remount,ro,bind", path])
    if result.returncode != 0:
        raise RuntimeError(
            f"sudo mount -o remount,ro,bind {path} failed (exit {result.returncode})"
        )


def shared_skills_problem(path=None):
    """A string saying the shared skills store is writable, or None.

    Deliberately silent when nothing is mounted at `path`: a plain
    directory there is the container's own filesystem, private to this
    sandbox, and not the cross-sandbox channel this is about. Claiming
    otherwise would be §41's mistake in reverse (readonly_problem makes
    the same distinction, in the same words)."""
    path = path or SHARED_SKILLS_DIR
    if not os.path.ismount(path):
        return None
    return readonly_problem(path, "the shared agent-skills store")


def lock_shared_skills(path=None, run=subprocess.run):
    """Remount the shared agent-skills store read-only, and say what
    happened. Returns True if it is read-only afterwards.

    Every sandbox on the host gets this same directory, so a file written
    here from one sandbox is readable -- and deletable -- from every
    other, including ones created later, and it outlives the `sbx rm` of
    the sandbox that wrote it (MEASURED, sbx/NOTES.md §77.2-§77.4). A
    skill file is instructions an agent reads and acts on, which makes a
    writable shared store a cross-sandbox influence channel between agents
    that are otherwise isolated from each other.

    **This is a second layer, not a boundary**, and for exactly the reason
    SECURITY.md §3 gives for the repo mirrors: the agent has passwordless
    sudo in its own sandbox, so `sudo mount -o remount,rw,bind` lifts this
    in one command (MEASURED, §79: ro holds against an ordinary write,
    and the remount back to rw succeeds on the first try). It stops a
    careless write, an agent that installs a skill without thinking, and a
    prompt-injected one that does not think to try; it stops nothing that
    means it. The fix that *would* be a boundary is host-side and upstream
    -- mount the store `ro`, or per-sandbox -- which is why the remount
    ships alongside the bug report rather than instead of it.

    Never fatal: a sandbox whose skills store could not be locked is worse
    than one where it could, but far better than no sandbox at all."""
    path = path or SHARED_SKILLS_DIR
    if not os.path.ismount(path):
        # Either this sbx doesn't share a skills store (older, or a future
        # one that fixed it) or the directory isn't there at all. Both are
        # fine and neither is ours to report as a problem.
        return True
    if "ro" in (mount_options(path) or []):
        return True  # a restart pass over a sandbox that never stopped
    try:
        remount_readonly(path, run=run)
    except RuntimeError as e:
        print(f"warning: {e}; the shared agent-skills store at {path} stays "
              f"writable, and it is shared with every other sandbox on this "
              f"host (SECURITY.md §7.6)", file=sys.stderr)
        return False
    print(f"{path}: shared agent-skills store remounted read-only",
          file=sys.stderr)
    return True


def setup_repo(host_home, literal_path, is_ro, run=subprocess.run):
    """Puts one repo in place, and returns a layout entry describing what
    it actually did -- `{"literal", "dest", "orig", "mode"}`, where `dest`
    is the parallel path (None for a repo outside host_home, which gets no
    parallel copy of either kind) and `mode` is one of:

      alias    moved aside, cloned, and the clone mounted over the path the
               mirror vacated -- the normal case for a writable repo
      clone    cloned, mirror left at its own path: the fallback when the
               move or the alias didn't take
      bind     ':ro' -- no clone; the parallel path is a second read-only
               bind mount of the original
      inplace  outside host_home: read-only remount and nothing else

    For a writable repo the host mirror is first moved aside with
    `mount --move` and the finished clone is bind-mounted over the path it
    vacated, so `literal_path` -- the agent's starting working directory,
    and the path everything host-side calls this repo -- *is* the clone.
    The parallel tree is untouched by that: the clone is still really at
    `dest`, still what the git daemon serves, still where the alias points.
    See sbx/NOTES.md §39; the aliasing degrades to "no alias" (the old
    behaviour), never to a hidden clone.

    None of these mounts survive `sbx stop` -- restore_mounts redoes them
    from the recorded mode on every container start (§40)."""
    dest = parallel_path(host_home, SANDBOX_HOME, literal_path)
    orig = None if is_ro else originals_path(host_home, literal_path)
    mode = "bind" if is_ro else "inplace"
    if is_ro:
        # No aliasing here, and none wanted: ':ro' asks for the pristine
        # original, and that's exactly what the literal path keeps showing.
        # Lock it down first, then propagate that into the parallel tree --
        # see "Read-only opt-out" in sbx/DESIGN-parallel-clone-tree.md §2.
        remount_readonly(literal_path, run=run)
        if dest is not None:
            bind_into_parallel_tree(literal_path, dest, run=run)
            remount_readonly(dest, run=run)
    elif dest is None:
        remount_readonly(literal_path, run=run)
    else:
        mode = "clone"
        moved = orig is not None and move_original(literal_path, orig, run=run)
        # Clone from wherever the original actually is now. It has to be
        # moved *first*: `--shared` writes an alternates line pointing at
        # the source, so cloning from literal_path and then mounting the
        # clone over it would leave the clone alternating to itself, with
        # the borrowed objects unreachable.
        source = orig if moved else literal_path
        clone_into_parallel_tree(source, dest, run=run)
        remount_readonly(source, run=run)
        if moved and bind_over(dest, literal_path, run=run):
            mode = "alias"
            print(
                f"{literal_path} is now the clone at {dest}; "
                f"the host mirror moved to {orig}",
                file=sys.stderr,
            )
    return {
        "literal": literal_path,
        "dest": dest,
        # Only meaningful for "alias"; recorded either way so a layout
        # entry is readable on its own.
        "orig": orig if mode == "alias" else None,
        "mode": mode,
    }


def layout_path(originals_dir=None):
    return os.path.join(originals_dir or ORIGINALS_DIR, LAYOUT_NAME)


def write_layout(entries, originals_dir=None):
    """Record what setup_repo did to each repo, so the restore pass on the
    next container start can redo exactly that -- and, just as important,
    *not* redo what didn't happen: moving a mirror aside that a fallback
    clone's alternates line still points at would break the clone."""
    path = layout_path(originals_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": LAYOUT_VERSION, "repos": entries}, f, indent=2)
            f.write("\n")
    except OSError as e:
        print(f"warning: could not write {path} ({e}); a restart will not "
              "be able to restore the mounts", file=sys.stderr)
        return False
    return True


def load_layout(originals_dir=None):
    """The layout written by the last setup run, or None if there isn't one
    (this is the first container start, before install has run) or it is
    unreadable/too new to act on."""
    path = layout_path(originals_dir)
    try:
        with open(path, encoding="utf-8") as f:
            layout = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"error: could not read {path} ({e})", file=sys.stderr)
        return None
    if layout.get("version") != LAYOUT_VERSION:
        print(
            f"error: {path} is version {layout.get('version')}, "
            f"expected {LAYOUT_VERSION}",
            file=sys.stderr,
        )
        return None
    return layout.get("repos") or []


def alternates_of(dest):
    """The paths in dest's objects/info/alternates, or [] if it has none.
    These are the load-bearing thing a restart breaks: a clone whose
    alternate has gone missing still *looks* fine (git only complains when
    you ask it to walk history) but has lost every borrowed object --
    measured at 6707 reachable commits before a restart and 5 after
    (sbx/NOTES.md §40)."""
    path = os.path.join(dest, ".git", "objects", "info", "alternates")
    try:
        with open(path, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def unescape_mountinfo(field):
    """mountinfo octal-escapes the four characters that would otherwise
    break its space-separated fields."""
    for escape, char in (("\\040", " "), ("\\011", "\t"),
                         ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(escape, char)
    return field


def mount_options(path, mountinfo=None):
    """The per-mount options of the *topmost* mount at `path`, or None if
    nothing is mounted there. Mounts stack -- §39 deliberately stacks one
    over another -- and the one that decides whether a write succeeds is
    the last one listed."""
    found = None
    try:
        with open(mountinfo or MOUNTINFO, encoding="utf-8") as f:
            for line in f:
                fields = line.split(" ")
                if len(fields) > 5 and unescape_mountinfo(fields[4]) == path:
                    found = fields[5].split(",")
    except OSError:
        return None
    return found


def readonly_problem(path, what):
    """A string saying `path` is writable when it shouldn't be, or None.

    This is §40's fourth finding made checkable: the read-only lock-down
    on a host mirror is a `remount,ro,bind` in the container's mount
    namespace, so a restart that re-made the mount without it leaves the
    engineer's real checkout writable from inside the sandbox -- and
    nothing about the path, its owner or its permissions shows it. Only
    the mount options do."""
    options = mount_options(path)
    if options is None or "ro" in options:
        # Not in mountinfo at all: either nothing is mounted there (the
        # callers check that separately, and say so in their own words) or
        # /proc isn't readable. Neither is evidence of a writable mount,
        # and inventing the stronger claim would be the §41 mistake in
        # reverse.
        return None
    return (f"{path} is mounted {','.join(options)}, so {what} is writable "
            f"from inside the sandbox")


def verify_repo(entry):
    """Check the post-conditions of one restored repo and return the
    problems as strings.

    The restore pass is otherwise silent on success, which is precisely
    what made its first real failure unreadable: exit 0, an empty problem
    list, and a sandbox with none of its mounts (§41). Nothing here
    re-does any work -- it only asserts what should now be true, so the
    status file says which half went wrong."""
    problems = []
    literal, dest, mode = entry.get("literal"), entry.get("dest"), entry.get("mode")
    if mode == "inplace":
        # No parallel copy and no clone: the whole of this mode is the
        # read-only remount, so it is also the whole of the check.
        return [p for p in [readonly_problem(literal, "the host directory")] if p]
    if dest is None or not os.path.isdir(dest):
        return problems
    if mode == "alias":
        orig = entry.get("orig")
        if not (os.path.exists(literal) and os.path.samefile(dest, literal)):
            problems.append(f"{literal} is not the clone at {dest}")
        if orig and not os.path.ismount(orig):
            problems.append(f"{orig} is not a mount point (the host mirror was not moved aside)")
        elif orig:
            problems.append(readonly_problem(orig, "the host mirror"))
    elif mode == "clone":
        # The mirror never moved, so the literal path *is* the mirror.
        problems.append(readonly_problem(literal, "the host mirror"))
    elif mode == "bind":
        if not os.path.ismount(dest):
            problems.append(f"{dest} is not a mount point")
        problems.append(readonly_problem(literal, "the host mirror"))
        problems.append(readonly_problem(dest, "its parallel-tree copy"))
    for alternate in alternates_of(dest):
        if not os.path.isdir(alternate):
            problems.append(
                f"{dest} borrows objects from {alternate}, which does not exist"
            )
    return [p for p in problems if p]


def restore_repo(entry, run=subprocess.run):
    """Redo one repo's mounts after a container restart. Idempotent, and
    raises RuntimeError only for the things that should stop the repo (a
    failed remount); an alias that can't be re-established warns, exactly
    as it does at setup time."""
    literal, dest, mode = entry.get("literal"), entry.get("dest"), entry.get("mode")
    if mode == "inplace":
        print(f"{literal}: remounting the host mirror read-only", file=sys.stderr)
        remount_readonly(literal, run=run)
        return
    if dest is None or not os.path.isdir(dest):
        # Nothing was ever put there -- the very first container start
        # runs the startup commands before install has cloned anything.
        print(f"{literal}: nothing at {dest} yet, skipping", file=sys.stderr)
        return
    print(f"{literal}: restoring ({mode})", file=sys.stderr)
    if mode == "bind":
        remount_readonly(literal, run=run)
        bind_into_parallel_tree(literal, dest, run=run)
        remount_readonly(dest, run=run)
    elif mode == "clone":
        # The mirror stayed at its own path, and the clone's alternates
        # line points at it. Nothing to move; it just came back writable.
        remount_readonly(literal, run=run)
    elif mode == "alias":
        orig = entry.get("orig")
        if os.path.exists(dest) and os.path.exists(literal) and os.path.samefile(dest, literal):
            print(f"{literal}: already the clone; nothing to do", file=sys.stderr)
            return  # already restored -- a second start with no stop
        if move_original(literal, orig, run=run):
            # Before the alias, so a failure here can't be masked by one.
            remount_readonly(orig, run=run)
            bind_over(dest, literal, run=run)
    else:
        print(f"warning: unknown layout mode {mode!r} for {literal}", file=sys.stderr)


def restore_mounts(entries, run=subprocess.run, verify=verify_repo):
    """Redo every repo's mounts, keeping going past a repo that fails, and
    check each one's post-conditions afterwards. Returns an exit status."""
    status = 0
    for entry in entries:
        try:
            restore_repo(entry, run=run)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            status = 1
            continue
        for problem in verify(entry):
            print(f"error: {problem}", file=sys.stderr)
            status = 1
    return status


def run_restore(argv, run=subprocess.run):
    """`wmf-sbx-setup --restore` -- the kit's `setup.startup` entry (see
    wmf_sbx_kit.restore_startup_command). Everything setup_repo mounted is
    namespace state that `sbx stop` throws away (sbx/NOTES.md §40): the
    clones survive, but their `--shared` alternates point into
    .sbx-originals, which is empty again, and the host mirrors come back at
    their own paths -- *writable*, for any repo sbx itself mounted rw."""
    # The log appends, so every run has to say which one it is; without
    # this, three starts' worth of identical narration is one paragraph.
    print(f"=== --restore {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===",
          file=sys.stderr)
    # Before the layout, and before the early return below: the skills
    # store is a mount like any other, so `sbx stop` drops the read-only
    # remount exactly the way it drops the repos' -- and unlike the repos,
    # it is there to be re-locked on the very first container start, when
    # there is no layout yet.
    lock_shared_skills(run=run)
    entries = load_layout(argv[0] if argv else None)
    if entries is None:
        print(f"no layout to restore at {layout_path(argv[0] if argv else None)}",
              file=sys.stderr)
        return 0
    print(f"restoring {len(entries)} repo(s) from "
          f"{layout_path(argv[0] if argv else None)}", file=sys.stderr)
    return restore_mounts(entries, run=run)


def parse_verify_argv(argv):
    """(originals_dir, wait_seconds) for `--verify`. Hand-parsed: this
    script is injected into the sandbox as a single file and its argument
    handling is positional everywhere else."""
    originals, wait = None, 0.0
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--wait":
            value = rest.pop(0) if rest else str(DEFAULT_VERIFY_WAIT)
        elif arg.startswith("--wait="):
            value = arg.split("=", 1)[1]
        else:
            originals = arg
            continue
        try:
            wait = float(value)
        except ValueError:
            raise RuntimeError(f"--wait wants a number of seconds, not {value!r}")
    return originals, wait


def verify_problems(entries, verify=verify_repo):
    """[(entry, [problem, ...]), ...] -- every repo, checked once."""
    return [(entry, verify(entry)) for entry in entries]


def run_verify(argv, verify=verify_repo, sleep=time.sleep, clock=time.monotonic,
               skills=shared_skills_problem):
    """`wmf-sbx-setup --verify [DIR] [--wait SECONDS]` -- assert the mount
    layout without touching it, and say so repo by repo.

    `--restore` already verifies what it restored, but only along the path
    that runs it. This is the same check for every *other* way into a
    sandbox: a plain `sbx run`, an `sbx exec`, a start that `wmf-sbx-resume`
    never saw. It answers the two questions the ad-hoc probes in §40 and
    §42 were spelled out by hand to answer -- is the literal path the
    clone, and is the host mirror read-only -- plus the one §77.3 added,
    is the shared agent-skills store still locked; so that answer stops
    depending on getting a shell one-liner right, and so a mirror that
    came back writable is a reported error rather than something you have
    to think to look for. Reads only: no mounts, no root, no /var/log.

    `--wait` exists because the honest answer to "are the mounts up?" on a
    container that started a moment ago is "not yet" (§46): the startup
    dispatcher restores them, but it does not block the `sbx exec` that
    triggered the start, so a check racing it loses. Polling here rather
    than host-side keeps it to one `sbx exec`, and makes "wait, then
    restore only if it's still wrong" the cheap thing for a caller to do
    -- which is what keeps two `--restore` passes from doing `mount
    --move` at each other."""
    try:
        originals, wait = parse_verify_argv(argv)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    entries = load_layout(originals)
    if entries is None:
        print(f"no layout to verify at {layout_path(originals)}", file=sys.stderr)
        return 1
    print(f"verifying {len(entries)} repo(s) from {layout_path(originals)}",
          file=sys.stderr)
    deadline = clock() + wait
    checked = verify_problems(entries, verify=verify)
    shared = skills()
    while (shared or any(problems for _entry, problems in checked)) and clock() < deadline:
        sleep(min(VERIFY_POLL_SECONDS, max(0.0, deadline - clock())))
        checked = verify_problems(entries, verify=verify)
        shared = skills()
    status = 0
    if shared:
        # Not a repo, so it is reported on its own -- but through the same
        # exit status, because a caller that waits on the mounts is waiting
        # on the same startup pass that locks this.
        print(f"error: {shared}", file=sys.stderr)
        status = 1
    for entry, problems in checked:
        literal, dest, mode = entry.get("literal"), entry.get("dest"), entry.get("mode")
        if problems:
            status = 1
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
        elif mode != "inplace" and (dest is None or not os.path.isdir(dest)):
            # verify_repo has nothing to say about a repo that was never
            # put in place; for a human running this by hand, "nothing
            # here yet" and "all good" must not look the same.
            print(f"{literal}: nothing at {dest} yet", file=sys.stderr)
        else:
            print(f"{literal}: ok ({mode})", file=sys.stderr)
    if status and wait:
        print(f"error: still wrong after {wait:g}s", file=sys.stderr)
    return status


def deep_merge(base, patch):
    """`patch` merged into `base`, returning a new object.

    Dicts merge key by key. Lists are **unioned** -- base order first,
    then anything new -- rather than replaced. That is unusual enough to
    state: the only list the kit patches is `permissions.deny`, where
    replacing would silently drop a deny the engineer or a future sbx
    added, and where a duplicate entry is harmless. Scalars are replaced.
    """
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, value in patch.items():
            merged[key] = deep_merge(base.get(key), value) if key in base else value
        return merged
    if isinstance(base, list) and isinstance(patch, list):
        return base + [item for item in patch if item not in base]
    return patch


def merge_settings(path, patch, run=subprocess.run):
    """Deep-merge `patch` into the JSON file at `path`, creating it if it
    is not there. Returns True if the file now holds the merged result.

    Idempotent, which it has to be: this runs once at install and again
    on every container start (see wmf_sbx.kit.settings_merge_argv, which
    is spelled into both halves of the kit's `setup:` block).
    Nothing is written when the merge changes nothing, so a restart
    doesn't churn the file's mtime.

    A file that is there but unreadable or not JSON is left alone and
    reported. Overwriting it would throw away whatever sbx or the
    engineer put there, and the permission denies this adds are a second
    layer of defense -- worth having, never worth that.
    """
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError) as e:
            print(f"error: could not read {path} ({e}); leaving it alone",
                  file=sys.stderr)
            return False
        if not isinstance(existing, dict):
            print(f"error: {path} is not a JSON object; leaving it alone",
                  file=sys.stderr)
            return False
    merged = deep_merge(existing, patch)
    if merged == existing:
        print(f"{path} already has the kit's settings", file=sys.stderr)
        return True
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
    except OSError as e:
        print(f"error: could not write {path} ({e})", file=sys.stderr)
        return False
    # The install step runs as root (setup.install's default user), so
    # without this the agent's own settings.json ends up root-owned and
    # Claude Code cannot update it. The startup run is already the agent,
    # where this is a no-op.
    result = run(["sudo", "chown", f"{SANDBOX_USER}:{SANDBOX_USER}", path])
    if result.returncode != 0:
        print(f"warning: could not chown {path} (exit {result.returncode})",
              file=sys.stderr)
    print(f"merged the kit's settings into {path}", file=sys.stderr)
    return True


def run_exec_bits(argv):
    """`wmf-sbx-setup --exec-bits PATH [PATH ...]` -- restate mode 0755 on
    the files the kit ships as *programs*.

    `write_kit_dir` already chmods them 0755 in the staging tree, but what
    puts them in the sandbox is sbx's own `files/home` copy, and the bit
    does not appear to survive it: a real create left
    ~/.claude/plugins/.../bin/session-start.sh unexecutable, so every
    session started with

        SessionStart:startup hook error
        /bin/sh: 1: .../bin/session-start.sh: Permission denied

    and the agent lost its whole orientation block (sbx/NOTES.md §71).
    The same drop ships ~/.local/bin/wmf-sbx-mcp-proxy, which Claude Code
    spawns directly, with no interpreter in front of it.

    So this is the `install -m 0755` of install_helper_scripts, one layer
    up: don't depend on another tool's mode fidelity for a file that has
    to run -- say the mode in the sandbox, where we control it.

    A path that isn't there is a warning, not a failure: kits vary (a
    --no-mcp sandbox has no proxy), and a create is not worth failing over
    a chmod. A chmod that *fails* is an error, because then something is
    genuinely wrong with the drop.

    The summary line is deliberate. It is the only place that records
    whether sbx still strips the bit, so a future create's log answers the
    question without anyone re-deriving it."""
    if not argv:
        print("error: --exec-bits wants at least one path", file=sys.stderr)
        return 1
    changed = fine = 0
    status = 0
    for path in argv:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as e:
            print(f"warning: {path} is not in this sandbox ({e}); not chmodding it",
                  file=sys.stderr)
            continue
        if mode & 0o111 == 0o111:
            fine += 1
            continue
        try:
            os.chmod(path, 0o755)
        except OSError as e:
            print(f"error: could not make {path} executable ({e})", file=sys.stderr)
            status = 1
            continue
        print(f"made {path} executable (was {mode:04o})", file=sys.stderr)
        changed += 1
    print(f"exec bits: {changed} restored, {fine} already executable",
          file=sys.stderr)
    return status


def run_settings(argv, run=subprocess.run):
    """`wmf-sbx-setup --settings PATCH.json [SETTINGS.json]` -- the step
    that tells Claude Code the wmf-claude plugin is enabled and ports the
    parent package's permission denies (sbx/DESIGN-plugin-integration.md
    §3, Route A).

    It is a merge rather than a `files/home/` drop because sbx writes
    ~/.claude/settings.json itself and a static file would replace it
    wholesale -- taking `permissions.defaultMode: bypassPermissions` with
    it, which would leave the agent asking for approval it has no terminal
    to get (sbx/NOTES.md §55.1).

    The patch travels as data in the kit rather than as a here-doc in the
    command, so `settings.json` is inspectable next to the file it
    patches and the spec stays readable."""
    if not argv:
        print("error: --settings wants the path of a JSON patch file",
              file=sys.stderr)
        return 1
    patch_path = argv[0]
    target = argv[1] if len(argv) > 1 else SANDBOX_SETTINGS_FILE
    try:
        with open(patch_path, encoding="utf-8") as f:
            patch = json.load(f)
    except (OSError, ValueError) as e:
        print(f"error: could not read the settings patch {patch_path} ({e})",
              file=sys.stderr)
        return 1
    if not isinstance(patch, dict):
        print(f"error: {patch_path} is not a JSON object", file=sys.stderr)
        return 1
    return 0 if merge_settings(target, patch, run=run) else 1


def registered_mcp_servers(path=SANDBOX_CLAUDE_JSON):
    """The user-scope `mcpServers` object out of ~/.claude.json, or {}.

    A read is all this is: it decides whether `claude mcp add` has
    anything to do, which is cheaper than `claude mcp get` (that starts
    the server to health-check it, once per server, on every container
    start). Unreadable or malformed reads as empty, which costs an
    already-registered server one failed `add` saying so -- the opposite
    failure, refusing to register anything because the file looked odd,
    would leave the sandbox with no MCP at all.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def mcp_entry_matches(entry, wanted):
    """True when what is registered is already what the kit wants.

    `claude mcp add` stores `{type, command, args, env}`; only the first
    three are ours. `args` carries the `--tools` allowlist, so a stale
    one is precisely the case that must be re-registered rather than
    left alone -- a shortened allowlist is a policy change."""
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("type", "stdio") == "stdio"
        and entry.get("command") == wanted.get("command")
        and list(entry.get("args") or []) == list(wanted.get("args") or [])
    )


def claude_executable():
    """The `claude` to run, absolute where we can manage it.

    The agent's own install comes first: this is usually invoked through
    `sudo -u agent`, whose PATH is sudo's `secure_path` and not the
    agent's, so a bare `claude` is not found at all (SANDBOX_CLAUDE_BIN).
    `which` is the fallback for anywhere that isn't a sandbox built from
    this image -- the unit tests, notably."""
    if os.access(SANDBOX_CLAUDE_BIN, os.X_OK):
        return SANDBOX_CLAUDE_BIN
    return shutil.which("claude") or "claude"


def claude_mcp(args, run=subprocess.run, root=None):
    """`claude mcp ...`, as the agent whose ~/.claude.json it is.

    The install-time run is root (setup.install's default user), and
    `claude` as root would write the registration into /root/.claude.json,
    where the agent never sees it. The startup run is already the agent,
    where the sudo would be a fork for nothing -- hence the euid test."""
    argv = [claude_executable(), "mcp"] + list(args)
    if root is None:
        root = os.geteuid() == 0
    if root:
        return as_agent(argv, run=run)
    return run(argv)


def register_mcp_servers(config, path=SANDBOX_CLAUDE_JSON, run=subprocess.run):
    """Make the user-scope MCP registrations match `config` (the
    `{name: {command, args}}` mapping wmf_sbx.kit.mcp_config wrote into
    the kit). Returns True when every server in it is registered.

    Each entry points at the in-sandbox wmf-sbx-mcp-proxy, which speaks to
    the *host's* real servers through sbx's MCP gateway: the credentials
    those servers need stay on the host, which is the whole reason the
    servers are not simply installed in here (sbx/NOTES.md §60-62,
    DESIGN-plugin-integration.md §5 step 4).

    Idempotent by the same dual-run logic as merge_settings: this runs at
    install and again on every container start. `claude mcp add` refuses a
    name that is taken (exit 1), so a changed entry is removed first --
    there is no `claude mcp set`."""
    existing = registered_mcp_servers(path)
    ok = True
    for name in sorted(config):
        wanted = config[name]
        if mcp_entry_matches(existing.get(name), wanted):
            print(f"the {name} MCP server is already registered", file=sys.stderr)
            continue
        if name in existing:
            # Failure here is not worth reporting on its own: the add
            # right below says something far more useful if this mattered.
            claude_mcp(["remove", "--scope", "user", name], run=run)
        result = claude_mcp(
            ["add", "--scope", "user", name, "--", wanted["command"]]
            + list(wanted.get("args") or []),
            run=run,
        )
        if result.returncode != 0:
            print(f"error: could not register the {name} MCP server "
                  f"(exit {result.returncode})", file=sys.stderr)
            ok = False
        else:
            print(f"registered the {name} MCP server", file=sys.stderr)
    return ok


def run_mcp(argv, run=subprocess.run):
    """`wmf-sbx-setup --mcp SERVERS.json [CLAUDE.json]` -- the step that
    points Claude Code at the host's Phabricator and Gerrit MCP servers
    through the proxy the kit installed on PATH.

    Like `--settings`, the registrations travel as data in the kit rather
    than as a here-doc in the spec's command, so the `--tools` allowlist
    that is the policy decision here (gerrit serves 20 tools, 15 of which
    write under the engineer's credential) is a readable file in the kit
    next to the proxy that enforces it."""
    if not argv:
        print("error: --mcp wants the path of a JSON registration file",
              file=sys.stderr)
        return 1
    config_path = argv[0]
    target = argv[1] if len(argv) > 1 else SANDBOX_CLAUDE_JSON
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        print(f"error: could not read the MCP registrations {config_path} ({e})",
              file=sys.stderr)
        return 1
    if not isinstance(config, dict):
        print(f"error: {config_path} is not a JSON object", file=sys.stderr)
        return 1
    return 0 if register_mcp_servers(config, path=target, run=run) else 1


def link_into_core(core_path, links, core_readonly=False):
    """Symlink each extension/skin's parallel clone into the core clone's
    extensions/ or skins/, so `wfLoadExtension( 'Cite' )` finds it. Returns
    the number of links made or refreshed.

    links: [(link_dir, link_name, target), ...] -- link_dir is 'extensions'
    or 'skins', link_name is the *directory* name to use (which is not the
    manifest's `name` field; see sbx/DESIGN-dependency-walk.md §1), and
    target is the repo's work_path.

    The target is the path the agent and the engineer both work in
    (/home/cananian/Projects/Wikimedia/Extensions/Cite), not the
    /home/agent alias of it: these symlinks are files on disk that outlive
    the sandbox's mounts, and a `ls -l extensions/` that names a directory
    the engineer has never heard of is a puzzle for no gain. They resolve
    to the writable clone, because the alias mount makes the literal path
    *be* the clone (see setup_repo and work_path). The one window where
    that is not yet true is a container that has started but not yet run
    `--restore`; §40's startup step closes it, and nothing loads the wiki
    before then. See sbx/NOTES.md §63.1.

    Never fatal: a symlink that can't be made is warned about and skipped.
    The rest of the setup (and the git daemon the host fetches through) is
    worth more than an exact `extensions/` directory.
    """
    if not links:
        return 0
    if core_path is None:
        print(
            "no mediawiki/core clone in this sandbox; skipping the "
            "extension/skin symlinks",
            file=sys.stderr,
        )
        return 0
    if core_readonly:
        # A ':ro' core is the host's own tree, mounted read-only. Writing
        # into it is both impossible and wrong.
        print(
            f"{core_path} is a read-only bind mount of the host's core; "
            "skipping the extension/skin symlinks (drop the ':ro' on core "
            "to get them)",
            file=sys.stderr,
        )
        return 0

    linked = 0
    for link_dir, link_name, target in links:
        parent = os.path.join(core_path, link_dir)
        dest = os.path.join(parent, link_name)
        try:
            os.makedirs(parent, exist_ok=True)
            if os.path.islink(dest):
                # Idempotent: wmf-sbx-resume runs this again, and a link
                # pointing somewhere else is stale, not sacred.
                if os.readlink(dest) == target:
                    continue
                os.unlink(dest)
            elif os.path.exists(dest):
                print(
                    f"warning: {dest} already exists and is not a symlink; "
                    f"leaving it alone (not linking {target}). That is either "
                    f"a real checkout or a name collision -- either way, "
                    f"clobbering it would destroy work.",
                    file=sys.stderr,
                )
                continue
            os.symlink(target, dest)
        except OSError as e:
            print(f"warning: could not link {dest} -> {target} ({e})", file=sys.stderr)
            continue
        # This script runs as root, so the link lands owned by root. Chown
        # the link *itself*, not the (already agent-owned) clone it points
        # at -- hence lchown, and hence not `sudo chown -h`, which is what
        # this used to be and which silently left every link root-owned in
        # the first real sandbox (2026-09-08). No subprocess, no PATH, no
        # sudo, and a real exception when it fails.
        try:
            os.lchown(dest, *sandbox_ids())
        except OSError as e:
            print(f"warning: could not chown {dest} ({e})", file=sys.stderr)
        print(f"linked {link_dir}/{link_name} -> {target}", file=sys.stderr)
        linked += 1
    return linked


def install_helper_scripts(run=subprocess.run, source_dir=None,
                           dest_dir=HELPER_INSTALL_DIR):
    """Put the kit's helper scripts on PATH. Returns the names installed.

    Never fatal: without them `git safe-reset`, `mw-install-browser` and
    `mw-install-cypress` just aren't available, which costs the reset step
    and the browser suites and nothing else.

    source_dir defaults in the body, not in the signature. A default in the
    signature holds the value HELPER_SOURCE_DIR had at import time. A test
    that moves ~/bin then reads the real home of the machine that runs
    it."""
    if source_dir is None:
        source_dir = HELPER_SOURCE_DIR
    installed = []
    for name in HELPER_SCRIPTS:
        src = os.path.join(source_dir, name)
        if not os.path.exists(src):
            print(
                f"warning: {src} is missing from this kit; {name} will not "
                f"be available in this sandbox",
                file=sys.stderr,
            )
            continue
        result = run(["install", "-m", "0755", src, os.path.join(dest_dir, name)])
        if result.returncode != 0:
            print(
                f"warning: could not install {name} into {dest_dir} "
                f"(exit {result.returncode})",
                file=sys.stderr,
            )
            continue
        installed.append(name)
    return installed


def as_agent(argv, cwd=None, run=subprocess.run):
    """Run argv as SANDBOX_USER with its own $HOME.

    -H is load-bearing, not tidiness: this script runs as root, and
    without it git, composer, and npm all write their caches and config
    into /root, and the files they create in the clone come out root-owned
    -- undoing clone_into_parallel_tree's chown and leaving a checkout the
    agent can't write."""
    return run(["sudo", "-u", SANDBOX_USER, "-H"] + argv, cwd=cwd)


def configure_remotes(repo_path, upstream_url, run=subprocess.run):
    """Make the clone's remotes mean what an engineer expects them to mean,
    and return the remote name git-safe-reset should reset against.

    `git clone` named the host's checkout `origin`, because that is what we
    cloned from. But inside the sandbox `origin` should mean what it means
    everywhere else -- the project's real upstream on Gerrit or GitLab --
    so `git safe-reset` resets to upstream master rather than to whatever
    branch the engineer's own host checkout happened to be sitting on. So:
    rename the mirror to `local`, and install the true upstream as
    `origin`. `local` stays around deliberately; it is the fast, offline
    way to pick up something the host has and upstream doesn't.

    https, not ssh://: the sandbox has no SSH agent by design (see
    sbx/NOTES.md §8), so anonymous http is the only fetch that can work.
    Pushing from inside the sandbox is not a thing -- the host fetches from
    the sandbox's git daemon.

    Falls back to `local` (and says so) whenever the swap can't be
    completed -- an unknown forge, no network at install time. A sandbox
    matching the host is worse than one matching upstream, but it is much
    better than a failed create.
    """
    if not upstream_url:
        # No canonical, or a forge wmf_sbx_create can't build a URL for.
        # `origin` is still the host mirror; leave the names alone rather
        # than inventing a `local` that means something different here
        # than it does in every other clone in the tree.
        return UPSTREAM_REMOTE

    result = as_agent(
        ["git", "remote", "rename", UPSTREAM_REMOTE, MIRROR_REMOTE], cwd=repo_path, run=run
    )
    if result.returncode != 0:
        print(
            f"warning: could not rename {UPSTREAM_REMOTE} to {MIRROR_REMOTE} in "
            f"{repo_path}; leaving it pointing at the host mirror",
            file=sys.stderr,
        )
        return UPSTREAM_REMOTE

    result = as_agent(
        ["git", "remote", "add", UPSTREAM_REMOTE, upstream_url], cwd=repo_path, run=run
    )
    if result.returncode != 0:
        print(
            f"warning: could not add {UPSTREAM_REMOTE} {upstream_url} in {repo_path}; "
            f"the host mirror is available as {MIRROR_REMOTE}",
            file=sys.stderr,
        )
        return MIRROR_REMOTE

    # Cheap despite appearances: the clone's alternates line already makes
    # every object the host had reachable, so this transfers only what
    # upstream has moved on by.
    result = as_agent(["git", "fetch", "--quiet", UPSTREAM_REMOTE], cwd=repo_path, run=run)
    if result.returncode != 0:
        print(
            f"warning: could not fetch {UPSTREAM_REMOTE} ({upstream_url}) in {repo_path}; "
            f"falling back to {MIRROR_REMOTE}",
            file=sys.stderr,
        )
        reset_remote = MIRROR_REMOTE
    else:
        reset_remote = UPSTREAM_REMOTE

    # Now that two remotes both carry a `master`, a clone whose HEAD was a
    # topic branch has no local `master` for `git checkout master` to find
    # and git refuses to guess: "fatal: 'master' matched multiple (2)
    # remote tracking branches". That is git-safe-reset's first command,
    # and it is what silently skipped the reset of a Parsoid clone parked
    # on a topic branch (sbx/NOTES.md §31.2). This config is git's own
    # suggested remedy: it makes the DWIM branch track reset_remote.
    as_agent(
        ["git", "config", "checkout.defaultRemote", reset_remote], cwd=repo_path, run=run
    )
    return reset_remote


def git_safe_reset(repo_path, remote=UPSTREAM_REMOTE, run=subprocess.run):
    """Reset a dependency clone to `remote`'s master. Returns True on
    success.

    The remote is passed explicitly rather than left to git-safe-reset's
    own `origin` default, because which remote is the right answer here
    depends on whether configure_remotes managed to install the real
    upstream: `origin` means Gerrit when it did, and the host mirror when
    it didn't.

    --force skips git-safe-reset's git-review-check gate. That check exists
    to stop you destroying your own unpushed work; in a clone made seconds
    ago from a read-only original there is none to destroy, so any unpushed
    commit the host happened to have in a *dependency* would otherwise
    abort setup for nothing. The dirty-tree guard stays live and is worth
    keeping: it can only fire on a re-run, which is exactly when it should.
    """
    result = as_agent(["git", "safe-reset", "--force", remote], cwd=repo_path, run=run)
    return result.returncode == 0


def repos_to_leave_alone(plan):
    """The literal host paths whose clones must keep the branch and commit
    the host was sitting on, rather than being reset to upstream master.

    Every repo the engineer named on the command line, not just the
    primary: naming a repo is how you say "this one is what I'm here to
    work on", and `git clone` already landed us on exactly the host's
    branch. Repos that arrived through the dependency walk carry no
    `requested` flag and do get reset -- they're there to build against,
    and upstream master is the right thing to build against.

    `resetAll` (wmf-sbx-create --reset-all) empties this -- primary
    included, since "all" that exempted one repo would be a trap: reset the
    lot, then hand-pick from inside the couple you want back on their host
    branch. See sbx/DESIGN-setup-steps.md §8.1.
    """
    if plan.get("resetAll"):
        return set()
    keep = {r["path"] for r in plan.get("repos", []) if r.get("requested")}
    primary = plan.get("primary")
    if primary:
        # Belt and braces: a plan that somehow carries no `requested` flags
        # should still not throw away the primary's work in progress.
        keep.add(primary)
    return keep


def composer_update(repo_path, run=subprocess.run):
    """True/False for a repo with a composer.json, None for one without.

    Run in the primary too, unlike git_safe_reset: this touches nothing but
    vendor/ (composer.lock is gitignored in core), so there is no work in
    progress for it to disturb -- and when the primary *is* mediawiki/core,
    skipping it would leave no vendor/ at all and the install would fail
    outright. See sbx/DESIGN-setup-steps.md §3."""
    if not os.path.exists(os.path.join(repo_path, "composer.json")):
        return None
    result = as_agent(
        ["composer", "update", "--no-interaction", "--no-progress"], cwd=repo_path, run=run
    )
    return result.returncode == 0


def composer_local_contents(parsoid_checkout=False):
    """Core's composer.local.json as a JSON string. Tab indent, as for
    the api-testing config: core's eslint lints every JSON file.

    parsoid_checkout: True when Parsoid is a workspace clone. Only then
    does the file exclude the vendor copy of Parsoid from the classmap."""
    config = {}
    if parsoid_checkout:
        config["autoload"] = {"exclude-from-classmap": COMPOSER_LOCAL_EXCLUDE_FROM_CLASSMAP}
    config["extra"] = {"merge-plugin": {"include": COMPOSER_LOCAL_INCLUDE}}
    return json.dumps(config, indent="\t") + "\n"


def write_composer_local(core_path, parsoid_checkout=False, run=subprocess.run):
    """Write <core>/composer.local.json, as quibble does. True when the
    file holds our content afterward, False when it does not.

    Runs after link_into_core and before core's composer update: the
    globs find the extensions through the symlinks, and the update is what
    installs their packages into core's vendor/. Without this, a library
    that only an extension requires (Translate's mustangostang/spyc) is in
    that extension's own vendor/ alone, and phan, which reads core's
    vendor/, reports its classes as undeclared.

    Each extension keeps its own composer update too, as in CI: its
    `composer test` and `composer phan` run from its own vendor/bin.

    Like .env, an edited copy is left alone: an engineer's own
    composer.local.json can hold other merges they need. The form for the
    other value of parsoid_checkout is ours, not an edit, so it is
    replaced."""
    path = os.path.join(core_path, COMPOSER_LOCAL_NAME)
    contents = composer_local_contents(parsoid_checkout)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            existing = None
        if existing == contents:
            return True
        if existing == composer_local_contents(not parsoid_checkout):
            with open(path, "w", encoding="utf-8") as f:
                f.write(contents)
            print(f"wrote {path}", file=sys.stderr)
            return True
        print(
            f"{path} differs from the template; leaving it alone. Extension "
            f"packages that it does not merge are missing from core's "
            f"vendor/, and phan reports their classes as undeclared",
            file=sys.stderr,
        )
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(contents)
    result = run(["sudo", "chown", f"{SANDBOX_USER}:{SANDBOX_USER}", path])
    if result.returncode != 0:
        print(f"warning: could not chown {path} (exit {result.returncode})", file=sys.stderr)
    print(f"wrote {path}", file=sys.stderr)
    return True


def parse_install_params(core_path, fallback=None):
    """--server / --scriptpath / --pass as core's own composer.json
    mw-install:sqlite script gives them, so the .env we write can't drift
    from the wiki we install (sbx/DESIGN-setup-steps.md §4.2). Anything
    unparseable falls back to `fallback`, then to DEFAULT_INSTALL_PARAMS."""
    params = dict(DEFAULT_INSTALL_PARAMS)
    params.update(fallback or {})
    try:
        with open(os.path.join(core_path, "composer.json"), encoding="utf-8") as f:
            scripts = (json.load(f).get("scripts") or {})
    except (OSError, ValueError):
        return params
    script = scripts.get(INSTALL_SCRIPT_NAME)
    if isinstance(script, list):
        script = " ".join(str(line) for line in script)
    if not isinstance(script, str):
        return params
    try:
        tokens = shlex.split(script)
    except ValueError:
        return params
    for i, token in enumerate(tokens):
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        for flag, key in (("--server", "server"), ("--scriptpath", "scriptPath"),
                          ("--pass", "adminPassword")):
            if token.startswith(flag + "="):
                params[key] = token[len(flag) + 1:]
            elif token == flag and following is not None:
                params[key] = following
    return params


def env_file_contents(params, uid, gid):
    server = params.get("server") or DEFAULT_INSTALL_PARAMS["server"]
    # MW_DOCKER_PORT has to agree with MW_SERVER; the install's --server is
    # the only place the port is stated.
    port = urllib.parse.urlsplit(server).port or (443 if server.startswith("https") else 80)
    return ENV_TEMPLATE.format(
        # `/`, not the install's own empty --scriptpath=: this is a docroot
        # install, and the harnesses that read MW_SCRIPT_PATH reject the
        # empty string (wdio-mediawiki does, though core's Gruntfile calls
        # it valid). Quibble exports the same `/`. The install keeps the
        # empty value; only what the harnesses read changes. See
        # sbx/DESIGN-testing-instructions.md §2.6 and §5.4.
        script_path=params.get("scriptPath") or "/",
        server=server,
        port=port,
        user=params.get("adminUser") or DEFAULT_INSTALL_PARAMS["adminUser"],
        password=params.get("adminPassword") or DEFAULT_INSTALL_PARAMS["adminPassword"],
        uid=uid,
        gid=gid,
    )


def write_env_file(core_path, params, uid=None, gid=None, run=subprocess.run):
    """Write <core>/.env. Returns True if the file holds our content
    afterward, False if an engineer's edited copy was left in place.

    .env is gitignored in core, so this never dirties the tree."""
    if uid is None or gid is None:
        agent_uid, agent_gid = sandbox_ids()
        uid = agent_uid if uid is None else uid
        gid = agent_gid if gid is None else gid
    path = os.path.join(core_path, ".env")
    contents = env_file_contents(params, uid, gid)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            existing = None
        if existing == contents:
            return True
        # Someone edited it, which under wmf-sbx-resume means they meant to.
        print(f"{path} differs from the template; leaving it alone", file=sys.stderr)
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(contents)
    result = run(["sudo", "chown", f"{SANDBOX_USER}:{SANDBOX_USER}", path])
    if result.returncode != 0:
        print(f"warning: could not chown {path} (exit {result.returncode})", file=sys.stderr)
    print(f"wrote {path}", file=sys.stderr)
    return True


def read_secret_key(core_path):
    """$wgSecretKey out of <core>/LocalSettings.php, or None.

    None whenever the value cannot be read: the caller warns and skips the
    file, because a config with a wrong secret_key is worse than no config
    at all -- the tests then fail at login with nothing to point at."""
    try:
        with open(os.path.join(core_path, "LocalSettings.php"), encoding="utf-8") as f:
            contents = f.read()
    except OSError:
        return None
    match = _SECRET_KEY_RE.search(contents)
    return match.group(1) if match else None


def api_testing_config_contents(params, secret_key, indent="\t"):
    """The api-testing config as a JSON string, one key per field
    `api-testing/lib/config.js` reads.

    base_uri is the server root with a trailing slash: the library strips
    the slash and appends `api.php` or `rest.php` itself, and this wiki is
    a docroot install, so there is no script path between the two.

    The indent is a tab. Core's `.gitignore` hides the file from git, but
    not from eslint: `npm test` in core lints every JSON file, and its
    `indent` rule rejects spaces. With spaces, `npm test` fails on a
    clean checkout."""
    server = params.get("server") or DEFAULT_INSTALL_PARAMS["server"]
    config = {
        "base_uri": server.rstrip("/") + "/",
        "main_page": API_TESTING_MAIN_PAGE,
        "root_user": {
            "name": params.get("adminUser") or DEFAULT_INSTALL_PARAMS["adminUser"],
            "password": (params.get("adminPassword")
                         or DEFAULT_INSTALL_PARAMS["adminPassword"]),
        },
        "secret_key": secret_key,
    }
    return json.dumps(config, indent=indent) + "\n"


def write_api_testing_config(core_path, params, run=subprocess.run):
    """Write <core>/.api-testing.config.json. True when the file holds our
    content afterward, False when it does not.

    Runs after install_mediawiki, which is when $wgSecretKey exists. Like
    .env, an edited copy is left alone: under wmf-sbx-resume an engineer
    who changed it meant to. A copy in the old four-space form is ours, not
    an edit, so it is replaced."""
    secret_key = read_secret_key(core_path)
    if secret_key is None:
        print(
            f"could not read $wgSecretKey from {core_path}/LocalSettings.php; "
            f"skipping {API_TESTING_CONFIG_NAME}",
            file=sys.stderr,
        )
        return False
    path = os.path.join(core_path, API_TESTING_CONFIG_NAME)
    contents = api_testing_config_contents(params, secret_key)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            existing = None
        if existing == contents:
            return True
        old_form = api_testing_config_contents(params, secret_key, indent=4)
        if existing == old_form:
            with open(path, "w", encoding="utf-8") as f:
                f.write(contents)
            return True
        print(f"{path} differs from the template; leaving it alone", file=sys.stderr)
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(contents)
    result = run(["sudo", "chown", f"{SANDBOX_USER}:{SANDBOX_USER}", path])
    if result.returncode != 0:
        print(f"warning: could not chown {path} (exit {result.returncode})", file=sys.stderr)
    print(f"wrote {path}", file=sys.stderr)
    return True


def npm_install(repo_path, run=subprocess.run):
    """True/False, or None if the repo has no package.json.

    Runs in every clone that has a package.json, not in core alone: an
    extension's own JS tests, linters and selenium harness all come from
    its own node_modules. Measured cost for the four repos of the test
    sandbox: 29 s. See sbx/DESIGN-testing-instructions.md §5.1.

    `npm ci` rather than `npm install`: core's package-lock.json is
    *tracked*, so an install that touches it leaves the clone dirty and a
    later git safe-reset then refuses. ci is also lockfile-exact and
    faster. Falls back to install when there's no lockfile to be exact
    about. See sbx/DESIGN-setup-steps.md §5.

    CYPRESS_INSTALL_BINARY=0 because a repo that lists cypress would
    otherwise download an 800 MB binary at every create, and most tasks
    never run its Cypress specs. The variable makes the package install
    without the binary; `mw-install-cypress` fetches it on demand
    (sbx/NOTES.md §91)."""
    if not os.path.exists(os.path.join(repo_path, "package.json")):
        return None
    if os.path.exists(os.path.join(repo_path, "package-lock.json")):
        argv = ["npm", "ci", "--no-audit", "--no-fund"]
    else:
        argv = ["npm", "install", "--no-audit", "--no-fund"]
    # `env`, not run(env=...): as_agent goes through sudo, which drops the
    # environment it is given.
    result = as_agent(["env", "CYPRESS_INSTALL_BINARY=0"] + argv,
                      cwd=repo_path, run=run)
    return result.returncode == 0


def phpunit_config(core_path, run=subprocess.run):
    """Generate <core>/phpunit.xml. True/False, or None with no core
    composer.json.

    Core ships `phpunit.xml.template`, not a `.dist`, so a fresh checkout
    has no PHPUnit config at all, and `vendor/bin/phpunit <path>` then
    loads no bootstrap and dies with `Class "MediaWikiUnitTestCase" not
    found` -- which reads as a broken sandbox. One command at create time
    removes the trap for every later test run. The file is gitignored in
    core, so this never dirties the tree.

    `composer phpunit:config` is core's own script for the job
    (`@php tests/phpunit/generatePHPUnitConfig.php`). Claude still has to
    run it again after an extension list changes, which is why the
    template documents it. See sbx/DESIGN-testing-instructions.md §5.2."""
    if not os.path.exists(os.path.join(core_path, "composer.json")):
        return None
    result = as_agent(["composer", "phpunit:config"], cwd=core_path, run=run)
    return result.returncode == 0


def install_mediawiki(core_path, run=subprocess.run):
    """True/False, or None when LocalSettings.php already exists (the
    installer refuses in that case, and under wmf-sbx-resume it will).

    --with-extensions is the whole point of the flag: core's own
    mw-install:sqlite doesn't pass it, and CliInstaller only enables
    extensions when it's given -- skins are detected either way. Without
    it the generated LocalSettings.php has no wfLoadExtension lines at
    all, and the dependency walk and the symlinks buy nothing. Composer
    appends extra arguments to the script's last command, and MediaWiki's
    option parser accepts an option after positionals, so this runs core's
    script verbatim with the flag on the end. See §6.1."""
    if os.path.exists(os.path.join(core_path, "LocalSettings.php")):
        print(
            f"{core_path}/LocalSettings.php already exists; skipping the install",
            file=sys.stderr,
        )
        return None
    result = as_agent(
        ["composer", INSTALL_SCRIPT_NAME, "--", "--with-extensions"], cwd=core_path, run=run
    )
    return result.returncode == 0


# The line install_mediawiki's composer mw-install:sqlite --with-extensions
# actually writes for a wfLoadExtension'd Parsoid. link_parsoid_checkout
# deletes it from wherever it landed -- the intercept block below now
# makes that same wfLoadExtension('Parsoid', ...) call itself, from a
# spot earlier in the file, so leaving the original in place would
# double-load the extension. Observed live for other extensions
# (sbx/NOTES.md §30 -- `wfLoadExtension( 'Translate' )`); MediaWiki's
# ExtensionRegistry always emits this exact spacing.
PARSOID_LOAD_EXTENSION_LINE = "wfLoadExtension( 'Parsoid' );"

# mw-install:sqlite's own comment, always emitted ahead of every
# wfLoadSkin/wfLoadExtension call it writes. link_parsoid_checkout inserts
# PARSOID_LOCAL_SETTINGS_TEMPLATE just above this line, not in place of
# PARSOID_LOAD_EXTENSION_LINE: the autoloader intercept has to be
# registered before *any* skin or extension loads, in case one of them
# references a Parsoid class before Parsoid's own wfLoadExtension would
# otherwise have run.
PARSOID_ENABLED_SKINS_MARKER = "// Enabled skins."

# https://www.mediawiki.org/wiki/Parsoid#Linking_a_developer_checkout_of_Parsoid
# -- adapted for a generated file: $parsoidInstallDir is set directly to
# this sandbox's own clone (a placeholder link_parsoid_checkout fills in),
# not the human-facing bundled-copy/commented-out-alternative pair the
# wiki page shows, since there is only ever one value here and no reason
# to toggle it by hand. The wiki page also guards this block with
# `if ( $parsoidInstallDir !== 'vendor/wikimedia/parsoid' )`; that's
# dropped here -- $parsoidInstallDir is always this sandbox's own
# absolute path, so the guard can never be false, and a dead branch kept
# only to match the docs byte-for-byte isn't worth carrying.
PARSOID_LOCAL_SETTINGS_TEMPLATE = r"""$parsoidInstallDir = '__PARSOID_INSTALL_DIR__'; # this sandbox's gerrit:mediawiki/services/parsoid clone

// For developers: ensure Parsoid is executed from $parsoidInstallDir,
// (not the version included in mediawiki-core by default)
// Must occur *before* any skin or extension is loaded.
function wfInterceptParsoidLoading( $className ) {
    // Only intercept Parsoid namespace classes
    if ( preg_match( '/(MW|Wikimedia\\\\)Parsoid\\\\/', $className ) ) {
       $fileName = Autoloader::find( $className );
       if ( $fileName !== null ) {
           require $fileName;
       }
    }
}
spl_autoload_register( 'wfInterceptParsoidLoading', true, true );
// AutoLoader::registerNamespaces was added in MW 1.39
AutoLoader::registerNamespaces( [
    // Keep this in sync with the "autoload" clause in
    // $parsoidInstallDir/composer.json
    'Wikimedia\\Parsoid\\' => "$parsoidInstallDir/src/",
] );

wfLoadExtension( 'Parsoid', "$parsoidInstallDir/extension.json" );
unset( $parsoidInstallDir );"""


def link_parsoid_checkout(core_path, parsoid_path):
    """Rewrites <core_path>/LocalSettings.php so Parsoid loads from this
    sandbox's own writable gerrit:mediawiki/services/parsoid clone
    instead of the composer-vendored copy -- see
    PARSOID_LOCAL_SETTINGS_TEMPLATE. Call only right after
    install_mediawiki returns True: that's the one moment
    PARSOID_LOAD_EXTENSION_LINE is guaranteed to be there verbatim, fresh
    off CliInstaller's own write, with nothing else touched the file yet.

    The autoloader intercept goes in just above PARSOID_ENABLED_SKINS_MARKER
    -- ahead of every skin and extension load, not just Parsoid's own --
    and the original PARSOID_LOAD_EXTENSION_LINE is deleted from its old
    spot below, since the inserted block now makes that call itself.

    Warn-and-continue, not fatal, on any failure mode below: the wiki
    already works with the bundled Parsoid, so a missed link is a
    degradation, not the thing install_mediawiki being fatal is there to
    catch. Returns True/False accordingly."""
    path = os.path.join(core_path, "LocalSettings.php")
    try:
        with open(path, encoding="utf-8") as f:
            contents = f.read()
    except OSError as e:
        print(f"warning: could not read {path} to link Parsoid ({e})", file=sys.stderr)
        return False
    if PARSOID_LOAD_EXTENSION_LINE not in contents:
        print(
            f"warning: no {PARSOID_LOAD_EXTENSION_LINE!r} line in {path}; "
            "Parsoid checkout not linked (is the extension actually enabled?)",
            file=sys.stderr,
        )
        return False
    if PARSOID_ENABLED_SKINS_MARKER not in contents:
        print(
            f"warning: no {PARSOID_ENABLED_SKINS_MARKER!r} marker in {path}; "
            "Parsoid checkout not linked (unexpected LocalSettings.php shape)",
            file=sys.stderr,
        )
        return False
    # PHP single-quoted string escaping: a literal backslash or quote in
    # the path (unusual, but not impossible) has to survive round-tripping
    # through PHP's own string parser, not just Python's.
    escaped_path = parsoid_path.replace("\\", "\\\\").replace("'", "\\'")
    block = PARSOID_LOCAL_SETTINGS_TEMPLATE.replace("__PARSOID_INSTALL_DIR__", escaped_path)
    # The intercept has to be registered before anything loads, so the
    # block goes in above the skins marker...
    contents = contents.replace(
        PARSOID_ENABLED_SKINS_MARKER, block + "\n\n" + PARSOID_ENABLED_SKINS_MARKER, 1
    )
    # ...and the original call -- now redundant -- comes out of its old spot.
    contents = contents.replace(PARSOID_LOAD_EXTENSION_LINE + "\n", "", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(contents)
    print(f"linked Parsoid checkout ({parsoid_path}) into {path}", file=sys.stderr)
    return True


def daemon_argv(sandbox_home, port):
    """The `git daemon` command line itself, without the `sudo -u agent`
    prefix start_daemon puts in front of it.

    Split out because wmf_sbx_kit needs the identical command for the
    kit's `setup.startup` entry -- that one already runs as the agent, and
    it is what brings the daemon back after `sbx stop`/`sbx run`, which
    this install-time launch cannot survive. Two copies of an argv this
    load-bearing (see start_daemon on why every flag is there) would
    diverge."""
    return [
        "git", "-c", "safe.directory=*", "daemon", "--reuseaddr", "--export-all",
        f"--base-path={sandbox_home}", "--listen=0.0.0.0", f"--port={port}",
        sandbox_home,
    ]


def start_daemon(sandbox_home, port, popen=subprocess.Popen):
    """Starts a single `git daemon` serving the whole parallel tree,
    detached from this script's own process group (start_new_session=True,
    the Python equivalent of `setsid`) so it outlives the `setup.install`
    step that launched it -- see sbx/DESIGN-parallel-clone-tree.md §3-§4.
    Does not survive `sbx stop`/`sbx run`; that gap is closed by the kit's
    `setup.startup` entry (wmf_sbx_kit.daemon_startup_command), which runs
    the same command again on every container start and no-ops when this
    one is still listening.

    Confirmed root cause of a 2026-09-07 live incident (see sbx/NOTES.md
    #21): this script runs as root (a commands.install step), and the
    daemon it started inherited that -- so every forked upload-pack child
    hit git's CVE-2022-24765 "dubious ownership" check (root's euid vs.
    the repo's owning uid) the moment a repo's ownership stopped matching
    root, e.g. right after clone_into_parallel_tree's own chown to
    SANDBOX_USER. The daemon process itself stayed alive throughout (only
    the per-connection child dies), which is why it looked "stuck" rather
    than crashed. Two fixes, both needed:
      1. Run as SANDBOX_USER, not root -- no reason a read-only,
         fetch-only daemon needs root, and it makes euid match the owner
         of every git-cloned repo in the parallel tree (see
         clone_into_parallel_tree's chown).
      2. -c safe.directory='*' on top of that -- SANDBOX_USER's euid only
         matches *cloned* repos' ownership; a ':ro' opt-out repo's parallel
         path is a bind mount (bind_into_parallel_tree), which is never
         chowned (that would corrupt the pristine original) and so keeps
         whatever ownership the host-mirrored original has inside the
         sandbox -- SANDBOX_USER by coincidence only when the host user's
         uid happens to be 1000. safe.directory='*' is scoped to just this
         git invocation (and, via GIT_CONFIG_PARAMETERS, its forked
         children) -- not a global gitconfig change -- which is an
         acceptable blanket trust here since the daemon only ever serves
         read-only fetches (no --enable=receive-pack) out of this sandbox's
         own, otherwise-inaccessible-from-outside tree."""
    argv = ["sudo", "-u", SANDBOX_USER] + daemon_argv(sandbox_home, port)
    popen(
        argv,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def load_plan(path):
    """The JSON plan wmf_sbx_kit writes next to this script -- see
    sbx/DESIGN-setup-steps.md §1. Raises RuntimeError on anything it can't
    trust: a half-understood plan silently drops repos."""
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"could not read the plan file {path}: {e}") from None
    if not isinstance(plan, dict):
        raise RuntimeError(f"{path}: expected a JSON object")
    version = plan.get("version")
    if version != PLAN_VERSION:
        raise RuntimeError(
            f"{path}: plan version {version!r}, but this wmf-sbx-setup "
            f"understands version {PLAN_VERSION}"
        )
    for key in ("hostHome", "daemonPort", "repos"):
        if key not in plan:
            raise RuntimeError(f"{path}: missing {key!r}")
    if not isinstance(plan["repos"], list):
        raise RuntimeError(f"{path}: 'repos' must be a list")
    return plan


def plan_from_argv(argv):
    """The positional form, normalised into the same shape as a plan file,
    so main() only has one code path. It carries no canonicals, no link
    names and no upstream URLs, so no symlinks get made and the clones keep
    the host mirror as `origin` -- see this module's usage string.

    Every repo here is `requested`: there is no dependency walk in this
    form, so each one was typed out by hand.
    """
    host_home, port, *repo_args = argv
    repos = []
    for spec in repo_args:
        literal_path, is_ro = parse_repo_arg(spec)
        repos.append({
            "path": literal_path, "canonical": None, "readOnly": is_ro,
            "linkName": None, "linkDir": None, "upstreamUrl": None,
            "requested": True,
        })
    return {
        "version": PLAN_VERSION,
        "hostHome": host_home,
        "daemonPort": port,
        "primary": repos[0]["path"] if repos else None,
        "repos": repos,
    }


def mediawiki_setup(plan, core_path, core_readonly, links, clones, warnings,
                    parsoid_path=None, run=subprocess.run):
    """Everything after the parallel tree: reset the dependency clones,
    wire the extensions into core, install their PHP deps, and install a
    wiki. Returns 0, or 1 if one of the core steps failed.

    Every path here is a work_path, not a parallel-tree `dest`: composer,
    npm and the installer all write absolute paths into files that outlive
    them (`.env`, `LocalSettings.php`, `vendor/`, the extension symlinks),
    and those should name the path the agent and the engineer use rather
    than the `/home/agent` alias of it. See work_path and sbx/NOTES.md
    §63.1.

    clones: [(literal_path, work_path, reset_remote), ...] for the repos
    that got a writable clone, in plan order -- reset_remote is
    configure_remotes' verdict on which remote safe-reset should reset
    that clone against. literal_path stays as the *identity* of the repo,
    because that is what the plan and the command line name it.
    warnings: a list this appends one line to per
    non-fatal problem, so the summary at the end of a very long `sbx
    create` log names them all in one place.
    parsoid_path: this sandbox's writable
    gerrit:mediawiki/services/parsoid clone, if any.
    link_parsoid_checkout points LocalSettings.php at it once
    install_mediawiki has written one to patch (see PARSOID_CANONICAL).

    The whole chain is gated on core being present *and* cloned: with no
    writable core there is nothing to link into and no wiki to install, so
    resetting and composer-updating a dozen dependencies would be minutes
    of work for nothing. See sbx/DESIGN-setup-steps.md §8.
    """
    if core_path is None:
        print(
            "no mediawiki/core clone in this sandbox; skipping the MediaWiki "
            "setup (symlinks, .env, npm, install)",
            file=sys.stderr,
        )
        return 0
    if core_readonly:
        print(
            f"{core_path} is a read-only bind mount of the host's core; "
            "skipping the MediaWiki setup (drop the ':ro' on core to get it)",
            file=sys.stderr,
        )
        return 0

    keep = repos_to_leave_alone(plan)
    # Per-repo steps: warn and continue. One extension whose composer
    # update fails shouldn't cost the engineer the whole sandbox -- it can
    # be fixed in place, from inside.
    for literal_path, work, reset_remote in clones:
        if literal_path in keep:
            print(
                f"{work}: named on the command line, so keeping the host's "
                "branch (pass --reset-all to reset it too)",
                file=sys.stderr,
            )
            continue
        if not git_safe_reset(work, remote=reset_remote, run=run):
            warnings.append(f"git safe-reset {reset_remote} failed in {work}")

    # Links and composer.local.json first: core's composer update merges
    # every extension's and skin's composer.json that the links make
    # visible. See write_composer_local.
    link_into_core(core_path, links, core_readonly=core_readonly)
    try:
        write_composer_local(core_path, parsoid_checkout=parsoid_path is not None,
                             run=run)
    except OSError as e:
        warnings.append(f"could not write {core_path}/{COMPOSER_LOCAL_NAME} ({e})")

    for _literal_path, work, _reset_remote in clones:
        if composer_update(work, run=run) is False:
            warnings.append(f"composer update failed in {work}")

    # Core's own npm install is a core step below, and fatal there. Every
    # other clone gets one here, non-fatally: an extension whose JS deps
    # fail should cost that extension's JS tests, not the whole sandbox.
    for _literal_path, work, _reset_remote in clones:
        if work == core_path:
            continue
        if npm_install(work, run=run) is False:
            warnings.append(f"npm install failed in {work}")

    # Core steps: fatal. A sandbox without a working wiki is the one thing
    # this whole exercise was for, so it should fail loudly at create time
    # rather than quietly hand over a wiki that doesn't run.
    params = parse_install_params(core_path, fallback=plan.get("mediawiki"))
    try:
        write_env_file(core_path, params, run=run)
    except (OSError, KeyError) as e:
        print(f"error: could not write {core_path}/.env ({e})", file=sys.stderr)
        return 1

    if npm_install(core_path, run=run) is False:
        print(f"error: npm install failed in {core_path}", file=sys.stderr)
        return 1

    installed = install_mediawiki(core_path, run=run)
    if installed is False:
        print(f"error: {INSTALL_SCRIPT_NAME} failed in {core_path}", file=sys.stderr)
        return 1

    # After the install, and both non-fatal: a wiki with no phpunit.xml and
    # no api-testing config still runs, and either file can be made again
    # from inside the sandbox.
    if phpunit_config(core_path, run=run) is False:
        warnings.append(f"composer phpunit:config failed in {core_path}")
    # Only with a LocalSettings.php to read $wgSecretKey out of. Without
    # one there is no wiki either, and the install above has already said
    # so more usefully than a second warning about a config file would.
    if os.path.exists(os.path.join(core_path, "LocalSettings.php")):
        api_testing_config = os.path.join(core_path, API_TESTING_CONFIG_NAME)
        try:
            write_api_testing_config(core_path, params, run=run)
        except OSError as e:
            print(f"warning: could not write {api_testing_config} ({e})", file=sys.stderr)
        # The test is the file, not the return value: False also means an
        # engineer's own edited copy was kept, which is not a problem.
        if not os.path.exists(api_testing_config):
            warnings.append(f"no {api_testing_config}; the api-testing suites need one")

    # Only when install_mediawiki just wrote a fresh LocalSettings.php
    # (installed is True, not None) -- that's the one moment the line it
    # replaces is guaranteed present verbatim. See link_parsoid_checkout.
    if installed and parsoid_path is not None:
        if not link_parsoid_checkout(core_path, parsoid_path):
            warnings.append(f"could not link the Parsoid checkout at {parsoid_path}")
    return 0


def claude_md_status_path(log_dir=LOG_DIR):
    return os.path.join(log_dir, CLAUDE_MD_STATUS_NAME)


_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")


def _normalize_heading(line):
    """A heading line with its runs of white space made one space, and
    any closing #s removed. Returns (level, text), or None if the line is
    not a heading."""
    m = _HEADING_RE.match(line)
    if not m:
        return None
    text = re.sub(r"[ \t]+#+$", "", m.group(2) or "")
    return len(m.group(1)), " ".join(text.split())


def markdown_headings(lines):
    """[(index, level, text)] for each ATX heading in lines. Lines in a
    fenced code block are not headings: the sbx text has shell comments
    such as `# DO NOT ADD THESE ...` in its code blocks."""
    headings = []
    fence = None
    for i, line in enumerate(lines):
        m = _FENCE_RE.match(line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip()[len(m.group(1)):].strip():
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        h = _normalize_heading(line)
        if h is not None:
            headings.append((i, h[0], h[1]))
    return headings


def apply_claude_md_edits(text, edits):
    """Apply the edits in order to the markdown text. Returns
    (new_text, []) if every edit applied, or (None, failures) if any did
    not. All or nothing: a half-edited file is harder to read than the
    upstream one.

    Each edit names a section by its exact heading line ("## Git
    workspace mode"), compared with white space normalized. The section
    runs from the heading to the next heading of the same or a higher
    level, so its subsections go with it. Operations:

      {"op": "replace", "heading": H, "text": T}   the section becomes T
      {"op": "remove", "heading": H}               the section goes

    An optional "contains" list names text that must be in the section.
    It makes a section that upstream rewrote fail, instead of a replace
    that silently removes new text."""
    lines = text.split("\n")
    failures = []
    for n, edit in enumerate(edits, 1):
        op = edit.get("op") if isinstance(edit, dict) else None
        heading = edit.get("heading") if isinstance(edit, dict) else None
        where = f"edit {n} ({op} {heading!r})"
        if op not in ("replace", "remove") or not isinstance(heading, str):
            failures.append(f"{where}: not a valid edit")
            continue
        wanted = _normalize_heading(heading)
        if wanted is None:
            failures.append(f"{where}: the heading does not start with #")
            continue
        headings = markdown_headings(lines)
        found = [k for k, h in enumerate(headings) if (h[1], h[2]) == wanted]
        if not found:
            failures.append(f"{where}: no such heading")
            continue
        if len(found) > 1:
            failures.append(f"{where}: the heading occurs {len(found)} times")
            continue
        k = found[0]
        start, level = headings[k][0], headings[k][1]
        end = next((h[0] for h in headings[k + 1:] if h[1] <= level), len(lines))
        section = " ".join("\n".join(lines[start:end]).split())
        missing = [c for c in edit.get("contains") or []
                   if " ".join(c.split()) not in section]
        if missing:
            failures.append(
                f"{where}: the section no longer contains "
                + ", ".join(repr(c) for c in missing))
            continue
        new = []
        if op == "replace":
            new = edit.get("text", "").rstrip("\n").split("\n") + [""]
        lines[start:end] = new
    if failures:
        return None, failures
    return "\n".join(lines), []


def default_claude_md_path(plan_path=SANDBOX_PLAN_FILE):
    """The CLAUDE.md sbx writes: in the parent directory of the primary
    workspace (MEASURED, sbx/NOTES.md §95). Raises RuntimeError if the
    plan does not name a primary workspace."""
    plan = load_plan(plan_path)
    primary = plan.get("primary")
    if not primary:
        raise RuntimeError(f"{plan_path}: no primary workspace")
    return os.path.join(os.path.dirname(primary.rstrip("/")), "CLAUDE.md")


def _write_like(path, text, st, mode=None):
    """Write text to path through a temporary file and a rename, with the
    owner of the stat result `st`, and its mode unless `mode` is given.
    The rename makes the change atomic: the startup step and the host's
    exec can run at the same time, and neither may read a half-written
    file."""
    tmp = f"{path}.wmf-sbx.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass  # not root: the file is ours already
    os.chmod(tmp, stat.S_IMODE(st.st_mode) if mode is None else mode)
    os.replace(tmp, path)


def edit_claude_md(edits_path, target=None, upstream_copy=SANDBOX_CLAUDE_MD_UPSTREAM,
                   plan_path=SANDBOX_PLAN_FILE):
    """Correct the sections of sbx's CLAUDE.md that are wrong in a
    wmf-sbx sandbox. Returns (exit_code, problems).

    Idempotent: the first line of an edited file is a marker, and a file
    with the marker is left alone. sbx writes the file after the install
    steps and does not rewrite it on a restart (sbx/NOTES.md §95), so a
    file without the marker is new text from sbx, and gets the edits."""
    problems = []
    try:
        with open(edits_path, encoding="utf-8") as f:
            data = json.load(f)
        edits = data["edits"]
        if not isinstance(edits, list):
            raise ValueError("'edits' is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return 1, [f"error: could not read the edits {edits_path} ({e})"]
    try:
        target = target or default_claude_md_path(plan_path)
    except RuntimeError as e:
        return 1, [f"error: could not find sbx's CLAUDE.md: {e}"]
    try:
        with open(target, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return 0, [f"warning: {target} does not exist, so there is nothing to "
                   f"edit. sbx may have moved it."]
    except OSError as e:
        return 1, [f"error: could not read {target} ({e})"]
    if text.startswith(CLAUDE_MD_MARKER):
        print(f"{target} is already edited", file=sys.stderr)
        return 0, problems
    if upstream_copy:
        # Before the edit, and also when it fails: the refresh helper
        # needs this text most when the edits do not apply to it.
        try:
            _write_like(upstream_copy, text, os.stat(target), mode=0o644)
        except OSError as e:
            problems.append(f"warning: could not save sbx's text to "
                            f"{upstream_copy} ({e})")
    new, failures = apply_claude_md_edits(text, edits)
    if new is None:
        problems.append(
            f"error: the edits in {edits_path} no longer apply to sbx's "
            f"{target} (written for {data.get('upstream', 'an older sbx')}), "
            f"so it is unchanged")
        problems.extend(f"error: {f}" for f in failures)
        return 1, problems
    digest = hashlib.sha256(
        json.dumps(edits, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    marker = f"{CLAUDE_MD_MARKER} (edits {digest}) -->\n"
    try:
        _write_like(target, marker + new, os.stat(target))
    except OSError as e:
        problems.append(f"error: could not write {target} ({e})")
        return 1, problems
    print(f"applied {len(edits)} edit(s) to {target}", file=sys.stderr)
    return 0, problems


def run_claude_md(argv, log_dir=LOG_DIR):
    """`wmf-sbx-setup --claude-md EDITS.json [CLAUDE.md]` -- a startup
    step, as root. Also run by the host over `sbx exec` after a create or
    a start, which reads the status file back and prints its problems.

    It exits 0 even when the edits do not apply. The sandbox works
    without them: ~/.claude/CLAUDE.md corrects the same sections in
    words. The status file holds the real result."""
    if not argv:
        print("error: --claude-md wants the path of the edits file",
              file=sys.stderr)
        return 1
    try:
        status, problems = edit_claude_md(argv[0], argv[1] if len(argv) > 1 else None)
    except Exception as e:  # noqa: BLE001 - the report is the whole point
        traceback.print_exc(file=sys.stderr)
        status, problems = 1, [f"error: wmf-sbx-setup --claude-md did not "
                               f"finish ({type(e).__name__}: {e})"]
    for problem in problems:
        print(problem, file=sys.stderr)
    if log_dir:
        write_status(claude_md_status_path(log_dir), status, problems)
    return 0


def main(argv=None, run=subprocess.run, popen=subprocess.Popen, log_dir=LOG_DIR):
    """Runs the whole setup with sys.stderr redirected through a SetupLog,
    so everything printed lands in log_dir/wmf-sbx-setup.log and the
    problems land in log_dir/wmf-sbx-setup.status as well as on stderr.

    `--restore` runs the much smaller restart pass instead (§40), with its
    own log and status file so it can't clobber the record of the run that
    built the sandbox.

    log_dir=None turns both off (hand invocations, and the tests, which
    have no business writing to /var/log)."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--verify"]:
        # A diagnostic, run by hand over `sbx exec`: it changes nothing, so
        # it needs no root and leaves no report behind. Its output is for
        # the terminal it was typed into.
        return run_verify(argv[1:])
    if argv[:1] == ["--settings"]:
        # Its own step in the kit, run at install and again on every
        # start, so it gets neither log: sbx captures install output, and
        # startup output already lands in /var/log/sbx-kit-startup.log,
        # which report_setup_problems reads. Writing to the setup log
        # would append to the record of the run that built the sandbox.
        return run_settings(argv[1:], run=run)
    if argv[:1] == ["--mcp"]:
        # Same reasoning as --settings above: its own kit step, at install
        # and on every start, so it keeps its output where sbx already
        # collects that step's output.
        return run_mcp(argv[1:], run=run)
    if argv[:1] == ["--exec-bits"]:
        # Same reasoning again, and it has to come before the setup log is
        # opened: this step's whole job is to be readable in the install
        # output of the create it ran in.
        return run_exec_bits(argv[1:])
    if argv[:1] == ["--claude-md"]:
        # A startup step with its own status file: the setup log is the
        # record of the run that built the sandbox.
        return run_claude_md(argv[1:], log_dir=log_dir)
    restoring = argv[:1] == ["--restore"]
    log_name = RESTORE_LOG_NAME if restoring else SETUP_LOG_NAME
    status_name = RESTORE_STATUS_NAME if restoring else SETUP_STATUS_NAME
    log_path = os.path.join(log_dir, log_name) if log_dir else None
    log = SetupLog(sys.stderr, log_path, append=restoring,
                   max_bytes=RESTORE_LOG_MAX_BYTES)
    real_stderr = sys.stderr
    sys.stderr = log
    try:
        if restoring:
            status = run_restore(argv[1:], run=run)
        else:
            status = run_setup(argv, run=run, popen=popen, log=log)
    except Exception:  # noqa: BLE001 - the report is the whole point
        # An unhandled exception is exactly the failure the engineer never
        # sees: `sbx create` prints one collapsed ✗ and moves on. Put the
        # traceback in the log and a one-line summary in the status.
        traceback.print_exc(file=sys.stderr)
        log.record(
            "error: wmf-sbx-setup did not finish (unhandled exception; the "
            "traceback is in the log)"
        )
        status = 1
    finally:
        sys.stderr = real_stderr
        log.close()
    if log_dir:
        write_status(
            os.path.join(log_dir, status_name), status, log.problems,
            log_path=log.path,
        )
    return status


def run_setup(argv, run=subprocess.run, popen=subprocess.Popen, log=None):
    if argv and argv[0].startswith("--"):
        # This copy of the script is a create-time snapshot -- nothing
        # updates /home/agent/wmf-sbx-setup in an existing sandbox -- so a
        # flag added later reaches an older sandbox as a plan-file path,
        # and "could not read the plan file --verify" is a baffling way to
        # be told your sandbox predates the flag (sbx/NOTES.md §45).
        print(
            f"error: unknown option {argv[0]!r}. This sandbox's copy of "
            f"wmf-sbx-setup was written when it was created and is never "
            f"updated, so a newer flag needs a newer sandbox.",
            file=sys.stderr,
        )
        argv = []  # fall through to the usage message
    try:
        if len(argv) == 1:
            plan = load_plan(argv[0])
        elif len(argv) >= 3:
            plan = plan_from_argv(argv)
        else:
            print(
                "Usage: wmf-sbx-setup PLAN.json\n"
                "       wmf-sbx-setup HOST_HOME PORT REPO[:ro] [REPO[:ro] ...]\n"
                "       wmf-sbx-setup --restore [ORIGINALS_DIR]\n"
                "       wmf-sbx-setup --verify [ORIGINALS_DIR]\n"
                "       wmf-sbx-setup --settings PATCH.json [SETTINGS.json]\n"
                "       wmf-sbx-setup --mcp SERVERS.json [CLAUDE.json]\n"
                "       wmf-sbx-setup --exec-bits PATH [PATH ...]\n"
                "       wmf-sbx-setup --claude-md EDITS.json [CLAUDE.md]",
                file=sys.stderr,
            )
            return 1
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    host_home = plan["hostHome"]
    port = plan["daemonPort"]
    warnings = []
    install_helper_scripts(run=run)
    # Before any repo work: this is the one mount in the sandbox that is
    # shared with *other* sandboxes, so the sooner in the install it is
    # read-only the smaller the window (see lock_shared_skills). The
    # startup pass re-locks it on every container start.
    lock_shared_skills(run=run)

    core_path = None
    core_readonly = False
    parsoid_path = None
    links = []
    clones = []
    layout = []
    for repo in plan["repos"]:
        literal_path = repo["path"]
        is_ro = bool(repo.get("readOnly"))
        try:
            entry = setup_repo(host_home, literal_path, is_ro, run=run)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            # Whatever did get mounted before this repo failed still has to
            # be restorable on the next container start.
            write_layout(layout)
            return 1
        layout.append(entry)
        dest = entry["dest"]
        # The mounts are in place now, so from here on everything works
        # through the path the agent and the engineer both use -- which is
        # the literal one except in the "clone" fallback, where the alias
        # didn't take. See work_path and sbx/NOTES.md §63.1.
        work = work_path(entry)
        if dest is None:
            print(
                f"{literal_path} is outside {host_home}; remounted read-only in place",
                file=sys.stderr,
            )
        elif is_ro:
            print(f"{literal_path} -> {dest} (read-only bind mount)", file=sys.stderr)
        else:
            print(f"cloned {literal_path} -> {dest}", file=sys.stderr)
            reset_remote = configure_remotes(work, repo.get("upstreamUrl"), run=run)
            clones.append((literal_path, work, reset_remote))
        if repo.get("canonical") == CORE_CANONICAL:
            core_path, core_readonly = (work if dest else None), is_ro
        elif dest and repo.get("linkName") and repo.get("linkDir"):
            links.append((repo["linkDir"], repo["linkName"], work))
        # Independent of the if/elif above: a writable Parsoid clone (not
        # a :ro bind mount, hence `not is_ro`) also gets linked into
        # LocalSettings.php, by mediawiki_setup -- see PARSOID_CANONICAL.
        if repo.get("canonical") == PARSOID_CANONICAL and dest and not is_ro:
            parsoid_path = work

    # Before the slow MediaWiki phase too: if the create dies in composer,
    # the sandbox still comes back with its mounts intact on the next start.
    write_layout(layout)

    # Before the slow MediaWiki phase, not after it: the daemon is what the
    # host fetches and inspects through, and there's no reason to make that
    # wait on composer and npm. Nothing below depends on it.
    start_daemon(SANDBOX_HOME, port, popen=popen)
    print(f"git daemon listening on 0.0.0.0:{port}, serving {SANDBOX_HOME}", file=sys.stderr)

    status = mediawiki_setup(
        plan, core_path, core_readonly, links, clones, warnings,
        parsoid_path=parsoid_path, run=run,
    )
    if warnings:
        # `sbx create`'s output is long and composer's is longer; a warning
        # in the middle of it is a warning nobody reads.
        print(
            f"\n{len(warnings)} step(s) did not succeed:\n"
            + "\n".join(f"    {w}" for w in warnings),
            file=sys.stderr,
        )
        if log is not None:
            # These are reported as a block rather than one `warning:` line
            # each, so the SetupLog's own prefix scan doesn't see them.
            for w in warnings:
                log.record(f"warning: {w}")
    return status


if __name__ == "__main__":
    sys.exit(main())
