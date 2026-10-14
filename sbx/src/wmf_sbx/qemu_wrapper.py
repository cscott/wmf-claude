#!/usr/bin/env python3
"""The QEMU wrapper for a sandbox behind the host egress proxy
(lima-port/PLAN-SUDO.md, A2.1), and for every other QEMU sandbox, for
free-page reporting. Lima runs it in place of QEMU
(QEMU_SYSTEM_<ARCH>=bin/wmf-sbx-qemu); wmf-sbx sets:

  WMF_SBX_QEMU_REAL      the real qemu-system-<arch>
  WMF_SBX_EGRESS_CONFIG  the sandbox's proxy.json (egress.write_config)
  WMF_SBX_QEMU_PLAIN     1 for a sandbox with no egress proxy (contained
                         mode): the boot call gets only the balloon.
                         Without it, a boot with no egress config is
                         refused, so that a --sudo sandbox never boots
                         without its proxy by mistake.

Every boot call has its -virtfs mounts moved to multifunction slots
(spread_virtfs: Lima gives each its own slot, and the root bus runs out
at 30 or 31 devices), and gets a virtio balloon with free-page reporting
(BALLOON): the guest gives the pages it frees back to the host. Without
it QEMU keeps all the memory that the guest ever touched, and a few
sandboxes that once ran an image build use up the host's memory (RAN:
the host's OOM killer took a 16 GiB nested sandbox, holding 15 GB).

Lima also calls QEMU to probe it (`--version`, `-netdev help`, ...). A
call without -pidfile or -qmp is a probe, and runs the real QEMU
unchanged. The boot call:

- must have exactly one `-netdev user,...` and no other network
  backend, else the wrapper refuses it (exit 1, a message on stderr);
- gets `restrict=on` (no route out of QEMU's user network) and the
  egress `guestfwd`s on that netdev (egress.guestfwds: the proxy, the
  resolver, the session's credential proxy);
- runs with the proxy up: the wrapper starts `nono proxy` (and an
  AuthShim thread when the upstream needs credentials), writes its port,
  and the fake-address resolver (fakedns.py, a thread) when the config
  names its socket, then runs QEMU as its child, passes
  SIGTERM/SIGINT/SIGHUP on, and stops every proxy it started when QEMU
  exits. It exits with QEMU's status.

SIGUSR1 starts a new proxy from proxy.json, read again, and points the
port file at it (`wmf-sbx allow`). The old proxy keeps the connections it
has and stops with the VM.
"""

import os
import signal
import subprocess
import sys
import threading

from . import egress as egress_mod
from . import template as template_mod
from . import fakedns as fakedns_mod

REAL_VAR = "WMF_SBX_QEMU_REAL"
CONFIG_VAR = "WMF_SBX_EGRESS_CONFIG"
PLAIN_VAR = "WMF_SBX_QEMU_PLAIN"
BALLOON = "virtio-balloon-pci,free-page-reporting=on"
FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# netdev backends that are not the user network; -nic and -net make a
# backend without -netdev.
NET_OPTIONS = ("-netdev", "--netdev", "-nic", "--nic", "-net", "--net")
PR_SET_PDEATHSIG = 1
# Resolved now: preexec_fn runs in the forked child while the shim's
# threads exist, where an import could deadlock on a lock they held.
if sys.platform.startswith("linux"):
    import ctypes
    _prctl = ctypes.CDLL(None, use_errno=True).prctl
else:
    _prctl = None


class WrapperError(Exception):
    """The wrapper refuses a call, or cannot start the proxy."""


def is_boot(args):
    """A probe has no -pidfile and no -qmp; Lima's boot call has both."""
    return any(a.lstrip("-") in ("pidfile", "qmp") and a.startswith("-") for a in args)


def boot_argv(args, guestfwd):
    """`args` with restrict=on and `guestfwd` on its one user netdev."""
    out, users = [], 0
    i = 0
    while i < len(args):
        a = args[i]
        if a in NET_OPTIONS:
            if i + 1 >= len(args):
                raise WrapperError(f"{a} without a value")
            value = args[i + 1]
            if a.lstrip("-") != "netdev" or not value.startswith("user,"):
                raise WrapperError(f"refusing a network other than one user netdev: {a} {value}")
            opts = value.split(",")
            if any(o.startswith(("restrict=", "guestfwd=")) for o in opts):
                raise WrapperError(f"the user netdev already sets restrict or guestfwd: {value}")
            users += 1
            out += [a, f"{value},restrict=on,{guestfwd}"]
            i += 2
            continue
        if a.startswith(tuple(o + "=" for o in NET_OPTIONS)):
            raise WrapperError(f"refusing a network option in one argument: {a}")
        out.append(a)
        i += 1
    if users != 1:
        raise WrapperError(f"want exactly one -netdev user, found {users}")
    return out


# The highest root-bus slot the 9p devices take (q35 has the ICH9 at
# 0x1f; virt has nothing there). They go downward from it; QEMU gives
# Lima's other devices and the balloon the lowest free slots.
NINEP_TOP_SLOT = 0x1e


def _split_opts(value):
    """QEMU's option list: `,` separates, `,,` is a comma."""
    parts, cur, i = [], "", 0
    while i < len(value):
        if value[i] == ",":
            if value[i + 1:i + 2] == ",":
                cur += ","
                i += 2
                continue
            parts.append(cur)
            cur = ""
        else:
            cur += value[i]
        i += 1
    parts.append(cur)
    return parts


def _join_opts(parts):
    return ",".join(p.replace(",", ",,") for p in parts)


def spread_virtfs(args):
    """`args` with each `-virtfs local,...` as an `-fsdev` and a
    virtio-9p-pci at an explicit address: 8 to a root-bus slot (functions
    0-7, multifunction on function 0), from NINEP_TOP_SLOT down. Lima
    gives each mount a slot of its own, and the root bus holds 30 or 31
    devices: a sandbox with 24 repos left no slot for the balloon (RAN).
    The guest finds each mount by its mount_tag, as before."""
    out, n, i = [], 0, 0
    limit = template_mod.MAX_QEMU_MOUNTS
    while i < len(args):
        a = args[i]
        if a.startswith(("-virtfs=", "--virtfs=")):
            raise WrapperError(f"refusing a -virtfs in one argument: {a}")
        if a not in ("-virtfs", "--virtfs"):
            out.append(a)
            i += 1
            continue
        if i + 1 >= len(args):
            raise WrapperError(f"{a} without a value")
        parts = _split_opts(args[i + 1])
        if parts[0] != "local":
            raise WrapperError(f"only a local -virtfs is moved: {args[i + 1]}")
        tag, fs = None, []
        for p in parts[1:]:
            key = p.partition("=")[0]
            if key == "mount_tag":
                tag = p.partition("=")[2]
            elif key != "id":
                fs.append(p)
        if not tag:
            raise WrapperError(f"-virtfs with no mount_tag: {args[i + 1]}")
        if n >= limit:
            raise WrapperError(f"more than {limit} -virtfs mounts")
        fsdev = f"wmf-sbx-fs{n}"
        slot, function = NINEP_TOP_SLOT - n // 8, n % 8
        device = ["virtio-9p-pci", f"fsdev={fsdev}", f"mount_tag={tag}",
                  f"addr=0x{slot:x}.0x{function:x}"]
        if function == 0:
            device.append("multifunction=on")
        out += ["-fsdev", _join_opts(["local", f"id={fsdev}"] + fs),
                "-device", _join_opts(device)]
        n += 1
        i += 2
    return out


def with_balloon(args):
    """`args` with free-page reporting: on Lima's own balloon device if
    it has one (QEMU allows one), else on a new one."""
    out = list(args)
    for i, a in enumerate(out[:-1]):
        if a in ("-device", "--device") and out[i + 1].split(",")[0].startswith("virtio-balloon"):
            if "free-page-reporting=" not in out[i + 1]:
                out[i + 1] += ",free-page-reporting=on"
            return out
    return out + ["-device", BALLOON]


def _die_with_parent():
    """In the proxy's and QEMU's child, before exec: on Linux, SIGTERM
    when the wrapper dies, even by SIGKILL, so neither outlives it (Lima's
    host agent waits on the wrapper; a QEMU left behind would run with no
    proxy and no owner). macOS has no equivalent."""
    if _prctl:
        _prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


class Egress:
    """The proxies one VM uses: started from proxy.json, all stopped
    together."""

    def __init__(self, config_path, env, popen=subprocess.Popen, log=None):
        self.config_path = config_path
        self.env = env
        self.popen = popen
        self.log = log or (lambda m: print(f"wmf-sbx-qemu: {m}", file=sys.stderr))
        self.procs = []
        self.shims = []
        self.lock = threading.Lock()

    def start(self):
        """Start a proxy from the config as it is now; point the port file
        at it. Returns its port."""
        with self.lock:
            config = egress_mod.read_config(self.config_path)
            argv = list(config["argv"])
            up = config.get("upstream")
            if up and (up.get("user") or up.get("password")):
                shim = egress_mod.AuthShim(up["host"], up["port"], up["user"], up["password"])
                self.shims.append(shim)
                argv += ["--upstream-proxy", f"127.0.0.1:{shim.start()}"]
            elif up:
                argv += ["--upstream-proxy", f"{up['host']}:{up['port']}"]
            port = egress_mod.free_port()
            argv += ["--port", str(port)]
            # stdout has nono's banner (egress.proxy_argv).
            proc = self.popen(argv, env=egress_mod.proxy_env(self.env, config),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              preexec_fn=_die_with_parent)
            self.procs.append(proc)
            egress_mod.wait_listening(port, proc)
            egress_mod.write_port(config["portFile"], port)
            return port

    def replace(self):
        try:
            self.log(f"new proxy on port {self.start()}")
        except (OSError, ValueError, KeyError, egress_mod.EgressError) as e:
            self.log(f"could not start a new proxy, keeping the old one: {e}")

    def start_replacer(self):
        """Run replace() on a thread that lives as long as the wrapper, at
        each request(). Not a thread per request: PR_SET_PDEATHSIG fires
        when the thread that forked the proxy exits, so a short-lived one
        would take its new proxy with it (seen in measurement 9)."""
        self.wanted = threading.Event()

        def loop():
            while True:
                self.wanted.wait()
                self.wanted.clear()
                self.replace()
        threading.Thread(target=loop, daemon=True).start()

    def request(self):
        self.wanted.set()

    def stop(self):
        with self.lock:
            for proc in self.procs:
                if proc.poll() is None:
                    proc.terminate()
            for proc in self.procs:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for shim in self.shims:
                shim.close()


def exit_status(returncode):
    return 128 - returncode if returncode < 0 else returncode


def run(args, env=None, popen=subprocess.Popen, execv=os.execv, self_path=None):
    env = os.environ if env is None else env
    real = env.get(REAL_VAR)
    if not real:
        raise WrapperError(f"{REAL_VAR} is not set")
    me = os.path.realpath(self_path or sys.argv[0])
    if os.path.realpath(real) == me:
        raise WrapperError(f"{REAL_VAR} is this wrapper, not QEMU")
    if not is_boot(args):
        execv(real, [real] + list(args))
        return 0
    config_path = env.get(CONFIG_VAR)
    if not config_path:
        if env.get(PLAIN_VAR) != "1":
            raise WrapperError(f"{CONFIG_VAR} is not set: no egress proxy for this VM "
                               f"(and {PLAIN_VAR} is not 1)")
        execv(real, [real] + with_balloon(spread_virtfs(args)))
        return 0
    config = egress_mod.read_config(config_path)
    argv = [real] + with_balloon(spread_virtfs(boot_argv(list(args), egress_mod.guestfwds(config))))

    egress = Egress(config_path, dict(env), popen=popen)
    resolver = None
    qemu = None
    try:
        if config.get("dnsSocket"):
            resolver = fakedns_mod.Resolver(fakedns_mod.Mapping(config["namesFile"]))
            resolver.start(config["dnsSocket"])
        egress.start()
        # The SIGUSR1 handler before the pid file: whoever reads the pid
        # may signal at once, and SIGUSR1's default action ends the
        # wrapper, and the VM with it (a test saw it, under load).
        egress.start_replacer()
        signal.signal(signal.SIGUSR1, lambda _s, _f: egress.request())
        egress_mod.write_pid(config["pidFile"], os.getpid())
        qemu = popen(argv, preexec_fn=_die_with_parent)
        for sig in FORWARDED:
            signal.signal(sig, lambda s, _f: qemu.send_signal(s))
        return exit_status(qemu.wait())
    finally:
        signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        if qemu is not None and qemu.poll() is None:
            qemu.kill()
            qemu.wait()
        egress.stop()
        if resolver is not None:
            resolver.close()
        egress_mod.remove_pid(config["pidFile"], os.getpid())


def main(argv=None):
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except (WrapperError, egress_mod.EgressError, OSError, ValueError, KeyError) as e:
        print(f"wmf-sbx-qemu: {e}", file=sys.stderr)
        return 1
