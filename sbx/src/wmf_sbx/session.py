#!/usr/bin/env python3
"""The Claude session of a Lima sandbox (lima-port/HANDOFF-LIMA.md §8,
phase 6). In nono mode (README.md, "Modes") the agent runs Claude Code under nono,
through upstream's bin/claude. In a `sudo` sandbox (README.md, "`--sudo` sessions") it
runs bin/launch-claude.sh with no nono: the host egress proxy is the
network boundary.

At create (create.py, setup.py --lima), as the agent:
- wmf-claude-setup registers the MCP servers and pulls nono's base pack,
  as lima/guest-install.sh does;
- the session files: ~/.claude/CLAUDE.md (LIMA_HOME_CLAUDE_MD),
  ~/MEDIAWIKI-TESTING.md, and the wiki variables in the `env` key of
  ~/.claude/settings.json (D4), because nono's allow_vars drops them.

At resume (resume.py), launcher_argv() is what runs in the VM.

The credential (track L): the host's Claude credential stays on the
host, behind a proxy that `resume` runs for the session (credproxy.py).
The session gets the proxy's address and a phantom credential. They go
into the VM on stdin, into a 0600 file in /dev/shm (RAM), and the
launcher reads and deletes the file before it starts bin/claude.
"""

import json
import re
import os
import shlex
import subprocess

from . import clipboard as clipboard_mod
from . import credproxy as credproxy_mod
from . import deps as deps_mod
from . import egress as egress_mod
from . import image as image_mod
from . import kit as kit_mod
from . import nested as nested_mod
from . import setup as setup_mod
from . import state as state_mod
from . import template as template_mod
from . import vm as vm_mod

WMF_CLAUDE_DIR = "/opt/wmf-claude"
LAUNCHER = f"{WMF_CLAUDE_DIR}/bin/claude"
WMF_CLAUDE_SETUP = f"{WMF_CLAUDE_DIR}/bin/wmf-claude-setup"
SANDBOX_BACKEND = "lima-sbx"
# A `sudo` sandbox: bin/claude's own launcher, with the plugin and the
# tool layer that bin/claude would pass, and no nono.
SUDO_LAUNCHER = f"{WMF_CLAUDE_DIR}/bin/launch-claude.sh"
SUDO_SETTINGS = f"{WMF_CLAUDE_DIR}/wiring/settings-merge.json"
SUDO_SANDBOX_BACKEND = "lima-sbx-sudo"
# The status line's session segment (bin/statusline.sh), which bin/claude
# sets in a nono session.
SUDO_SESSION = "sudo · host egress proxy"
WIKI_PORT = 4000
KARMA_PORT = 9876


def local_ports():
    """--local-web's list: the wiki, karma, and the guest's ephemeral port
    window (template.EPHEMERAL_PORTS), where chromedriver, Chrome's DevTools
    and Cypress listen. With --listen-port 0, a bind to port 0 gets a port
    in the window, and the window's ports can be connected to. Landlock
    cannot tell loopback from other hosts; the VM's nftables does (it
    allows the agent TCP 80 and 443 only, off the VM)."""
    lo, hi = template_mod.EPHEMERAL_PORTS
    return f"{WIKI_PORT},{KARMA_PORT},{lo}-{hi}"

# Read grants that every session needs; nono grants the launch directory
# only, and the wmf-engineer profile has no Linux system paths (RAN,
# phase 6):
# - /opt/claude-code and /opt/node hold the image's Claude Code and Node
#   (/usr/local/bin has only links to them): without them, EACCES;
# - /etc/php: without it PHP loads no .ini and no extension, and
#   `composer serve` stops ("iconv OR mbstring ... missing");
# - /etc/gitconfig: without it every git command stops ("fatal: unknown
#   error occurred while reading the configuration files"); it also holds
#   the safe.directory entries (sandbox-repos.sh);
# - /etc/bash.bashrc: without it each Bash tool call prints a "Permission
#   denied" line. (~/.bashrc is in the profile's deny list, on purpose, so
#   its line stays.)
# The setup log, which ~/MEDIAWIKI-TESTING.md points at, and fontconfig,
# which Chrome reads to lay out text (RAN, phase 7: both were denied).
READ_GRANTS = (template_mod.HOST_MOUNT_ROOT, "/opt/claude-code", "/opt/node", "/etc/php",
               setup_mod.LOG_DIR, "/etc/fonts")
# The agent's home itself is not readable under the profile (only its
# dot-directories are), so the guide that ~/.claude/CLAUDE.md points at
# needs its own grant (RAN, phase 7: `cat ~/MEDIAWIKI-TESTING.md` was
# denied, path_not_granted).
TESTING_GUIDE_PATH = f"{setup_mod.SANDBOX_HOME}/{kit_mod.TESTING_GUIDE}"
READ_FILE_GRANTS = ("/etc/gitconfig", "/etc/bash.bashrc", TESTING_GUIDE_PATH)
# The profile denies ~/.bashrc, so the skeleton's copy makes every Bash
# call print "/home/agent/.bashrc: Permission denied". The agent needs no
# .bashrc; create removes it, after wmf-claude-setup.
DENIED_DOTFILES = (f"{setup_mod.SANDBOX_HOME}/.bashrc",)
# Write grants in the agent's home: the caches of composer, npm and the
# browser installers, which the agent uses in a session (the profile has
# none; RAN, phase 6). Not ~/.config/composer: the profile denies its
# auth.json, and Landlock cannot deny a path under an allowed one, so nono
# refuses to start (RAN). COMPOSER_HOME is under ~/.cache instead.
ALLOW_GRANTS = tuple(f"{vm_mod.AGENT_HOME}/{p}" for p in (".cache", ".npm"))
COMPOSER_HOME = f"{vm_mod.AGENT_HOME}/.cache/composer-home"
# Write grants for Cypress (image-build.sh makes both, for the agent):
# Electron's config dir (without it, `cypress verify` dies with SIGTRAP),
# and Xvfb's compiled keymaps (without it, Xvfb stops: "Failed to
# activate virtual core keyboard"). Xvfb's socket in /tmp/.X11-unix needs
# no grant (RAN, Xvfb and an X client under the session's flags).
CYPRESS_GRANTS = (f"{vm_mod.AGENT_HOME}/.config/Cypress", "/var/lib/xkb")

# The hosts that npm, composer and the browser installers download from
# (D3, decided: per-session --allow-domain, no new profile). The
# wmf-engineer profile has none of them.
REGISTRY_DOMAINS = (
    "registry.npmjs.org",                     # npm ci, npm install
    "repo.packagist.org", "packagist.org",    # composer metadata
    "github.com", "api.github.com",           # composer: git sources, dist API
    "codeload.github.com",                    # composer: dist archives
    "objects.githubusercontent.com",          # release downloads
    "googlechromelabs.github.io",             # mw-install-browser: versions
    "storage.googleapis.com",                 # mw-install-browser: Chrome
    "download.cypress.io", "cdn.cypress.io",  # mw-install-cypress
)

# For the host credential proxy (credproxy.py), in every session; the
# address and the phantom come per session, in the credential file.
# - NO_PROXY: in a `sudo` sandbox (no nono), the proxy's address, so the
#   API call goes to it directly, not to the egress proxy, which cannot
#   reach it. Under nono, only the loopback (nono's own value): nono 0.80
#   refuses a connect off the loopback (RAN: EACCES), so the call goes
#   through nono's proxy, which reaches the address directly
#   (launcher_argv: --upstream-bypass). Claude Code applies its settings
#   `env` after nono, and the value is the same for every session, so two
#   sessions do not clash.
# - ENABLE_TOOL_SEARCH: Claude Code turns tool search off for a base URL
#   that is not Anthropic's unless told that the proxy forwards
#   tool_reference blocks; this one forwards the requests as they are.
# A sandbox behind the egress proxy reaches the credential proxy at
# egress.EGRESS_ADDR instead (resume.start_proxy).
SESSION_NO_PROXY = ",".join(("localhost", "127.0.0.1", credproxy_mod.GUEST_HOST,
                             egress_mod.EGRESS_ADDR))
NONO_NO_PROXY = "localhost,127.0.0.1"
# In every agent command in the sandbox (the session's settings `env`, and
# `wmf-sbx exec`): the sandbox's name. bin/git-review-check reads it to
# know that it runs in a sandbox, where `local` is the host's repo.
SANDBOX_NAME_VAR = "SANDBOX_NAME"

CREDENTIAL_PROXY_ENV = {"NO_PROXY": SESSION_NO_PROXY, "no_proxy": SESSION_NO_PROXY,
                        "ENABLE_TOOL_SEARCH": "true"}

# The state key of the variables that `wmf-sbx exec` gives a command as the
# agent: the session's `env`, less the credential proxy's.
EXEC_ENV_KEY = "sessionEnv"


def exec_env(session_env_vars):
    """The session's variables for `wmf-sbx exec`: the wiki, test and
    composer variables, the git identity and settings.yaml `environment:`;
    not the credential proxy's or the clipboard's (exec runs no Claude
    Code)."""
    return {k: v for k, v in session_env_vars.items()
            if k not in CREDENTIAL_PROXY_ENV and k not in clipboard_env()
            and not k.startswith("ANTHROPIC_")}


# Reads the credential file (if any), deletes it, then runs the launcher.
CREDENTIAL_WRAPPER = ('f=$1; shift; if [ -n "$f" ]; then set -a; . "$f"; set +a; '
                      'rm -f -- "$f"; fi; exec "$@"')


class SessionError(Exception):
    """A user-facing problem with the session setup or the credential."""


# The engineer's preferences for every new sandbox, from the `session:`
# section of ~/.config/wmf-sbx/settings.yaml. They go into the agent's
# ~/.claude/settings.json at create, so each sandbox starts with them;
# `sudo` is create's default for --sudo/--no-sudo instead.
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "bypassPermissions")
THEMES = ("dark", "light", "dark-daltonized", "light-daltonized", "dark-ansi", "light-ansi")
PREFERENCE_KEYS = ("model", "permission_mode", "theme", "ste", "settings", "native_copy")


def first_run_marker(env=None):
    """The host file that says the engineer has seen Claude Code's
    first-run questions and warnings in a sandbox (`resume` makes it after
    the first session). Until it exists, `create` answers none of them, so
    the first sandbox shows them all; after it, every new sandbox skips
    them. Delete it to see them again."""
    return os.path.join(os.path.dirname(state_mod.state_dir(env)), "first-run-seen")


def first_run_seen(env=None):
    return os.path.exists(first_run_marker(env))


def mark_first_run_seen(env=None):
    path = first_run_marker(env)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError:
        pass
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]*$")


def preference_settings(prefs, quiet=True, warn=lambda msg: None):
    """The settings.json patch for the `session:` preferences: `model`
    and `permission_mode`. `native_copy` is native_copy's. Raises
    SessionError for a bad value, so that a
    typo stops the create before it builds anything. An unknown key goes
    to `warn`, and is ignored: a newer wmf-sbx may read it
    (lima-port/PLAN-SETTINGS-DEFAULTS.md). `settings` is cli.py's.

    bin/claude refuses every command-line spelling of the permission
    bypass; in a sandbox it is the engineer's choice, made here once, in
    settings. `skipDangerousModePermissionPrompt` skips Claude Code's
    confirmation of that choice. The deny rules of wiring/settings-merge.json
    still apply (bin/claude passes them with --settings)."""
    prefs = prefs or {}
    if not isinstance(prefs, dict):
        raise SessionError("settings.yaml: `session:` must be a mapping")
    unknown = sorted(set(prefs) - set(PREFERENCE_KEYS))
    if unknown:
        warn(f"warning: settings.yaml: unknown `session:` key(s): {', '.join(unknown)}; "
             f"ignored (it has: {', '.join(PREFERENCE_KEYS)})")
    theme = prefs.get("theme")
    if theme is not None and theme not in THEMES:
        raise SessionError(f"settings.yaml: session.theme {theme!r}: want one of {', '.join(THEMES)}")
    for key in ("ste", "settings", "native_copy"):
        value = prefs.get(key)
        if value is not None and not isinstance(value, bool):
            raise SessionError(f"settings.yaml: session.{key} {value!r}: want true or false")
    ste = prefs.get("ste")
    patch = {}
    model = prefs.get("model")
    if model is not None:
        if not isinstance(model, str) or not MODEL_RE.match(model):
            raise SessionError(f"settings.yaml: session.model {model!r} is not a model name")
        patch["model"] = model
    mode = prefs.get("permission_mode")
    if mode is not None:
        if mode not in PERMISSION_MODES:
            raise SessionError(f"settings.yaml: session.permission_mode {mode!r}: want one of "
                               f"{', '.join(PERMISSION_MODES)}")
        patch["permissions"] = {"defaultMode": mode}
        if mode == "bypassPermissions" and quiet:
            patch["skipDangerousModePermissionPrompt"] = True
    if ste is False:
        # bin/session-start.sh leaves out the ASD-STE100 writing rule.
        patch["env"] = {"WMF_CLAUDE_STE": "0"}
    return patch


def native_copy(prefs, egress):
    """Whether a sandbox copies to the host's clipboard (clipboard.py):
    behind the egress proxy (`egress`), unless the `session:` preferences
    have `native_copy: false`."""
    return bool(egress) and (prefs if isinstance(prefs, dict) else {}).get("native_copy") \
        is not False


def claude_json_patch(prefs, trusted_paths, claude_version=None, quiet=True):
    """The patch for the agent's ~/.claude.json: Claude Code's first-run
    questions, answered once for every new sandbox. Onboarding (the theme
    and the security notes) is marked done, with the engineer's `theme`
    if set; each repo of the sandbox is marked trusted, so the session
    does not ask "do you trust this folder?". The repos are the
    engineer's own, named on `create`, and the session runs them under
    nono in a VM in any case."""
    patch = {}
    if quiet:
        patch = {"hasCompletedOnboarding": True,
                 "projects": {path: {"hasTrustDialogAccepted": True,
                                     "hasCompletedProjectOnboarding": True}
                              for path in trusted_paths}}
        if claude_version:
            patch["lastOnboardingVersion"] = claude_version
    theme = (prefs or {}).get("theme")
    if theme:
        patch["theme"] = theme
    return patch


def credential_file_contents(cred):
    """The session's variables as shell assignments, for the wrapper to
    source."""
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in sorted(cred.items()))


def upstream_proxy(env=None, state=None):
    """HOST:PORT of the host's HTTPS proxy as the guest reaches it, or
    None. nono's own proxy must chain to it (--upstream-proxy): with a
    host proxy, a direct connection from the guest may be refused (RAN in
    a cloud sandbox, phase 6). Behind the egress proxy, that proxy: the
    guest has no other way out."""
    if egress_mod.enabled(state):
        return f"{egress_mod.EGRESS_ADDR}:{egress_mod.EGRESS_PORT}"
    guest = image_mod.guest_proxy_env(env)
    url = guest.get("https_proxy") or guest.get("HTTPS_PROXY")
    if not url:
        return None
    hostport = url.split("://", 1)[-1].split("@")[-1].rstrip("/")
    return hostport or None


def sudo_launcher_argv(state, cred_path=None, claude_args=()):
    """argv to run in the guest for one session of a `sudo` sandbox: the
    agent runs bin/launch-claude.sh in the primary clone, with no nono.
    The egress proxy's variables are in the env: `sudo -u agent` drops
    those of /etc/environment, and nono set its own in a nono
    session. NODE_EXTRA_CA_CERTS: Node trusts its own CA list, not the
    system's, which has the sandbox's egress CA (Lima's caCerts)."""
    name = state.get("name") or "<this sandbox>"
    claude = ([SUDO_LAUNCHER, "--plugin-dir", WMF_CLAUDE_DIR, "--settings", SUDO_SETTINGS]
              + list(claude_args))
    wrapper = ["bash", "-c", CREDENTIAL_WRAPPER, "_", cred_path or ""] + claude
    # A nested one's inner VMs reach the proxy through its loopback
    # forwarder (egress.FORWARD_PORT).
    env = dict(egress_mod.guest_environment(
        name, forward=bool(state.get(egress_mod.FORWARD_KEY))))
    env.update({"no_proxy": SESSION_NO_PROXY, "NO_PROXY": SESSION_NO_PROXY,
                "NODE_EXTRA_CA_CERTS": image_mod.SYSTEM_CA_BUNDLE,
                "WMF_CLAUDE_SANDBOX_BACKEND": SUDO_SANDBOX_BACKEND,
                "WMF_CLAUDE_HOME": WMF_CLAUDE_DIR,
                "WMF_CLAUDE_SESSION": SUDO_SESSION,
                "WMF_CLAUDE_SBX_EGRESS": name})
    return vm_mod.agent_argv(wrapper, workdir=state["primaryDir"], env=env)


def launcher_argv(state, cred_path=None, launcher_flags=(), claude_args=(),
                  proxy=None):
    """argv to run in the guest (as the engineer, Lima's user) for one
    nono session: the agent runs bin/claude in the primary clone
    with the grants for the other clones and the mounts. A `sudo`
    sandbox takes no launcher flags (resume refuses them)."""
    if state.get("sudo"):
        if launcher_flags:
            raise SessionError("a sudo sandbox takes no bin/claude flags")
        return sudo_launcher_argv(state, cred_path=cred_path, claude_args=claude_args)
    primary = state["primaryDir"]
    readonly = set(state.get("readOnly") or [])
    grants = []
    for path in state.get("repos") or []:
        if path != primary:
            grants += ["--read" if path in readonly else "--allow", path]
    for path in ALLOW_GRANTS + CYPRESS_GRANTS:
        grants += ["--allow", path]
    for path in READ_GRANTS:
        grants += ["--read", path]
    for path in READ_FILE_GRANTS:
        grants += ["--read-file", path]
    # Behind the egress proxy, every host: the outer proxy is the only
    # allowlist (it has REGISTRY_DOMAINS and `wmf-sbx allow`'s hosts), and
    # the inner one still applies the profile's endpoint rules (NOTES,
    # A2.0 item 9, RAN: a wiki POST got 403).
    for domain in (["*"] if egress_mod.enabled(state) else REGISTRY_DOMAINS):
        grants += ["--allow-domain", domain]
    # The credential proxy and the clipboard listener: nono 0.80 refuses
    # a connect off the loopback, even on an --open-port port, so these
    # go through nono's proxy, which must reach them directly, not
    # through the upstream proxy (RAN with nono 0.80).
    addr = egress_mod.EGRESS_ADDR if egress_mod.enabled(state) else credproxy_mod.GUEST_HOST
    if not egress_mod.enabled(state) and cred_path:
        grants += ["--allow-domain", addr]
    if proxy:
        grants += ["--upstream-proxy", proxy, "--upstream-bypass", addr]
    if state.get("nested"):
        grants += nested_mod.session_grants(state.get("nestedSrc"))
    claude = ([LAUNCHER, f"--local-web={local_ports()}",
               "--listen-port", "0"] + grants
              + list(launcher_flags) + ["--"] + list(claude_args))
    wrapper = ["bash", "-c", CREDENTIAL_WRAPPER, "_", cred_path or ""] + claude
    env = {"WMF_CLAUDE_SANDBOX_BACKEND": SANDBOX_BACKEND,
           # no "Press Enter" pause after bin/claude's notices
           "WMF_CLAUDE_NO_PAUSE": "1"}
    if egress_mod.enabled(state):
        # The session-start hook's egress bullet names the sandbox in the
        # `wmf-sbx allow` command that it gives. WMF_CLAUDE_*, not WMF_SBX_*:
        # wmf-claude reads it, and nono passes only the profile's allow_vars.
        env["WMF_CLAUDE_SBX_EGRESS"] = state.get("name") or "<this sandbox>"
    return vm_mod.agent_argv(wrapper, workdir=primary, env=env)


# In a nested sandbox, the session's phantom and proxy also go into the
# nested marker, root's and readable by the agent only, for the inner
# sandboxes' credential proxies (credproxy.nested_credential). Replaced
# whole, so the marker is never missing.
NESTED_CREDENTIAL_SCRIPT = (
    't=$(mktemp "$1.XXXXXX") && cat > "$t" && chgrp "$2" "$t" && chmod 0640 "$t" '
    '&& mv -f "$t" "$1"')


def put_credential(name, cred, lima=None, nested=False):
    """Write the credential into a new 0600 file in the guest's /dev/shm,
    owned by the agent; return its path. The value goes on stdin. With
    `nested`, into the nested marker too."""
    argv = vm_mod.agent_argv(["sh", "-c", "umask 077; f=$(mktemp /dev/shm/wmf-sbx-cred.XXXXXX)"
                              ' && cat > "$f" && echo "$f"'], workdir="/")
    res = vm_mod.shell(name, argv, lima=lima, input=credential_file_contents(cred),
                       check=False)
    path = (res.stdout or "").strip()
    if res.returncode != 0 or not path.startswith("/dev/shm/wmf-sbx-cred."):
        raise SessionError(f"could not pass the credential into the VM (exit {res.returncode})")
    if nested:
        res = vm_mod.shell(name, ["sudo", "sh", "-c", NESTED_CREDENTIAL_SCRIPT, "_",
                                  image_mod.NESTED_MARKER, vm_mod.AGENT],
                           lima=lima, input=credential_file_contents(cred), check=False)
        if res.returncode != 0:
            raise SessionError(f"could not pass the credential to the inner sandboxes "
                               f"(exit {res.returncode})")
    return path


# -- at create ----------------------------------------------------------------

LIMA_HOME_CLAUDE_MD = """\
## This sandbox

You are in a wmf-sbx sandbox named `@NAME@`: a disposable Lima VM on the
engineer's machine. You run as the user `agent`, @MODE@.

## Repo layout

Each repo here is **your own git clone**, at the same path as the
engineer's checkout on their machine. Nothing you write reaches their
files.

- `local` is the engineer's checkout, read-only. It is current as of
  their last `git fetch`: `git fetch local` (or `git safe-reset local`)
  picks up what they fetched, at once.
- `origin` is the real upstream (Gerrit or GitLab). Push is not possible
  from here.
- A repo the engineer named with `:ro` is read-only for you.

## End your turn with a commit

Committing is the only way your work leaves this sandbox. The engineer
has a remote named `@NAME@` in each of their repos, and fetches with:

```bash
git -C <that repo> fetch @NAME@
```

So commit before you stop, on a branch, even when the work is
unfinished, and say which branch and commit you left. "Changes made,
waiting for instructions to commit" is not a stopping point: it leaves
the engineer unable to see, run, or review any of it. A commit is cheap
to take back -- `git commit --amend`, or `git reset --soft HEAD^` -- on
your next turn.

The engineer takes your branch with `git sbx-pull` on their machine. When
they hand work back to you (they type `/sbx-pull`, or say they edited
your branch or left work for you on theirs), use the `/sbx-pull` skill:
it takes their branch, shows what they changed, and keeps you on the
same branch name.
"""


RUNNING_TESTS_MD = """\
## Running tests

This sandbox holds its own set of repos; `ls "$MW_INSTALL_PATH"/extensions
"$MW_INSTALL_PATH"/skins` and `echo "$PARSOID"` show which. Setup installed
`vendor/` and `node_modules/` in core and in the repos the sandbox was
made for, and a SQLite wiki at `$MW_INSTALL_PATH` with every checked-out
extension and skin loaded. A repo that came in only as a dependency has
neither of its own, as in CI. **Before you run a dependency's own composer
or npm scripts or tests in its directory (after a fix there, say), run
`composer update` and `npm ci` in it**; the setup log lists these repos. Then phan in the other repos can report `PhanRedefined*` for classes
in that new `vendor/`: those are not yours. A step
that failed is listed in `~/.wmf-sbx/wmf-sbx-setup.status` (and
`~/.wmf-sbx/wmf-sbx-setup.log`), with where to find its cause; read them
when something is missing, before you try to install it yourself.
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

Manual testing (`/wmf-claude:manual-test`): the wiki is
`$MW_SERVER` (`http://localhost:4000`), served by `composer serve` from
core; start it first. It is a docroot install: the scripts are at the
root, with no `/w` prefix (`$MW_SERVER/api.php`, `$MW_SERVER/rest.php`,
`$MW_SERVER/index.php?title=Main_Page`). It is plain HTTP on localhost,
with no vhost: do not use `-k`, `--resolve` or a `Host` header, but do
use `--noproxy '*'`. For example:
`curl -sS --noproxy '*' "$MW_SERVER/api.php?action=query&meta=siteinfo&format=json" | jq .query.general.generator`.
The admin account is `Admin` / `adminpassword`. This session already
reaches local web ports (`--local-web`). Tier 2 (the chrome-devtools
MCP) is not set up in this sandbox: use Tier 1, or a selenium test.

Cypress e2e tests (Cite's `selenium-test`, for one) need their own
install, about 800 MB: read ~/MEDIAWIKI-TESTING.md §5.6 first.

`composer phan` sees what CI's phan job sees, for the repos this sandbox
was made for. An undeclared class from an extension that
`.phan/config.php` names and this sandbox does not have is not your error
(~/MEDIAWIKI-TESTING.md §3).
"""


# The mode, in ~/.claude/CLAUDE.md and RUNNING_TESTS_MD: nono mode, then
# a `sudo` sandbox (no nono, so no --local-web either).
NONO_MODE = "under nono, with no sudo"
SUDO_MODE = ("with passwordless sudo. The VM, and the egress proxy\n"
             "on the engineer's machine, are the boundary")
LOCAL_WEB_LINE = "reaches local web ports (`--local-web`)."
SUDO_LOCAL_WEB_LINE = "reaches local web ports."


def home_claude_md(name, sudo=False):
    """The agent's ~/.claude/CLAUDE.md: the sandbox, the repo layout, the
    commit rule, and how to run the tests."""
    head = LIMA_HOME_CLAUDE_MD.replace("@NAME@", name).replace(
        "@MODE@", SUDO_MODE if sudo else NONO_MODE)
    tests = RUNNING_TESTS_MD
    if sudo:
        tests = tests.replace(LOCAL_WEB_LINE, SUDO_LOCAL_WEB_LINE)
    return head + "\n" + tests


def git_identity(run=subprocess.run):
    """GIT_AUTHOR_* and GIT_COMMITTER_* from the host's `git config
    user.name` and `user.email`, or {}. The agent has no identity of its
    own, so its first commit failed (RAN, phase 6); its commits are the
    engineer's to review and push. In the settings `env`, not
    ~/.gitconfig, which nono would need another grant for."""
    out = {}
    for key, names in (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                       ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        try:
            res = run(["git", "config", "--get", key], capture_output=True, text=True,
                      stdin=subprocess.DEVNULL)
        except OSError:
            return {}
        value = (res.stdout or "").strip() if res.returncode == 0 else ""
        if value:
            out.update({n: value for n in names})
    return out


def session_env(resolved_for_kit, readonly=(), identity=None, sudo=False):
    """The `env` key of the agent's settings (D4): the wiki and test
    variables, which nono's allow_vars would drop from the environment,
    and the engineer's git identity. `sudo`: the sandbox runs no nono."""
    # No Composer process timeout: core's `composer test` (parallel-lint)
    # takes more than the default 300 s in a VM (RAN, phase 7: 22 min under
    # QEMU without KVM). The Bash tool has its own timeout.
    env = {"COMPOSER_HOME": COMPOSER_HOME, "COMPOSER_PROCESS_TIMEOUT": "0"}
    env.update(CREDENTIAL_PROXY_ENV)
    if not sudo:
        env.update({"NO_PROXY": NONO_NO_PROXY, "no_proxy": NONO_NO_PROXY})
    env.update(identity or {})
    for canonical, path in resolved_for_kit:
        for var in kit_mod.REPO_ENVIRONMENT_VARS.get(canonical, []):
            env[var] = path
    env.update(kit_mod.mediawiki_env_vars(resolved_for_kit))
    env.update(kit_mod.parsoid_env_vars(resolved_for_kit, readonly_dirs=readonly))
    return env


ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_environment(environment):
    """settings.yaml `environment:` as {NAME: str}. Raises SessionError for a
    bad name, a non-scalar value, or a variable the credential proxy needs
    (CREDENTIAL_PROXY_ENV, ANTHROPIC_*)."""
    out = {}
    for name, value in (environment or {}).items():
        if not isinstance(name, str) or not ENV_NAME_RE.match(name):
            raise SessionError(f"settings.yaml: `environment:` {name!r} is not a variable name")
        if name in CREDENTIAL_PROXY_ENV or name.startswith("ANTHROPIC_"):
            raise SessionError(f"settings.yaml: `environment:` {name} is set by wmf-sbx "
                               f"for the credential proxy")
        if name == SANDBOX_NAME_VAR:
            raise SessionError(f"settings.yaml: `environment:` {name} is set by wmf-sbx "
                               f"(the sandbox's name)")
        if isinstance(value, bool):
            value = "1" if value else "0"
        if not isinstance(value, (str, int, float)):
            raise SessionError(f"settings.yaml: `environment:` {name}: want a string")
        out[name] = str(value)
    return out


def git_config_env(env):
    """GIT_CONFIG_COUNT/KEY_n/VALUE_n that `env` gets, after any that
    settings.yaml `environment:` sets, so that git reaches Gerrit from a
    session:
    - http.proxyAuthMethod=basic: nono's proxy URL has credentials, and
      git aborts the CONNECT unless it sends them at once (RAN). A proxy
      with none (the egress proxy) is not changed.
    - Gerrit's user agent, GERRITUA, when it is set
      (deps.gerrit_git_config)."""
    pairs = [("http.proxyAuthMethod", "basic")] + deps_mod.gerrit_git_config(
        env.get("GERRITUA"))
    try:
        return deps_mod.git_config_env(pairs, env)
    except ValueError:
        raise SessionError("settings.yaml: `environment:` GIT_CONFIG_COUNT: want a number")


def clipboard_env():
    """The settings `env` of a sandbox with native_copy: Claude Code finds
    the `wl-copy` shim, and the shim finds the listener (clipboard.py)."""
    return {"WAYLAND_DISPLAY": clipboard_mod.WAYLAND_DISPLAY,
            clipboard_mod.ADDRESS_VAR: f"{egress_mod.EGRESS_ADDR}:{egress_mod.CLIP_PORT}"}


# The skill for a hand-off from the host (`git sbx-pull` in the guest,
# sbx/helpers/git-sbx-pull). A user skill, not the plugin's: it is wmf-sbx's,
# and its command is `/sbx-pull`.
SBX_PULL_SKILL_SOURCE = os.path.join(kit_mod.SBX_ROOT, "templates", "skills", "sbx-pull",
                                     "SKILL.md")
SBX_PULL_SKILL_PATH = "~/.claude/skills/sbx-pull/SKILL.md"


def sbx_pull_skill():
    with open(SBX_PULL_SKILL_SOURCE, encoding="utf-8") as f:
        return f.read()


def session_plan(name, resolved_for_kit, readonly=(), identity=None, preferences=None,
                 claude_version=None, quiet=True, environment=None, sudo=False,
                 copy=False):
    """The `session` part of the plan, which setup.py --lima writes as the
    agent: settings `env` (wmf-sbx's, then settings.yaml `environment:`)
    and the engineer's preferences, and files under the agent's home.
    `copy` (native_copy): the clipboard shim and its variables."""
    with open(kit_mod.TESTING_GUIDE_SOURCE, encoding="utf-8") as f:
        guide = f.read()
    settings = preference_settings(preferences, quiet=quiet)
    env = session_env(resolved_for_kit, readonly, identity, sudo=sudo)
    env.update(check_environment(environment))
    env.update(settings.pop("env", {}))
    env.update(git_config_env(env))
    env[SANDBOX_NAME_VAR] = name
    files = [
        {"path": "~/.claude/CLAUDE.md", "content": home_claude_md(name, sudo=sudo)},
        {"path": f"~/{kit_mod.TESTING_GUIDE}", "content": guide},
        {"path": SBX_PULL_SKILL_PATH, "content": sbx_pull_skill()},
    ]
    if copy:
        env.update(clipboard_env())
        files.append({"path": clipboard_mod.SHIM_PATH, "content": clipboard_mod.GUEST_SHIM,
                      "mode": 0o755})
    return {
        "env": env,
        "settings": settings,
        "claude_json": claude_json_patch(preferences, [path for _c, path in resolved_for_kit],
                                         claude_version, quiet=quiet),
        "files": files,
    }


def phabricator_config(username):
    """~/.config/wmf-claude/config.json for wmf-claude-setup, which then
    offers the username as its default."""
    return {"phabricatorUsername": username} if username else {}


def wmf_claude_setup(name, username, lima=None, env=None):
    """Run bin/wmf-claude-setup in the VM as the agent: it pulls nono's base
    pack and registers the MCP servers in the agent's ~/.claude.json, as
    lima/guest-install.sh does. Root first gives the agent the one file
    it writes in the read-only tree (the Gerrit MCP's log)."""
    log = "/opt/wmf-claude/gerrit-mcp-server/server.log"
    res = vm_mod.shell(name, ["sudo", "install", "-m", "0644", "-o", vm_mod.AGENT,
                              "-g", vm_mod.AGENT, "/dev/null", log], lima=lima, check=False)
    if res.returncode != 0:
        raise SessionError(f"could not create {log} for the agent")
    config = json.dumps(phabricator_config(username)) + "\n"
    # The cache directories too: nono may refuse a grant for a path that
    # does not exist (ALLOW_GRANTS).
    write = vm_mod.agent_argv(["sh", "-c", "mkdir -p ~/.config/wmf-claude " + " ".join(
        shlex.quote(p) for p in ALLOW_GRANTS + (COMPOSER_HOME,))
        + " && cat > ~/.config/wmf-claude/config.json"])
    vm_mod.shell(name, write, lima=lima, input=config, check=False)
    proxy = vm_mod.guest_proxy_env(name, env)
    # SKIP_UPDATE: the image pins Claude Code (image.claude_release; the
    # version is in the cache key). `claude update` would download ~250 MB
    # per sandbox into an agent-writable ~/.local/bin that sessions do not
    # run (RAN: /usr/local/bin is first on AGENT_PATH).
    argv = vm_mod.agent_argv([WMF_CLAUDE_SETUP], env=dict(
        proxy, WMF_CLAUDE_SKIP_BUILD="1", WMF_CLAUDE_IN_VM="1", WMF_CLAUDE_SKIP_CHROME="1",
        WMF_CLAUDE_SKIP_UPDATE="1"))
    # Enter: take the default Phabricator username (from config.json).
    res = vm_mod.shell(name, argv, lima=lima, input="\n", check=False, capture=False)
    if res.returncode != 0:
        raise SessionError(f"wmf-claude-setup failed in the VM (exit {res.returncode})")
    # After wmf-claude-setup, which writes its `claude` alias into ~/.bashrc
    # (RAN, phase 7 run 2: the line was back). Sessions run bin/claude
    # directly and need no alias.
    res = vm_mod.shell(name, ["sudo", "rm", "-f", "--"] + list(DENIED_DOTFILES),
                       lima=lima, check=False)
    if res.returncode != 0:
        raise SessionError(f"could not remove {' '.join(DENIED_DOTFILES)}")
