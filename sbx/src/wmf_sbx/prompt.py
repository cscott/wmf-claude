"""Questions to the engineer on the terminal: pick one from a list, or y/N.

Every wmf-sbx command that would otherwise stop with "name one" or
"-f to ... anyway" asks with this instead. Questions and lists go to
stderr: stdout stays for the command's output.

Prompt checks for a terminal itself (stdin and stderr both TTYs), so that
no caller can forget to: with none, it asks nothing and the answer is
"no" (pick() is None, confirm() is False), and the caller stops as it did
before there were questions. A script or a pipe never hangs on a question.
An empty answer, the end of input (Ctrl-D) and three answers that are not
in the list are "no" too.
"""

import sys

TRIES = 3


class Prompt:
    """`ask` is input()-like (it takes the question, returns the line, and
    raises EOFError at the end of input); the default reads stdin and
    writes the question to stderr. `isatty` says whether to ask at all;
    the default is `interactive` with the default `ask`, and yes with a
    caller's `ask`, which gives the answers itself and so cannot hang."""

    def __init__(self, ask=None, out=None, isatty=None):
        self._ask = ask or self._read
        self._out = out
        self._isatty = isatty or (lambda: True if ask else interactive())

    @property
    def available(self):
        """Whether this prompt asks (a terminal), or answers no at once."""
        return bool(self._isatty())

    @property
    def out(self):
        # Looked up at each write, so that redirect_stderr() works.
        return self._out or sys.stderr

    def _read(self, question):
        self.out.write(question)
        self.out.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line

    def ask(self, question):
        """The answer, stripped; "" at the end of input or with no terminal."""
        if not self.available:
            return ""
        try:
            return self._ask(question).strip()
        except EOFError:
            self.out.write("\n")
            return ""

    def pick(self, question, labels):
        """The index of the label chosen, by number or by the label
        itself; None for no choice, or with no terminal."""
        if not self.available:
            return None
        print(question, file=self.out)
        for i, label in enumerate(labels, 1):
            print(f"  {i}) {label}", file=self.out)
        for _ in range(TRIES):
            answer = self.ask(f"Which one? [1-{len(labels)}, Enter for none] ")
            if not answer:
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(labels):
                return int(answer) - 1
            if answer in labels:
                return labels.index(answer)
        return None

    def confirm(self, question):
        """True for y or yes; everything else is no, and no terminal."""
        return self.ask(f"{question} [y/N] ").lower() in ("y", "yes")


def interactive(stdin=None, stderr=None):
    """Whether stdin and stderr are both TTYs."""
    try:
        return (stdin or sys.stdin).isatty() and (stderr or sys.stderr).isatty()
    except (AttributeError, ValueError):  # a closed or replaced stream
        return False
