#!/usr/bin/env python3
"""The in-VM half of a Lima sandbox's setup, run as the agent.

`wmf-sbx create` sends this file on stdin to `python3 - PLAN.json`
in the VM, as the agent, outside nono, before the first session
(mediawiki.py; DESIGN.md, "The MediaWiki setup"). The clones are there
already, at the host paths, with their remotes and resets
(sandbox-repos.sh). This script does the MediaWiki part: the links of the
extensions and skins into core, composer.local.json, `composer update`,
`npm ci`, `.env`, the install, phpunit.xml and the api-testing config. The
per-repo steps warn and continue; the core ones are fatal.

Everything this script prints goes to the terminal and to
LOG_DIR/wmf-sbx-setup.log; the `error:`/`warning:` lines also go to
LOG_DIR/wmf-sbx-setup.status.

It also writes the plan's `session` part (session.py): the settings
`env` and the agent's CLAUDE.md and testing guide. The MCP servers are
registered by bin/wmf-claude-setup, not here.

It must not import any sibling wmf_sbx module: it runs in the VM alone.

Usage: python3 setup.py PLAN.json
"""

import hashlib
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import time
import traceback
import urllib.parse

SANDBOX_HOME = "/home/agent"


# Where this script's own narration ends up, and the machine-readable
# list of its problems. The agent cannot write /var/log.
LOG_DIR = os.path.join(SANDBOX_HOME, ".wmf-sbx")
SETUP_LOG_NAME = "wmf-sbx-setup.log"
SETUP_STATUS_NAME = "wmf-sbx-setup.status"


# The plan that `wmf-sbx create` writes into the VM (mediawiki.py,
# kit.build_plan) and passes to this script.
SANDBOX_PLAN_FILE = os.path.join(SANDBOX_HOME, "wmf-sbx-plan.json")


# Bumped for an incompatible change to the .status file.
STATUS_VERSION = 1

# Bumped only for an incompatible change to the plan file. create sends
# the plan and this script together, so this guards against a stale
# hand-written plan only.
PLAN_VERSION = 1

# The one canonical this script has to recognise: core is where everything
# else gets linked, and it's never itself a link.
CORE_CANONICAL = "gerrit:mediawiki/core"

# The second one it has to recognise: a writable Parsoid clone gets linked
# into LocalSettings.php by link_parsoid_checkout, same idea as
# CORE_CANONICAL but there's no symlink involved -- see mediawiki_setup.
PARSOID_CANONICAL = "gerrit:mediawiki/services/parsoid"

# The user the agent runs as. On Lima this script runs as that user; run
# as root (the tests), it gives what it writes back to the
# agent (give_to_agent), or git refuses it ("detected dubious ownership").
SANDBOX_USER = "agent"


# Used only if core's own composer.json can't be read or parsed -- the
# install script there is the authority for all of these, and the plan file's `mediawiki` block
# overrides these in turn.
DEFAULT_INSTALL_PARAMS = {
    "server": "http://localhost:4000",
    "scriptPath": "",
    "adminUser": "Admin",
    "adminPassword": "adminpassword",
}

# core's own composer script, which is what actually installs the wiki.
INSTALL_SCRIPT_NAME = "mw-install:sqlite"
# In node_modules: the sha256 of the package-lock.json of the last npm ci.
NPM_STAMP_NAME = ".wmf-sbx-package-lock.sha256"

# Shape from mediawiki-core-clean/.env. Read by `docker compose`, which
# isn't running here -- the values that make the test harnesses work are
# the ones in the agent's settings `env` (session.py). This
# file is still written because it's the documented artifact and it's
# where an engineer will look.
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
# LocalSettings.php, whose values it repeats. The settings `env` also has
# API_TESTING_CONFIG_FILE with the absolute path, so an extension's own
# `npm run api-testing` finds it from outside core.
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


class SetupLog:
    """A stand-in for sys.stderr that mirrors this script's own narration
    into a log file and remembers the lines that reported a problem.

    Only *this script's* output: the commands it runs (composer, npm, git)
    inherit the real fd 2 and go straight to the terminal of `wmf-sbx
    create`, which is what we want -- their combined output runs to tens of
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


def deep_merge(base, patch):
    """`patch` merged into `base`, returning a new object.

    Dicts merge key by key. Lists are **unioned** -- base order first,
    then anything new -- rather than replaced. That is unusual enough to
    state: replacing a list would silently drop an entry the engineer
    added, and a duplicate entry is harmless. Scalars are replaced.
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

    Idempotent: nothing is written when the merge changes nothing, so a
    second setup run doesn't churn the file's mtime.

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
        print(f"{path} already has the settings", file=sys.stderr)
        return True
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
    except OSError as e:
        print(f"error: could not write {path} ({e})", file=sys.stderr)
        return False
    # Run as root, the agent's settings.json would be root-owned and
    # Claude Code could not update it. As the agent this is a no-op.
    give_to_agent(path, run=run)
    print(f"merged the settings into {path}", file=sys.stderr)
    return True


def link_into_core(core_path, links, core_readonly=False):
    """Symlink each extension/skin clone into the core clone's
    extensions/ or skins/, so `wfLoadExtension( 'Cite' )` finds it. Returns
    the number of links made or refreshed.

    links: [(link_dir, link_name, target), ...] -- link_dir is 'extensions'
    or 'skins', link_name is the *directory* name to use (which is not the
    manifest's `name` field; see sbx/README.md, Architecture), and
    target is the clone's path, which is the host path.

    Never fatal: a symlink that can't be made is warned about and skipped.
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
        # A ':ro' core is the engineer's; the agent cannot write it.
        print(
            f"{core_path} is read-only (':ro'); skipping the extension/skin "
            "symlinks (drop the ':ro' on core to get them)",
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
                # Idempotent: a second run is harmless, and a link
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
        # Run as root, the link lands owned by root: chown the link
        # *itself* (lchown), not the clone it points at. As the agent this
        # is a no-op.
        try:
            os.lchown(dest, *sandbox_ids())
        except OSError as e:
            print(f"warning: could not chown {dest} ({e})", file=sys.stderr)
        print(f"linked {link_dir}/{link_name} -> {target}", file=sys.stderr)
        linked += 1
    return linked


def running_as_agent():
    """True when this script already runs as SANDBOX_USER: on Lima it runs
    as the agent, where sudo is not available and not needed."""
    try:
        return os.geteuid() == pwd.getpwnam(SANDBOX_USER).pw_uid
    except KeyError:
        return False


def give_to_agent(path, run=subprocess.run):
    """chown a file this script wrote to SANDBOX_USER. Nothing to do when
    the script runs as the agent: the file is the agent's already."""
    if running_as_agent():
        return
    result = run(["sudo", "chown", f"{SANDBOX_USER}:{SANDBOX_USER}", path])
    if result.returncode != 0:
        print(f"warning: could not chown {path} (exit {result.returncode})", file=sys.stderr)


def as_agent(argv, cwd=None, run=subprocess.run):
    """Run argv as SANDBOX_USER with its own $HOME. When the script runs
    as the agent already, argv runs directly (with the script's own
    environment, which on Lima has the proxy variables).

    As root, -H is load-bearing: without it git, composer, and npm write
    their caches and config into /root, and the files they create in the
    clone come out root-owned."""
    if running_as_agent():
        return run(list(argv), cwd=cwd)
    return run(["sudo", "-u", SANDBOX_USER, "-H"] + argv, cwd=cwd)


# Step timings (wmf-sbx create's cost): composer runs with --profile, which
# stamps each output line with its elapsed time, and its output is also
# kept in TIMING_DIR, one file per repo. composer_phases() splits it into
# resolution, download and install, and autoload and scripts; the summary
# goes into the setup log as a `timing:` line.
TIMING_DIR = os.path.join(LOG_DIR, "timings")
PROFILE_LINE = re.compile(r"^\[\s*[\d.]+\s*[KMGT]?i?B/\s*([\d.]+)s\]\s?(.*)$")


def run_tee(argv, cwd, path):
    """Run argv with its output on the terminal (the real stderr, not the
    setup log) and in the file `path`. Returns a CompletedProcess."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
        for line in proc.stdout:
            sys.__stderr__.write(line)
            out.write(line)
        proc.wait()
    return subprocess.CompletedProcess(argv, proc.returncode)


def agent_argv(argv):
    """argv as as_agent runs it."""
    return list(argv) if running_as_agent() else ["sudo", "-u", SANDBOX_USER, "-H"] + argv


def composer_phases(text):
    """{phase: seconds} from `composer --profile` output: `resolve`
    (metadata and the solver, up to the lock file), `install` (downloads
    and extraction), `autoload` (autoload and scripts), `total`. A phase
    whose markers are not in the output is left out."""
    marks = []
    for line in text.splitlines():
        m = PROFILE_LINE.match(line)
        if m:
            marks.append((float(m.group(1)), m.group(2)))
    if not marks:
        return {}

    def first(*prefixes):
        for t, msg in marks:
            if msg.startswith(prefixes):
                return t
        return None
    locked = first("Writing lock file", "Installing dependencies from lock file")
    install = first("Package operations", "Nothing to install")
    autoload = first("Generating autoload files", "Generating optimized autoload")
    end = marks[-1][0]
    phases = {"total": end}
    if locked is not None:
        phases["resolve"] = locked
    if install is not None and autoload is not None:
        phases["install"] = autoload - install
    if autoload is not None:
        phases["autoload"] = end - autoload
    return phases


def timing_line(step, path, seconds, phases=None):
    detail = ""
    if phases:
        detail = " (" + ", ".join(f"{k} {phases[k]:.0f} s" for k in ("resolve", "install", "autoload")
                                  if k in phases) + ")"
    return f"timing: {step} {path}: {seconds:.0f} s{detail}"


def composer_update(repo_path, run=subprocess.run, profile_path=None):
    """True/False for a repo with a composer.json, None for one without.

    Run in the primary too, unlike git_safe_reset: this touches nothing but
    vendor/ (composer.lock is gitignored in core), so there is no work in
    progress for it to disturb -- and when the primary *is* mediawiki/core,
    skipping it would leave no vendor/ at all and the install would fail
    outright."""
    if not os.path.exists(os.path.join(repo_path, "composer.json")):
        return None
    argv = ["composer", "update", "--no-interaction", "--no-progress"]
    if profile_path and run is subprocess.run:
        result = run_tee(agent_argv(argv + ["--profile"]), repo_path, profile_path)
    else:
        result = as_agent(argv, cwd=repo_path, run=run)
    return result.returncode == 0


def dependency_paths(plan, core_path):
    """The clones that the engineer did not name (`requested` false in the
    plan): the dependency walk's. They get no `npm ci` and no `composer
    update` (only core, which every sandbox needs, does), so no
    `node_modules/` or `vendor/` of their own:
    CI installs one only in the repo under test. A sibling's own
    `vendor/` makes phan report `PhanRedefined*` for the classes it shares
    with core's, and an extension that loads its own autoloader
    (`load_composer_autoloader`) can put its versions of a library over
    core's (EventLogging's json-schema). A plan without the field names
    every repo."""
    return {r["path"] for r in plan.get("repos") or []
            if r.get("requested") is False and r["path"] != core_path}


def drop_own_vendor(repo_path, run=subprocess.run):
    """Remove a dependency's `vendor/` that an earlier create left (a warm
    snapshot made when the repo was named). True when one was removed.
    A `vendor/` with files in git is the repo's own, and stays; so does
    one in a directory git cannot read. (The check only reads, so it does
    not go through `run`.)"""
    vendor = os.path.join(repo_path, "vendor")
    if not os.path.isdir(vendor):
        return False
    tracked = subprocess.run(agent_argv(["git", "-C", repo_path, "ls-files", "--", "vendor"]),
                             capture_output=True, text=True)
    if tracked.returncode != 0 or (tracked.stdout or "").strip():
        return False
    return as_agent(["rm", "-rf", "--", vendor], run=run).returncode == 0


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

    Each named extension keeps its own composer update too, as in CI:
    its `composer test` and `composer phan` run from its own vendor/bin.
    A dependency does not (dependency_paths).

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
    give_to_agent(path, run=run)
    print(f"wrote {path}", file=sys.stderr)
    return True


def parse_install_params(core_path, fallback=None):
    """--server / --scriptpath / --pass as core's own composer.json
    mw-install:sqlite script gives them, so the .env we write can't drift
    from the wiki we install. Anything
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
        # empty value; only what the harnesses read changes.
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
    give_to_agent(path, run=run)
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
        print(f"{path} differs from the template; leaving it alone", file=sys.stderr)
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(contents)
    give_to_agent(path, run=run)
    print(f"wrote {path}", file=sys.stderr)
    return True


def npm_install(repo_path, run=subprocess.run):
    """True/False, or None if the repo has no package.json.

    Runs in core (a core step) and in each named clone that has a
    package.json: an extension's own JS tests, linters and selenium
    harness all come from its own node_modules. Not in a dependency
    (dependency_paths), as CI does not: no extension or skin in the test
    sandboxes loads a ResourceLoader file from node_modules/ (checked), so
    the wiki does not need them. Measured cost for the four repos of the test
    sandbox: 29 s.

    `npm ci` rather than `npm install`: core's package-lock.json is
    *tracked*, so an install that touches it leaves the clone dirty and a
    later git safe-reset then refuses. ci is also lockfile-exact and
    faster. Falls back to install when there's no lockfile to be exact
    about.

    CYPRESS_INSTALL_BINARY=0 because a repo that lists cypress would
    otherwise download an 800 MB binary at every create, and most tasks
    never run its Cypress specs. The variable makes the package install
    without the binary; `mw-install-cypress` fetches it on demand."""
    if not os.path.exists(os.path.join(repo_path, "package.json")):
        return None
    lock = os.path.join(repo_path, "package-lock.json")
    stamp = os.path.join(repo_path, "node_modules", NPM_STAMP_NAME)
    if os.path.exists(lock):
        # A warm snapshot's node_modules: ci again only if the
        # lock file changed since it was installed.
        digest = _sha256(lock)
        try:
            with open(stamp, encoding="utf-8") as f:
                if f.read().strip() == digest:
                    print(f"{repo_path}: package-lock.json is as at the last npm ci; "
                          f"skipping it", file=sys.stderr)
                    return True
        except OSError:
            pass
        argv = ["npm", "ci", "--no-audit", "--no-fund"]
    else:
        argv = ["npm", "install", "--no-audit", "--no-fund"]
    # `env`, not run(env=...): as_agent goes through sudo, which drops the
    # environment it is given.
    result = as_agent(["env", "CYPRESS_INSTALL_BINARY=0"] + argv,
                      cwd=repo_path, run=run)
    if result.returncode == 0 and os.path.exists(lock):
        try:
            with open(stamp, "w", encoding="utf-8") as f:
                f.write(digest + "\n")
        except OSError:
            pass
    return result.returncode == 0


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def update_mediawiki(core_path, run=subprocess.run):
    """`update.php` for a wiki that a warm snapshot installed:
    the refreshed code may have new tables or fields. True/False."""
    result = as_agent(["php", "maintenance/run.php", "update", "--quick"],
                      cwd=core_path, run=run)
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
    template documents it."""
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
    script verbatim with the flag on the end."""
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
# (`wfLoadExtension( 'Translate' )`); MediaWiki's
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


def link_phan_paths(core_path, links):
    """For phan: many repos' .phan/config.php name their siblings as
    `../../extensions/<Name>` (and `../../skins/<Name>`), which resolves
    in CI's layout, core/extensions/<Name>, but not at a clone's own host
    path. So beside each linked clone's grandparent directory, make
    `extensions` and `skins` symlinks to core's. Only where the name is
    free: a real directory there is the engineer's layout, left alone.
    Never fatal. Returns the links made."""
    if not links or core_path is None:
        return []
    made = []
    for _link_dir, _link_name, target in links:
        grandparent = os.path.dirname(os.path.dirname(os.path.normpath(target)))
        if grandparent in ("", os.sep):
            continue
        for section in ("extensions", "skins"):
            dest = os.path.join(grandparent, section)
            core_dir = os.path.join(core_path, section)
            if (dest in made or os.path.normpath(dest) == os.path.normpath(core_dir)
                    or os.path.lexists(dest) or not os.path.isdir(core_dir)):
                continue
            try:
                os.symlink(core_dir, dest)
            except OSError as e:
                print(f"warning: could not link {dest} -> {core_dir} for phan ({e})",
                      file=sys.stderr)
                continue
            try:
                os.lchown(dest, *sandbox_ids())
            except OSError:
                pass
            print(f"linked {dest} -> {core_dir} (phan's ../../{section})", file=sys.stderr)
            made.append(dest)
    return made


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


def mediawiki_setup(plan, core_path, core_readonly, links, clones, warnings,
                    parsoid_path=None, run=subprocess.run):
    """Wire the extensions into core, install their PHP and JS deps, and
    install a wiki. Returns 0, or 1 if one of the core steps failed.

    The clones exist already, at the host paths, with their branches
    and resets done (sandbox-repos.sh).

    clones: the paths of the repos the agent can write (not ':ro'), in
    plan order. warnings: a list this appends one line to per non-fatal
    problem, so the summary at the end of a very long create log names
    them all in one place. parsoid_path: this sandbox's writable
    gerrit:mediawiki/services/parsoid clone, if any.
    link_parsoid_checkout points LocalSettings.php at it once
    install_mediawiki has written one to patch (see PARSOID_CANONICAL).

    The whole chain is gated on a writable core: with none there is
    nothing to link into and no wiki to install. The failure policy:
    the per-repo steps warn and continue, the core ones are fatal.
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
            f"{core_path} is read-only (':ro'); skipping the MediaWiki setup "
            "(drop the ':ro' on core to get it)",
            file=sys.stderr,
        )
        return 0

    # Links and composer.local.json first: core's composer update merges
    # every extension's and skin's composer.json that the links make
    # visible. See write_composer_local.
    link_into_core(core_path, links, core_readonly=core_readonly)
    link_phan_paths(core_path, links)
    try:
        write_composer_local(core_path, parsoid_checkout=parsoid_path is not None,
                             run=run)
    except OSError as e:
        warnings.append(f"could not write {core_path}/{COMPOSER_LOCAL_NAME} ({e})")

    dependencies = dependency_paths(plan, core_path)
    for work in clones:
        if work in dependencies:
            dropped = drop_own_vendor(work, run=run)
            if os.path.exists(os.path.join(work, "composer.json")) or dropped:
                print(f"composer update: not in {work}, a dependency"
                      + (" (removed the vendor/ an earlier create left)" if dropped else "")
                      + "; run `composer update` there before its own composer scripts",
                      file=sys.stderr)
            continue
        profile = os.path.join(TIMING_DIR, "composer-" + work.strip("/").replace("/", "_") + ".log")
        start = time.monotonic()
        updated = composer_update(work, run=run, profile_path=profile)
        if updated is not None:
            phases = {}
            if run is subprocess.run and os.path.exists(profile):
                with open(profile, encoding="utf-8", errors="replace") as f:
                    phases = composer_phases(f.read())
            print(timing_line("composer update", work, time.monotonic() - start, phases),
                  file=sys.stderr)
        if updated is False:
            warnings.append(f"composer update failed in {work} (the setup log does not "
                            f"hold composer's output; run `composer update` there to see why)")

    # Core's own npm install is a core step below, and fatal there. Every
    # other clone gets one here, non-fatally: an extension whose JS deps
    # fail should cost that extension's JS tests, not the whole sandbox.
    for work in clones:
        if work == core_path:
            continue
        if work in dependencies:
            if os.path.exists(os.path.join(work, "package.json")):
                print(f"npm ci: not in {work}, a dependency; run `npm ci` there before "
                      f"its own npm scripts", file=sys.stderr)
            continue
        start = time.monotonic()
        installed = npm_install(work, run=run)
        if installed is not None:
            print(timing_line("npm ci", work, time.monotonic() - start), file=sys.stderr)
        if installed is False:
            warnings.append(f"npm install failed in {work} (npm's own log is the newest "
                            f"file in ~/.npm/_logs/)")

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
    if installed is None and not update_mediawiki(core_path, run=run):
        print(f"error: update.php failed in {core_path}", file=sys.stderr)
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


def repo_roles(plan):
    """(core_path, core_readonly, links, clones, parsoid_path) from a Lima
    plan. On Lima each clone is at the host path, so the work path is
    the plan's path. A ':ro' clone is the engineer's: it can be linked and
    read, but composer and npm cannot write it, so it is not in clones."""
    core_path, core_readonly, parsoid_path = None, False, None
    links, clones = [], []
    for repo in plan["repos"]:
        path, is_ro = repo["path"], bool(repo.get("readOnly"))
        if not is_ro:
            clones.append(path)
        if repo.get("canonical") == CORE_CANONICAL:
            core_path, core_readonly = path, is_ro
        elif repo.get("linkName") and repo.get("linkDir"):
            links.append((repo["linkDir"], repo["linkName"], path))
        if repo.get("canonical") == PARSOID_CANONICAL and not is_ro:
            parsoid_path = path
    return core_path, core_readonly, links, clones, parsoid_path


def load_plan(path):
    """The plan (kit.build_plan): a JSON object with `repos`."""
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"could not read the plan file {path}: {e}") from None
    if not isinstance(plan, dict) or not isinstance(plan.get("repos"), list):
        raise RuntimeError(f"{path}: expected a JSON object with a 'repos' list")
    if plan.get("version") != PLAN_VERSION:
        raise RuntimeError(f"{path}: plan version {plan.get('version')!r}, "
                           f"want {PLAN_VERSION}")
    return plan


def write_session(session, home=None, run=subprocess.run):
    """The `session` part of a Lima plan (session.py): merge `env` and
    the engineer's preference `settings` into the agent's
    ~/.claude/settings.json, `claude_json` (the first-run answers) into
    ~/.claude.json, and write each file, a path
    under the agent's home (with its `mode`, if it has one). Returns False if a step failed."""
    home = home or os.path.expanduser("~")
    ok = True
    patch = dict(session.get("settings") or {})
    if session.get("env"):
        patch["env"] = session["env"]
    if patch:
        settings = os.path.join(home, ".claude", "settings.json")
        ok = merge_settings(settings, patch, run=run) and ok
    if session.get("claude_json"):
        ok = merge_settings(os.path.join(home, ".claude.json"), session["claude_json"],
                            run=run) and ok
    for entry in session.get("files") or []:
        rel = entry["path"]
        if not rel.startswith("~/") or ".." in rel.split("/"):
            print(f"error: refusing to write {rel!r}: not a path under ~/", file=sys.stderr)
            ok = False
            continue
        path = os.path.join(home, rel[2:])
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(entry["content"])
            if entry.get("mode") is not None:
                os.chmod(path, entry["mode"])
        except OSError as e:
            print(f"error: could not write {path} ({e})", file=sys.stderr)
            ok = False
            continue
        give_to_agent(path, run=run)
        print(f"wrote {path}", file=sys.stderr)
    return ok


def run_plan(path, run=subprocess.run, log=None):
    """The MediaWiki setup from the plan file at `path`, run by `wmf-sbx
    create` as the agent, outside nono, before the first session. The
    clones, the remotes and the resets are done already
    (sandbox-repos.sh)."""
    try:
        plan = load_plan(path)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    core_path, core_readonly, links, clones, parsoid_path = repo_roles(plan)
    warnings = []
    # The session files first: they need nothing from the slow part, and a
    # failed composer run should still leave a usable session.
    if plan.get("session") and not write_session(plan["session"], run=run):
        warnings.append("the session settings or files were not all written")
    status = mediawiki_setup(plan, core_path, core_readonly, links, clones, warnings,
                             parsoid_path=parsoid_path, run=run)
    if warnings:
        print(f"\n{len(warnings)} step(s) did not succeed:\n"
              + "\n".join(f"    {w}" for w in warnings), file=sys.stderr)
        if log is not None:
            for w in warnings:
                log.record(f"warning: {w}")
    return status


def main(argv=None, run=subprocess.run, log_dir=LOG_DIR):
    """Run the plan named in argv, with stderr through a SetupLog,
    so everything printed also lands in log_dir/wmf-sbx-setup.log and the
    problems in log_dir/wmf-sbx-setup.status. log_dir=None turns both off
    (the tests)."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0].startswith("-"):
        print(__doc__.split("Usage: ", 1)[1].rstrip(), file=sys.stderr)
        return 1
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, SETUP_LOG_NAME) if log_dir else None
    log = SetupLog(sys.stderr, log_path)
    real_stderr = sys.stderr
    sys.stderr = log
    try:
        status = run_plan(argv[0], run=run, log=log)
    except Exception:  # noqa: BLE001 - the report is the whole point
        traceback.print_exc(file=sys.stderr)
        log.record("error: wmf-sbx-setup did not finish (unhandled exception; the "
                   "traceback is in the log)")
        status = 1
    finally:
        sys.stderr = real_stderr
        log.close()
    if log_dir:
        write_status(os.path.join(log_dir, SETUP_STATUS_NAME), status, log.problems,
                     log_path=log.path)
    return status


if __name__ == "__main__":
    sys.exit(main())
