---
name: feedback-stdin-devnull-for-noninteractive-subprocess
description: non-interactive subprocess calls in sbx/bin/ must set stdin=subprocess.DEVNULL so an unexpected prompt fails fast instead of hanging invisibly
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1c38e171-1e24-4c67-b927-f7e6c71d49c8
  modified: 2026-09-08T22:05:33.247Z
---

Any subprocess call that isn't supposed to be interactive (e.g. internal
`sbx`/`wmf-sbx` invocations in `sbx/bin/*.py` using `capture_output=True`)
should pass `stdin=subprocess.DEVNULL`.

**Why:** cananian hit a real invisible hang — `wmf-sbx-create` called
internal `sbx ls -q` with `capture_output=True` and no `stdin=` override.
`sbx` itself unexpectedly showed a version-mismatch restart prompt; the
prompt text went into the captured, unread buffer while stdin still
pointed at the real terminal, so the process sat waiting for a keypress
the user was never shown was expected. cananian's exact words: "you
should probably ensure that commands without expected keyboard input have
their input pipe set to /dev/null so that they fail rather than just hang
invisibly." Fixed 2026-09-08 across all 8 non-attach call sites in
`wmf_sbx_create.py`; see [[project_wmf_claude_sbx_redesign]] and
`sbx/NOTES.md` §47.7-47.8.

**How to apply:** When adding any new non-interactive subprocess call
anywhere in `sbx/bin/` (or similar tooling), default to
`stdin=subprocess.DEVNULL` unless the call is a genuine interactive
attach (e.g. the real `sbx create`/`sbx run` attach, or `wmf-sbx-rm`'s
confirmation prompt) — those must keep inheriting stdio and must NOT get
this treatment.
