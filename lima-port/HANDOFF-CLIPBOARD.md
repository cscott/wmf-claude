# HANDOFF: Native copy-and-paste doesn't work in sandboxed Claude Code

## Problem

When text is selected with the mouse in the Claude Code TUI, Claude Code captures
the selection itself and shows a toast like:

> sent NNN characters via OSC 52, if paste fails hold shift for native copy

Paste then fails, because nothing reached the desktop clipboard. Holding
Shift while selecting bypasses Claude Code's mouse capture and works, but it is
awkward. It also loses Claude Code's cleanup of the copied text, such as
stripping margins and handling indentation.

### Environment

- Ubuntu 24.04, kernel 7.0.0, x86_64, GNOME session (Wayland by default; confirm
  with `echo $XDG_SESSION_TYPE` on the host).
- Terminal: GNOME Terminal (`gnome-terminal-server`), which is built on **VTE**.
- `claude` runs **inside a sandbox** (bwrap, container or similar) that
  deliberately does **not** expose the Wayland or X11 socket.

### Root cause

1. Claude Code copies a selection through the first transport that works:
   a platform clipboard tool (`wl-copy`, `xclip`, `xsel`, ...), a tmux buffer,
   or, as a last resort, **OSC 52** (an escape sequence asking the
   terminal to set the clipboard).
2. Inside the sandbox there is no usable clipboard tool. Either the binary is
   missing or there is no display socket for it to talk to. Claude Code
   therefore falls back to OSC 52.
3. **VTE does not implement OSC 52** and silently drops it
   (https://gitlab.gnome.org/GNOME/vte/-/issues/2495). No GNOME Terminal setting
   changes this.

### Rejected alternatives

- **Bind-mounting the Wayland socket into the sandbox.** This is far too broad.
  It grants clipboard *reads*, window creation and other compositor access.
- **Switching to a terminal emulator that supports OSC 52** (kitty, WezTerm,
  foot, Ghostty). This is possible, but out of scope; the goal is to keep
  GNOME Terminal.
- **`CLAUDE_CODE_DISABLE_MOUSE=1` or `/tui default`.** These avoid the problem by
  giving up Claude Code's own selection handling.

## Chosen approach: a write-only clipboard proxy

Put a fake `wl-copy` (and/or `xclip`) on `PATH` inside the sandbox. It forwards
stdin over a **single bind-mounted Unix socket** to a listener on the host. The
listener feeds the bytes to the real `wl-copy`.

```
 sandbox                                   host
 ┌─────────────────────────┐               ┌──────────────────────────────┐
 │ claude ──exec──> shim   │──unix sock───>│ listener ──> real wl-copy    │
 │ (thinks it's wl-copy)   │  (write only) │ (socat / tiny daemon)        │
 └─────────────────────────┘               └──────────────────────────────┘
```

Properties we want:

- **Copy only.** The sandbox can never read the clipboard. Reads fail cleanly.
- **One narrow channel.** A single socket file with mode `0600`. Nothing else
  from `$XDG_RUNTIME_DIR` is exposed.
- **The clipboard owner lives on the host.** `wl-copy` forks a background
  process that serves the selection until another client takes ownership. If
  that process ran inside the sandbox, it could die when the sandbox exits,
  taking the clipboard contents with it. Running it host-side avoids this.

## Step 1: Investigate what Claude Code actually does

Do **not** assume the probing logic. Claude Code's backend selection is
undocumented, and there is no known setting or environment variable for a
custom clipboard command. Measure it.

### 1a. Logging shim

Inside the sandbox, create a directory early on `PATH`
(for example `~/.local/sandbox-bin`) and install this script under every name
Claude Code might try: `wl-copy`, `wl-paste`, `xclip`, `xsel`, and possibly
`pbcopy` and `clip.exe` for completeness.

```sh
#!/bin/sh
# clipshim-log: record how we were invoked, and what we were given.
log=/tmp/clipshim.log
{
  printf '=== %s pid=%s ppid=%s\n' "$(date +%T.%N)" $$ "$PPID"
  printf 'argv0=%s\n' "$0"
  printf 'argv:'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\n'
  env | grep -E '^(WAYLAND_DISPLAY|DISPLAY|XDG_SESSION_TYPE|XDG_RUNTIME_DIR|TMUX|SSH_)' 
  printf 'stdin is tty: '; [ -t 0 ] && echo yes || echo no
} >>"$log"
# Capture stdin (bounded), so we can see exactly what would be copied.
head -c 1048576 >>"$log" 2>/dev/null
printf '\n=== end\n' >>"$log"
exit 0
```

Then start `claude` and make a mouse selection. Record the following:

- [ ] Which binary is invoked, and in what order (is there a probe such as
      `which` or `--version` before the real call?).
- [ ] The exact arguments (for example `xclip -selection clipboard`, or
      `wl-copy` with or without `--type` / `--primary`).
- [ ] Whether data arrives on stdin, or as an argument.
- [ ] Whether the toast changes from "sent via OSC 52" to something else. This
      is the success signal.

### 1b. If the shim is never called

Claude Code is probably deciding *before* exec'ing that no display is
available. Try the following, one at a time, restarting `claude` each time:

1. Set a dummy display inside the sandbox: `WAYLAND_DISPLAY=wayland-0`
   (the socket does not need to exist), and/or `XDG_SESSION_TYPE=wayland`.
2. If that doesn't work, try `DISPLAY=:0` and the `xclip` name instead.
3. Make sure neither `TMUX` nor `SSH_CONNECTION`/`SSH_TTY` is set inside the
   sandbox. Claude Code may deliberately prefer OSC 52 in those cases, because a
   local tool would write to the wrong machine.

To see every exec attempt, including probes of binaries that don't exist, run:

```sh
strace -f -e trace=execve,access,faccessat2,stat,statx -o /tmp/claude.strace claude
grep -E 'wl-copy|wl-paste|xclip|xsel|pbcopy' /tmp/claude.strace
```

(This needs `strace` inside the sandbox and a ptrace-permitting seccomp policy.
If the sandbox blocks ptrace, run `strace` around the sandbox launcher from the host.)

### 1c. Other things to note

- [ ] Does Claude Code check the shim's **exit status**? Try `exit 1` and see
      whether it falls back to OSC 52. The production shim should return
      non-zero if forwarding fails, so the fallback still happens.
- [ ] Does Claude Code wait for the process to exit? Synchronous or
      fire-and-forget behaviour affects how the shim should handle a slow or
      absent host listener. Add a timeout either way.
- [ ] Is the clipboard also **read**? Image paste (Ctrl+V of a screenshot) is a
      likely candidate, via `wl-paste` or `xclip -o`. Decide whether reads stay
      blocked; the current plan is to block them.

Write the findings into the **Findings** section at the bottom of this file
before building the real shim.

## Step 2: Host-side listener

### Minimal version (socat)

Run one `wl-copy` per connection:

```sh
socat UNIX-LISTEN:"$XDG_RUNTIME_DIR/claude-clip.sock",fork,mode=600,unlink-early \
      SYSTEM:'head -c 1048576 | wl-copy --type text/plain\;charset=utf-8'
```

- `--type text/plain;charset=utf-8` stops `wl-copy` from guessing a MIME type
  from the content (it sometimes picks odd ones).
- `head -c` caps the payload at 1 MiB. Adjust as needed.
- Optional: also set PRIMARY so middle-click works, with
  `tee >(wl-copy --primary ...) | wl-copy ...`. This needs `bash -c` rather than `sh`.
- On an X11 session, use `xclip -selection clipboard -in` instead.

### systemd user unit

`~/.config/systemd/user/claude-clip.service`:

```ini
[Unit]
Description=Write-only clipboard proxy for sandboxed Claude Code
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/socat UNIX-LISTEN:%t/claude-clip.sock,fork,mode=600,unlink-early \
  SYSTEM:'head -c 1048576 | wl-copy --type "text/plain;charset=utf-8"'
Restart=on-failure

[Install]
WantedBy=graphical-session.target
```

Enable it with `systemctl --user enable --now claude-clip.service`. (`%t` expands to
`$XDG_RUNTIME_DIR`.) The service inherits `WAYLAND_DISPLAY` from the
user session, provided GNOME has imported it into the systemd user environment.
Check with `systemctl --user show-environment | grep WAYLAND`.

Systemd socket activation (a `.socket` unit with `Accept=yes`) is a cleaner
alternative, if preferred.

## Step 3: Sandbox side

### Bind-mount only the socket

```sh
# bwrap example
--bind "$XDG_RUNTIME_DIR/claude-clip.sock" /run/claude-clip.sock \
--setenv CLAUDE_CLIP_SOCK /run/claude-clip.sock \
--setenv WAYLAND_DISPLAY wayland-0     # dummy, only if step 1b showed it's needed
```

The real Wayland socket must **not** be mounted. A dummy `WAYLAND_DISPLAY` value
pointing at a non-existent socket is harmless.

### Production shim

Install it under whichever names step 1 showed are used (at minimum `wl-copy`,
and probably `xclip`). Install it as `wl-paste` too, so that reads fail fast.

```sh
#!/bin/sh
# clipshim: write-only clipboard forwarder for sandboxed Claude Code.
sock=${CLAUDE_CLIP_SOCK:-/run/claude-clip.sock}
name=$(basename "$0")

# Refuse every read path.
case "$name" in wl-paste) exit 1 ;; esac
for a in "$@"; do
  case "$a" in
    -o|-out|--output|-p|--paste) [ "$name" = wl-copy ] || exit 1 ;;
  esac
done
# (Adjust the flag list to match what step 1 observed. Note that -p means
#  --primary for wl-copy, but "output" for some xsel invocations.)

# Ignore other args (selection, type, ...); the host decides those.
[ -S "$sock" ] || exit 1
exec timeout 5 socat -u - UNIX-CONNECT:"$sock"
```

- Non-zero exit on any failure, so Claude Code can fall back (if it checks; see 1c).
- `socat` must be available inside the sandbox. If it isn't, a few lines of
  Python, or `nc -U` (OpenBSD netcat), will do.

## Step 4: Test plan

1. Host only: `echo hello | socat -u - UNIX-CONNECT:$XDG_RUNTIME_DIR/claude-clip.sock`,
   then paste into a GNOME app. The result should be `hello`.
2. Inside the sandbox: `echo hello | wl-copy`, then paste on the host.
3. Inside the sandbox: `wl-paste` and `xclip -o` must both **fail** and print nothing.
4. Inside the sandbox, in `claude`: mouse-select text. The toast should no
   longer mention OSC 52, and Ctrl+V on the host should paste the clean selection.
5. Unicode, multi-line selections, and a selection larger than 1 MiB
   (expect truncation, not a hang).
6. Stop the host service, then select in `claude`. The shim should exit non-zero within
   about 5 seconds, and Claude Code should fall back without freezing.
7. Exit the sandbox entirely. The clipboard contents should still be pasteable on
   the host, which confirms that the selection owner is the host-side `wl-copy`.

## Security notes

- The channel is write-only by construction: the host side never sends bytes back.
- A compromised or misbehaving process inside the sandbox can still **set**
  the clipboard at any time. Pastejacking risk: it could replace what you
  copied with a malicious shell command. Possible mitigations:
  - Have the listener call `notify-send` with a preview or byte count on each write.
  - Rate-limit writes, or only accept them while the GNOME Terminal window is
    focused (harder on Wayland).
  - Make the socket available only to the `claude` process, not to the whole
    sandbox. This depends on how the sandbox is structured.
- Keep the socket at mode `0600` under `$XDG_RUNTIME_DIR` (already per-user and `0700`).

## Open questions

- Does Claude Code expose (or will it add) a supported setting for a custom
  clipboard command? That would remove the need for name-squatting shims.
  Worth checking release notes, or filing a feature request in anthropics/claude-code.
- Should image paste (clipboard *read*) ever be supported? It would need
  an explicit, user-initiated read channel. Out of scope for now.

## Findings

READ in Claude Code 2.1.292 (the JavaScript in `/opt/claude-code/2.1.292/claude`,
extracted with `strings` and Python; the names below are minified ones,
so they change from build to build). Not yet RAN: step 1a's logging
shim would confirm it in a live session.

| Question | Observation |
|---|---|
| Binary invoked | Linux: `wl-copy` if `WAYLAND_DISPLAY` is set and `wl-copy` is on `PATH`; else, if `DISPLAY` is set, `xclip`, then `xsel`; else a built-in Rust addon (`clipboard-napi`, wl-clipboard-rs and X11) that speaks the display protocol itself. |
| Probe | `Bun.which(NAME)`: a `PATH` lookup only, with no exec, no `--version`. It runs once, early (when the TUI mounts), and the result is cached for the process. |
| Arguments | `wl-copy` (no arguments), then, after it exits, `wl-copy --primary`. `xclip -selection clipboard` and `xclip -selection primary`, both at once. `xsel --clipboard --input` and `xsel --primary --input`. |
| Data via stdin? | Yes, the text as UTF-8; nothing in argv. |
| Env vars required to trigger | `WAYLAND_DISPLAY` (any value) for `wl-copy`, or `DISPLAY` for `xclip`/`xsel`. Also **no `SSH_CONNECTION`**: with it set, Claude Code calls no native tool at all and uses OSC 52 alone (and the tmux buffer, if `TMUX` is set). |
| Exit status checked? | **No.** Calls are fire-and-forget, with a 2-second timeout, and stdout and stderr ignored. Only the addon's result counts: a failed addon write makes it probe again. |
| Waits for exit? | Only to order the two `wl-copy` calls (the `--primary` one is skipped if a newer copy started meanwhile). The UI never waits. A shim that is still running after 2 s is killed. |
| What decides the toast | The probe, not the result. If the probe found a tool (and no SSH), the toast says "copied N chars to clipboard", even when the tool then fails. Otherwise "sent N chars via OSC 52". |
| OSC 52 | **Always sent as well**, after the native tool, whatever the probe found. On VTE it is dropped, so it does no harm. |
| Clipboard reads attempted? | Yes. Text paste: `wl-paste --no-newline`, then `xclip -selection clipboard -o`, then `xsel --clipboard --output`, then the addon; the first exit 0 wins. Image paste (shell lines): `xclip -selection clipboard -t TARGETS -o \|\| wl-paste -l`, then `xclip ... -t image/png -o \|\| wl-paste --type image/png` (and `image/bmp`). A shim that exits 1 for these makes them fail fast. |
| Other writes | The screenshot copy (`/screenshot`-style) runs `xclip -selection clipboard -t image/png -i FILE`, **xclip only**, and reports failure. A `wl-copy`-only shim leaves it failing as it does now. |

How a paste reaches Claude Code (READ in 2.1.292):

- **Keyboard paste** (Ctrl+Shift+V, or Edit > Paste, in the terminal):
  the terminal reads the host's clipboard and types the text into the
  pty between bracketed-paste markers (`ESC [200~` ... `ESC [201~`),
  which Claude Code reads as `paste-start` and `paste-end`. Claude Code
  reads no clipboard, so this works in a sandbox today.
- **Mouse paste**: with Claude Code's mouse capture on, the terminal
  sends the clicks to Claude Code and does not paste itself. Claude Code
  does it in their place, on Linux: a middle click reads PRIMARY, and a
  right click with no selection reads CLIPBOARD (`wl-paste`, `xclip -o`,
  `xsel --output`, then the addon), and the text goes in as a paste. In
  a sandbox the read fails, so middle and right click paste nothing.
  Shift+middle click goes to the terminal, which pastes PRIMARY itself.
  A write-only channel cannot fix these.
- **Ctrl+V** (`chat:imagePaste`): reads the clipboard for an image, and
  falls back to a text read. Same as the mouse: it needs a read.

Why `wl-copy` and not `xclip` or `xsel`: any one of them works, and the
host's tool does not have to match the shim's name. `wl-copy` because:

- it needs `WAYLAND_DISPLAY`, which little in a headless VM reads. The
  `xclip`/`xsel` path needs `DISPLAY`, which X clients in the sandbox
  (Chrome for the QUnit and selenium tests, Cypress, `xdg-open`) read as
  "a display is here": Cypress, for one, then uses it instead of starting
  its own Xvfb, and fails;
- its arguments are simple (none, or `--primary`). An `xclip` shim also
  gets the screenshot copy (`-t image/png -i FILE`: a file, and an
  image), and `xsel`'s `-p` means PRIMARY, not "paste";
- Claude Code calls it once per selection and waits for it before the
  `--primary` call; `xclip` and `xsel` are called twice at once.

What `xclip` would add is the screenshot copy, which would need an
image-capable channel to the host.

Consequences for the design above:

- The shim needs the name `wl-copy` and a dummy `WAYLAND_DISPLAY`
  (step 1b's guess is right). `xclip` is needed only for the screenshot
  copy; the `xclip` and `xsel` names also need `DISPLAY`, which would
  also turn on the addon's X11 path, so leave them out.
- With a fake `WAYLAND_DISPLAY` and no `wl-copy` on `PATH`, the addon
  tries the missing socket, fails, and the probe ends at `null`
  (OSC 52). So the dummy variable is safe without the shim, too.
- The shim is called twice per copy (CLIPBOARD, then PRIMARY). Forward
  only the call without `--primary`, or tell the host which one it is.
- A failed forward is silent: the toast still says "copied". The shim
  must finish well within 2 s (connect timeout about 1 s), and the host
  side should be where a failure is reported (a log, or `notify-send`).
- Step 6 of the test plan changes: Claude Code does not fall back to
  OSC 52 when the shim fails, and does not freeze.

## In wmf-sbx

How it applies to a wmf-sbx session (READ in `sbx/src/wmf_sbx`; nothing
built yet):

- **The environment.** Both kinds of session start Claude Code with
  `sudo -H -u agent env -C DIR PATH=... VAR=...` (`vm.agent_argv`), and
  sudo's `env_reset` removes `SSH_CONNECTION`, `DISPLAY`,
  `WAYLAND_DISPLAY`, `TMUX` and `VTE_VERSION`. So Claude Code does not
  see the `limactl shell` SSH connection, and does not take the
  SSH-means-OSC-52-only path. Today it finds no display and no tool, and
  sends OSC 52 alone, which GNOME Terminal drops: the problem as
  reported. `WAYLAND_DISPLAY` would go in through `agent_argv`'s `env`.
- **The transport.** The credential proxy (`credproxy.py`,
  `resume.start_proxy`) already is "a listener per session on the host,
  for a client in the guest", and a clipboard listener can do the same
  in each mode:
  - without the egress proxy (`vz`, `--no-egress-proxy`): a thread on
    the host's loopback at an OS-picked port; the guest connects to
    `192.168.5.2:PORT`; `start_proxy` opens the guest's host block for
    that port, and a contained session gets `--open-port=PORT` for its
    nono;
  - behind the egress proxy: a Unix socket per sandbox (as `cred.sock`)
    and a fourth guestfwd on `192.168.5.100` (a new port next to
    `CRED_PORT` 3129, in `egress.guestfwds`). The guestfwds are fixed
    when QEMU starts, so an existing sandbox gets it at its next
    `start`. The guest's nftables rules for 192.168.5.100 need the new
    port.
  This is a TCP port in the guest, not a bind-mounted socket: the VM
  has no host mounts other than the read-only repos. Anything in the VM
  that can reach the port can set the clipboard: in a `--sudo` sandbox
  that is any process; in a contained one, the agent's processes that
  nono lets reach it.
- **The listener's lifetime.** The listener can stop with the session
  (as the credential proxy does): the clipboard owner is the host's
  `wl-copy`, which forks and serves the selection after the listener
  exits (test 7 of the plan).
- **The host side.** Linux on Wayland: `wl-copy`; on X11: `xclip
  -selection clipboard`; macOS: `pbcopy`. wmf-sbx is Python on the host,
  so the listener is a thread in `resume`, not socat or a systemd unit.
  Cap the size (1 MiB) and log a line per write (bytes, never the text).
- **The shim.** A `wl-copy` script in the image (on `AGENT_PATH`), with
  `nc` or bash's `/dev/tcp` as the client (`socat` is in this image;
  check that the golden image has one of them). Read the address from
  a variable that `launcher_argv` sets (a `WMF_CLAUDE_*` name: the
  sandbox sees it). Exit 1 under the name `wl-paste`.
- **Nested sandboxes.** An inner session would forward to the outer
  session's shim address, as the credential proxy chains to the outer
  one. Later.

Measurements on the engineer's host (Ubuntu 24.04, GNOME on Wayland,
GNOME Terminal), RAN 2026-10-15:

- `echo hi | wl-copy`: the text goes to CLIPBOARD (Ctrl+V), not PRIMARY
  (middle click), as documented; `echo hi | wl-copy --primary` sets
  PRIMARY. So the host can do both of Claude Code's calls: the shim
  tells it which one each connection is.
  The command returns at once, and the text still pastes after it: the
  forked owner serves it. So the listener runs `wl-copy` and does not
  wait on it.
- The same from a process with no terminal (`setsid sh -c 'sleep 3;
  echo bg | wl-copy' </dev/null >/dev/null 2>&1 &`, the focus in another
  window): works. GNOME 46 has no data-control protocol, and this was
  the risk for a listener with no window; it holds.
- `xclip -selection clipboard` works too (the engineer uses it every
  day): a fallback on the same host, through XWayland.

Still to measure before building (the egress-proxy case first):

- the logging shim in a live session (step 1a), with `WAYLAND_DISPLAY`
  from the agent's settings `env`: the toast says "copied", and two
  calls arrive (plain, then `--primary`). If settings `env` comes after
  Claude Code's probe, the launcher must set the variable;
- from a child of Claude Code in a contained egress session: a TCP
  connection to `192.168.5.100:3129` (opened with `--open-port`) works,
  one to an unopened port is refused, and the round trip through the
  guestfwd's per-connection `socat` is well inside 2 s.

Open questions:

- Does nono (`--landlock-only` and the proxy mode) let the shim, a
  child of Claude Code, connect to `192.168.5.2:PORT` with only
  `--open-port`? It does for Claude Code's own credential-proxy
  traffic; check it for a child process.
- VTE: https://gitlab.gnome.org/GNOME/vte/-/issues/2495 is still open
  (READ 2026-10-15, last updated 2026-09-25; the API lists comments
  only with a login). If VTE implements it, GNOME Terminal fixes the
  copy with no code.
- Paste with the mouse, and Ctrl+V, need a clipboard read (above).
  Keep them broken (keyboard paste and Shift+middle click work), or
  add a read channel that the engineer controls?
- The pastejacking note in "Security notes" applies as written. A
  per-write desktop notification would also show a write that the
  engineer did not make.
- Claude Code has no setting for a clipboard command (the probe list
  above is fixed in the code). A feature request for one (for example
  an env var naming a copy command) would remove the name squatting.
