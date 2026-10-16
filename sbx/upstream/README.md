# Upstream tasks

Bugs this work found in code outside `sbx/`, written as Phabricator tasks
for the engineer to file. Each `PHAB-TASK-<n>.md` follows the
`/wmf-claude:write-phab-task` template; `PHAB-ATTACHMENT-<n>.patch` is its
fix, as a `git diff` against the named repo. Every patch was applied and
tested as the task says.

| n | Project | Bug | Patch against |
|---|---|---|---|
| 1 | MediaWiki-REST-API | header parameters looked up by exact case | mediawiki/core |
| 2 | wdio-mediawiki | empty `MW_SCRIPT_PATH` rejected | mediawiki/core `tests/selenium/wdio-mediawiki/` |
| 3 | Popups | node-qunit tests do not run on Node 21+, exit 0 | mediawiki/extensions/Popups `package.json` |
| 4 | wmf-claude | Linux glob caveat may be stale (re-measure first) | — |

When a task is filed, add its T-number to the table. When upstream takes
a fix, delete the pair.

Issues for other projects, for the engineer to file:

| File | Project | Bug | Issue |
|---|---|---|---|
| `NONO-ISSUE-1.md` | always-further/nono | the OAuth capture store is read once and written whole: proxies that share it lose phantoms (track L6). Seen with 0.79; check 0.80 (the new pin) before filing | — |

Handoffs: bugs for an agent to fix in a sandbox of its own, which then
files the task and the change. When cananian gives the Gerrit and
Phabricator numbers, record them in `sbx/NOTES.md`, and delete the
handoff when the fix merges.

| Handoff, issue | Repo of the fix | Bug | Gerrit / task |
|---|---|---|---|
| `HANDOFF-CORE.md` 1 | mediawiki/core | `SchemaGenerator::normalizePath()` leaves the path absolute for a symlinked extension: 11 AbuseFilter schema tests fail | [1355223](https://gerrit.wikimedia.org/r/c/mediawiki/core/+/1355223) |
| `HANDOFF-CORE.md` 2 | mediawiki/core | api-testing `ReparseDependentPages.js` expects the `?title=` red-link URL; Parsoid read views write `/index.php/X?action=edit` | [1355226](https://gerrit.wikimedia.org/r/c/mediawiki/core/+/1355226), [1355227](https://gerrit.wikimedia.org/r/c/mediawiki/core/+/1355227) |
| `HANDOFF-CORE.md` 3 | mediawiki/core | `UndoIntegrationTest::testMcrUndoAction` has no title in its context; EventLogging's hook dereferences it | [1355229](https://gerrit.wikimedia.org/r/c/mediawiki/core/+/1355229) |
| `HANDOFF-CORE.md` 4 | mediawiki/core | `WikiBirthdayTest` does not clear the `GetHumanTimestamp` hook, which cldr handles | [1355230](https://gerrit.wikimedia.org/r/c/mediawiki/core/+/1355230) |

A wmf-claude fix that is ready goes upstream as a GitLab merge request,
not as a task here.

**Merged** (upstream `main` 21bc986; `lima-port` is rebased on it):

- The `find -exec` deny spelling and the dead `Write(...)` denies:
  [!106](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/106)
  and
  [!112](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/112).
  Only the Linux glob question (task 4) is left; the caveat it questions
  came with
  [!57](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/57).
- The Lima VM mode that `lima-port` started from:
  [!127](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/127),
  as 4c89180, and much more since (`bin/claude-vm`, contained mode;
  `sbx/FROM-UPSTREAM.md`). Upstream also took our fixes for the Docker
  handshake, `lsof -O` and `/tmp/claude-$UID`. Our old copy of the MR
  commit is gone from `lima-port`.
- `run-tests` and `vendor/bin/phpunit`:
  [!130](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/130).
- **`WMF_CLAUDE_STE=0`** leaves the ASD-STE100 writing bullet out of the
  SessionStart context:
  [!133](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/133).

**Open** (`lima-port` carries !131 and !132, changed in the rebase; the MR
branches do not have those changes yet):

- The SessionStart backend seam:
  [!132](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/132).
  Upstream has since added inline browser text for its VM; on
  `lima-port` it is in `hooks/context/nono/chrome.txt` and
  `chrome-vm.txt`, chosen by `WMF_CLAUDE_CHROME_IN_VM`. Update the MR
  branch to match.
- The Phabricator username in `config.json`:
  [!131](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/131).
  Upstream's `WMF_CLAUDE_PHAB_USER` now does most of it. Withdraw it, or
  rebase it on upstream's setup flow, as `lima-port` has it.
- **The manual-test skill's `/w` path**: a docroot install (CI's, a
  `composer serve` wiki, a wmf-sbx sandbox) has its scripts at the root.
  [!134](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/134),
  from branch `manual-test-script-path` on
  [cscott/wmf-claude](https://github.com/cscott/wmf-claude/tree/manual-test-script-path),
  one commit on upstream `main` 70712f6. Rebase it on 21bc986: upstream
  changed `skills/manual-test/SKILL.md` lines 28 and 75 (its VM text).
  `lima-port` does not carry it; the sandbox's CLAUDE.md has its own
  "Manual testing" section.

**To send** (commits on `lima-port` that touch only shared files and
stand without sbx: a small MR on upstream `main` each, or a change to an
open MR):

- **pipefail on the Claude Code install** (`lima: Stop guest-install.sh
  when the Claude Code download fails`). `lima/guest-install.sh` runs
  `curl … | bash`; without `pipefail`, a failed download gives an empty
  script, bash exits 0, and the install goes on with no Claude Code.
  Upstream's line 186 still lacks it. `tests/test-lima.sh` does not catch
  it: its check matches a substring.
- **The test environment fix** (`tests: Keep the session-start and status
  line tests off the caller's env`). The byte-for-byte fixture tests
  fail when the caller exports `WMF_CLAUDE_STE` (a session with
  `WMF_CLAUDE_STE=0`) or one of upstream's new VM variables
  (`WMF_CLAUDE_IN_VM`, `WMF_CLAUDE_CHROME_IN_VM`,
  `WMF_CLAUDE_MODEL_PROXY_PORT`). The fixtures come with !132, so this
  part goes into !132, with the commit's CLAUDE.md line. The status line
  part (`WMF_CLAUDE_SANDBOX_BACKEND`) tests our sbx code and stays on
  `lima-port`.
- **shellcheck in `bin/claude-vm`** (`lima: Fix the shellcheck findings in
  the VM scripts`): `tests/test-lima.sh` fails where shellcheck is
  installed (0.11.0: SC2016 in `bin/claude-vm`, SC1091 in
  `lima/guest-install.sh`). CI has no shellcheck, so CI does not see it.
- **`--local-web=LO-HI` ranges in `bin/claude`** (part of `sbx: Let the
  browser tools listen on local ports in a session`). Split the
  `bin/claude` and `tests/test-templates.sh` hunks into a commit of
  their own first. The case without sbx: karma, chromedriver and Cypress
  listen on ports that a fixed list cannot name.
