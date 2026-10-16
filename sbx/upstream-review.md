# Upstream review: 70712f6..21bc986 compared with wmf-sbx (835242c)

Written 2026-10-16 by a review agent, against `lima-port` at 835242c,
before the rebase onto 21bc986. Done since: the (c) items, in the rebase
and in 9339a31 (`--landlock-only`). The (a) and (b) items to do are in
`FROM-UPSTREAM.md`. The commit hashes of our side are from before the
rebase.

Scope: 38 upstream commits. All except 3982f3a are Lima VM work by
Kosta Harlan. Our side: `sbx/` at 835242c and our shared-file commits
(9cb7e4a, d4fc0b5, 7298689, f620e93, 8f8aeda, cc754a2, 4ea5744, c0cd91e,
cee1064, 9ec14f8, 64b2f9d, b6feb58, 2f822af). I used read-only git
commands for all of the checks.

Categories: (c) breaks us after the rebase, (a) new and useful,
(b) redundant with what we have, (d) not relevant.

---

## (c) Breaks us, or needs a change after the rebase

1. **`--landlock-only` is removed (eb4961d).** The parent is fixing this.
   - `bin/claude:201` now exits 1 on the flag.
   - Our session passes it at `sbx/src/wmf_sbx/session.py:358`.
   - Our session also writes `landlock-only-ack` at `session.py:690-698`.
     Remove that write; the file now does nothing.
   - `bin/claude:284-299` refuses localhost-port flags on Linux when
     nono is older than 0.80.

2. **nono pin 0.78.0 → 0.80.0 (7097fb9).**
   - `image.py:66,225,376` reads `.nono-version` into the image key. The
     rebase changes the key, so the next `create` builds a new golden
     image. That is correct, but it costs time.
   - Without `--landlock-only`, nono's proxy-mode seccomp layer is active
     again in our sessions. This affects:
     - `--upstream-proxy` combined with `--allow-domain "*"`
       (`session.py:352-355`);
     - `--listen-port 0`;
     - the `--local-web` range ports (8f8aeda).

     None of these have been run under 0.80. **Before you merge, run one
     session: a wiki curl on the port window, a Gerrit fetch, a
     read-only POST that gets 403, and the `/login` callback.**
   - Docs that describe `--landlock-only` as current are now wrong:
     - `sbx/SECURITY.md:116-` (the §3 rationale; the risk is now smaller);
     - `sbx/README.md:258`;
     - `sbx/MACOS.md:378,388`;
     - `sbx/NOTES.md:1120,1317`;
     - the `session.py:62` comment.

3. **`bin/wmf-claude-setup` conflicts with f620e93 (!131).**
   - Upstream moved the CDP `mcp-config.json` write to
     `bin/wmf-claude-build:126-145`. Setup now only checks it
     (setup:228).
   - Upstream added `WMF_CLAUDE_PHAB_USER` (setup:178-185).
   - When `WMF_CLAUDE_IN_VM` is set or stdin is not a terminal, upstream
     keeps the recalled username without a prompt.
   - Re-apply !131 on upstream's version of the file, or drop it (see
     (b)3).

4. **`bin/session-start.sh` conflicts with 7298689 (the hooks/context
   seam).**
   - Upstream added two inline blocks: the Chrome text
     (`WMF_CLAUDE_CHROME_IN_VM`, :21-) and a VM block (`WMF_CLAUDE_IN_VM`,
     :69-).
   - The working tree already shows `hooks/context/nono/chrome{,-vm}.txt`,
     so the parent is moving the Chrome text into the seam.
   - **Do not set `WMF_CLAUDE_IN_VM` in our sessions.** Today we set it
     only for the setup call (`session.py:705`). If a session had it,
     two things would go wrong:
     - the hook would give the wrong advice: `php -S 127.0.0.1:8080`
       and MariaDB `wikiuser`/`wikipass`, while we serve the wiki with
       `composer serve` on `$MW_SERVER` and use SQLite;
     - `bin/claude:1259` would show "isolated VM" in the status line.
   - Keeping the variable in the setup call is now useful: no
     username prompt, and no `claude-vm` alias in the guest.

5. **`bin/wmf-claude-vm` is a 3-line shim (0b85dc4).** Our 399-line copy
   from 9cb7e4a and d4d0dd7 (its shellcheck fixes) is out of date. Take
   upstream's version.
   - Stale comment: `image.py:795` says "as bin/wmf-claude-vm does it".
     Change it to `bin/claude-vm`.

6. **`lima/guest-install.sh`:** our 32fbab1 (`bash -o pipefail -c 'curl
   … | bash'`) is not upstream (upstream :186). It still passes
   `tests/test-lima.sh:116-122`, because the check uses a substring
   match and counts one pipe. Re-apply it, and propose it as a small MR.

7. **`skills/manual-test/SKILL.md`:**
   - Upstream changed lines 28 and 75 (the VM wiki and Chrome text).
   - Our !134 (the `/w` change) is not in 835242c's diff, so I expect no
     conflict on our branch. Check this again when !134 is rebased.

Upstream already has the other parts of our 9cb7e4a:
- `WMF_DOCKER_HANDSHAKE` (`bin/claude:163,740`);
- `lsof -O` (`:771`);
- `/tmp/claude-$UID`, now with a symlink check (`:1311-1318`).

Take upstream's versions of these. The symlink check is better than ours.

## (a) New and useful: how to adopt

1. **`lima/claude-context.py` (a59ed4b): carry memory and sessions
   between host and VM.**
   - wmf-sbx has nothing like this. `resume.py` resumes in the VM only.
   - Our repos are at the same paths as the engineer's, so no `--map`
     path rewrite is necessary. Only the home prefix differs.
   - The script is Python 3.9, has a command-line interface
     (`stage|emit|receive|merge|memdiff`) and validates names. We can
     call it from `create`/`resume` (push) and `sbx-pull`/`rm`
     (pull: stage only, never write host memory).
   - Quick fix: `resume.py:121-124` encodes the project directory without
     the over-200-characters hash rule. Use `enc()` from claude-context.py.

2. **Unpulled-work report (61dd761, ea18f80).**
   - We report unfetched tips (`remotes.unfetched_tips`, `rm.py:59`).
   - We do not report workspaces with uncommitted files.
   - Add one read-only `git status --porcelain` pass, as the agent, to
     `rm` and to the end of a session.
   - Our printed names are refnames only, and git refnames cannot contain
     control characters. If we print file names or commit subjects,
     strip control characters, as upstream does.

3. **Headless Chromium for `--chrome` (d7dfe34, `lima/guest-chrome.sh`).**
   - Upstream runs Chromium as a separate `wmfchrome` user, with a
     per-uid nftables reject.
   - This would give us Tier 2 (the chrome-devtools MCP). Today we set
     `WMF_CLAUDE_SKIP_CHROME=1`.
   - To adopt it we need an image user and a per-uid rule in
     `sandbox-provision.sh`. Effort: medium.

4. **nftables `ct direction reply` (c907cbd).**
   - Upstream changed its lock to
     `ct state established,related ct direction reply accept`.
   - Our host block uses the plain form at `sandbox-provision.sh:103,117`.
   - The change stops a connection that started before the rules loaded
     from surviving them. Our rules load at boot, so the gain is small,
     but the change is one line per chain. RAN-test it before you adopt it.

5. **Request-body policy and transcript capture in the model proxy
   (c907cbd, 25c2902).**
   - `authproxy.py:100` denies only the `/v1/mcp_servers` paths.
   - It does not refuse an `mcp_servers` field or server tools in the
     body of `/v1/messages`. Through these, the Anthropic servers can
     fetch URLs, which bypasses our egress allowlist.
   - This is a judgment call: refusing server tools stops Claude Code's
     WebSearch. An opt-in setting, or refusing `mcp_servers` alone, is
     the safe first step.
   - Transcript capture on the host is optional for us: our VMs are
     disposable, and `resume.py` needs the transcript in the VM.

6. **Terminal env vars (3982f3a).**
   - The profile now passes `TERM_PROGRAM`, `TERM_PROGRAM_VERSION` and
     `COLORTERM`.
   - `session.py:361` passes only our `WMF_CLAUDE_*` vars. Copy these
     three from the host env into `env`. Low priority.

7. **`/etc/php` read grant (4f36b8a).** This partly closes our NOTES item
   "grant the Linux system paths". Remove `/etc/php` from our session
   grants if we have it there.

## (b) Redundant: compare, and say which way to converge

1. **Contained mode (da47934, 233cf1d, `wmf-claude-model-proxy`,
   `profiles/wmf-contained.json`) compared with credproxy/authproxy.**
   - Both keep the real token on the host. Upstream uses a placeholder
     OAuth token and `ANTHROPIC_BASE_URL`; we use phantoms behind a
     transparent proxy.
   - Upstream allows no egress at all. We allow egress through an
     allowlist.
   - Keep ours. Take the policy ideas in (a)5.
   - Our mode is neither of upstream's two modes: the token stays on
     the host, and egress goes through an allowlist. Say this in
     `sbx/README.md`, so readers do not assume that wmf-sbx means
     "isolated".

2. **Nested push/pull (0c98ee2, 69876ca) compared with `wmfsbx://`
   remotes and `sandbox-repos.sh`.**
   - Ours is native git: `fetch dev`, `sbx-pull`, completions.
   - Theirs uses bundles in a tar, and the guest can send only an index
     into the host's list.
   - Do not converge. Both have the same security property: the host
     chooses the names.

3. **Phabricator username: `WMF_CLAUDE_PHAB_USER` (upstream) compared
   with config.json (!131).**
   - `create.py:495-508` already falls back to parsing
     `claude mcp get phabricator`.
   - So `session.py` can pass `WMF_CLAUDE_PHAB_USER=<username>` to
     setup. It can then stop writing `config.json` into the VM and stop
     piping `"\n"` (`session.py:671-708`).
   - Then !131 is needed only on a host where `claude mcp get` does not
     work. Consider withdrawing !131, which removes conflict (c)3.

4. **PHP, Composer and MariaDB in the guest (4f36b8a) compared with our
   image.**
   - Our image builds php-ast the way CI does, and installs a SQLite
     wiki.
   - Keep ours. MariaDB is not necessary.

5. **Modes, `list` and `--vm` (0b85dc4) compared with the `wmf-sbx`
   verbs (`ls`, `status`, `create`).**
   - These are parallel CLIs. Do not converge.

## (d) Not relevant to wmf-sbx

- The Docker broker hardening (NET_ADMIN, host network, binds). We do
  not use the broker now. We inherit the change with no effect.
- The Landlock ABI V6 check and `init.defaultBranch=main` in the
  workspace clones. Both apply to upstream's own VM.
- The `claude-vm` alias and the docs reorganisation (07d155e, edc88ca).
- The racy `vm_exists` fix in 4f36b8a. That is upstream's code. Our
  `lima.py` uses Python, not `grep -q`.

## Order of work

1. Do (c)1, (c)2 (with the RAN session test and the doc updates), (c)3,
   (c)4 and (c)5 in the rebase.
2. Do (b)3 (`WMF_CLAUDE_PHAB_USER`) and (a)1's `enc()` fix: both are
   small.
3. Do (a)2 (the dirty-workspace report) and (a)1 (memory carry) next.
4. Discuss with the engineer before you do (a)3 and (a)5.
