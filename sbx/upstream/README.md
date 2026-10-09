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

A wmf-claude fix that is ready goes upstream as a GitLab merge request,
not as a task here. Two former tasks are gone for that reason:

- The `find -exec` deny spelling and the dead `Write(...)` denies:
  upstream made the same fix, so the task and its patch are deleted.
  Only the Linux glob question (task 4) is left.
- `run-tests` and `vendor/bin/phpunit`: this is MR D, sent as
  [!130](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/130)
  (see `sbx/NOTES.md` §104). The `lima-port` branch carries !130, so
  `sbx/patches/plugin/01-run-tests-composer-entrypoint.patch` is
  deleted. The kit now ships upstream's `run-tests` unchanged.
