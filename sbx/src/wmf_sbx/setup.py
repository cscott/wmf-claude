#!/usr/bin/env python3
"""The in-VM half of a Lima sandbox's setup, run as the agent
(lima-port/HANDOFF-LIMA.md §7).

`wmf-sbx create` sends this file on stdin to `python3 - --lima PLAN.json`
in the VM, as the agent, outside nono, before the first session (D7;
mediawiki.py). The clones are there already, at the host paths (D8),
with their remotes and resets (sandbox-repos.sh). This script does the
MediaWiki part: the links of the extensions and skins into core,
composer.local.json, `composer update`, `npm ci`, `.env`, the install,
phpunit.xml and the api-testing config. See sbx/DESIGN-setup-steps.md
§8 for the order and the failure policy: the per-repo steps warn and
continue, the core ones are fatal.

Everything this script prints goes to the terminal and to
LOG_DIR/wmf-sbx-setup.log; the `error:`/`warning:` lines also go to
LOG_DIR/wmf-sbx-setup.status.

It must not import any sibling wmf_sbx module: it runs in the VM alone.
The --settings and --mcp modes are the Docker kit's; phase 6 (the
session) decides what of them stays.

Usage: wmf-sbx-setup --lima PLAN.json
       wmf-sbx-setup --settings PATCH.json [SETTINGS.json]
       wmf-sbx-setup --mcp SERVERS.json [CLAUDE.json]
"""

import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import traceback
import urllib.parse

SANDBOX_HOME = "/home/agent"


# Claude Code's user-level settings, which sbx has already written by the
# time anything here runs (permissions.defaultMode, model, theme...). The
# kit adds its plugin and its permission denies by *merging* into this
# file -- see merge_settings and wmf_sbx_kit.settings_patch.
SANDBOX_SETTINGS_FILE = os.path.join(SANDBOX_HOME, ".claude", "settings.json")


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
# list of its problems. The agent cannot write /var/log.
LOG_DIR = os.path.join(SANDBOX_HOME, ".wmf-sbx")
SETUP_LOG_NAME = "wmf-sbx-setup.log"
SETUP_STATUS_NAME = "wmf-sbx-setup.status"


# The plan that `wmf-sbx create` writes into the VM (mediawiki.py,
# kit.build_plan) and passes to `--lima`.
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
# as root (the Docker kit, the tests), it gives what it writes back to the
# agent (give_to_agent), or git refuses it ("detected dubious ownership").
SANDBOX_USER = "agent"


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
    """Where the report of the last setup run is, in the VM."""
    return os.path.join(log_dir, SETUP_STATUS_NAME)


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
    """Symlink each extension/skin clone into the core clone's
    extensions/ or skins/, so `wfLoadExtension( 'Cite' )` finds it. Returns
    the number of links made or refreshed.

    links: [(link_dir, link_name, target), ...] -- link_dir is 'extensions'
    or 'skins', link_name is the *directory* name to use (which is not the
    manifest's `name` field; see sbx/DESIGN-dependency-walk.md §1), and
    target is the clone's path, which is the host path (D8).

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
    as the agent (`--lima`, lima-port/HANDOFF-LIMA.md §7), where sudo is
    not available and not needed."""
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
    give_to_agent(path, run=run)
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
        old_form = api_testing_config_contents(params, secret_key, indent=4)
        if existing == old_form:
            with open(path, "w", encoding="utf-8") as f:
                f.write(contents)
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


def mediawiki_setup(plan, core_path, core_readonly, links, clones, warnings,
                    parsoid_path=None, run=subprocess.run):
    """Wire the extensions into core, install their PHP and JS deps, and
    install a wiki. Returns 0, or 1 if one of the core steps failed.

    The clones exist already, at the host paths (D8), with their branches
    and resets done (sandbox-repos.sh, lima-port/HANDOFF-LIMA.md §6.1).

    clones: the paths of the repos the agent can write (not ':ro'), in
    plan order. warnings: a list this appends one line to per non-fatal
    problem, so the summary at the end of a very long create log names
    them all in one place. parsoid_path: this sandbox's writable
    gerrit:mediawiki/services/parsoid clone, if any.
    link_parsoid_checkout points LocalSettings.php at it once
    install_mediawiki has written one to patch (see PARSOID_CANONICAL).

    The whole chain is gated on a writable core: with none there is
    nothing to link into and no wiki to install. See
    sbx/DESIGN-setup-steps.md §8 for the order and the failure policy:
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
    try:
        write_composer_local(core_path, parsoid_checkout=parsoid_path is not None,
                             run=run)
    except OSError as e:
        warnings.append(f"could not write {core_path}/{COMPOSER_LOCAL_NAME} ({e})")

    for work in clones:
        if composer_update(work, run=run) is False:
            warnings.append(f"composer update failed in {work}")

    # Core's own npm install is a core step below, and fatal there. Every
    # other clone gets one here, non-fatally: an extension whose JS deps
    # fail should cost that extension's JS tests, not the whole sandbox.
    for work in clones:
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


def lima_repo_roles(plan):
    """(core_path, core_readonly, links, clones, parsoid_path) from a Lima
    plan. On Lima each clone is at the host path (D8), so the work path is
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


def load_lima_plan(path):
    """A Lima plan (create.py's lima_plan): a JSON object with `repos`."""
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
    """The `session` part of a Lima plan (session.py): merge `env` into
    the agent's ~/.claude/settings.json (D4), and write each file, a path
    under the agent's home. Returns False if a step failed."""
    home = home or os.path.expanduser("~")
    ok = True
    if session.get("env"):
        settings = os.path.join(home, ".claude", "settings.json")
        ok = merge_settings(settings, {"env": session["env"]}, run=run) and ok
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
        except OSError as e:
            print(f"error: could not write {path} ({e})", file=sys.stderr)
            ok = False
            continue
        give_to_agent(path, run=run)
        print(f"wrote {path}", file=sys.stderr)
    return ok


def run_lima_setup(argv, run=subprocess.run, log=None):
    """`--lima PLAN.json`: the MediaWiki setup in a Lima sandbox, run by
    `wmf-sbx create` as the agent, outside nono, before the first session
    (lima-port/HANDOFF-LIMA.md §7, D7). The clones, the remotes and the
    resets are done already (sandbox-repos.sh)."""
    if len(argv) != 1:
        print("usage: wmf-sbx-setup --lima PLAN.json", file=sys.stderr)
        return 1
    try:
        plan = load_lima_plan(argv[0])
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    core_path, core_readonly, links, clones, parsoid_path = lima_repo_roles(plan)
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
    """Dispatch on the mode. `--lima` runs with stderr through a SetupLog,
    so everything printed also lands in log_dir/wmf-sbx-setup.log and the
    problems in log_dir/wmf-sbx-setup.status. log_dir=None turns both off
    (the tests)."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--settings"]:
        return run_settings(argv[1:], run=run)
    if argv[:1] == ["--mcp"]:
        return run_mcp(argv[1:], run=run)
    if argv[:1] != ["--lima"]:
        print(__doc__.split("Usage: ", 1)[1].rstrip(), file=sys.stderr)
        return 1
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, SETUP_LOG_NAME) if log_dir else None
    log = SetupLog(sys.stderr, log_path)
    real_stderr = sys.stderr
    sys.stderr = log
    try:
        status = run_lima_setup(argv[1:], run=run, log=log)
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
