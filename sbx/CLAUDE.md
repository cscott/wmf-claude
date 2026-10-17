# sbx/: wmf-sbx

Guidance for Claude Code working on wmf-sbx, the Lima sandbox manager.
`README.md` is how to use it, `SECURITY.md` what it guards, `NOTES.md` the
findings and decisions (with what was RAN), `FROM-UPSTREAM.md` the open
items.

- **Test wmf-sbx yourself.** In a sandbox made with `wmf-sbx create
  --nested` you have KVM, QEMU and Lima: run `create`, `resume`, `exec`
  and the rest here, and record the result as RAN. Do not ask the engineer
  to run a check that works here. Only host checks (the host egress proxy,
  the host's clipboard, macOS) are the engineer's: write the commands, and
  record their results as RAN by the engineer.
- **`wmf-sbx` is not on PATH** in a sandbox: the image installs nothing
  from this checkout. Run it as `sbx/bin/wmf-sbx` from the repo root (the
  `git sbx-pull` and `git safe-reset` helpers are in `sbx/bin/` too). Each
  Bash call is a new shell, so `export PATH` does not persist: use the
  path.
- **Tests**, from `sbx/`: `env -u SANDBOX_NAME python3 -m unittest discover
  -s tests`. One file: add `-p test_wmf_sbx_<name>.py`. Do not use
  `python3 -m unittest tests.<name>`: the `isolation` import needs
  discover. The shared files (`bin/`, `hooks/`, `profiles/`) have their
  own suites at the repo root (`CLAUDE.md`, "Tests").
- **An inner `create` may build an inner golden image** first (about
  10 minutes). Run it in the background with a log in `$TMPDIR`, and
  check the log; `sbx/bin/wmf-sbx stop NAME` when you are done.
- `WMF_SBX_*` variables are host configuration; `WMF_CLAUDE_*` are what
  the sandboxed session sees (nono passes only the profile's
  `allow_vars`).
