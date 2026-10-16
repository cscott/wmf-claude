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

`nono proxy` reads its capture store once, when it starts, and writes
all of what it holds at each capture (nono's oauth_capture/mod.rs and
persist.rs, RAN in L6: a sandbox whose proxy had started before the
/login in another could not use the login). So a proxy that started
before the store's last change is restarted (`allow.swap`, which keeps
its connections) before its sandbox gets phantoms: refresh_proxy.

The phantoms are copied from sandbox to sandbox: the host keeps the
current `claudeAiOauth` record in credentials_file(), and `resume` syncs
it with the guest's ~/.claude/.credentials.json (sync). Only phantoms
are copied (phantoms()): a record whose refresh token is not one of
nono's is ignored, and so is the rest of the file (it also holds MCP
servers' OAuth tokens).

A refresh revokes the old access and refresh tokens, and of two
refreshes with one refresh token, one fails (RAN, L6). So the guests
never refresh (their copy expires a year later than the real login),
and one process does: one of the running resumes, under a lock
(Coordinator).
"""

import contextlib
import fcntl
import json
import os
import random
import selectors
import socket
import sys
import tempfile
import threading
import time

from . import credproxy as credproxy_mod
from . import egress as egress_mod
from . import state as state_mod
from . import vm as vm_mod

STATE_KEY = "authProxy"
GUEST_CREDENTIALS = f"{vm_mod.AGENT_HOME}/.claude/.credentials.json"
# The guests never refresh: their copy expires this much later than the
# real login (for_guest). A guest's record that expires later than
# REAL_HORIZON_MS from now is one of these, not a login of Claude Code's.
GUEST_EXTRA_MS = 365 * 24 * 3600 * 1000
REAL_HORIZON_MS = 2 * 24 * 3600 * 1000
# A resume refreshes this long (give or take JITTER_SECONDS) before the
# real expiry; after a failed refresh, it tries again RETRY_SECONDS later.
REFRESH_MARGIN_MS = 10 * 60 * 1000
JITTER_SECONDS = 30
# How long a resume waits for another's refresh (a model turn) to finish;
# the refresh in the guest is cut off before that (REFRESH_SCRIPT).
LOCK_WAIT_SECONDS = 180
# The host's own variable that replaces it, in minutes: for measurements
# (lima-port/checks/oauth-l6.sh `l7`: 472 refreshes every 8 minutes).
MARGIN_VAR = "WMF_SBX_AUTH_MARGIN_MINUTES"
RETRY_SECONDS = 300
# Every member syncs its guest this often (a guest that emptied its file,
# or a /login), and once more RECHECK_SECONDS after a `new`.
BACKSTOP_SECONDS = 300
RECHECK_SECONDS = 15
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


def store_file(env=None):
    """nono's OAuth capture store (nono: user_state_dir()/oauth-capture)."""
    return os.path.join(nono_state_dir(env), "nono", "oauth-capture", "providers.json")


def proxy_stale(name, env=None):
    """Whether the sandbox's running proxy started before the capture
    store last changed (the port file is written when a proxy starts)."""
    try:
        started = os.stat(egress_mod.port_file(name, env)).st_mtime
        changed = os.stat(store_file(env)).st_mtime
    except OSError:
        return False
    return started < changed


def refresh_proxy(name, env=None, swap=None):
    """Restart the sandbox's proxy if it is stale, so that it reads the
    store again. Returns True when it was restarted."""
    if not proxy_stale(name, env):
        return False
    if swap is None:
        from . import allow as allow_mod
        swap = allow_mod.swap
    return swap(name, env) is not None


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


def _now_ms():
    return int(time.time() * 1000)


def real_looking(oauth, now_ms=None):
    """Whether a guest's record has Claude Code's own expiry (hours
    ahead), not the far one that sync writes (for_guest)."""
    return oauth["expiresAt"] <= (_now_ms() if now_ms is None else now_ms) + REAL_HORIZON_MS


def is_new(guest, host, now_ms=None):
    """Whether the guest has a login the host has not: Claude Code's own
    (a /login, or a refresh of its own), other tokens, a later expiry."""
    if guest is None or not real_looking(guest, now_ms):
        return False
    return host is None or (guest["accessToken"] != host["accessToken"]
                            and guest["expiresAt"] > host["expiresAt"])


def for_guest(host):
    """The record for a guest: the host's, with an expiry far ahead, so
    that Claude Code there never refreshes it (a Coordinator does)."""
    return dict(host, expiresAt=host["expiresAt"] + GUEST_EXTRA_MS)


def sync(name, lima=None, env=None, now_ms=None):
    """Take a new login from the guest (is_new), then give the guest the
    host's (for_guest) if it has another. Returns "pulled", "pushed", or
    None (nothing to do, or the guest could not be read)."""
    doc, _text = read_guest(name, lima)
    if doc is None:
        return None
    guest, host = phantoms(doc), read_host(env)
    result = None
    if is_new(guest, host, now_ms):
        write_host(guest, env)
        host, result = guest, "pulled"
    if host is None:
        return result
    want = for_guest(host)
    if guest == want:
        return result
    old = doc.get("claudeAiOauth")
    doc = dict(doc, claudeAiOauth=dict(old, **want) if isinstance(old, dict) else want)
    if not write_guest(name, doc, lima):
        return result
    return result or "pushed"


# Refresh in the guest, as the agent, in a throwaway CLAUDE_CONFIG_DIR:
# the login comes on stdin with expiresAt 0; one turn makes Claude Code
# refresh it through the egress proxy (RAN, L6 `trigger`: `claude auth
# status` does not); the new login goes to stdout.
REFRESH_SCRIPT = (
    'umask 077; d=$(mktemp -d) || exit 1; trap \'rm -rf "$d"\' EXIT; '
    'cat > "$d/.credentials.json" && '
    'CLAUDE_CONFIG_DIR="$d" timeout 120 claude -p ok --model haiku < /dev/null '
    '> "$d/turn.log" 2>&1; '
    'rc=$?; cat "$d/.credentials.json"; exit $rc')


def refresh_in_guest(name, host, lima=None, env=None):
    """Refresh the shared login `host` in the sandbox `name`. Returns the
    new record, or None."""
    argv = vm_mod.agent_argv(["sh", "-c", REFRESH_SCRIPT], workdir="/tmp",
                             env=vm_mod.guest_proxy_env(name, env))
    res = vm_mod.shell(name, argv, lima=lima, check=False,
                       input=json.dumps({"claudeAiOauth": dict(host, expiresAt=0)}))
    try:
        new = phantoms(json.loads(res.stdout or ""))
    except ValueError:
        return None
    if new is None or new["accessToken"] == host["accessToken"] or not real_looking(new):
        return None
    return new


class _Lock:
    """flock on auth/refresh.lock, waited for at most `wait` seconds:
    the one refresh at a time. The kernel drops it when its holder dies."""

    def __init__(self, env, wait=LOCK_WAIT_SECONDS):
        _make_dir(env)
        self.path = os.path.join(auth_dir(env), "refresh.lock")
        self.wait = wait
        self.held = False

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        end = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return self
            except BlockingIOError:
                if time.monotonic() >= end:
                    return self
                time.sleep(0.2)

    def __exit__(self, *exc):
        os.close(self.fd)


def members_dir(env=None):
    return os.path.join(auth_dir(env), "members")


def _recv(sock):
    """What the peer sent; b"" when it has gone (closed or reset)."""
    try:
        return sock.recv(4096)
    except OSError:
        return b""


def notify(env=None, skip=None):
    """Send `new` to every running resume's socket in members_dir but
    `skip`. A socket nobody listens on is a resume that died: removed."""
    try:
        names = os.listdir(members_dir(env))
    except FileNotFoundError:
        return 0
    sent = 0
    for leaf in names:
        path = os.path.join(members_dir(env), leaf)
        if not leaf.endswith(".sock") or path == skip:
            continue
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(path)
            sock.sendall(b"new\n")
            sent += 1
        except (ConnectionRefusedError, FileNotFoundError):
            with contextlib.suppress(OSError):
                os.unlink(path)
        except OSError:
            pass
        finally:
            sock.close()
    return sent


class Coordinator:
    """The shared login during a `resume` (L7). Every running resume of an
    auth-proxy sandbox listens on a socket of its own in members_dir().

    - Each one plans the refresh for `margin` before the login's expiry,
      moved by a random up to `jitter` seconds either way. When its time
      comes, it takes the refresh lock (_Lock) and reads the host copy
      again: if another resume refreshed it meanwhile, it only syncs;
      else it refreshes the login in its own guest (refresh_in_guest),
      writes the host copy, and sends `new` to the others (notify). So
      the login is refreshed while any resume runs, whichever of them
      ends, hangs or dies.
    - A resume that gets `new` syncs its guest at once (sync: the new
      phantoms, and a proxy restart if its proxy is stale), and again
      `recheck` seconds later: a request in the moment between the
      refresh and the push gets a 401, and Claude Code may then empty
      its file (L6 `live`, `race`).
    - Each one also syncs every `backstop` seconds, for a guest that
      emptied its file or made a login of its own (/login); one that
      takes a new login from its guest sends `new` too.

    The guests never refresh: sync writes them an expiry far ahead."""

    def __init__(self, name, lima=None, env=None, log=None, swap=None, refresher=None,
                 backstop=BACKSTOP_SECONDS, recheck=RECHECK_SECONDS,
                 margin_ms=None, jitter=JITTER_SECONDS, retry=RETRY_SECONDS,
                 lock_wait=LOCK_WAIT_SECONDS, rng=random.uniform):
        if margin_ms is None:
            minutes = (os.environ if env is None else env).get(MARGIN_VAR)
            margin_ms = (int(float(minutes) * 60 * 1000) if minutes
                         else REFRESH_MARGIN_MS)
        self.name, self.lima, self.env = name, lima, env
        self.log = log or (lambda m: print(m, file=sys.stderr))
        self.swap = swap
        self.refresher = refresher or refresh_in_guest
        self.backstop, self.recheck, self.margin_ms, self.jitter = (
            backstop, recheck, margin_ms, jitter)
        self.retry, self.lock_wait, self.rng = retry, lock_wait, rng
        self.path = None
        self._listener = None
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._sel.register(self._wake_r, selectors.EVENT_READ)
        self._stop = False
        self._thread = None
        self._recheck_at = None
        self._refresh_after = 0
        self._planned = None       # (the expiry it is for, the offset)
        self.refreshed = 0

    # -- the socket ------------------------------------------------------

    def _listen(self):
        """Listen on members/NAME.PID.sock: bound under a temporary name
        and renamed into place once it listens, so that notify never
        finds it refusing (and removes it)."""
        d = members_dir(self.env)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        self.path = os.path.join(d, f"{self.name}.{os.getpid()}.{id(self)}.sock")
        tmp = self.path + ".new"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(tmp)
        os.chmod(tmp, 0o600)
        listener.listen(16)
        os.replace(tmp, self.path)
        self._listener = listener
        self._sel.register(listener, selectors.EVENT_READ)

    def _close(self):
        for key in list(self._sel.get_map().values()):
            if key.fileobj is not self._wake_r:
                self._sel.unregister(key.fileobj)
                key.fileobj.close()
        if self.path:
            with contextlib.suppress(OSError):
                os.unlink(self.path)

    # -- the work --------------------------------------------------------

    def _sync(self):
        try:
            refresh_proxy(self.name, self.env, swap=self.swap)
            result = sync(self.name, self.lima, self.env)
        except Exception as e:     # a failed sync must not end the session
            self.log(f"warning: could not sync the shared login with {self.name} ({e})")
            return None
        if result == "pulled":
            notify(self.env, skip=self.path)
        return result

    def _due(self):
        """Seconds until this resume tries the refresh, or None (no login)."""
        host = read_host(self.env)
        if host is None:
            return None
        if self._planned is None or self._planned[0] != host["expiresAt"]:
            self._planned = (host["expiresAt"], self.rng(-self.jitter, self.jitter))
        due = (host["expiresAt"] - self.margin_ms - _now_ms()) / 1000 + self._planned[1]
        return max(due, self._refresh_after - time.time(), 0)

    def _needs_refresh(self, host):
        return host["expiresAt"] - _now_ms() <= self.margin_ms + self.jitter * 1000

    def _refresh(self):
        """Refresh the login unless another resume did. Returns the new
        record, or None."""
        with _Lock(self.env, wait=self.lock_wait) as lock:
            if not lock.held:
                self._refresh_after = time.time() + self.retry
                self.log(f"warning: another wmf-sbx resume has held the shared login's "
                         f"refresh lock for {self.lock_wait} s; {self.name} tries again in "
                         f"{self.retry} s")
                return None
            host = read_host(self.env)
            if host is None or not self._needs_refresh(host):
                self._sync()               # another resume refreshed it
                return None
            try:
                new = self.refresher(self.name, host, lima=self.lima, env=self.env)
            except Exception as e:
                self.log(f"warning: the shared login's refresh in {self.name} failed ({e})")
                new = None
            if new is None:
                self._refresh_after = time.time() + self.retry
                self.log(f"warning: could not refresh the shared login in {self.name}; "
                         f"trying again in {self.retry} s. If it keeps failing, run /login "
                         f"in a session.")
                return None
            write_host(new, self.env)
        self.refreshed += 1
        notify(self.env, skip=self.path)
        self._sync()
        self._recheck_at = time.monotonic() + self.recheck
        return new

    def _run(self):
        backstop_at = time.monotonic() + self.backstop
        while not self._stop:
            now = time.monotonic()
            timeout = backstop_at - now
            if self._recheck_at is not None:
                timeout = min(timeout, self._recheck_at - now)
            due = self._due()
            if due is not None:
                timeout = min(timeout, due)
            for key, _mask in self._sel.select(max(timeout, 0)):
                sock = key.fileobj
                if sock is self._wake_r:
                    sock.recv(64)
                elif sock is self._listener:
                    conn, _addr = sock.accept()
                    self._sel.register(conn, selectors.EVENT_READ)
                else:
                    data = _recv(sock)
                    if b"new" in data:
                        self._sync()
                        self._recheck_at = time.monotonic() + self.recheck
                    if not data or b"new" in data:
                        self._sel.unregister(sock)
                        sock.close()
            if self._stop:
                break
            now = time.monotonic()
            if self._recheck_at is not None and now >= self._recheck_at:
                self._recheck_at = None
                self._sync()
            if now >= backstop_at:
                backstop_at = now + self.backstop
                self._sync()
            due = self._due()
            if due is not None and due <= 0:
                self._refresh()

    def start(self):
        """Listen; refresh a login that is (nearly) expired before the
        session; sync the guest. Returns the sync's result."""
        self._listen()
        host = read_host(self.env)
        if host is not None and self._needs_refresh(host):
            self._refresh()
        first = self._sync()
        self._thread = threading.Thread(target=self._run, name=f"auth-{self.name}",
                                        daemon=True)
        self._thread.start()
        return first

    def stop(self):
        self._stop = True
        self._wake_w.send(b"x")
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._close()
        self._wake_r.close()
        self._wake_w.close()
        self._sel.close()
        return self._sync()
