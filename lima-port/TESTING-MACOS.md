# Testing wmf-sbx on macOS

wmf-sbx makes disposable MediaWiki development sandboxes: Lima VMs in which
Claude Code runs as an unprivileged user. It has been tested on Linux only.
This page asks a person with a Mac to check that it works there, and to send
the results back. It takes an afternoon, mostly waiting: about 2 hours of
machine time and 30 minutes of yours.

Nothing here changes your Mac outside these places: Homebrew packages,
`~/wmf-claude`, `~/wmf-sbx-src` (the repos the sandbox works on),
`~/.lima/wmf-sbx-*`, `~/.cache/wmf-sbx`, `~/.config/wmf-sbx`,
`~/.local/state/wmf-sbx`, and `~/wmf-sbx-macos` for the results. Step 9
removes everything wmf-sbx made.

## What you need

- A Mac with macOS 13 or later, and 40 GB of free disk.
  - Apple Silicon (M1 to M4): everything except the browser tests, which
    cannot run there (there is no Chrome for Testing for Linux on arm64).
  - Intel: everything.
- [Homebrew](https://brew.sh/).
- A Claude subscription (Pro or Max) to sign in with. You can use an API
  key instead (`export ANTHROPIC_API_KEY=...`); then skip step 4.
- A Gerrit account is not needed: the sandbox clones anonymously.

## Recording the results

Run every command in this page from one Terminal window, in the results
directory, and keep its output:

```bash
mkdir -p ~/wmf-sbx-macos && cd ~/wmf-sbx-macos
script -a session.log        # records everything you see from here on; `exit` stops it
```

If a step fails, do not fix it: note the step number, keep going where you
can, and say so in `RESULTS.md` (step 10). A failure on macOS is the
result we are looking for.

## 1. Install

```bash
brew install lima jq git python@3 pyyaml
git clone --recurse-submodules -b lima-port https://github.com/cscott/wmf-claude ~/wmf-claude
export PATH="$HOME/wmf-claude/sbx/bin:$PATH"
python3 -c 'import yaml' && echo pyyaml-ok
sw_vers; uname -m; sysctl -n machdep.cpu.brand_string; limactl --version
```

If `import yaml` fails, `python3 -m pip install --user pyyaml` (add
`--break-system-packages` if pip refuses).

Tell wmf-sbx where to clone the repos on the Mac (if you already have a
`~/.config/wmf-sbx/repos.yaml`, keep yours and skip this):

```bash
mkdir -p ~/.config/wmf-sbx
cat > ~/.config/wmf-sbx/repos.yaml <<'YAML'
rules:
  - match: "gerrit:mediawiki/core"
    path: "~/wmf-sbx-src/core"
  - match: "gerrit:mediawiki/extensions/{name}"
    path: "~/wmf-sbx-src/extensions/{name}"
  - match: "gerrit:mediawiki/skins/{name}"
    path: "~/wmf-sbx-src/skins/{name}"
  - match: "gerrit:**/{name}"
    path: "~/wmf-sbx-src/{name}"
YAML
```

## 2. Unit tests (5 minutes)

```bash
cd ~/wmf-claude/sbx && python3 -m unittest discover -s tests 2>&1 | tail -30; cd -
```

Want: `OK` at the end. Failures here are macOS differences in paths,
commands or Python; they are useful on their own.

## 3. Make a sandbox (30 to 60 minutes the first time)

```bash
time wmf-sbx create --name mac1 gerrit:mediawiki/extensions/Cite
wmf-sbx status mac1
wmf-sbx ls
```

The first `create` builds the golden image (a VM that installs PHP, Node,
Claude Code and more, then is saved), then makes the sandbox: clones of Cite
and MediaWiki core, a working wiki. Want: every `ok` line in `status`, and
`create` ending with the commands to resume.

## 4. Sign in (5 minutes)

```bash
wmf-sbx login
```

It starts a small VM from the golden image that step 3 built. It prints a
link: open it, sign in to Claude, copy the code it shows, paste it
into the Terminal. Want: "Saved the token in ...". Note anything odd on the
screen (garbled text, a link you could not open or copy).

## 5. Use it (15 minutes)

```bash
wmf-sbx resume mac1
```

Claude Code starts inside the sandbox. Want: no login question, no theme
question. Ask it, one after the other:

1. "Run Cite's PHP unit tests and tell me the summary line."
2. "Start the wiki and fetch its main page with curl; tell me the HTTP status."
3. "Make a branch named mac-test, add a file MAC-TEST.md with today's date,
   and commit it."

Then `/exit`. On the Mac, fetch the commit:

```bash
git -C ~/wmf-sbx-src/extensions/Cite fetch mac1
git -C ~/wmf-sbx-src/extensions/Cite log -1 --stat mac1/mac-test
```

Want: the commit with MAC-TEST.md.

## 6. Browser tests (Intel Macs only; 15 minutes)

```bash
WMF_SBX_CHECK_NAME=mac1 ~/wmf-claude/lima-port/checks/real-hardware.sh browser
WMF_SBX_CHECK_NAME=mac1 ~/wmf-claude/lima-port/checks/real-hardware.sh qunit
```

On Apple Silicon, run only the first line and expect it to fail (record
the message).

## 7. The credential stays on the Mac (20 minutes)

```bash
cd ~/wmf-claude
lima-port/checks/credential-l3.sh create
lima-port/checks/credential-l3.sh source
lima-port/checks/credential-l3.sh concurrent
lima-port/checks/credential-l3.sh scan
lima-port/checks/credential-l3.sh rm
cd ~/wmf-sbx-macos
```

Want: `source` names the token file; `concurrent` shows `rc 0` and
`l3-ok-...` for both; `scan` ends with `== scan: PASS`. The scan searches
both VMs for your Claude token without ever printing it.

If you use Claude Code on this Mac, also run `source` once with the token
file moved away, to check the keychain is read:

```bash
mv ~/.config/wmf-sbx/claude-oauth-token{,.away}
~/wmf-claude/lima-port/checks/credential-l3.sh source
mv ~/.config/wmf-sbx/claude-oauth-token{.away,}
```

## 8. Optional: QEMU instead of Apple's virtualisation (30 minutes)

```bash
brew install qemu
time wmf-sbx create --vm-type qemu --name mac-qemu gerrit:mediawiki/extensions/Cite
wmf-sbx status mac-qemu
wmf-sbx rm --force mac-qemu
```

## 9. Clean up

First check the recording for secrets, while the token is still there:

```bash
python3 ~/wmf-claude/lima-port/checks/credential-scan.py results ~/wmf-sbx-macos/*
```

Want: `0 hits` for each credential found. (With an API key, keep
`ANTHROPIC_API_KEY` set for this.) Then:

```bash
wmf-sbx rm --force mac1
wmf-sbx rm --prune
wmf-sbx image prune
limactl list                               # want: no wmf-sbx-* left
rm ~/.config/wmf-sbx/claude-oauth-token    # unless you keep using wmf-sbx
```

To remove everything: also `rm -rf ~/.cache/wmf-sbx ~/.local/state/wmf-sbx
~/.config/wmf-sbx ~/wmf-claude ~/wmf-sbx-src`, and `brew uninstall lima` if you do not
use it otherwise. The token can be revoked at claude.ai (Settings, Claude
Code) if you want.

## 10. Send the results

Stop the recording (`exit`), then write `~/wmf-sbx-macos/RESULTS.md`: the
Mac model and chip, the macOS version, and for each step 1 to 9 "pass",
"fail" or "skipped", with a line about anything that surprised you.

Then pack the directory (step 9 checked it for secrets):

```bash
tar czf ~/wmf-sbx-macos.tgz -C ~ wmf-sbx-macos
```

Send `~/wmf-sbx-macos.tgz` to the person who asked you to test.

## For the person who gets the results

The results go to the Claude Code session that works on wmf-sbx (a cloud
session, or the track C sandbox). Unpack the tarball and attach
`RESULTS.md` and `session.log` to the conversation; in a wmf-sbx sandbox,
copy them in with `wmf-sbx cp`, or put them in the repo under
`lima-port/results/` and let Claude fetch them. `session.log` is a terminal
recording, with escape sequences; Claude reads it as is.
