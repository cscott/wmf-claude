#!/usr/bin/env python3
"""Track L, L3: count the host's Claude credentials in a byte stream or
in files, without printing them or sending them anywhere.

  credential-scan.py LABEL [FILE ...]    # no FILE: read stdin

It reads the credentials itself, on the host: $ANTHROPIC_API_KEY,
$CLAUDE_CODE_OAUTH_TOKEN, ~/.config/wmf-sbx/claude-oauth-token, and the
access and refresh tokens of the host's Claude Code login
(~/.claude/.credentials.json, or the macOS keychain). It prints one line
per credential it found on the host: "LABEL: <which>: N hits". Want: 0.
It also prints "LABEL: <which>: absent" for a source the host does not
have, and the number of bytes scanned.
"""

import json
import os
import subprocess
import sys

MIN_LEN = 20
CHUNK = 1 << 20


def credentials():
    """[(name, bytes)] of the host's credentials."""
    found = []

    def add(name, value):
        if isinstance(value, str) and len(value.strip()) >= MIN_LEN:
            found.append((name, value.strip().encode()))
        else:
            found.append((name, None))

    add("$ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY"))
    add("$CLAUDE_CODE_OAUTH_TOKEN", os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(config, "wmf-sbx", "claude-oauth-token")) as f:
            add("token file", f.read())
    except OSError:
        add("token file", None)
    doc = None
    if sys.platform == "darwin":
        res = subprocess.run(["security", "find-generic-password", "-s",
                              "Claude Code-credentials", "-w"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if res.returncode == 0:
            doc = json.loads(res.stdout)
    else:
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        try:
            with open(os.path.join(base, ".credentials.json")) as f:
                doc = json.load(f)
        except OSError:
            pass
    oauth = (doc or {}).get("claudeAiOauth") or {}
    add("login access token", oauth.get("accessToken"))
    add("login refresh token", oauth.get("refreshToken"))
    return found


def scan(streams, secrets):
    longest = max(len(s) for _n, s in secrets)
    hits = {n: 0 for n, _s in secrets}
    total = 0
    for stream in streams:
        tail = b""
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            buf = tail + chunk
            for name, s in secrets:
                # count matches that end in the new chunk only
                start = 0
                while True:
                    i = buf.find(s, start)
                    if i < 0:
                        break
                    if i + len(s) > len(tail):
                        hits[name] += 1
                    start = i + 1
            tail = buf[-(longest - 1):] if longest > 1 else b""
    return hits, total


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    label, files = sys.argv[1], sys.argv[2:]
    found = credentials()
    secrets = [(n, s) for n, s in found if s]
    for n, s in found:
        if not s:
            print(f"{label}: {n}: absent")
    if not secrets:
        print(f"{label}: no credential on this host to look for")
        return 1
    streams = [open(f, "rb") for f in files] if files else [sys.stdin.buffer]
    hits, total = scan(streams, secrets)
    for n, _s in secrets:
        print(f"{label}: {n}: {hits[n]} hits")
    print(f"{label}: {total} bytes scanned")
    return 1 if any(hits.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
