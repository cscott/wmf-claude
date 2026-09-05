"""ANSI color helpers for wmf-sbx-create's own terminal output.

Matches the 24-bit palette the real `sbx create` uses on its own output
(see sbx/NOTES.md's "Make the output of wmf-sbx-create prettier" to-do
and responses29.txt, the captured reference transcript) -- so our own
progress/warning/error lines don't look out of place interleaved with
sbx's colored ones. `run_with_color` goes the other direction: it gives
the real `sbx create` child process a pty when we're colorized
ourselves, so its own output gets a chance to colorize too.
"""

import os
import subprocess
import sys

_DIM = "\x1b[38;2;128;128;128m"
_BLUE = "\x1b[38;2;122;162;247m"
# Not observed in a captured transcript (responses29.txt is an
# all-success run) -- inferred to match the "Tokyo Night" theme family
# the rest of this palette comes from exactly (blue and green both match
# it verbatim). Revisit if a real failing-command transcript shows
# otherwise.
_RED = "\x1b[38;2;247;118;142m"
_RESET = "\x1b[m"


def _enabled(stream=None):
    # `stream=None` resolved to `sys.stderr` here, at call time, rather
    # than bound as a default argument at import time: tests (and
    # anything else using contextlib.redirect_stderr) reassign
    # `sys.stderr` itself, which a def-time-bound default would miss.
    if stream is None:
        stream = sys.stderr
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def dim(text, *, stream=None):
    """Routine/log-line styling -- sbx's own gray for section labels and
    progress lines like "resolving configuration...", "-> pull"."""
    return f"{_DIM}{text}{_RESET}" if _enabled(stream) else text


def highlight(text, *, stream=None):
    """The "here's the command to run" styling -- sbx's own blue for its
    final `sbx run --name NAME` suggestion."""
    return f"{_BLUE}{text}{_RESET}" if _enabled(stream) else text


def error(text, *, stream=None):
    """Failure styling, used sparingly -- only for lines that mean this
    invocation just failed, mirroring sbx's own red-on-failure."""
    return f"{_RED}{text}{_RESET}" if _enabled(stream) else text


def run_with_color(cmd, *, env=None, run=subprocess.run, stream=None):
    """Run cmd, giving it a pty iff `_enabled(stream)` -- the same check
    that gates our own coloring -- so a child that only colorizes its own
    output when its stdout is a real terminal (like `sbx create`) gets
    the chance to do so here too, with its combined stdout/stderr relayed
    back to our real stdout as it runs.

    Best-effort: `pty` is POSIX-only, and forking one can still fail even
    there (no controlling terminal, out of ptys, ...). `run` (the
    test-overridable subprocess runner already threaded through
    create.py's main()) is the fallback for every one of those cases, so
    the command still runs -- it just runs uncolored, same as if this
    function weren't called at all.
    """
    if not _enabled(stream):
        return run(cmd, env=env)
    try:
        import pty
        import termios
        import tty
    except ImportError:
        return run(cmd, env=env)  # pty is POSIX-only
    try:
        pid, master_fd = pty.fork()
    except OSError:
        return run(cmd, env=env)  # no pty available here either
    if pid == pty.CHILD:
        try:
            os.execvpe(cmd[0], list(cmd), os.environ if env is None else env)
        finally:
            os._exit(127)  # exec failed
    try:
        mode = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
    except (termios.error, ValueError, OSError):
        mode = None
    try:
        pty._copy(master_fd)
    except OSError:
        pass
    finally:
        if mode is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, mode)
        os.close(master_fd)
    returncode = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    return subprocess.CompletedProcess(cmd, returncode)
