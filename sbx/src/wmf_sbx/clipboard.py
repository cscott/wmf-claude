#!/usr/bin/env python3
"""Copy from Claude Code in a sandbox to the host's clipboard
(lima-port/HANDOFF-CLIPBOARD.md).

Claude Code copies a mouse selection with `wl-copy` when it finds one on
PATH and `WAYLAND_DISPLAY` is set, and sends OSC 52 too, which GNOME
Terminal drops. In a sandbox there is no display, so `create` gives the
agent a `wl-copy` of ours (GUEST_SHIM, in ~/.local/bin) and a dummy
`WAYLAND_DISPLAY` (session.clipboard_env). The shim sends the text to
`resume`'s listener on the host, which gives it to the host's own tool
(host_command). The listener only takes text: it sends nothing back, so
nothing in the VM can read the host's clipboard.

For now, only a sandbox behind the egress proxy (the guestfwd at
egress.EGRESS_ADDR:CLIP_PORT, to egress.clip_socket), and only when
settings.yaml has no `session: {native_copy: false}`.

What the shim sends: a line, `clipboard` or `primary` (Claude Code calls
`wl-copy` for each, in turn), then the text until EOF, at most MAX_BYTES.
The listener logs a line per write (the selection, the size, the
result), never the text.
"""

import datetime
import os
import shutil
import socketserver
import subprocess
import sys
import threading

# A bigger write is refused, not cut: a cut-off command pasted into a
# shell is worse than none.
MAX_BYTES = 1 << 20
SELECTIONS = ("clipboard", "primary")
# How long a connection may take to send its text, and the host's tool
# to take it. Claude Code kills the shim after 2 s.
TIMEOUT = 5
# What create puts in the agent's settings `env` (session.clipboard_env):
# Claude Code looks for `wl-copy` only when WAYLAND_DISPLAY is set (any
# value; no compositor is asked), and the shim connects to ADDRESS_VAR.
# WMF_CLAUDE_*: the sandbox sees it.
WAYLAND_DISPLAY = "wmf-sbx"
ADDRESS_VAR = "WMF_CLAUDE_CLIPBOARD"
SHIM_PATH = "~/.local/bin/wl-copy"

GUEST_SHIM = f"""\
#!/bin/bash
# wl-copy for a wmf-sbx sandbox (sbx/src/wmf_sbx/clipboard.py): sends the
# text on stdin to the host's clipboard, through the session's listener at
# ${ADDRESS_VAR}. It only writes: nothing comes back. Every option but
# --primary is ignored.
sel=clipboard
for a in "$@"; do
  case $a in -p|--primary) sel=primary ;; esac
done
addr=${{{ADDRESS_VAR}:-}}
if [ -z "$addr" ]; then
  echo "wl-copy: {ADDRESS_VAR} is not set: no clipboard in this session" >&2
  exit 1
fi
exec 3>"/dev/tcp/${{addr%:*}}/${{addr##*:}}" || exit 1
{{ printf '%s\\n' "$sel"; cat; }} >&3
"""


def host_command(selection, env, platform=sys.platform, which=shutil.which):
    """argv that puts stdin on the host's `selection` (SELECTIONS), or None
    when the host has no tool for it. Wayland: wl-copy; X11: xclip; macOS:
    pbcopy (no PRIMARY there)."""
    primary = selection == "primary"
    if platform == "darwin":
        return None if primary else ["pbcopy"]
    if env.get("WAYLAND_DISPLAY") and which("wl-copy"):
        return (["wl-copy", "--type", "text/plain;charset=utf-8"]
                + (["--primary"] if primary else []))
    if env.get("DISPLAY") and which("xclip"):
        return ["xclip", "-selection", selection]
    return None


class _Handler(socketserver.StreamRequestHandler):
    timeout = TIMEOUT

    def handle(self):
        try:
            selection = self.rfile.readline(16).decode("ascii", "replace").strip()
            text = self.rfile.read(MAX_BYTES + 1)
        except OSError as e:
            self.server.listener.log(f"refused: {e}")
            return
        self.server.listener.copy(selection, text)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Listener:
    """One session's clipboard listener. start() binds the Unix socket
    `path` (0600) and serves in a daemon thread; stop() closes it."""

    def __init__(self, path, env, log_path=None, run=subprocess.run,
                 platform=sys.platform, which=shutil.which):
        self.path = path
        self.env = env
        self.log_path = log_path
        self.run = run
        self.platform = platform
        self.which = which
        self._server = None
        self._ino = None
        self._lock = threading.Lock()

    def available(self):
        """Whether the host has a tool for the CLIPBOARD selection."""
        return host_command("clipboard", self.env, self.platform, self.which) is not None

    def log(self, line):
        if not self.log_path:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(f"{stamp} {line}\n")
            except OSError:
                pass

    def copy(self, selection, text):
        """Give `text` (bytes) to the host's tool for `selection`; log it."""
        if selection not in SELECTIONS:
            self.log(f"refused: no selection ({selection[:16]!r})")
            return
        if len(text) > MAX_BYTES:
            self.log(f"{selection} refused: more than {MAX_BYTES} bytes")
            return
        if not text:
            self.log(f"{selection} refused: empty")
            return
        argv = host_command(selection, self.env, self.platform, self.which)
        if argv is None:
            self.log(f"{selection} {len(text)}B not copied: no host tool for it")
            return
        try:
            # wl-copy and xclip fork a process that holds the selection
            # after the session ends; its own session keeps it out of the
            # terminal's signals.
            res = self.run(argv, input=text, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=dict(self.env), timeout=TIMEOUT,
                           start_new_session=True, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log(f"{selection} {len(text)}B failed: {argv[0]}: {e}")
            return
        result = "copied" if res.returncode == 0 else f"failed: {argv[0]} exit {res.returncode}"
        self.log(f"{selection} {len(text)}B {result}")

    def start(self):
        """Serve on the Unix socket (one left by an earlier session is
        replaced)."""
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        old = os.umask(0o177)
        try:
            self._server = _Server(self.path, _Handler)
        finally:
            os.umask(old)
        self._ino = os.stat(self.path).st_ino
        self._server.listener = self
        threading.Thread(target=self._server.serve_forever, args=(0.1,), daemon=True,
                         name="wmf-sbx-clipboard").start()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._ino is not None:
            # Not a socket that a later session bound in its place.
            try:
                if os.stat(self.path).st_ino == self._ino:
                    os.unlink(self.path)
            except OSError:
                pass
            self._ino = None
