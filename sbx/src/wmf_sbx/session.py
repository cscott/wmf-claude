#!/usr/bin/env python3
"""The Claude session of a Lima sandbox (lima-port/HANDOFF-LIMA.md §8,
phase 6). Contained mode only: the agent runs Claude Code under nono,
through upstream's bin/claude.

At create (create.py, setup.py --lima), as the agent:
- wmf-claude-setup registers the MCP servers and pulls nono's base pack,
  as lima/guest-install.sh does;
- the session files: ~/.claude/CLAUDE.md (LIMA_HOME_CLAUDE_MD),
  ~/MEDIAWIKI-TESTING.md, and the wiki variables in the `env` key of
  ~/.claude/settings.json (D4), because nono's allow_vars drops them.

At resume (resume.py), launcher_argv() is what runs in the VM.

The credential (D6, MVP): a Claude token or API key from the host
(host_credential), given to one session only. It goes into the VM on
stdin, into a 0600 file in /dev/shm (RAM), and the launcher reads and
deletes the file before it starts bin/claude. It is never in a command
line and never on the VM's disk. The Claude process has it in its
environment while it runs.
"""

import json
import os
import shlex
import stat
import subprocess

from . import image as image_mod
from . import kit as kit_mod
from . import setup as setup_mod
from . import template as template_mod
from . import vm as vm_mod

LAUNCHER = "/opt/wmf-claude/bin/claude"
WMF_CLAUDE_SETUP = "/opt/wmf-claude/bin/wmf-claude-setup"
SANDBOX_BACKEND = "lima-sbx"
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
# .bashrc; create removes it.
DENIED_DOTFILES = (f"{setup_mod.SANDBOX_HOME}/.bashrc",)
# Write grants in the agent's home: the caches of composer, npm and the
# browser installers, which the agent uses in a session (the profile has
# none; RAN, phase 6). Not ~/.config/composer: the profile denies its
# auth.json, and Landlock cannot deny a path under an allowed one, so nono
# refuses to start (RAN). COMPOSER_HOME is under ~/.cache instead.
ALLOW_GRANTS = tuple(f"{vm_mod.AGENT_HOME}/{p}" for p in (".cache", ".npm"))
COMPOSER_HOME = f"{vm_mod.AGENT_HOME}/.cache/composer-home"

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

# The host's credential for the session (D6, MVP). The variables first,
# in Claude Code's own order of precedence; then the token file, for a
# token from `claude setup-token`.
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Reads the credential file (if any), deletes it, then runs the launcher.
CREDENTIAL_WRAPPER = ('f=$1; shift; if [ -n "$f" ]; then set -a; . "$f"; set +a; '
                      'rm -f -- "$f"; fi; exec "$@"')


class SessionError(Exception):
    """A user-facing problem with the session setup or the credential."""


def token_file(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "wmf-sbx", "claude-oauth-token")


def host_credential(env=None):
    """{VAR: value} for the session, or {} when the host has none. A token
    file that other users can read is refused."""
    env = os.environ if env is None else env
    for var in CREDENTIAL_VARS:
        if env.get(var):
            return {var: env[var]}
    path = token_file(env)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SessionError(f"{path} can be read by other users; chmod 600 it")
    with open(path, encoding="utf-8") as f:
        token = f.read().strip()
    return {TOKEN_VAR: token} if token else {}


def credential_file_contents(cred):
    """The credential as shell assignments, for the wrapper to source."""
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in sorted(cred.items()))


def upstream_proxy(env=None):
    """HOST:PORT of the host's HTTPS proxy as the guest reaches it, or
    None. nono's own proxy must chain to it (--upstream-proxy): with a
    host proxy, a direct connection from the guest may be refused (RAN in
    a cloud sandbox, phase 6)."""
    guest = image_mod.guest_proxy_env(env)
    url = guest.get("https_proxy") or guest.get("HTTPS_PROXY")
    if not url:
        return None
    hostport = url.split("://", 1)[-1].split("@")[-1].rstrip("/")
    return hostport or None


def launcher_argv(state, cred_path=None, launcher_flags=(), claude_args=(),
                  proxy=None):
    """argv to run in the guest (as the engineer, Lima's user) for one
    contained session: the agent runs bin/claude in the primary clone
    with the grants for the other clones and the mounts."""
    primary = state["primaryDir"]
    readonly = set(state.get("readOnly") or [])
    grants = []
    for path in state.get("repos") or []:
        if path != primary:
            grants += ["--read" if path in readonly else "--allow", path]
    for path in ALLOW_GRANTS:
        grants += ["--allow", path]
    for path in READ_GRANTS:
        grants += ["--read", path]
    for path in READ_FILE_GRANTS:
        grants += ["--read-file", path]
    for domain in REGISTRY_DOMAINS:
        grants += ["--allow-domain", domain]
    if proxy:
        grants += ["--upstream-proxy", proxy]
    claude = ([LAUNCHER, f"--local-web={local_ports()}", "--landlock-only",
               "--listen-port", "0"] + grants
              + list(launcher_flags) + ["--"] + list(claude_args))
    wrapper = ["bash", "-c", CREDENTIAL_WRAPPER, "_", cred_path or ""] + claude
    return vm_mod.agent_argv(wrapper, workdir=primary,
                             env={"WMF_CLAUDE_SANDBOX_BACKEND": SANDBOX_BACKEND})


def put_credential(name, cred, lima=None):
    """Write the credential into a new 0600 file in the guest's /dev/shm,
    owned by the agent; return its path. The value goes on stdin."""
    argv = vm_mod.agent_argv(["sh", "-c", "umask 077; f=$(mktemp /dev/shm/wmf-sbx-cred.XXXXXX)"
                              ' && cat > "$f" && echo "$f"'], workdir="/")
    res = vm_mod.shell(name, argv, lima=lima, input=credential_file_contents(cred),
                       check=False)
    path = (res.stdout or "").strip()
    if res.returncode != 0 or not path.startswith("/dev/shm/wmf-sbx-cred."):
        raise SessionError(f"could not pass the credential into the VM (exit {res.returncode})")
    return path


# -- at create ----------------------------------------------------------------

LIMA_HOME_CLAUDE_MD = """\
## This sandbox

You are in a wmf-sbx sandbox named `@NAME@`: a disposable Lima VM on the
engineer's machine. You run as the user `agent`, under nono, with no sudo.

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
"""


RUNNING_TESTS_MD = """\
## Running tests

This sandbox holds its own set of repos; `ls "$MW_INSTALL_PATH"/extensions
"$MW_INSTALL_PATH"/skins` and `echo "$PARSOID"` show which. Setup installed
`vendor/` and `node_modules/` in every repo, and a SQLite wiki at
`$MW_INSTALL_PATH` with every checked-out extension and skin loaded. A step
that failed is listed in `~/.wmf-sbx/wmf-sbx-setup.status`, and
`~/.wmf-sbx/wmf-sbx-setup.log` says why; read them when something is
missing, before you try to install it yourself.
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


def home_claude_md(name):
    """The agent's ~/.claude/CLAUDE.md: the sandbox, the repo layout, the
    commit rule, and how to run the tests."""
    return LIMA_HOME_CLAUDE_MD.replace("@NAME@", name) + "\n" + RUNNING_TESTS_MD


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


def session_env(resolved_for_kit, readonly=(), identity=None):
    """The `env` key of the agent's settings (D4): the wiki and test
    variables, which nono's allow_vars would drop from the environment,
    and the engineer's git identity."""
    # No Composer process timeout: core's `composer test` (parallel-lint)
    # takes more than the default 300 s in a VM (RAN, phase 7: 22 min under
    # QEMU without KVM). The Bash tool has its own timeout.
    env = {"COMPOSER_HOME": COMPOSER_HOME, "COMPOSER_PROCESS_TIMEOUT": "0"}
    env.update(identity or {})
    for canonical, path in resolved_for_kit:
        for var in kit_mod.REPO_ENVIRONMENT_VARS.get(canonical, []):
            env[var] = path
    env.update(kit_mod.mediawiki_env_vars(resolved_for_kit))
    env.update(kit_mod.parsoid_env_vars(resolved_for_kit, readonly_dirs=readonly))
    return env


def session_plan(name, resolved_for_kit, readonly=(), identity=None):
    """The `session` part of the plan, which setup.py --lima writes as the
    agent: settings `env`, and files under the agent's home."""
    with open(kit_mod.TESTING_GUIDE_SOURCE, encoding="utf-8") as f:
        guide = f.read()
    return {
        "env": session_env(resolved_for_kit, readonly, identity),
        "files": [
            {"path": "~/.claude/CLAUDE.md", "content": home_claude_md(name)},
            {"path": f"~/{kit_mod.TESTING_GUIDE}", "content": guide},
        ],
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
    res = vm_mod.shell(name, ["sudo", "rm", "-f", "--"] + list(DENIED_DOTFILES),
                       lima=lima, check=False)
    if res.returncode != 0:
        raise SessionError(f"could not remove {' '.join(DENIED_DOTFILES)}")
    config = json.dumps(phabricator_config(username)) + "\n"
    # The cache directories too: nono may refuse a grant for a path that
    # does not exist (ALLOW_GRANTS).
    write = vm_mod.agent_argv(["sh", "-c", "mkdir -p ~/.config/wmf-claude " + " ".join(
        shlex.quote(p) for p in ALLOW_GRANTS + (COMPOSER_HOME,))
        + " && cat > ~/.config/wmf-claude/config.json"])
    vm_mod.shell(name, write, lima=lima, input=config, check=False)
    proxy = image_mod.guest_proxy_env(env)
    argv = vm_mod.agent_argv([WMF_CLAUDE_SETUP], env=dict(
        proxy, WMF_CLAUDE_SKIP_BUILD="1", WMF_CLAUDE_IN_VM="1", WMF_CLAUDE_SKIP_CHROME="1"))
    # Enter: take the default Phabricator username (from config.json).
    res = vm_mod.shell(name, argv, lima=lima, input="\n", check=False, capture=False)
    if res.returncode != 0:
        raise SessionError(f"wmf-claude-setup failed in the VM (exit {res.returncode})")
