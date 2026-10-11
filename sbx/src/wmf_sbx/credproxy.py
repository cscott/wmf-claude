#!/usr/bin/env python3
"""The host credential proxy (lima-port/HANDOFF-LIMA.md, track L,
option H): the session's Claude credential stays on the host.

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

The proxy forwards only the paths Claude Code uses (L1: /v1/..., and the
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
import socket
import socketserver
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse

UPSTREAM = "api.anthropic.com"
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
            raise CredentialError("the host's Claude Code login has expired; run `claude` "
                                  "on the host once to refresh it")
        return token
    return read


def host_source(env=None, run=subprocess.run, platform=sys.platform):
    """The host's credential as a Source, or None when it has none. A token
    file that other users can read is refused."""
    env = os.environ if env is None else env
    if env.get(API_KEY_VAR):
        return Source("api-key", f"${API_KEY_VAR}", _static(env[API_KEY_VAR]))
    if env.get(TOKEN_VAR):
        return Source("oauth", f"${TOKEN_VAR}", _static(env[TOKEN_VAR]))
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


def _must(token):
    if not token:
        raise CredentialError("the host's Claude Code login is gone; run `claude` on the "
                              "host and log in")
    return token


def check_source(source):
    """The problem with `source` now, as a message, or None."""
    try:
        source.value()
    except CredentialError as e:
        return str(e)
    return None


# -- the proxy ------------------------------------------------------------

def upstream_connection(env=None, ca_files=()):
    """An HTTPS connection to UPSTREAM, through $HTTPS_PROXY if set."""
    env = os.environ if env is None else env
    context = ssl.create_default_context()
    for path in ca_files:
        context.load_verify_locations(cafile=path)
    proxy = env.get("HTTPS_PROXY") or env.get("https_proxy")
    if not proxy:
        return http.client.HTTPSConnection(UPSTREAM, 443, timeout=600, context=context)
    # set_tunnel: a plain-HTTP CONNECT to the proxy, then TLS to UPSTREAM.
    p = urllib.parse.urlsplit(proxy)
    headers = {}
    if p.username:
        cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
    conn = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=600, context=context)
    conn.set_tunnel(UPSTREAM, 443, headers=headers)
    return conn


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy = None   # the CredentialProxy; set per server class

    def log_message(self, *a):
        pass

    def _serve(self):
        start = time.monotonic()
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
        headers["host"] = UPSTREAM
        try:
            conn = self.proxy.connect()
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            self._refuse(502, f"wmf-sbx credential proxy: {UPSTREAM}: {e}", start)
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

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _serve


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class CredentialProxy:
    """One session's proxy. start() binds 127.0.0.1 at a free port and
    serves in a daemon thread; stop() closes it."""

    def __init__(self, source, env=None, ca_files=(), log_path=None, phantom=None):
        self.source = source
        self.env = os.environ if env is None else env
        self.ca_files = tuple(ca_files)
        self.log_path = log_path
        self.phantom = phantom or "wmfsbx-" + secrets.token_urlsafe(32)
        self._server = None
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

    @property
    def port(self):
        return self._server.server_address[1]

    def start(self):
        handler = type("Handler", (_Handler,), {"proxy": self})
        self._server = _Server(("127.0.0.1", 0), handler)
        threading.Thread(target=self._server.serve_forever, args=(0.1,), daemon=True,
                         name="wmf-sbx-credproxy").start()
        return self.port

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def session_env(self):
        """The session's variables for Claude Code in the VM. NO_PROXY for
        GUEST_HOST is in the agent's settings (session.CREDENTIAL_PROXY_ENV)."""
        return {self.source.phantom_var: self.phantom,
                "ANTHROPIC_BASE_URL": f"http://{GUEST_HOST}:{self.port}"}


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


def port_in_use(port, host="127.0.0.1"):
    """Whether something on the host listens on `port`."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, int(port))) == 0
