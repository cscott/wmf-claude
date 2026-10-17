#!/usr/bin/env python3
"""The host credential proxy: the session's Claude credential stays on
the host (DESIGN.md, "The credential proxy and shared login").

`resume` runs one proxy per session, in a thread, on the host's loopback
at a port the OS picks. The guest reaches it at 192.168.5.2:PORT (Lima's
user network). Claude Code in the VM gets ANTHROPIC_BASE_URL for it and a
random **phantom** credential. The proxy refuses a request without the
phantom, puts the real credential in its place, and forwards the request
to api.anthropic.com, streaming the response. The proxy stops when the
session ends.

The real credential (host_source), in this order:
  $ANTHROPIC_API_KEY             -> x-api-key
  $CLAUDE_CODE_OAUTH_TOKEN       -> Authorization: Bearer
  ~/.config/wmf-sbx/claude-oauth-token (from `claude setup-token`; 0600)
  the host's Claude Code login   -> Authorization: Bearer, read again
      (Linux: ~/.claude/.credentials.json; macOS: the keychain item
      "Claude Code-credentials"), so the host's refreshes are used;
      WMF_SBX_HOST_LOGIN=0 leaves it out.

In a nested sandbox, the credential is the enclosing session's phantom,
and the proxy forwards to that session's credential proxy, not to
api.anthropic.com (upstream_url). `resume` writes both into the nested
marker for each session (session.put_credential), and nested_credential
reads them; $CLAUDE_CODE_OAUTH_TOKEN, $ANTHROPIC_API_KEY and
$ANTHROPIC_BASE_URL override them. The inner sandbox chains to the outer
one, and the real credential stays on the outermost host. Anywhere else
$ANTHROPIC_BASE_URL is ignored, so a host's real credential goes only to
api.anthropic.com.

The proxy forwards only the paths Claude Code uses (/v1/..., and the
credential-free /api/hello probe). It logs one line per request (method,
path, status, bytes, seconds) to a file, never a credential or a body.
"""

import base64
import datetime
import http.client
import http.server
import json
import os
import secrets
import shlex
import socket
import socketserver
import ssl
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse

from . import image as image_mod

UPSTREAM = "api.anthropic.com"
BASE_URL_VAR = "ANTHROPIC_BASE_URL"
GUEST_HOST = "192.168.5.2"
# Paths that get the real credential; anything else is refused (403) and
# logged. OPEN_PATHS are forwarded without a credential.
FORWARD_PREFIXES = ("/v1/",)
OPEN_PATHS = {("HEAD", "/api/hello"), ("GET", "/api/hello")}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
AUTH_HEADERS = ("x-api-key", "authorization")

API_KEY_VAR = "ANTHROPIC_API_KEY"
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
KEYCHAIN_SERVICE = "Claude Code-credentials"
# How long a live login read from the keychain is reused (one `security`
# call per request would be slow).
KEYCHAIN_CACHE_SECONDS = 30
# Set to 0 to leave the host's Claude Code login out of the sources.
HOST_LOGIN_VAR = "WMF_SBX_HOST_LOGIN"


class CredentialError(Exception):
    """A user-facing problem with the host's credential."""


def _home(env):
    return env.get("HOME") or os.path.expanduser("~")


def token_file(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(_home(env), ".config")
    return os.path.join(base, "wmf-sbx", "claude-oauth-token")


def claude_credentials_file(env=None):
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(_home(env), ".claude")
    return os.path.join(base, ".credentials.json")


class Source:
    """A real credential: `header` is x-api-key or authorization; `read()`
    returns the header's value now. `kind` is "api-key" or "oauth", which
    decides the phantom's variable in the VM."""

    def __init__(self, kind, describe, read):
        self.kind = kind
        self.describe = describe
        self._read = read

    @property
    def header(self):
        return "x-api-key" if self.kind == "api-key" else "authorization"

    @property
    def phantom_var(self):
        return API_KEY_VAR if self.kind == "api-key" else TOKEN_VAR

    def value(self):
        v = self._read()
        return v if self.kind == "api-key" else f"Bearer {v}"


def _static(value):
    return lambda: value


def live_login_reader(env=None, run=subprocess.run, platform=sys.platform,
                      clock=time.monotonic, now=None):
    """A function that returns the host login's access token, or None if
    the host has no login. It raises CredentialError when the login has
    expired: the proxy cannot refresh it without breaking the host's copy
    (the refresh token rotates)."""
    cache = {}

    def load():
        if platform == "darwin":
            if cache and clock() - cache["at"] < KEYCHAIN_CACHE_SECONDS:
                return cache["doc"]
            res = run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                      capture_output=True, text=True, stdin=subprocess.DEVNULL)
            doc = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None
            cache.update(at=clock(), doc=doc)
            return doc
        path = claude_credentials_file(env)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def read():
        try:
            doc = load()
        except (OSError, ValueError) as e:
            raise CredentialError(f"cannot read the host's Claude Code login ({e})") from e
        oauth = (doc or {}).get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            return None
        expires = oauth.get("expiresAt")
        t = (now or time.time)()
        if isinstance(expires, (int, float)) and expires / 1000 <= t:
            raise CredentialError("the host's Claude Code login has expired; run "
                                  "`wmf-sbx login` on the host for a token that lasts a year")
        return token
    return read


def nested_credential():
    """The enclosing session's variables (its phantom and
    ANTHROPIC_BASE_URL) from the nested marker, or {} outside a nested
    sandbox or before a session."""
    try:
        with open(image_mod.NESTED_MARKER, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k in (API_KEY_VAR, TOKEN_VAR, BASE_URL_VAR):
            words = shlex.split(v)
            if words:
                out[k] = words[0]
    return out


def host_source(env=None, run=subprocess.run, platform=sys.platform):
    """The host's credential as a Source, or None when it has none: an
    inference_source, else the host's own Claude Code login."""
    env = os.environ if env is None else env
    source = inference_source(env)
    if source is not None:
        return source
    if env.get(HOST_LOGIN_VAR, "").strip() == "0":
        return None
    read = live_login_reader(env, run=run, platform=platform)
    try:
        if read() is None:
            return None
    except CredentialError:
        pass   # expired: still the source; resume warns, and the proxy says why
    where = "the keychain" if platform == "darwin" else claude_credentials_file(env)
    return Source("oauth", f"the host's Claude Code login ({where})",
                  lambda: _must(read()))


def inference_source(env=None):
    """The host's credential other than its own Claude Code login, as a
    Source, or None: $ANTHROPIC_API_KEY, $CLAUDE_CODE_OAUTH_TOKEN, the
    enclosing session's phantom (nested), or the token file. A token
    file that other users can read is refused."""
    env = os.environ if env is None else env
    if env.get(API_KEY_VAR):
        return Source("api-key", f"${API_KEY_VAR}", _static(env[API_KEY_VAR]))
    if env.get(TOKEN_VAR):
        return Source("oauth", f"${TOKEN_VAR}", _static(env[TOKEN_VAR]))
    nested = nested_credential()
    for var, kind in ((API_KEY_VAR, "api-key"), (TOKEN_VAR, "oauth")):
        if nested.get(var):
            return Source(kind, f"the enclosing session's phantom ({image_mod.NESTED_MARKER})",
                          _static(nested[var]))
    path = token_file(env)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise CredentialError(f"{path} can be read by other users; chmod 600 it")
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return Source("oauth", path, _static(token))
    return None


def _must(token):
    if not token:
        raise CredentialError("the host's Claude Code login is gone; run `wmf-sbx login` "
                              "on the host")
    return token


def check_source(source):
    """The problem with `source` now, as a message, or None."""
    try:
        source.value()
    except CredentialError as e:
        return str(e)
    return None


# -- the proxy ------------------------------------------------------------

def upstream_url(env=None):
    """(scheme, host, port) the proxy forwards to: in a nested sandbox
    (root's marker), $ANTHROPIC_BASE_URL or the enclosing session's (the
    marker's), else UPSTREAM over HTTPS. A base URL with a path is
    refused: the proxy forwards the request's path as it is."""
    env = os.environ if env is None else env
    if not os.path.exists(image_mod.NESTED_MARKER):
        return ("https", UPSTREAM, 443)
    base = (env.get(BASE_URL_VAR) or nested_credential().get(BASE_URL_VAR) or "").strip()
    if not base:
        return ("https", UPSTREAM, 443)
    u = urllib.parse.urlsplit(base)
    if u.scheme not in ("http", "https") or not u.hostname or u.path.strip("/"):
        raise CredentialError(f"${BASE_URL_VAR} is not http(s)://HOST[:PORT]: {base}")
    return (u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80))


def _tunnel_auth(proxy):
    """(proxy URL parts, CONNECT headers) for a proxy URL with user:password."""
    p = urllib.parse.urlsplit(proxy)
    headers = {}
    if p.username:
        cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
    return p, headers


def upstream_connection(env=None, ca_files=()):
    """A connection to upstream_url: plain HTTP directly (an enclosing
    sandbox's credential proxy, which its guest reaches without a proxy),
    or through a CONNECT to nono's proxy when this runs in a nono session;
    HTTPS through $HTTPS_PROXY if set."""
    env = os.environ if env is None else env
    scheme, host, port = upstream_url(env)
    if scheme == "http":
        # nono 0.80 refuses a connect off the loopback, even on an opened
        # port, and does not open the enclosing proxy's port any more. Its
        # proxy goes to that address directly (session.launcher_argv:
        # --upstream-bypass), as for the clipboard shim.
        proxy = env.get("HTTP_PROXY") or env.get("http_proxy")
        if not (env.get("NONO_PROXY_TOKEN") and proxy):
            return http.client.HTTPConnection(host, port, timeout=600)
        p, headers = _tunnel_auth(proxy)
        conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=600)
        conn.set_tunnel(host, port, headers=headers)
        return conn
    context = ssl.create_default_context()
    for path in ca_files:
        context.load_verify_locations(cafile=path)
    proxy = env.get("HTTPS_PROXY") or env.get("https_proxy")
    if not proxy:
        return http.client.HTTPSConnection(host, port, timeout=600, context=context)
    # set_tunnel: a plain-HTTP CONNECT to the proxy, then TLS to UPSTREAM.
    p, headers = _tunnel_auth(proxy)
    conn = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=600, context=context)
    conn.set_tunnel(host, port, headers=headers)
    return conn


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy = None   # the CredentialProxy; set per server class

    def log_message(self, *a):
        pass

    def end_headers(self):
        """One request per connection. A nono session reaches this proxy
        through nono's HTTP forward proxy, which (0.80) sends the client's
        request on unchanged and copies the response until this side
        closes; on a kept-alive connection it waits, and the session's
        next request on that connection waits too (for minutes on a long
        prompt). send_header sets close_connection."""
        self.send_header("connection", "close")
        super().end_headers()

    def _guarded(self):
        """_serve, but a client that goes away is no error: Claude Code
        closes a streamed response when the engineer interrupts it, and
        a BrokenPipeError traceback would be written over the session's
        screen."""
        start = time.monotonic()
        try:
            self._serve(start)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            self.proxy.log(self.command, self.path.split("?")[0], "-", 0, start,
                           note="the client closed the connection")

    def _serve(self, start):
        # Read the body first: a refusal must not leave it on a kept-alive
        # connection, where it would be read as the next request.
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else None
        path = self.path.split("?")[0]
        open_path = (self.command, path) in OPEN_PATHS
        if not open_path and not path.startswith(FORWARD_PREFIXES):
            self._refuse(403, f"wmf-sbx credential proxy: {path} is not forwarded", start)
            return
        if not open_path and not self.proxy.has_phantom(self.headers):
            self._refuse(403, "wmf-sbx credential proxy: no session credential", start)
            return
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in AUTH_HEADERS}
        if not open_path:
            try:
                headers[self.proxy.source.header] = self.proxy.source.value()
            except CredentialError as e:
                self._refuse(401, f"wmf-sbx credential proxy: {e}", start)
                return
        headers["host"] = self.proxy.upstream_host
        try:
            conn = self.proxy.connect()
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            self._refuse(502, f"wmf-sbx credential proxy: {self.proxy.upstream_host}: {e}",
                         start)
            return
        try:
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            if self.command == "HEAD":
                self.end_headers()
                self.proxy.log(self.command, path, resp.status, 0, start)
                return
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            sent = 0
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                sent += len(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.proxy.log(self.command, path, resp.status, sent, start)
        finally:
            conn.close()

    def _refuse(self, status, why, start):
        msg = json.dumps({"type": "error",
                          "error": {"type": "permission_error" if status == 403
                                    else "authentication_error" if status == 401
                                    else "api_error", "message": why}}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(msg)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(msg)
        self.proxy.log(self.command, self.path.split("?")[0], status, len(msg), start,
                       note=why)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _guarded


class _LogErrors:
    proxy = None

    def handle_error(self, request, client_address):
        """To the proxy's log, never to stderr: stderr is the terminal
        that Claude Code draws its screen on."""
        if self.proxy is not None:
            self.proxy.log_error(traceback.format_exc())


class _Server(_LogErrors, socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _UnixServer(_LogErrors, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """For a sandbox behind the egress proxy, which reaches the proxy by a
    guestfwd to this socket (egress.CRED_PORT)."""
    daemon_threads = True


class CredentialProxy:
    """One session's proxy. start() binds 127.0.0.1 at a free port, or a
    Unix socket, and serves in a daemon thread; stop() closes it."""

    def __init__(self, source, env=None, ca_files=(), log_path=None, phantom=None):
        self.source = source
        self.env = os.environ if env is None else env
        self.ca_files = tuple(ca_files)
        self.log_path = log_path
        self.phantom = phantom or "wmfsbx-" + secrets.token_urlsafe(32)
        _, host, port = upstream_url(self.env)
        self.upstream_host = host if port in (80, 443) else f"{host}:{port}"
        self._server = None
        self._unix_path = None
        self._lock = threading.Lock()

    def has_phantom(self, headers):
        for h in AUTH_HEADERS:
            v = headers.get(h) or ""
            if v.startswith("Bearer "):
                v = v[len("Bearer "):]
            if v and secrets.compare_digest(v, self.phantom):
                return True
        return False

    def connect(self):
        return upstream_connection(self.env, self.ca_files)

    def log(self, method, path, status, size, start, note=None):
        if not self.log_path:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = (f"{stamp} {method} {path} {status} {size}B "
                f"{time.monotonic() - start:.1f}s" + (f" ({note})" if note else "") + "\n")
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass

    def log_error(self, text):
        if not self.log_path:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(f"{stamp} error in the proxy:\n{text}")
            except OSError:
                pass

    @property
    def port(self):
        """The TCP port, or None on a Unix socket."""
        if self._unix_path:
            return None
        return self._server.server_address[1]

    def start(self, unix_path=None):
        """Serve on 127.0.0.1 at a free port (returned), or on the Unix
        socket `unix_path` (0600; one left by an earlier session is
        replaced)."""
        handler = type("Handler", (_Handler,), {"proxy": self})
        if unix_path:
            try:
                os.unlink(unix_path)
            except FileNotFoundError:
                pass
            old = os.umask(0o177)
            try:
                self._server = _UnixServer(unix_path, handler)
            finally:
                os.umask(old)
            self._unix_path = unix_path
            self._unix_ino = os.stat(unix_path).st_ino
        else:
            self._server = _Server(("127.0.0.1", 0), handler)
        self._server.proxy = self
        threading.Thread(target=self._server.serve_forever, args=(0.1,), daemon=True,
                         name="wmf-sbx-credproxy").start()
        return None if unix_path else self.port

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._unix_path:
            # Not a socket that a later session bound in its place.
            try:
                if os.stat(self._unix_path).st_ino == self._unix_ino:
                    os.unlink(self._unix_path)
            except OSError:
                pass
            self._unix_path = None

    def session_env(self, base_url=None):
        """The session's variables for Claude Code in the VM: by default
        the proxy at GUEST_HOST:port; `base_url` for one the guest reaches
        otherwise. NO_PROXY for the address is in the agent's settings
        (session.CREDENTIAL_PROXY_ENV)."""
        return {self.source.phantom_var: self.phantom,
                "ANTHROPIC_BASE_URL": base_url or f"http://{GUEST_HOST}:{self.port}"}


# -- the guest's host block -------------------------------------------------

RULE_TAG = "wmf-sbx-cred-"
CHAINS = ("block", "agent_egress")


def guest_rule_ports_argv():
    """argv (as the engineer) that prints the ports of the proxy rules in
    the guest's host block, one per line."""
    return ["sudo", "-n", "sh", "-c",
            "nft list chain inet wmf_sbx_hostblock block | "
            f"sed -n 's/.*comment \"{RULE_TAG}\\([0-9]*\\)\".*/\\1/p' | sort -u"]


def guest_rules_argv(add=(), remove=()):
    """argv (as the engineer) that opens the host block for the agent to
    192.168.5.2 on each port in `add`, and closes it for each in `remove`.
    The rules are tagged with their port, so that a later resume can
    remove a rule that a crashed one left."""
    lines = []
    for port in remove:
        port = int(port)
        for chain in CHAINS:
            lines.append(
                f"nft -a list chain inet wmf_sbx_hostblock {chain} | "
                f"sed -n 's/.*comment \"{RULE_TAG}{port}\" # handle \\([0-9]*\\)$/\\1/p' | "
                f"while read h; do nft delete rule inet wmf_sbx_hostblock {chain} handle $h; done")
    for port in add:
        port = int(port)
        for chain in CHAINS:
            lines.append(f"nft insert rule inet wmf_sbx_hostblock {chain} ip daddr "
                         f"{GUEST_HOST} tcp dport {port} accept comment \"{RULE_TAG}{port}\"")
    return ["sudo", "-n", "sh", "-ec", "\n".join(lines)]


def socket_in_use(path):
    """Whether something listens on the Unix socket `path`."""
    with socket.socket(socket.AF_UNIX) as s:
        try:
            s.connect(path)
        except OSError:
            return False
        return True
