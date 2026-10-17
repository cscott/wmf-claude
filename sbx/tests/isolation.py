"""Keeps the machine's own state out of the tests: the engineer's
settings, their credentials, their sandboxes and Lima instances, and
the nested marker of the sandbox the tests may run in. A test that
fails must not print a real credential, and a test must not act on a
real sandbox.

Every test module calls `isolate()` from its setUpModule
(test_wmf_sbx_isolation checks it). For the module's run:

- settings.yaml and repos.yaml are paths that do not exist (every
  command reads them for its option defaults, cli.parse_args);
- `image.NESTED_MARKER` is a path that does not exist (a nested sandbox
  keeps its session's phantom there); a test that wants a marker
  patches its own;
- HOME and the XDG directories are a new empty directory, so
  ~/.claude/.credentials.json, ~/.config, ~/.local/state/wmf-sbx,
  ~/.cache/wmf-sbx and ~/.lima are the test's, also for subprocesses;
- the credential, wmf-sbx host and Phabricator variables are unset;
- `claude` and `security` (the macOS keychain) on PATH are stubs that
  fail: create asks `claude mcp get phabricator`, which starts the MCP
  server under the real login.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import wmf_sbx.image as image_mod  # noqa: E402
import wmf_sbx.resolve as resolve_mod  # noqa: E402

# Unset for the tests: credentials, and what the engineer configures.
UNSET_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
              "ANTHROPIC_BASE_URL", "WMF_CLAUDE_PHAB_USER", "LIMA_HOME",
              "GIT_CONFIG_GLOBAL")
UNSET_PREFIXES = ("WMF_SBX_",)
STUBS = ("claude", "security")
STUB_SCRIPT = "#!/bin/sh\necho \"$0: a stub; the tests do not run it\" >&2\nexit 1\n"


def _start(patch):
    patch.start()
    unittest.addModuleCleanup(patch.stop)


def isolate():
    _start(mock.patch.object(resolve_mod, "DEFAULT_CONFIG",
                             "/nonexistent/wmf-sbx/DEFAULT_CONFIG.yaml"))
    _start(mock.patch.object(image_mod, "NESTED_MARKER", "/nonexistent/wmf-sbx/nested"))

    home = tempfile.mkdtemp(prefix="wmf-sbx-test-home.")
    unittest.addModuleCleanup(shutil.rmtree, home, ignore_errors=True)
    stubs = os.path.join(home, ".stub-bin")
    os.mkdir(stubs)
    for stub in STUBS:
        path = os.path.join(stubs, stub)
        with open(path, "w", encoding="utf-8") as f:
            f.write(STUB_SCRIPT)
        os.chmod(path, 0o755)

    env = {k: v for k, v in os.environ.items()
           if k not in UNSET_VARS and not k.startswith(UNSET_PREFIXES)}
    env.update(HOME=home,
               XDG_CONFIG_HOME=os.path.join(home, ".config"),
               XDG_STATE_HOME=os.path.join(home, ".local", "state"),
               XDG_CACHE_HOME=os.path.join(home, ".cache"),
               PATH=stubs + os.pathsep + os.environ.get("PATH", os.defpath))
    _start(mock.patch.dict(os.environ, env, clear=True))
