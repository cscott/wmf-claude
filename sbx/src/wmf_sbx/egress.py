#!/usr/bin/env python3
"""The host egress proxy for a QEMU sandbox behind `restrict=on`
(README.md, Architecture; HANDOFF-LIMA.md D11).

The guest has no route and no DNS. Its one way out is a QEMU `guestfwd`
at EGRESS_ADDR:EGRESS_PORT, which the QEMU wrapper
(bin/wmf-sbx-qemu, qemu_wrapper.py) connects to `nono proxy` on the host's loopback.
`nono proxy` enforces profiles/wmf-engineer.json (endpoint rules
included) plus the hosts listed here, so the guest gets the same policy
as a fenced+nono session.

Per sandbox, in egress_dir(NAME), all 0600 in a 0700 directory:
  ca.pem, ca.key  the CA `nono proxy` signs intercepted TLS with; made
                  at create, put in the guest by Lima's caCerts.
  allow           extra hosts or URL globs, one per line (`wmf-sbx allow`).
  proxy.json      the proxy's argv and environment (write_config), which
                  the wrapper reads; written at each start.
  port            the port of the proxy the guestfwd connects to now.
  wrapper.pid     the running wrapper (SIGUSR1 starts a new proxy).
  proxy.log       the proxy's log.
  dns.sock        the fake-address resolver (fakedns.py), a thread of
                  the wrapper; names is its mapping, append-only.
  cred.sock       the session's credential proxy (credproxy.py), bound
                  by `resume` while a session runs.
  clip.sock       the session's clipboard listener (clipboard.py), bound
                  by `resume` while a session runs.

The guest reaches the four on EGRESS_ADDR: the proxy on EGRESS_PORT,
the resolver on DNS_PORT, the credential proxy on CRED_PORT, the
clipboard listener on CLIP_PORT. Each is a `guestfwd`, so the guest
needs no route to the host.

`nono proxy --upstream-proxy` sends no credentials (sbx/NOTES.md,
"`--sudo` egress"). When the host's proxy needs them (a nested sandbox
always does), the wrapper runs AuthShim between the two.

The guest gives the proxy no password (`--no-auth`). Only the guest
reaches the guestfwd, and a nono session's own nono, chained to
this proxy, cannot send one (NOTES, item 9).
"""

import base64
import contextlib
import datetime
import json
import os
import platform
import re
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
DNS_PORT = 53
CRED_PORT = 3129
CLIP_PORT = 3130
# A nested sandbox's forwarder to the proxy, on the guest's loopback
# (nested.forward_script). An inner VM's only way out is 192.168.5.2, the
# outer guest's loopback, where Lima and image.guest_proxy_env point it.
# A nono session's nono proxy listens there; for a `sudo` session and
# `exec`, which have no nono, the forwarder does (state FORWARD_KEY).
FORWARD_PORT = 3128
FORWARD_KEY = "egressForward"
PROFILE = os.path.join(kit.REPO_ROOT, "profiles", "wmf-engineer.json")
# The host's own variable that replaces the egress proxy's profile (PROFILE,
# or the auth proxy's): for measurements (lima-port/checks/oauth-l5.sh),
# read when the proxy is started.
PROFILE_VAR = "WMF_SBX_EGRESS_PROFILE"
# The guest's apt sources (/etc/apt/mirrors/debian*.list in the image's
# Debian 13 cloud base) are all on this host.
DEBIAN_DOMAINS = ("deb.debian.org",)
# What create's wmf-claude-setup needs that the profile has not: `nono
# pull` of the base pack (RAN: denied, and the only host missing).
SETUP_DOMAINS = ("registry.nono.sh",)
# The CA's lifetime. It is per sandbox and its key stays on the host; a
# year outlives a sandbox, and `status` shows the expiry.
CA_DAYS = 365
# How long the wrapper waits for a new proxy to listen.
LISTEN_TIMEOUT = 15
# The state key of a sandbox behind the proxy (the default on QEMU).
STATE_KEY = "egress"
STATE_VALUE = "proxy"
WRAPPER = os.path.join(kit.REPO_ROOT, "sbx", "bin", "wmf-sbx-qemu")
# How long `start` waits for Lima's first time sync of the guest.
TIME_SYNC_TIMEOUT = 60


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


def log_file(name, env=None):
    """The proxy's log: its allow and deny decisions (`wmf-sbx logs`)."""
    return _path(name, "proxy.log", env)


def dns_socket(name, env=None):
    return _path(name, "dns.sock", env)


def cred_socket(name, env=None):
    return _path(name, "cred.sock", env)


def clip_socket(name, env=None):
    return _path(name, "clip.sock", env)


def names_file(name, env=None):
    return _path(name, "names", env)


def make_dir(name, env=None):
    """The sandbox's egress directory, 0700, made if missing."""
    return _make_dir(name, env)


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


def extra_domains(name, env=None):
    """The sandbox's `allow` entries: one host or URL glob per line; blank
    lines and `#` comments are skipped."""
    try:
        with open(_path(name, "allow", env), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def set_extra_domains(name, domains, env=None):
    """Write the sandbox's `allow` file (`wmf-sbx allow`)."""
    _make_dir(name, env)
    _write_private(_path(name, "allow", env), "".join(f"{d}\n" for d in domains))


def remove(name, env=None):
    """Delete the sandbox's egress directory (at `rm`)."""
    shutil.rmtree(egress_dir(name, env), ignore_errors=True)


def enabled(state):
    """Whether the sandbox runs behind the host egress proxy."""
    return bool(state) and state.get(STATE_KEY) == STATE_VALUE


def mode(state):
    """The sandbox's mode (README.md, "Modes"): "fenced" behind the host
    egress proxy, else "unfenced"; then "+sudo" or "+nono"."""
    return (("fenced" if enabled(state) else "unfenced")
            + ("+sudo" if (state or {}).get("sudo") else "+nono"))


def qemu_arch(machine=None):
    machine = (machine or platform.machine()).lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    raise EgressError(f"no QEMU architecture for this machine ({machine})")


def wrapper_env(name, env=None, arch=None, which=shutil.which):
    """Write proxy.json, and return the variables that make `limactl
    start` run QEMU through the wrapper (qemu_wrapper.py). Lima's host
    agent inherits them from `limactl start`, so they go on every start
    of the VM: a start without them boots it with a route out."""
    env = os.environ if env is None else env
    arch = arch or qemu_arch()
    real = which(f"qemu-system-{arch}", path=env.get("PATH"))
    if not real:
        raise EgressError(f"qemu-system-{arch} is not on PATH")
    if os.path.realpath(real) == os.path.realpath(WRAPPER):
        raise EgressError(f"qemu-system-{arch} on PATH is the egress wrapper, not QEMU")
    return {f"QEMU_SYSTEM_{arch.upper()}": WRAPPER,
            "WMF_SBX_QEMU_REAL": real,
            "WMF_SBX_EGRESS_CONFIG": write_config(name, env)}


def missing_tools(env=None, arch=None, which=shutil.which):
    """The host commands that a sandbox behind the proxy needs and PATH
    lacks: QEMU (the wrapper runs it), nono (`nono proxy`), openssl (the
    CA, make_ca)."""
    env = os.environ if env is None else env
    try:
        arch = arch or qemu_arch()
    except EgressError:
        return [f"qemu-system-{platform.machine().lower()}"]
    # An env with no PATH finds nothing (not the caller's own PATH).
    return [t for t in (f"qemu-system-{arch}", "nono", "openssl")
            if not which(t, path=env.get("PATH", ""))]


NONO_OLDER = ("warning: the host's nono is {have}, older than the pinned {pin} "
              "(.nono-version) that the guest gets. The egress proxy and the auth "
              "proxy run on the host's nono, and are tested only with the pinned "
              "version. Upgrade it: `brew upgrade nono`, or ./setup.sh on Linux.")


def version_tuple(text):
    """(major, minor, patch) from the first x.y.z in `text`, or None."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def nono_version_warning(pinned, env=None, run=subprocess.run, which=shutil.which):
    """A warning when the host's nono (`nono proxy`) is older than
    `pinned` (.nono-version, which the golden image has); else None.
    Says nothing when nono is missing (missing_tools) or its version
    cannot be read."""
    env = os.environ if env is None else env
    nono = which("nono", path=env.get("PATH", ""))
    pin = version_tuple(pinned)
    if not nono or not pin:
        return None
    try:
        out = run([nono, "--version"], capture_output=True, text=True,
                  timeout=10, env=dict(env)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    have = version_tuple(out)
    if have is None or have >= pin:
        return None
    return NONO_OLDER.format(have=".".join(map(str, have)), pin=".".join(map(str, pin)))


TIME_SYNC_DONE = ("Time sync: guest clock adjusted", "Time sync: drift")


def time_synced(log_text, since):
    """Whether Lima's host agent log (ha.stderr.log, JSON lines) has a
    finished time sync at or after `since` (an aware datetime)."""
    for line in log_text.splitlines():
        try:
            entry = json.loads(line)
            stamp = datetime.datetime.fromisoformat(entry["time"])
        except (ValueError, KeyError, TypeError):
            continue
        if stamp >= since and str(entry.get("msg", "")).startswith(TIME_SYNC_DONE):
            return True
    return False


def wait_time_sync(log_path, since, timeout=TIME_SYNC_TIMEOUT, clock=time.monotonic,
                   sleep=time.sleep):
    """Wait for Lima's first time sync after a start. The guest has no
    NTP behind restrict=on, and nono's leaf certificates start at "now":
    a guest clock behind the host's fails TLS through the proxy until the
    sync (sbx/NOTES.md, "`--sudo` egress", item 7). True once synced,
    False when the time runs out."""
    deadline = clock() + timeout
    while True:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                if time_synced(f.read(), since):
                    return True
        except OSError:
            pass
        if clock() >= deadline:
            return False
        sleep(0.5)


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
    on stdout, which the wrapper sends to /dev/null."""
    # Imported here: session and nested import vm, which imports this module.
    from . import nested as nested_mod
    from . import session as session_mod
    from . import authproxy as authproxy_mod
    auth = authproxy_mod.enabled(state_mod.load(name, env))
    profile = ((os.environ if env is None else env).get(PROFILE_VAR)
               or (authproxy_mod.write_profile(env) if auth else PROFILE))
    argv = [nono, "proxy", "-v", "--no-auth", "--listen", "127.0.0.1", "--profile", profile]
    domains = (list(session_mod.REGISTRY_DOMAINS) + list(DEBIAN_DOMAINS)
               + list(SETUP_DOMAINS))
    # A nested sandbox's inner VMs reach the network through the session's
    # nono proxy, whose upstream is this one: what its session grants
    # (nested.session_grants), this proxy must allow too.
    if (state_mod.load(name, env) or {}).get("nested"):
        domains += list(nested_mod.DOMAINS)
    for domain in dict.fromkeys(domains + extra_domains(name, env)):
        argv += ["--allow-domain", domain]
    argv += ["--proxy-ca-cert", ca_cert(name, env), "--proxy-ca-key", ca_key(name, env),
             "--log-file", log_file(name, env)]
    return argv


def proxy_config(name, env=None, nono="nono"):
    """What the wrapper needs to start the proxy: its argv and the
    variables to set, the upstream (or None), and where to write the port."""
    from . import authproxy as authproxy_mod
    # nono writes its state (update check) there; under a session's nono
    # ~/.local/state/nono is not writable. With the auth proxy, it is
    # shared: nono's OAuth capture store is in it.
    state_home = (authproxy_mod.nono_state_dir(env)
                  if authproxy_mod.enabled(state_mod.load(name, env))
                  else os.path.join(egress_dir(name, env), "state"))
    return {"argv": proxy_argv(name, env, nono=nono),
            "env": {"XDG_STATE_HOME": state_home},
            "upstream": upstream(env),
            "portFile": port_file(name, env),
            "pidFile": _path(name, "wrapper.pid", env),
            "dnsSocket": dns_socket(name, env),
            "namesFile": names_file(name, env),
            "credSocket": cred_socket(name, env),
            "clipSocket": clip_socket(name, env)}


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


def proxy_url(host=EGRESS_ADDR, port=EGRESS_PORT):
    return f"http://{host}:{port}"


def guest_environment(name, env=None, forward=False):
    """Variables for the guest's /etc/environment. NODE_USE_ENV_PROXY:
    Node's fetch ignores the proxy variables without it (RAN). With
    `forward`, the proxy is the loopback forwarder (FORWARD_PORT)."""
    url = proxy_url("127.0.0.1", FORWARD_PORT) if forward else proxy_url()
    no_proxy = "localhost,127.0.0.1"
    return {"http_proxy": url, "https_proxy": url, "HTTP_PROXY": url, "HTTPS_PROXY": url,
            "no_proxy": no_proxy, "NO_PROXY": no_proxy, "NODE_USE_ENV_PROXY": "1"}


def guest_files(name, env=None):
    """Files for the guest, {path: content}. With no guest DNS, apt still
    works through the proxy, which gets the name in CONNECT."""
    url = proxy_url()
    return {
        "/etc/apt/apt.conf.d/90wmf-sbx-proxy":
            f'Acquire::http::Proxy "{url}";\nAcquire::https::Proxy "{url}";\n',
    }


ENV_BEGIN = "# wmf-sbx egress begin"
ENV_END = "# wmf-sbx egress end"


def guest_provision(name=None, env=None):
    """Shell for the guest's root provisioning (sandbox-provision.sh, at
    every boot): the proxy variables in /etc/environment, between markers
    (Lima's propagateProxyEnv is off), apt's and git's proxy, and glibc
    straight to the fake-address resolver over TCP (`use-vc`).
    systemd-resolved would try UDP first and go to TCP only after
    timeouts (NOTES, A2.0 item 9), so it is masked."""
    lines = ["# The host egress proxy (egress.py).",
             f"sed -i '/^{ENV_BEGIN}$/,/^{ENV_END}$/d' /etc/environment",
             "cat >> /etc/environment <<'WMF_SBX_ENV'", ENV_BEGIN]
    lines += [f"{k}={v}" for k, v in sorted(guest_environment(name, env).items())]
    lines += [ENV_END, "WMF_SBX_ENV"]
    for path, content in sorted(guest_files(name, env).items()):
        lines += [f"install -d -m 0755 {os.path.dirname(path)}",
                  f"cat > {path} <<'WMF_SBX_FILE'", content.rstrip("\n"), "WMF_SBX_FILE"]
    # git takes the proxy from the variables, as every other client does:
    # a system http.proxy wins over them, and in a nono session nono
    # gives git its own proxy and refuses a connection to this one (RAN:
    # "Failed to connect to 192.168.5.100 port 3128"). The session's git
    # config (session.git_config_env) makes nono's proxy work.
    # --unset-all: a sandbox made before this had http.proxy.
    lines += ["git config --system --unset-all http.proxy || true",
              "if systemctl list-unit-files systemd-resolved.service >/dev/null 2>&1; then",
              "  systemctl disable --now systemd-resolved.service >/dev/null 2>&1 || true",
              "  systemctl mask systemd-resolved.service >/dev/null 2>&1 || true",
              "fi",
              "rm -f /etc/resolv.conf",
              f"printf 'nameserver {EGRESS_ADDR}\\noptions use-vc\\n' > /etc/resolv.conf"]
    return "\n".join(lines) + "\n"


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


def read_port(name, env=None):
    """The running proxy's port, or None."""
    try:
        with open(port_file(name, env), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def listening(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(2)
        try:
            s.connect((host, port))
        except OSError:
            return False
        return True


# `status` warns when the CA has fewer days than this left.
CA_WARN_DAYS = 30


def ca_expiry(name, env=None, run=subprocess.run):
    """The CA's notAfter, a datetime in UTC, or None."""
    try:
        res = run(["openssl", "x509", "-noout", "-enddate", "-in", ca_cert(name, env)],
                  capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return None
    text = (res.stdout or "").strip()
    if res.returncode != 0 or not text.startswith("notAfter="):
        return None
    try:
        when = datetime.datetime.strptime(text.split("=", 1)[1], "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return None
    return when.replace(tzinfo=datetime.timezone.utc)


def status_lines(name, env=None, running=False, run=subprocess.run, now=None):
    """[(text, ok)] for `wmf-sbx status`: the proxy, the CA, the hosts
    `wmf-sbx allow` added."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = []
    port = read_port(name, env)
    if not running:
        out.append(("proxy: not running (the VM is stopped)", True))
    elif port and listening(port):
        out.append((f"proxy: listening on 127.0.0.1:{port}", True))
    else:
        out.append((f"proxy: not listening (port {port or 'unknown'}); "
                    f"`wmf-sbx stop {name}` and start it again", False))
    end = ca_expiry(name, env, run=run)
    if end is None:
        out.append((f"CA {ca_cert(name, env)}: missing or unreadable", False))
    elif end <= now:
        out.append((f"CA expired on {end:%Y-%m-%d}: HTTPS through the proxy fails", False))
    else:
        days = (end - now).days
        # Soon is a warning, not a failure: the sandbox still works.
        out.append((f"CA expires {end:%Y-%m-%d}"
                    + (f" (warning: in {days} days; make a new sandbox before then)"
                       if days < CA_WARN_DAYS else ""), True))
    extra = extra_domains(name, env)
    out.append((f"allowed by `wmf-sbx allow`: {', '.join(extra) if extra else 'none'}", True))
    return out


def write_pid(path, pid):
    _write_private(path, f"{pid}\n")


def remove_pid(path, pid):
    """Remove the pid file if it still names `pid` (a newer wrapper's
    stays)."""
    with contextlib.suppress(OSError, ValueError):
        with open(path, encoding="utf-8") as f:
            if int(f.read().strip()) == pid:
                os.unlink(path)


def _check_fwd_path(path):
    # ' ends the sh -c quote, `,` the netdev option, `:` a socat address,
    # and a space splits $(cat ...).
    if any(c in path for c in "', :"):
        raise EgressError(f"cannot use {path!r} in a guestfwd: it has ', comma, : or space")


def guestfwd(port_path):
    """The `guestfwd` netdev option. libslirp splits `cmd:` into argv
    itself, with no shell, so `sh -c`; the port is read at each guest
    connection, so replacing the proxy needs no restart (RAN)."""
    _check_fwd_path(port_path)
    return (f"guestfwd=tcp:{EGRESS_ADDR}:{EGRESS_PORT}-cmd:"
            f"sh -c 'exec socat - TCP:127.0.0.1:$(cat {port_path})'")


def socket_guestfwd(guest_port, sock_path):
    """A `guestfwd` from EGRESS_ADDR:guest_port to a Unix socket on the
    host. A socket with no listener refuses the connection; a port could
    have been taken by another host service since."""
    _check_fwd_path(sock_path)
    return (f"guestfwd=tcp:{EGRESS_ADDR}:{int(guest_port)}-cmd:"
            f"sh -c 'exec socat - UNIX-CONNECT:{sock_path}'")


def guestfwds(config):
    """Every guestfwd of the netdev, comma-separated: the proxy, and the
    resolver, the credential proxy and the clipboard listener when the
    config names them."""
    fwds = [guestfwd(config["portFile"])]
    if config.get("dnsSocket"):
        fwds.append(socket_guestfwd(DNS_PORT, config["dnsSocket"]))
    if config.get("credSocket"):
        fwds.append(socket_guestfwd(CRED_PORT, config["credSocket"]))
    if config.get("clipSocket"):
        fwds.append(socket_guestfwd(CLIP_PORT, config["clipSocket"]))
    return ",".join(fwds)


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
