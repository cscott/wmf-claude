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
not as a task here. Those already sent or merged:

- The `find -exec` deny spelling and the dead `Write(...)` denies:
  fixed upstream by
  [!106](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/106)
  and
  [!112](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/112).
  Only the Linux glob question (task 4) is left; the caveat it questions
  came with
  [!57](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/57).
- `run-tests` and `vendor/bin/phpunit`:
  [!130](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/130)
  (open). The `lima-port` branch carries it.
- The SessionStart backend seam and the Phabricator username in
  `config.json`:
  [!132](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/132)
  and
  [!131](https://gitlab.wikimedia.org/repos/product-safety-and-integrity/wmf-claude/-/merge_requests/131)
  (open). The `lima-port` branch carries them.
