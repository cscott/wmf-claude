"""ANSI color helpers for wmf-sbx's own terminal output.

A 24-bit palette from the "Tokyo Night" theme. Color only on a
terminal, and never with NO_COLOR set.
"""

import os
import sys

_DIM = "\x1b[38;2;128;128;128m"
_BLUE = "\x1b[38;2;122;162;247m"
_RED = "\x1b[38;2;247;118;142m"
# Tokyo Night's orange, for warnings: they stand out from the plan lines
# above them, and are not the red of a failure.
_ORANGE = "\x1b[38;2;255;158;100m"
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
    """Routine/log-line styling: gray, for section labels and progress
    lines."""
    return f"{_DIM}{text}{_RESET}" if _enabled(stream) else text


def highlight(text, *, stream=None):
    """The "here's the command to run" styling: blue."""
    return f"{_BLUE}{text}{_RESET}" if _enabled(stream) else text


def warning(text, *, stream=None):
    """Warning styling: a line that does not stop this invocation but
    needs the engineer's attention (a fallback, a degraded setup)."""
    return f"{_ORANGE}{text}{_RESET}" if _enabled(stream) else text


def error(text, *, stream=None):
    """Failure styling, used sparingly -- only for lines that mean this
    invocation just failed."""
    return f"{_RED}{text}{_RESET}" if _enabled(stream) else text

