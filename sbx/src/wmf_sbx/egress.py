#!/usr/bin/env python3
"""The host egress proxy for a QEMU sandbox behind `restrict=on`
(lima-port/PLAN-SUDO.md, A2.2; HANDOFF-LIMA.md D11).

The guest has no route and no DNS. Its one way out is a QEMU `guestfwd`
at EGRESS_ADDR:EGRESS_PORT, which the QEMU wrapper
(bin/wmf-sbx-qemu, qemu_wrapper.py) connects to `nono proxy` on the host's loopback.
`nono proxy` enforces profiles/wmf-engineer.json (endpoint rules
included) plus the hosts listed here, so the guest gets the same policy
as a contained session.

Per sandbox, in egress_dir(NAME), all 0600 in a 0700 directory:
  ca.pem, ca.key  the CA `nono proxy` signs intercepted TLS with; made
                  at create, put in the guest by Lima's caCerts.
  password        what the guest gives the proxy (Proxy-Authorization).
  allow           extra hosts or URL globs, one per line (`wmf-sbx allow`).
  proxy.json      the proxy's argv and environment (write_config), which
                  the wrapper reads; written at each start.
  port            the port of the proxy the guestfwd connects to now.
  wrapper.pid     the running wrapper (SIGUSR1 starts a new proxy).
  proxy.log       the proxy's log.

`nono proxy --upstream-proxy` sends no credentials (sbx/NOTES.md,
"`--sudo` egress"). When the host's proxy needs them (a nested sandbox
always does), the wrapper runs AuthShim between the two.
"""

import base64
import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse

from . import kit
from . import state as state_mod

# The guest's address for the proxy: in QEMU's user network, outside the
# DHCP range, not the gateway (192.168.5.2) or DNS (192.168.5.3).
EGRESS_ADDR = "192.168.5.100"
EGRESS_PORT = 3128
PROXY_USER = "nono"
PROFILE = os.path.join(kit.REPO_ROOT, "profiles", "wmf-engineer.json")
# The guest's apt sources (/etc/apt/mirrors/debian*.list in the image's
# Debian 13 cloud base) are all on this host.
DEBIAN_DOMAINS = ("deb.debian.org",)
# The CA's lifetime. It is per sandbox and its key stays on the host; a
# year outlives a sandbox, and `status` shows the expiry.
CA_DAYS = 365
# How long the wrapper waits for a new proxy to listen.
LISTEN_TIMEOUT = 15


class EgressError(Exception):
    """A user-facing problem with the egress proxy's files or startup."""


def egress_dir(name, env=None):
    return os.path.join(os.path.dirname(state_mod.state_dir(env)), "egress",
                        state_mod.validate_name(name))


def _path(name, leaf, env=None):
    return os.path.join(egress_dir(name, env), leaf)


def ca_cert(name, env=None):
    return _path(name, "ca.pem", env)


def ca_key(name, env=None):
    return _path(name, "ca.key", env)


def port_file(name, env=None):
    return _path(name, "port", env)


def config_file(name, env=None):
    return _path(name, "proxy.json", env)


def _write_private(path, text):
    """Write `text` to `path`, 0600, atomically."""
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


def _make_dir(name, env=None):
    path = egress_dir(name, env)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def make_ca(name, env=None, run=subprocess.run, days=CA_DAYS):
    """Make the sandbox's CA (EC P-256; the key in PKCS#8, as `nono proxy
    --proxy-ca-key` wants it) unless it exists. Returns the cert's path."""
    cert, key = ca_cert(name, env), ca_key(name, env)
    if os.path.isfile(cert) and os.path.isfile(key):
        return cert
    _make_dir(name, env)
    tmp_key, tmp_cert = key + ".new", cert + ".new"
    try:
        old = os.umask(0o077)
        try:
            run(["openssl", "genpkey", "-algorithm", "EC",
                 "-pkeyopt", "ec_paramgen_curve:P-256", "-out", tmp_key],
                check=True, capture_output=True, stdin=subprocess.DEVNULL)
            run(["openssl", "req", "-new", "-x509", "-key", tmp_key, "-out", tmp_cert,
                 "-days", str(days), "-subj", f"/CN=wmf-sbx egress {name}",
                 "-addext", "basicConstraints=critical,CA:TRUE",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
                check=True, capture_output=True, stdin=subprocess.DEVNULL)
        finally:
            os.umask(old)
        os.replace(tmp_key, key)
        os.replace(tmp_cert, cert)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="replace").strip()
        raise EgressError(f"could not make the egress CA for {name}: {err}") from e
    finally:
        for tmp in (tmp_key, tmp_cert):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    return cert


def password(name, env=None):
    """The guest's proxy password, made on first use."""
    path = _path(name, "password", env)
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    _make_dir(name, env)
    value = secrets.token_hex(32)
    _write_private(path, value + "\n")
    return value


def extra_domains(name, env=None):
    """The sandbox's `allow` entries: one host or URL glob per line; blank
    lines and `#` comments are skipped."""
    try:
        with open(_path(name, "allow", env), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def remove(name, env=None):
    """Delete the sandbox's egress directory (at `rm`)."""
    shutil.rmtree(egress_dir(name, env), ignore_errors=True)


def upstream(env=None):
    """The host's HTTPS proxy as a dict {host, port, user, password}, or
    None. Read from the host's own variables (a loopback address stays
    loopback: the proxy runs on the host)."""
    env = os.environ if env is None else env
    url = env.get("https_proxy") or env.get("HTTPS_PROXY")
    if not url:
        return None
    parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    if not parts.hostname or not parts.port:
        raise EgressError(f"cannot use the host's https_proxy {parts.hostname!r}: "
                          "it needs a host and a port")
    return {"host": parts.hostname, "port": parts.port,
            "user": urllib.parse.unquote(parts.username or ""),
            "password": urllib.parse.unquote(parts.password or "")}


def proxy_argv(name, env=None, nono="nono"):
    """`nono proxy` for the sandbox, without --port and --upstream-proxy,
    which the wrapper adds when it starts it. Not --silent: it also stops
    the log of allow/deny decisions (RAN). So the proxy prints a banner
    with the password on stdout, which the wrapper sends to /dev/null."""
    # Imported here: session imports vm, which will import this module.
    from . import session as session_mod
    argv = [nono, "proxy", "-v", "--listen", "127.0.0.1", "--profile", PROFILE]
    for domain in (list(session_mod.REGISTRY_DOMAINS) + list(DEBIAN_DOMAINS)
                   + extra_domains(name, env)):
        argv += ["--allow-domain", domain]
    argv += ["--proxy-ca-cert", ca_cert(name, env), "--proxy-ca-key", ca_key(name, env),
             "--log-file", _path(name, "proxy.log", env)]
    return argv


def proxy_config(name, env=None, nono="nono"):
    """What the wrapper needs to start the proxy: its argv and the
    variables to set (the password goes by NONO_PROXY_PASS, not argv),
    the upstream (or None), and where to write the port."""
    return {"argv": proxy_argv(name, env, nono=nono),
            "env": {"NONO_PROXY_PASS": password(name, env),
                    # nono writes its state (update check) here; under a
                    # session's nono ~/.local/state/nono is not writable.
                    "XDG_STATE_HOME": os.path.join(egress_dir(name, env), "state")},
            "upstream": upstream(env),
            "portFile": port_file(name, env),
            "pidFile": _path(name, "wrapper.pid", env)}


def write_config(name, env=None, nono="nono"):
    """Write proxy.json (0600) for the wrapper; returns its path."""
    _make_dir(name, env)
    config = proxy_config(name, env, nono=nono)
    return _write_private(config_file(name, env), json.dumps(config, indent=2) + "\n")


def read_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def proxy_env(base, config):
    """The environment for `nono proxy`: `base` without NONO_* (a nested
    session's nono sets some, and nono proxy reads e.g. NONO_ALLOW_DOMAIN
    and NONO_UPSTREAM_PROXY), plus the config's variables."""
    env = {k: v for k, v in base.items() if not k.startswith("NONO_")}
    env.update(config["env"])
    return env


def proxy_url(name, env=None, host=EGRESS_ADDR, port=EGRESS_PORT):
    return f"http://{PROXY_USER}:{password(name, env)}@{host}:{port}"


def guest_environment(name, env=None):
    """Variables for the guest's /etc/environment. NODE_USE_ENV_PROXY:
    Node's fetch ignores the proxy variables without it (RAN)."""
    url = proxy_url(name, env)
    no_proxy = "localhost,127.0.0.1"
    return {"http_proxy": url, "https_proxy": url, "HTTP_PROXY": url, "HTTPS_PROXY": url,
            "no_proxy": no_proxy, "NO_PROXY": no_proxy, "NODE_USE_ENV_PROXY": "1"}


def guest_files(name, env=None):
    """Files for the guest, {path: content}. With no guest DNS, apt and
    git still work through the proxy, which gets the name in CONNECT; git
    aborts the CONNECT until proxyAuthMethod is basic (RAN)."""
    url = proxy_url(name, env)
    return {
        "/etc/apt/apt.conf.d/90wmf-sbx-proxy":
            f'Acquire::http::Proxy "{url}";\nAcquire::https::Proxy "{url}";\n',
        "/etc/gitconfig": f"[http]\n\tproxy = {url}\n\tproxyAuthMethod = basic\n",
    }


def free_port():
    """A port free on the loopback now (the proxy binds it next)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port, proc, timeout=LISTEN_TIMEOUT, clock=time.monotonic,
                   sleep=time.sleep):
    """Wait until something accepts on 127.0.0.1:port. Raises EgressError
    when `proc` exits first or the time runs out."""
    deadline = clock() + timeout
    while True:
        if proc.poll() is not None:
            raise EgressError(f"nono proxy exited ({proc.returncode}) before it listened "
                              f"on port {port}")
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        if clock() >= deadline:
            raise EgressError(f"nono proxy did not listen on port {port} within {timeout}s")
        sleep(0.1)


def write_port(path, port):
    """Point the guestfwd at `port`: new guest connections go there."""
    _write_private(path, f"{port}\n")


def set_port(name, port, env=None):
    write_port(port_file(name, env), port)


def write_pid(path, pid):
    _write_private(path, f"{pid}\n")


def remove_pid(path, pid):
    """Remove the pid file if it still names `pid` (a newer wrapper's
    stays)."""
    with contextlib.suppress(OSError, ValueError):
        with open(path, encoding="utf-8") as f:
            if int(f.read().strip()) == pid:
                os.unlink(path)


def guestfwd(port_path):
    """The `guestfwd` netdev option. libslirp splits `cmd:` into argv
    itself, with no shell, so `sh -c`; the port is read at each guest
    connection, so replacing the proxy needs no restart (RAN)."""
    if "'" in port_path or "," in port_path:
        raise EgressError(f"cannot use {port_path!r} in a guestfwd: it has ' or ,")
    return (f"guestfwd=tcp:{EGRESS_ADDR}:{EGRESS_PORT}-cmd:"
            f"sh -c 'exec socat - TCP:127.0.0.1:$(cat {port_path})'")


class AuthShim:
    """A loopback forwarder that adds Proxy-Authorization for the
    upstream proxy: `nono proxy --upstream-proxy` sends none. It reads
    each request head (CONNECT, or a request with an absolute URI),
    replaces any Proxy-Authorization with the upstream's, and then copies
    bytes both ways."""

    MAX_HEAD = 65536

    def __init__(self, host, port, user, password):
        self.upstream = (host, port)
        cred = f"{user}:{password}".encode()
        self.auth = b"Proxy-Authorization: Basic " + base64.b64encode(cred)
        self.sock = None

    def start(self):
        """Listen on 127.0.0.1 (a port the OS picks) and serve in a
        daemon thread. Returns the port."""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        threading.Thread(target=self._serve, daemon=True).start()
        return self.sock.getsockname()[1]

    def close(self):
        if self.sock:
            with contextlib.suppress(OSError):
                self.sock.close()

    def _serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def rewrite(self, head):
        """The request head with the upstream's credential in place of any
        Proxy-Authorization. `head` ends with the blank line."""
        lines = head[:-4].split(b"\r\n")
        kept = [ln for ln in lines[1:] if not ln.lower().startswith(b"proxy-authorization:")]
        return b"\r\n".join([lines[0], self.auth] + kept) + b"\r\n\r\n"

    def _handle(self, client):
        data = b""
        with contextlib.ExitStack() as stack:
            stack.callback(client.close)
            while b"\r\n\r\n" not in data:
                chunk = client.recv(65536)
                if not chunk or len(data) + len(chunk) > self.MAX_HEAD:
                    return
                data += chunk
            end = data.index(b"\r\n\r\n") + 4
            try:
                up = socket.create_connection(self.upstream, timeout=30)
            except OSError:
                with contextlib.suppress(OSError):
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            stack.callback(up.close)
            up.settimeout(None)
            up.sendall(self.rewrite(data[:end]) + data[end:])
            back = threading.Thread(target=_pipe, args=(up, client), daemon=True)
            back.start()
            _pipe(client, up)
            back.join()


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        # A reset on one side: end both, so the other copy stops too.
        for s in (src, dst):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)
