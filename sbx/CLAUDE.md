# sbx/: wmf-sbx

Guidance for Claude Code working on wmf-sbx, the Lima sandbox manager.
`README.md` is how to use it, `SECURITY.md` what it guards, `DESIGN.md`
the reasons for the design, `HISTORY.md` the
paths that were abandoned, and `checks/` the manual checks that the unit
tests cannot do.

- **Test wmf-sbx yourself.** In a sandbox made with `wmf-sbx create
  --nested` you have KVM, QEMU and Lima: run `create`, `resume`, `exec`
  and the rest here, and say what you ran and what it showed. Do not ask
  the engineer to run a check that works here. Only host checks (the host
  egress proxy, the host's clipboard, macOS; see `checks/`) are the
  engineer's: write the commands for them, and say which results came
  from the engineer.
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
- **An inner sandbox, with no terminal.** The Bash tool has none, so
  each question gets "no":
  - `create --clone-missing --no-start REPO`: clones without asking,
    and does not start a session. The first `create` builds an inner
    golden image (10-20 minutes). Run it in the background, with its log
    in a directory under `~` (`$TMPDIR` is a tmpfs, lost if the VM
    restarts), and check the log.
  - The image key has the wmf-claude HEAD in it, so each commit makes
    the next `create` build again. When the change is not in the image,
    `--image KEY` (`image ls`) uses the cached one, or set
    `WMF_SBX_GIT_HASH_OVERRIDE` (`latest`: the newest cached image) in
    the same command (README, "The golden image").
  - Give `--memory` and `--disk` a unit (`4GiB`): a bare number is
    bytes, and `create` refuses less than 1GiB.
  - Put `sbx/bin` on `PATH` in the same command (`PATH="$PWD/sbx/bin:$PATH"`)
    for anything that runs git on the sandbox remotes (`git sbx-pull`,
    `rm`): git needs `git-remote-wmfsbx`.
  - There is no `/login`: an inner session uses the outer session's
    credential, from `/etc/wmf-sbx/nested`.
  - A task: `resume NAME -- --permission-mode auto -p "..."` (add
    `--continue` to keep the conversation). Without a permission mode the
    agent asks for approval and stops.
  - `rm` refuses with no terminal: `rm --force`, which also skips the
    check for unfetched commits.
  - Your git identity is in this session's `GIT_AUTHOR_*` and
    `GIT_COMMITTER_*` environment, not in `git config`; `create` passes
    it on to the inner agent (session.git_identity).
  - `sbx/bin/wmf-sbx stop NAME` or `rm --force NAME` when you are done.
- `WMF_SBX_*` variables are host configuration; `WMF_CLAUDE_*` are what
  the sandboxed session sees (nono passes only the profile's
  `allow_vars`).
