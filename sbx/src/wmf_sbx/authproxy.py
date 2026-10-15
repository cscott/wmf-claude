#!/usr/bin/env python3
"""The shared login behind the host egress proxy (track L, L5 and L6 in
lima-port/HANDOFF-LIMA.md; README.md, "Sign-in").

A sandbox with the auth proxy (state AUTH_KEY; `create --auth-proxy`, or
the default behind the egress proxy when the host has no inference
token, choose) runs its egress proxy with profile(): the engineer's
profile plus nono's `claude_code` OAuth capture provider (PROVIDER) and
a route for api.anthropic.com (ROUTE). `/login` in the sandbox goes
through the proxy, which keeps the real tokens in nono's capture store
on the host and gives Claude Code phantoms (RAN, L5). Every auth-proxy
sandbox's proxy uses one nono state directory (nono_state_dir), so a
phantom that one proxy made resolves in all of them.

The phantoms are copied from sandbox to sandbox: the host keeps the
newest `claudeAiOauth` record (by expiresAt) in credentials_file(), and
`resume` syncs it with the guest's ~/.claude/.credentials.json at the
start and end of a session and every SYNC_SECONDS during it (Syncer).
Only phantoms are copied (phantoms()): a record whose refresh token is
not one of nono's is ignored, and so is the rest of the file (it also
holds MCP servers' OAuth tokens).
"""

import contextlib
import json
import os
import sys
import tempfile
import threading

from . import credproxy as credproxy_mod
from . import egress as egress_mod
from . import state as state_mod
from . import vm as vm_mod

STATE_KEY = "authProxy"
GUEST_CREDENTIALS = f"{vm_mod.AGENT_HOME}/.claude/.credentials.json"
SYNC_SECONDS = 60
# The guest's file is read on the host: a bigger one is not a login.
MAX_GUEST_FILE = 1 << 20

# nono's provider for Claude Code, as `nono profile guide` gives it
# (nono 0.78; RAN with 0.79).
PROVIDER = {
    "type": "oauth_capture",
    "token_endpoints": [{
        "host": "https://platform.claude.com",
        "path": "/v1/oauth/token",
        "response_fields": [
            {"path": "access_token", "kind": "opaque", "format": "sk-ant-oat01-{}"},
            {"path": "refresh_token", "kind": "opaque"},
            {"path": "id_token", "kind": "jwt"},
        ],
        "request_body": "auto",
        "request_nonce_fields": ["access_token", "refresh_token"],
    }],
    "api_hosts": ["https://api.anthropic.com"],
}
# What a login may not reach on api.anthropic.com: claude.ai's connectors
# (the engineer's Gmail, Drive, ... through the MCP servers that claude.ai
# has for the account) and the telemetry stream. Everything else is
# allowed: this path is for the features an inference-only token lacks
# (Remote Control, sessions, /usage, the organization's policy).
DENIED_PATHS = ("/v1/mcp_servers", "/v1/mcp_servers/**", "/mcp-registry/**",
                "/api/event_logging/**")
ROUTE = {
    "name": "anthropic_oauth",
    "provider": "claude_code",
    "endpoint_policy": {
        "default": {"decision": "allow"},
        "deny": [{"method": "*", "path": p} for p in DENIED_PATHS],
    },
}
# The keys of a `claudeAiOauth` record that are copied.
OAUTH_KEYS = ("accessToken", "refreshToken", "expiresAt", "scopes", "subscriptionType",
              "rateLimitTier")
ACCESS_PREFIX = "sk-ant-oat01-"
# nono's phantom refresh tokens (RAN: `nono_` and 64 characters).
REFRESH_PREFIX = "nono_"


class AuthProxyError(Exception):
    """A user-facing problem with the choice of sign-in."""


def enabled(state):
    return bool(state) and bool(state.get(STATE_KEY))


def inference_token(env=None):
    """Whether the host has a credential for the credential proxy other
    than its own Claude Code login: $ANTHROPIC_API_KEY,
    $CLAUDE_CODE_OAUTH_TOKEN, the `wmf-sbx login` token file, or (in a
    nested sandbox) the enclosing session's phantom."""
    env = os.environ if env is None else env
    return credproxy_mod.inference_source(env) is not None


def choose(requested, egress, env=None):
    """Whether a new sandbox gets the auth proxy. `requested` is
    --auth-proxy (True), --no-auth-proxy (False), or neither (None).
    --auth-proxy needs the egress proxy; without either flag, the auth
    proxy is used behind the egress proxy when the host has no inference
    token (inference_token)."""
    if requested:
        if not egress:
            raise AuthProxyError(
                "--auth-proxy needs the host egress proxy, and this sandbox is not behind "
                "it; drop --no-egress-proxy (on QEMU), or use --no-auth-proxy and "
                "`wmf-sbx login`")
        return True
    if requested is False or not egress:
        return False
    try:
        return not inference_token(env)
    except credproxy_mod.CredentialError as e:
        raise AuthProxyError(str(e)) from None


def auth_dir(env=None):
    return os.path.join(os.path.dirname(state_mod.state_dir(env)), "auth")


def nono_state_dir(env=None):
    """XDG_STATE_HOME of every auth-proxy sandbox's `nono proxy`: nono's
    OAuth capture store is in it (nono/oauth-capture)."""
    return os.path.join(auth_dir(env), "state")


def credentials_file(env=None):
    return os.path.join(auth_dir(env), "credentials.json")


def profile_file(env=None):
    return os.path.join(auth_dir(env), "profile.json")


def _make_dir(env):
    path = auth_dir(env)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def profile_doc(base=egress_mod.PROFILE):
    with open(base, encoding="utf-8") as f:
        doc = json.load(f)
    doc["credential_providers"] = dict(doc.get("credential_providers") or {},
                                       claude_code=PROVIDER)
    doc["credential_routes"] = [r for r in doc.get("credential_routes") or []
                                if r.get("name") != ROUTE["name"]] + [ROUTE]
    return doc


def write_profile(env=None, base=egress_mod.PROFILE):
    """Write the auth proxy's profile (from `base`, at each proxy start);
    returns its path."""
    _make_dir(env)
    return _write_private(profile_file(env), json.dumps(profile_doc(base), indent=2) + "\n")


def phantoms(doc):
    """The `claudeAiOauth` record of a credentials document, with only
    OAUTH_KEYS, when it holds nono's phantoms; else None."""
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):
        return None
    access, refresh, expires = (oauth.get("accessToken"), oauth.get("refreshToken"),
                                oauth.get("expiresAt"))
    if not (isinstance(access, str) and access.startswith(ACCESS_PREFIX)
            and isinstance(refresh, str) and refresh.startswith(REFRESH_PREFIX)
            and isinstance(expires, int) and not isinstance(expires, bool)):
        return None
    return {k: oauth[k] for k in OAUTH_KEYS if k in oauth}


def _expires(oauth):
    return oauth["expiresAt"] if oauth else -1


def read_host(env=None):
    try:
        with open(credentials_file(env), encoding="utf-8") as f:
            return phantoms(json.load(f))
    except (OSError, ValueError):
        return None


def write_host(oauth, env=None):
    _make_dir(env)
    _write_private(credentials_file(env), json.dumps({"claudeAiOauth": oauth}) + "\n")


def read_guest(name, lima=None):
    """(the guest's credentials document or None, its text or None).
    None, None when it could not be read (a stopped VM)."""
    res = vm_mod.shell(name, vm_mod.agent_argv(
        ["sh", "-c", 'test -f "$1" || exit 3; head -c "$2" "$1"', "_", GUEST_CREDENTIALS,
         str(MAX_GUEST_FILE + 1)], workdir="/tmp"), lima=lima, check=False)
    if res.returncode == 3:
        return {}, ""
    if res.returncode != 0:
        return None, None
    text = res.stdout or ""
    if len(text) > MAX_GUEST_FILE:
        return None, None
    try:
        doc = json.loads(text)
    except ValueError:
        return {}, text
    return (doc if isinstance(doc, dict) else {}), text


WRITE_GUEST = ('umask 077; mkdir -p "$(dirname "$1")" && cat > "$1.wmf-sbx" '
               '&& mv -f "$1.wmf-sbx" "$1"')


def write_guest(name, doc, lima=None):
    res = vm_mod.shell(name, vm_mod.agent_argv(["sh", "-c", WRITE_GUEST, "_", GUEST_CREDENTIALS],
                                               workdir="/tmp"),
                       lima=lima, check=False, input=json.dumps(doc))
    return res.returncode == 0


def sync(name, lima=None, env=None):
    """Make the guest's login and the host's copy the newer of the two.
    Returns "pulled", "pushed", or None (nothing to do, or the guest could
    not be read)."""
    doc, _text = read_guest(name, lima)
    if doc is None:
        return None
    guest, host = phantoms(doc), read_host(env)
    if _expires(guest) > _expires(host):
        write_host(guest, env)
        return "pulled"
    if _expires(host) > _expires(guest):
        old = doc.get("claudeAiOauth")
        doc = dict(doc, claudeAiOauth=dict(old, **host) if isinstance(old, dict) else host)
        return "pushed" if write_guest(name, doc, lima) else None
    return None


class Syncer:
    """sync() now, every `interval` seconds in a thread, and at stop()."""

    def __init__(self, name, lima=None, env=None, interval=SYNC_SECONDS, log=None):
        self.name, self.lima, self.env, self.interval = name, lima, env, interval
        self.log = log or (lambda m: print(m, file=sys.stderr))
        self._stop = threading.Event()
        self._thread = None

    def _sync(self):
        try:
            return sync(self.name, self.lima, self.env)
        except Exception as e:     # a failed sync must not end the session
            self.log(f"warning: could not sync the shared login with {self.name} ({e})")
            return None

    def _run(self):
        while not self._stop.wait(self.interval):
            self._sync()

    def start(self):
        first = self._sync()
        self._thread = threading.Thread(target=self._run, name=f"auth-sync-{self.name}",
                                        daemon=True)
        self._thread.start()
        return first

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return self._sync()
